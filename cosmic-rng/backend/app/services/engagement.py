"""What brings a player back tomorrow: login bonus, weekly boards, the season
pass and server-wide community goals. All server-authoritative; the client only
asks and claims."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, Conflict, NotFound
from ..core.timeutil import utcnow
from ..db import greatest, upsert as insert
from ..models import GameEvent, SeasonPassClaim, User, UserStats, WeeklyStats
from . import feed as feed_svc
from . import progress as progress_svc
from . import seasons as seasons_svc
from . import users as users_svc

log = logging.getLogger("cosmic.engagement")


def _tz() -> ZoneInfo:
    return ZoneInfo(str(get_registry().setting("quests.reset_timezone") or "Asia/Tokyo"))


def local_day(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(_tz()).date().isoformat()


# ---------------------------------------------------------------------------
# Login bonus — a 7-day cycle that keeps going as long as the streak does.
# ---------------------------------------------------------------------------
LOGIN_BONUS: list[dict[str, Any]] = [
    {"stardust": 400},
    {"stardust": 700, "boosts": [{"key": "starlight_candle", "qty": 1}]},
    {"stardust": 1000, "shards": 5},
    {"stardust": 1500, "boosts": [{"key": "stellar_tonic", "qty": 1}]},
    {"stardust": 2200, "shards": 10},
    {"stardust": 3000, "boosts": [{"key": "triple_charm", "qty": 1}]},
    {"stardust": 6000, "shards": 40, "boosts": [{"key": "fortune_elixir", "qty": 1}]},
]


async def daily_status(db: AsyncSession, user_id: int) -> dict[str, Any]:
    stats = await users_svc.lock_stats(db, user_id)
    today = local_day()
    available = stats.last_bonus_day != today
    # A missed day breaks the streak; the cycle position always shows what the
    # NEXT claim will pay.
    yesterday = (utcnow().astimezone(_tz()).date() - timedelta(days=1)).isoformat()
    next_streak = stats.login_streak + 1 if stats.last_bonus_day in (today, yesterday) and stats.login_streak > 0 else 1
    if not available:
        next_streak = stats.login_streak
    cycle = (next_streak - 1) % len(LOGIN_BONUS)
    return {
        "available": available, "streak": stats.login_streak, "next_streak": next_streak,
        "cycle_day": cycle, "reward": LOGIN_BONUS[cycle], "days": LOGIN_BONUS, "today": today,
    }


async def claim_daily(db: AsyncSession, user_id: int) -> dict[str, Any]:
    user = await users_svc.lock_user(db, user_id)
    stats = await users_svc.lock_stats(db, user_id)
    today = local_day()
    if stats.last_bonus_day == today:
        raise Conflict("本日のボーナスは受け取り済みです", code="already_claimed")
    yesterday = (utcnow().astimezone(_tz()).date() - timedelta(days=1)).isoformat()
    stats.login_streak = stats.login_streak + 1 if stats.last_bonus_day == yesterday else 1
    stats.last_bonus_day = today
    cycle = (stats.login_streak - 1) % len(LOGIN_BONUS)
    stats.login_cycle = cycle
    reward = LOGIN_BONUS[cycle]
    granted = await progress_svc.grant_rewards(db, user, stats, reward, source="daily_bonus")
    if reward.get("shards"):
        stats.shards += int(reward["shards"])
        granted["shards"] = int(reward["shards"])
    return {"ok": True, "streak": stats.login_streak, "cycle_day": cycle, "granted": granted,
            "stardust": user.stardust, "shards": stats.shards}


# ---------------------------------------------------------------------------
# Weekly boards — ISO week in the quest timezone, resets Monday 00:00.
# ---------------------------------------------------------------------------
def week_key(now: datetime | None = None) -> str:
    d = (now or utcnow()).astimezone(_tz()).date()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


async def add_weekly(db: AsyncSession, user_id: int, *, rolls: int = 0, best_odds: float = 0.0, points: int = 0) -> None:
    stmt = insert(WeeklyStats).values(week=week_key(), user_id=user_id, rolls=rolls, best_odds=best_odds, points=points)
    stmt = stmt.on_conflict_do_update(
        index_elements=["week", "user_id"],
        set_={"rolls": WeeklyStats.rolls + rolls, "best_odds": greatest(WeeklyStats.best_odds, best_odds),
              "points": WeeklyStats.points + points},
    )
    await db.execute(stmt)


WEEKLY_BOARDS = {"weekly_rolls": WeeklyStats.rolls, "weekly_best": WeeklyStats.best_odds}


async def weekly_board(db: AsyncSession, key: str, limit: int = 100) -> dict[str, Any]:
    col = WEEKLY_BOARDS.get(key)
    if col is None:
        raise NotFound("ランキングが見つかりません")
    wk = week_key()
    rows = (await db.execute(
        select(User, WeeklyStats).join(WeeklyStats, WeeklyStats.user_id == User.id)
        .where(WeeklyStats.week == wk, User.status != "banned", col > 0)
        .order_by(col.desc(), User.id).limit(limit)
    )).all()
    entries = [{"rank": i, "user": users_svc.user_brief(u), "value": float(getattr(s, col.key))}
               for i, (u, s) in enumerate(rows, start=1)]
    return {"board": key, "week": wk, "entries": entries,
            "title": "今週のRoll数" if key == "weekly_rolls" else "今週の最高レア"}


# ---------------------------------------------------------------------------
# Season pass — free reward track over the season points players already earn.
# ---------------------------------------------------------------------------
PASS_TIERS: list[dict[str, Any]] = [
    {"points": 50, "rewards": {"stardust": 800}},
    {"points": 120, "rewards": {"boosts": [{"key": "starlight_candle", "qty": 2}]}},
    {"points": 220, "rewards": {"stardust": 1500, "shards": 10}},
    {"points": 360, "rewards": {"boosts": [{"key": "stellar_tonic", "qty": 2}]}},
    {"points": 550, "rewards": {"cosmetics": ["t_season_runner"]}},
    {"points": 800, "rewards": {"stardust": 4000}},
    {"points": 1120, "rewards": {"boosts": [{"key": "triple_charm", "qty": 2}], "shards": 20}},
    {"points": 1520, "rewards": {"stardust": 6000}},
    {"points": 2000, "rewards": {"boosts": [{"key": "rare_guarantee", "qty": 1}]}},
    {"points": 2600, "rewards": {"cosmetics": ["b_season_ace"], "shards": 40}},
    {"points": 3300, "rewards": {"stardust": 12000}},
    {"points": 4200, "rewards": {"boosts": [{"key": "fortune_elixir", "qty": 2}]}},
    {"points": 5300, "rewards": {"stardust": 18000, "shards": 60}},
    {"points": 6600, "rewards": {"boosts": [{"key": "epic_guarantee", "qty": 1}]}},
    {"points": 8200, "rewards": {"cosmetics": ["bg_season_meteor"]}},
    {"points": 10000, "rewards": {"stardust": 30000, "shards": 100}},
    {"points": 12500, "rewards": {"boosts": [{"key": "celestial_surge", "qty": 1}]}},
    {"points": 15500, "rewards": {"stardust": 45000}},
    {"points": 19000, "rewards": {"boosts": [{"key": "overdrive_chip", "qty": 2}], "shards": 150}},
    {"points": 23000, "rewards": {"cosmetics": ["t_season_sovereign"], "stardust": 80000}},
]


def reward_summary(rewards: dict[str, Any]) -> str:
    """One readable line per tier — names resolved here, where content lives."""
    snap = get_registry().snap
    parts: list[str] = []
    if rewards.get("stardust"):
        parts.append(f"✦{int(rewards['stardust']):,}")
    if rewards.get("shards"):
        parts.append(f"星の欠片 ×{int(rewards['shards'])}")
    for b in rewards.get("boosts", []) or []:
        d = snap.boosts.get(b["key"]) or {}
        parts.append(f"{d.get('name_ja') or d.get('name') or b['key']} ×{b.get('qty', 1)}")
    for key in rewards.get("cosmetics", []) or []:
        c = snap.cosmetics.get(key) or {}
        kind = {"title": "称号", "badge": "バッジ", "background": "背景"}.get(str(c.get("kind")), "装飾")
        parts.append(f"{kind}「{c.get('name_ja') or c.get('name') or key}」")
    for it in rewards.get("items", []) or []:
        d = snap.items_by_key.get(it["key"])
        parts.append(f"{(d.name_ja or d.name) if d else it['key']} ×{it.get('qty', 1)}")
    return " · ".join(parts) or "—"


async def pass_state(db: AsyncSession, user_id: int) -> dict[str, Any]:
    sid = await seasons_svc.active_season_id(db)
    if sid is None:
        return {"season": None, "points": 0, "tiers": [], "claimed": []}
    from ..models import Season, SeasonStats

    season = await db.get(Season, sid)
    st = await db.get(SeasonStats, (sid, user_id))
    points = int(st.points) if st else 0
    claimed = {r[0] for r in (await db.execute(
        select(SeasonPassClaim.tier_idx).where(SeasonPassClaim.season_id == sid, SeasonPassClaim.user_id == user_id))).all()}
    tiers = [{"idx": i, "points": t["points"], "rewards": t["rewards"], "summary": reward_summary(t["rewards"]),
              "reached": points >= t["points"], "claimed": i in claimed}
             for i, t in enumerate(PASS_TIERS)]
    return {"season": seasons_svc.season_public(season) if season else None, "points": points,
            "tiers": tiers, "claimed": sorted(claimed)}


async def claim_pass_tier(db: AsyncSession, user_id: int, tier_idx: int) -> dict[str, Any]:
    if tier_idx < 0 or tier_idx >= len(PASS_TIERS):
        raise AppError("段が不正です", code="invalid_tier")
    sid = await seasons_svc.active_season_id(db)
    if sid is None:
        raise AppError("開催中のシーズンがありません", code="no_season")
    from ..models import SeasonStats

    user = await users_svc.lock_user(db, user_id)
    stats = await users_svc.lock_stats(db, user_id)
    st = await db.get(SeasonStats, (sid, user_id))
    points = int(st.points) if st else 0
    tier = PASS_TIERS[tier_idx]
    if points < tier["points"]:
        raise AppError(f"シーズンポイントが足りません（{tier['points']}必要）", code="not_reached")
    res = await db.execute(
        insert(SeasonPassClaim).values(season_id=sid, user_id=user_id, tier_idx=tier_idx)
        .on_conflict_do_nothing().returning(SeasonPassClaim.tier_idx))
    if res.scalar_one_or_none() is None:
        raise AppError("この段は受け取り済みです", code="already_claimed")
    rewards = dict(tier["rewards"])
    granted = await progress_svc.grant_rewards(db, user, stats, rewards, source="season_pass")
    if rewards.get("shards"):
        stats.shards += int(rewards["shards"])
        granted["shards"] = int(rewards["shards"])
    return {"ok": True, "tier": tier_idx, "granted": granted, "stardust": user.stardust, "shards": stats.shards}


# ---------------------------------------------------------------------------
# Events, as players see them — plus community goals the whole server fills.
# ---------------------------------------------------------------------------
async def community_progress(db: AsyncSession, ev: dict[str, Any]) -> dict[str, Any]:
    params = ev.get("params") or {}
    baseline = params.get("baseline")
    total = int((await db.execute(select(func.coalesce(func.sum(UserStats.total_rolls), 0)))).scalar_one())
    progress = max(0, total - int(baseline)) if baseline is not None else 0
    target = max(1, int(params.get("target", 1)))
    return {"progress": min(progress, target), "target": target, "completed": bool(params.get("completed")),
            "reward_mult": float(params.get("reward_mult", 2.0)), "reward_hours": float(params.get("reward_hours", 24))}


async def public_events(db: AsyncSession) -> dict[str, Any]:
    snap = get_registry().snap
    now = utcnow()
    active, upcoming = [], []
    for e in snap.events:
        if not e.get("is_active"):
            continue
        starts, ends = e.get("starts_at"), e.get("ends_at")
        base = {"key": e["key"], "name": e["name"], "description": e.get("description") or "", "type": e["type"],
                "starts_at": starts.isoformat() if starts else None, "ends_at": ends.isoformat() if ends else None}
        if e["type"] == "luck_multiplier":
            base["mult"] = float((e.get("params") or {}).get("mult", 1.0))
        if e["type"] == "community_goal":
            base.update(await community_progress(db, e))
        if (starts is None or starts <= now) and (ends is None or ends > now):
            active.append(base)
        elif starts is not None and now < starts <= now + timedelta(days=7):
            upcoming.append(base)
    return {"active": active, "upcoming": upcoming, "server_time": now.isoformat()}


async def check_community_goals(db: AsyncSession) -> None:
    """Scheduler job: seed baselines, detect completion, hand out the reward."""
    now = utcnow()
    rows = (await db.execute(select(GameEvent).where(GameEvent.type == "community_goal", GameEvent.is_active.is_(True)))).scalars().all()
    for ev in rows:
        if ev.starts_at and ev.starts_at > now:
            continue
        if ev.ends_at and ev.ends_at <= now:
            continue
        params = dict(ev.params or {})
        if params.get("completed"):
            continue
        total = int((await db.execute(select(func.coalesce(func.sum(UserStats.total_rolls), 0)))).scalar_one())
        if params.get("baseline") is None:
            params["baseline"] = total
            ev.params = params
            await db.commit()
            continue
        target = max(1, int(params.get("target", 1)))
        if total - int(params["baseline"]) < target:
            continue
        params["completed"] = True
        params["completed_at"] = now.isoformat()
        ev.params = params
        mult = float(params.get("reward_mult", 2.0))
        hours = float(params.get("reward_hours", 24))
        db.add(GameEvent(key=f"{ev.key}_reward_{int(now.timestamp())}", name=f"{ev.name} 達成報酬",
                         description=f"みんなで目標達成！{int(hours)}時間 Luck ×{mult:g}", type="luck_multiplier",
                         params={"mult": mult}, starts_at=now, ends_at=now + timedelta(hours=hours), is_active=True))
        await feed_svc.add_world_event(db, "community_goal", None,
                                       {"name": ev.name, "mult": mult, "hours": hours}, public_user=False)
        await db.commit()
        # the 2s content-refresh loop picks the new reward event up on its own
        from ..core.pubsub import bus

        await bus.publish("all", "game_event", {"kind": "community_goal_done", "name": ev.name, "mult": mult, "hours": hours})
        log.info("community goal %s completed: luck x%s for %sh", ev.key, mult, hours)
