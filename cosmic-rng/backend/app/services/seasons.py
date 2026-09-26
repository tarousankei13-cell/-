"""Seasons: active season lookup, per-season stats and end-of-season archival."""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import greatest, upsert as insert
from ..core.timeutil import utcnow
from ..models import Ranking, Season, SeasonStats, User

log = logging.getLogger("cosmic.seasons")

_cache: dict[str, Any] = {"at": 0.0, "id": None}


async def active_season_id(db: AsyncSession) -> int | None:
    now_m = time.monotonic()
    if now_m - _cache["at"] < 20:
        return _cache["id"]
    now = utcnow()
    sid = (await db.execute(
        select(Season.id).where(Season.status == "active", Season.starts_at <= now, Season.ends_at > now)
        .order_by(Season.starts_at.desc()).limit(1)
    )).scalar_one_or_none()
    _cache.update(at=now_m, id=sid)
    return sid


def invalidate_cache() -> None:
    _cache["at"] = 0.0


async def add_stats(db: AsyncSession, user_id: int, *, rolls: int = 0, points: int = 0, best_odds: float = 0.0,
                    first_discoveries: int = 0) -> None:
    sid = await active_season_id(db)
    if sid is None:
        return
    stmt = insert(SeasonStats).values(season_id=sid, user_id=user_id, rolls=rolls, points=points, best_odds=best_odds,
                                      first_discoveries=first_discoveries)
    stmt = stmt.on_conflict_do_update(
        index_elements=["season_id", "user_id"],
        set_={
            "rolls": SeasonStats.rolls + rolls,
            "points": SeasonStats.points + points,
            "best_odds": greatest(SeasonStats.best_odds, best_odds),
            "first_discoveries": SeasonStats.first_discoveries + first_discoveries,
        },
    )
    await db.execute(stmt)


SEASON_BOARDS = {"points": SeasonStats.points, "rolls": SeasonStats.rolls, "best": SeasonStats.best_odds,
                 "firsts": SeasonStats.first_discoveries}


async def update_statuses(db: AsyncSession) -> list[int]:
    """Activate scheduled seasons and finalise ended ones. Returns finalised season ids."""
    now = utcnow()
    finalized: list[int] = []
    for s in (await db.execute(select(Season).where(Season.status == "scheduled", Season.starts_at <= now, Season.ends_at > now))).scalars().all():
        s.status = "active"
        log.info("season %s activated", s.key)
    for s in (await db.execute(select(Season).where(Season.status.in_(["active", "scheduled"]), Season.ends_at <= now)
                               .with_for_update(skip_locked=True))).scalars().all():
        for board, col in SEASON_BOARDS.items():
            rows = (await db.execute(
                select(SeasonStats.user_id, col.label("v")).join(User, User.id == SeasonStats.user_id)
                .where(SeasonStats.season_id == s.id, User.status != "banned", col > 0).order_by(col.desc()).limit(100)
            )).all()
            for rank, r in enumerate(rows, start=1):
                db.add(Ranking(board=f"season_{board}", season_id=s.id, rank=rank, user_id=r.user_id, value=float(r.v)))
        s.status = "ended"
        s.finalized_at = now
        finalized.append(s.id)
        log.info("season %s finalized", s.key)
    await db.commit()
    if finalized:
        invalidate_cache()
    return finalized


async def list_seasons(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Season).order_by(Season.starts_at.desc()).limit(50))).scalars().all()
    return [season_public(s) for s in rows]


def season_public(s: Season) -> dict[str, Any]:
    return {"id": s.id, "key": s.key, "name": s.name, "description": s.description, "starts_at": s.starts_at.isoformat(),
            "ends_at": s.ends_at.isoformat(), "status": s.status}

