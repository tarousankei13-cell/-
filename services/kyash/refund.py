"""
返金（実際にお金をお返しする）

残高を戻すだけの「取り消し」とは別物。Kyash の送金リンクを作って
利用者にお渡しし、**現金が口座から出ていく**。

⚠️ 歯止めを重ねてある。
   1. 既定で無効（/config refund on で管理者が明示的に開ける）
   2. 1回あたりの上限（refund_max）
   3. **先に残高を引いてから** リンクを作る
      （作ってから引くと、作れたのに引けなかったときに二重取りになる）
   4. リンクを作れなかったら残高を戻す
   5. 同じ申請で二度送らない（refunds の主キー）
   6. 監査ログに必ず残す

⚠️ 作ったリンクは **利用者が開くまで受け取られない**。
   開かれたかどうかはここでは分からないので、状態は PENDING のまま。
   取り消したいときは Kyash アプリ側で行うこと。
"""

from __future__ import annotations

import logging
import uuid

import config
from core import ledger as L
from core import settings
from db.models import Refund
from db.session import session_scope, user_scope

log = logging.getLogger("bot.refund")


class RefundError(Exception):
    """そのまま見せてよい、返金できない理由。"""


def enabled() -> bool:
    return bool(settings.get("refund_enabled", config.REFUND_ENABLED))


def max_amount() -> int:
    return max(0, int(settings.get("refund_max", config.REFUND_MAX)))


async def send(
    discord_id: int, amount: int, *, reason: str = "", requested_by: int | None = None
) -> Refund:
    """
    残高を引いて、その額の送金リンクを作る。

    返り値の link_url を利用者に伝えること。
    """
    from services.kyash import accounts as kyash_accounts

    if not enabled():
        raise RefundError(
            "いまは返金を受け付けていません。\n"
            "（管理者の方へ: `/config refund` で有効にしてください）"
        )
    amount = int(amount)
    if amount <= 0:
        raise RefundError("返金額は1円以上で指定してください。")
    if max_amount() and amount > max_amount():
        raise RefundError(
            f"1回に返金できるのは ¥{max_amount():,} までです。"
        )

    refund_id = str(uuid.uuid4())

    # ---- ① 先に残高を引く ----
    # ⚠️ 順番が要。リンクを先に作ると、作れたのに引けなかったときに
    #    お金だけ出ていく。引けなければ、ここで止まるのが正しい。
    try:
        async with user_scope(discord_id) as s:
            balance = await L.user_balance(s, discord_id)
            if balance < amount:
                raise RefundError(
                    f"残高が足りません。\n"
                    f"いまの残高 **¥{balance:,}** ／ 返金額 **¥{amount:,}**"
                )
            await L.adjust(s, discord_id, -amount, memo=f"返金 {reason}".strip())
    except L.LedgerError as e:
        raise RefundError(f"残高を引けませんでした: {e}") from e

    async with session_scope() as s:
        s.add(Refund(
            id=refund_id, discord_id=discord_id, amount=amount,
            reason=reason or None, requested_by=requested_by, status="PENDING",
        ))

    # ---- ② 送金リンクを作る ----
    handle = None
    try:
        handle = await kyash_accounts.pick_account(0)
        url = await handle.client.create_link(
            amount, message=reason or "返金"
        )
        if not url:
            raise RefundError("送金リンクを作れませんでした。")
    except Exception as e:
        # ⚠️ 作れなかったら残高を戻す。引いたままにしない。
        log.exception("返金の送金リンクを作れませんでした")
        try:
            async with user_scope(discord_id) as s:
                await L.adjust(s, discord_id, amount, memo="返金の取り消し")
        except L.LedgerError:
            log.exception("返金の取り消しに失敗しました（要確認）: %s", refund_id)
        async with session_scope() as s:
            row = await s.get(Refund, refund_id)
            if row is not None:
                row.status = "FAILED"
                row.error = str(e)[:500]
        raise RefundError(
            "送金リンクを作れませんでした。残高は元に戻しています。\n"
            f"（{str(e)[:80]}）"
        ) from e
    finally:
        if handle:
            await handle.aclose()

    async with session_scope() as s:
        row = await s.get(Refund, refund_id)
        row.link_url = url[:255]
        row.kyash_account_id = handle.account_id if handle else None
        s.expunge(row)
        made = row

    log.info("返金リンクを作りました: %s へ ¥%d", discord_id, amount)
    return made


async def history(discord_id: int | None = None, limit: int = 10) -> list[Refund]:
    """返金の記録。discord_id を省くと全体。"""
    from sqlalchemy import select

    async with session_scope() as s:
        q = select(Refund).order_by(Refund.created_at.desc()).limit(limit)
        if discord_id is not None:
            q = q.where(Refund.discord_id == discord_id)
        rows = (await s.execute(q)).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)
