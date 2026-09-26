"""Background jobs.

Per-worker loops: content freshness, stat buffer flush, WebSocket presence and
biome ticks. Cluster-wide jobs (expiry, rankings push, Discord outbox, season
transitions, retention) run only on the leader worker, elected with a
PostgreSQL advisory lock held on a dedicated connection. In SQLite mode there is
exactly one worker, so it is the leader from the start and no lock is needed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, text

from ..config import get_settings
from ..content.registry import get_registry
from ..core.pubsub import bus
from ..core.timeutil import utcnow
from ..db import is_sqlite, session_scope
from ..models import IdempotencyKey, Notification, Session as DBSession, User, WorldEvent
from ..rng.engine import table_cache
from ..services import admin_content, discord_notify, effects as effects_svc, market as market_svc, rankings as rankings_svc
from ..services import seasons as seasons_svc, stats as stats_svc, trades as trades_svc
from ..services.constants import TIER_BY_KEY
from ..ws import routes as ws_routes

log = logging.getLogger("cosmic.scheduler")
LEADER_LOCK_ID = 7_310_442_991


class Scheduler:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []
        self.is_leader = False
        self._leader_conn: Any | None = None
        self._last_rank_sig = ""
        self._stop = asyncio.Event()

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        self.tasks.append(asyncio.create_task(coro, name=name))

    async def start(self) -> None:
        self._spawn(self._loop("content_refresh", 2, self._content_refresh), "content_refresh")
        self._spawn(self._loop("stats_flush", 3, self._flush_stats), "stats_flush")
        self._spawn(ws_routes.presence_loop(), "presence")
        self._spawn(ws_routes.biome_ticker(), "biome_ticker")
        self._spawn(self._leader_election(), "leader")
        self._spawn(self._loop("online_push", 10, self._leader_only(self._push_online)), "online_push")
        self._spawn(self._loop("rankings_push", 15, self._leader_only(self._push_rankings)), "rankings_push")
        self._spawn(self._loop("admin_stats", 5, self._leader_only(self._push_admin_stats)), "admin_stats")
        self._spawn(self._loop("discord_outbox", 5, self._leader_only(self._discord)), "discord")
        self._spawn(self._loop("expiry", 30, self._leader_only(self._expiry)), "expiry")
        self._spawn(self._loop("aggregates", 300, self._leader_only(self._aggregates)), "aggregates")
        self._spawn(self._loop("retention", 3600, self._leader_only(self._retention)), "retention")
        self._spawn(self._loop("community_goals", 60, self._leader_only(self._community_goals)), "community_goals")
        self._spawn(self._loop("auto_backup", 1800, self._leader_only(self._auto_backup)), "auto_backup")

    async def stop(self) -> None:
        self._stop.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        async with session_scope() as db:
            try:
                await stats_svc.item_stats.flush(db)
            except Exception:
                log.exception("final stats flush failed")
        if self._leader_conn is not None and not self._leader_conn.is_closed():
            await self._leader_conn.close()

    async def _loop(self, name: str, interval: float, fn: Callable[[], Awaitable[None]]) -> None:
        await asyncio.sleep(min(interval, 3))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job %s failed", name)
            await asyncio.sleep(max(0.2, interval - (time.monotonic() - started)))

    def _leader_only(self, fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        async def wrapper() -> None:
            if self.is_leader:
                await fn()
        return wrapper

    async def _leader_election(self) -> None:
        if is_sqlite():
            self.is_leader = True
            log.info("single-process mode: this worker is the scheduler leader")
            return
        import asyncpg

        while not self._stop.is_set():
            try:
                if self._leader_conn is None or self._leader_conn.is_closed():
                    self._leader_conn = await asyncpg.connect(get_settings().sync_database_url)
                    self.is_leader = False
                if not self.is_leader:
                    got = await self._leader_conn.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_LOCK_ID)
                    if got:
                        self.is_leader = True
                        log.info("this worker is now the scheduler leader")
                else:
                    await self._leader_conn.execute("SELECT 1")
            except Exception:
                log.exception("leader election error")
                self.is_leader = False
                try:
                    if self._leader_conn is not None:
                        await self._leader_conn.close()
                except Exception:
                    pass
                self._leader_conn = None
            await asyncio.sleep(10)

    # --- per worker --------------------------------------------------------
    async def _content_refresh(self) -> None:
        reg = get_registry()
        before = reg.snap.version if reg.loaded else None
        async with session_scope() as db:
            snap = await reg.ensure_fresh(db, force=True)
        if before is not None and snap.version != before:
            table_cache.clear()
            rankings_svc.clear_cache()

    async def _flush_stats(self) -> None:
        async with session_scope() as db:
            await stats_svc.item_stats.flush(db)

    # --- leader --------------------------------------------------------------
    async def _push_online(self) -> None:
        async with session_scope() as db:
            cutoff = utcnow() - timedelta(seconds=90)
            n = (await db.execute(select(func.count()).select_from(User).where(User.last_seen_at > cutoff))).scalar_one()
        await bus.publish("all", "online", {"count": int(n)})

    async def _push_rankings(self) -> None:
        async with session_scope() as db:
            boards = {k: await rankings_svc.board(db, k, 10) for k in rankings_svc.BOARDS}
        sig = rankings_svc.signature()
        if sig != self._last_rank_sig:
            self._last_rank_sig = sig
            await bus.publish("all", "ranking", {"boards": {k: v["entries"] for k, v in boards.items()}})

    async def _push_admin_stats(self) -> None:
        from ..services import admin_ops

        async with session_scope() as db:
            data = await admin_ops.dashboard(db)
        data.pop("notifications", None)
        data.pop("recent_errors", None)
        await bus.publish("admins", "admin_stats", data)

    async def _discord(self) -> None:
        async with session_scope() as db:
            await discord_notify.process_outbox(db)

    async def _expiry(self) -> None:
        async with session_scope() as db:
            n = await admin_content.expire_overrides(db)
            if n:
                log.info("expired %s content overrides", n)
        async with session_scope() as db:
            await market_svc.expire_listings(db)
        async with session_scope() as db:
            await trades_svc.expire_trades(db)
        async with session_scope() as db:
            await effects_svc.cleanup_expired(db)
        async with session_scope() as db:
            await seasons_svc.update_statuses(db)

    async def _aggregates(self) -> None:
        async with session_scope() as db:
            await stats_svc.recompute_owner_counts(db)
        async with session_scope() as db:
            await stats_svc.recompute_market_values(db)
        async with session_scope() as db:
            await stats_svc.recompute_net_worth(db)
        rankings_svc.clear_cache()

    async def _community_goals(self) -> None:
        from ..services import engagement as engagement_svc

        async with session_scope() as db:
            await engagement_svc.check_community_goals(db)

    async def _auto_backup(self) -> None:
        """One scheduled backup per local day, pruned by backup.create itself."""
        reg = get_registry()
        enabled = reg.setting("backup.auto_enabled")
        if enabled is not None and not bool(enabled):
            return
        from ..models import Backup
        from ..services import backup as backup_svc
        from ..services.engagement import local_day

        async with session_scope() as db:
            last = (await db.execute(select(Backup).where(Backup.kind == "scheduled")
                                     .order_by(Backup.id.desc()).limit(1))).scalars().first()
            if last is not None and last.created_at is not None and local_day(last.created_at) == local_day():
                return
        async with session_scope() as db:
            res = await backup_svc.create(db, kind="scheduled", note="自動バックアップ")
            log.info("scheduled backup: %s (%s)", res.get("filename"), res.get("status"))

    async def _retention(self) -> None:
        reg = get_registry()
        days = int(reg.setting("retention.roll_log_days") or 21)
        keep_tier = TIER_BY_KEY.get(str(reg.setting("retention.roll_log_keep_tier") or "epic"), 3)
        feed_days = int(reg.setting("retention.feed_days") or 60)
        now = utcnow()
        async with session_scope() as db:
            # delete in bounded batches to keep locks short
            for _ in range(50):
                res = await db.execute(text(
                    "DELETE FROM rolls WHERE id IN (SELECT id FROM rolls WHERE created_at < :cut AND tier < :tier AND flags & 16 = 0 LIMIT 20000)"),
                    {"cut": now - timedelta(days=days), "tier": keep_tier})
                await db.commit()
                if (res.rowcount or 0) < 20000:
                    break
            await db.execute(delete(WorldEvent).where(WorldEvent.created_at < now - timedelta(days=feed_days),
                                                      WorldEvent.type.not_in(["first_discovery", "world_first_achievement"])))
            await db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < now - timedelta(days=2)))
            await db.execute(delete(DBSession).where(DBSession.expires_at < now))
            await db.execute(delete(Notification).where(Notification.read.is_(True), Notification.created_at < now - timedelta(days=60)))
            await db.commit()


scheduler = Scheduler()
