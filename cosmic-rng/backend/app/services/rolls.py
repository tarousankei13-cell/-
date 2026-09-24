"""Roll orchestration: online rolls, offline auto-roll batches and burst rolls.

Everything authoritative happens here, on the server, inside one database
transaction per request, with the player's row locked (``SELECT ... FOR
UPDATE``) so concurrent requests for the same player are serialised:

    cooldown check → biome advance → effects/equipment → luck → compiled table
    → CSPRNG draw → auto-delete / capacity → instances, collection, first
    discovery → stats, XP, quests, achievements → feed / Discord / WebSocket

The client only ever sends "roll" (and whether it is an auto roll); luck,
odds, results and timing are never accepted from the client.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..content.registry import ItemDef, Snapshot, get_registry
from ..db import is_sqlite, upsert as insert
from ..core.errors import AppError, Forbidden, RateLimited
from ..core.pubsub import queue_event
from ..core.timeutil import utcnow
from ..models import ActiveEffect, Collection, Item, Roll, RollBatch, User, UserStats, UserUnlock
from ..rng import engine as rng_engine
from ..rng.biome_engine import Segment, Transition
from ..rng.engine import RNG_VERSION, CompiledTable, LuckBreakdown, RollContext, fortune, system_rng, table_cache
from ..rng.modifiers import (
    EffectData,
    EquipData,
    RollModifiers,
    combine_effects,
    equipment_luck,
    passive_luck,
    passive_sum,
    roll_cooldown,
    special_roll_info,
)
from ..rng.procedural import GeneratedSpec, generate
from . import biomes as biomes_svc
from . import discord_notify
from . import effects as effects_svc
from . import equipment as equipment_svc
from . import feed as feed_svc
from . import inventory as inv_svc
from . import progress as progress_svc
from . import seasons as seasons_svc
from . import user_settings as settings_svc
from . import users as users_svc
from .constants import (
    FLAG_AUTO,
    FLAG_AUTO_DELETED,
    FLAG_AUTO_SOLD,
    FLAG_BURST,
    FLAG_FIRST_DISCOVERY,
    FLAG_FORCED,
    FLAG_HIDDEN_SPECIAL,
    FLAG_OFFLINE,
    FLAG_OVERFLOW,
    FLAG_PREVIEW,
    FLAG_SPECIAL,
    TIER_BY_KEY,
)
from .metrics import metrics
from .progress import ProgressEvent, ProgressResult
from .stats import item_stats

log = logging.getLogger("cosmic.rolls")

MAX_INDIVIDUAL_BATCH_ROLLS = 500


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
@dataclass
class Env:
    snap: Snapshot
    now: datetime
    user: User
    stats: UserStats
    settings: settings_svc.PlayerSettings
    biome: biomes_svc.BiomeContext
    effect_rows: list[ActiveEffect]
    effects: list[EffectData]
    equips: list[EquipData]
    equip_visuals: list[dict[str, Any]]
    favorites: set[int]
    unlocks: set[str]
    chance_mult: float
    progress: ProgressResult = field(default_factory=ProgressResult)
    events: list[ProgressEvent] = field(default_factory=list)

    @property
    def sell_bonus(self) -> float:
        return passive_sum(self.equips, "sell_bonus")

    @property
    def xp_bonus(self) -> float:
        return passive_sum(self.equips, "xp_bonus")

    @property
    def reveal_rng(self) -> bool:
        return any(e.effect_type == "reveal_rng" and e.active(self.now) for e in self.effects)


async def load_env(db: AsyncSession, user: User, now: datetime, *, record_from: datetime | None = None) -> Env:
    snap = await get_registry().ensure_fresh(db)
    stats = await users_svc.lock_stats(db, user.id)
    settings = await settings_svc.load(db, user.id)
    rows = await effects_svc.load_active(db, user.id, now, lock=True)
    equips, visuals = await equipment_svc.load_equipped(db, user.id)
    chance = biomes_svc.chance_multiplier(snap, equips, rows, now)
    biome_ctx = await biomes_svc.advance_user(db, user, now, chance_mult=chance, record_from=record_from)
    favorites = await inv_svc.item_favorites(db, user.id)
    unlocks = {r[0] for r in (await db.execute(select(UserUnlock.unlock_key).where(UserUnlock.user_id == user.id))).all()}
    env = Env(snap=snap, now=now, user=user, stats=stats, settings=settings, biome=biome_ctx, effect_rows=rows,
              effects=[effects_svc.to_data(r) for r in rows], equips=equips, equip_visuals=visuals, favorites=favorites,
              unlocks=unlocks, chance_mult=chance)
    await handle_biome_transitions(db, env, biome_ctx.result.transitions)
    return env


async def handle_biome_transitions(db: AsyncSession, env: Env, transitions: list[Transition], notify: bool = True) -> None:
    snap = env.snap
    entered = [t for t in transitions if t.kind == "biome" and t.to_key and t.to_key != snap.default_biome.key]
    for t in entered:
        await progress_svc.record_biome_seen(env.stats, t.to_key)
        env.events.append(ProgressEvent("biome_enter", params={"biome": t.to_key}))
    if entered:
        last = entered[-1].to_key
        if last and biomes_svc.is_announce_worthy(snap, last) and (utcnow() - entered[-1].at).total_seconds() < 600:
            b = snap.biomes[last]
            await feed_svc.add_world_event(db, "rare_biome", env.user, {"biome": b.key, "name": b.name, "odds_per_sec": b.odds_per_sec},
                                           env.settings.privacy.public_drops)
    if transitions and notify:
        queue_event(db, "user", "biome", biomes_svc.public_state(env.biome.row, reveal=env.reveal_rng), user_id=env.user.id)


# ---------------------------------------------------------------------------
# Luck & context
# ---------------------------------------------------------------------------
def event_luck(snap: Snapshot, now: datetime) -> float:
    mult = float(snap.settings.get("roll.global_luck_mult", 1.0) or 1.0)
    for ev in snap.active_events(now):
        if ev["type"] == "luck_multiplier":
            mult *= float((ev.get("params") or {}).get("mult", 1.0))
    return mult


def compute_luck(env: Env, *, biome_key: str, state_key: str | None, mods: RollModifiers, special: bool,
                 hidden: dict[str, Any] | None, roll_number: int, at: datetime) -> LuckBreakdown:
    snap = env.snap
    lb = LuckBreakdown()
    lb.base = max(float(env.user.base_luck), 0.0)
    lb.equipment = equipment_luck(env.equips, str(snap.settings.get("luck.equipment_mode", "additive"))) * passive_luck(
        env.equips, roll_number=roll_number, biome_key=biome_key, biome_state=state_key, now=at)
    biome = snap.biomes.get(biome_key) or snap.default_biome
    lb.biome = biome.luck_mult
    if state_key:
        st = next((s for s in biome.states if s.key == state_key), None)
        if st:
            lb.biome *= st.luck_mult
    lb.temporary = mods.temporary_luck
    if special:
        lb.special = float(snap.settings.get("roll.special_luck_mult", 1.2)) + passive_sum(env.equips, "special_bonus") + mods.special_extra
    if hidden:
        lb.special *= float(hidden.get("luck_mult", 1.0))
    lb.event = event_luck(snap, at)
    lb.other = mods.artifact_luck
    return lb


def special_for(env: Env, roll_number: int) -> tuple[bool, dict[str, Any] | None]:
    s = env.snap.settings
    delta = int(sum(float(p.get("delta", 0)) for e in env.equips for p in e.passives if p.get("type") == "special_interval"))
    return special_roll_info(roll_number, int(s.get("roll.special_interval", 10)), delta, list(s.get("roll.hidden_specials") or []))


def build_ctx(env: Env, *, biome_key: str, state_key: str | None, lb: LuckBreakdown, special: bool, hidden_key: str | None,
              mods: RollModifiers, at: datetime) -> RollContext:
    snap = env.snap
    item_mults = {snap.items_by_key[k].id: v for k, v in mods.item_mults.items() if k in snap.items_by_key}
    return RollContext(
        biome_key=biome_key, luck=lb.final, biome_state=state_key, level=env.user.level, special=special,
        hidden_special=hidden_key, now=at, active_events=frozenset(e["key"] for e in snap.active_events(at)),
        min_tier=max(mods.min_tier, mods.force_min_tier), flatten=mods.flatten, item_mults=item_mults, tier_mults=dict(mods.tier_mults),
    )


def cooldown_seconds(env: Env) -> float:
    s = env.snap.settings
    return roll_cooldown(base_seconds=float(s.get("roll.base_seconds", 1.0)), min_seconds=float(s.get("roll.min_seconds", 0.08)),
                         equips=env.equips, effects=env.effects, now=env.now)


def consume_in_memory(env: Env, mods: RollModifiers) -> None:
    """Mirror consumption onto the in-memory EffectData list (used by batches)."""
    keep = []
    for e in env.effects:
        if e.id in mods.consumed and e.remaining_rolls is not None:
            e.remaining_rolls -= mods.consumed[e.id]
        if e.id in mods.param_updates:
            e.params = mods.param_updates[e.id]
        if e.remaining_rolls is None or e.remaining_rolls > 0:
            keep.append(e)
    env.effects = keep


# ---------------------------------------------------------------------------
# Result resolution
# ---------------------------------------------------------------------------
@dataclass
class Won:
    item_id: int
    key: str
    info: dict[str, Any]
    tier: int
    odds: float
    final_chance: float
    kind: str
    first_discoverer_id: int | None
    slot_id: int | None = None
    is_new_item: bool = False


async def materialize_generated(db: AsyncSession, snap: Snapshot, slot: ItemDef, spec: GeneratedSpec) -> tuple[Item, bool]:
    row = (await db.execute(select(Item).where(Item.key == spec.key))).scalar_one_or_none()
    if row is not None:
        return row, False
    rar = snap.rarity_for_odds(spec.odds)
    new_id = (await db.execute(
        insert(Item).values(
            key=spec.key, name=spec.name, description=spec.description, kind="generated", rarity_key=rar.key, odds=spec.odds,
            rollable=False, sell_value=spec.sell_value, visual=spec.visual, procedural={"slot": slot.key, "parts": spec.parts},
            tradeable=True, sort_order=100000,
        ).on_conflict_do_nothing(index_elements=["key"]).returning(Item.id)
    )).scalar_one_or_none()
    row = await db.get(Item, new_id) if new_id else (await db.execute(select(Item).where(Item.key == spec.key))).scalar_one()
    return row, new_id is not None


async def resolve(db: AsyncSession, env: Env, item: ItemDef, table: CompiledTable | None) -> Won:
    snap = env.snap
    base_p = table.prob_of(item.id) if table is not None else 0.0
    if item.kind == "procedural_slot":
        spec = generate(item, snap, system_rng())
        row, created = await materialize_generated(db, snap, item, spec)
        info = inv_svc._item_row_public(row)  # noqa: SLF001
        return Won(item_id=row.id, key=row.key, info=info, tier=info["tier"], odds=float(row.odds or spec.odds),
                   final_chance=base_p * spec.combo_prob, kind="generated", first_discoverer_id=row.first_discoverer_id,
                   slot_id=item.id, is_new_item=created)
    return Won(item_id=item.id, key=item.key, info=item.public(snap.rarities), tier=item.tier, odds=float(item.odds or 1),
               final_chance=base_p, kind=item.kind, first_discoverer_id=item.first_discoverer_id)


def pick_index(table: CompiledTable, mods: RollModifiers, env: Env, *, u: float | None = None) -> int:
    rng = system_rng()
    idx = table.sample_index(rng.random() if u is None else u)
    for _ in range(max(0, mods.best_of - 1)):
        other = table.sample_index(rng.random())
        if (table.items[other].odds or 1) > (table.items[idx].odds or 1):
            idx = other
    return idx


# ---------------------------------------------------------------------------
# Granting (single item)
# ---------------------------------------------------------------------------
def auto_delete_decision(env: Env, won: Won, is_new: bool) -> str | None:
    """Returns 'delete' / 'sell' when the auto-delete filter removes the item, else None."""
    ad = env.settings.auto_delete
    if not ad.enabled or not users_svc.feature_unlocked(env.user, "auto_delete"):
        return None
    if won.item_id in env.favorites or won.kind in ("admin_artifact", "commemorative"):
        return None
    if is_new and ad.protect_new:
        return None
    if won.odds <= ad.max_odds or won.info.get("rarity") in ad.tiers:
        return ad.mode
    return None


def sell_price(env: Env, info: dict[str, Any], count: int = 1) -> int:
    mult = float(env.snap.settings.get("economy.sell_mult", 1.0) or 1.0) * (1.0 + env.sell_bonus)
    return int(round(int(info.get("sell_value", 0)) * mult)) * count


async def collection_upsert(db: AsyncSession, user_id: int, item_id: int, count: int) -> bool:
    """Record the item in the player's collection; True if this is their first one."""
    stmt = insert(Collection).values(user_id=user_id, item_id=item_id, times_obtained=count)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id", "item_id"],
                                      set_={"times_obtained": Collection.times_obtained + count})
    if is_sqlite():
        # No xmax, but SQLite has a single writer: this transaction already holds the
        # write lock, so a pre-check cannot race with another insert.
        existed = (await db.execute(
            select(Collection.user_id).where(Collection.user_id == user_id, Collection.item_id == item_id)
        )).first() is not None
        await db.execute(stmt)
        return not existed
    res = await db.execute(stmt.returning(literal_column("(xmax = 0)").label("inserted")))
    return bool(res.scalar_one())


