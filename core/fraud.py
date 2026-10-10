"""
不正利用の検知

お金が絡む以上、誰かが試す前提で備えておく。
ただし**疑わしきは止めない**のが基本方針にしてある。

  ・ふつうに使っている人を誤って止めるほうが、取りこぼしより痛い
  ・判断は人がする。BOTは「気付いて知らせる」ところまで
  ・自動で止めるかどうかは管理者が選べる（既定は通知だけ）

見ているもの:

  短時間に何度も注文      いたずらや動作確認の可能性
  同じ送金リンクの再利用  チャージの二重取りの試み
  極端に高額な注文        誤操作か、意図的なもの
  チャージ直後の高額注文  他人の送金リンクを使った疑い
  複数人で同じKyash口座   アカウントの貸し借り
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select

import config
from core import saga
from db.models import Ledger, Order
from db.session import session_scope

log = logging.getLogger("bot.fraud")

# 深刻さ
INFO = "info"
WARN = "warn"
HIGH = "high"

_LABEL = {INFO: "参考", WARN: "注意", HIGH: "要対応"}


@dataclass
class Finding:
    """見つけたもの1件。"""
    kind: str
    level: str
    discord_id: int
    summary: str
    detail: str = ""

    @property
    def level_label(self) -> str:
        return _LABEL.get(self.level, self.level)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.findings)

    @property
    def worst(self) -> str:
        for lv in (HIGH, WARN, INFO):
            if any(f.level == lv for f in self.findings):
                return lv
        return INFO

    def for_user(self, discord_id: int) -> list[Finding]:
        return [f for f in self.findings if f.discord_id == discord_id]


def _enabled(key: str, default: bool = True) -> bool:
    from core import settings

    return bool(settings.get(f"fraud_{key}", default))


def _limit(key: str, default):
    from core import settings

    return settings.get(f"fraud_{key}", default)


async def check_user(discord_id: int) -> Report:
    """
    注文の直前に、その人だけを見る。

    重い処理はしない。注文を待たせないため。
    """
    report = Report()
    # ⚠️ SQLの比較に使うのでUTCで扱う。日本時間のままだと9時間ずれる。
    now = config.utcnow_naive()
    DONE = [saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED]

    async with session_scope() as s:
        # ① 短時間に何度も注文
        window = int(_limit("burst_minutes", 10))
        cap = int(_limit("burst_count", 5))
        since = now - timedelta(minutes=window)
        recent = int(
            await s.scalar(
                select(func.count()).select_from(Order).where(
                    Order.discord_id == discord_id,
                    Order.created_at >= since,
                )
            ) or 0
        )
        if _enabled("burst") and recent >= cap:
            report.findings.append(Finding(
                kind="burst", level=WARN, discord_id=discord_id,
                summary=f"{window}分で {recent} 回注文しています",
                detail=f"上限の目安は {cap} 回です。",
            ))

        # ② チャージ直後の高額注文
        if _enabled("quick_spend"):
            minutes = int(_limit("quick_spend_minutes", 5))
            threshold = int(_limit("quick_spend_amount", 5000))
            fresh = (
                await s.execute(
                    select(Ledger)
                    .where(
                        Ledger.account == f"user:{discord_id}",
                        Ledger.amount >= threshold,
                        Ledger.created_at >= now - timedelta(minutes=minutes),
                    )
                    .limit(1)
                )
            ).scalars().first()
            if fresh is not None:
                report.findings.append(Finding(
                    kind="quick_spend", level=INFO, discord_id=discord_id,
                    summary=f"{minutes}分以内に {fresh.amount:,}円 をチャージしています",
                    detail="他人の送金リンクを使っている可能性もあるため、念のため。",
                ))

        # ③ 今月の利用が突出している
        if _enabled("heavy"):
            cap_month = int(_limit("heavy_monthly", 50000))
            # 月の区切りは日本時間で決め、比較用にUTCへ直す
            month_start = config.jst_month_start_utc()
            spent = int(
                await s.scalar(
                    select(func.coalesce(func.sum(Order.user_amount), 0)).where(
                        Order.discord_id == discord_id,
                        Order.created_at >= month_start,
                        Order.state.in_(DONE),
                    )
                ) or 0
            )
            if spent >= cap_month:
                report.findings.append(Finding(
                    kind="heavy", level=INFO, discord_id=discord_id,
                    summary=f"今月の利用が {spent:,}円 に達しています",
                ))

    return report


async def check_order(discord_id: int, list_price: int) -> Report:
    """これから出す注文そのものを見る。"""
    report = Report()
    if _enabled("big_order"):
        threshold = int(_limit("big_order_amount", 10000))
        if list_price >= threshold:
            report.findings.append(Finding(
                kind="big_order", level=WARN, discord_id=discord_id,
                summary=f"1回の注文が {list_price:,}円 です",
                detail=f"{threshold:,}円 以上の注文は目に留まるようにしています。",
            ))
    return report


async def scan_all() -> Report:
    """
    全体を見渡す。定期処理から呼ぶ。

    1人ずつでは気付けない、またがった異常を探す。
    """
    report = Report()
    now = config.utcnow_naive()   # SQLの比較に使うのでUTC

    async with session_scope() as s:
        # ④ 同じ送金リンクが何度も使われた
        if _enabled("reused_link"):
            dup = (
                await s.execute(
                    select(Ledger.receipt_id, func.count().label("n"))
                    .where(Ledger.receipt_id.is_not(None))
                    .group_by(Ledger.receipt_id)
                    .having(func.count() > 2)
                    .limit(20)
                )
            ).all()
            for receipt_id, n in dup:
                # 1回のチャージで複数行の記帳が入るのは正常。
                # 想定より多い場合だけ拾う。
                report.findings.append(Finding(
                    kind="reused_link", level=HIGH, discord_id=0,
                    summary=f"同じ受取番号が {n} 回使われています",
                    detail=f"受取番号 `{receipt_id}`",
                ))

        # ⑤ 残高がマイナスの人がいないか（元帳の異常）
        rows = (
            await s.execute(
                select(Ledger.account, func.sum(Ledger.amount).label("bal"))
                .where(Ledger.account.like("user:%"))
                .group_by(Ledger.account)
                .having(func.sum(Ledger.amount) < 0)
                .limit(20)
            )
        ).all()
        for account, bal in rows:
            try:
                uid = int(str(account).split(":")[1])
            except (IndexError, ValueError):
                uid = 0
            report.findings.append(Finding(
                kind="negative", level=HIGH, discord_id=uid,
                summary=f"残高がマイナスになっています（{int(bal):,}円）",
                detail="元帳の不整合か、処理の取りこぼしの可能性があります。",
            ))

        # ⑥ 短時間に集中して注文している人
        if _enabled("burst"):
            window = int(_limit("burst_minutes", 10))
            cap = int(_limit("burst_count", 5))
            since = now - timedelta(minutes=window)
            busy = (
                await s.execute(
                    select(Order.discord_id, func.count().label("n"))
                    .where(Order.created_at >= since)
                    .group_by(Order.discord_id)
                    .having(func.count() >= cap)
                    .limit(20)
                )
            ).all()
            for uid, n in busy:
                report.findings.append(Finding(
                    kind="burst", level=WARN, discord_id=int(uid),
                    summary=f"{window}分で {n} 回注文しています",
                ))

    if report.any:
        log.warning("気になる動きを %d 件見つけました", len(report.findings))
    return report


def format_report(r: Report) -> str:
    """管理者への通知文。"""
    if not r.any:
        return ""
    lines = []
    for lv in (HIGH, WARN, INFO):
        group = [f for f in r.findings if f.level == lv]
        if not group:
            continue
        lines.append(f"**{_LABEL[lv]}**")
        for f in group[:10]:
            who = f"<@{f.discord_id}>" if f.discord_id else ""
            lines.append(f"　{who} {f.summary}")
            if f.detail:
                lines.append(f"　　{f.detail}")
        if len(group) > 10:
            lines.append(f"　…ほか {len(group) - 10} 件")
    lines.append(
        "\n⚠️ これは**自動で止めたものではありません**。"
        "内容を見て判断してください。"
    )
    return "\n".join(lines)
