"""Cross-worker event bus.

Events produced by any worker (rare drops, biome changes, trade updates, admin
broadcasts ...) must reach WebSocket clients connected to *any* worker. With
``EVENT_BUS=postgres`` events travel through PostgreSQL LISTEN/NOTIFY (no extra
infrastructure); ``local`` dispatches in-process (single worker / SQLite / tests)
and is selected automatically when there is no PostgreSQL to listen on.

Envelope: {"scope": "all"|"user"|"admins", "user_id": int|None, "type": str, "data": {...}}
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import get_settings
from ..db import is_sqlite

log = logging.getLogger("cosmic.pubsub")

CHANNEL = "cosmic_events"
Handler = Callable[[dict[str, Any]], Awaitable[None]]
MAX_PAYLOAD = 7800


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []
        self._conn: Any | None = None
        self._mode = "local"
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        s = get_settings()
        # A single SQLite process has nothing to broadcast to but itself.
        self._mode = "local" if is_sqlite() else s.event_bus
        if self._mode == "postgres":
            await self._connect()
            self._task = asyncio.create_task(self._watchdog())

    async def _connect(self) -> None:
        import asyncpg

        dsn = get_settings().sync_database_url
        self._conn = await asyncpg.connect(dsn)
        await self._conn.add_listener(CHANNEL, self._on_notify)
        log.info("event bus listening on %s", CHANNEL)

    async def _watchdog(self) -> None:
        while not self._closing:
            await asyncio.sleep(5)
            try:
                if self._conn is None or self._conn.is_closed():
                    await self._connect()
                else:
                    await self._conn.execute("SELECT 1")
            except Exception:
                log.exception("event bus connection lost; reconnecting")
                try:
                    if self._conn is not None:
                        await self._conn.close()
                except Exception:
                    pass
                self._conn = None

    async def stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
        if self._conn is not None and not self._conn.is_closed():
            await self._conn.close()

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        try:
            env = json.loads(payload)
        except ValueError:
            return
        asyncio.get_event_loop().create_task(self._dispatch(env))

    async def _dispatch(self, env: dict[str, Any]) -> None:
        for h in list(self._handlers):
            try:
                await h(env)
            except Exception:
                log.exception("event handler failed")

    async def publish(self, scope: str, type_: str, data: dict[str, Any], user_id: int | None = None) -> None:
        env = {"scope": scope, "user_id": user_id, "type": type_, "data": data}
        if self._mode != "postgres" or self._conn is None:
            await self._dispatch(env)
            return
        payload = json.dumps(env, default=str, separators=(",", ":"))
        if len(payload.encode()) > MAX_PAYLOAD:
            # Too large for NOTIFY: send a lightweight hint so clients refetch.
            payload = json.dumps({"scope": scope, "user_id": user_id, "type": "refresh", "data": {"reason": type_}})
        try:
            await self._conn.execute("SELECT pg_notify($1, $2)", CHANNEL, payload)
        except Exception:
            log.exception("pg_notify failed; dispatching locally")
            await self._dispatch(env)


bus = EventBus()


# ---------------------------------------------------------------------------
# Post-commit event queue attached to a DB session
# ---------------------------------------------------------------------------
def queue_event(db: Any, scope: str, type_: str, data: dict[str, Any], user_id: int | None = None) -> None:
    db.info.setdefault("post_commit_events", []).append((scope, type_, data, user_id))


async def publish_queued(db: Any) -> None:
    events = db.info.pop("post_commit_events", [])
    for scope, type_, data, user_id in events:
        try:
            await bus.publish(scope, type_, data, user_id)
        except Exception:
            log.exception("publish failed")


async def commit_and_publish(db: Any) -> None:
    await db.commit()
    await publish_queued(db)
