"""
PayPay受取口座の管理

Kyash 側（services/kyash/accounts.py）と同じ考え方で揃えてある。
違うのは

  ・トークンが90日もつ（Kyashは1ヶ月）
  ・**日本からしかアクセスできない** ので proxy_url の意味が重い
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

import config
from core.crypto import get_cipher
from db.models import PayPayAccount, as_utc
from db.session import session_scope
from services.paypay.client import PayPayClient, PayPayError, PayPaySession

log = logging.getLogger("bot.paypay.accounts")

STATUS_ACTIVE = "ACTIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_DISABLED = "DISABLED"


@dataclass
class PayPayHandle:
    account_id: int
    label: str
    client: PayPayClient

    async def aclose(self) -> None:
        await self.client.aclose()


def _dec(blob: bytes | None) -> str:
    if not blob:
        return ""
    try:
        return get_cipher().decrypt(blob)
    except Exception:
        log.exception("PayPayの保存情報を復号できませんでした")
        return ""


def build_client(acc: PayPayAccount) -> PayPayClient:
    """
    口座から通信用のクライアントを作る。

    ⚠️ proxy_url を必ず渡すこと。日本の外から叩くと弾かれる。
    """
    return PayPayClient(
        PayPaySession(
            access_token=_dec(acc.access_token_enc),
            refresh_token=_dec(acc.refresh_token_enc),
            device_uuid=acc.device_uuid or "",
            client_uuid=acc.client_uuid or "",
        ),
        proxy=acc.proxy_url or config_proxy(),
    )


def config_proxy() -> str | None:
    """全体に設定されたプロキシ（口座ごとの指定が無いときに使う）。"""
    from core import settings

    return str(settings.get("paypay_proxy", "") or "") or None


def token_days_left(acc: PayPayAccount) -> float | None:
    """
    アクセストークンの残り日数。

    ⚠️ アプリ側のトークンは90日もつ。Web側（2時間）と混同しないこと。
    """
    if not acc.token_obtained_at:
        return None
    elapsed = (datetime.now(timezone.utc) - as_utc(acc.token_obtained_at)).days
    return config.PAYPAY_TOKEN_LIFETIME_DAYS - elapsed


def _score(acc: PayPayAccount) -> float:
    """受取口座の選び方。今月の受取額に余裕がある口座を優先する。"""
    left = 1.0
    if acc.monthly_cap:
        used = acc.received_this_month or 0
        left = max(0.0, 1.0 - used / max(1, acc.monthly_cap))
    return left


async def pick_account(amount: int) -> PayPayHandle:
    """チャージを受ける口座を選ぶ。"""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(PayPayAccount).where(PayPayAccount.status == STATUS_ACTIVE)
            )
        ).scalars().all()
        usable = [
            a for a in rows
            if a.access_token_enc
            and (not a.monthly_cap or a.received_this_month + amount <= a.monthly_cap)
        ]
        if not usable:
            raise PayPayError(
                "チャージを受け付けられる PayPay アカウントがありません。"
                "管理者にお問い合わせください"
            )
        best = max(usable, key=_score)
        return PayPayHandle(
            account_id=best.id, label=best.label, client=build_client(best)
        )


async def record_received(
    account_id: int, amount: int, balance: int | None = None
) -> None:
    async with session_scope() as s:
        acc = await s.get(PayPayAccount, account_id)
        if acc is None:
            return
        acc.received_this_month = (acc.received_this_month or 0) + amount
        if balance is not None:
            acc.last_balance = balance
        acc.last_error = None


async def report_failure(account_id: int, error: str) -> None:
    async with session_scope() as s:
        acc = await s.get(PayPayAccount, account_id)
        if acc:
            acc.last_error = error[:500]
            acc.status = STATUS_DEGRADED


async def report_success_healthcheck(account_id: int) -> None:
    async with session_scope() as s:
        acc = await s.get(PayPayAccount, account_id)
        if acc is None:
            return
        acc.last_error = None
        if acc.status == STATUS_DEGRADED:
            acc.status = STATUS_ACTIVE
            log.info("PayPay口座 %s が復帰しました", acc.label)


async def healthcheck_all() -> list[tuple[int, str, bool]]:
    """全口座の生存確認。(id, label, 結果) を返す。"""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(PayPayAccount).where(PayPayAccount.status != STATUS_DISABLED)
            )
        ).scalars().all()
        targets = [
            PayPayHandle(account_id=a.id, label=a.label, client=build_client(a))
            for a in rows if a.access_token_enc
        ]

    results = []
    for handle in targets:
        alive = False
        try:
            alive = await handle.client.healthcheck()
        except Exception as e:
            await report_failure(handle.account_id, str(e))
        finally:
            await handle.aclose()
        if alive:
            await report_success_healthcheck(handle.account_id)
        results.append((handle.account_id, handle.label, alive))
    return results


async def reset_monthly_counters() -> None:
    async with session_scope() as s:
        for acc in (await s.execute(select(PayPayAccount))).scalars().all():
            acc.received_this_month = 0


async def expiring_accounts() -> list[tuple[int, str, float]]:
    """トークンの期限が近い口座。"""
    out = []
    async with session_scope() as s:
        for acc in (await s.execute(select(PayPayAccount))).scalars().all():
            left = token_days_left(acc)
            if left is not None and left <= config.PAYPAY_TOKEN_WARN_DAYS:
                out.append((acc.id, acc.label, left))
    return out


async def save_session(
    account_id: int, session: PayPaySession, *, obtained: bool = True
) -> None:
    """ログインで得たトークンを保存する。"""
    cipher = get_cipher()
    async with session_scope() as s:
        acc = await s.get(PayPayAccount, account_id)
        if acc is None:
            return
        acc.access_token_enc = cipher.encrypt(session.access_token)
        if session.refresh_token:
            acc.refresh_token_enc = cipher.encrypt(session.refresh_token)
        acc.device_uuid = session.device_uuid
        acc.client_uuid = session.client_uuid
        if obtained:
            from db.models import utcnow

            acc.token_obtained_at = utcnow()
        acc.status = STATUS_ACTIVE
        acc.last_error = None
