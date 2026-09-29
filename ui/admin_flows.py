"""管理者パネルの操作"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import func, select

import emoji as E
from core import ledger as L
from core import saga, settings
from db.models import (
    KyashAccount, Ledger, McdAccount, Order, SubsidyRule, User,
)
from db.session import session_scope
from ui import embeds

log = logging.getLogger("bot.admin_flows")


async def collect_stats() -> dict:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as s:
        mcd_total = await s.scalar(select(func.count()).select_from(McdAccount)) or 0
        mcd_active = await s.scalar(
            select(func.count()).select_from(McdAccount).where(McdAccount.status == "ACTIVE")
        ) or 0
        kyash_total = await s.scalar(select(func.count()).select_from(KyashAccount)) or 0
        kyash_active = await s.scalar(
            select(func.count()).select_from(KyashAccount).where(KyashAccount.status == "ACTIVE")
        ) or 0
        orders_today = await s.scalar(
            select(func.count()).select_from(Order).where(Order.created_at >= today)
        ) or 0
        subsidy_today = await s.scalar(
            select(func.coalesce(func.sum(-Ledger.amount), 0)).where(
                Ledger.account == "subsidy_pool",
                Ledger.amount < 0,
                Ledger.created_at >= today,
            )
        ) or 0
        review = await s.scalar(
            select(func.count()).select_from(Order).where(Order.state == saga.MANUAL_REVIEW)
        ) or 0
        outstanding = await L.outstanding_user_balance(s)
    return {
        "mcd_total": int(mcd_total), "mcd_active": int(mcd_active),
        "kyash_total": int(kyash_total), "kyash_active": int(kyash_active),
        "orders_today": int(orders_today), "subsidy_today": int(subsidy_today),
        "review": int(review), "outstanding": int(outstanding),
    }


async def refresh_admin_panel(interaction: discord.Interaction) -> None:
    """パネル本文の数値を最新にする。ボタンはそのまま使い続ける。"""
    from ui.panels import AdminPanel

    await interaction.response.defer()
    stats = await collect_stats()
    try:
        await interaction.edit_original_response(
            embed=embeds.admin_panel(stats), view=AdminPanel()
        )
    except discord.HTTPException:
        log.exception("管理パネルの更新に失敗しました")


async def show_stats(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    stats = await collect_stats()
    week = datetime.now(timezone.utc) - timedelta(days=7)
    async with session_scope() as s:
        total_orders = await s.scalar(select(func.count()).select_from(Order)) or 0
        succeeded = await s.scalar(
            select(func.count()).select_from(Order).where(
                Order.state.in_([saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED])
            )
        ) or 0
        week_orders = await s.scalar(
            select(func.count()).select_from(Order).where(Order.created_at >= week)
        ) or 0
        gmv = await s.scalar(
            select(func.coalesce(func.sum(Order.list_price), 0)).where(
                Order.state.in_([saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED])
            )
        ) or 0
        subsidy_total = await s.scalar(
            select(func.coalesce(func.sum(-Ledger.amount), 0)).where(
                Ledger.account == "subsidy_pool", Ledger.amount < 0
            )
        ) or 0
        users = await s.scalar(select(func.count()).select_from(User)) or 0
        report = await L.verify_integrity(s)

    rate = (succeeded / total_orders * 100) if total_orders else 0.0
    e = discord.Embed(title=f"{E.CHART} 統計", color=embeds.BLUE)
    e.add_field(name="注文", value=f"累計 **{total_orders}** 件\n直近7日 **{week_orders}** 件\n本日 **{stats['orders_today']}** 件", inline=True)
    e.add_field(name="成功率", value=f"**{rate:.1f}%**\n（成功 {succeeded} 件）", inline=True)
    e.add_field(name="利用者", value=f"**{users}** 人", inline=True)
    e.add_field(name=f"{E.YEN} 流通総額", value=f"**{embeds.yen(int(gmv))}**", inline=True)
    e.add_field(name=f"{E.YEN} 負担総額", value=f"**{embeds.yen(int(subsidy_total))}**", inline=True)
    e.add_field(name=f"{E.WALLET} 未使用残高", value=f"**{embeds.yen(stats['outstanding'])}**", inline=True)
    integrity = (
        f"{E.OK} 正常（{report.checked} 取引）"
        if report.ok
        else f"{E.NG} **不整合あり**\n貸借不一致 {len(report.broken)} 件 / マイナス残高 {len(report.negative)} 件"
    )
    e.add_field(name="元帳の整合性", value=integrity, inline=False)
    if stats["review"]:
        e.add_field(name=f"{E.WARN} 要確認", value=f"**{stats['review']}** 件", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


async def show_subsidy(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with session_scope() as s:
        rules = (
            await s.execute(
                select(SubsidyRule).where(SubsidyRule.enabled.is_(True))
                .order_by(SubsidyRule.priority)
            )
        ).scalars().all()

    g = float(settings.get("subsidy_rate", 40.0))
    e = discord.Embed(title=f"{E.YEN} 負担率の設定", color=embeds.BLUE)
    e.add_field(
        name="全体の既定値",
        value=f"管理者負担 **{g:g}%** → 利用者の支払い **{100 - g:g}%**",
        inline=False,
    )
    if rules:
        lines = []
        for r in rules:
            target = (
                f"<@&{r.target_id}>" if r.scope == "role"
                else f"<@{r.target_id}>" if r.scope == "user" else "全体"
            )
            cap = f"／月上限 {embeds.yen(int(r.monthly_cap))}" if r.monthly_cap else ""
            lines.append(
                f"`#{r.id}` {target}　負担 **{float(r.subsidy_rate):g}%**"
                f"（優先度 {r.priority}）{cap}"
            )
        e.add_field(name="個別ルール", value="\n".join(lines)[:1024], inline=False)
    else:
        e.add_field(name="個別ルール", value="（未設定）", inline=False)
    e.set_footer(text="変更は /config subsidy コマンドから行えます")
    await interaction.followup.send(embed=e, ephemeral=True)


async def show_accounts(interaction: discord.Interaction) -> None:
    from services.kyash.accounts import token_days_left

    await interaction.response.defer(ephemeral=True, thinking=True)
    async with session_scope() as s:
        mcds = (await s.execute(select(McdAccount).order_by(McdAccount.id))).scalars().all()
        kyashes = (await s.execute(select(KyashAccount).order_by(KyashAccount.id))).scalars().all()
        mcd_rows = [
            (a.id, a.label, a.status, a.consecutive_failures, a.orders_today, bool(a.card_id))
            for a in mcds
        ]
        kyash_rows = [
            (a.id, a.label, a.status, a.is_kyc, a.received_this_month, token_days_left(a))
            for a in kyashes
        ]

    e = discord.Embed(title=f"{E.KEY} アカウント", color=embeds.BLUE)
    if mcd_rows:
        lines = []
        for aid, label, status, fails, today, has_card in mcd_rows:
            mark = E.HEALTH.get(status, E.GREY if hasattr(E, "GREY") else E.YELLOW)
            card = "" if has_card else f"　{E.WARN} カード未設定"
            fail = f"　失敗{fails}回" if fails else ""
            lines.append(f"{mark} `#{aid}` **{label}**　本日{today}件{fail}{card}")
        e.add_field(name="マクドナルド", value="\n".join(lines)[:1024], inline=False)
    else:
        e.add_field(name="マクドナルド", value=f"{E.WARN} 未登録（`/mcd add` で追加）", inline=False)

    if kyash_rows:
        lines = []
        for aid, label, status, kyc, received, days in kyash_rows:
            mark = E.HEALTH.get(status, E.YELLOW)
            kyc_s = "本人確認済" if kyc else "未確認"
            exp = ""
            if days is not None:
                exp = f"　{E.WARN} 残り{int(days)}日" if days <= 7 else f"　残り{int(days)}日"
            lines.append(
                f"{mark} `#{aid}` **{label}**　{kyc_s}　今月{embeds.yen(received)}{exp}"
            )
        e.add_field(name="Kyash", value="\n".join(lines)[:1024], inline=False)
    else:
        e.add_field(name="Kyash", value=f"{E.WARN} 未登録（`/kyash add` で追加）", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


class ReviewView(discord.ui.View):
    """要確認の注文を1件ずつ処理する。"""

    def __init__(self, orders: list[tuple[str, str, int, str]]) -> None:
        super().__init__(timeout=300)
        self.orders = orders
        self.index = 0
        self._build()

    def current(self):
        return self.orders[self.index] if self.index < len(self.orders) else None

    def build_embed(self) -> discord.Embed:
        cur = self.current()
        if cur is None:
            return embeds.ok("要確認の注文はありません。")
        order_id, receipt, amount, error = cur
        e = discord.Embed(
            title=f"{E.WARN} 要確認の注文（{self.index + 1}/{len(self.orders)}）",
            description=(
                "課金が成立している可能性があるため、自動返金は行っていません。\n"
                "マクドナルドのアプリや店舗で実際の状況を確認してから処理してください。"
            ),
            color=embeds.RED,
        )
        e.add_field(name="注文ID", value=f"`{order_id}`", inline=False)
        e.add_field(name="注文番号", value=receipt or "—", inline=True)
        e.add_field(name="利用者の支払額", value=embeds.yen(amount), inline=True)
        if error:
            e.add_field(name="エラー", value=f"```{error[:500]}```", inline=False)
        return e

    def _build(self) -> None:
        self.clear_items()
        if self.current() is None:
            return
        done = discord.ui.Button(label="注文は成立した（確定）", emoji=E.OK, style=discord.ButtonStyle.success)
        done.callback = self._on_done
        self.add_item(done)
        refund = discord.ui.Button(label="注文は不成立（返金）", emoji=E.YEN, style=discord.ButtonStyle.danger)
        refund.callback = self._on_refund
        self.add_item(refund)
        skip = discord.ui.Button(label="あとで", style=discord.ButtonStyle.secondary)
        skip.callback = self._on_skip
        self.add_item(skip)

    async def _resolve(self, interaction: discord.Interaction, refund: bool) -> None:
        cur = self.current()
        if cur is None:
            return
        try:
            await saga.resolve_review(cur[0], refund=refund)
        except Exception as e:
            await interaction.response.edit_message(embed=embeds.error(str(e)), view=None)
            return
        self.orders.pop(self.index)
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_done(self, interaction: discord.Interaction) -> None:
        await self._resolve(interaction, refund=False)

    async def _on_refund(self, interaction: discord.Interaction) -> None:
        await self._resolve(interaction, refund=True)

    async def _on_skip(self, interaction: discord.Interaction) -> None:
        self.index = (self.index + 1) % max(1, len(self.orders))
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


async def show_review(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    rows = await saga.list_manual_review()
    orders = [(o.id, o.receipt_number or "", o.user_amount, o.error or "") for o in rows]
    if not orders:
        await interaction.followup.send(embed=embeds.ok("要確認の注文はありません。"), ephemeral=True)
        return
    view = ReviewView(orders)
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


async def sync_menus(interaction: discord.Interaction) -> None:
    from services.mcd import accounts as mcd_accounts
    from services.mcd import stores as mcd_stores

    await interaction.response.defer(ephemeral=True, thinking=True)
    store_ids = await mcd_stores.active_store_ids()
    if not store_ids:
        await interaction.followup.send(
            embed=embeds.info("同期対象の店舗がまだありません。"), ephemeral=True
        )
        return

    handle = None
    lines = []
    try:
        handle = await mcd_accounts.pick_account()
        for store_id in store_ids[:20]:
            try:
                diff = await mcd_stores.sync_menu(handle.client, store_id)
                parts = []
                if diff.added:
                    parts.append(f"{E.PLUS}{len(diff.added)}")
                if diff.removed:
                    parts.append(f"{E.MINUS}{len(diff.removed)}")
                if diff.price_changed:
                    parts.append(f"{E.YEN}{len(diff.price_changed)}")
                lines.append(f"{E.OK} `{store_id}` " + ("　".join(parts) if parts else "変更なし"))
            except Exception as e:
                lines.append(f"{E.NG} `{store_id}` {str(e)[:60]}")
    except Exception as e:
        await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
        return
    finally:
        if handle:
            await handle.aclose()

    await interaction.followup.send(
        embed=discord.Embed(
            title=f"{E.SYNC} メニュー同期",
            description="\n".join(lines)[:4000],
            color=embeds.BLUE,
        ),
        ephemeral=True,
    )