async def try_first_discovery(db: AsyncSession, env: Env, won: Won) -> bool:
    if won.first_discoverer_id is not None:
        return False
    res = await db.execute(
        update(Item).where(Item.id == won.item_id, Item.first_discoverer_id.is_(None))
        .values(first_discoverer_id=env.user.id, first_discovered_at=env.now).returning(Item.id)
    )
    if res.scalar_one_or_none() is None:
        get_registry().mark_discovered(won.item_id, -1)
        return False
    get_registry().mark_discovered(won.item_id, env.user.id)
    return True


async def try_fastest(db: AsyncSession, env: Env, won: Won, roll_number: int) -> None:
    if won.tier < 2:
        return
    await db.execute(
        update(Item).where(Item.id == won.item_id, (Item.fastest_roll_count.is_(None)) | (Item.fastest_roll_count > roll_number))
        .values(fastest_user_id=env.user.id, fastest_roll_count=roll_number)
    )


def _record_rarity(env: Env, won: Won, count: int) -> None:
    rc = dict(env.stats.rarity_counts or {})
    rk = str(won.info.get("rarity"))
    rc[rk] = int(rc.get(rk, 0)) + count
    env.stats.rarity_counts = rc
    flag_modified(env.stats, "rarity_counts")
    if won.odds > env.stats.best_odds:
        env.stats.best_odds = won.odds
        env.stats.best_item_id = won.item_id


