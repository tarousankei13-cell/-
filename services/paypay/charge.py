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
from services.paypay.client import (
    LinkAlreadyUsed, LinkOnHold, PayPayError, normalize_link,
)

log = logging.getLogger("bot.paypay.charge")

RESERVED = "RESERVED"
RECEIVING = "RECEIVING"
RECEIVED = "RECEIVED"
CREDITED = "CREDITED"
FAILED = "FAILED"
MANUAL_REVIEW = "MANUAL_REVIEW"
# ⚠️ まだ受け取れないが、終わってもいない。PayPay が送金を保留している
#    場合など。送った側が「送る」を押すと受け取れるようになるので、
#    こちらは諦めずに見に行く。
#    https://paypay.ne.jp/notice/20240228/f-p2p-money-link/
HELD = "HELD"

# 保留のリンクを見に行く間隔（分）。だんだん間を空ける。
HOLD_RETRY_MINUTES = (2, 5, 10, 30, 60, 120, 240)
# これを超えたら諦める（リンク自体に期限がある）
HOLD_GIVE_UP_HOURS = 24


class ChargeOnHold(Exception):
    """まだ受け取れないが、見張りに入れた。

    ⚠️ ChargeError と分ける。ChargeError は「駄目でした」だが、
       これは「まだです、こちらで見ています」。利用者への伝え方が
       まったく違う。
    """

    def __init__(self, amount: int, sender_name: str = "",
                 link_status: str = "", receipt_id: str = "") -> None:
        super().__init__("受け取り待ちです")
        self.amount = amount
        self.sender_name = sender_name
        self.link_status = link_status
        # ⚠️ 知らせを二重に送らないための印に使う。利用者は焦って
        #    同じリンクを何度も貼るので、その都度DMを送ってはいけない。
        self.receipt_id = receipt_id


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
            # ⚠️ **終わったものと保留を分ける。** 以前はどちらも
            #    「もう使えません」と伝えていたため、保留中の人は
            #    お金が送り主の手元にあるのに諦めさせられていた。
            if info.terminal:
                raise ChargeError(
                    "このリンクはすでに受け取り・辞退・取り消しのいずれかが済んでいます。"
                )
            held_id = await _remember_hold(
                receipt_id, discord_id, code, info, handle_id=None,
            )
            raise ChargeOnHold(
                info.amount, info.sender_name, info.status, held_id,
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
        try:
            await handle.client.link_receive(code, info=info, passcode=passcode)
        except LinkOnHold as e:
            # ⚠️ ①で見たときは受け取れたのに、ここで保留になった。
            #    時間差で起きうる。お金はまだ動いていないので、
            #    予約を見張りに切り替えて、あとで取りに来る。
            async with session_scope() as s:
                row = await s.get(PayPayReceipt, receipt_id)
                if row:
                    row.status = HELD
                    row.link_status = (e.status or "")[:32]
                    row.next_check_at = None   # 次の巡回で見る
            log.info("受け取りの直前に保留になりました: %s", code[:16])
            raise ChargeOnHold(
                info.amount, info.sender_name, e.status, receipt_id,
            ) from e

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


# ============================================================
#  保留されたリンクの見張り
# ============================================================
#
# PayPay は 2024-02-28 から、受け取りリンクを**保留**にすることがある。
# 送った側に警告を出し、「送る」か「キャンセル」を選ばせる仕組み。
#   https://paypay.ne.jp/notice/20240228/f-p2p-money-link/
#
# 保留中のリンクは**まだ生きている**。送った側が「送る」を押せば
# 受け取れるようになる。だから諦めずに、ときどき見に行く。
#
# ⚠️ 利用者に「もう一度貼ってください」と言わせない。保留が解けるのは
#    送った側の操作次第で、いつになるか分からない。こちらで見張る。

async def _remember_hold(
    receipt_id: str, discord_id: int, code: str, info, *, handle_id=None,
) -> str:
    """保留のリンクを覚えて、見張りに入れる。実際に使った行のIDを返す。

    ⚠️ すでに覚えているリンクなら、二重に作らない。利用者が焦って
       何度も貼ることがある。
    """
    from datetime import timedelta

    from sqlalchemy import select

    from db.models import utcnow

    key = info.order_id or code
    async with session_scope() as s:
        row = (await s.execute(
            select(PayPayReceipt).where(PayPayReceipt.link_uuid == key)
        )).scalars().first()
        now = utcnow()
        if row is not None:
            # ⚠️ すでに受け取り済みのものを保留に戻さない。
            if row.status in (RECEIVED, CREDITED):
                return row.id
            row.status = HELD
            row.link_status = (info.status or "")[:32]
            row.next_check_at = now + timedelta(minutes=HOLD_RETRY_MINUTES[0])
            return row.id
        s.add(PayPayReceipt(
            id=receipt_id, link_uuid=key,
            paypay_account_id=handle_id, discord_id=discord_id,
            amount=info.amount, sender_name=info.sender_name,
            status=HELD, raw_link=code[:255],
            link_status=(info.status or "")[:32],
            next_check_at=now + timedelta(minutes=HOLD_RETRY_MINUTES[0]),
        ))
    log.info(
        "保留のリンクを見張りに入れました: %s ¥%s 状態=%s",
        key[:16], info.amount, info.status,
    )
    return receipt_id


async def held_links() -> list[dict]:
    """いま見張っている保留のリンク。"""
    from sqlalchemy import select

    async with session_scope() as s:
        rows = (await s.execute(
            select(PayPayReceipt)
            .where(PayPayReceipt.status == HELD)
            .order_by(PayPayReceipt.created_at)
        )).scalars().all()
        return [
            {
                "id": r.id, "discord_id": r.discord_id, "amount": r.amount,
                "sender_name": r.sender_name or "", "link": r.raw_link or "",
                "link_status": r.link_status or "", "checks": r.checks or 0,
                "created_at": r.created_at, "next_check_at": r.next_check_at,
                "notified": r.notified,
            }
            for r in rows
        ]


async def mark_notified(receipt_id: str) -> bool:
    """保留を知らせた印。すでに付いていれば False。"""
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, receipt_id)
        if row is None or row.notified:
            return False
        row.notified = True
        return True


