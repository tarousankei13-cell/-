"""マクドナルドアカウントのプール管理。

HATTIMCD をそのまま呼ぶと次の問題があるので、ここで包む。

* ``refresh_token`` は呼び出しのたびに新品へ差し替わる（ローテーション）。
  同じアカウントを並行で叩くと片方のトークンが即失効するため、
  **アカウントごとに asyncio.Lock を持ち、直列化する**。
* ライブラリ側に永続化がないので、更新のたびに DB へ書き戻す。
* ``_ensure_auth()`` が毎回フルリフレッシュするので、有効期限を見て省略する。
* 店舗の group を総当たりするので、一度成功した group を覚えて先頭に回す。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

from vendor.HATTIMCD.main import (
    MCD,
    DecodedOrder,
    MCDAuthError,
    MCDError,
    MCDNetworkError,
    MCDOrderError,
    OrderResult,
    TokenSet,
    decode_hex,
)

from .logsetup import event
from .store import Store

log = logging.getLogger("bot.mcd")

ALL_GROUPS = ["group-e", "group-f", "group-g", "group-h"]


def hex_digest(hex_str: str) -> str:
    return hashlib.sha256(hex_str.strip().lower().encode("utf-8")).hexdigest()


class NoAccountAvailable(MCDError):
    pass


class PaymentUncertain(MCDError):
    """決済が通ったか判断できない状態。自動リトライは絶対にしない。"""


def _mark(exc: Exception, safe_to_retry: bool) -> Exception:
    """同じ hex での再試行が安全かどうかを例外に添える。

    StoreOrder より手前で落ちたなら課金は起きていないので再試行してよい。
    AuthoriseOrder まで進んだあとは、二重注文になるので再試行させない。
    """
    setattr(exc, "safe_to_retry", safe_to_retry)
    return exc


class _CachedMCD(MCD):
    """有効期限を見てトークン再取得を省くサブクラス。"""

    def _ensure_auth(self) -> None:
        now = time.time()
        if not self.tokens.access_token or (self.tokens.access_token_exp - now) < 60:
            self._refresh_access_token()
        if not self.tokens.root_paseto or (self.tokens.root_paseto_exp - now) < 120:
            self._refresh_root_paseto()


@dataclass
class PaidOrder:
    account_id: int
    account_label: str
    receipt_number: str
    short_code: str
    order_code: str
    order_token: str
    group: str
    store_name: str


class McdPool:
    def __init__(self, store: Store, timeout: int = 25, fail_threshold: int = 3):
        self.store = store
        self.timeout = timeout
        self.fail_threshold = fail_threshold
        self._locks: dict[int, asyncio.Lock] = {}
        self._clients: dict[int, _CachedMCD] = {}
        self._select_lock = asyncio.Lock()
        # 同時に走っている注文の数。パネルの待ち行列表示に使う。
        self.inflight = 0
        self.waiting = 0

    # ------------------------------------------------------------ 内部

    def _lock_for(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def _client_for(self, row) -> _CachedMCD:
        account_id = int(row["id"])
        client = self._clients.get(account_id)
        if client is None:
            client = _CachedMCD(
                tokens=TokenSet(refresh_token=row["refresh_token"]), timeout=self.timeout
            )
            self._clients[account_id] = client
        return client

    def _persist_tokens(self, account_id: int, client: _CachedMCD) -> None:
        if client.tokens.refresh_token:
            self.store.update_mcd_account(
                account_id, refresh_token=client.tokens.refresh_token
            )

    def _group_hint_key(self, store_id: str) -> str:
        return f"group_hint:{store_id}"

    def _ordered_groups(self, store_id: str) -> list[str]:
        hint = self.store.get_kv(self._group_hint_key(store_id))
        if hint in ALL_GROUPS:
            return [hint] + [g for g in ALL_GROUPS if g != hint]
        return list(ALL_GROUPS)

    # ------------------------------------------------------- アカウント選択

    def _candidates(self) -> list:
        rows = self.store.list_mcd_accounts(only_enabled=True)
        return [r for r in rows if int(r["fail_count"]) < self.fail_threshold]

    def available_count(self) -> int:
        return len(self._candidates())

    @asynccontextmanager
    async def account(self, prefer: Optional[int] = None):
        """使えるアカウントを1つ確保し、そのロックを握ったまま渡す。

        選び方は least-recently-used。連続失敗が閾値に達したものは外す。
        """
        self.waiting += 1
        try:
            async with self._select_lock:
                rows = self._candidates()
                if not rows:
                    raise NoAccountAvailable(
                        "使用できるマクドナルドアカウントがありません。"
                        "/mcd account list で状態を確認してください。"
                    )
                if prefer is not None:
                    rows.sort(key=lambda r: (int(r["id"]) != prefer, r["last_used_at"] or ""))
                else:
                    rows.sort(key=lambda r: r["last_used_at"] or "")

                # 空いているアカウントを優先。全部埋まっていたら先頭を待つ。
                row = next((r for r in rows if not self._lock_for(int(r["id"])).locked()), rows[0])
                lock = self._lock_for(int(row["id"]))

            await lock.acquire()
        finally:
            self.waiting -= 1

        account_id = int(row["id"])
        self.inflight += 1
        try:
            fresh = next(
                (r for r in self.store.list_mcd_accounts() if int(r["id"]) == account_id), row
            )
            yield account_id, str(fresh["label"]), self._client_for(fresh), fresh
        finally:
            self.inflight -= 1
            lock.release()

    def _note_success(self, account_id: int) -> None:
        from .store import ts

        self.store.update_mcd_account(
            account_id, fail_count=0, last_used_at=ts(), last_error=None
        )

    def _note_failure(self, account_id: int, err: str) -> int:
        from .store import ts

        rows = [r for r in self.store.list_mcd_accounts() if int(r["id"]) == account_id]
        count = (int(rows[0]["fail_count"]) if rows else 0) + 1
        self.store.update_mcd_account(
            account_id, fail_count=count, last_used_at=ts(), last_error=err[:400]
        )
        if count >= self.fail_threshold:
            self.store.update_mcd_account(account_id, enabled=0)
            log.warning(
                "アカウント #%s を連続失敗 %s 回で自動停止しました", account_id, count
            )
        return count

    # --------------------------------------------------------------- 公開API

    @staticmethod
    def decode(hex_str: str) -> DecodedOrder:
        """オフライン解析。通信しないので課金は起きない。"""
        return decode_hex(hex_str)

    async def store_name(self, store_id: str) -> str:
        """店名の取得。認証不要なのでロックを取らない。"""
        rows = self.store.list_mcd_accounts(only_enabled=True)
        if not rows:
            return ""
        client = self._client_for(rows[0])
        try:
            return await asyncio.to_thread(client.get_store_name, store_id)
        except Exception:
            return ""

    async def cards_of(self, account_id: int) -> list[dict]:
        async with self.account(prefer=account_id) as (aid, _label, client, _row):
            if aid != account_id:
                raise NoAccountAvailable("指定のアカウントが使用できません")
            try:
                cards = await asyncio.to_thread(client.get_cards)
            finally:
                self._persist_tokens(aid, client)
            return cards

    async def health_check(self, account_id: int) -> tuple[bool, str]:
        async with self.account(prefer=account_id) as (aid, _label, client, _row):
            try:
                await asyncio.to_thread(client._ensure_auth)
                self._persist_tokens(aid, client)
                self._note_success(aid)
                return True, "OK"
            except Exception as exc:
                self._persist_tokens(aid, client)
                self._note_failure(aid, str(exc))
                return False, str(exc)[:300]

    async def pay(self, hex_str: str, decoded: DecodedOrder, trace: str) -> PaidOrder:
        """hex から決済を完了させる。

        認証の失敗までは別アカウントへ乗り換えるが、``store_order`` が
        通ったあとは絶対に乗り換えない。二重注文になるため。
        """
        last_error: Optional[Exception] = None
        tried: set[int] = set()

        for _attempt in range(max(1, self.available_count())):
            async with self.account() as (account_id, label, client, row):
                if account_id in tried:
                    continue
                tried.add(account_id)
                started = time.monotonic()

                # --- 1) 認証。ここで失敗したら他のアカウントを試してよい ---
                try:
                    await asyncio.to_thread(client._ensure_auth)
                except (MCDAuthError, MCDNetworkError) as exc:
                    self._persist_tokens(account_id, client)
                    self._note_failure(account_id, f"auth: {exc}")
                    event(
                        log, "mcd.auth.failed", level=logging.WARNING,
                        trace=trace, account=account_id, error=str(exc)[:200],
                    )
                    last_error = exc
                    continue
                self._persist_tokens(account_id, client)

                # --- 2) StoreOrder。ここから先は乗り換え禁止 ---
                client._GROUPS = self._ordered_groups(decoded.store_id)
                card_id = (row["card_id"] or "") if row is not None else ""
                try:
                    so: OrderResult = await asyncio.to_thread(
                        client.store_order, decoded, card_id
                    )
                except Exception as exc:
                    self._persist_tokens(account_id, client)
                    self._note_failure(account_id, f"store_order: {exc}")
                    event(
                        log, "mcd.store_order.failed", level=logging.WARNING,
                        trace=trace, account=account_id, error=str(exc)[:300],
                    )
                    raise _mark(
                        MCDOrderError(f"注文の登録に失敗しました: {exc}"), True
                    ) from exc

                if not so.order_token:
                    raise _mark(
                        MCDOrderError("注文トークンが取得できませんでした"), True
                    )

                self.store.set_kv(self._group_hint_key(decoded.store_id), so.group)

                # --- 3) AuthoriseOrder。ここは失敗してもリトライしない ---
                try:
                    auth: OrderResult = await asyncio.to_thread(
                        client.authorise_order, so.order_token, so.group
                    )
                except MCDNetworkError as exc:
                    # 応答が返ってこなかった。課金されたか判断できない。
                    self._persist_tokens(account_id, client)
                    self._note_failure(account_id, f"authorise(net): {exc}")
                    event(
                        log, "mcd.authorise.uncertain", level=logging.ERROR,
                        trace=trace, account=account_id, error=str(exc)[:300],
                    )
                    raise _mark(
                        PaymentUncertain(
                            "決済の応答が確認できませんでした。課金された可能性があるため "
                            "自動リトライは行いません。オーナーに確認してください。"
                        ), False
                    ) from exc
                except Exception as exc:
                    self._persist_tokens(account_id, client)
                    self._note_failure(account_id, f"authorise: {exc}")
                    raise _mark(
                        MCDOrderError(f"決済に失敗しました: {exc}"), False
                    ) from exc

                self._persist_tokens(account_id, client)

                # --- 4) 受け取り番号 ---
                receipt = auth.receipt_number
                if not receipt:
                    try:
                        paid = await asyncio.to_thread(
                            client.get_paid_order, auth.order_token or so.order_token, so.group
                        )
                        receipt = paid.receipt_number
                    except Exception:
                        receipt = ""

                try:
                    name = await asyncio.to_thread(client.get_store_name, decoded.store_id)
                except Exception:
                    name = ""

                self._note_success(account_id)
                event(
                    log, "mcd.paid", trace=trace, account=account_id,
                    ms=int((time.monotonic() - started) * 1000),
                    receipt=receipt, group=so.group, store=decoded.store_id,
                )
                return PaidOrder(
                    account_id=account_id,
                    account_label=label,
                    receipt_number=receipt or "",
                    short_code=auth.short_code or so.short_code or "",
                    order_code=auth.order_code or "",
                    order_token=auth.order_token or so.order_token,
                    group=so.group,
                    store_name=name,
                )

        raise _mark(
            NoAccountAvailable(f"どのアカウントでも認証できませんでした: {last_error}"), True
        )

    async def buzzer(self, order_token: str, group: str, account_id: int) -> Optional[int]:
        async with self.account(prefer=account_id) as (aid, _label, client, _row):
            try:
                await asyncio.to_thread(client._ensure_auth)
                value = await asyncio.to_thread(
                    client.get_order_buzzer_notification, order_token, group
                )
                self._persist_tokens(aid, client)
                return value
            except Exception:
                self._persist_tokens(aid, client)
                return None
