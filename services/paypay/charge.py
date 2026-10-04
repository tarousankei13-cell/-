"""
PayPay の送金リンクからチャージする

⚠️ 順序は Kyash と同じ（docs/03 §3.1 の「受取成功 → 残高検証 → 記帳」）。
   ここを崩すと、お金が動いたのに残高に入らない／入っていないのに
   残高が増える、のどちらかが起きる。

     ① リンクを読む（まだ受け取らない。金額が分かるまで口座を選べない）
     ② 予約する（同じリンクを2回使わせない。DBの一意制約で守る）
     ③ 受け取り前の残高を控える
     ④ 受け取る ★ここでお金が動く
     ⑤ 残高の差分が金額と一致するか確かめる
     ⑥ 記帳する（チャージ率を掛けたあとの額）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from core import ledger as L
from core import limits, settings
from db.models import PayPayReceipt
from db.session import session_scope, user_scope
from services.paypay import accounts as paypay_accounts
from services.paypay.client import LinkAlreadyUsed, PayPayError, normalize_link

log = logging.getLogger("bot.paypay.charge")

RESERVED = "RESERVED"
RECEIVING = "RECEIVING"
RECEIVED = "RECEIVED"
CREDITED = "CREDITED"
FAILED = "FAILED"
MANUAL_REVIEW = "MANUAL_REVIEW"


class ChargeError(Exception):
    """利用者にそのまま見せてよい、チャージの失敗理由。"""


@dataclass
class ChargeResult:
    amount: int               # 実際に受け取った額
    balance: int              # 記帳後の残高
    sender_name: str = ""
    receipt_id: str = ""
    credited: int = 0         # 残高に入れた額（チャージ率を掛けたあと）
    rate: int = 100

    @property
    def bonus(self) -> int:
        return max(0, self.credited - self.amount)


async def charge_from_link(
    discord_id: int, url: str, passcode: str | None = None
) -> ChargeResult:
    charge_min = int(settings.get("charge_min", 100))
    charge_max = int(settings.get("charge_max", 50_000))

    handle = None
    checker = None
    receipt_id = str(uuid.uuid4())
    code = normalize_link(url)
    if not code:
        raise ChargeError("送金リンクを正しく貼り付けてください")

    try:
        # ① リンクの確認（読むだけ）
        checker = await paypay_accounts.pick_account(charge_min)
        try:
            info = await checker.client.link_check(code)
        except LinkAlreadyUsed as e:
            raise ChargeError(f"このリンクは使用できません。{e}") from e

        if not info.receivable:
            raise ChargeError(
                "このリンクはすでに受け取り・辞退・取り消しのいずれかが済んでいます。"
            )
        if info.amount < charge_min:
            raise ChargeError(f"チャージは ¥{charge_min:,} 以上から受け付けています")
        if info.amount > charge_max:
            raise ChargeError(f"1回のチャージは ¥{charge_max:,} までです")
        if info.has_password and not passcode:
            raise ChargeError(
                "このリンクにはパスコードが設定されています。\n"
                "パスコードも入力してください（4桁の数字）。"
            )

        # ★金額が確定したので、その額を受けられる口座を選び直す
        await checker.aclose()
        handle, checker = await paypay_accounts.pick_account(info.amount), None

        # ② 予約（同じリンクを2回使わせない）
        try:
            async with session_scope() as s:
                s.add(PayPayReceipt(
                    id=receipt_id, link_uuid=info.order_id or code,
                    paypay_account_id=handle.account_id, discord_id=discord_id,
                    amount=info.amount, sender_name=info.sender_name,
                    status=RESERVED, raw_link=code[:255],
                ))
        except IntegrityError:
            raise ChargeError("このリンクはすでに使用されています") from None

        # ③ 受取前の残高
        before = (await handle.client.get_balance()).all_balance
        async with session_scope() as s:
            row = await s.get(PayPayReceipt, receipt_id)
            row.wallet_before = before
            row.status = RECEIVING   # ★落ちても復旧できるよう、実行前に確定させる

        # ④ 受け取り（ここでお金が動く）
        await handle.client.link_receive(code, info=info, passcode=passcode)

        # ⑤ 残高の検証
        after = (await handle.client.get_balance()).all_balance
        async with session_scope() as s:
            row = await s.get(PayPayReceipt, receipt_id)
            row.wallet_after = after
            row.status = RECEIVED

        delta = after - before
        if delta != info.amount:
            async with session_scope() as s:
                row = await s.get(PayPayReceipt, receipt_id)
                row.status = MANUAL_REVIEW
                row.error = f"残高差分 {delta} が金額 {info.amount} と一致しません"
            log.error(
                "PayPayチャージの残高差分が一致しません: link=%s 想定=%d 実際=%d",
                code, info.amount, delta,
            )
            raise ChargeError(
                "受け取りの確認中です。管理者が対応しますので少々お待ちください"
            )

        # ⑥ 記帳
        # ⚠️ 残高に入れるのはチャージ率を掛けたあとの額。
        #    受け取った額（info.amount）は現実に動いたお金なので、
        #    PayPayReceipt にはそのまま残す。
        rate = limits.charge_rate()
        credited = limits.credited_for(info.amount)
        memo = f"PayPay {info.sender_name}".strip()
        if credited != info.amount:
            memo = f"{memo}（チャージ率{rate}%: ¥{info.amount:,}→¥{credited:,}）"
        async with user_scope(discord_id) as s:
            await L.charge(
                s, discord_id, credited, receipt_id=receipt_id, memo=memo,
            )
            balance = await L.user_balance(s, discord_id)

        async with session_scope() as s:
            row = await s.get(PayPayReceipt, receipt_id)
            row.status = CREDITED

        await paypay_accounts.record_received(handle.account_id, info.amount, after)
        log.info(
            "PayPayチャージ完了: %s に ¥%d（送金 ¥%d・率%d%%・残高 ¥%d）",
            discord_id, credited, info.amount, rate, balance,
        )
        return ChargeResult(
            amount=info.amount, balance=balance,
            sender_name=info.sender_name, receipt_id=receipt_id,
            credited=credited, rate=rate,
        )

    except IntegrityError:
        raise ChargeError("このリンクはすでに使用されています") from None
    except ChargeError:
        raise
    except PayPayError as e:
        failed = handle or checker
        if failed:
            await paypay_accounts.report_failure(failed.account_id, str(e))
        async with session_scope() as s:
            row = await s.get(PayPayReceipt, receipt_id)
            if row and row.status == RESERVED:
                row.status = FAILED
                row.error = str(e)[:500]
        log.warning("PayPayチャージに失敗しました: %s", e)
        raise ChargeError(f"チャージに失敗しました: {e}") from e
    finally:
        if checker:
            await checker.aclose()
        if handle:
            await handle.aclose()
