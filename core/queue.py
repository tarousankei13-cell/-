"""
注文の同時実行の制御と、待機列

■ 同時実行の上限
大人数が一斉に注文すると、マクドナルド側から見て不自然な量の
要求が短時間に集中する。同時に処理する数に上限を設け、
超えた分は順番に待ってもらう。

■ 待機列
マクドナルド側が落ちているときは、注文を受け付けても失敗するだけ。
かといって「いまは使えません」で追い返すと、利用者は何度も試す。
復帰を待ってから流すほうが、利用者にとっても相手にとってもよい。

⚠️ 待つあいだ**残高は確保したまま**にする。
   先に解放すると、順番が来たときに残高が足りなくなりうる。
   確保したまま待たせ、諦めるときに必ず解放する。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import config

log = logging.getLogger("bot.queue")


class QueueFull(Exception):
    """待機列がいっぱい。これ以上は受けられない。"""


class QueueTimeout(Exception):
    """待っても順番が来なかった。"""


@dataclass
class Slot:
    """順番待ちの1人ぶん。"""
    discord_id: int
    queued_at: float = field(default_factory=time.monotonic)

    @property
    def waited(self) -> float:
        return time.monotonic() - self.queued_at


class OrderGate:
    """
    注文の入口。

    `async with gate.enter(discord_id) as slot:` の形で使う。
    中に入れるのは同時に `limit` 人まで。
    """

    def __init__(self, limit: int | None = None, max_wait: float | None = None,
                 max_queue: int | None = None) -> None:
        self._limit = limit or config.ORDER_CONCURRENCY
        self.max_wait = max_wait if max_wait is not None else config.ORDER_MAX_WAIT_SECONDS
        self.max_queue = max_queue or config.ORDER_MAX_QUEUE
        self._sem = asyncio.Semaphore(self._limit)
        self._waiting: list[Slot] = []
        self._running: set[int] = set()
        self.total_queued = 0
        self.total_rejected = 0
        self.longest_wait = 0.0

    # -- 状態 --

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def running(self) -> int:
        return len(self._running)

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    def position(self, discord_id: int) -> int:
        """その人が何番目か（1始まり）。並んでいなければ0。"""
        for i, s in enumerate(self._waiting, 1):
            if s.discord_id == discord_id:
                return i
        return 0

    def set_limit(self, limit: int) -> None:
        """
        上限を変える。

        いま動いている処理には影響しない（増減は次から効く）。
        """
        limit = max(1, int(limit))
        if limit == self._limit:
            return
        self._limit = limit
        self._sem = asyncio.Semaphore(limit)
        log.info("同時に処理する注文の上限を %d にしました", limit)

    # -- 入退場 --

    class _Entry:
        def __init__(self, gate: "OrderGate", slot: Slot) -> None:
            self.gate = gate
            self.slot = slot

        async def __aenter__(self) -> Slot:
            return self.slot

        async def __aexit__(self, *exc) -> None:
            self.gate._leave(self.slot)

    async def acquire(self, discord_id: int) -> Slot:
        """
        順番を待って入場する。

        待機列がいっぱいなら QueueFull、
        待ちすぎたら QueueTimeout を投げる。
        """
        if self.waiting >= self.max_queue:
            self.total_rejected += 1
            raise QueueFull(
                f"ただいま混み合っています（{self.waiting}人待ち）。"
                "しばらくしてからお試しください"
            )

        slot = Slot(discord_id=discord_id)
        self._waiting.append(slot)
        self.total_queued += 1
        try:
            if self.max_wait > 0:
                await asyncio.wait_for(self._sem.acquire(), timeout=self.max_wait)
            else:
                await self._sem.acquire()
        except asyncio.TimeoutError as e:
            self._waiting.remove(slot)
            raise QueueTimeout(
                "混み合っているため、順番が回ってきませんでした。"
                "しばらくしてからお試しください"
            ) from e
        except BaseException:
            if slot in self._waiting:
                self._waiting.remove(slot)
            raise

        self._waiting.remove(slot)
        self._running.add(discord_id)
        self.longest_wait = max(self.longest_wait, slot.waited)
        if slot.waited > 1.0:
            log.info("注文が %.1f 秒待ちました（%d人待ち）", slot.waited, self.waiting)
        return slot

    def _leave(self, slot: Slot) -> None:
        self._running.discard(slot.discord_id)
        self._sem.release()

    def enter(self, discord_id: int) -> "OrderGate._EnterCtx":
        return OrderGate._EnterCtx(self, discord_id)

    class _EnterCtx:
        def __init__(self, gate: "OrderGate", discord_id: int) -> None:
            self.gate = gate
            self.discord_id = discord_id
            self.slot: Slot | None = None

        async def __aenter__(self) -> Slot:
            self.slot = await self.gate.acquire(self.discord_id)
            return self.slot

        async def __aexit__(self, *exc) -> None:
            if self.slot is not None:
                self.gate._leave(self.slot)

    def describe(self) -> str:
        return (
            f"処理中 {self.running} / {self.limit}　待ち {self.waiting}人"
        )


# BOT全体で1つ
gate = OrderGate()
