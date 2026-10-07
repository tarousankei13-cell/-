"""Kyash受取口座の管理"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

import config
from core import crypto
from db.models import as_utc, KyashAccount
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
    return KyashClient(
        KyashSession(
            # ⚠️ 鍵が合わなければ空にする。ここで落とすと、チャージの
            #    途中で理由の分からない失敗になる。
            access_token=crypto.try_decrypt(acc.access_token_enc) or "",
            client_uuid=acc.client_uuid or "",
            installation_uuid=acc.installation_uuid or "",
        ),
        proxy=acc.proxy_url,
    )


def token_days_left(acc: KyashAccount) -> float | None:
    """アクセストークンの残り日数。1ヶ月で失効する。"""
    if not acc.token_obtained_at:
        return None
    obtained = as_utc(acc.token_obtained_at)
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
    # 保存前のオブジェクトでは列の既定値がまだ入っていないので 0 を補う
    if acc.monthly_cap:
        score += 2.0 * max(
            0.0, 1.0 - (acc.received_this_month or 0) / acc.monthly_cap
        )
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


async def report_success_healthcheck(account_id: int) -> None:
    """生存確認に成功した。落としていた口座は戻す。"""
    async with session_scope() as s:
        acc = await s.get(KyashAccount, account_id)
        if acc is None:
            return
        acc.last_error = None
        if acc.status == STATUS_DEGRADED:
            acc.status = STATUS_ACTIVE
            log.info("Kyash口座 %s が復帰しました", acc.label)


async def healthcheck_all() -> list[tuple[int, str, bool]]:
    """
    全口座の生存確認。(id, label, 結果) を返す。

    ⚠️ Kyash のトークンは1ヶ月で切れ、取り直しにはOTPが必要なため
       **自動では更新できない**。切れる前に気付けるよう、
       残り日数の通知とは別に、実際に通信して確かめておく。
       （凍結・ログアウトは期限とは関係なく起きる）
    """
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(KyashAccount).where(KyashAccount.status != STATUS_DISABLED)
            )
        ).scalars().all()
        # ⚠️ トークンがまだ無い口座は、確かめる相手がいない。
        #    「応答しない」と報告すると管理者を驚かせるので外す。
        targets = [
            KyashHandle(account_id=a.id, label=a.label, client=build_client(a))
            for a in rows if a.access_token_enc
        ]

    results = []
    for handle in targets:
        alive = False
        try:
            alive = await handle.client.healthcheck()
        except Exception as e:                   # 通信も認証も落ちうる
            await report_failure(handle.account_id, str(e))
        finally:
            await handle.aclose()
        if alive:
            await report_success_healthcheck(handle.account_id)
        results.append((handle.account_id, handle.label, alive))
    return results


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
