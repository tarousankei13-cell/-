"""
注文の状態機械（Saga）

StoreOrder → AuthoriseOrder → GetPaidOrder は別々のリクエストで、
その間にプロセスが落ちたり通信が切れたりしうる。何も対策しないと
「残高だけ減って商品が来ない」「課金されたのに注文番号が分からない」
といった事故が起きる。

そこで注文を状態機械として扱い、各遷移を必ずDBへ確定させてから次へ進む。
起動時には未完了の注文を洗い出して再開する。

鉄則:
  1. AuthoriseOrder は絶対にリトライしない（二重課金になる）
  2. MCD_AUTHORISED に到達した後は自動返金しない
     （実際に課金されている可能性があるため、必ず人が確認する）
  3. 状態はメモリではなくDBを信用する
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select

from core import ledger as L
from core.subsidy import Quote
from db.models import Order, OrderEvent, User, utcnow
from db.session import session_scope, user_scope
from services.mcd import accounts as mcd_accounts
from services.mcd import stores as mcd_stores
from services.mcd.client import McdError, McdNetworkError, McdOrderError
from services.mcd.protocol import DecodedOrder, OrderItem, build_store_order_body
from services.web import new_view_token

log = logging.getLogger("bot.saga")

# 状態
class PriceChanged(Exception):
    """
    見積りと、マクドナルドが言う金額が食い違った。

    決済前に気づけるので、ここで止めれば1円も動かない。
    カタログを取り直せば直るので、利用者には「もう一度」と伝える。
    """

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"見積り {expected}円 に対して実際は {actual}円 でした")


CREATED = "CREATED"
QUOTED = "QUOTED"
BALANCE_HELD = "BALANCE_HELD"
MCD_STORING = "MCD_STORING"            # 注文を登録している最中（まだ課金はされない）
MCD_STORED = "MCD_STORED"
MCD_AUTHORISING = "MCD_AUTHORISING"    # ★決済を呼んでいる最中。課金の成否が不明
MCD_AUTHORISED = "MCD_AUTHORISED"
RECEIPT_FETCHED = "RECEIPT_FETCHED"
CAPTURED = "CAPTURED"
NOTIFIED = "NOTIFIED"
COMPLETED = "COMPLETED"
COMPENSATING = "COMPENSATING"
REFUNDED = "REFUNDED"
MANUAL_REVIEW = "MANUAL_REVIEW"        # ⚠️ 人の確認が必要

TERMINAL = {COMPLETED, REFUNDED, MANUAL_REVIEW}
# この状態以降は、失敗しても自動で返金してはいけない。
# ⚠️ MCD_AUTHORISING を含めるのが要。決済リクエストの途中で通信が切れた場合、
#    課金されたかどうか分からないため、勝手に返金すると二重の損失になりうる。
PAID_OR_LATER = {
    MCD_AUTHORISING, MCD_AUTHORISED, RECEIPT_FETCHED, CAPTURED, NOTIFIED, COMPLETED,
}

ProgressCallback = Callable[[str, str], Awaitable[None]]  # (段階, 表示文言)


class SagaError(Exception):
    pass


@dataclass
class OrderResult:
    order_id: str
    state: str
    receipt_number: str = ""
    store_name: str = ""
    store_id: str = ""
    pickup_label: str = ""
    list_price: int = 0
    # 見積りと実際の金額が食い違ったとき (ご案内した額, 実際の額)
    price_changed: tuple[int, int] | None = None
    user_amount: int = 0
    subsidy_rate: float = 0.0
    balance_after: int = 0
    view_token: str = ""            # 注文番号ページのURLに使う
    total_orders: int = 0
    error: str = ""
    error_info: object = None   # services.mcd.errors.McdErrorInfo（分かれば）

    @property
    def user_message(self) -> str:
        """利用者に見せる説明。解析できていればその文言を使う。"""
        if self.price_changed is not None:
            before, after = self.price_changed
            return (
                "お値段が変わっていたため、注文を中止しました。\n\n"
                f"ご案内した金額　**¥{before:,}**\n"
                f"実際の金額　　　**¥{after:,}**\n\n"
                "最新のお値段を取り直しましたので、"
                "お手数ですがもう一度お試しください。\n"
                "**お支払いは発生していません。** 残高は元に戻っています。"
            )
        info = self.error_info
        if info is not None and getattr(info, "user_text", ""):
            return info.user_text
        return (
            "注文を完了できませんでした。\n"
            "管理者が確認しますので、しばらくお待ちください。\n"
            "残高は元に戻っています。"
        )

    @property
    def succeeded(self) -> bool:
        return self.state in (COMPLETED, NOTIFIED, CAPTURED)

    @property
    def needs_review(self) -> bool:
        return self.state == MANUAL_REVIEW


async def _record(order_id: str, from_state: str | None, to_state: str, detail: dict | None = None) -> None:
    async with session_scope() as s:
        order = await s.get(Order, order_id)
        if order:
            order.state = to_state
            order.updated_at = utcnow()
        s.add(
            OrderEvent(
                order_id=order_id, from_state=from_state, to_state=to_state,
                detail=json.dumps(detail, ensure_ascii=False) if detail else None,
            )
        )
    log.info("注文 %s: %s → %s", order_id[:8], from_state or "-", to_state)


# ============================================================
#  作成
# ============================================================

async def create_order(
    *,
    discord_id: int,
    decoded: DecodedOrder,
    quote: Quote,
    pickup_method: str,
    store_name: str,
    group: str,
    idempotency_key: str,
) -> str:
    """
    注文を作る。まだ金は動かさない。

    同じ idempotency_key ですでに注文があれば、そのIDを返す（二重注文の防止）。
    """
    async with session_scope() as s:
        existing = (
            await s.execute(
                select(Order).where(Order.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing:
            log.info("同じ操作の注文がすでにあります: %s", existing.id[:8])
            return existing.id

        order = Order(
            id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            discord_id=discord_id,
            state=QUOTED,
            hex_payload=decoded.raw_hex,
            store_id=decoded.store_id,
            store_name=store_name,
            group_name=group,
            pickup_method=pickup_method,
            items_json=json.dumps([i.to_dict() for i in decoded.items], ensure_ascii=False),
            list_price=quote.list_price,
            subsidy_rate=quote.subsidy_rate,
            user_amount=quote.user_amount,
            subsidy_amount=quote.subsidy_amount,
        )
        s.add(order)
        s.add(OrderEvent(order_id=order.id, from_state=CREATED, to_state=QUOTED))
        return order.id


def _decoded_from_order(order: Order) -> DecodedOrder:
    items = [OrderItem.from_dict(d) for d in json.loads(order.items_json or "[]")]
    return DecodedOrder(
        store_id=order.store_id or "",
        pickup_method=order.pickup_method,
        items=items,
        raw_hex=order.hex_payload,
    )


# ============================================================
#  実行
# ============================================================

async def execute(order_id: str, progress: ProgressCallback | None = None) -> OrderResult:
    """注文を最後まで実行する。"""

    async def notify(step: str, text: str) -> None:
        if progress:
            try:
                await progress(step, text)
            except Exception:
                log.debug("進捗通知に失敗しました", exc_info=True)

    async with session_scope() as s:
        order = await s.get(Order, order_id)
        if order is None:
            raise SagaError("注文が見つかりません")
        discord_id = order.discord_id
        user_amount = order.user_amount
        subsidy_amount = order.subsidy_amount
        list_price = order.list_price          # 金額の食い違いを見つけるため
        state = order.state
        store_id = order.store_id or ""

    result = OrderResult(
        order_id=order_id, state=state, store_id=store_id,
    )

    # ---- ① 残高の確保 ----
    if state == QUOTED:
        try:
            async with user_scope(discord_id) as s:
                tx = await L.hold(s, discord_id, user_amount, order_id=order_id)
                order = await s.get(Order, order_id)
                order.hold_tx_id = tx
        except L.InsufficientBalance as e:
            await _record(order_id, state, REFUNDED, {"reason": "残高不足"})
            result.state = REFUNDED
            result.error = str(e)
            return result
        await _record(order_id, state, BALANCE_HELD)
        state = BALANCE_HELD
        await notify("hold", f"残高を確保しました（¥{user_amount:,}）")

    handle = None
    try:
        # ---- ② アカウントの確保と下準備 ----
        if state in (BALANCE_HELD, MCD_STORING):
            if state == MCD_STORING:
                # 登録の最中に落ちていた。StoreOrder は課金を伴わないので
                # 作り直して問題ない（未払いの注文が店側に残ることはある）。
                log.warning("注文の登録中に中断していたため、やり直します: %s", order_id[:8])
            handle = await mcd_accounts.pick_account()
            # 認証の更新と店舗の確認は互いに関係が無いので同時に行う。
            # 店舗情報は認証がいらない配信元から取るため、待つ必要が無い。
            _auth, info = await asyncio.gather(
                handle.client.ensure_auth(),
                mcd_stores.resolve_store(handle.client, store_id),
            )
            await notify("store", f"店舗を確認しました（{info.name}）")

            async with session_scope() as s:
                order = await s.get(Order, order_id)
                order.mcd_account_id = handle.account_id
                order.group_name = info.group
                if info.name:
                    order.store_name = info.name
                decoded = _decoded_from_order(order)
                pickup = order.pickup_method or "takeOut"
                result.store_name = order.store_name or ""

            # ---- ③ 注文の登録 ----
            await notify("send", "マクドナルドへ送信しています…")
            if state != MCD_STORING:
                await _record(order_id, state, MCD_STORING)
                state = MCD_STORING
            # 短時間なら前のトークンを使い回す（1注文あたり1往復減る）
            pos_paseto = await mcd_accounts.pos_paseto(handle, info.group)
            body = build_store_order_body(
                decoded, pos_paseto=pos_paseto, card_id=handle.card_id,
                pickup_method=pickup,
            )
            stored = await handle.client.store_order(info.group, body)
            if not stored.order_token:
                raise McdOrderError("注文トークンを取得できませんでした")

            # ⚠️ マクドナルドが言う金額と、こちらの見積りを突き合わせる。
            #    カタログは最大 MENU_STALE_MINUTES 分古いことがある。
            #    値上げに気づかずに進むと、利用者からは古い安い金額を
            #    取り、カードには新しい高い金額が請求される。
            #    差額は管理者が黙って被ることになる。
            #
            #    ここはまだ決済前（④の AuthoriseOrder が課金）なので、
            #    止めれば1円も動かない。必ずここで確かめる。
            if stored.total_amount and stored.total_amount != list_price:
                log.warning(
                    "見積りと実際の金額が違います: 注文=%s 見積り=%d 実際=%d",
                    order_id, list_price, stored.total_amount,
                )
                raise PriceChanged(list_price, stored.total_amount)

            async with session_scope() as s:
                order = await s.get(Order, order_id)
                order.order_token = stored.order_token
                order.order_code = stored.order_code
            await _record(order_id, state, MCD_STORED, {"order_code": stored.order_code})
            state = MCD_STORED

        # ---- ④ 支払いの確定（ここから自動返金禁止） ----
        receipt = ""
        if state in (MCD_STORED, MCD_AUTHORISING):
            if handle is None:
                handle = await _reopen_account(order_id)
            async with session_scope() as s:
                order = await s.get(Order, order_id)
                token, group = order.order_token or "", order.group_name or "group-f"

            if state == MCD_STORED:
                # ★呼び出す前に状態を確定させる。
                #   ここで落ちても「決済を試みた」ことが記録に残る。
                await _record(order_id, state, MCD_AUTHORISING)
                state = MCD_AUTHORISING
                try:
                    auth = await handle.client.authorise_order(group, token)
                    receipt = auth.display_order_number
                except (McdError, McdNetworkError, McdOrderError) as exc:
                    # ⚠️ 課金されたかどうか分からない。リトライは絶対にしない。
                    #    読み取り専用の GetPaidOrder で実際の状態を確かめる。
                    paid = await _probe_paid(handle, group, token)
                    if paid is None:
                        log.error(
                            "決済の成否を確認できませんでした。手動確認へ回します: %s", exc
                        )
                        raise
                    log.warning("決済は成立していました（応答だけが届きませんでした）")
                    receipt = paid.display_order_number
            else:
                # 復旧時: 決済を試みた直後に落ちていた
                paid = await _probe_paid(handle, group, token)
                if paid is None:
                    raise SagaError("決済の成否を確認できませんでした")
                receipt = paid.display_order_number

            await _record(order_id, state, MCD_AUTHORISED, {"receipt_number": receipt})
            state = MCD_AUTHORISED

        # ---- ⑤ 注文番号の取得 ----
        if state == MCD_AUTHORISED:
            if handle is None:
                handle = await _reopen_account(order_id)
            async with session_scope() as s:
                order = await s.get(Order, order_id)
                token, group = order.order_token or "", order.group_name or "group-f"

            if not receipt:
                receipt = await _fetch_receipt(handle, group, token)
            async with session_scope() as s:
                order = await s.get(Order, order_id)
                order.receipt_number = receipt
                # 注文番号ページのURLに入れる合い言葉。
                # 一度作ったら変えない（渡したリンクが死ぬため）。
                if receipt and not order.view_token:
                    order.view_token = new_view_token()
            await _record(order_id, state, RECEIPT_FETCHED, {"receipt_number": receipt})
            state = RECEIPT_FETCHED
            await notify("receipt", f"注文番号を取得しました（{receipt or '取得中'}）")

        # ---- ⑥ 残高の確定 ----
        if state == RECEIPT_FETCHED:
            async with user_scope(discord_id) as s:
                await L.capture(s, discord_id, user_amount, subsidy_amount, order_id=order_id)
            await _record(order_id, state, CAPTURED)
            state = CAPTURED
            # 利用回数はここで1回だけ増やす。
            # _finalize でやると、復旧のたびに二重に数えてしまう。
            async with session_scope() as s:
                user = await s.get(User, discord_id)
                if user:
                    user.total_orders += 1
                    # 次回の手数を減らすため、今回の店舗と受取方法を覚えておく
                    order = await s.get(Order, order_id)
                    if order is not None:
                        user.last_store_id = order.store_id
                        user.last_store_name = order.store_name
                        user.last_pickup = order.pickup_method

        if handle:
            await mcd_accounts.report_success(handle.account_id)

    except PriceChanged as e:
        # まだ決済していないので、確保していた残高を戻すだけでよい
        return await _handle_failure(order_id, state, handle, e, result)
    except (McdError, McdNetworkError, McdOrderError) as e:
        return await _handle_failure(order_id, state, handle, e, result)
    except Exception as e:  # 想定外
        log.exception("注文処理で予期しないエラーが発生しました")
        return await _handle_failure(order_id, state, handle, e, result)
    finally:
        if handle:
            await handle.aclose()

    return await _finalize(order_id, result)


async def _reopen_account(order_id: str):
    async with session_scope() as s:
        order = await s.get(Order, order_id)
        account_id = order.mcd_account_id
    if not account_id:
        raise SagaError("この注文に紐づくアカウントが分かりません")
    return await mcd_accounts.open_account(account_id)


async def _probe_paid(handle, group: str, token: str, attempts: int = 3):
    """
    決済が実際に成立したかを確認する。

    GetPaidOrder は読み取り専用なので何度呼んでも安全。
    注文が返ってくれば成立、確認できなければ None を返す（→ 手動確認へ）。
    """
    for i in range(attempts):
        try:
            paid = await handle.client.get_paid_order(group, token)
            if paid.order_code or paid.display_order_number:
                return paid
        except McdError as e:
            log.info("決済状況の確認を再試行します (%d/%d): %s", i + 1, attempts, e)
        await asyncio.sleep(1.5)
    return None


async def _fetch_receipt(handle, group: str, token: str, attempts: int = 5) -> str:
    """
    注文番号を取る。読み取り専用なので何度でも呼べる。

    決済直後は番号がまだ出ていないことがあるため、少し待って繰り返す。
    """
    for i in range(attempts):
        try:
            paid = await handle.client.get_paid_order(group, token)
            if paid.display_order_number:
                return paid.display_order_number
        except McdError as e:
            log.info("注文番号の取得を再試行します (%d/%d): %s", i + 1, attempts, e)
        await asyncio.sleep(1.0)
    return ""


async def cancel_waiting(order_id: str, reason: str) -> None:
    """
    まだ何も送っていない注文を取り消す。

    順番待ちで諦めた場合に使う。マクドナルドへは一切送っていないので、
    確保した残高を戻して終わりにできる。

    ⚠️ すでに送信を始めていたら何もしない。取り消してよいのは
       BALANCE_HELD（残高を確保しただけ）の状態だけ。
    """
    async with session_scope() as s:
        order = await s.get(Order, order_id)
        if order is None:
            return
        state = order.state
        discord_id = order.discord_id
        user_amount = order.user_amount

    # 取り消してよいのは、まだ何も送っていない状態だけ。
    #   CREATED / QUOTED  … 残高もまだ確保していない
    #   BALANCE_HELD      … 残高を確保しただけ。解放が必要
    if state not in (CREATED, QUOTED, BALANCE_HELD):
        log.warning(
            "取り消せない状態です（%s）。そのまま処理を続けます: %s", state, order_id[:8]
        )
        return

    await _record(order_id, state, COMPENSATING, {"error": reason})

    if state == BALANCE_HELD:
        try:
            async with user_scope(discord_id) as s:
                await L.release(
                    s, discord_id, user_amount, order_id=order_id, memo=reason[:200]
                )
        except Exception:
            log.exception("ホールドの解放に失敗しました。手動確認が必要です")
            await _record(order_id, COMPENSATING, MANUAL_REVIEW, {"error": reason})
            return

    await _record(order_id, COMPENSATING, REFUNDED, {"error": reason})
    log.info("順番待ちで取り消しました: %s（%s）", order_id[:8], reason)


async def _handle_failure(order_id: str, state: str, handle, error: Exception, result: OrderResult) -> OrderResult:
    message = str(error)
    info = getattr(error, "info", None)
    result.error_info = info
    if isinstance(error, PriceChanged):
        result.price_changed = (error.expected, error.actual)
    log.warning("注文 %s が状態 %s で失敗しました: %s", order_id[:8], state, message)

    # ⚠️ 金額の食い違いはアカウントのせいではない。
    #    これで健全性を下げると、値上げのたびにアカウントが止まる。
    if handle and not isinstance(error, PriceChanged):
        # カードが使えない・認証が切れているなど、そのアカウントを
        # 使い続けても直らない種類のときは、すぐ候補から外す。
        # 放っておくと以降の注文が全部同じ理由で失敗し続けるため。
        fatal = bool(info is not None and getattr(info, "account_fault", False))
        await mcd_accounts.report_failure(handle.account_id, message, fatal=fatal)
        if fatal:
            log.error(
                "アカウント %s を候補から外しました（%s）",
                handle.account_id, getattr(info, "kind", "?"),
            )

    async with session_scope() as s:
        order = await s.get(Order, order_id)
        order.error = message[:1000]
        order.attempts += 1
        discord_id = order.discord_id
        user_amount = order.user_amount

    if state in PAID_OR_LATER:
        # ⚠️ 課金が成立している可能性がある。自動で返金してはいけない。
        await _record(order_id, state, MANUAL_REVIEW, {"error": message})
        result.state = MANUAL_REVIEW
        result.error = message
        return result

    # ここまでなら課金は成立していないので、ホールドを解放して返金する
    await _record(order_id, state, COMPENSATING, {"error": message})
    if state in (BALANCE_HELD, MCD_STORING, MCD_STORED):
        try:
            async with user_scope(discord_id) as s:
                await L.release(s, discord_id, user_amount, order_id=order_id, memo=message[:200])
        except Exception:
            log.exception("ホールドの解放に失敗しました。手動確認が必要です")
            await _record(order_id, COMPENSATING, MANUAL_REVIEW, {"error": "解放に失敗"})
            result.state = MANUAL_REVIEW
            result.error = message
            return result

    await _record(order_id, COMPENSATING, REFUNDED)
    result.state = REFUNDED
    result.error = message
    return result


async def _finalize(order_id: str, result: OrderResult) -> OrderResult:
    from services.mcd.protocol import PICKUP_LABEL

    async with session_scope() as s:
        order = await s.get(Order, order_id)
        result.state = order.state
        result.receipt_number = order.receipt_number or ""
        result.view_token = order.view_token or ""
        result.store_name = order.store_name or ""
        result.store_id = order.store_id or ""
        result.pickup_label = PICKUP_LABEL.get(order.pickup_method or "", "テイクアウト")
        result.list_price = order.list_price
        result.user_amount = order.user_amount
        result.subsidy_rate = float(order.subsidy_rate)
        discord_id = order.discord_id

    async with session_scope() as s:
        result.balance_after = await L.user_balance(s, discord_id)
        user = await s.get(User, discord_id)
        result.total_orders = user.total_orders if user else 0
    return result


async def mark_notified(order_id: str) -> None:
    async with session_scope() as s:
        order = await s.get(Order, order_id)
        if order and order.state == CAPTURED:
            order.state = COMPLETED
            s.add(OrderEvent(order_id=order_id, from_state=CAPTURED, to_state=COMPLETED))


# ============================================================
#  起動時の復旧
# ============================================================

async def recover_pending() -> list[OrderResult]:
    """
    未完了の注文を洗い出して再開する。

    BOTが落ちたタイミングによっては、課金済みなのに残高が確定していない
    注文が残る。起動のたびにこれを片付ける。
    """
    async with session_scope() as s:
        rows = (
            await s.execute(select(Order).where(Order.state.notin_(list(TERMINAL))))
        ).scalars().all()
        pending = [(o.id, o.state) for o in rows]

    if not pending:
        return []

    log.warning("未完了の注文が %d 件あります。復旧を試みます", len(pending))
    results = []
    for order_id, state in pending:
        try:
            log.info("復旧中: %s (状態 %s)", order_id[:8], state)
            r = await execute(order_id)
            # 復旧した注文はDMを送れないまま残るので、ここで完了にしておく。
            # そのままだと起動のたびに拾われ続けてしまう。
            if r.state == CAPTURED:
                await mark_notified(order_id)
                r.state = COMPLETED
            results.append(r)
        except Exception:
            log.exception("注文 %s の復旧に失敗しました", order_id[:8])
            await _record(order_id, state, MANUAL_REVIEW, {"error": "復旧に失敗"})
    return results


async def list_manual_review() -> list[Order]:
    async with session_scope() as s:
        return list(
            (
                await s.execute(
                    select(Order).where(Order.state == MANUAL_REVIEW)
                    .order_by(Order.created_at.desc()).limit(25)
                )
            ).scalars().all()
        )


async def resolve_review(order_id: str, *, refund: bool) -> None:
    """管理者が手動で処理を確定する。"""
    async with session_scope() as s:
        order = await s.get(Order, order_id)
        if order is None or order.state != MANUAL_REVIEW:
            raise SagaError("対象の注文が見つかりません")
        discord_id, user_amount, subsidy = order.discord_id, order.user_amount, order.subsidy_amount

    if refund:
        async with user_scope(discord_id) as s:
            held = await L.held_balance(s, discord_id)
            if held >= user_amount:
                await L.release(s, discord_id, user_amount, order_id=order_id, memo="管理者による返金")
        await _record(order_id, MANUAL_REVIEW, REFUNDED, {"by": "admin"})
    else:
        async with user_scope(discord_id) as s:
            held = await L.held_balance(s, discord_id)
            if held >= user_amount:
                await L.capture(s, discord_id, user_amount, subsidy, order_id=order_id)
        await _record(order_id, MANUAL_REVIEW, COMPLETED, {"by": "admin"})
