"""Quests, achievements, XP/levels and reward granting.

Game actions emit ``ProgressEvent``s; this module advances the player's active
quests and checks achievement conditions. Rewards from achievements are granted
immediately; quest rewards are claimed by the player.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..content.registry import Snapshot, get_registry
from ..db import upsert as insert
from ..core.errors import AppError, NotFound
from ..core.pubsub import queue_event
from ..core.timeutil import period_key, utcnow
from ..models import Achievement, Collection, User, UserAchievement, UserCosmetic, UserQuest, UserStats
from .constants import TIER_BY_KEY
from . import effects as effects_svc
from . import feed as feed_svc
from . import users as users_svc

log = logging.getLogger("cosmic.progress")


@dataclass(slots=True)
class ProgressEvent:
    type: str
    count: int = 1
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProgressResult:
    quests_completed: list[dict[str, Any]] = field(default_factory=list)
    achievements: list[dict[str, Any]] = field(default_factory=list)
    level_up: dict[str, Any] | None = None
    unlocked: list[str] = field(default_factory=list)
    rewards_stardust: int = 0

    def merge(self, other: "ProgressResult") -> None:
        self.quests_completed += other.quests_completed
        self.achievements += other.achievements
        if other.level_up:
            if self.level_up:
                self.level_up["to"] = other.level_up["to"]
            else:
                self.level_up = other.level_up
        self.unlocked += [u for u in other.unlocked if u not in self.unlocked]
        self.rewards_stardust += other.rewards_stardust

    def public(self) -> dict[str, Any]:
        return {"quests_completed": self.quests_completed, "achievements": self.achievements, "level_up": self.level_up,
                "unlocked": self.unlocked}


# ---------------------------------------------------------------------------
# XP & rewards
# ---------------------------------------------------------------------------
async def add_xp(db: AsyncSession, user: User, stats: UserStats, xp: int, result: ProgressResult) -> None:
    if xp <= 0:
        return
    user.xp += int(xp)
    new_level = users_svc.level_for_xp(user.xp)
    if new_level > user.level:
        old = user.level
        user.level = new_level
        result.level_up = {"from": old, "to": new_level} if result.level_up is None else {**result.level_up, "to": new_level}
        result.unlocked += users_svc.newly_unlocked(old, new_level)
        sub = await handle_events(db, user, stats, [ProgressEvent("level", params={"level": new_level})])
        result.merge(sub)


async def grant_rewards(db: AsyncSession, user: User, stats: UserStats, rewards: dict[str, Any], *, source: str,
                        scale_level: bool = False, result: ProgressResult | None = None) -> dict[str, Any]:
    result = result if result is not None else ProgressResult()
    mult = 1.0 + (float(rewards.get("per_level", 0)) * user.level if scale_level else 0.0)
    stardust = int(round(int(rewards.get("stardust", 0) or 0) * mult))
    granted: dict[str, Any] = {"stardust": stardust, "xp": 0, "boosts": [], "items": [], "cosmetics": []}
    if stardust:
        user.stardust += stardust
        stats.stardust_earned += stardust
        result.rewards_stardust += stardust
    for b in rewards.get("boosts", []) or []:
        await effects_svc.grant_boost_items(db, user.id, b["key"], int(b.get("qty", 1)))
        granted["boosts"].append(b)
    for c in rewards.get("cosmetics", []) or []:
        await grant_cosmetic(db, user.id, c, source)
        granted["cosmetics"].append(c)
    snap = get_registry().snap
    for it in rewards.get("items", []) or []:
        item = snap.items_by_key.get(it["key"])
        if item is None:
            continue
        from .inventory import create_instances

        await create_instances(db, user.id, item.id, int(it.get("qty", 1)), source, tier=item.tier)
        granted["items"].append({"key": item.key, "name": item.name, "qty": int(it.get("qty", 1))})
    xp = int(round(int(rewards.get("xp", 0) or 0) * mult))
    if xp:
        granted["xp"] = xp
        await add_xp(db, user, stats, xp, result)
    return granted


async def grant_cosmetic(db: AsyncSession, user_id: int, key: str, source: str) -> bool:
    if key not in get_registry().snap.cosmetics:
        return False
    res = await db.execute(
        insert(UserCosmetic).values(user_id=user_id, cosmetic_key=key, source=source)
        .on_conflict_do_nothing().returning(UserCosmetic.cosmetic_key)
    )
    return res.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------
def _tz() -> str:
    return str(get_registry().setting("quests.reset_timezone") or "Asia/Tokyo")


def _scaled_target(obj: dict[str, Any], level: int) -> int:
    return max(1, int(round(int(obj.get("target", 1)) + float(obj.get("per_level", 0)) * (level - 1))))


def _quest_row(q: dict[str, Any], user: User, period: str) -> dict[str, Any]:
    obj = dict(q["objective"] or {})
    return {"user_id": user.id, "quest_id": q["id"], "period_key": period, "objective": obj,
            "target": _scaled_target(obj, user.level), "rewards": q["rewards"] or {}}


async def ensure_quests(db: AsyncSession, user: User) -> None:
    """Assign today's dailies, current chain steps and hidden quests (idempotent)."""
    snap = get_registry().snap
    now = utcnow()
    today = period_key(now, _tz())
    rows = (await db.execute(select(UserQuest.quest_id, UserQuest.period_key, UserQuest.status).where(UserQuest.user_id == user.id))).all()
    have = {(r.quest_id, r.period_key) for r in rows}
    status_by_quest: dict[int, set[str]] = {}
    for r in rows:
        status_by_quest.setdefault(r.quest_id, set()).add(r.status)
    new_rows: list[dict[str, Any]] = []

    # hidden quests: permanent
    for q in snap.quests.values():
        if q["kind"] == "hidden" and q["is_active"] and (q["id"], "hidden") not in have and user.level >= q["min_level"]:
            new_rows.append(_quest_row(q, user, "hidden"))

    # chain quests: the lowest unclaimed step of each chain
    chains: dict[str, list[dict[str, Any]]] = {}
    for q in snap.quests.values():
        if q["kind"] == "chain" and q["is_active"] and q["chain_key"]:
            chains.setdefault(q["chain_key"], []).append(q)
    for steps in chains.values():
        steps.sort(key=lambda s: s["chain_step"] or 0)
        for step in steps:
            st = status_by_quest.get(step["id"], set())
            if "claimed" in st:
                continue
            if not st and user.level >= step["min_level"]:
                new_rows.append(_quest_row(step, user, "chain"))
            break

    # dailies
    if users_svc.feature_unlocked(user, "quests"):
        count = int(get_registry().setting("quests.daily_count") or 0)
        todays = [r for r in rows if r.period_key == today]
        if not todays and count > 0:
            pool = [q for q in snap.quests.values() if q["kind"] == "daily" and q["is_active"] and user.level >= q["min_level"]]
            rng = random.Random(f"{user.id}:{today}")
            chosen: list[dict[str, Any]] = []
            while pool and len(chosen) < count:
                total = sum(max(q["weight"], 0.01) for q in pool)
                u = rng.random() * total
                acc = 0.0
                for q in pool:
                    acc += max(q["weight"], 0.01)
                    if u < acc:
                        chosen.append(q)
                        pool.remove(q)
                        break
            for q in chosen:
                new_rows.append(_quest_row(q, user, today))
    if new_rows:
        await db.execute(insert(UserQuest).values(new_rows).on_conflict_do_nothing())