def _streak(env: Env, item_id: int) -> int:
    c = dict(env.stats.counters or {})
    if c.get("last_item") == item_id:
        c["streak"] = int(c.get("streak", 1)) + 1
    else:
        c["last_item"] = item_id
        c["streak"] = 1
    env.stats.counters = c
    flag_modified(env.stats, "counters")
    return int(c["streak"])


async def announce_first(db: AsyncSession, env: Env, won: Won, *, offline: bool = False) -> dict[str, Any]:
    snap = env.snap
    payload = {"item": won.info, "odds": won.odds, "discovered_at": env.now.isoformat(), "player": env.user.display_name,
               "player_id": env.user.id, "offline": offline}
    min_tier = 2 if won.kind != "generated" else TIER_BY_KEY.get(str(snap.settings.get("feed.procedural_first_min_tier", "legendary")), 4)
    if won.tier >= min_tier:
        await feed_svc.add_world_event(db, "first_discovery", env.user, payload, True)
        queue_event(db, "all", "first_discovery", {**payload, "user": users_svc.user_brief(env.user)})
    if won.tier >= 4:
        medal = snap.items_by_key.get("pioneer_starmedal")
        if medal:
            await inv_svc.create_instances(db, env.user.id, medal.id, 1, "achievement", tier=medal.tier,
                                           meta={"discovered_item": won.key, "discovered_name": won.info["name"]})
    return payload


async def announce_drop(db: AsyncSession, env: Env, won: Won, *, roll_luck: float, serial: int | None, first: bool,
                        biome_name: str, offline: bool = False, count: int = 1) -> None:
    snap = env.snap
    feed_min = TIER_BY_KEY.get(str(snap.settings.get("feed.min_tier", "legendary")), 4)
    if won.tier >= feed_min and not (first and won.tier >= 2):
        await feed_svc.add_world_event(db, "rare_drop", env.user, {
            "item": won.info, "odds": won.odds, "luck": roll_luck, "biome": biome_name, "serial": serial, "offline": offline,
            "count": count,
        }, env.settings.privacy.public_drops)
    await discord_notify.enqueue_drop(db, player=env.user.display_name, item=won.info, odds=won.odds, final_luck=roll_luck,
                                      biome=biome_name, first_discovery=first, obtained_at=env.now.isoformat(), serial=serial)


# ---------------------------------------------------------------------------
# Online single roll
# ---------------------------------------------------------------------------
def _offline_window(env: Env) -> tuple[datetime, datetime] | None:
    u = env.user
    s = env.snap.settings
    if not u.auto_roll_enabled or not s.get("features.offline_roll_enabled") or not s.get("features.auto_roll_enabled"):
        return None
    if u.last_roll_at is None:
        return None
    start = max(u.last_roll_at, u.offline_processed_until or u.last_roll_at, u.auto_roll_since or u.last_roll_at)
    if (env.now - start).total_seconds() < float(s.get("offline.min_gap_seconds", 45)):
        return None
    hours = float(s.get("offline.max_hours", 8))
    if "offline_processor_1" in env.unlocks:
        hours += 4
    if "offline_processor_2" in env.unlocks:
        hours += 8
    start = max(start, env.now - timedelta(hours=hours))
    return start, env.now


