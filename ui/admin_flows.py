"""管理者パネルの操作"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config
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
            mark = E.HEALTH.get(status, E.YELLOW)
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
    await interaction.followup.send(
        embed=e, view=AccountHealthView(mcd_rows), ephemeral=True
    )


# ============================================================
#  利用者カード（管理5）
# ============================================================

async def show_user(interaction: discord.Interaction, user: discord.User) -> None:
    """
    1人分の情報を1画面にまとめる。

    残高・利用状況・適用中の負担率とその理由・直近の注文・チャージ履歴。
    複数のコマンドを行き来しないと全体像がつかめない状態を解消する。
    """
    from datetime import datetime, timezone

    from core import ledger as L, saga, subsidy
    from db.models import as_utc, Ledger, Order, User

    await interaction.response.defer(ephemeral=True, thinking=True)
    discord_id = user.id

    role_ids = [r.id for r in getattr(user, "roles", [])] if hasattr(user, "roles") else []
    month_start = config.now_jst().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    async with session_scope() as s:
        row = await s.get(User, discord_id)
        balance = await L.user_balance(s, discord_id)
        quote = await subsidy.resolve(s, discord_id, role_ids, 1000)

        orders = (
            await s.execute(
                select(Order)
                .where(Order.discord_id == discord_id)
                .order_by(Order.created_at.desc())
                .limit(5)
            )
        ).scalars().all()
        recent = [
            (as_utc(o.created_at), o.state, o.store_name or o.store_id or "",
             o.user_amount, o.list_price, o.receipt_number or "")
            for o in orders
        ]

        charges = (
            await s.execute(
                select(Ledger)
                .where(
                    Ledger.account == f"user:{discord_id}",
                    Ledger.amount > 0,
                )
                .order_by(Ledger.created_at.desc())
                .limit(3)
            )
        ).scalars().all()
        charge_rows = [(as_utc(c.created_at), c.amount, c.memo or "") for c in charges]

        month_spent = int(
            await s.scalar(
                select(func.coalesce(func.sum(Order.user_amount), 0)).where(
                    Order.discord_id == discord_id,
                    Order.created_at >= month_start,
                    Order.state.in_([saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED]),
                )
            ) or 0
        )
        month_subsidy = int(
            await s.scalar(
                select(
                    func.coalesce(func.sum(Order.list_price - Order.user_amount), 0)
                ).where(
                    Order.discord_id == discord_id,
                    Order.created_at >= month_start,
                    Order.state.in_([saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED]),
                )
            ) or 0
        )

    banned = bool(row and row.is_banned)
    e = discord.Embed(
        title=f"{E.USER} {user}",
        description=(
            f"{E.BAN} **利用停止中**" + (f"\n理由: {row.note}" if row and row.note else "")
            if banned else f"`{discord_id}`"
        ),
        color=embeds.RED if banned else embeds.BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name=f"{E.WALLET} 残高", value=f"**{embeds.yen(balance)}**", inline=True)
    e.add_field(
        name=f"{E.FRIES} 通算注文",
        value=f"**{(row.total_orders if row else 0):,}** 回",
        inline=True,
    )
    e.add_field(
        name=f"{E.CHART} 今月",
        value=f"支払 {embeds.yen(month_spent)}\n負担 {embeds.yen(month_subsidy)}",
        inline=True,
    )

    user_rate = 100 - quote.subsidy_rate
    e.add_field(
        name=f"{E.YEN} 適用中の負担率",
        value=(
            f"管理者負担 **{quote.subsidy_rate:g}%**（利用者 {user_rate:g}%）\n"
            f"{E.INFO} 根拠: **{quote.source}**"
        ),
        inline=False,
    )

    if recent:
        lines = []
        for when, state, store, amount, price, receipt in recent:
            mark = {
                saga.COMPLETED: E.OK, saga.NOTIFIED: E.OK, saga.CAPTURED: E.OK,
                saga.REFUNDED: E.NG, saga.MANUAL_REVIEW: E.WARN,
            }.get(state, E.LOADING)
            stamp = f"<t:{int(when.timestamp())}:R>" if when else "—"
            num = f"　`{receipt}`" if receipt else ""
            lines.append(
                f"{mark} {stamp}　{store or '—'}　{embeds.yen(amount)}"
                f"（定価 {embeds.yen(price)}）{num}"
            )
        e.add_field(name=f"{E.HISTORY} 直近の注文", value="\n".join(lines)[:1024],
                    inline=False)
    else:
        e.add_field(name=f"{E.HISTORY} 直近の注文", value="まだありません", inline=False)

    if charge_rows:
        lines = [
            f"<t:{int(w.timestamp())}:R>　**+{embeds.yen(a)}**"
            + (f"　{m[:40]}" if m else "")
            for w, a, m in charge_rows
        ]
        e.add_field(name=f"{E.CHARGE} 直近のチャージ", value="\n".join(lines)[:1024],
                    inline=False)

    await interaction.followup.send(
        embed=e, view=UserCardView(discord_id, banned), ephemeral=True
    )


class UserCardView(discord.ui.View):
    """利用者カードから、そのまま手当てできるようにする。"""

    def __init__(self, discord_id: int, banned: bool) -> None:
        super().__init__(timeout=300)
        self.discord_id = discord_id

        hist = discord.ui.Button(
            label="注文履歴をすべて見る", emoji=E.HISTORY,
            style=discord.ButtonStyle.secondary,
        )
        hist.callback = self._on_history
        self.add_item(hist)

        audit_btn = discord.ui.Button(
            label="この人への操作記録", emoji=E.NOTE,
            style=discord.ButtonStyle.secondary,
        )
        audit_btn.callback = self._on_audit
        self.add_item(audit_btn)

    async def _on_history(self, interaction: discord.Interaction) -> None:
        from core import saga
        from db.models import as_utc, Order

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(Order)
                    .where(Order.discord_id == self.discord_id)
                    .order_by(Order.created_at.desc())
                    .limit(25)
                )
            ).scalars().all()
            items = [
                (as_utc(o.created_at), o.state, o.store_name or "",
                 o.user_amount, o.receipt_number or "")
                for o in rows
            ]
        if not items:
            await interaction.followup.send(
                embed=embeds.info("注文はまだありません。"), ephemeral=True
            )
            return
        lines = [
            f"{'✅' if st in (saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED) else '⚠️'}"
            f" <t:{int(w.timestamp())}:f>　{store or '—'}　{embeds.yen(amt)}"
            + (f"　`{num}`" if num else "")
            for w, st, store, amt, num in items
        ]
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.HISTORY} 注文履歴（最新{len(items)}件）",
                description="\n".join(lines)[:4000],
                color=embeds.BLUE,
            ),
            ephemeral=True,
        )

    async def _on_audit(self, interaction: discord.Interaction) -> None:
        from core import audit

        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await audit.search(target=str(self.discord_id), limit=20)
        if not rows:
            await interaction.followup.send(
                embed=embeds.info("この利用者への操作は記録されていません。"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.NOTE} この利用者への操作記録",
                description="\n\n".join(r.line() for r in rows)[:4000],
                color=embeds.BLUE,
            ),
            ephemeral=True,
        )


# ============================================================
#  アカウントの健全性（管理1）
# ============================================================

class AccountHealthView(discord.ui.View):
    """
    アカウントの状態を見て、その場で手当てできるようにする。

    一覧を見て「このアカウントが止まっている」と分かっても、
    別のコマンドを打ちに行くのでは手間がかかる。
    """

    def __init__(self, mcd_rows: list) -> None:
        super().__init__(timeout=300)
        self.mcd_rows = mcd_rows
        self._build()

    def _build(self) -> None:
        self.clear_items()
        detail = discord.ui.Button(
            label="詳しく見る", emoji=E.CHART, style=discord.ButtonStyle.primary, row=0
        )
        detail.callback = self._on_detail
        self.add_item(detail)

        # 止まっているアカウントがあれば、戻すボタンを出す
        from core import breaker

        blocked = [b for b in breaker.accounts.snapshot() if b.state != breaker.CLOSED]
        if blocked:
            revive = discord.ui.Button(
                label=f"止まっている{len(blocked)}件を戻す", emoji=E.SYNC,
                style=discord.ButtonStyle.success, row=0,
            )
            revive.callback = self._on_revive
            self.add_item(revive)

        quarantined = [r for r in self.mcd_rows if r[2] not in ("ACTIVE", "DEGRADED")]
        if quarantined:
            unlock = discord.ui.Button(
                label=f"隔離中の{len(quarantined)}件を復帰", emoji=E.OK,
                style=discord.ButtonStyle.secondary, row=0,
            )
            unlock.callback = self._on_unlock
            self.add_item(unlock)

        check = discord.ui.Button(
            label="いま確かめる", emoji=E.SYNC, style=discord.ButtonStyle.secondary, row=1
        )
        check.callback = self._on_check
        self.add_item(check)

    async def _on_detail(self, interaction: discord.Interaction) -> None:
        """1件ずつの詳しい状態。何をすべきかまで書く。"""
        from core import breaker
        from db.models import as_utc
        from services.kyash.accounts import token_days_left

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            mcds = (
                await s.execute(select(McdAccount).order_by(McdAccount.id))
            ).scalars().all()
            rows = [
                (a.id, a.label, a.status, a.consecutive_failures, a.orders_today,
                 bool(a.card_id), a.last_error or "", as_utc(a.last_used_at))
                for a in mcds
            ]
            kyashes = (
                await s.execute(select(KyashAccount).order_by(KyashAccount.id))
            ).scalars().all()
            krows = [(a.id, a.label, a.status, token_days_left(a)) for a in kyashes]

        e = discord.Embed(title=f"{E.KEY} アカウントの詳しい状態", color=embeds.BLUE)
        for aid, label, status, fails, today, has_card, err, last_used in rows:
            mark = E.HEALTH.get(status, E.YELLOW)
            br = breaker.accounts.get(f"mcd:{aid}")
            body = [f"状態 **{status}**（{br.describe()}）"]
            body.append(f"本日 {today} 件　連続失敗 {fails} 回")
            if last_used:
                body.append(f"最後に使用 <t:{int(last_used.timestamp())}:R>")
            if not has_card:
                body.append(f"{E.WARN} **カード未設定** → `/mcd card {aid}`")
            if err:
                body.append(f"{E.NG} `{err[:90]}`")
            todo = _account_todo(status, has_card, err)
            if todo:
                body.append(f"{E.INFO} {todo}")
            e.add_field(
                name=f"{mark} #{aid} {label}", value="\n".join(body)[:1024], inline=False
            )
        for aid, label, status, days in krows:
            mark = E.HEALTH.get(status, E.YELLOW)
            body = [f"状態 **{status}**"]
            if days is not None:
                body.append(
                    f"トークン残り **{int(days)}日**"
                    + (f"　{E.WARN} `/kyash relogin {aid}` を" if days <= 7 else "")
                )
            e.add_field(
                name=f"{mark} Kyash #{aid} {label}", value="\n".join(body)[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=e, ephemeral=True)

    async def _on_revive(self, interaction: discord.Interaction) -> None:
        """一時的に止めている経路を戻す。"""
        from core import audit, breaker

        blocked = [b.name for b in breaker.accounts.snapshot()
                   if b.state != breaker.CLOSED]
        breaker.accounts.reset_all()
        breaker.groups.reset_all()
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.quarantine", target="breaker",
            before=",".join(blocked)[:200], after="戻した",
        )
        await interaction.response.send_message(
            embed=embeds.ok(
                f"止めていた経路を戻しました（{len(blocked)}件）。\n"
                f"{E.INFO} 原因が直っていなければ、また止まります。"
            ),
            ephemeral=True,
        )

    async def _on_unlock(self, interaction: discord.Interaction) -> None:
        """隔離したアカウントを使える状態に戻す。"""
        from core import audit, breaker
        from services.mcd.accounts import STATUS_ACTIVE

        await interaction.response.defer(ephemeral=True, thinking=True)
        restored = []
        async with session_scope() as s:
            rows = (await s.execute(select(McdAccount))).scalars().all()
            for a in rows:
                if a.status not in ("ACTIVE", "DEGRADED", "BANNED"):
                    a.status = STATUS_ACTIVE
                    a.consecutive_failures = 0
                    a.last_error = None
                    restored.append(f"#{a.id} {a.label}")
                    breaker.accounts.get(f"mcd:{a.id}").reset()
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.quarantine", target="restore",
            after=", ".join(restored)[:500],
        )
        await interaction.followup.send(
            embed=embeds.ok(
                f"{len(restored)}件を使える状態に戻しました。\n"
                + ("\n".join(f"・{r}" for r in restored) if restored else "")
                + f"\n\n{E.WARN} カードの残高など、原因が直っているか確認してください。"
            ),
            ephemeral=True,
        )

    async def _on_check(self, interaction: discord.Interaction) -> None:
        """いま本当に使えるか、実際に確かめる。"""
        from services.mcd import accounts as mcd_accounts

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await mcd_accounts.healthcheck_all()
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error(f"確認できませんでした。\n```{e}```"), ephemeral=True
            )
            return
        good = [r for r in results if r[2]]
        bad = [r for r in results if not r[2]]
        e = discord.Embed(
            title=f"{E.KEY} 確認しました",
            description=f"使える **{len(good)}** / {len(results)} 件",
            color=embeds.GREEN if not bad else embeds.ORANGE,
        )
        if bad:
            e.add_field(
                name=f"{E.NG} 使えないアカウント",
                value="\n".join(f"`#{i}` {lbl}" for i, lbl, _ in bad)[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=e, ephemeral=True)


def _account_todo(status: str, has_card: bool, error: str) -> str:
    """何をすべきかを一言で。"""
    if not has_card:
        return "決済カードを設定してください"
    low = (error or "").lower()
    if "決済" in error or "payment" in low or "残高" in error:
        return "カードの残高・利用限度額・有効期限を確認してください"
    if "認証" in error or "auth" in low or "token" in low:
        return "再ログインしてください"
    if status not in ("ACTIVE", "DEGRADED"):
        return "原因を直してから「隔離中の件を復帰」を押してください"
    return ""


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
