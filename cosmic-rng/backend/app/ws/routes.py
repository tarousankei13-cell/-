"""WebSocket endpoint: authenticated via the session cookie, origin-checked, rate limited."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..config import get_settings
from ..content.registry import get_registry
from ..core.errors import RateLimited
from ..core.ratelimit import limiter
from ..core.security import SESSION_COOKIE, load_principal
from ..core.timeutil import utcnow
from ..db import session_scope
from ..models import User
from ..services import feed as feed_svc
from .hub import Connection, hub

log = logging.getLogger("cosmic.ws")
router = APIRouter()


def _ip(ws: WebSocket) -> str:
    peer = ws.client.host if ws.client else "unknown"
    if peer in get_settings().trusted_proxy_set:
        fwd = ws.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[-1].strip()[:64]
    return peer


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    origin = ws.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in get_settings().origins:
        await ws.close(code=4403)
        return
    ip = _ip(ws)
    try:
        limiter.check("ws_connect", ip)
    except RateLimited:
        await ws.close(code=4429)
        return
    principal = None
    async with session_scope() as db:
        principal = await load_principal(db, ws.cookies.get(SESSION_COOKIE), ip)
        if principal is not None:
            max_conn = int(get_registry().setting("security.max_ws_per_user") or 5)
            if hub.user_connections(principal.user.id) >= max_conn:
                await ws.close(code=4409)
                return
            principal.user.last_seen_at = utcnow()
        await db.commit()
        feed = await feed_svc.recent(db, 30)
    await ws.accept()
    conn = Connection(ws=ws, user_id=principal.user.id if principal else None,
                      session_id=principal.session.id if principal else None,
                      is_admin=bool(principal and principal.is_admin), admin_mode=bool(principal and principal.admin_mode))
    hub.register(conn)
    if principal is not None:
        hub.biome_due[principal.user.id] = 0.0
    conn.send("hello", {"authenticated": principal is not None, "user_id": conn.user_id, "feed": feed,
                        "server_time": utcnow().isoformat(), "online": await _online_count()})
    last_touch = time.monotonic()
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > 4096:
                continue
            try:
                limiter.check("ws_msg", f"{conn.user_id or ip}")
            except RateLimited:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("t") if isinstance(msg, dict) else None
            if t == "ping":
                conn.send("pong", {"server_time": utcnow().isoformat()})
                if conn.user_id is not None and time.monotonic() - last_touch > 25:
                    last_touch = time.monotonic()
                    hub.biome_due.setdefault(conn.user_id, 0.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        hub.unregister(conn)


_online_cache = {"at": 0.0, "n": 0}


async def _online_count() -> int:
    now = time.monotonic()
    if now - _online_cache["at"] < 5:
        return _online_cache["n"]
    from datetime import timedelta

    from sqlalchemy import func

    async with session_scope() as db:
        n = (await db.execute(select(func.count()).select_from(User).where(User.last_seen_at > utcnow() - timedelta(seconds=90)))).scalar_one()
    _online_cache.update(at=now, n=int(n))
    return int(n)


async def presence_loop() -> None:
    """Refresh last_seen for users connected to this worker (feeds the online count)."""
    from sqlalchemy import update

    while True:
        await asyncio.sleep(30)
        ids = hub.online_users
        if not ids:
            continue
        try:
            async with session_scope() as db:
                await db.execute(update(User).where(User.id.in_(ids)).values(last_seen_at=utcnow()))
                await db.commit()
        except Exception:
            log.exception("presence update failed")


async def biome_ticker() -> None:
    """Advance the biome process of connected players whose next event is due and push changes."""
    from ..core.pubsub import publish_queued
    from ..models import UserBiome
    from ..services import rolls as rolls_svc
    from ..services import users as users_svc

    while True:
        await asyncio.sleep(1.0)
        now_m = time.monotonic()
        due = [uid for uid, t in list(hub.biome_due.items()) if t <= now_m]
        for uid in due[:200]:
            try:
                async with session_scope() as db:
                    row = await db.get(UserBiome, uid)
                    now = utcnow()
                    if row is not None:
                        candidates = [row.next_eval_at] + [x for x in (row.state_ends_at, row.next_state_at) if x is not None]
                        nxt = min(candidates)
                        if nxt <= now:
                            user = await users_svc.lock_user(db, uid)
                            env = await rolls_svc.load_env(db, user, now)
                            await rolls_svc._finish_progress(db, env)  # noqa: SLF001
                            await db.commit()
                            await publish_queued(db)
                            row = env.biome.row
                            candidates = [row.next_eval_at] + [x for x in (row.state_ends_at, row.next_state_at) if x is not None]
                            nxt = min(candidates)
                        wait = max(0.5, min(15.0, (nxt - utcnow()).total_seconds() + 0.05))
                    else:
                        wait = 15.0
                if uid in hub.by_user:
                    hub.biome_due[uid] = time.monotonic() + wait
            except Exception:
                log.exception("biome tick failed for %s", uid)
                if uid in hub.by_user:
                    hub.biome_due[uid] = time.monotonic() + 10
