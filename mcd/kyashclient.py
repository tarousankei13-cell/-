"""Kyash アカウントのプール管理。

マクドナルド側とはロックを分ける。チャージ処理が注文を塞がないようにするため。

受け取りの防御は3段構え。
1. 請求リンクを弾く（BOT が支払う側になってしまうため）
2. link_uuid を DB で先に押さえる（同じリンクの二重計上を防ぐ）
3. link_check の申告額ではなく、受け取り結果と突き合わせた額を採用する
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from vendor.Kyasher.main import Kyash, KyashError, KyashLoginError, NetWorkError

from .logsetup import event
from .store import JST, Store, ts

log = logging.getLogger("bot.kyash")

TOKEN_LIFETIME_DAYS = 30


class NoKyashAccount(KyashError):
    pass


class ClaimLinkRejected(KyashError):
    """請求リンクが貼られた。受け取りではなく支払いになるので拒否する。"""


@dataclass
class ReceivedCharge:
    account_id: int
    account_label: str
    link_uuid: str
    amount: int
    sender_name: str
    sender_public_id: str
    declared_amount: int


class KyashPool:
    def __init__(self, store: Store, fail_threshold: int = 3):
        self.store = store
        self.fail_threshold = fail_threshold
        self._locks: dict[int, asyncio.Lock] = {}
        self._clients: dict[int, Kyash] = {}
        self._select_lock = asyncio.Lock()

    # ------------------------------------------------------------ 内部

    def _lock_for(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def _client_for(self, row) -> Kyash:
        account_id = int(row["id"])
        client = self._clients.get(account_id)
        if client is None:
            client = Kyash(access_token=row["access_token"])
            self._clients[account_id] = client
        return client

    def _candidates(self) -> list:
        rows = self.store.list_kyash_accounts(only_enabled=True)
        return [r for r in rows if int(r["fail_count"]) < self.fail_threshold]

    def available_count(self) -> int:
        return len(self._candidates())

    @asynccontextmanager
    async def account(self, prefer: Optional[int] = None):
        async with self._select_lock:
            rows = self._candidates()
            if not rows:
                raise NoKyashAccount(
                    "使用できる Kyash アカウントがありません。"
                    "/kyash account list で状態を確認してください。"
                )
            if prefer is not None:
                rows.sort(key=lambda r: (int(r["id"]) != prefer, r["last_used_at"] or ""))
            else:
                rows.sort(key=lambda r: r["last_used_at"] or "")
            row = next((r for r in rows if not self._lock_for(int(r["id"])).locked()), rows[0])
            lock = self._lock_for(int(row["id"]))

        await lock.acquire()
        try:
            yield int(row["id"]), str(row["label"]), self._client_for(row)
        finally:
            lock.release()

    def _note_success(self, account_id: int) -> None:
        self.store.update_kyash_account(
            account_id, fail_count=0, last_used_at=ts(), last_error=None
        )

    def _note_failure(self, account_id: int, err: str) -> None:
        rows = [r for r in self.store.list_kyash_accounts() if int(r["id"]) == account_id]
        count = (int(rows[0]["fail_count"]) if rows else 0) + 1
        self.store.update_kyash_account(
            account_id, fail_count=count, last_used_at=ts(), last_error=err[:400]
        )
        if count >= self.fail_threshold:
            self.store.update_kyash_account(account_id, enabled=0)
            log.warning("Kyash アカウント #%s を連続失敗で自動停止しました", account_id)

    # ------------------------------------------------------- トークン期限

    def token_status(self) -> list[dict]:
        out = []
        for row in self.store.list_kyash_accounts():
            obtained = row["token_obtained_at"]
            days_left: Optional[int] = None
            if obtained:
                try:
                    got = datetime.fromisoformat(obtained)
                    if got.tzinfo is None:
                        got = got.replace(tzinfo=JST)
                    days_left = (
                        got + timedelta(days=TOKEN_LIFETIME_DAYS) - datetime.now(JST)
                    ).days
                except ValueError:
                    days_left = None
            out.append(
                {
                    "id": int(row["id"]),
                    "label": row["label"],
                    "enabled": bool(row["enabled"]),
                    "days_left": days_left,
                    "fail_count": int(row["fail_count"]),
                    "last_error": row["last_error"],
                }
            )
        return out

    # --------------------------------------------------------------- 公開API

    async def wallet(self, account_id: int) -> dict[str, Any]:
        async with self.account(prefer=account_id) as (aid, label, client):
            try:
                w = await asyncio.to_thread(client.get_wallet)
                self._note_success(aid)
                return {
                    "account_id": aid,
                    "label": label,
                    "all_balance": w.all_balance,
                    "money": w.money,
                    "value": w.value,
                    "point": w.point,
                }
            except Exception as exc:
                self._note_failure(aid, str(exc))
                raise

    async def total_balance(self) -> int:
        total = 0
        for row in self.store.list_kyash_accounts(only_enabled=True):
            try:
                w = await self.wallet(int(row["id"]))
                total += int(w["all_balance"])
            except Exception:
                continue
        return total

    async def inspect_link(self, url: str) -> Any:
        """受け取らずに中身だけ見る。請求リンクならここで弾く。"""
        async with self.account() as (aid, _label, client):
            try:
                info = await asyncio.to_thread(client.link_check, url)
            except Exception as exc:
                self._note_failure(aid, str(exc))
                raise
            if not info.send_to_me:
                raise ClaimLinkRejected(
                    "これは請求リンクです。チャージには送金リンクを貼ってください。"
                )
            return info

    async def receive(self, url: str = "", trace: str = "", info: Any = None) -> ReceivedCharge:
        """送金リンクを受け取る。

        ``info`` に :meth:`inspect_link` の結果を渡すと再チェックを省く。
        link_uuid は呼び出し側で先に DB へ押さえておくこと。
        """
        started = time.monotonic()
        async with self.account() as (aid, label, client):
            if info is None:
                try:
                    info = await asyncio.to_thread(client.link_check, url)
                except Exception as exc:
                    self._note_failure(aid, f"link_check: {exc}")
                    raise

            if not info.send_to_me:
                raise ClaimLinkRejected(
                    "これは請求リンクです。チャージには送金リンクを貼ってください。"
                )

            declared = int(info.amount)

            try:
                result = await asyncio.to_thread(client.link_recieve, None, info.uuid)
            except Exception as exc:
                self._note_failure(aid, f"link_recieve: {exc}")
                raise

            actual = _extract_amount(result)
            if actual is None:
                # 受け取りAPIが金額を返さなかった場合のみ、確認済みの申告額を使う
                actual = declared
            elif actual != declared:
                log.warning(
                    "受け取り額の不一致: 申告 %s / 実際 %s (link=%s)",
                    declared, actual, info.uuid,
                )

            self._note_success(aid)
            event(
                log, "kyash.received", trace=trace, account=aid,
                ms=int((time.monotonic() - started) * 1000),
                amount=actual, declared=declared,
            )
            return ReceivedCharge(
                account_id=aid,
                account_label=label,
                link_uuid=info.uuid,
                amount=int(actual),
                sender_name=info.sender_name or "",
                sender_public_id=info.public_id or "",
                declared_amount=declared,
            )

    async def create_claim_link(self, amount: int, message: str) -> str:
        """不足分の請求リンクを作る（残高不足時の案内用）。"""
        async with self.account() as (aid, _label, client):
            try:
                created = await asyncio.to_thread(client.create_link, amount, message, True)
                self._note_success(aid)
                return created.link
            except Exception as exc:
                self._note_failure(aid, str(exc))
                raise

    async def health_check(self, account_id: int) -> tuple[bool, str]:
        try:
            await self.wallet(account_id)
            return True, "OK"
        except Exception as exc:
            return False, str(exc)[:300]


def _extract_amount(payload: Any) -> Optional[int]:
    """受け取りAPIのレスポンスから金額らしき整数を掘り出す。

    Kyash 側のスキーマ変更に耐えるよう、決め打ちせず広く探す。
    """
    if not isinstance(payload, dict):
        return None
    keys = ("amount", "receivedAmount", "transactionAmount", "value")

    def walk(node: Any, depth: int = 0) -> Optional[int]:
        if depth > 6:
            return None
        if isinstance(node, dict):
            for key in keys:
                got = node.get(key)
                if isinstance(got, bool):
                    continue
                if isinstance(got, int) and got > 0:
                    return got
                if isinstance(got, str) and got.isdigit() and int(got) > 0:
                    return int(got)
                if isinstance(got, dict):
                    nested = walk(got, depth + 1)
                    if nested:
                        return nested
            for child in node.values():
                found = walk(child, depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = walk(child, depth + 1)
                if found:
                    return found
        return None

    return walk(payload)
