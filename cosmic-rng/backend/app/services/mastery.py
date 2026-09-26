"""Long-game systems: star shards, collection sets, prestige, fortune report."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.collection_sets import SET_MAP, SETS
from ..content.registry import get_registry
from ..core.errors import AppError, NotFound
from ..core.timeutil import utcnow
from ..db import upsert as insert
from ..models import ActiveEffect, Collection, ItemInstance, Roll, RollBatch, SetClaim, User, UserStats
from . import effects as effects_svc
from . import feed as feed_svc
from . import progress as progress_svc
from . import users as users_svc

# ---------------------------------------------------------------------------
# Star shards — duplicates become a slow, deterministic path to a guarantee.
# The odds table itself is never touched: what you buy is a one-roll floor.
# ---------------------------------------------------------------------------
SHARD_VALUE = {1: 1, 2: 4, 3: 15, 4: 60, 5: 250, 6: 1000, 7: 4000}

SHARD_EXCHANGES: list[dict[str, Any]] = [
    {"key": "guarantee_rare", "name": "確定：稀少以上", "cost": 60, "tier": "rare",
     "description": "次の1回が必ず稀少級以上になります"},
    {"key": "guarantee_epic", "name": "確定：英雄以上", "cost": 350, "tier": "epic",
     "description": "次の1回が必ず英雄級以上になります"},
    {"key": "guarantee_legendary", "name": "確定：伝説以上", "cost": 2200, "tier": "legendary",
     "description": "次の1回が必ず伝説級以上になります"},
    {"key": "guarantee_secret", "name": "確定：秘匿以上", "cost": 13000, "tier": "secret",
     "description": "次の1回が必ず秘匿級以上になります"},
]


async def shards_state(db: AsyncSession, user_id: int) -> dict[str, Any]:
    stats = await users_svc.lock_stats(db, user_id)
    preview = await _dup_preview(db, user_id)
    return {"shards": int(stats.shards), "values": SHARD_VALUE, "exchanges": SHARD_EXCHANGES, "convertible": preview}


async def _dup_candidates(db: AsyncSession, user_id: int) -> list[ItemInstance]:
    """Every owned copy beyond the first of each item — unlocked, unlisted."""
    rows = (await db.execute(
        select(ItemInstance).where(ItemInstance.owner_id == user_id, ItemInstance.state == "owned",
                                   ItemInstance.locked.is_(False))
        .order_by(ItemInstance.item_id, ItemInstance.id))).scalars().all()
    fav = await _favorites(db, user_id)
    out: list[ItemInstance] = []
    seen: set[int] = set()
    for r in rows:
        if r.item_id in fav:
            continue
        if r.item_id in seen:
            out.append(r)
        else:
            seen.add(r.item_id)
    return out


async def _favorites(db: AsyncSession, user_id: int) -> set[int]:
    from . import inventory as inv_svc

    return await inv_svc.item_favorites(db, user_id)


def _shard_value_of(tier: int) -> int:
    return SHARD_VALUE.get(max(1, min(7, int(tier))), 1)


async def _dup_preview(db: AsyncSession, user_id: int) -> dict[str, Any]:
    cands = await _dup_candidates(db, user_id)
    return {"count": len(cands), "shards": sum(_shard_value_of(c.tier) for c in cands)}


async def convert_duplicates(db: AsyncSession, user_id: int) -> dict[str, Any]:
    stats = await users_svc.lock_stats(db, user_id)
    cands = await _dup_candidates(db, user_id)
    if not cands:
        raise AppError("変換できる重複がありません", code="nothing_to_convert")
    total = sum(_shard_value_of(c.tier) for c in cands)
    for c in cands:
        await db.delete(c)
    stats.shards += total
    return {"ok": True, "converted": len(cands), "gained": total, "shards": int(stats.shards)}


async def exchange_shards(db: AsyncSession, user_id: int, key: str) -> dict[str, Any]:
    ex = next((e for e in SHARD_EXCHANGES if e["key"] == key), None)
    if ex is None:
        raise NotFound("交換先が見つかりません")
    stats = await users_svc.lock_stats(db, user_id)
    if stats.shards < ex["cost"]:
        raise AppError("欠片が足りません", code="insufficient_shards")
    stats.shards -= ex["cost"]
    await effects_svc.add_effect(
        db, user_id, source_type="shards", source_key=ex["key"], name=ex["name"], effect_type="min_rarity",
        rolls=1, params={"tier": ex["tier"]})
    return {"ok": True, "shards": int(stats.shards), "effect": ex["name"]}


# ---------------------------------------------------------------------------
# Collection sets
# ---------------------------------------------------------------------------
def _set_item_ids(snap: Any, s: dict[str, Any]) -> list[int]:
    return [snap.items_by_key[k].id for k in s["items"] if k in snap.items_by_key]


async def sets_state(db: AsyncSession, user_id: int) -> dict[str, Any]:
    snap = get_registry().snap
    owned = {r[0] for r in (await db.execute(select(Collection.item_id).where(Collection.user_id == user_id))).all()}
    claimed = {r[0] for r in (await db.execute(select(SetClaim.set_key).where(SetClaim.user_id == user_id))).all()}
    out = []
    for s in SETS:
        ids = _set_item_ids(snap, s)
        items = []
        for k in s["items"]:
            it = snap.items_by_key.get(k)
            if it is None:
                continue
            have = it.id in owned
            items.append({"key": k, "name": it.name, "name_ja": it.name_ja, "rarity": it.rarity_key,
                          "visual": it.visual if have else {}, "owned": have})
        done = len(ids) > 0 and all(i in owned for i in ids)
        out.append({"key": s["key"], "name": s["name"], "name_ja": s["name_ja"], "luck_pct": s["luck_pct"],
                    "stardust": s.get("stardust", 0), "cosmetic": s.get("cosmetic"), "items": items,
                    "owned": sum(1 for i in items if i["owned"]), "total": len(items),
                    "complete": done, "claimed": s["key"] in claimed})
    return {"sets": out, "claimed": len(claimed)}


async def claim_set(db: AsyncSession, user_id: int, set_key: str) -> dict[str, Any]:
    s = SET_MAP.get(set_key)
    if s is None:
        raise NotFound("セットが見つかりません")
    snap = get_registry().snap
    ids = _set_item_ids(snap, s)
    owned = {r[0] for r in (await db.execute(
        select(Collection.item_id).where(Collection.user_id == user_id, Collection.item_id.in_(ids)))).all()}
    if not ids or any(i not in owned for i in ids):
        raise AppError("まだ全アイテムを集めていません", code="incomplete")
    res = await db.execute(insert(SetClaim).values(user_id=user_id, set_key=set_key)
                           .on_conflict_do_nothing().returning(SetClaim.set_key))
    if res.scalar_one_or_none() is None:
        raise AppError("このセットは受け取り済みです", code="already_claimed")
    user = await users_svc.lock_user(db, user_id)
    stats = await users_svc.lock_stats(db, user_id)
    await effects_svc.add_effect(
        db, user_id, source_type="set", source_key=set_key, name=f"セット: {s['name_ja']}",
        effect_type="luck", value=float(s["luck_pct"]), stack_mode="add")
    rewards: dict[str, Any] = {"stardust": s.get("stardust", 0)}
    if s.get("cosmetic"):
        rewards["cosmetics"] = [s["cosmetic"]]
    granted = await progress_svc.grant_rewards(db, user, stats, rewards, source="set")
    return {"ok": True, "granted": granted, "luck_pct": s["luck_pct"], "stardust": user.stardust}


# ---------------------------------------------------------------------------
# Prestige — the ceiling becomes a door.
# ---------------------------------------------------------------------------
PRESTIGE_LUCK_PCT = 5.0
PRESTIGE_TITLES = {1: "t_reborn_1", 3: "t_reborn_3", 5: "t_reborn_5", 10: "t_reborn_10"}


async def prestige_state(db: AsyncSession, user_id: int) -> dict[str, Any]:
    user = await db.get(User, user_id)
    stats = await users_svc.lock_stats(db, user_id)
    max_level = int(get_registry().setting("progression.max_level") or 200)
    return {"level": user.level, "max_level": max_level, "prestige": int(stats.prestige),
            "luck_pct_each": PRESTIGE_LUCK_PCT, "current_bonus_pct": PRESTIGE_LUCK_PCT * int(stats.prestige),
            "ready": user.level >= max_level}


async def do_prestige(db: AsyncSession, user_id: int) -> dict[str, Any]:
    user = await users_svc.lock_user(db, user_id)
    stats = await users_svc.lock_stats(db, user_id)
    max_level = int(get_registry().setting("progression.max_level") or 200)
    if user.level < max_level:
        raise AppError(f"転生には Lv.{max_level} が必要です", code="level_required")
    stats.prestige += 1
    user.level = 1
    user.xp = 0
    # One permanent effect row that grows, instead of a pile of rows.
    row = (await db.execute(select(ActiveEffect).where(
        ActiveEffect.user_id == user_id, ActiveEffect.source_type == "prestige"))).scalars().first()
    if row is None:
        await effects_svc.add_effect(db, user_id, source_type="prestige", source_key="prestige",
                                     name=f"転生 ★{stats.prestige}", effect_type="luck",
                                     value=PRESTIGE_LUCK_PCT * stats.prestige, stack_mode="add")
    else:
        row.value = PRESTIGE_LUCK_PCT * stats.prestige
        row.name = f"転生 ★{stats.prestige}"
    title = PRESTIGE_TITLES.get(stats.prestige)
    if title:
        await progress_svc.grant_cosmetic(db, user_id, title, "prestige")
    await feed_svc.add_world_event(db, "prestige", user, {"count": stats.prestige}, True)
    return {"ok": True, "prestige": stats.prestige, "bonus_pct": PRESTIGE_LUCK_PCT * stats.prestige,
            "title": title, "level": user.level}


# ---------------------------------------------------------------------------
# Fortune report — how lucky have you actually been?
# ---------------------------------------------------------------------------
_expected_cache: dict[int, dict[int, float]] = {}


def _expected_tier_probs(snap: Any) -> dict[int, float]:
    """Neutral-luck probability of each tier, from the base odds of everything
    rollable. Luck reshapes this in play, so the comparison flatters nobody:
    beating it means the rolls, boosts and biomes actually went your way."""
    hit = _expected_cache.get(snap.version)
    if hit is not None:
        return hit
    weights: dict[int, float] = {}
    for it in snap.rollable:
        if it.odds:
            weights[it.tier] = weights.get(it.tier, 0.0) + 1.0 / float(it.odds)
    z = sum(weights.values()) or 1.0
    probs = {t: w / z for t, w in weights.items()}
    _expected_cache.clear()
    _expected_cache[snap.version] = probs
    return probs


async def fortune_report(db: AsyncSession, user_id: int) -> dict[str, Any]:
    snap = get_registry().snap
    stats = await users_svc.lock_stats(db, user_id)
    total = int(stats.total_rolls)
    # percentile by best find
    higher = (await db.execute(select(func.count()).select_from(UserStats)
                               .where(UserStats.best_odds > stats.best_odds, UserStats.total_rolls > 0))).scalar_one()
    everyone = (await db.execute(select(func.count()).select_from(UserStats)
                                 .where(UserStats.total_rolls > 0))).scalar_one() or 1
    top_pct = (int(higher) + 1) / int(everyone) * 100

    exp = _expected_tier_probs(snap)
    tier_names = {r.tier: {"key": r.key, "name": r.name, "name_ja": r.name_ja} for r in snap.rarities.values()}
    counts = {int(TIERS_BY_KEY.get(k, 0)): int(v) for k, v in (stats.rarity_counts or {}).items() if k in TIERS_BY_KEY}
    tiers = []
    for t in sorted(exp):
        expected = exp[t] * total
        actual = counts.get(t, 0)
        tiers.append({"tier": t, **tier_names.get(t, {}), "actual": actual,
                      "expected": round(expected, 2),
                      "ratio": round(actual / expected, 3) if expected >= 0.5 else None})
    # last 14 days of activity: individual rolls + offline batches
    since = utcnow() - timedelta(days=14)
    daily: dict[str, int] = {}
    for d, n in (await db.execute(
            select(func.date(Roll.created_at), func.count()).where(Roll.user_id == user_id, Roll.created_at >= since)
            .group_by(func.date(Roll.created_at)))).all():
        daily[str(d)] = daily.get(str(d), 0) + int(n)
    for d, n in (await db.execute(
            select(func.date(RollBatch.window_end), func.coalesce(func.sum(RollBatch.roll_count), 0))
            .where(RollBatch.user_id == user_id, RollBatch.window_end >= since)
            .group_by(func.date(RollBatch.window_end)))).all():
        daily[str(d)] = daily.get(str(d), 0) + int(n)
    days = []
    day = utcnow().date() - timedelta(days=13)
    for i in range(14):
        key = (day + timedelta(days=i)).isoformat()
        days.append({"date": key, "rolls": daily.get(key, 0)})
    best_item = None
    if stats.best_item_id:
        from . import inventory as inv_svc

        try:
            best_item = await inv_svc.item_info(db, stats.best_item_id)
        except Exception:
            best_item = None
    return {"total_rolls": total, "best_odds": stats.best_odds, "best_item": best_item,
            "top_percent": round(top_pct, 2), "players": int(everyone), "tiers": tiers, "daily": days,
            "prestige": int(stats.prestige), "shards": int(stats.shards)}


TIERS_BY_KEY = {"common": 1, "rare": 2, "epic": 3, "legendary": 4, "secret": 5, "ultra_secret": 6, "mythic": 7}