async def perform_roll(db: AsyncSession, user_id: int, *, auto: bool = False) -> dict[str, Any]:
    reg = get_registry()
    now = utcnow()
    user = await users_svc.lock_user(db, user_id)
    if user.status != "active":
        raise Forbidden("このアカウントでは現在Rollできません", code="account_restricted")
    if auto:
        if not user.auto_roll_enabled:
            raise AppError("Auto Rollが無効です", code="auto_roll_disabled", status_code=409)
    tolerance = timedelta(milliseconds=int(reg.setting("roll.tolerance_ms") or 0))
    offline_needed = bool(user.auto_roll_enabled and user.last_roll_at)
    record_from = None
    if offline_needed and user.last_roll_at:
        record_from = max(user.last_roll_at, now - timedelta(hours=24))
    if not offline_needed and user.next_roll_at and now + tolerance < user.next_roll_at:
        retry = (user.next_roll_at - now).total_seconds()
        raise RateLimited("クールダウン中です", code="cooldown", data={"retry_after_ms": int(retry * 1000)})
    env = await load_env(db, user, now, record_from=record_from)
    offline_summary = None
    window = _offline_window(env)
    if window:
        offline_summary = await process_offline(db, env, window)
    elif user.next_roll_at and now + tolerance < user.next_roll_at:
        retry = (user.next_roll_at - now).total_seconds()
        raise RateLimited("クールダウン中です", code="cooldown", data={"retry_after_ms": int(retry * 1000)})

    result = await _single_roll(db, env, auto=auto)
    cd = cooldown_seconds(env)
    user.next_roll_at = now + timedelta(seconds=cd)
    user.last_roll_at = now
    user.last_seen_at = now
    await _finish_progress(db, env)
    result["progress"] = env.progress.public()
    return {"roll": result, "offline": offline_summary, "state": await hud_state(db, env, cooldown=cd)}


async def _single_roll(db: AsyncSession, env: Env, *, auto: bool, flags_extra: int = 0) -> dict[str, Any]:
    snap = env.snap
    user = env.user
    st = env.biome.state
    roll_number = user.roll_counter + 1
    special, hidden = special_for(env, roll_number)
    mods = combine_effects(env.effects, now=env.now, biome_key=st.biome_key, special=special, rng=system_rng())
    lb = compute_luck(env, biome_key=st.biome_key, state_key=st.state_key, mods=mods, special=special, hidden=hidden,
                      roll_number=roll_number, at=env.now)
    ctx = build_ctx(env, biome_key=st.biome_key, state_key=st.state_key, lb=lb, special=special,
                    hidden_key=hidden["key"] if hidden else None, mods=mods, at=env.now)
    table = table_cache.get(snap, ctx)
    flags = flags_extra | (FLAG_AUTO if auto else 0) | (FLAG_SPECIAL if special else 0) | (FLAG_HIDDEN_SPECIAL if hidden else 0)

    forced_item = snap.items_by_key.get(mods.force_item_key) if mods.force_item_key else None
    if forced_item is not None and forced_item.kind not in ("admin_artifact",):
        chosen = forced_item
        flags |= FLAG_FORCED
    else:
        u = None
        if mods.preview_effect_id is not None:
            pe = next((e for e in env.effects if e.id == mods.preview_effect_id), None)
            if pe is not None and pe.params.get("u") is not None:
                u = float(pe.params["u"])
                flags |= FLAG_PREVIEW
                charges = int(pe.params.get("charges", 1)) - 1
                row = next(r for r in env.effect_rows if r.id == pe.id)
                if charges <= 0:
                    await db.delete(row)
                else:
                    row.params = {**(row.params or {}), "charges": charges, "u": system_rng().random()}
        chosen = table.items[pick_index(table, mods, env, u=u)]
    won = await resolve(db, env, chosen, table)
    tail = table.tail_of(chosen.id) if chosen.id in table.index else 1e-12
    if won.slot_id is not None:
        tail = max(1e-300, tail * won.final_chance / max(table.prob_of(won.slot_id), 1e-300))
    fort = fortune(tail)

    # --- consume effects -------------------------------------------------
    removed = await effects_svc.apply_consumption(db, env.effect_rows, mods.consumed, mods.param_updates)
    consume_in_memory(env, mods)
    env.effect_rows = [r for r in env.effect_rows if r.id not in removed]

    # --- auto delete / capacity -----------------------------------------
    coll_exists = await db.get(Collection, (user.id, won.item_id)) is not None
    is_new = not coll_exists
    decision = auto_delete_decision(env, won, is_new)
    auto_sold = 0
    overflow = False
    kept = decision is None
    if decision:
        flags |= FLAG_AUTO_DELETED
        env.stats.items_auto_deleted += 1
        if decision == "sell":
            auto_sold = sell_price(env, won.info)
            flags |= FLAG_AUTO_SOLD
    else:
        protect_tier = TIER_BY_KEY.get(str(snap.settings.get("inventory.overflow_protect_tier", "legendary")), 4)
        if won.tier < protect_tier and won.kind not in ("commemorative",):
            if await inv_svc.count_instances(db, user.id) >= await inv_svc.capacity(db, user.id):
                overflow = True
                kept = False
                auto_sold = sell_price(env, won.info)
                flags |= FLAG_OVERFLOW | FLAG_AUTO_SOLD

    first = False
    first_payload = None
    serial = None
    instance_ids: list[int] = []
    duplicated = 0
    new_collection = False
    if kept:
        new_collection = await collection_upsert(db, user.id, won.item_id, 1)
        if new_collection:
            if won.item_id in snap.collectible_ids:
                env.stats.discovered_count += 1
            env.events.append(ProgressEvent("discover", params={"item_key": won.key}))
            await try_fastest(db, env, won, roll_number)
        first = await try_first_discovery(db, env, won)
        if first:
            flags |= FLAG_FIRST_DISCOVERY
            env.stats.first_discoveries += 1
        count = 1
        if mods.duplicate_effect_id is not None:
            dup_row = next((r for r in env.effect_rows if r.id == mods.duplicate_effect_id), None)
            if dup_row is not None:
                count = 2
                duplicated = 1
                if dup_row.remaining_rolls is not None:
                    dup_row.remaining_rolls -= 1
                    if dup_row.remaining_rolls <= 0:
                        await db.delete(dup_row)
                        env.effect_rows.remove(dup_row)
                        env.effects = [e for e in env.effects if e.id != dup_row.id]
        env.stats.items_obtained += count
        streak = _streak(env, won.item_id)
        env.events.append(ProgressEvent("obtain", count=count, params={"tier": won.tier, "item_key": won.key, "streak": streak}))
    if auto_sold:
        user.stardust += auto_sold
        env.stats.stardust_earned += auto_sold
        env.stats.items_sold += 1
        env.events.append(ProgressEvent("earn", params={"amount": auto_sold}))

    # --- roll row ------------------------------------------------------------
    biome_def = snap.biomes.get(env.biome.state.biome_key) or snap.default_biome
    detail = None
    if won.tier >= 3 or flags & (FLAG_FORCED | FLAG_FIRST_DISCOVERY):
        detail = {"luck": lb.as_dict(), "effects": mods.applied_names, "table_version": snap.version}
    roll = Roll(
        user_id=user.id, roll_number=roll_number, item_id=won.item_id, tier=won.tier, biome_key=biome_def.key,
        biome_state=env.biome.state.state_key, luck=lb.final, base_odds=won.odds, final_chance=won.final_chance,
        equipment_mult=lb.equipment, boost_mult=lb.temporary, flags=flags, rng_version=RNG_VERSION,
        content_version=snap.version, detail=detail,
    )
    db.add(roll)
    await db.flush()
    if kept:
        instance_ids = await inv_svc.create_instances(
            db, user.id, won.item_id, 1 + duplicated, "roll", tier=won.tier, roll_id=roll.id,
            meta={"luck": round(lb.final, 4), "biome": biome_def.key} if won.tier >= 3 else None,
        )
        roll.instance_id = instance_ids[0]
        if won.tier >= 3:
            from ..models import ItemInstance

            serial = (await db.get(ItemInstance, instance_ids[0])).serial  # type: ignore[union-attr]
        if first:
            first_payload = await announce_first(db, env, won)
        if won.tier >= 4 or first:
            await announce_drop(db, env, won, roll_luck=lb.final, serial=serial, first=first, biome_name=biome_def.name)

    # --- stats / xp / season --------------------------------------------------
    user.roll_counter = roll_number
    env.stats.total_rolls += 1
    if special:
        env.stats.special_rolls += 1
    env.stats.max_luck = max(env.stats.max_luck, lb.final)
    _record_rarity(env, won, 1)
    rar = snap.rarities.get(str(won.info.get("rarity")))
    xp_gain = int(round((rar.xp if rar else 1) * (1 + env.xp_bonus) * mods.xp_mult))
    await progress_svc.add_xp(db, user, env.stats, xp_gain, env.progress)
    await seasons_svc.add_stats(db, user.id, rolls=1, points=rar.season_points if rar else 1, best_odds=won.odds,
                                first_discoveries=1 if first else 0)
    env.events.append(ProgressEvent("roll", params={
        "biome": biome_def.key, "special_count": 1 if special else 0,
        "hidden_specials": {hidden["key"]: 1} if hidden else {}, "luck": lb.final, "time": env.now,
    }))
    item_stats.add(won.item_id)
    metrics.add_rolls(1)

    reveal_info = None
    if env.reveal_rng:
        reveal_info = {"top": [{"name": it.name, "rarity": it.rarity_key, "p": p} for it, p in rng_engine.iter_top(table, 15)],
                       "table_size": len(table.items)}
    return {
        "id": roll.id, "number": roll_number, "item": {**won.info, "serial": serial}, "instance_ids": instance_ids,
        "odds": won.odds, "final_chance": won.final_chance, "final_odds": (1 / won.final_chance) if won.final_chance > 0 else None,
        "luck": lb.as_dict(), "biome": {"key": biome_def.key, "name": biome_def.name, "state": env.biome.state.state_key},
        "special": special, "hidden_special": {"key": hidden["key"], "name": hidden.get("name", hidden["key"])} if hidden else None,
        "effects_applied": mods.applied_names, "fortune": fort, "auto_deleted": bool(decision), "auto_delete_mode": decision,
        "auto_sold": auto_sold, "overflow": overflow, "first_discovery": first_payload, "new_collection": new_collection,
        "duplicated": duplicated, "forced": bool(flags & FLAG_FORCED), "best_of": mods.best_of, "preview_used": bool(flags & FLAG_PREVIEW),
        "xp": xp_gain, "cosmic_eye": reveal_info,
    }


