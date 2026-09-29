"""
利用者ごとの排他制御

残高の確保は「残高を読む → 足りるか判定 → 記帳する」という手順になる。
この間に別の処理が割り込むと、同じ残高を二重に使えてしまい、
残高がマイナスになる（＝運営が損をする）。

そこで利用者ごとにロックを取り、この一連の処理を直列化する。

  - プロセス内   : asyncio.Lock（MWSのような単一プロセス運用はこれで十分）
  - PostgreSQL   : アドバイザリロック（将来プロセスを増やしたときの保険）

さらに ContextVar で「今ロックを持っているか」を記録し、
ロック外から残高を変更しようとしたら例外で止める。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

log = logging.getLogger("bot.locks")

_locks: dict[int, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

# 現在このタスクがロックしている利用者ID
_locked_user: ContextVar[int | None] = ContextVar("locked_user", default=None)


class LockError(Exception):
    pass


async def _get_lock(discord_id: int) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get(discord_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[discord_id] = lock
        return lock


@asynccontextmanager
async def lock_user(discord_id: int, *, timeout: float = 30.0):
    """
    利用者ごとの排他ロック。

        async with lock_user(discord_id):
            ...  # 残高の読み取りと記帳をここで行う
    """
    lock = await _get_lock(discord_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise LockError(
            f"利用者 {discord_id} の処理が混み合っています（{timeout}秒待っても空きませんでした）"
        ) from e

    token = _locked_user.set(discord_id)
    try:
        yield
    finally:
        _locked_user.reset(token)
        lock.release()


def assert_locked(discord_id: int) -> None:
    """
    ロックを持っていなければ例外。

    残高を変更する処理の入口で呼ぶ。実装ミスで
    lock_user() を忘れたまま記帳するのを防ぐ。
    """
    current = _locked_user.get()
    if current != discord_id:
        raise LockError(
            f"利用者 {discord_id} のロックを取らずに残高を変更しようとしました。"
            f"lock_user({discord_id}) で囲ってください（現在のロック: {current}）"
        )


def is_locked(discord_id: int) -> bool:
    return _locked_user.get() == discord_id