async def recheck_held() -> list[tuple[str, int, str, int]]:
    """保留のリンクを見に行き、受け取れるようになっていれば受け取る。

    返すのは (結果, discord_id, 受取ID, 金額) の一覧。
    結果は "credited" / "gone" / "still"。

    ⚠️ **1件ずつ順に行う。** まとめて並列に通信すると、同じ口座へ
       同時に受け取りを投げることになり、残高の突き合わせ（④〜⑤）が
       崩れる。
    """
    from datetime import timedelta

    from sqlalchemy import select

    from db.models import utcnow
    from services.paypay.client import LinkAlreadyUsed as _Used

    out: list[tuple[str, int, str, int]] = []
    now = utcnow()
    async with session_scope() as s:
        rows = (await s.execute(
            select(PayPayReceipt).where(PayPayReceipt.status == HELD)
        )).scalars().all()
        due = [
            (r.id, r.discord_id, r.raw_link or "", r.checks or 0, r.created_at)
            for r in rows
            if r.next_check_at is None or r.next_check_at <= now
        ]

    for receipt_id, discord_id, code, checks, created in due:
        # ⚠️ 期限を過ぎたものは諦める。リンク自体に期限があるので、
        #    いつまでも見に行っても無駄に通信するだけ。
        age_h = (now - (created.replace(tzinfo=now.tzinfo)
                        if created.tzinfo is None else created)).total_seconds() / 3600
        if age_h > HOLD_GIVE_UP_HOURS:
            async with session_scope() as s:
                row = await s.get(PayPayReceipt, receipt_id)
                if row and row.status == HELD:
                    row.status = FAILED
                    row.error = f"{HOLD_GIVE_UP_HOURS}時間たっても受け取れませんでした"
            out.append(("gone", discord_id, receipt_id, 0))
            continue

        try:
            result = await _try_receive_held(discord_id, code, receipt_id)
        except _Used:
            # 送った側が取り消した、または別の口座で受け取られた
            async with session_scope() as s:
                row = await s.get(PayPayReceipt, receipt_id)
                if row and row.status == HELD:
                    row.status = FAILED
                    row.error = "送った側が取り消したか、すでに受け取られました"
            out.append(("gone", discord_id, receipt_id, 0))
            continue
        except Exception as e:
            log.info("保留リンクの再確認に失敗しました: %s", e)
            result = None

        if result is not None:
            out.append(("credited", discord_id, receipt_id, result.credited))
            continue

        # まだ保留。次に見る時刻を延ばす
        wait = HOLD_RETRY_MINUTES[min(checks, len(HOLD_RETRY_MINUTES) - 1)]
        async with session_scope() as s:
            row = await s.get(PayPayReceipt, receipt_id)
            if row and row.status == HELD:
                row.checks = checks + 1
                row.next_check_at = now + timedelta(minutes=wait)
        out.append(("still", discord_id, receipt_id, 0))
    return out


async def _try_receive_held(
    discord_id: int, code: str, receipt_id: str
) -> ChargeResult | None:
    """保留が解けていれば受け取る。まだなら None。

    ⚠️ 受け取れる状態になっていたら、**通常のチャージ処理をそのまま
       使う**。ここで受け取りと記帳を書き直すと、残高の突き合わせや
       二重受け取りの防ぎ方が本流とずれていく。
    """
    # 見張り用の行を外してから本流に渡す。本流は自分で行を作るため、
    # 残したままだと link_uuid の一意制約にぶつかる。
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, receipt_id)
        if row is None or row.status != HELD:
            return None

    checker = None
    try:
        checker = await paypay_accounts.pick_account(
            int(settings.get("charge_min", 100))
        )
        info = await checker.client.link_check(code)
    finally:
        if checker:
            await checker.aclose()

    if not info.receivable:
        # まだ保留。見えた状態だけ更新しておく（値を知るため）
        async with session_scope() as s:
            row = await s.get(PayPayReceipt, receipt_id)
            if row:
                row.link_status = (info.status or "")[:32]
        if info.terminal:
            raise LinkAlreadyUsed("すでに受け取り・辞退・取り消し済みです")
        return None

    # 受け取れる。見張りの行を消してから本流へ。
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, receipt_id)
        if row and row.status == HELD:
            await s.delete(row)

    log.info("保留が解けました。受け取ります: %s ¥%s", code[:16], info.amount)
    return await charge_from_link(discord_id, code)