async def _finish_progress(db: AsyncSession, env: Env) -> None:
    if env.events:
        res = await progress_svc.handle_events(db, env.user, env.stats, env.events)
        env.progress.merge(res)
        env.events = []


# ---------------------------------------------------------------------------
# Batches (offline auto roll, burst)
# ---------------------------------------------------------------------------
@dataclass
class BatchAgg:
    counts: dict[int, int] = field(default_factory=dict)            # item_id -> rolled count
    won: dict[int, Won] = field(default_factory=dict)
    notable: list[dict[str, Any]] = field(default_factory=list)      # per-roll records to log
    luck_max: float = 0.0
    specials: int = 0
    hidden: dict[str, int] = field(default_factory=dict)
    per_biome: dict[str, int] = field(default_factory=dict)
    rolls: int = 0


async def _add_won(db: AsyncSession, env: Env, agg: BatchAgg, item: ItemDef, table: CompiledTable, n: int, *, luck: float,
                   biome_key: str, state_key: str | None, roll_number: int, log_tier: int, flags: int) -> None:
    if item.kind == "procedural_slot":
        for i in range(n):
            won = await resolve(db, env, item, table)
            agg.counts[won.item_id] = agg.counts.get(won.item_id, 0) + 1
            agg.won[won.item_id] = won
            if won.tier >= log_tier:
                agg.notable.append({"item_id": won.item_id, "tier": won.tier, "odds": won.odds, "p": won.final_chance, "luck": luck,
                                    "biome": biome_key, "state": state_key, "number": roll_number + i, "flags": flags})
        return
    won = agg.won.get(item.id) or await resolve(db, env, item, table)
    agg.won[item.id] = won
    agg.counts[item.id] = agg.counts.get(item.id, 0) + n
    if won.tier >= log_tier:
        for i in range(min(n, 50)):
            agg.notable.append({"item_id": won.item_id, "tier": won.tier, "odds": won.odds, "p": table.prob_of(item.id), "luck": luck,
                                "biome": biome_key, "state": state_key, "number": roll_number + i, "flags": flags})


