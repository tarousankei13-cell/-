"""運用コマンド（管理者・オーナー用）"""

from __future__ import annotations

import io
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

import emoji as E
from core import ledger as L
from core import saga
from core import users as user_repo
from db.models import User
from db.session import session_scope, user_scope
from cogs._checks import admin_only, handle_check_failure, owner_only
from ui import admin_flows, embeds

log = logging.getLogger("bot.cogs.admin")


class AdminCog(commands.Cog):
    """統計・元帳・利用者管理・保守"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    admin = app_commands.Group(name="admin", description="運用操作（管理者用）")
    stats_group = app_commands.Group(name="stats", description="統計（管理者用）")
    menu_group = app_commands.Group(name="menu", description="メニュー（管理者用）")
    debug_group = app_commands.Group(name="debug", description="調査用（管理者用）")

    # -- 統計 ---------------------------------------------------

    @stats_group.command(name="show", description="全体の統計を表示します")
    @admin_only()
    async def stats_show(self, interaction: discord.Interaction) -> None:
        await admin_flows.show_stats(interaction)

    @stats_group.command(name="ledger", description="元帳の整合性と未使用残高を確認します")
    @admin_only()
    async def stats_ledger(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            report = await L.verify_integrity(s)
            outstanding = await L.outstanding_user_balance(s)
            settlement = await L.balance(s, L.SETTLEMENT)
            pool = await L.balance(s, L.SUBSIDY_POOL)

        e = discord.Embed(
            title=f"{E.CHART} 元帳",
            color=embeds.GREEN if report.ok else embeds.RED,
        )
        e.add_field(
            name="整合性",
            value=(
                f"{E.OK} 正常（{report.checked} 取引すべてで貸借一致）"
                if report.ok
                else f"{E.NG} **不整合があります**"
            ),
            inline=False,
        )
        if report.broken:
            e.add_field(
                name="貸借が一致しない取引",
                value="\n".join(f"`{t}` 差額 {d:+,}" for t, d in report.broken[:10])[:1024],
                inline=False,
            )
        if report.negative:
            e.add_field(
                name="マイナス残高",
                value="\n".join(f"`{a}` {b:,}" for a, b in report.negative[:10])[:1024],
                inline=False,
            )
        e.add_field(name=f"{E.WALLET} 利用者の未使用残高", value=f"**{embeds.yen(outstanding)}**", inline=True)
        e.add_field(name="マクドナルドへの支払累計", value=embeds.yen(settlement), inline=True)
        e.add_field(name="負担プール", value=embeds.yen(pool), inline=True)
        if outstanding >= 10_000_000:
            e.add_field(
                name=f"{E.WARN} 注意",
                value=(
                    "未使用残高が1,000万円を超えています。\n"
                    "前払式支払手段の届出が必要になる場合があります。"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=e, ephemeral=True)

    @stats_group.command(name="account", description="アカウント別の使用状況")
    @admin_only()
    async def stats_account(self, interaction: discord.Interaction) -> None:
        await admin_flows.show_accounts(interaction)

    # -- 利用者 -------------------------------------------------

    @admin.command(name="grant", description="利用者の残高を増減します")
    @app_commands.describe(user="対象", amount="増減額（マイナスで減算）", reason="理由（元帳に残ります）")
    @admin_only()
    async def grant(
        self, interaction: discord.Interaction, user: discord.User, amount: int, reason: str
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await user_repo.get_or_create(user.id)
        try:
            async with user_scope(user.id) as s:
                await L.adjust(s, user.id, amount, memo=f"{reason}（{interaction.user}）")
                balance = await L.user_balance(s, user.id)
        except L.LedgerError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=embeds.ok(
                f"{user.mention} の残高を **{amount:+,}円** しました。\n"
                f"現在の残高: **{embeds.yen(balance)}**"
            ),
            ephemeral=True,
        )

    @admin.command(name="ban", description="利用者の利用を停止します")
    @admin_only()
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: str = "") -> None:
        async with session_scope() as s:
            u = await user_repo.ensure_user(s, user.id)
            u.is_banned = True
            u.note = reason or u.note
        await interaction.response.send_message(
            embed=embeds.ok(f"{user.mention} の利用を停止しました。"), ephemeral=True
        )

    @admin.command(name="unban", description="利用停止を解除します")
    @admin_only()
    async def unban(self, interaction: discord.Interaction, user: discord.User) -> None:
        async with session_scope() as s:
            u = await user_repo.ensure_user(s, user.id)
            u.is_banned = False
        await interaction.response.send_message(
            embed=embeds.ok(f"{user.mention} の利用停止を解除しました。"), ephemeral=True
        )

    @admin.command(name="review", description="要確認の注文を処理します")
    @admin_only()
    async def review(self, interaction: discord.Interaction) -> None:
        await admin_flows.show_review(interaction)

    @admin.command(name="achievement", description="実績を代理で送信します")
    @app_commands.describe(
        user="実績の対象となる利用者",
        list_price="定価（円）",
        subsidy_rate="管理者の負担率(%)。省略するとその利用者の設定を使います",
        user_amount="利用者の支払額（円）。省略すると定価と負担率から計算します",
        store_name="店舗名（表示項目がONのときだけ出ます）",
        receipt_number="注文番号（表示項目がONのときだけ出ます）",
        pickup="受取方法（表示項目がONのときだけ出ます）",
        daily_count="本日の通算件数。省略すると実際の件数を使います",
    )
    @app_commands.choices(
        pickup=[
            app_commands.Choice(name="テイクアウト", value="takeOut"),
            app_commands.Choice(name="店内（カウンター受取）", value="eatIn"),
            app_commands.Choice(name="店内（席まで）", value="tableDelivery"),
            app_commands.Choice(name="駐車場で受け取る", value="curbsidePickUp"),
            app_commands.Choice(name="ドライブスルー", value="driveThru"),
            app_commands.Choice(name="デリバリー", value="addressDelivery"),
        ]
    )
    @admin_only()
    async def achievement(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        list_price: int,
        subsidy_rate: app_commands.Range[float, 0.0, 100.0] | None = None,
        user_amount: int | None = None,
        store_name: str | None = None,
        receipt_number: str | None = None,
        pickup: app_commands.Choice[str] | None = None,
        daily_count: int | None = None,
    ) -> None:
        """
        実績を代理で送信する。

        見た目は通常の実績とまったく同じ。表示項目も
        /config achievement_fields の設定にそのまま従う。
        （誰が代理送信したかは、埋め込みには出さずサーバーのログにだけ残す）
        """
        from core import subsidy as subsidy_mod
        from core import users as user_repo
        from services.mcd.protocol import PICKUP_LABEL
        from ui import flows

        await interaction.response.defer(ephemeral=True, thinking=True)

        if list_price < 0:
            await interaction.followup.send(
                embed=embeds.error("定価は0円以上で指定してください。"), ephemeral=True
            )
            return

        await user_repo.get_or_create(user.id)

        # 負担率を省略したら、その利用者に実際に適用される率を使う
        if subsidy_rate is None:
            member = interaction.guild.get_member(user.id) if interaction.guild else None
            role_ids = [r.id for r in getattr(member, "roles", [])]
            async with session_scope() as s:
                quote = await subsidy_mod.resolve(s, user.id, role_ids, list_price)
            rate = quote.subsidy_rate
            amount = quote.user_amount
        else:
            rate = float(subsidy_rate)
            amount, _ = subsidy_mod.calculate(list_price, rate)

        if user_amount is not None:
            if user_amount < 0 or user_amount > list_price:
                await interaction.followup.send(
                    embed=embeds.error("支払額は 0円以上・定価以下で指定してください。"),
                    ephemeral=True,
                )
                return
            amount = user_amount

        display_name = getattr(user, "display_name", None) or user.name
        label = PICKUP_LABEL.get(pickup.value, None) if pickup else None

        sent = await flows.send_achievement(
            self.bot,
            discord_id=user.id,
            display_name=display_name,
            list_price=list_price,
            subsidy_rate=rate,
            user_amount=amount,
            store_name=store_name,
            receipt_number=receipt_number,
            pickup_label=label,
            daily_count=daily_count,
        )

        if not sent:
            await interaction.followup.send(
                embed=embeds.error(
                    "実績チャンネルへ送信できませんでした。\n"
                    "`/config channel achievement` でチャンネルを設定し、"
                    "BOTに送信権限があるか確認してください。"
                ),
                ephemeral=True,
            )
            return

        log.info(
            "実績を代理送信しました: 対象=%s(%s) 定価=%s 負担率=%s 支払=%s 実行者=%s(%s)",
            display_name, user.id, list_price, rate, amount,
            interaction.user, interaction.user.id,
        )
        await interaction.followup.send(
            embed=embeds.ok(
                f"{user.mention} の実績を送信しました。\n"
                f"定価 {embeds.yen(list_price)}／負担 {rate:g}%／支払 {embeds.yen(amount)}"
            ),
            ephemeral=True,
        )

    @admin.command(name="backup", description="データベースのバックアップを取り出します")
    @owner_only()
    async def backup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        from services.tasks import make_backup

        try:
            name, data = await make_backup()
        except Exception as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        await interaction.followup.send(
            content=(
                f"{E.OK} バックアップ（{len(data) / 1024:.0f} KB）\n"
                f"{E.INFO} 残高・注文履歴はこのファイルだけで復元できます。\n"
                f"　　登録済みアカウントも戻すには、サーバーの "
                f"`data/encryption_key.txt` も必要です"
            ),
            file=discord.File(io.BytesIO(data), filename=name),
            ephemeral=True,
        )

    # -- メニュー -----------------------------------------------

    @menu_group.command(name="sync", description="メニューを同期します")
    @app_commands.describe(store_id="店舗ID。省略すると使用中の全店舗")
    @admin_only()
    async def menu_sync(self, interaction: discord.Interaction, store_id: str | None = None) -> None:
        from services.mcd import accounts as mcd_accounts
        from services.mcd import stores as mcd_stores
        from services.mcd.client import McdError

        if store_id is None:
            await admin_flows.sync_menus(interaction)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        handle = None
        try:
            handle = await mcd_accounts.pick_account()
            diff = await mcd_stores.sync_menu(handle.client, store_id.strip())
        except McdError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        finally:
            if handle:
                await handle.aclose()

        e = discord.Embed(title=f"{E.SYNC} メニューを同期しました", color=embeds.BLUE)
        e.add_field(name="店舗", value=f"`{store_id}`", inline=True)
        e.add_field(name=f"{E.PLUS} 新商品", value=f"{len(diff.added)} 件", inline=True)
        e.add_field(name=f"{E.MINUS} 販売終了", value=f"{len(diff.removed)} 件", inline=True)
        if diff.price_changed:
            e.add_field(
                name=f"{E.YEN} 価格変更",
                value="\n".join(
                    f"{n}　{embeds.yen(o)} → {embeds.yen(p)}"
                    for _, n, o, p in diff.price_changed[:10]
                )[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=e, ephemeral=True)

    # -- 調査 ---------------------------------------------------

    @debug_group.command(name="hex", description="注文コードの中身を表示します")
    @app_commands.describe(code="注文コード（HEX）")
    @admin_only()
    async def debug_hex(self, interaction: discord.Interaction, code: str) -> None:
        from services.mcd.protocol import PICKUP_LABEL, ProtocolError, decode_hex

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            d = decode_hex(code)
        except ProtocolError as e:
            await interaction.followup.send(
                embed=embeds.error(f"解析できませんでした。\n```{e}```"), ephemeral=True
            )
            return

        e = discord.Embed(title=f"{E.RECEIPT} 注文コードの解析", color=embeds.BLUE)
        e.add_field(name="店舗ID", value=f"`{d.store_id}`", inline=True)
        e.add_field(
            name="受取方法",
            value=f"{PICKUP_LABEL.get(d.pickup_method or '', '未指定')}（field {d.pickup_method}）",
            inline=True,
        )
        e.add_field(name="合計", value=embeds.yen(d.total_amount), inline=True)

        lines = []
        for item in d.items:
            lines.append(f"`{item.product_code}` ×{item.quantity}　{embeds.yen(item.amount)}")
            for comp in item.components:
                for leaf in comp.walk():
                    depth = 1 if leaf is comp else 2
                    lines.append("　" * depth + f"└ `{leaf.product_code}`")
        e.add_field(name=f"商品（{len(d.items)} 点）", value="\n".join(lines)[:1024] or "—", inline=False)
        if d.redirect_urls:
            e.add_field(name="リダイレクトURL", value="\n".join(d.redirect_urls[:3])[:1024], inline=False)
        e.set_footer(text=f"{len(d.raw_hex) // 2} バイト")
        await interaction.followup.send(embed=e, ephemeral=True)

    # -- 保守 ---------------------------------------------------

    @app_commands.command(name="sync", description="スラッシュコマンドを手動で同期します")
    @owner_only()
    async def sync_commands(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.bot.sync_commands(force=True)
        except Exception as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=embeds.ok(
                f"{result}\n\n"
                f"{E.INFO} 反映されない場合は、Discordアプリを再起動してみてください。"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="restart", description="BOTを再起動します")
    @owner_only()
    async def restart(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=embeds.info(f"{E.SYNC} 再起動しています。少々お待ちください。"), ephemeral=True
        )
        log.info("再起動が要求されました（%s）", interaction.user)
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("admin コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
