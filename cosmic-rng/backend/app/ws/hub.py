"""Per-worker WebSocket connection hub.

Receives envelopes from the cross-worker event bus and fans them out to the
connections held by this worker. Each connection has a bounded send queue and
a dedicated writer task so a slow client can never stall broadcasting.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger("cosmic.ws")

QUEUE_MAX = 256


@dataclass(eq=False)
class Connection:
    ws: WebSocket
    user_id: int | None
    session_id: str | None
    is_admin: bool = False
    admin_mode: bool = False
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAX))
    writer: asyncio.Task[None] | None = None
    connected_at: float = field(default_factory=time.monotonic)
    dropped: int = 0

    def send(self, type_: str, data: Any) -> None:
        msg = json.dumps({"t": type_, "d": data}, default=str, separators=(",", ":"))
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped > 50:
                # Persistently slow client: disconnect; it will reconnect and resync.
                asyncio.get_event_loop().create_task(self.ws.close(code=4008))

    async def run_writer(self) -> None:
        try:
            while True:
                msg = await self.queue.get()
                await self.ws.send_text(msg)
        except Exception:
            pass


class Hub:
    def __init__(self) -> None:
        self.conns: set[Connection] = set()
        self.by_user: dict[int, set[Connection]] = {}
        self.biome_due: dict[int, float] = {}

    def register(self, c: Connection) -> None:
        self.conns.add(c)
        if c.user_id is not None:
            self.by_user.setdefault(c.user_id, set()).add(c)
        c.writer = asyncio.create_task(c.run_writer())

    def unregister(self, c: Connection) -> None:
        self.conns.discard(c)
        if c.user_id is not None:
            s = self.by_user.get(c.user_id)
            if s is not None:
                s.discard(c)
                if not s:
                    self.by_user.pop(c.user_id, None)
                    self.biome_due.pop(c.user_id, None)
        if c.writer is not None:
            c.writer.cancel()

    def user_connections(self, user_id: int) -> int:
        return len(self.by_user.get(user_id, ()))

    @property
    def online_users(self) -> list[int]:
        return list(self.by_user.keys())

    async def dispatch(self, env: dict[str, Any]) -> None:
        scope = env.get("scope")
        t = env.get("type", "event")
        data = env.get("data", {})
        if scope == "all":
            for c in list(self.conns):
                c.send(t, data)
        elif scope == "user":
            uid = env.get("user_id")
            for c in list(self.by_user.get(int(uid), ())) if uid is not None else []:
                c.send(t, data)
            if t == "biome" and uid is not None:
                self.biome_due[int(uid)] = 0.0  # re-read schedule soon
            if t == "admin_mode" and uid is not None:
                for c in list(self.by_user.get(int(uid), ())):
                    if c.session_id and c.session_id[:16] == data.get("session"):
                        c.admin_mode = bool(data.get("admin_mode"))
            if t == "force_logout" and uid is not None:
                for c in list(self.by_user.get(int(uid), ())):
                    asyncio.get_event_loop().create_task(c.ws.close(code=4001))
        elif scope == "admins":
            for c in list(self.conns):
                if c.is_admin and c.admin_mode:
                    c.send(t, data)


hub = Hub()