async def run_batch(db: AsyncSession, env: Env, segments: list[Segment], n_total: int, *, kind: str,
                    window: tuple[datetime, datetime]) -> dict[str, Any]:
    snap = env.snap
    user = env.user
    rng = system_rng()
    log_tier = TIER_BY_KEY.get(str(snap.settings.get("offline.log_min_tier", "epic")), 3)
    base_flag = FLAG_OFFLINE if kind == "offline" else FLAG_BURST
    agg = BatchAgg()
    total_secs = sum(s.seconds for s in segments) or 1.0
    # distribute rolls uniformly over time
    counts: list[int] = []
    acc = 0.0
    assigned = 0
    for s in segments:
        acc += s.seconds
        upto = int(math.floor(n_total * acc / total_secs + 1e-9))
        counts.append(max(0, upto - assigned))
        assigned = upto
    if counts:
        counts[-1] += n_total - assigned
    number = user.roll_counter
    individual_budget = MAX_INDIVIDUAL_BATCH_ROLLS
    interval_delta = int(sum(float(p.get("delta", 0)) for e in env.equips for p in e.passives if p.get("type") == "special_interval"))
    interval = max(2, int(snap.settings.get("roll.special_interval", 10)) + interval_delta)
    hidden_defs = [h for h in (snap.settings.get("roll.hidden_specials") or []) if isinstance(h, dict) and int(h.get("every", 0) or 0) > 1]

    for seg, cnt in zip(segments, counts):
        if cnt <= 0:
            continue
        agg.per_biome[seg.biome_key] = agg.per_biome.get(seg.biome_key, 0) + cnt
        seg_mid = seg.start + (seg.end - seg.start) / 2
        remaining = cnt
        while remaining > 0:
            has_roll_scoped = any(e.remaining_rolls is not None and e.applies_in(seg.biome_key) and e.active(seg_mid)
                                  and e.effect_type not in ("special_boost",) for e in env.effects)
            next_number = number + 1
            special, hidden = special_for(env, next_number)
            if (has_roll_scoped and individual_budget > 0) or hidden:
                individual_budget -= 1
                mods = combine_effects(env.effects, now=seg_mid, biome_key=seg.biome_key, special=special, rng=rng)
                lb = compute_luck(env, biome_key=seg.biome_key, state_key=seg.state_key, mods=mods, special=special, hidden=hidden,
                                  roll_number=next_number, at=seg_mid)
                ctx = build_ctx(env, biome_key=seg.biome_key, state_key=seg.state_key, lb=lb, special=special,
                                hidden_key=hidden["key"] if hidden else None, mods=mods, at=seg_mid)
                table = table_cache.get(snap, ctx)
                forced = snap.items_by_key.get(mods.force_item_key) if mods.force_item_key else None
                item = forced if (forced and forced.kind != "admin_artifact") else table.items[pick_index(table, mods, env)]
                removed = await effects_svc.apply_consumption(db, env.effect_rows, mods.consumed, mods.param_updates)
                consume_in_memory(env, mods)
                env.effect_rows = [r for r in env.effect_rows if r.id not in removed]
                flags = base_flag | (FLAG_SPECIAL if special else 0) | (FLAG_HIDDEN_SPECIAL if hidden else 0)
                await _add_won(db, env, agg, item, table, 1, luck=lb.final, biome_key=seg.biome_key, state_key=seg.state_key,
                               roll_number=next_number, log_tier=log_tier, flags=flags)
                agg.luck_max = max(agg.luck_max, lb.final)
                agg.specials += 1 if special else 0
                if hidden:
                    agg.hidden[hidden["key"]] = agg.hidden.get(hidden["key"], 0) + 1
                number += 1
                remaining -= 1
                continue
            # bulk chunk up to (but excluding) the next hidden special
            chunk = remaining
            for h in hidden_defs:
                every = int(h["every"])
                nxt = ((number // every) + 1) * every  # next multiple after `number`
                chunk = min(chunk, max(1, nxt - number - 1)) if nxt - number - 1 >= 1 else chunk
            a, b = number + 1, number + chunk  # inclusive range
            n_special = b // interval - (a - 1) // interval
            n_normal = chunk - n_special
            # Bulk portion: roll-scoped effects are excluded (they are consumed only
            # by individually simulated rolls above); time-based effects apply.
            bulk_effects = [e for e in env.effects if e.remaining_rolls is None]
            for is_special, n in ((False, n_normal), (True, n_special)):
                if n <= 0:
                    continue
                mods = combine_effects(bulk_effects, now=seg_mid, biome_key=seg.biome_key, special=is_special, rng=rng)
                lb = compute_luck(env, biome_key=seg.biome_key, state_key=seg.state_key, mods=mods, special=is_special, hidden=None,
                                  roll_number=a, at=seg_mid)
                ctx = build_ctx(env, biome_key=seg.biome_key, state_key=seg.state_key, lb=lb, special=is_special, hidden_key=None,
                                mods=mods, at=seg_mid)
                table = table_cache.get(snap, ctx)
                sampled = rng_engine.sample_many(table, n, rng)
                flags = base_flag | (FLAG_SPECIAL if is_special else 0)
                for idx, c in sampled.items():
                    await _add_won(db, env, agg, table.items[idx], table, c, luck=lb.final, biome_key=seg.biome_key,
                                   state_key=seg.state_key, roll_number=a, log_tier=log_tier, flags=flags)
                agg.luck_max = max(agg.luck_max, lb.final)
            agg.specials += n_special
            number += chunk
            remaining -= chunk
    agg.rolls = n_total
    return await _apply_batch(db, env, agg, kind=kind, window=window, segments=segments)


async def _apply_batch(db: AsyncSession, env: Env, agg: BatchAgg, *, kind: str, window: tuple[datetime, datetime],
                       segments: list[Segment]) -> dict[str, Any]:
    snap = env.snap
    user = env.user
    stats = env.stats
    reveal_tier = TIER_BY_KEY.get(str(snap.settings.get("offline.reveal_min_tier", "legendary")), 4)
    protect_tier = TIER_BY_KEY.get(str(snap.settings.get("inventory.overflow_protect_tier", "legendary")), 4)
    ids = list(agg.counts.keys())
    existing = {r[0] for r in (await db.execute(select(Collection.item_id).where(Collection.user_id == user.id,
                                                                                  Collection.item_id.in_(ids)))).all()} if ids else set()
    free = max(0, await inv_svc.capacity(db, user.id) - await inv_svc.count_instances(db, user.id))
    batch = RollBatch(user_id=user.id, kind=kind, window_start=window[0], window_end=window[1], roll_count=agg.rolls,
                      summary={}, rng_version=RNG_VERSION, content_version=snap.version)
    db.add(batch)
    await db.flush()

    totals = {"kept": 0, "deleted": 0, "sold": 0, "overflow": 0, "stardust": 0}
    xp = 0
    points = 0
    firsts = 0
    results: list[dict[str, Any]] = []
    first_payloads: list[dict[str, Any]] = []
    # rarest first so capacity is spent on the best items
    for item_id in sorted(ids, key=lambda i: -agg.won[i].odds):
        won = agg.won[item_id]
        c = agg.counts[item_id]
        rar = snap.rarities.get(str(won.info.get("rarity")))
        xp += (rar.xp if rar else 1) * c
        points += (rar.season_points if rar else 1) * c
        _record_rarity(env, won, c)
        item_stats.add(item_id, c)
        is_new = item_id not in existing
        decision = auto_delete_decision(env, won, is_new)
        kept = 0
        sold = 0
        if decision:
            totals["deleted"] += c
            stats.items_auto_deleted += c
            if decision == "sell":
                sold = c
        else:
            kept = c
            if won.tier < protect_tier and won.kind != "commemorative" and c > free:
                sold = c - free
                kept = free
                totals["overflow"] += sold
            free -= kept
        if sold:
            gain = sell_price(env, won.info, sold)
            totals["stardust"] += gain
            totals["sold"] += sold
        first = False
        if kept:
            totals["kept"] += kept
            new_coll = await collection_upsert(db, user.id, item_id, kept)
            if new_coll:
                if item_id in snap.collectible_ids:
                    stats.discovered_count += 1
                env.events.append(ProgressEvent("discover", params={"item_key": won.key}))
            first = await try_first_discovery(db, env, won)
            if first:
                firsts += 1
                stats.first_discoveries += 1
                first_payloads.append(await announce_first(db, env, won, offline=kind == "offline"))
            await inv_svc.create_instances(db, user.id, item_id, kept, "roll", tier=won.tier, meta={"batch": batch.id})
            stats.items_obtained += kept
            env.events.append(ProgressEvent("obtain", count=kept, params={"tier": won.tier, "item_key": won.key}))
            if won.tier >= 4 or first:
                await announce_drop(db, env, won, roll_luck=agg.luck_max, serial=None, first=first, biome_name=kind,
                                    offline=kind == "offline", count=kept)
        results.append({"item": won.info, "count": c, "kept": kept, "sold": sold, "deleted": c if decision else 0,
                        "first": first, "reveal": won.tier >= reveal_tier and kept > 0})

    for rec in agg.notable[:2000]:
        db.add(Roll(user_id=user.id, roll_number=rec["number"], item_id=rec["item_id"], tier=rec["tier"], biome_key=rec["biome"],
                    biome_state=rec["state"], luck=rec["luck"], base_odds=rec["odds"], final_chance=rec["p"], flags=rec["flags"],
                    rng_version=RNG_VERSION, content_version=snap.version, batch_id=batch.id))

    if totals["stardust"]:
        user.stardust += totals["stardust"]
        stats.stardust_earned += totals["stardust"]
        stats.items_sold += totals["sold"]
        env.events.append(ProgressEvent("earn", params={"amount": totals["stardust"]}))
    user.roll_counter += agg.rolls
    stats.total_rolls += agg.rolls
    if kind == "offline":
        stats.offline_rolls += agg.rolls
    stats.special_rolls += agg.specials
    stats.max_luck = max(stats.max_luck, agg.luck_max)
    await progress_svc.add_xp(db, user, stats, int(round(xp * (1 + env.xp_bonus))), env.progress)
    best = max((agg.won[i].odds for i in ids), default=0.0)
    await seasons_svc.add_stats(db, user.id, rolls=agg.rolls, points=points, best_odds=best, first_discoveries=firsts)
    for bkey, n in agg.per_biome.items():
        env.events.append(ProgressEvent("roll", count=n, params={"biome": bkey, "special_count": 0, "luck": agg.luck_max}))
    env.events.append(ProgressEvent("roll", count=0, params={"biome": "", "special_count": agg.specials, "hidden_specials": agg.hidden}))
    metrics.add_rolls(agg.rolls)

    results.sort(key=lambda r: (-r["item"]["tier"], -(r["item"]["odds"] or 0)))
    biomes_visited = sorted({s.biome_key for s in segments if s.biome_key != snap.default_biome.key})
    summary = {
        "kind": kind, "batch_id": batch.id, "rolls": agg.rolls, "window_start": window[0].isoformat(), "window_end": window[1].isoformat(),
        "seconds": (window[1] - window[0]).total_seconds(), "totals": totals, "specials": agg.specials, "luck_max": agg.luck_max,
        "results": results[:150], "distinct": len(results), "first_discoveries": first_payloads,
        "biomes": [{"key": k, "name": snap.biomes[k].name} for k in biomes_visited if k in snap.biomes],
    }
    batch.summary = {k: v for k, v in summary.items() if k != "first_discoveries"}
    return summary


async def process_offline(db: AsyncSession, env: Env, window: tuple[datetime, datetime]) -> dict[str, Any] | None:
    snap = env.snap
    start, end = window
    seconds = (end - start).total_seconds()
    eff = float(snap.settings.get("offline.efficiency", 0.6)) + passive_sum(env.equips, "offline_efficiency")
    eff = min(max(eff, 0.0), 1.0)
    cd = cooldown_seconds(env)
    n = int(min(int(snap.settings.get("offline.max_rolls", 40000)), math.floor(seconds * eff / max(cd, 0.01))))
    env.user.offline_processed_until = end
    if n <= 0:
        return None
    segs = [s for s in env.biome.result.segments if s.end > start]
    clipped = [Segment(s.biome_key, s.state_key, max(s.start, start), min(s.end, end)) for s in segs if min(s.end, end) > max(s.start, start)]
    if not clipped:
        st = env.biome.state
        clipped = [Segment(st.biome_key, st.state_key, start, end)]
    summary = await run_batch(db, env, clipped, n, kind="offline", window=window)
    summary["efficiency"] = eff
    env.user.last_roll_at = end
    return summary


async def burst_roll(db: AsyncSession, env: Env, count: int) -> dict[str, Any]:
    st = env.biome.state
    seg = Segment(st.biome_key, st.state_key, env.now, env.now + timedelta(seconds=1))
    return await run_batch(db, env, [seg], count, kind="burst", window=(env.now, env.now))


async def claim_offline(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """Process any pending offline auto-roll window without performing a new roll (called on app load)."""
    now = utcnow()
    user = await users_svc.lock_user(db, user_id)
    if user.status == "banned":
        raise Forbidden("アカウントが停止されています", code="account_restricted")
    record_from = max(user.last_roll_at, now - timedelta(hours=24)) if (user.auto_roll_enabled and user.last_roll_at) else None
    env = await load_env(db, user, now, record_from=record_from)
    summary = None
    window = _offline_window(env) if user.status == "active" else None
    if window:
        summary = await process_offline(db, env, window)
        user.next_roll_at = now
    user.last_seen_at = now
    await _finish_progress(db, env)
    return {"offline": summary, "progress": env.progress.public(), "state": await hud_state(db, env)}


async def set_auto_roll(db: AsyncSession, user_id: int, enabled: bool) -> dict[str, Any]:
    user = await users_svc.lock_user(db, user_id)
    reg = get_registry()
    if enabled:
        users_svc.require_feature(user, "auto_roll", "features.auto_roll_enabled")
    now = utcnow()
    user.auto_roll_enabled = enabled
    user.auto_roll_since = now if enabled else None
    user.offline_processed_until = now
    return {"auto_roll": enabled, "offline_enabled": bool(reg.setting("features.offline_roll_enabled"))}


# ---------------------------------------------------------------------------
# HUD state
# ---------------------------------------------------------------------------
def _alive(r: ActiveEffect, now: datetime) -> bool:
    from sqlalchemy import inspect as sa_inspect

    ins = sa_inspect(r)
    if ins.deleted or ins.was_deleted:
        return False
    if r.expires_at is not None and r.expires_at <= now:
        return False
    return r.remaining_rolls is None or r.remaining_rolls > 0


async def hud_state(db: AsyncSession, env: Env, cooldown: float | None = None) -> dict[str, Any]:
    snap = env.snap
    user = env.user
    st = env.biome.state
    next_number = user.roll_counter + 1
    special, hidden = special_for(env, next_number)
    mods = combine_effects(env.effects, now=env.now, biome_key=st.biome_key, special=special, rng=system_rng())
    lb = compute_luck(env, biome_key=st.biome_key, state_key=st.state_key, mods=mods, special=special, hidden=None,
                      roll_number=next_number, at=env.now)
    cd = cooldown if cooldown is not None else cooldown_seconds(env)
    interval = max(2, int(snap.settings.get("roll.special_interval", 10)) + int(
        sum(float(p.get("delta", 0)) for e in env.equips for p in e.passives if p.get("type") == "special_interval")))
    return {
        "stardust": user.stardust, "level": user.level, "xp": user.xp, "xp_level": users_svc.xp_for_level(user.level),
        "xp_next": users_svc.xp_for_level(user.level + 1), "roll_counter": user.roll_counter,
        "next_roll_at": user.next_roll_at.isoformat() if user.next_roll_at else None, "cooldown": cd,
        "server_time": utcnow().isoformat(), "luck": lb.as_dict(), "next_special": special,
        "special_in": (interval - (user.roll_counter % interval)) % interval or interval,
        "special_interval": interval, "roll_speed": 1.0 / max(cd, 1e-6),
        "effects": [effects_svc.public(r) for r in env.effect_rows if _alive(r, env.now)],
        "equipment": env.equip_visuals, "biome": biomes_svc.public_state(env.biome.row, reveal=env.reveal_rng),
        "auto_roll": user.auto_roll_enabled, "unlocked": users_svc.unlocked_features(user.level),
        "unlocks": sorted(env.unlocks), "reveal_rng": env.reveal_rng,
        "reveal_secrets": any(e.effect_type == "reveal_secrets" and e.active(env.now) for e in env.effects),
    }


async def get_state(db: AsyncSession, user_id: int) -> dict[str, Any]:
    now = utcnow()
    user = await users_svc.lock_user(db, user_id)
    env = await load_env(db, user, now)
    await _finish_progress(db, env)
    return await hud_state(db, env)


async def table_preview(db: AsyncSession, user_id: int, limit: int = 200) -> dict[str, Any]:
    """Exact final probabilities for the current state (RNG Analyzer unlock / Cosmic Eye)."""
    now = utcnow()
    user = await users_svc.lock_user(db, user_id)
    env = await load_env(db, user, now)
    if "rng_analyzer" not in env.unlocks and not env.reveal_rng:
        raise Forbidden("RNG Analyzerが必要です", code="analyzer_required")
    st = env.biome.state
    special, hidden = special_for(env, user.roll_counter + 1)
    mods = combine_effects(env.effects, now=now, biome_key=st.biome_key, special=special, rng=system_rng())
    lb = compute_luck(env, biome_key=st.biome_key, state_key=st.state_key, mods=mods, special=special, hidden=hidden,
                      roll_number=user.roll_counter + 1, at=now)
    ctx = build_ctx(env, biome_key=st.biome_key, state_key=st.state_key, lb=lb, special=special,
                    hidden_key=hidden["key"] if hidden else None, mods=mods, at=now)
    table = table_cache.get(env.snap, ctx)
    reveal_hidden = env.reveal_rng
    rows = []
    for it, p in zip(table.items, table.probs):
        if it.hidden and not reveal_hidden:
            continue
        rows.append({"key": it.key, "name": it.name, "rarity": it.rarity_key, "odds": it.odds, "p": p})
    await _finish_progress(db, env)
    return {"luck": lb.as_dict(), "biome": st.biome_key, "special": special, "items": rows[:limit], "count": len(rows)}
