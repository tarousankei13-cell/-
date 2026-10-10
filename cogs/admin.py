"""運用コマンド（管理者・オーナー用）"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands

import config
import emoji as E
from core import audit
from core import ledger as L
from core import settings
from core import users as user_repo
from db.session import session_scope, user_scope
from cogs._checks import admin_only, handle_check_failure, owner_only
from ui import admin_flows, balance_panel, embeds

log = logging.getLogger("bot.cogs.admin")


class AdminCog(commands.Cog):
    """統計・元帳・利用者管理・保守"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    admin = app_commands.Group(name="admin", description="運用操作（管理者用）")
    stats_group = app_commands.Group(name="stats", description="統計（管理者用）")
    menu_group = app_commands.Group(name="menu", description="メニュー（管理者用）")
    debug_group = app_commands.Group(name="debug", description="調査用（管理者用）")
    store_group = app_commands.Group(name="store", description="店舗一覧（管理者用）")

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

    @stats_group.command(name="network", description="通信の速さと失敗率を表示します")
    @admin_only()
    async def stats_network(self, interaction: discord.Interaction) -> None:
        """
        相手先ごとの応答時間と失敗率。

        「最近遅い」を感覚ではなく数字で判断できるようにするためのもの。
        p50 は半数がこれ以内、p95 は95%がこれ以内という意味。
        """
        from datetime import datetime, timezone

        from core.telemetry import metrics

        snap = metrics.snapshot()
        if not snap:
            await interaction.response.send_message(
                embed=embeds.info(
                    "まだ通信の記録がありません。\n"
                    "注文や同期が動くと貯まります。"
                ),
                ephemeral=True,
            )
            return

        e = discord.Embed(
            title=f"{E.CHART} 通信の状況",
            description=(
                f"起動してから **{metrics.total_requests:,}** 件\n"
                f"うち失敗 **{metrics.total_failures:,}** 件"
            ),
            color=embeds.BLUE,
            timestamp=datetime.now(timezone.utc),
        )

        def ms(v):
            if v is None:
                return "—"
            return f"{v/1000:.1f}秒" if v >= 1000 else f"{v:.0f}ms"

        for st in snap[:12]:
            rate = st.failure_rate * 100
            mark = E.GREEN if rate < 1 else (E.YELLOW if rate < 10 else E.RED)
            body = (
                f"件数 **{st.total:,}**　失敗 **{rate:.1f}%**\n"
                f"中央値 {ms(st.p50)}　95% {ms(st.p95)}"
            )
            if st.last_error:
                body += f"\n{E.WARN} 直近の失敗: `{st.last_error[:40]}`"
            e.add_field(name=f"{mark} {st.host}", value=body, inline=True)

        elapsed = (datetime.now(timezone.utc).timestamp() - metrics.started_at) / 60
        e.set_footer(text=f"起動から {elapsed:.0f} 分 / 直近300件から算出")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @stats_group.command(name="breaker", description="一時的に使っていない経路を表示します")
    @admin_only()
    async def stats_breaker(self, interaction: discord.Interaction) -> None:
        """
        続けて失敗した相手は、しばらく使わないようにしている。
        その状態を確認する。
        """
        from core import breaker

        rows = breaker.groups.snapshot() + breaker.accounts.snapshot()
        if not rows:
            await interaction.response.send_message(
                embed=embeds.ok("すべて正常です。止めている経路はありません。"),
                ephemeral=True,
            )
            return

        blocked = [b for b in rows if b.state != breaker.CLOSED]
        e = discord.Embed(
            title=f"{E.SYNC} 経路の状態",
            description=(
                f"{E.OK} 正常 {len(rows) - len(blocked)} 件"
                + (f"　/　{E.WARN} 停止中 {len(blocked)} 件" if blocked else "")
            ),
            color=embeds.ORANGE if blocked else embeds.GREEN,
        )
        for b in (blocked or rows)[:15]:
            mark = {
                breaker.CLOSED: E.GREEN, breaker.HALF: E.YELLOW, breaker.OPEN: E.RED
            }.get(b.state, E.GREEN)
            body = b.describe()
            if b.last_error:
                body += f"\n`{b.last_error[:60]}`"
            if b.total_blocked:
                body += f"\n送らずに済ませた回数 {b.total_blocked}"
            e.add_field(name=f"{mark} {b.name}", value=body, inline=True)
        if not blocked:
            e.set_footer(text="すべて正常です")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @stats_group.command(name="dns", description="名前解決の控えの状況を表示します")
    @admin_only()
    async def stats_dns(self, interaction: discord.Interaction) -> None:
        from core import dns

        st = dns.stats()
        total = st["hit"] + st["miss"]
        rate = (st["hit"] / total * 100) if total else 0
        e = discord.Embed(
            title=f"{E.SYNC} 名前解決の控え",
            description=(
                f"控えている相手 **{st['entries']}** 件\n"
                f"控えで済んだ割合 **{rate:.0f}%**（{st['hit']:,} / {total:,}）"
            ),
            color=embeds.BLUE,
        )
        if st["stale"]:
            e.add_field(
                name=f"{E.WARN} 期限切れの結果を使った回数",
                value=f"{st['stale']:,} 回\n"
                      "DNSが引けない状態が起きています",
                inline=False,
            )
        if st["fail"]:
            e.add_field(name=f"{E.NG} 引けなかった回数",
                        value=f"{st['fail']:,} 回", inline=True)
        e.set_footer(text=f"控えは {dns.TTL:.0f} 秒で作り直します")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @stats_group.command(name="health", description="マクドナルド側に繋がるかを外から見る（通信の様子）")
    @admin_only()
    async def stats_health(self, interaction: discord.Interaction) -> None:
        """いま接続できているかを、その場で確かめる。"""
        from services import monitor

        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await monitor.check()

        color = (
            embeds.RED if report.all_down
            else (embeds.GREEN if report.healthy == len(report.results)
                  else embeds.ORANGE)
        )
        e = discord.Embed(
            title=f"{E.STORE} マクドナルドへの接続",
            description=f"正常 **{report.healthy}** / {len(report.results)} 件",
            color=color,
        )
        for h in report.results:
            mark = E.GREEN if h.ok else E.RED
            body = f"{h.latency_ms:.0f}ms" if h.ok else f"`{h.last_error}`"
            if not h.ok and h.last_ok:
                body += f"\n最後に応答 <t:{int(h.last_ok)}:R>"
            e.add_field(name=f"{mark} {h.name}", value=body, inline=True)
        if report.all_down:
            e.set_footer(
                text="すべて応答していません。マクドナルド側の障害か、回線の問題です"
            )
        else:
            e.set_footer(text=f"{config.MONITOR_INTERVAL_MINUTES}分ごとに自動で確認しています")
        await interaction.followup.send(embed=e, ephemeral=True)

    @stats_group.command(name="queue", description="注文の混雑状況を表示します")
    @admin_only()
    async def stats_queue(self, interaction: discord.Interaction) -> None:
        from core import queue as order_gate

        g = order_gate.gate
        e = discord.Embed(
            title=f"{E.CART} 注文の混雑",
            description=g.describe(),
            color=embeds.ORANGE if g.waiting else embeds.GREEN,
        )
        e.add_field(name="同時に処理する上限", value=f"{g.limit} 件", inline=True)
        e.add_field(name="いま処理中", value=f"{g.running} 件", inline=True)
        e.add_field(name="順番待ち", value=f"{g.waiting} 人", inline=True)
        e.add_field(name="通した注文", value=f"{g.total_queued:,} 件", inline=True)
        if g.total_rejected:
            e.add_field(
                name=f"{E.WARN} 混雑でお断りした数",
                value=f"{g.total_rejected:,} 件", inline=True,
            )
        if g.longest_wait > 1:
            e.add_field(
                name="最も長かった待ち時間",
                value=f"{g.longest_wait:.0f} 秒", inline=True,
            )
        e.set_footer(text="/config order_concurrency で上限を変えられます")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @stats_group.command(name="account", description="アカウント別の使用状況")
    @admin_only()
    async def stats_account(self, interaction: discord.Interaction) -> None:
        await admin_flows.show_accounts(interaction)

    # -- 利用者 -------------------------------------------------

    @admin.command(name="grant", description="利用者の残高を増減します")
    @app_commands.describe(user="対象", amount="増減額（マイナスで減算）", reason="理由（元帳に残ります）")
    @admin_only()
    async def grant(
        self, interaction: discord.Interaction, user: discord.User,
        amount: int, reason: app_commands.Range[str, 1, 400],
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
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="balance.grant", target=str(user.id),
            after=f"{amount:+,}円", reason=reason,
            detail={"balance_after": balance, "user": str(user)},
        )
        await interaction.followup.send(
            embed=embeds.ok(
                f"{user.mention} の残高を **{amount:+,}円** しました。\n"
                f"現在の残高: **{embeds.yen(balance)}**"
            ),
            ephemeral=True,
        )
        # 誰の残高をどう動かしたかを公開パネルにも残す（記録が人目に触れる形で残る）
        await balance_panel.post(
            interaction,
            amount=amount,
            balance=balance,
            reason=f"{balance_panel.REASON_GRANT}：{reason}",
            display_name=user.display_name,
            discord_id=user.id,
        )

    @admin.command(
        name="refund", description="【実行】いますぐ返金する（お金が動きます）"
    )
    @app_commands.describe(
        user="お返しする相手", amount="返金額（円）", reason="理由（記録に残ります）",
    )
    @admin_only()
    async def refund(
        self, interaction: discord.Interaction, user: discord.User,
        amount: app_commands.Range[int, 1, 1000000],
        reason: app_commands.Range[str, 0, 400] = "",
    ) -> None:
        """
        ⚠️ 残高を戻すだけの `/admin grant` とは別物。
           ここは **実際に Kyash からお金が出ていく**。
        """
        from services.kyash import refund as refund_svc

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            made = await refund_svc.send(
                user.id, int(amount), reason=reason,
                requested_by=interaction.user.id,
            )
        except refund_svc.RefundError as e:
            await interaction.followup.send(embed=embeds.warn(str(e)), ephemeral=True)
            return
        except Exception:
            log.exception("返金に失敗しました")
            await interaction.followup.send(
                embed=embeds.error(
                    "返金に失敗しました。残高は元に戻しています。\n"
                    "ログをご確認ください。"
                ),
                ephemeral=True,
            )
            return

        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="refund.send", target=str(user.id),
            reason=reason or None,
            detail={"金額": amount, "返金ID": made.id},
        )

        # ⚠️ リンクは本人にだけ渡す。公開の場に出すと誰でも受け取れてしまう。
        sent = False
        try:
            await user.send(
                embed=embeds.ok(
                    f"**{embeds.yen(int(amount))}** をお返しします。\n"
                    f"下のリンクを開いてお受け取りください。\n\n{made.link_url}"
                    + (f"\n\n理由: {reason}" if reason else ""),
                    title=f"{E.YEN} 返金のご案内",
                )
            )
            sent = True
        except (discord.HTTPException, AttributeError):
            log.info("返金リンクをDMできませんでした（%s）", user.id)

        await interaction.followup.send(
            embed=embeds.ok(
                f"{user.mention} へ **{embeds.yen(int(amount))}** の返金リンクを作りました。\n"
                + (
                    f"{E.OK} 本人へDMでお送りしました。"
                    if sent else
                    f"{E.WARN} DMを送れませんでした。下のリンクをご本人へお渡しください。\n"
                    f"{made.link_url}"
                )
                + f"\n\n{E.INFO} 残高からは引き済みです。"
                "リンクを開くまで受け取りは完了しません。"
            ),
            ephemeral=True,
        )

    @admin.command(name="ban", description="【BOT】この人のBOT利用を止める（Discordには残ります）")
    @app_commands.describe(user="対象の利用者", reason="停止の理由（記録に残ります）")
    @admin_only()
    async def ban(
        self, interaction: discord.Interaction, user: discord.User,
        reason: app_commands.Range[str, 0, 400] = "",
    ) -> None:
        async with session_scope() as s:
            u = await user_repo.ensure_user(s, user.id)
            u.is_banned = True
            u.note = reason or u.note
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="user.ban", target=str(user.id), reason=reason or None,
            before="利用可", after="停止中", detail={"user": str(user)},
        )
        await interaction.response.send_message(
            embed=embeds.ok(f"{user.mention} の利用を停止しました。"), ephemeral=True
        )

    @admin.command(name="unban", description="【BOT】BOT利用の停止を解除する")
    @app_commands.describe(user="解除する利用者")
    @admin_only()
    async def unban(self, interaction: discord.Interaction, user: discord.User) -> None:
        async with session_scope() as s:
            u = await user_repo.ensure_user(s, user.id)
            u.is_banned = False
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="user.unban", target=str(user.id),
            before="停止中", after="利用可", detail={"user": str(user)},
        )
        await interaction.response.send_message(
            embed=embeds.ok(f"{user.mention} の利用停止を解除しました。"), ephemeral=True
        )

    @admin.command(name="restore", description="バックアップから復元します（要注意）")
    @app_commands.describe(file="復元するバックアップファイル（.db）")
    @owner_only()
    async def admin_restore(
        self, interaction: discord.Interaction, file: discord.Attachment
    ) -> None:
        """
        バックアップから戻す。

        ⚠️ いまの内容は失われる。中身を確かめ、確認を取ってから実行する。
           戻す直前のものは別名で残すので、間違えても戻せる。
        """
        from services import backup as backup_svc

        await interaction.response.defer(ephemeral=True, thinking=True)
        if file.size > 200 * 1024 * 1024:
            await interaction.followup.send(
                embed=embeds.error("ファイルが大きすぎます（200MBまで）。"),
                ephemeral=True,
            )
            return
        try:
            data = await file.read()
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error(f"ファイルを読めませんでした。\n```{e}```"),
                ephemeral=True,
            )
            return

        good, message = await asyncio.to_thread(backup_svc.verify, data)
        if not good:
            await interaction.followup.send(
                embed=embeds.error(
                    f"このファイルからは復元できません。\n{message}"
                ),
                ephemeral=True,
            )
            return

        e = discord.Embed(
            title=f"{E.WARN} 本当に復元しますか",
            description=(
                f"`{file.filename}`（{file.size / 1024:.0f} KB）\n{message}\n\n"
                "**いまの残高・注文履歴は、このファイルの内容で置き換わります。**\n"
                "復元する直前の内容は別名で残すので、間違えても戻せます。\n\n"
                f"{E.INFO} 復元後は**BOTの再起動が必要**です。"
            ),
            color=embeds.RED,
        )
        await interaction.followup.send(
            embed=e, view=RestoreConfirm(data, file.filename), ephemeral=True
        )

    @admin.command(name="broadcast", description="利用者へお知らせを送ります")
    @app_commands.describe(
        target="送り先（DM: 利用者全員 / channel: お知らせチャンネル）"
    )
    @app_commands.choices(target=[
        app_commands.Choice(name="利用者全員のDM", value="dm"),
        app_commands.Choice(name="お知らせチャンネル", value="channel"),
    ])
    @owner_only()
    async def admin_broadcast(
        self, interaction: discord.Interaction,
        target: app_commands.Choice[str] | None = None,
    ) -> None:
        """
        お知らせを配る。

        送る前に、実際の見た目と宛先の数を確認してから送信する。
        一斉送信は取り消せないため。
        """
        await admin_flows.start_broadcast(
            interaction, target.value if target else "dm"
        )

    @admin.command(name="user", description="利用者の情報をまとめて表示します")
    @app_commands.describe(user="対象の利用者")
    @admin_only()
    async def admin_user(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        """
        残高・利用状況・適用中の負担率とその根拠・直近の注文・
        チャージ履歴を1画面にまとめる。
        """
        await admin_flows.show_user(interaction, user)

    @admin.command(name="audit", description="管理操作の記録を表示します")
    @app_commands.describe(
        user="この人の操作だけ表示（任意）",
        action="種類で絞り込み（例: balance / subsidy / account）",
        limit="表示件数（既定20・最大50）",
    )
    @admin_only()
    async def admin_audit(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        action: app_commands.Range[str, 1, 50] | None = None,
        limit: app_commands.Range[int, 1, 50] = 20,
    ) -> None:
        """
        誰がいつ何を変えたかの記録。

        お金を扱うので、設定の変更・残高の付与・利用停止・
        アカウントの削除はすべてここに残る。
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        entries = await audit.search(
            actor_id=user.id if user else None, action=action, limit=int(limit)
        )
        total = await audit.count()

        if not entries:
            await interaction.followup.send(
                embed=embeds.info(
                    "条件に合う記録がありませんでした。\n"
                    f"記録は全部で **{total:,}** 件あります。"
                ),
                ephemeral=True,
            )
            return

        body = "\n\n".join(e.line() for e in entries)
        cond = []
        if user:
            cond.append(f"操作者 {user.mention}")
        if action:
            cond.append(f"種類 `{action}`")
        e = discord.Embed(
            title=f"{E.NOTE} 管理操作の記録",
            description=(("　/　".join(cond) + "\n\n") if cond else "") + body[:3800],
            color=embeds.BLUE,
        )
        e.set_footer(text=f"{len(entries)} 件を表示 / 記録は全部で {total:,} 件")
        await interaction.followup.send(embed=e, ephemeral=True)

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
        store_name: app_commands.Range[str, 1, 100] | None = None,
        receipt_number: app_commands.Range[str, 1, 20] | None = None,
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

    @menu_group.command(name="sync", description="マクドナルドのメニューを取り直す")
    @app_commands.describe(store_id="店舗ID。省略すると使用中の全店舗")
    @admin_only()
    async def menu_sync(
        self, interaction: discord.Interaction,
        store_id: app_commands.Range[str, 1, 20] | None = None,
    ) -> None:
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

    # -- 店舗一覧 -----------------------------------------------

    @store_group.command(name="search", description="店名の検索を試します")
    @app_commands.describe(query="店名の一部、または店舗ID")
    @admin_only()
    async def store_search(
        self, interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 100],
    ) -> None:
        from services.mcd import store_index

        await interaction.response.defer(ephemeral=True, thinking=True)
        hits = store_index.search(query, limit=20)
        if not hits:
            await interaction.followup.send(
                embed=embeds.warn(
                    f"「{query}」に一致する店舗が見つかりませんでした。\n"
                    f"（インデックス: {store_index.get_index().count} 店舗）"
                ),
                ephemeral=True,
            )
            return
        lines = [f"`{e.store_id}` **{e.name}**　{e.address}" for e in hits]
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.STORE} 「{query}」の検索結果 {len(hits)} 件",
                description="\n".join(lines)[:4000],
                color=embeds.BLUE,
            ).set_footer(text=f"インデックス: {store_index.get_index().count} 店舗"),
            ephemeral=True,
        )

    @store_group.command(name="info", description="店舗一覧の状態を表示します")
    @admin_only()
    async def store_info(self, interaction: discord.Interaction) -> None:
        from services.mcd import store_index

        from datetime import datetime, timezone

        idx = store_index.get_index()
        e = discord.Embed(title=f"{E.STORE} 店舗一覧", color=embeds.BLUE)
        if idx.available:
            e.description = f"**{idx.count:,}** 店舗を読み込み済みです。店名で検索できます。"
            meta = idx.meta
            src = (
                "定期同期で取得した一覧"
                if store_index.SYNCED_PATH.exists()
                else "同梱の一覧（まだ同期していません）"
            )
            e.add_field(name=f"{E.INFO} 読み込み元", value=src, inline=False)
            if meta.get("updated_at"):
                when = datetime.fromtimestamp(int(meta["updated_at"]), timezone.utc)
                e.add_field(
                    name=f"{E.SYNC} 最終同期",
                    value=f"<t:{int(meta['updated_at'])}:R>（{when:%m/%d %H:%M} UTC）",
                    inline=True,
                )
            if meta.get("sitemap_at"):
                e.add_field(
                    name=f"{E.CHART} 店舗IDの照合",
                    value=f"<t:{int(meta['sitemap_at'])}:R>",
                    inline=True,
                )
            unresolved = meta.get("unresolved") or {}
            if unresolved:
                e.add_field(
                    name=f"{E.WARN} 取得できない店舗",
                    value=f"{len(unresolved)} 件（閉店直後などで配信元に無い）",
                    inline=False,
                )
            interval = settings.get(
                "store_index_sync_minutes", config.STORE_INDEX_SYNC_MINUTES
            )
            e.set_footer(
                text=f"{interval}分ごとに巡回更新 / "
                f"{config.STORE_SITEMAP_CHECK_MINUTES}分ごとに店舗IDを照合"
            )
        else:
            e.color = embeds.ORANGE
            e.description = (
                "店舗一覧が読み込まれていません。\n"
                "利用者は店舗IDでの指定のみになります。\n\n"
                "`/store reindex` で作成できます（数分で終わります）。"
            )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @store_group.command(name="reindex", description="店舗一覧を今すぐ作り直します")
    @owner_only()
    async def store_reindex(self, interaction: discord.Interaction) -> None:
        """
        全店舗を取り直す。

        通常は定期同期が自動で最新に保つので、この操作は
        一覧を壊してしまったときや、すぐに反映したいときだけで足りる。
        """
        from services.mcd import store_index, store_sync

        await interaction.response.send_message(
            embed=embeds.info(
                f"{E.LOADING} 店舗一覧を作り直しています…\n"
                "**3〜5分**で終わります。進み具合はこのメッセージに表示します。\n"
                f"{E.INFO} 作業中もBOTは通常どおり使えます。"
            ),
            ephemeral=True,
        )

        last = {"pct": -1}

        async def on_progress(done: int, total: int, found: int) -> None:
            pct = done * 100 // max(total, 1)
            if pct == last["pct"]:
                return
            last["pct"] = pct
            try:
                await interaction.edit_original_response(
                    embed=embeds.info(
                        f"{E.LOADING} 店舗一覧を作成中… **{pct}%**\n"
                        f"　{done:,} / {total:,} 件を確認　**{found:,}** 店舗"
                    )
                )
            except discord.HTTPException:
                pass

        try:
            report = await store_sync.sync(full=True, progress=on_progress)
        except Exception as e:
            log.exception("店舗一覧の作成に失敗しました")
            await interaction.edit_original_response(
                embed=embeds.error(f"店舗一覧の作成に失敗しました。\n```{e}```")
            )
            return

        size = (
            store_index.SYNCED_PATH.stat().st_size
            if store_index.SYNCED_PATH.exists()
            else 0
        )
        detail = store_sync.format_report(report)
        await interaction.edit_original_response(
            embed=embeds.ok(
                f"店舗一覧を作り直しました。\n"
                f"**{report.total:,}** 店舗（{size / 1024:.0f} KB）\n"
                + (f"\n{detail}\n" if detail else "")
                + f"{E.INFO} 利用者は店名の一部で検索できます。"
            )
        )

    # -- 調査 ---------------------------------------------------

    @debug_group.command(
        name="fails", description="失敗した注文の中身とエラーを表示します"
    )
    @app_commands.describe(
        count="見る件数", product="この商品コードを含むものだけ（省略可）",
    )
    @admin_only()
    async def debug_fails(
        self, interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 10] = 3,
        product: app_commands.Range[str, 1, 16] | None = None,
    ) -> None:
        """
        注文が通らない原因を突き止めるための窓口。

        ⚠️ **送った中身そのもの**と、マクドナルドが返した文言を並べて出す。
           どちらか片方だけでは原因が分からない。
           「何を送って、何と言われたか」が揃って初めて切り分けられる。
        """
        from sqlalchemy import select

        from db.models import Order, as_utc
        from services.mcd.protocol import ProtocolError, decode_hex

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            q = (
                select(Order)
                .where(Order.error.is_not(None))
                .order_by(Order.created_at.desc())
                .limit(50)
            )
            rows = list((await s.execute(q)).scalars().all())

        if product:
            want = str(product).strip()
            rows = [o for o in rows if want in (o.items_json or "")
                    or want in (o.hex_payload or "")]
        rows = rows[:count]

        if not rows:
            await interaction.followup.send(
                embed=embeds.info(
                    "失敗した注文は見つかりませんでした。" if not product
                    else f"`{product}` を含む失敗した注文は見つかりませんでした。"
                ),
                ephemeral=True,
            )
            return

        from services.mcd import stores as mcd_stores

        from services.mcd import errors as mcd_errors

        blocks = []
        raw: list[str] = []
        for o in rows:
            created = as_utc(o.created_at)
            when = (created.astimezone(config.JST).strftime("%m/%d %H:%M")
                    if created else "—")
            lines = [
                f"**{when}　{o.store_name or o.store_id or '—'}**",
                f"状態 `{o.state}`　定価 {embeds.yen(o.list_price)}",
            ]
            # ⚠️ 商品名を引く道具は try の**外**で作る。
            #    中で作ると、送った中身が読めなかったときに未定義になり、
            #    この下の「相手が指した原因」で落ちる。
            #    原因が分からない注文こそ読めないことが多く、
            #    一番必要な場面で使えなくなる。
            menu = None
            try:
                menu = await mcd_stores.load_menu(str(o.store_id))
            except Exception:
                pass

            def nm(code: str, _menu=None) -> str:
                m = _menu if _menu is not None else menu
                if m is None:
                    return ""
                p = m.products.get(str(code))
                return f" {p.name}" if p else " （枠・中間）"

            # 送った中身を木の形で出す
            try:
                dec = decode_hex(o.hex_payload)

                def tree(it, d=0):
                    out = ["　" * d + f"`{it.product_code}`×{it.quantity}"
                           + ("[枠]" if it.has_flag else "") + nm(it.product_code)]
                    for c in it.components:
                        out += tree(c, d + 1)
                    return out

                for it in dec.items:
                    lines += tree(it)
            except ProtocolError as e:
                lines.append(f"（送った中身を読めませんでした: {e}）")

            # ⚠️ 応答には「どれが駄目か」の経路が入っていることがある。
            #    そこを読んで、分かりやすく出す。
            pairs = mcd_errors.rejected_pairs(o.error or "")
            if pairs:
                told = []
                for slot_code, product_code in pairs:
                    told.append(
                        f"枠 `{slot_code}` に `{product_code}`"
                        + nm(product_code) + " を入れたのが断られました"
                    )
                lines.append(f"{E.WARN} **相手が指した原因**")
                lines += [f"・{t}" for t in told]

            lines.append(f"{E.NG} **マクドナルドの応答**")
            lines.append(
                f"```\n{(mcd_errors.scrub(o.error) or '（記録なし）')[:700]}\n```"
            )
            blocks.append("\n".join(lines))

            # ⚠️ 画面には収まらないので、全文は別に取っておく。
            #    切れた先に原因が書いてあることがある。
            raw.append(
                f"=== {when}  注文 {str(o.id)[:8]}  店舗 {o.store_id}"
                f"（{o.store_name or '—'}）===\n"
                f"状態: {o.state}\n"
                f"定価: {o.list_price}\n"
                f"--- 送った注文コード ---\n{o.hex_payload or '（記録なし）'}\n"
                f"--- 送った中身（JSON） ---\n{o.items_json or '（記録なし）'}\n"
                f"--- 応答（全文） ---\n"
                f"{mcd_errors.scrub(o.error) or '（記録なし）'}\n"
            )

        body = "\n\n".join(blocks)
        # ⚠️ 1つでも切れていたら全文を付ける。原因は切れた先にあることが多い。
        joined = "\n".join(raw)
        too_long = len(body) > 4000 or any(
            len(o.error or "") > 700 for o in rows
        )
        kw: dict = {}
        if too_long:
            import io

            kw["file"] = discord.File(
                io.BytesIO(joined.encode("utf-8")), filename="fails.txt"
            )
            body = body[:3900] + (
                f"\n\n{E.INFO} 画面に収まらないため、"
                f"**全文を `fails.txt` に付けました。**"
            )
        await interaction.followup.send(
            embed=embeds.info(body[:4000], title=f"{E.WARN} 通らなかった注文"),
            ephemeral=True,
            **kw,
        )

    @debug_group.command(name="hex", description="注文コードの中身を表示します")
    @app_commands.describe(code="注文コード（HEX）")
    @admin_only()
    # ⚠️ 注文コードは長い。Discord の文字列引数の上限いっぱいまで許す。
    async def debug_hex(
        self, interaction: discord.Interaction,
        code: app_commands.Range[str, 1, 6000],
    ) -> None:
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

        # ⚠️ ここでは覚えない。読むだけの窓口が勝手に設定を書き換えると、
        #    「見ただけ」のつもりが挙動を変えてしまう。覚えるのは
        #    /debug learn の仕事。見つけたことだけ知らせる。
        from services.mcd import slot_bridge

        pairs = slot_bridge.pairs_in_order(d.items)
        new_pairs = [
            (sc, bc) for sc, bc in pairs if slot_bridge.bridge_for(sc) != bc
        ]
        if new_pairs:
            e.add_field(
                name=f"{E.WARN} まだ知らない枠の構造が入っています",
                value=(
                    "\n".join(f"`{sc}` → `{bc}`" for sc, bc in new_pairs[:8])
                    + f"\n\n{E.INFO} `/debug learn` に同じコードを貼ると覚えます。"
                )[:1024],
                inline=False,
            )
        e.set_footer(text=f"{len(d.raw_hex) // 2} バイト")
        await interaction.followup.send(embed=e, ephemeral=True)


    # -- 選択枠の中間ノード -------------------------------------

    async def _recent_store(self) -> str:
        """直近の注文の店舗。店舗を省略されたときの既定にする。"""
        from sqlalchemy import select

        from db.models import Order

        async with session_scope() as s:
            q = (
                select(Order.store_id)
                .where(Order.store_id.is_not(None))
                .order_by(Order.created_at.desc())
                .limit(1)
            )
            return (await s.execute(q)).scalars().first() or ""

    @debug_group.command(
        name="learn", description="注文コードから選択枠の構造をおぼえます"
    )
    @app_commands.describe(code="通った注文コード（HEX）")
    @admin_only()
    async def debug_learn(
        self, interaction: discord.Interaction,
        code: app_commands.Range[str, 1, 6000],
    ) -> None:
        """
        通った注文コードを貼って、選択枠の中間ノードをおぼえる窓口。

        ⚠️ これが無いと、構造を教える手段が無い。学習は注文の流れの
           中だけで動いており、そこを通るには金額・残高・店舗の確認を
           すべて越える必要があった。**構造だけ教えたい場合に使えない。**

        ⚠️ 中間ノードはカタログにもマクドナルド公式アプリにも載っていない
           （docs/04）。通った注文コードが唯一の手がかり。
        """
        from services.mcd import slot_bridge, stores as mcd_stores
        from services.mcd.protocol import ProtocolError, decode_hex

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            d = decode_hex(code)
        except ProtocolError as e:
            await interaction.followup.send(
                embed=embeds.error(
                    f"注文コードを読み取れませんでした。\n```{e}```\n"
                    f"{E.INFO} 最後まで省略せずに貼れているか確認してください。"
                ),
                ephemeral=True,
            )
            return

        pairs = slot_bridge.pairs_in_order(d.items)
        if not pairs:
            await interaction.followup.send(
                embed=embeds.info(
                    "この注文コードに、枠と中間ノードの組み合わせはありませんでした。\n\n"
                    f"{E.INFO} 中間ノードが要るのはセットのドリンク枠などです。"
                    "単品や、中間ノードの無いセットでは何も学べません。"
                ),
                ephemeral=True,
            )
            return

        # 覚える前に、どれが新しいかを見ておく
        before = {c: slot_bridge.bridge_for(c) for c, _ in pairs}
        learned = slot_bridge.learn_from_order(d.items)

        menu = None
        try:
            menu = await mcd_stores.load_menu(str(d.store_id or await self._recent_store()))
        except Exception:
            pass

        def nm(c: str) -> str:
            if menu is None:
                return ""
            p = menu.products.get(str(c))
            return f" {p.name}" if p else ""

        lines, fresh = [], []
        for slot_code, bridge_code in pairs:
            if before.get(slot_code) == bridge_code:
                lines.append(f"　既に知っています　`{slot_code}` → `{bridge_code}`")
            elif before.get(slot_code):
                # ⚠️ 既に別の値を知っていた。上書きされている。
                lines.append(
                    f"{E.WARN} 書き換えました　`{slot_code}` → `{bridge_code}`"
                    f"（前は `{before[slot_code]}`）"
                )
                fresh.append(slot_code)
            else:
                lines.append(f"{E.OK} おぼえました　`{slot_code}` → `{bridge_code}`")
                fresh.append(slot_code)

        e = discord.Embed(
            title=f"{E.SYNC} 選択枠の構造をおぼえました"
            if learned else f"{E.INFO} 新しく学ぶものはありませんでした",
            description="\n".join(lines)[:4000],
            color=embeds.GREEN if learned else embeds.BLUE,
        )

        # 何が注文できるようになったか
        if fresh and menu is not None:
            affected = menu.products_using_slots(fresh)
            if affected:
                names = [f"・{p.name}" for p in affected[:15]]
                more = len(affected) - len(names)
                e.add_field(
                    name=f"これで通るようになる見込みの商品（{len(affected)} 件）",
                    value=("\n".join(names) + (f"\n…ほか {more} 件" if more > 0 else ""))[:1024],
                    inline=False,
                )
        if fresh and menu is None:
            e.add_field(
                name="注意",
                value=f"{E.WARN} メニューを読めなかったため、影響する商品を出せませんでした。",
                inline=False,
            )
        e.set_footer(text=f"店舗 {d.store_id or '—'}　/debug bridges で一覧を確認できます")
        await interaction.followup.send(embed=e, ephemeral=True)

    @debug_group.command(
        name="bridges", description="選択枠の中間ノードの分かっている分と不明な分を表示します"
    )
    @app_commands.describe(store="店舗ID（省略すると直近の注文の店舗）")
    @admin_only()
    async def debug_bridges(
        self, interaction: discord.Interaction,
        store: app_commands.Range[str, 1, 8] | None = None,
    ) -> None:
        """何が分かっていて、何が分かっていないかを見せる。

        ⚠️ 「不明」と「中間不要と確認済み」を混ぜない。混ぜると、
           直す必要の無いものを追いかけることになる。
        """
        from services.mcd import slot_bridge, stores as mcd_stores

        await interaction.response.defer(ephemeral=True, thinking=True)
        store_id = str(store or await self._recent_store() or "")

        e = discord.Embed(
            title=f"{E.RECEIPT} 選択枠の中間ノード", color=embeds.BLUE,
            description=(
                "セットの枠と商品の間に、もう1段入ることがあります。\n"
                "この値は**カタログにも公式アプリにも載っていない**ため、"
                "通った注文コードから覚えるしかありません。"
            ),
        )

        known = slot_bridge.all_known()
        learned_marks = [
            f"`{k}` → `{v}`" + ("（覚えた分）" if slot_bridge.is_learned(k) else "")
            for k, v in sorted(known.items())
        ]
        e.add_field(
            name=f"{E.OK} 中間ノードあり（{len(known)} 件）",
            value="\n".join(learned_marks)[:1024] or "—", inline=False,
        )
        e.add_field(
            name=f"{E.OK} 中間ノード不要と確認済み（{len(slot_bridge.NO_BRIDGE)} 件）",
            value="\n".join(
                f"`{k}` → `{v}` 直結" for k, v in sorted(slot_bridge.NO_BRIDGE.items())
            )[:1024] or "—",
            inline=False,
        )

        menu = None
        if store_id:
            try:
                menu = await mcd_stores.load_menu(store_id)
            except Exception:
                pass
        if menu is None:
            e.add_field(
                name="まだ分からない枠",
                value=(
                    f"{E.WARN} 店舗 `{store_id or '—'}` のメニューを読めなかったため、"
                    "一覧を出せませんでした。`store:` に店舗IDを指定してください。"
                ),
                inline=False,
            )
            await interaction.followup.send(embed=e, ephemeral=True)
            return

        gaps = menu.bridge_gaps()
        total = sum(len(v) for _, v in gaps)
        rows = []
        for slot_code, prods in gaps[:12]:
            例 = prods[0].name if prods else "—"
            rows.append(f"`{slot_code}`　{len(prods)}商品　例: {例}")
        more = len(gaps) - len(rows)
        e.add_field(
            name=f"{E.WARN} まだ分からない枠（{len(gaps)} 枠 / {total} 商品）",
            value=(
                "\n".join(rows) + (f"\n…ほか {more} 枠" if more > 0 else "")
            )[:1024] or "すべて判明しています",
            inline=False,
        )
        e.add_field(
            name="直し方",
            value=(
                "通った注文コードを `/debug learn code:…` に貼ると、"
                "その中にある分を自動でおぼえます。\n"
                f"{E.INFO} 中間が要らない枠もここに出ます。"
                "**全部が壊れているわけではありません。**"
            ),
            inline=False,
        )
        e.set_footer(text=f"店舗 {store_id}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @debug_group.command(
        name="forget", description="おぼえた中間ノードを1件消します"
    )
    @app_commands.describe(slot="枠のコード")
    @admin_only()
    async def debug_forget(
        self, interaction: discord.Interaction,
        slot: app_commands.Range[str, 1, 16],
    ) -> None:
        """間違っておぼえたものを消す。

        ⚠️ 実物で確認済みのもの（KNOWN・NO_BRIDGE）は消せない。
           消す手段を用意すると、事故で動かなくなる。
        """
        from services.mcd import slot_bridge

        await interaction.response.defer(ephemeral=True, thinking=True)
        code = str(slot).strip()
        if slot_bridge.forget(code):
            await interaction.followup.send(
                embed=embeds.ok(
                    f"枠 `{code}` におぼえていた中間ノードを消しました。\n"
                    f"{E.INFO} 次の注文からは、枠の直下に商品を置く形で送ります。"
                ),
                ephemeral=True,
            )
            return
        if code in slot_bridge.KNOWN or code in slot_bridge.NO_BRIDGE:
            await interaction.followup.send(
                embed=embeds.error(
                    f"枠 `{code}` は**実物の注文コードで確認済み**のため消せません。\n"
                    f"{E.INFO} 間違っていると思われる場合はご連絡ください。"
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=embeds.info(f"枠 `{code}` について、おぼえている内容はありません。"),
            ephemeral=True,
        )

    # -- 保守 ---------------------------------------------------

    @app_commands.command(name="sync", description="【保守】スラッシュコマンドをDiscordへ登録し直す")
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


class RestoreConfirm(discord.ui.View):
    """復元の最終確認。押し間違いが起きないよう、文言と色を強くする。"""

    def __init__(self, data: bytes, filename: str) -> None:
        super().__init__(timeout=120)
        self.data = data
        self.filename = filename

    @discord.ui.button(
        label="復元する（元に戻せません）", emoji="⚠️", style=discord.ButtonStyle.danger
    )
    async def do_restore(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        from services import backup as backup_svc

        await interaction.response.defer(ephemeral=True, thinking=True)
        database_url = getattr(interaction.client, "database_url", "")
        good, message = await asyncio.to_thread(
            backup_svc.restore, self.data, database_url
        )
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="backup.restore", target=self.filename,
            after="成功" if good else "失敗", detail=message,
        )
        await interaction.followup.send(
            embed=(embeds.ok if good else embeds.error)(message), ephemeral=True
        )
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=embeds.info("復元をやめました。"), view=None
        )
        self.stop()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
