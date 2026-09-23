"""Leaderboards (cached per worker) and season boards."""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import NotFound
from ..models import Ranking, Season, SeasonStats, User, UserStats
from . import seasons as seasons_svc
from .users import user_brief

BOARDS: dict[str, tuple[Any, str]] = {
    "rolls": (UserStats.total_rolls, "Total Rolls"),
    "best": (UserStats.best_odds, "Best Rarity"),
    "collection": (UserStats.discovered_count, "Collection"),
    "achievements": (UserStats.achievements_count, "Achievements"),
    "wealth": (UserStats.net_worth, "Wealth"),
    "firsts": (UserStats.first_discoveries, "First Discoveries"),
}

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
TTL = 10.0


async def board(db: AsyncSession, key: str, limit: int = 100) -> dict[str, Any]:
    snap = get_registry().snap
    now = time.monotonic()
    if key == "season":
        return await season_board(db, None, "points", limit)
    if key not in BOARDS:
        raise NotFound("ランキングが見つかりません")
    cached = _cache.get(key)
    if cached and now - cached[0] < TTL:
        return {"board": key, "title": BOARDS[key][1], "entries": cached[1][:limit]}
    col, title = BOARDS[key]
    rows = (await db.execute(
        select(User, UserStats).join(UserStats, UserStats.user_id == User.id)
        .where(User.status != "banned", col > 0).order_by(col.desc(), User.id).limit(100)
    )).all()
    total = len(snap.collectible_ids) or 1
    entries = []
    for rank, (u, s) in enumerate(rows, start=1):
        value = float(getattr(s, col.key))
        e: dict[str, Any] = {"rank": rank, "user": user_brief(u), "value": value}
        if key == "best" and s.best_item_id:
            item = snap.items.get(s.best_item_id)
            e["item"] = {"name": item.name, "rarity": item.rarity_key, "visual": item.visual} if item else None
        if key == "collection":
            e["rate"] = value / total
        entries.append(e)
    _cache[key] = (now, entries)
    return {"board": key, "title": title, "entries": entries[:limit]}


async def season_board(db: AsyncSession, season_id: int | None, metric: str = "points", limit: int = 100) -> dict[str, Any]:
    sid = season_id or await seasons_svc.active_season_id(db)
    if sid is None:
        return {"board": "season", "title": "Season", "entries": [], "season": None}
    season = await db.get(Season, sid)
    if season is None:
        raise NotFound("シーズンが見つかりません")
    if season.status == "ended":
        rows = (await db.execute(select(Ranking, User).join(User, User.id == Ranking.user_id)
                                 .where(Ranking.season_id == sid, Ranking.board == f"season_{metric}").order_by(Ranking.rank).limit(limit))).all()
        entries = [{"rank": r.rank, "user": user_brief(u), "value": r.value} for r, u in rows]
        return {"board": "season", "metric": metric, "title": season.name, "entries": entries, "season": seasons_svc.season_public(season),
                "final": True}
    col = seasons_svc.SEASON_BOARDS.get(metric, SeasonStats.points)
    rows = (await db.execute(select(User, SeasonStats).join(SeasonStats, SeasonStats.user_id == User.id)
                             .where(SeasonStats.season_id == sid, User.status != "banned", col > 0)
                             .order_by(col.desc(), User.id).limit(limit))).all()
    entries = [{"rank": i, "user": user_brief(u), "value": float(getattr(s, col.key))} for i, (u, s) in enumerate(rows, start=1)]
    return {"board": "season", "metric": metric, "title": season.name, "entries": entries, "season": seasons_svc.season_public(season),
            "final": False}


async def user_rank(db: AsyncSession, key: str, user_id: int) -> dict[str, Any] | None:
    if key not in BOARDS:
        return None
    col, _ = BOARDS[key]
    s = await db.get(UserStats, user_id)
    if s is None:
        return None
    value = getattr(s, col.key)
    from sqlalchemy import func

    higher = int((await db.execute(select(func.count()).select_from(UserStats).join(User, User.id == UserStats.user_id)
                                   .where(col > value, User.status != "banned"))).scalar_one())
    return {"rank": higher + 1, "value": float(value)}


def clear_cache() -> None:
    _cache.clear()


def signature() -> str:
    parts = []
    for k, (_, entries) in sorted(_cache.items()):
        parts.append(k + ":" + ",".join(f"{e['user']['id']}:{e['value']}" for e in entries[:10]))
    return "|".join(parts)