def _local(dt: datetime, tz: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz))
    except Exception:
        return dt


def _quest_increment(obj: dict[str, Any], ev: ProgressEvent, snap: Snapshot, current: int) -> int | None:
    """Returns the new progress value, or None if the event does not apply."""
    t = obj.get("type")
    p = obj.get("params") or {}
    e = ev.params
    default_biome = snap.default_biome.key
    if t == "roll" and ev.type == "roll":
        b = p.get("biome")
        if b == "any_special" and e.get("biome") == default_biome:
            return None
        if b and b != "any_special" and e.get("biome") != b:
            return None
        return current + ev.count
    if t == "special_roll" and ev.type == "roll":
        n = int(e.get("special_count", 0))
        return current + n if n else None
    if t == "hidden_special" and ev.type == "roll":
        n = int((e.get("hidden_specials") or {}).get(p.get("key"), 0))
        return current + n if n else None
    if t == "luck" and ev.type == "roll" and e.get("luck"):
        return max(current, int(min(float(e["luck"]), 9e15)))
    if t == "roll_at_time" and ev.type == "roll" and e.get("time"):
        lt = _local(e["time"], p.get("tz", "UTC"))
        return current + 1 if (lt.hour == int(p.get("hh", -1)) and lt.minute == int(p.get("mm", -1))) else None
    if t == "roll_between" and ev.type == "roll" and e.get("time"):
        lt = _local(e["time"], p.get("tz", "UTC"))
        s, en = int(p.get("start", 0)), int(p.get("end", 0))
        inside = (s <= lt.hour < en) if s <= en else (lt.hour >= s or lt.hour < en)
        return current + ev.count if inside else None
    if t == "obtain_tier" and ev.type == "obtain":
        return current + ev.count if int(e.get("tier", 0)) >= TIER_BY_KEY.get(p.get("tier", "common"), 1) else None
    if t == "obtain_item" and ev.type == "obtain":
        return current + ev.count if e.get("item_key") == p.get("item") else None
    if t == "streak_same" and ev.type == "obtain" and e.get("streak"):
        return max(current, int(e["streak"]))
    if t == "sell_value" and ev.type == "sell":
        return current + 1 if int(e.get("max_value", 0)) >= int(p.get("value", 0)) else None
    if t == "biome_enter" and ev.type == "biome_enter":
        b = p.get("biome")
        if b == "any_special":
            return current + 1 if e.get("biome") != default_biome else None
        return current + 1 if e.get("biome") == b else None
    if t == "level" and ev.type == "level":
        return max(current, int(e.get("level", 0)))
    if t == "earn" and ev.type == "earn":
        return current + int(e.get("amount", 0))
    simple = {"sell": "sell", "discover": "discover", "craft": "craft", "boost_use": "boost_use", "market_sell": "market_sell",
              "trade": "trade", "gift": "gift", "equip": "equip"}
    if t in simple and ev.type == simple[t]:
        return current + ev.count
    return None


