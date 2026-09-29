"""Kyash受取口座の管理"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import config
from core.crypto import get_cipher
from db.models import KyashAccount, utcnow
from db.session import session_scope
from services.kyash.client import KyashClient, KyashError, KyashSession

log = logging.getLogger("bot.kyash.accounts")

STATUS_ACTIVE = "ACTIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_DISABLED = "DISABLED"


@dataclass
class KyashHandle:
    account_id: int
    label: str
    client: KyashClient

    async def aclose(self) -> None:
        await self.client.aclose()


def build_client(acc: KyashAccount) -> KyashClient:
    cipher = get_cipher()
    return KyashClient(
        KyashSession(
            access_token=cipher.decrypt(acc.access_token_enc) or "",
            client_uuid=acc.client_uuid or "",
            installation_uuid=acc.installation_uuid or "",
        ),
        proxy=acc.proxy_url,
    )


def token_days_left(acc: KyashAccount) -> float | None:
    """アクセストークンの残り日数。1ヶ月で失効する。"""
    if not acc.token_obtained_at:
        return None
    obtained = acc.token_obtained_at
    if obtained.tzinfo is None:
        obtained = obtained.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - obtained).days
    return config.KYASH_TOKEN_LIFETIME_DAYS - elapsed


def _score(acc: KyashAccount) -> float:
    """
    受取口座の選び方。

      ・本人確認済みを優先（受取上限が高い）
      ・今月の受取額に余裕がある口座を優先
      ・トークンの期限が近い口座は避ける
    """
    score = 3.0 if acc.is_kyc else 0.0
    if acc.monthly_cap:
        score += 2.0 * max(0.0, 1.0 - acc.received_this_month / acc.monthly_cap)
    else:
        score += 2.0
    left = token_days_left(acc)
    if left is not None:
        score += min(left / config.KYASH_TOKEN_LIFETIME_DAYS, 1.0)
    else:
        score += 1.0
    return score


async def pick_account(amount: int) -> KyashHandle:
    """チャージを受ける口座を選ぶ。"""
    async with session_scope() as s:
        rows = (
            await s.execute(select(KyashAccount).where(KyashAccount.status == STATUS_ACTIVE))
        ).scalars().all()
        usable = [
            a for a in rows
            if a.access_token_enc
            and (not a.monthly_cap or a.received_this_month + amount <= a.monthly_cap)
        ]
        if not usable:
            raise KyashError(
                "チャージを受け付けられる Kyash アカウントがありません。"
                "管理者にお問い合わせください"
            )
        best = max(usable, key=_score)
        return KyashHandle(account_id=best.id, label=best.label, client=build_client(best))


async def record_received(account_id: int, amount: int, balance: int | None = None) -> None:
    async with session_scope() as s:
        acc = await s.get(KyashAccount, account_id)
        if acc:
            acc.received_this_month += amount
            if balance is not None:
                acc.last_balance = balance
            acc.last_error = None


async def report_failure(account_id: int, error: str) -> None:
    async with session_scope() as s:
        acc = await s.get(KyashAccount, account_id)
        if acc:
            acc.last_error = error[:500]
            acc.status = STATUS_DEGRADED


async def reset_monthly_counters() -> None:
    async with session_scope() as s:
        for acc in (await s.execute(select(KyashAccount))).scalars().all():
            acc.received_this_month = 0


async def expiring_accounts() -> list[tuple[int, str, float]]:
    """トークンの期限が近い口座。管理者へ通知する。"""
    out = []
    async with session_scope() as s:
        for acc in (await s.execute(select(KyashAccount))).scalars().all():
            left = token_days_left(acc)
            if left is not None and left <= config.KYASH_TOKEN_WARN_DAYS:
                out.append((acc.id, acc.label, left))
    return out
