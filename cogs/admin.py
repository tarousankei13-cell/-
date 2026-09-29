"""Admin commands for McDonald's Concierge Bot."""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from models import (
    EmbedColor,
    OrderStatus,
    TransactionType,
    local_now,
    parse_iso,
    resolve_rank,
    utc_now,
)
from views import (
    PanelView,
    _receipt_file,
    build_order_result_embed,
    build_panel_embed,
    make_receipt_bytes,
    notify_user,
    post_achievement,
    send_achievement,
    send_admin_log,
)

logger = logging.getLogger("bot.admin")

BACKUP_DIR = Path(__file__).parent.parent / "backups"


def _is_owner_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)
    return app_commands.check(predicate)


class OwnerGroup(app_commands.Group):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await interaction.client.is_owner(interaction.user):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="権限エラー",
                    description="この操作は管理者のみ実行できます。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return False
        return True


def ascii_bar(value: int, maximum: int, width: int = 14) -> str:
    if maximum <= 0:
        return " " * width
    filled = int(round(value / maximum * width))
    return "█" * filled + "░" * (width - filled)


async def build_report_embed(db, days: int = 7) -> discord.Embed:
    tz = await db.get_int_setting("tz_offset", 9)
    rows = await db.get_daily_stats(days, tz)
    stats = await db.get_stats()

    embed = discord.Embed(
        title="日次レポート",
        description=f"直近 {days} 日間の実績（{local_now(tz).strftime('%Y-%m-%d')} 時点）",
        color=EmbedColor.PRIMARY,
    )

    if rows:
        max_cnt = max(r["cnt"] for r in rows)
        lines = []
        for r in rows:
            lines.append(
                f"{r['d'][5:]}  {ascii_bar(r['cnt'], max_cnt)} "
                f"{r['cnt']:>3}件 ¥{r['revenue']:,}"
            )
        embed.add_field(
            name="日別推移", value="```\n" + "\n".join(lines) + "\n```", inline=False
        )
        total_cnt = sum(r["cnt"] for r in rows)
        total_rev = sum(r["revenue"] for r in rows)
        total_sub = sum(r["subsidy"] for r in rows)
        embed.add_field(name="期間注文数", value=f"{total_cnt:,}件", inline=True)
        embed.add_field(name="期間売上", value=f"¥{total_rev:,}", inline=True)
        embed.add_field(name="期間負担", value=f"¥{total_sub:,}", inline=True)
    else:
        embed.add_field(name="日別推移", value="データがありません。", inline=False)

    embed.add_field(name="累計注文", value=f"{stats['completed_orders']:,}件", inline=True)
    embed.add_field(name="累計売上", value=f"¥{stats['total_revenue']:,}", inline=True)
    embed.add_field(name="預り残高", value=f"¥{stats['total_balance']:,}", inline=True)
    embed.add_field(name="未処理入金", value=f"{stats['pending_deposits']:,}件", inline=True)
    embed.add_field(name="要確認注文", value=f"{stats['review_orders']:,}件", inline=True)
    embed.add_field(name="ユーザー数", value=f"{stats['user_count']:,}", inline=True)

    top = await db.get_top_stores(5)
    if top:
        embed.add_field(
            name="人気店舗",
            value="\n".join(f"{i}. {t['store']} — {t['cnt']}件"
                            for i, t in enumerate(top, 1)),
            inline=False,
        )
    embed.set_footer(text="McDonald's Concierge")
    return embed


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    # ── トップレベル ─────────────────────────────────────────

    @app_commands.command(name="setup_panel", description="パネルを設置します")
    @_is_owner_check()
    async def setup_panel(self, interaction: discord.Interaction) -> None:
        embed = await build_panel_embed(self.db)
        msg = await interaction.channel.send(embed=embed, view=PanelView())  # type: ignore[union-attr]
        await self.db.save_panel(
            interaction.guild_id, interaction.channel_id, msg.id  # type: ignore[arg-type]
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="パネル設置完了",
                description="パネルを設置しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="sync", description="スラッシュコマンドを同期します")
    @app_commands.describe(scope="同期範囲")
    @app_commands.choices(scope=[
        app_commands.Choice(name="グローバル（推奨・重複しません）", value="global"),
        app_commands.Choice(name="重複を修復（コマンドが2つずつある場合）", value="repair"),
        app_commands.Choice(name="このサーバーのみ即時反映（テスト用）", value="guild"),
    ])
    @_is_owner_check()
    async def sync_cmd(
        self, interaction: discord.Interaction, scope: str = "global"
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            if scope == "repair":
                if interaction.guild is None:
                    await interaction.followup.send(
                        "サーバー内で実行してください。", ephemeral=True
                    )
                    return
                # ギルド専用コマンドを消してグローバルのみに統一する
                self.bot.tree.clear_commands(guild=interaction.guild)
                await self.bot.tree.sync(guild=interaction.guild)
                synced = await self.bot.tree.sync()
                desc = (
                    f"このサーバーの重複コマンドを削除し、"
                    f"グローバルに {len(synced)} 件を再同期しました。\n"
                    "反映まで数十秒かかる場合があります。"
                )
            elif scope == "guild":
                if interaction.guild is None:
                    await interaction.followup.send(
                        "サーバー内で実行してください。", ephemeral=True
                    )
                    return
                self.bot.tree.copy_global_to(guild=interaction.guild)
                synced = await self.bot.tree.sync(guild=interaction.guild)
                desc = (
                    f"このサーバーに {len(synced)} 件を同期しました。\n"
                    "⚠ グローバル側にも同じコマンドが残っている場合、"
                    "一覧に2つずつ表示されます。その場合は "
                    "`/sync repair` を実行してください。"
                )
            else:
                synced = await self.bot.tree.sync()
                desc = f"グローバルに {len(synced)} 件のコマンドを同期しました。"
            await interaction.followup.send(
                embed=discord.Embed(
                    title="同期完了", description=desc, color=EmbedColor.SUCCESS
                ),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="同期失敗",
                    description=f"```{str(exc)[:500]}```",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_text(self, ctx: commands.Context, scope: str = "global") -> None:
        """スラッシュコマンドが壊れた時用のテキスト版同期コマンド。

        !sync          グローバル同期（重複しません）
        !sync repair   コマンドが2つずつ表示される場合の修復
        !sync guild    このサーバーのみ即時反映（テスト用）
        """
        try:
            if scope == "repair" and ctx.guild is not None:
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                synced = await self.bot.tree.sync()
                await ctx.send(
                    f"重複を修復しました。グローバル {len(synced)} 件に統一。"
                )
            elif scope == "guild" and ctx.guild is not None:
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(
                    f"このサーバーに {len(synced)} 件同期しました。"
                    "（重複表示された場合は `!sync repair`）"
                )
            else:
                synced = await self.bot.tree.sync()
                await ctx.send(f"グローバルに {len(synced)} 件同期しました。")
        except discord.HTTPException as exc:
            await ctx.send(f"同期失敗: {str(exc)[:500]}")

    @app_commands.command(name="restart", description="BOTを再起動します")
    @_is_owner_check()
    async def restart(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="再起動中...",
                description="BOTを再起動しています。",
                color=EmbedColor.WARNING,
            )
        )
        logger.info("Restart requested by %s", interaction.user)
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── /admin group ─────────────────────────────────────────

    admin_group = OwnerGroup(name="admin", description="管理者コマンド")

    # ── balance ──────────────────────────────────────────────

    balance_group = OwnerGroup(
        name="balance", description="残高管理", parent=admin_group
    )

    @balance_group.command(name="add", description="残高を加算します")
    @app_commands.describe(user="対象ユーザー", amount="加算額", reason="理由")
    async def balance_add(
        self, interaction: discord.Interaction, user: discord.User,
        amount: int, reason: str = "",
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                "金額は1以上で指定してください。", ephemeral=True
            )
            return
        new = await self.db.add_balance(
            user.id, amount, TransactionType.ADMIN_ADD,
            reason=reason or "管理者加算", operator_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高加算",
                description=(
                    f"**{user.display_name}** に ¥{amount:,} を加算しました。\n"
                    f"新残高: ¥{new:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await notify_user(
            self.bot, user.id,
            discord.Embed(
                title="残高が加算されました",
                description=f"+¥{amount:,}\n現在残高: ¥{new:,}",
                color=EmbedColor.SUCCESS,
            ),
        )
        await send_admin_log(
            self.bot, "残高加算",
            f"管理者 {interaction.user.display_name} が残高を加算しました。",
            対象=user.display_name, 金額=f"+¥{amount:,}",
            新残高=f"¥{new:,}", 理由=reason or "(なし)",
        )

    @balance_group.command(name="remove", description="残高を減算します")
    @app_commands.describe(user="対象ユーザー", amount="減算額", reason="理由")
    async def balance_remove(
        self, interaction: discord.Interaction, user: discord.User,
        amount: int, reason: str = "",
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                "金額は1以上で指定してください。", ephemeral=True
            )
            return
        try:
            new = await self.db.deduct_balance(
                user.id, amount, TransactionType.ADMIN_REMOVE,
                reason=reason or "管理者減算", operator_id=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高減算",
                description=(
                    f"**{user.display_name}** から ¥{amount:,} を減算しました。\n"
                    f"新残高: ¥{new:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await send_admin_log(
            self.bot, "残高減算",
            f"管理者 {interaction.user.display_name} が残高を減算しました。",
            対象=user.display_name, 金額=f"-¥{amount:,}",
            新残高=f"¥{new:,}", 理由=reason or "(なし)",
        )

    @balance_group.command(name="set", description="残高を設定します")
    @app_commands.describe(user="対象ユーザー", amount="設定額", reason="理由")
    async def balance_set(
        self, interaction: discord.Interaction, user: discord.User,
        amount: int, reason: str = "",
    ) -> None:
        if amount < 0:
            await interaction.response.send_message(
                "金額は0以上で指定してください。", ephemeral=True
            )
            return
        new = await self.db.set_balance(
            user.id, amount, reason=reason or "管理者設定",
            operator_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高設定",
                description=f"**{user.display_name}** の残高を ¥{new:,} に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await send_admin_log(
            self.bot, "残高設定",
            f"管理者 {interaction.user.display_name} が残高を設定しました。",
            対象=user.display_name, 新残高=f"¥{new:,}", 理由=reason or "(なし)",
        )

    @balance_group.command(name="view", description="ユーザーの残高を確認します")
    @app_commands.describe(user="対象ユーザー")
    async def balance_view(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        balance = await self.db.get_balance(user.id)
        points = await self.db.get_points(user.id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高確認",
                description=(
                    f"**{user.display_name}**\n"
                    f"残高: **¥{balance:,}**\n"
                    f"ポイント: {points:,}pt\n"
                    f"ID: {user.id}"
                ),
                color=EmbedColor.INFO,
            ),
            ephemeral=True,
        )

    @balance_group.command(name="history", description="取引履歴を確認します")
    @app_commands.describe(user="対象ユーザー", limit="表示件数")
    async def balance_history(
        self, interaction: discord.Interaction, user: discord.User, limit: int = 10
    ) -> None:
        txs = await self.db.get_transactions(user.id, limit=min(limit, 25))
        if not txs:
            await interaction.response.send_message(
                f"{user.display_name} の取引履歴はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"{user.display_name} の取引履歴", color=EmbedColor.DARK
        )
        for tx in txs:
            try:
                tx_type = TransactionType(tx["type"]).display
            except ValueError:
                tx_type = tx["type"]
            sign = "+" if tx["amount"] >= 0 else ""
            embed.add_field(
                name=tx["created_at"][:16],
                value=(
                    f"{tx_type}  {sign}¥{tx['amount']:,}\n"
                    f"残高: ¥{tx['balance_after']:,}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @balance_group.command(name="top", description="残高ランキングを表示します")
    @app_commands.describe(limit="表示件数")
    async def balance_top(
        self, interaction: discord.Interaction, limit: int = 10
    ) -> None:
        rows = await self.db.get_all_balances(limit=min(limit, 25))
        if not rows:
            await interaction.response.send_message(
                "データがありません。", ephemeral=True
            )
            return
        lines: list[str] = []
        for i, (uid, bal) in enumerate(rows, 1):
            try:
                u = await self.bot.fetch_user(uid)
                name = u.display_name
            except Exception:
                name = str(uid)
            lines.append(f"**{i}.** {name} — ¥{bal:,}")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高ランキング",
                description="\n".join(lines),
                color=EmbedColor.PRIMARY,
            ),
            ephemeral=True,
        )

    # ── order ────────────────────────────────────────────────

    order_group = OwnerGroup(
        name="order", description="注文管理", parent=admin_group
    )

    @order_group.command(name="view", description="注文詳細を確認します")
    @app_commands.describe(order_id="注文ID")
    async def order_view(
        self, interaction: discord.Interaction, order_id: int
    ) -> None:
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.response.send_message(
                "注文が見つかりません。", ephemeral=True
            )
            return
        status = OrderStatus(order["status"])
        embed = discord.Embed(title=f"注文 #{order_id:04d}", color=EmbedColor.INFO)
        embed.add_field(
            name="ステータス", value=f"{status.emoji} {status.display}", inline=True
        )
        embed.add_field(name="ユーザーID", value=str(order["user_id"]), inline=True)
        embed.add_field(
            name="店舗",
            value=order.get("store_name") or order.get("store_id") or "不明",
            inline=True,
        )
        embed.add_field(name="定価", value=f"¥{order['total_amount']:,}", inline=True)
        embed.add_field(
            name="ユーザー支払", value=f"¥{order['user_amount']:,}", inline=True
        )
        embed.add_field(
            name="負担額", value=f"¥{order['subsidy_amount']:,}", inline=True
        )
        if order.get("receipt_number"):
            embed.add_field(
                name="受取番号", value=order["receipt_number"], inline=True
            )
        embed.add_field(
            name="受取方法", value=order.get("pickup_method") or "不明", inline=True
        )
        if order.get("rate_used"):
            embed.add_field(
                name="適用負担率", value=f"{order['rate_used']}%", inline=True
            )
        if order.get("coupon_code"):
            embed.add_field(name="クーポン", value=order["coupon_code"], inline=True)
        if order.get("retry_count"):
            embed.add_field(
                name="リトライ", value=f"{order['retry_count']}回", inline=True
            )
        embed.add_field(name="作成日時", value=order["created_at"][:16], inline=True)
        embed.add_field(
            name="実績投稿",
            value="済" if order.get("achievement_posted") else "未",
            inline=True,
        )
        if order.get("error_info"):
            embed.add_field(
                name="エラー情報", value=order["error_info"][:200], inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @order_group.command(name="search", description="注文を検索します")
    @app_commands.describe(user="ユーザー", status="ステータス", limit="表示件数")
    @app_commands.choices(status=[
        app_commands.Choice(name="処理待ち", value="pending"),
        app_commands.Choice(name="処理中", value="processing"),
        app_commands.Choice(name="完了", value="completed"),
        app_commands.Choice(name="失敗", value="failed"),
        app_commands.Choice(name="要確認", value="manual_review"),
        app_commands.Choice(name="返金済", value="refunded"),
        app_commands.Choice(name="キャンセル", value="cancelled"),
    ])
    async def order_search(
        self, interaction: discord.Interaction,
        user: discord.User | None = None,
        status: str | None = None, limit: int = 10,
    ) -> None:
        orders = await self.db.search_orders(
            user_id=user.id if user else None, status=status,
            limit=min(limit, 25),
        )
        if not orders:
            await interaction.response.send_message(
                "条件に合う注文はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="注文検索結果", description=f"{len(orders)}件",
            color=EmbedColor.DARK,
        )
        for o in orders[:15]:
            s = OrderStatus(o["status"])
            embed.add_field(
                name=f"#{o['id']:04d}  {o['created_at'][:10]}",
                value=(
                    f"{s.emoji} {s.display} | UID:{o['user_id']} | "
                    f"¥{o['user_amount']:,}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @order_group.command(name="complete", description="注文を手動で完了にします")
    @app_commands.describe(order_id="注文ID", receipt_number="受取番号")
    async def order_complete(
        self, interaction: discord.Interaction, order_id: int,
        receipt_number: str = "",
    ) -> None:
        try:
            await self.db.update_order_status(
                order_id, OrderStatus.COMPLETED, receipt_number=receipt_number
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        order = await self.db.get_order(order_id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="注文完了",
                description=f"注文 #{order_id:04d} を完了にしました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        if order:
            await post_achievement(self.bot, order)
            embed = build_order_result_embed(
                order_id, OrderStatus.COMPLETED,
                receipt_number=receipt_number,
                store_name=order.get("store_name", ""),
                user_amount=order["user_amount"],
            )
            img = await make_receipt_bytes(receipt_number)
            if img:
                embed.set_image(url="attachment://order.png")
            await notify_user(self.bot, order["user_id"], embed, img)
        await send_admin_log(
            self.bot, "注文手動完了",
            f"管理者 {interaction.user.display_name} が注文を完了にしました。",
            注文ID=f"#{order_id:04d}", 受取番号=receipt_number or "(なし)",
        )

    @order_group.command(name="refund", description="注文を返金します")
    @app_commands.describe(order_id="注文ID")
    async def order_refund(
        self, interaction: discord.Interaction, order_id: int
    ) -> None:
        try:
            amount = await self.db.refund_order(order_id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        order = await self.db.get_order(order_id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="返金完了",
                description=(
                    f"注文 #{order_id:04d} を返金しました。\n返金額: ¥{amount:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        if order:
            await notify_user(
                self.bot, order["user_id"],
                discord.Embed(
                    title="返金のお知らせ",
                    description=(
                        f"注文 #{order_id:04d} が返金されました。\n"
                        f"返金額: ¥{amount:,}"
                    ),
                    color=EmbedColor.INFO,
                ),
            )
        await send_admin_log(
            self.bot, "注文返金",
            f"管理者 {interaction.user.display_name} が注文を返金しました。",
            注文ID=f"#{order_id:04d}", 返金額=f"¥{amount:,}",
        )

    @order_group.command(name="review", description="要確認注文を一覧表示します")
    async def order_review(self, interaction: discord.Interaction) -> None:
        orders = await self.db.search_orders(status="manual_review", limit=20)
        if not orders:
            await interaction.response.send_message(
                "要確認の注文はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="要確認注文一覧", description=f"{len(orders)}件",
            color=EmbedColor.WARNING,
        )
        for o in orders:
            embed.add_field(
                name=f"#{o['id']:04d}  {o['created_at'][:10]}",
                value=(
                    f"UID:{o['user_id']} | ¥{o['user_amount']:,}\n"
                    f"{(o.get('error_info') or '')[:80]}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @order_group.command(name="retry", description="失敗した注文を再処理します")
    @app_commands.describe(order_id="注文ID")
    async def order_retry(
        self, interaction: discord.Interaction, order_id: int
    ) -> None:
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.response.send_message(
                "注文が見つかりません。", ephemeral=True
            )
            return
        if order["status"] != OrderStatus.FAILED.value:
            await interaction.response.send_message(
                "失敗した注文のみ再処理できます。", ephemeral=True
            )
            return
        mcd = self.bot.mcd  # type: ignore[attr-defined]
        if not mcd.is_enabled:
            await interaction.response.send_message(
                "外部APIが設定されていません。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.db.update_order_status(order_id, OrderStatus.PROCESSING)
        result = await mcd.execute_order(order["hex_data"])
        if result.get("success"):
            receipt = result.get("receipt_number", "")
            await self.db.update_order_status(
                order_id, OrderStatus.COMPLETED, receipt_number=receipt,
                order_token=result.get("order_token", ""),
                order_group=result.get("order_group", ""),
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="再処理完了",
                    description=f"注文 #{order_id:04d} が完了しました。",
                    color=EmbedColor.SUCCESS,
                ),
                ephemeral=True,
            )
            refreshed = await self.db.get_order(order_id)
            if refreshed:
                await post_achievement(self.bot, refreshed)
        elif result.get("unknown_state"):
            await self.db.update_order_status(
                order_id, OrderStatus.MANUAL_REVIEW,
                error_info=result.get("error", ""),
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="要確認",
                    description=f"注文 #{order_id:04d} は確認が必要です。",
                    color=EmbedColor.WARNING,
                ),
                ephemeral=True,
            )
        else:
            await self.db.update_order_status(
                order_id, OrderStatus.FAILED, error_info=result.get("error", "")
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="再処理失敗",
                    description=(
                        f"注文 #{order_id:04d} の再処理に失敗しました。\n"
                        f"{result.get('error', '')[:200]}"
                    ),
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )

    # ── achievement ──────────────────────────────────────────

    achievement_group = OwnerGroup(
        name="achievement", description="実績投稿管理", parent=admin_group
    )

    @achievement_group.command(
        name="repost", description="既存注文の実績を再投稿します"
    )
    @app_commands.describe(order_id="注文ID")
    async def achievement_repost(
        self, interaction: discord.Interaction, order_id: int
    ) -> None:
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.response.send_message(
                "注文が見つかりません。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        ok = await send_achievement(self.bot, order)
        if ok:
            await self.db.set_achievement_posted(order_id)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="再投稿完了",
                    description=f"注文 #{order_id:04d} の実績を投稿しました。",
                    color=EmbedColor.SUCCESS,
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="投稿失敗",
                    description="実績チャンネルが未設定か、送信に失敗しました。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )

    @achievement_group.command(
        name="post", description="実績を代理投稿します（通常の実績と同一表示）"
    )
    @app_commands.describe(
        total_amount="通常価格",
        user_amount="ご利用額",
        store="店舗名",
        receipt_number="受取番号（4桁）",
        order_id="表示する注文ID（省略時は自動）",
    )
    async def achievement_post(
        self,
        interaction: discord.Interaction,
        total_amount: int,
        user_amount: int,
        store: str = "",
        receipt_number: str = "",
        order_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if order_id is None:
            order_id = await self.db.get_max_order_id() + 1

        fake_order = {
            "id": order_id,
            "store_name": store,
            "store_id": store,
            "user_amount": user_amount,
            "total_amount": total_amount,
            "receipt_number": receipt_number,
        }
        ok = await send_achievement(self.bot, fake_order)
        if ok:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="代理投稿完了",
                    description=f"実績 #{order_id:04d} を投稿しました。",
                    color=EmbedColor.SUCCESS,
                ),
                ephemeral=True,
            )
            await send_admin_log(
                self.bot, "実績代理投稿",
                f"管理者 {interaction.user.display_name} が実績を代理投稿しました。",
                注文ID=f"#{order_id:04d}", 金額=f"¥{user_amount:,}",
                受取番号=receipt_number or "(なし)",
            )
        else:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="投稿失敗",
                    description="実績チャンネルが未設定か、送信に失敗しました。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )

    @achievement_group.command(
        name="preview", description="完了画像のプレビューを表示します"
    )
    @app_commands.describe(receipt_number="受取番号")
    async def achievement_preview(
        self, interaction: discord.Interaction, receipt_number: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        img = await make_receipt_bytes(receipt_number)
        if not img:
            await interaction.followup.send(
                "画像を生成できませんでした。", ephemeral=True
            )
            return
        await interaction.followup.send(
            file=_receipt_file(img), ephemeral=True
        )

    # ── deposits ─────────────────────────────────────────────

    deposit_group = OwnerGroup(
        name="deposits", description="入金管理", parent=admin_group
    )

    @deposit_group.command(name="list", description="未処理の入金申請を表示します")
    async def deposit_list(self, interaction: discord.Interaction) -> None:
        deps = await self.db.get_pending_deposits()
        if not deps:
            await interaction.response.send_message(
                "未処理の入金申請はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="未処理入金申請", description=f"{len(deps)}件",
            color=EmbedColor.WARNING,
        )
        for d in deps:
            embed.add_field(
                name=f"#{d['id']}  {d['created_at'][:10]}",
                value=f"UID:{d['user_id']} | ¥{d['amount']:,}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @deposit_group.command(name="approve", description="入金を承認します")
    @app_commands.describe(deposit_id="入金ID")
    async def deposit_approve(
        self, interaction: discord.Interaction, deposit_id: int
    ) -> None:
        try:
            user_id, amount, new_balance = await self.db.approve_deposit(
                deposit_id, interaction.user.id
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="入金承認",
                description=(
                    f"入金 #{deposit_id} を承認しました。\n"
                    f"UID: {user_id} | ¥{amount:,} | 新残高: ¥{new_balance:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await notify_user(
            self.bot, user_id,
            discord.Embed(
                title="入金完了",
                description=(
                    f"入金 #{deposit_id} が承認されました。\n"
                    f"入金額: ¥{amount:,}\n現在残高: ¥{new_balance:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
        )

    @deposit_group.command(name="reject", description="入金を却下します")
    @app_commands.describe(deposit_id="入金ID", reason="理由")
    async def deposit_reject(
        self, interaction: discord.Interaction, deposit_id: int, reason: str = ""
    ) -> None:
        try:
            user_id, amount = await self.db.reject_deposit(
                deposit_id, interaction.user.id, reason
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="入金却下",
                description=f"入金 #{deposit_id} を却下しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        desc = f"入金申請 #{deposit_id} (¥{amount:,}) は却下されました。"
        if reason:
            desc += f"\n理由: {reason}"
        await notify_user(
            self.bot, user_id,
            discord.Embed(
                title="入金却下", description=desc, color=EmbedColor.ERROR
            ),
        )

    # ── blacklist ────────────────────────────────────────────

    blacklist_group = OwnerGroup(
        name="blacklist", description="利用制限管理", parent=admin_group
    )

    @blacklist_group.command(name="add", description="ユーザーの利用を制限します")
    @app_commands.describe(user="対象ユーザー", reason="理由")
    async def blacklist_add(
        self, interaction: discord.Interaction, user: discord.User, reason: str = ""
    ) -> None:
        await self.db.set_blacklist(user.id, True, reason)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="利用制限を追加",
                description=f"**{user.display_name}** の利用を制限しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await send_admin_log(
            self.bot, "利用制限追加",
            f"管理者 {interaction.user.display_name} がユーザーを制限しました。",
            対象=user.display_name, 理由=reason or "(なし)",
        )

    @blacklist_group.command(name="remove", description="利用制限を解除します")
    @app_commands.describe(user="対象ユーザー")
    async def blacklist_remove(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        await self.db.set_blacklist(user.id, False, "")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="利用制限を解除",
                description=f"**{user.display_name}** の制限を解除しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @blacklist_group.command(name="list", description="制限中のユーザーを表示します")
    async def blacklist_list(self, interaction: discord.Interaction) -> None:
        rows = await self.db.get_blacklist()
        if not rows:
            await interaction.response.send_message(
                "制限中のユーザーはいません。", ephemeral=True
            )
            return
        lines = [
            f"<@{r['user_id']}> (`{r['user_id']}`)"
            + (f" — {r['blacklist_reason']}" if r["blacklist_reason"] else "")
            for r in rows
        ]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="制限中のユーザー",
                description="\n".join(lines)[:4000],
                color=EmbedColor.WARNING,
            ),
            ephemeral=True,
        )

    # ── vip ──────────────────────────────────────────────────

    vip_group = OwnerGroup(
        name="vip", description="個別負担率管理", parent=admin_group
    )

    @vip_group.command(name="set", description="ユーザー個別の負担率を設定します")
    @app_commands.describe(user="対象ユーザー", rate="負担率（1-100）")
    async def vip_set(
        self, interaction: discord.Interaction, user: discord.User, rate: int
    ) -> None:
        if rate < 1 or rate > 100:
            await interaction.response.send_message(
                "負担率は1〜100で指定してください。", ephemeral=True
            )
            return
        await self.db.set_custom_rate(user.id, rate)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="VIP設定",
                description=(
                    f"**{user.display_name}** の負担率を **{rate}%** "
                    f"（{100 - rate}% OFF）に設定しました。"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @vip_group.command(name="clear", description="個別負担率を解除します")
    @app_commands.describe(user="対象ユーザー")
    async def vip_clear(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        await self.db.set_custom_rate(user.id, None)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="VIP解除",
                description=f"**{user.display_name}** の個別負担率を解除しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @vip_group.command(name="list", description="個別負担率の一覧を表示します")
    async def vip_list(self, interaction: discord.Interaction) -> None:
        rows = await self.db.get_vip_users()
        if not rows:
            await interaction.response.send_message(
                "個別設定されたユーザーはいません。", ephemeral=True
            )
            return
        lines = [
            f"<@{r['user_id']}> — {r['custom_rate']}% "
            f"({100 - r['custom_rate']}% OFF)"
            for r in rows
        ]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="VIPユーザー",
                description="\n".join(lines)[:4000],
                color=EmbedColor.PRIMARY,
            ),
            ephemeral=True,
        )

    # ── coupon ───────────────────────────────────────────────

    coupon_group = OwnerGroup(
        name="coupon", description="クーポン管理", parent=admin_group
    )

    @coupon_group.command(name="create", description="クーポンを作成します")
    @app_commands.describe(
        code="クーポンコード",
        bonus="追加割引（%ポイント）",
        uses="使用可能回数（-1で無制限）",
        per_user="1人あたりの使用回数",
        days="有効日数（0で無期限）",
    )
    async def coupon_create(
        self, interaction: discord.Interaction, code: str, bonus: int,
        uses: int = -1, per_user: int = 1, days: int = 0,
    ) -> None:
        if bonus < 1 or bonus > 99:
            await interaction.response.send_message(
                "追加割引は1〜99で指定してください。", ephemeral=True
            )
            return
        expires = ""
        if days > 0:
            expires = (utc_now() + timedelta(days=days)).isoformat()
        await self.db.create_coupon(
            code, bonus, uses, per_user, expires, interaction.user.id
        )
        desc = (
            f"コード: **{code.upper()}**\n"
            f"追加割引: +{bonus}%\n"
            f"使用可能回数: {'無制限' if uses < 0 else f'{uses}回'}\n"
            f"1人あたり: {per_user}回"
        )
        if expires:
            desc += f"\n有効期限: {days}日後"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="クーポン作成", description=desc, color=EmbedColor.SUCCESS
            ),
            ephemeral=True,
        )

    @coupon_group.command(name="list", description="クーポン一覧を表示します")
    async def coupon_list(self, interaction: discord.Interaction) -> None:
        rows = await self.db.list_coupons()
        if not rows:
            await interaction.response.send_message(
                "クーポンはありません。", ephemeral=True
            )
            return
        embed = discord.Embed(title="クーポン一覧", color=EmbedColor.PRIMARY)
        for c in rows[:20]:
            exp = parse_iso(c["expires_at"])
            exp_txt = f"<t:{int(exp.timestamp())}:R>" if exp else "無期限"
            embed.add_field(
                name=c["code"],
                value=(
                    f"+{c['bonus']}% | 残り: "
                    f"{'∞' if c['uses_left'] < 0 else c['uses_left']} | "
                    f"1人{c['per_user']}回 | {exp_txt}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @coupon_group.command(name="delete", description="クーポンを削除します")
    @app_commands.describe(code="クーポンコード")
    async def coupon_delete(
        self, interaction: discord.Interaction, code: str
    ) -> None:
        ok = await self.db.delete_coupon(code)
        await interaction.response.send_message(
            f"{'削除しました。' if ok else 'クーポンが見つかりません。'}",
            ephemeral=True,
        )

    # ── campaign ─────────────────────────────────────────────

    campaign_group = OwnerGroup(
        name="campaign", description="キャンペーン管理", parent=admin_group
    )

    @campaign_group.command(name="start", description="キャンペーンを開始します")
    @app_commands.describe(rate="キャンペーン負担率（1-100）", hours="継続時間（0で無期限）")
    async def campaign_start(
        self, interaction: discord.Interaction, rate: int, hours: int = 0
    ) -> None:
        if rate < 1 or rate > 100:
            await interaction.response.send_message(
                "負担率は1〜100で指定してください。", ephemeral=True
            )
            return
        end = ""
        if hours > 0:
            end = (utc_now() + timedelta(hours=hours)).isoformat()
        await self.db.set_setting("campaign_rate", str(rate))
        await self.db.set_setting("campaign_end", end)
        desc = f"キャンペーン負担率: **{rate}%**（{100 - rate}% OFF）"
        if end:
            desc += f"\n終了: {hours}時間後"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="キャンペーン開始", description=desc, color=EmbedColor.SUCCESS
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @campaign_group.command(name="stop", description="キャンペーンを終了します")
    async def campaign_stop(self, interaction: discord.Interaction) -> None:
        await self.db.set_setting("campaign_rate", "")
        await self.db.set_setting("campaign_end", "")
        await interaction.response.send_message(
            embed=discord.Embed(
                title="キャンペーン終了",
                description="キャンペーンを終了しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    # ── points ───────────────────────────────────────────────

    points_group = OwnerGroup(
        name="points", description="ポイント管理", parent=admin_group
    )

    @points_group.command(name="add", description="ポイントを付与します")
    @app_commands.describe(user="対象ユーザー", amount="付与ポイント")
    async def points_add(
        self, interaction: discord.Interaction, user: discord.User, amount: int
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                "1以上で指定してください。", ephemeral=True
            )
            return
        new = await self.db.add_points(user.id, amount)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="ポイント付与",
                description=(
                    f"**{user.display_name}** に {amount:,}pt を付与しました。\n"
                    f"保有: {new:,}pt"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await notify_user(
            self.bot, user.id,
            discord.Embed(
                title="ポイント付与",
                description=f"+{amount:,}pt\n保有ポイント: {new:,}pt",
                color=EmbedColor.SUCCESS,
            ),
        )

    @points_group.command(name="rate", description="ポイント還元率を設定します")
    @app_commands.describe(rate="還元率%（0で無効）")
    async def points_rate(
        self, interaction: discord.Interaction, rate: int
    ) -> None:
        if rate < 0 or rate > 100:
            await interaction.response.send_message(
                "0〜100で指定してください。", ephemeral=True
            )
            return
        await self.db.set_setting("point_rate", str(rate))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=(
                    f"ポイント還元率を **{rate}%** に設定しました。"
                    if rate else "ポイント還元を無効にしました。"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    # ── product ──────────────────────────────────────────────

    product_group = OwnerGroup(
        name="product", description="商品名辞書管理", parent=admin_group
    )

    @product_group.command(name="add", description="商品名を登録します")
    @app_commands.describe(product_id="商品ID", name="表示名")
    async def product_add(
        self, interaction: discord.Interaction, product_id: str, name: str
    ) -> None:
        await self.db.set_product_name(product_id.strip(), name.strip())
        await interaction.response.send_message(
            embed=discord.Embed(
                title="登録完了",
                description=f"`{product_id}` → **{name}**",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @product_group.command(name="list", description="登録済み商品名を表示します")
    async def product_list(self, interaction: discord.Interaction) -> None:
        rows = await self.db.list_product_names(100)
        if not rows:
            await interaction.response.send_message(
                "登録された商品名はありません。", ephemeral=True
            )
            return
        text = "\n".join(f"{r['product_id']} = {r['name']}" for r in rows)
        if len(text) > 1900:
            buf = io.BytesIO(text.encode("utf-8-sig"))
            await interaction.response.send_message(
                file=discord.File(buf, filename="products.txt"), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"商品名辞書（{len(rows)}件）",
                description=f"```\n{text}\n```",
                color=EmbedColor.DARK,
            ),
            ephemeral=True,
        )

    @product_group.command(name="delete", description="商品名を削除します")
    @app_commands.describe(product_id="商品ID")
    async def product_delete(
        self, interaction: discord.Interaction, product_id: str
    ) -> None:
        ok = await self.db.delete_product_name(product_id.strip())
        await interaction.response.send_message(
            "削除しました。" if ok else "見つかりません。", ephemeral=True
        )

    @product_group.command(
        name="unknown", description="未登録の商品IDを表示します"
    )
    async def product_unknown(self, interaction: discord.Interaction) -> None:
        rows = await self.db.list_unknown_products(50)
        if not rows:
            await interaction.response.send_message(
                "未登録の商品IDはありません。", ephemeral=True
            )
            return
        text = "\n".join(
            f"{r['product_id']}  ({r['seen_count']}回)" for r in rows
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"未登録商品ID（{len(rows)}件）",
                description=f"```\n{text[:1900]}\n```\n"
                            "`/admin product add` で登録してください。",
                color=EmbedColor.WARNING,
            ),
            ephemeral=True,
        )

    @product_group.command(
        name="import", description="CSVから商品名を一括登録します"
    )
    @app_commands.describe(file="CSVファイル（product_id,name）")
    async def product_import(
        self, interaction: discord.Interaction, file: discord.Attachment
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if file.size > 1_000_000:
            await interaction.followup.send(
                "ファイルが大きすぎます（上限1MB）。", ephemeral=True
            )
            return
        try:
            raw = await file.read()
            text = raw.decode("utf-8-sig", errors="replace")
        except Exception:
            await interaction.followup.send(
                "ファイルを読み込めませんでした。", ephemeral=True
            )
            return

        count = 0
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 2:
                continue
            pid, name = row[0].strip(), row[1].strip()
            if not pid or not name or pid.lower() == "product_id":
                continue
            await self.db.set_product_name(pid, name)
            count += 1

        await interaction.followup.send(
            embed=discord.Embed(
                title="インポート完了",
                description=f"{count:,}件の商品名を登録しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    # ── channel ──────────────────────────────────────────────

    channel_group = OwnerGroup(
        name="channel", description="チャンネル設定", parent=admin_group
    )

    @channel_group.command(
        name="achievement", description="実績投稿チャンネルを設定します"
    )
    @app_commands.describe(channel="チャンネル")
    async def channel_achievement(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.db.set_setting("achievement_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"実績チャンネルを {channel.mention} に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @channel_group.command(
        name="admin_log", description="管理者ログチャンネルを設定します"
    )
    @app_commands.describe(channel="チャンネル")
    async def channel_admin_log(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.db.set_setting("admin_log_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"管理者ログチャンネルを {channel.mention} に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @channel_group.command(
        name="report", description="日次レポートチャンネルを設定します"
    )
    @app_commands.describe(channel="チャンネル")
    async def channel_report(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await self.db.set_setting("report_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"レポートチャンネルを {channel.mention} に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    # ── limit ────────────────────────────────────────────────

    limit_group = OwnerGroup(
        name="limit", description="上限・制限設定", parent=admin_group
    )

    @limit_group.command(name="order_max", description="1回の注文上限額を設定します")
    @app_commands.describe(amount="上限額（0で無制限）")
    async def limit_order_max(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        await self.db.set_setting("max_order_amount", str(max(0, amount)))
        await interaction.response.send_message(
            f"注文上限額を {'無制限' if amount <= 0 else f'¥{amount:,}'} に設定しました。",
            ephemeral=True,
        )

    @limit_group.command(name="deposit", description="入金額の下限・上限を設定します")
    @app_commands.describe(minimum="最低入金額", maximum="最高入金額（0で無制限）")
    async def limit_deposit(
        self, interaction: discord.Interaction, minimum: int, maximum: int = 0
    ) -> None:
        await self.db.set_setting("min_deposit_amount", str(max(0, minimum)))
        await self.db.set_setting("max_deposit_amount", str(max(0, maximum)))
        await interaction.response.send_message(
            f"入金額を ¥{minimum:,} 〜 "
            f"{'無制限' if maximum <= 0 else f'¥{maximum:,}'} に設定しました。",
            ephemeral=True,
        )

    @limit_group.command(name="daily", description="1日あたりの注文回数上限")
    @app_commands.describe(count="上限回数（0で無制限）")
    async def limit_daily(
        self, interaction: discord.Interaction, count: int
    ) -> None:
        await self.db.set_setting("daily_order_limit", str(max(0, count)))
        await interaction.response.send_message(
            f"1日の注文上限を {'無制限' if count <= 0 else f'{count}回'} に設定しました。",
            ephemeral=True,
        )

    @limit_group.command(name="cooldown", description="操作のクールダウンを設定します")
    @app_commands.describe(order="注文の間隔（秒）", deposit="入金申請の間隔（秒）")
    async def limit_cooldown(
        self, interaction: discord.Interaction, order: int, deposit: int
    ) -> None:
        await self.db.set_setting("order_cooldown", str(max(0, order)))
        await self.db.set_setting("deposit_cooldown", str(max(0, deposit)))
        await interaction.response.send_message(
            f"クールダウンを 注文:{order}秒 / 入金:{deposit}秒 に設定しました。",
            ephemeral=True,
        )

    @limit_group.command(name="hex_check", description="Hex重複チェックの有効/無効")
    @app_commands.describe(enabled="有効にする")
    async def limit_hex_check(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.db.set_setting("hex_reuse_check", "1" if enabled else "0")
        await interaction.response.send_message(
            f"Hex重複チェックを{'有効' if enabled else '無効'}にしました。",
            ephemeral=True,
        )

    # ── backup ───────────────────────────────────────────────

    backup_group = OwnerGroup(
        name="backup", description="バックアップ管理", parent=admin_group
    )

    @backup_group.command(name="now", description="今すぐバックアップを作成します")
    async def backup_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            path, size = await self.create_backup()
        except Exception as exc:
            await interaction.followup.send(
                f"バックアップに失敗しました: {type(exc).__name__}", ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=discord.Embed(
                title="バックアップ完了",
                description=f"`{path.name}`\nサイズ: {size / 1024:.1f} KB",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @backup_group.command(name="list", description="バックアップ一覧を表示します")
    async def backup_list(self, interaction: discord.Interaction) -> None:
        BACKUP_DIR.mkdir(exist_ok=True)
        files = sorted(BACKUP_DIR.glob("*.db"), reverse=True)
        if not files:
            await interaction.response.send_message(
                "バックアップはありません。", ephemeral=True
            )
            return
        lines = [
            f"{f.name} — {f.stat().st_size / 1024:.1f} KB" for f in files[:20]
        ]
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"バックアップ一覧（{len(files)}件）",
                description="```\n" + "\n".join(lines) + "\n```",
                color=EmbedColor.DARK,
            ),
            ephemeral=True,
        )

    @backup_group.command(name="config", description="自動バックアップを設定します")
    @app_commands.describe(
        enabled="有効にする", interval_hours="間隔（時間）", keep="保持世代数"
    )
    async def backup_config(
        self, interaction: discord.Interaction, enabled: bool,
        interval_hours: int = 6, keep: int = 14,
    ) -> None:
        await self.db.set_setting("backup_enabled", "1" if enabled else "0")
        await self.db.set_setting("backup_interval_hours", str(max(1, interval_hours)))
        await self.db.set_setting("backup_keep", str(max(1, keep)))
        await interaction.response.send_message(
            f"自動バックアップ: {'有効' if enabled else '無効'} / "
            f"{interval_hours}時間毎 / {keep}世代保持",
            ephemeral=True,
        )

    # ── report ───────────────────────────────────────────────

    report_group = OwnerGroup(
        name="report", description="レポート管理", parent=admin_group
    )

    @report_group.command(name="now", description="レポートを今すぐ表示します")
    @app_commands.describe(days="集計日数")
    async def report_now(
        self, interaction: discord.Interaction, days: int = 7
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = await build_report_embed(self.db, max(1, min(30, days)))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @report_group.command(name="config", description="日次レポートを設定します")
    @app_commands.describe(enabled="有効にする", hour="送信時刻（0-23）")
    async def report_config(
        self, interaction: discord.Interaction, enabled: bool, hour: int = 9
    ) -> None:
        await self.db.set_setting("report_enabled", "1" if enabled else "0")
        await self.db.set_setting("report_hour", str(max(0, min(23, hour))))
        await interaction.response.send_message(
            f"日次レポート: {'有効' if enabled else '無効'} / {hour}時送信",
            ephemeral=True,
        )

    # ── panel ────────────────────────────────────────────────

    panel_group = OwnerGroup(
        name="panel", description="パネル管理", parent=admin_group
    )

    @panel_group.command(name="refresh", description="パネルを更新します")
    async def panel_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        count = await self._refresh_panels()
        await interaction.followup.send(
            embed=discord.Embed(
                title="パネル更新",
                description=f"{count}件のパネルを更新しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @panel_group.command(name="delete", description="パネルを削除します")
    async def panel_delete(self, interaction: discord.Interaction) -> None:
        panel = await self.db.get_panel(interaction.guild_id)  # type: ignore[arg-type]
        if not panel:
            await interaction.response.send_message(
                "このサーバーにパネルはありません。", ephemeral=True
            )
            return
        try:
            ch = self.bot.get_channel(panel["channel_id"])
            if ch is None:
                ch = await self.bot.fetch_channel(panel["channel_id"])
            msg = await ch.fetch_message(panel["message_id"])  # type: ignore[union-attr]
            await msg.delete()
        except Exception:
            pass
        await self.db.delete_panel(interaction.guild_id)  # type: ignore[arg-type]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="パネル削除",
                description="パネルを削除しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    # ── 単体コマンド ──────────────────────────────────────────

    @admin_group.command(name="rate", description="ユーザー負担率を設定します")
    @app_commands.describe(rate="負担率（1-100）")
    async def admin_rate(
        self, interaction: discord.Interaction, rate: int
    ) -> None:
        if rate < 1 or rate > 100:
            await interaction.response.send_message(
                "負担率は1〜100の範囲で指定してください。", ephemeral=True
            )
            return
        await self.db.set_setting("user_rate", str(rate))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"ユーザー負担率を **{rate}%** に設定しました。（割引 {100 - rate}%）",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @admin_group.command(name="minimum", description="最低注文額を設定します")
    @app_commands.describe(amount="最低注文額")
    async def admin_minimum(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        if amount < 0:
            await interaction.response.send_message(
                "金額は0以上で指定してください。", ephemeral=True
            )
            return
        await self.db.set_setting("min_order_amount", str(amount))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"最低注文額を **¥{amount:,}** に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @admin_group.command(
        name="maintenance", description="メンテナンスモードを切り替えます"
    )
    @app_commands.describe(enabled="有効にする")
    async def admin_maintenance(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.db.set_setting("maintenance", "1" if enabled else "0")
        state = "有効" if enabled else "無効"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="メンテナンスモード",
                description=f"メンテナンスモードを **{state}** にしました。",
                color=EmbedColor.WARNING if enabled else EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @admin_group.command(name="accepting", description="注文受付を切り替えます")
    @app_commands.describe(enabled="受付する")
    async def admin_accepting(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.db.set_setting("accepting_orders", "1" if enabled else "0")
        state = "開始" if enabled else "停止"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="注文受付",
                description=f"注文受付を **{state}** しました。",
                color=EmbedColor.SUCCESS if enabled else EmbedColor.WARNING,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @admin_group.command(name="stats", description="統計情報を表示します")
    async def admin_stats(self, interaction: discord.Interaction) -> None:
        s = await self.db.get_stats()
        embed = discord.Embed(title="統計情報", color=EmbedColor.PRIMARY)
        embed.add_field(name="ユーザー数", value=f"{s['user_count']:,}", inline=True)
        embed.add_field(name="総注文数", value=f"{s['total_orders']:,}", inline=True)
        embed.add_field(name="完了注文", value=f"{s['completed_orders']:,}", inline=True)
        embed.add_field(name="総売上", value=f"¥{s['total_revenue']:,}", inline=True)
        embed.add_field(name="総負担額", value=f"¥{s['total_subsidy']:,}", inline=True)
        embed.add_field(name="総残高", value=f"¥{s['total_balance']:,}", inline=True)
        embed.add_field(name="未処理入金", value=f"{s['pending_deposits']:,}", inline=True)
        embed.add_field(name="要確認注文", value=f"{s['review_orders']:,}", inline=True)
        embed.add_field(name="失敗注文", value=f"{s['failed_orders']:,}", inline=True)
        embed.add_field(name="総ポイント", value=f"{s['total_points']:,}pt", inline=True)
        embed.add_field(name="制限ユーザー", value=f"{s['blacklisted']:,}", inline=True)
        mcd = self.bot.mcd  # type: ignore[attr-defined]
        embed.add_field(
            name="外部API", value="有効" if mcd.is_enabled else "未設定", inline=True
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(name="user", description="ユーザー情報を表示します")
    @app_commands.describe(user="対象ユーザー")
    async def admin_user(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        row = await self.db.get_user(user.id)
        balance = await self.db.get_balance(user.id)
        points = await self.db.get_points(user.id)
        order_count = await self.db.get_user_order_count(user.id)
        completed = await self.db.get_user_completed_count(user.id)
        tx_count = await self.db.get_transaction_count(user.id)
        savings = await self.db.get_user_savings(user.id)
        rank = resolve_rank(completed)
        br = await self.db.resolve_rate(user.id)

        embed = discord.Embed(
            title=f"ユーザー情報: {user.display_name}", color=EmbedColor.INFO
        )
        embed.add_field(name="ID", value=str(user.id), inline=True)
        embed.add_field(name="残高", value=f"¥{balance:,}", inline=True)
        embed.add_field(name="ポイント", value=f"{points:,}pt", inline=True)
        embed.add_field(name="ランク", value=f"{rank.emoji} {rank.name}", inline=True)
        embed.add_field(name="適用負担率", value=f"{br.final}%", inline=True)
        embed.add_field(name="節約総額", value=f"¥{savings:,}", inline=True)
        embed.add_field(name="注文数", value=f"{order_count:,}", inline=True)
        embed.add_field(name="完了数", value=f"{completed:,}", inline=True)
        embed.add_field(name="取引数", value=f"{tx_count:,}", inline=True)
        if row.get("custom_rate") is not None:
            embed.add_field(name="VIP率", value=f"{row['custom_rate']}%", inline=True)
        if row.get("blacklisted"):
            embed.add_field(
                name="利用制限",
                value=row.get("blacklist_reason") or "制限中",
                inline=True,
            )
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_group.command(
        name="diagnose", description="設定の問題を自動診断します"
    )
    async def admin_diagnose(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        s = await self.db.get_all_settings()
        mcd = self.bot.mcd  # type: ignore[attr-defined]
        ok: list[str] = []
        warn: list[str] = []

        # 外部API
        if mcd.is_enabled:
            ok.append("外部API: 有効（自動決済が動作します）")
        else:
            warn.append(
                "外部API: **未設定**\n"
                "→ 全注文が「要確認」になり自動決済されません。\n"
                "　 main.py の `MCD_REFRESH_TOKEN` を設定してください。"
            )

        # チャンネル
        for key, label in (
            ("achievement_channel_id", "実績チャンネル"),
            ("admin_log_channel_id", "管理者ログ"),
        ):
            cid = s.get(key, "")
            if not cid:
                warn.append(
                    f"{label}: 未設定\n"
                    f"→ `/admin channel "
                    f"{'achievement' if 'achieve' in key else 'admin_log'}` で設定"
                )
                continue
            try:
                ch = self.bot.get_channel(int(cid)) or await self.bot.fetch_channel(
                    int(cid)
                )
                perms = ch.permissions_for(ch.guild.me)  # type: ignore[union-attr]
                if not (perms.send_messages and perms.embed_links):
                    warn.append(f"{label}: 送信権限が不足しています（{ch.mention}）")
                elif not perms.attach_files:
                    warn.append(
                        f"{label}: ファイル添付権限がなく完了画像を送れません"
                    )
                else:
                    ok.append(f"{label}: OK（{ch.mention}）")
            except Exception:
                warn.append(f"{label}: チャンネルID {cid} にアクセスできません")

        # コマンド重複
        try:
            global_cmds = await self.bot.tree.fetch_commands()
            guild_cmds = (
                await self.bot.tree.fetch_commands(guild=interaction.guild)
                if interaction.guild
                else []
            )
            if global_cmds and guild_cmds:
                warn.append(
                    f"コマンド重複: グローバル {len(global_cmds)}件 + "
                    f"サーバー {len(guild_cmds)}件 が両方登録されています。\n"
                    "→ 一覧に2つずつ表示されます。`/sync repair` で解消できます。"
                )
            else:
                ok.append(
                    f"コマンド登録: グローバル {len(global_cmds)}件 / "
                    f"サーバー {len(guild_cmds)}件（重複なし）"
                )
        except Exception:
            warn.append("コマンド登録状況を取得できませんでした。")

        # 制限設定
        if int(s.get("order_cooldown", "0") or 0) > 60:
            warn.append("注文クールダウンが60秒超です。長すぎないか確認してください。")
        if s.get("maintenance") == "1":
            warn.append("メンテナンスモードが**有効**です（注文できません）。")
        if s.get("accepting_orders") != "1":
            warn.append("注文受付が**停止中**です。")

        # パネル
        panels = await self.db.get_all_panels()
        if panels:
            ok.append(f"パネル: {len(panels)}件 設置済み")
        else:
            warn.append("パネル未設置 → `/setup_panel` を実行してください。")

        # 画像生成
        import image_gen
        if image_gen.is_available():
            ok.append("完了画像生成: 利用可能")
        else:
            warn.append(
                "完了画像生成: 利用不可（Pillow未導入 または assets/ が欠落）"
            )

        embed = discord.Embed(
            title="診断結果",
            color=EmbedColor.WARNING if warn else EmbedColor.SUCCESS,
        )
        if warn:
            embed.add_field(
                name=f"⚠ 要対応（{len(warn)}件）",
                value="\n\n".join(warn)[:1024],
                inline=False,
            )
        if ok:
            embed.add_field(
                name=f"✅ 正常（{len(ok)}件）",
                value="\n".join(ok)[:1024],
                inline=False,
            )
        if not warn:
            embed.description = "問題は見つかりませんでした。"
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin_group.command(name="settings", description="現在の設定を表示します")
    async def admin_settings(self, interaction: discord.Interaction) -> None:
        s = await self.db.get_all_settings()
        lines = [f"{k} = {v}" for k, v in sorted(s.items()) if k != "report_last_date"]
        text = "\n".join(lines)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="現在の設定",
                description=f"```\n{text[:3900]}\n```",
                color=EmbedColor.DARK,
            ),
            ephemeral=True,
        )

    @admin_group.command(name="export", description="データをエクスポートします")
    @app_commands.describe(target="対象")
    @app_commands.choices(target=[
        app_commands.Choice(name="注文", value="orders"),
        app_commands.Choice(name="取引", value="transactions"),
    ])
    async def admin_export(
        self, interaction: discord.Interaction, target: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if target == "orders":
            data = await self.db.export_orders()
        else:
            data = await self.db.export_transactions()
        if not data:
            await interaction.followup.send("データがありません。", ephemeral=True)
            return
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
        writer.writeheader()
        writer.writerows(data)
        buf.seek(0)
        file = discord.File(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")),
            filename=f"{target}.csv",
        )
        await interaction.followup.send(file=file, ephemeral=True)

    # ── helpers ──────────────────────────────────────────────

    async def create_backup(self) -> tuple[Path, int]:
        BACKUP_DIR.mkdir(exist_ok=True)
        tz = await self.db.get_int_setting("tz_offset", 9)
        stamp = local_now(tz).strftime("%Y%m%d_%H%M%S")
        dest = BACKUP_DIR / f"concierge_{stamp}.db"
        size = await self.db.backup_to(str(dest))
        keep = await self.db.get_int_setting("backup_keep", 14)
        files = sorted(BACKUP_DIR.glob("*.db"), reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest, size

    async def _refresh_panels(self) -> int:
        panels = await self.db.get_all_panels()
        embed = await build_panel_embed(self.db)
        count = 0
        for p in panels:
            try:
                ch = self.bot.get_channel(p["channel_id"])
                if ch is None:
                    ch = await self.bot.fetch_channel(p["channel_id"])
                msg = await ch.fetch_message(p["message_id"])  # type: ignore[union-attr]
                await msg.edit(embed=embed, view=PanelView())
                count += 1
            except Exception as exc:
                logger.warning("Failed to refresh panel %s: %s", p["id"], exc)
        return count


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