async def _advance_quests(db: AsyncSession, user: User, events: list[ProgressEvent], result: ProgressResult) -> None:
    snap = get_registry().snap
    today = period_key(utcnow(), _tz())
    rows = (await db.execute(select(UserQuest).where(UserQuest.user_id == user.id, UserQuest.status == "active"))).scalars().all()
    for uq in rows:
        if uq.period_key not in ("chain", "hidden", today):
            continue
        q = snap.quests_by_id.get(uq.quest_id)
        if q is None or not q["is_active"]:
            continue
        progress = int(uq.progress)
        changed = False
        for ev in events:
            new = _quest_increment(uq.objective or q["objective"], ev, snap, progress)
            if new is not None and new != progress:
                progress = new
                changed = True
        if changed:
            uq.progress = min(progress, uq.target) if (uq.objective or {}).get("type") != "luck" else progress
            if progress >= uq.target:
                uq.status = "completed"
                uq.completed_at = utcnow()
                result.quests_completed.append({"id": uq.id, "key": q["key"], "name": q["name"], "kind": q["kind"]})


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------
def _rarity_at_least(stats: UserStats, snap: Snapshot, rarity_key: str) -> int:
    need = snap.tier_of(rarity_key)
    counts = stats.rarity_counts or {}
    return sum(int(v) for k, v in counts.items() if snap.tier_of(k) >= need)


async def _condition_met(db: AsyncSession, cond: dict[str, Any], user: User, stats: UserStats, snap: Snapshot,
                         events: list[ProgressEvent]) -> bool:
    if "stat" in cond:
        return float(getattr(stats, cond["stat"], 0) or 0) >= float(cond.get("gte", 1))
    if "rarity" in cond:
        return _rarity_at_least(stats, snap, cond["rarity"]) >= int(cond.get("gte", 1))
    if "level" in cond:
        return user.level >= int(cond["level"])
    if "biome" in cond:
        return cond["biome"] in (stats.biomes_seen or [])
    if "biomes_count" in cond:
        natural = {b.key for b in snap.natural_biomes}
        return len(natural.intersection(stats.biomes_seen or [])) >= int(cond["biomes_count"])
    if "item" in cond:
        if not any(e.type == "obtain" and e.params.get("item_key") == cond["item"] for e in events):
            return False
        return True
    if "item_count" in cond:
        item = snap.items_by_key.get(cond["item_count"])
        if item is None or not any(e.type == "obtain" and e.params.get("item_key") == item.key for e in events):
            return False
        row = await db.get(Collection, (user.id, item.id))
        return bool(row and row.times_obtained >= int(cond.get("gte", 1)))
    if "event" in cond:
        return any(e.type == "trigger" and e.params.get("key") == cond["event"] for e in events)
    return False


