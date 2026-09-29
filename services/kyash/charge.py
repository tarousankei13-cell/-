"""
チャージ処理

利用者が Kyash で作った送金リンクを BOT が受け取り、内部残高に反映する。

手順（docs/03 §3.1）— この順序を変えてはいけない:
  ① リンクを確認（受け取りリンクか / 金額 / 使用済みでないか）
  ② DBに予約を入れる（link_uuid の UNIQUE 制約で二重受取を防ぐ）
  ③ 受取前の残高を記録
  ④ 受け取りを実行  ← ここで実際にお金が動く
  ⑤ 受取後の残高を検証（差分が金額と一致するか）
  ⑥ 元帳に記帳して内部残高へ反映

⑤で不一致なら記帳せず、管理者の確認へ回す。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from core import ledger as L
from core import settings
from db.models import KyashReceipt
from db.session import session_scope, user_scope
from services.kyash import accounts as kyash_accounts
from services.kyash.client import KyashError, LinkAlreadyUsed

log = logging.getLogger("bot.kyash.charge")

RESERVED = "RESERVED"
RECEIVING = "RECEIVING"
RECEIVED = "RECEIVED"
CREDITED = "CREDITED"
FAILED = "FAILED"
MANUAL_REVIEW = "MANUAL_REVIEW"


class ChargeError(Exception):
    """利用者にそのまま見せてよいエラー。"""


@dataclass
class ChargeResult:
    amount: int
    balance: int
    sender_name: str = ""
    receipt_id: str = ""


async def charge_from_link(discord_id: int, url: str) -> ChargeResult:
    charge_min = int(settings.get("charge_min", 100))
    charge_max = int(settings.get("charge_max", 50_000))

    handle = None
    checker = None
    receipt_id = str(uuid.uuid4())
    try:
        # ① リンクの確認。金額が分かるまで口座を決められないので、
        #    まず任意の口座で読み取りだけ行う（link_check は読み取り専用）。
        checker = await kyash_accounts.pick_account(charge_min)
        try:
            info = await checker.client.link_check(url)
        except LinkAlreadyUsed as e:
            raise ChargeError(f"このリンクは使用できません。{e}") from e

        if not info.send_to_me:
            raise ChargeError(
                "請求リンクは受け取れません。Kyashアプリで「送金リンク」を作成してください"
            )
        if info.amount < charge_min:
            raise ChargeError(f"チャージは ¥{charge_min:,} 以上から受け付けています")
        if info.amount > charge_max:
            raise ChargeError(f"1回のチャージは ¥{charge_max:,} までです")

        # ★金額が確定したので、その額を受けられる口座を選び直す。
        #   月間の受取上限を超えないようにするため。
        await checker.aclose()
        handle, checker = await kyash_accounts.pick_account(info.amount), None

        # ② 予約（同じリンクを2回使わせない）
        async with session_scope() as s:
            s.add(
                KyashReceipt(
                    id=receipt_id, link_uuid=info.uuid,
                    kyash_account_id=handle.account_id, discord_id=discord_id,
                    amount=info.amount, sender_name=info.sender_name,
                    status=RESERVED, raw_link=url[:255],
                )
            )
        # ここで IntegrityError が出たら、すでに使われたリンク

        # ③ 受取前の残高
        before = (await handle.client.get_wallet()).all_balance
        async with session_scope() as s:
            row = await s.get(KyashReceipt, receipt_id)
            row.wallet_before = before
            row.status = RECEIVING   # ★落ちても復旧できるよう、実行前に確定させる

        # ④ 受け取り（ここでお金が動く）
        await handle.client.link_receive(info.uuid)

        # ⑤ 残高の検証
        after = (await handle.client.get_wallet()).all_balance
        async with session_scope() as s:
            row = await s.get(KyashReceipt, receipt_id)
            row.wallet_after = after
            row.status = RECEIVED

        delta = after - before
        if delta != info.amount:
            async with session_scope() as s:
                row = await s.get(KyashReceipt, receipt_id)
                row.status = MANUAL_REVIEW
                row.error = f"残高差分 {delta} が金額 {info.amount} と一致しません"
            log.error(
                "チャージの残高差分が一致しません: link=%s 想定=%d 実際=%d",
                info.uuid, info.amount, delta,
            )
            raise ChargeError(
                "受け取りの確認中です。管理者が対応しますので少々お待ちください"
            )

        # ⑥ 記帳
        async with user_scope(discord_id) as s:
            await L.charge(
                s, discord_id, info.amount,
                receipt_id=receipt_id,
                memo=f"Kyash {info.sender_name}".strip(),
            )
            balance = await L.user_balance(s, discord_id)

        async with session_scope() as s:
            row = await s.get(KyashReceipt, receipt_id)
            row.status = CREDITED

        await kyash_accounts.record_received(handle.account_id, info.amount, after)
        log.info("チャージ完了: %s に ¥%d（残高 ¥%d）", discord_id, info.amount, balance)
        return ChargeResult(
            amount=info.amount, balance=balance,
            sender_name=info.sender_name, receipt_id=receipt_id,
        )

    except IntegrityError:
        raise ChargeError("このリンクはすでに使用されています") from None
    except ChargeError:
        raise
    except KyashError as e:
        failed = handle or checker
        if failed:
            await kyash_accounts.report_failure(failed.account_id, str(e))
        async with session_scope() as s:
            row = await s.get(KyashReceipt, receipt_id)
            if row and row.status in (RESERVED,):
                row.status = FAILED
                row.error = str(e)[:500]
        log.warning("チャージに失敗しました: %s", e)
        raise ChargeError(f"チャージに失敗しました: {e}") from e
    finally:
        if checker:
            await checker.aclose()
        if handle:
            await handle.aclose()


async def recover_pending() -> int:
    """
    起動時の復旧。

    RECEIVING のまま残っているものは、受け取りが成功したのに記帳前に
    落ちた可能性がある。残高を照合して判断する。
    """
    from sqlalchemy import select

    async with session_scope() as s:
        rows = (
            await s.execute(
                select(KyashReceipt).where(KyashReceipt.status.in_([RECEIVING, RECEIVED]))
            )
        ).scalars().all()
        pending = [(r.id, r.discord_id, r.amount, r.kyash_account_id, r.wallet_before) for r in rows]

    if not pending:
        return 0

    log.warning("未完了のチャージが %d 件あります。復旧します", len(pending))
    recovered = 0
    for receipt_id, discord_id, amount, account_id, before in pending:
        # 安全側に倒す：自動では記帳せず、管理者の確認へ回す
        async with session_scope() as s:
            row = await s.get(KyashReceipt, receipt_id)
            row.status = MANUAL_REVIEW
            row.error = "受け取り中にBOTが停止しました。Kyashの履歴と照合してください"
        recovered += 1
    return recovered