async def check_achievements(db: AsyncSession, user: User, stats: UserStats, events: list[ProgressEvent],
                             result: ProgressResult) -> None:
    snap = get_registry().snap
    achieved = {r[0] for r in (await db.execute(select(UserAchievement.achievement_id).where(UserAchievement.user_id == user.id))).all()}
    for a in snap.achievements.values():
        if not a["is_active"] or a["id"] in achieved:
            continue
        if not await _condition_met(db, a["condition"] or {}, user, stats, snap, events):
            continue
        ins = await db.execute(
            insert(UserAchievement).values(user_id=user.id, achievement_id=a["id"]).on_conflict_do_nothing()
            .returning(UserAchievement.achievement_id)
        )
        if ins.scalar_one_or_none() is None:
            continue
        achieved.add(a["id"])
        stats.achievements_count += 1
        first = (await db.execute(
            update(Achievement).where(Achievement.id == a["id"], Achievement.first_achiever_id.is_(None))
            .values(first_achiever_id=user.id, first_achieved_at=utcnow(), achiever_count=Achievement.achiever_count + 1)
            .returning(Achievement.id)
        )).scalar_one_or_none() is not None
        if not first:
            await db.execute(update(Achievement).where(Achievement.id == a["id"]).values(achiever_count=Achievement.achiever_count + 1))
        else:
            await db.execute(update(UserAchievement).where(UserAchievement.user_id == user.id, UserAchievement.achievement_id == a["id"])
                             .values(world_first=True))
        granted = await grant_rewards(db, user, stats, a["rewards"] or {}, source="achievement", result=result)
        if first:
            laurel = snap.items_by_key.get("laurel_of_firsts")
            if laurel:
                from .inventory import create_instances

                await create_instances(db, user.id, laurel.id, 1, "achievement", tier=laurel.tier,
                                       meta={"achievement": a["key"], "achievement_name": a["name"]})
                granted["items"].append({"key": laurel.key, "name": laurel.name, "qty": 1})
            await feed_svc.add_world_event(db, "world_first_achievement", user,
                                           {"achievement": a["key"], "name": a["name"], "tier": a["tier"]}, True)
        entry = {"key": a["key"], "name": a["name"], "description": a["description"], "tier": a["tier"], "category": a["category"],
                 "world_first": first, "rewards": granted}
        result.achievements.append(entry)
        queue_event(db, "user", "achievement", entry, user_id=user.id)


async def handle_events(db: AsyncSession, user: User, stats: UserStats, events: list[ProgressEvent]) -> ProgressResult:
    result = ProgressResult()
    if not events:
        return result
    await _advance_quests(db, user, events, result)
    await check_achievements(db, user, stats, events, result)
    return result


async def claim_quest(db: AsyncSession, user: User, user_quest_id: int) -> dict[str, Any]:
    snap = get_registry().snap
    uq = (await db.execute(select(UserQuest).where(UserQuest.id == user_quest_id, UserQuest.user_id == user.id).with_for_update())).scalar_one_or_none()
    if uq is None:
        raise NotFound("クエストが見つかりません")
    if uq.status == "claimed":
        raise AppError("報酬は受け取り済みです", code="already_claimed")
    if uq.status != "completed":
        raise AppError("クエストが未達成です", code="not_completed")
    q = snap.quests_by_id.get(uq.quest_id)
    uq.status = "claimed"
    uq.claimed_at = utcnow()
    stats = await users_svc.lock_stats(db, user.id)
    stats.quests_completed += 1
    result = ProgressResult()
    granted = await grant_rewards(db, user, stats, uq.rewards or {}, source="quest", scale_level=(q or {}).get("kind") == "daily",
                                  result=result)
    await check_achievements(db, user, stats, [ProgressEvent("quest_claim")], result)
    if q and q["kind"] == "chain":
        await ensure_quests(db, user)
    return {"granted": granted, "progress": result.public()}


async def record_biome_seen(stats: UserStats, biome_key: str) -> bool:
    seen = list(stats.biomes_seen or [])
    if biome_key in seen:
        return False
    seen.append(biome_key)
    stats.biomes_seen = seen
    flag_modified(stats, "biomes_seen")
    return True


async def trigger(db: AsyncSession, user: User, key: str) -> ProgressResult:
    stats = await users_svc.lock_stats(db, user.id)
    return await handle_events(db, user, stats, [ProgressEvent("trigger", params={"key": key})])
