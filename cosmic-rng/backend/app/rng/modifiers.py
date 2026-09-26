"""Combination of equipment, passives and temporary effects into roll modifiers.

Pure functions (no DB access) so they are unit-testable. The roll service
loads rows, calls these helpers, then persists the returned consumption plan.

Stack modes for temporary effects of the same effect type:
  add       — values are summed (+1000% and +1000% → +2000%), all consumed
  multiply  — factors are multiplied ((1+10)×(1+10) = ×121), all consumed
  queue     — "stock": only the oldest instance of the same source applies and
              is consumed; the rest wait their turn
  highest   — "individual": only the strongest instance of the same source
              applies and is consumed
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .engine import RandomSource, time_condition_ok

TIER_BY_KEY = {"common": 1, "rare": 2, "epic": 3, "legendary": 4, "secret": 5, "ultra_secret": 6, "mythic": 7}

LUCK_TYPES = {"luck", "luck_mult", "compounding_luck", "random_luck"}


@dataclass(slots=True)
class EffectData:
    id: int
    effect_type: str
    value: float
    stack_mode: str = "add"
    remaining_rolls: int | None = None
    expires_at: datetime | None = None
    biome_keys: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    source_key: str = ""
    name: str = ""
    source_type: str = "boost"

    def active(self, now: datetime) -> bool:
        if self.expires_at is not None and self.expires_at <= now:
            return False
        if self.remaining_rolls is not None and self.remaining_rolls <= 0:
            return False
        return True

    def applies_in(self, biome_key: str) -> bool:
        return not self.biome_keys or biome_key in self.biome_keys

    @property
    def roll_scoped(self) -> bool:
        return self.remaining_rolls is not None


@dataclass(slots=True)
class RollModifiers:
    temporary_luck: float = 1.0
    artifact_luck: float = 1.0
    min_tier: int = 0
    special_extra: float = 0.0
    flatten: float = 1.0
    force_item_key: str | None = None
    force_min_tier: int = 0
    best_of: int = 1
    item_mults: dict[str, float] = field(default_factory=dict)
    tier_mults: dict[int, float] = field(default_factory=dict)
    xp_mult: float = 1.0
    preview_effect_id: int | None = None
    duplicate_effect_id: int | None = None
    consumed: dict[int, int] = field(default_factory=dict)  # effect id -> rolls consumed
    param_updates: dict[int, dict[str, Any]] = field(default_factory=dict)
    applied_names: list[str] = field(default_factory=list)


def _consume(mods: RollModifiers, e: EffectData) -> None:
    if e.roll_scoped:
        mods.consumed[e.id] = mods.consumed.get(e.id, 0) + 1
    if e.name and e.name not in mods.applied_names:
        mods.applied_names.append(e.name)


def _pick(group: list[EffectData], mode: str) -> list[EffectData]:
    if mode in ("add", "multiply"):
        return group
    if mode == "queue":
        return [min(group, key=lambda e: e.id)]
    # highest
    return [max(group, key=lambda e: (e.value, TIER_BY_KEY.get(str(e.params.get("tier")), 0), -e.id))]


def combine_effects(
    effects: list[EffectData], *, now: datetime, biome_key: str, special: bool, rng: RandomSource
) -> RollModifiers:
    mods = RollModifiers()
    applicable = [e for e in effects if e.active(now) and e.applies_in(biome_key)]

    # --- luck (percentage) boosts, respecting stack modes per source --------
    add_total = 0.0
    mult_total = 1.0
    groups: dict[tuple[str, str], list[EffectData]] = {}
    for e in applicable:
        if e.effect_type == "luck":
            groups.setdefault((e.stack_mode, e.source_key), []).append(e)
    for (mode, _src), group in groups.items():
        for e in _pick(group, mode):
            if mode == "add":
                add_total += e.value / 100.0
            else:
                mult_total *= 1.0 + e.value / 100.0
            _consume(mods, e)
    mods.temporary_luck = (1.0 + add_total) * mult_total

    # --- direct multipliers (artifacts / admin) ---------------------------
    for e in applicable:
        t = e.effect_type
        if t == "luck_mult":
            mods.artifact_luck *= max(e.value, 0.0)
            _consume(mods, e)
        elif t == "compounding_luck":
            count = int(e.params.get("count", 0))
            mods.artifact_luck *= (1.0 + e.value) ** count
            mods.param_updates[e.id] = {**e.params, "count": count + 1}
            _consume(mods, e)
        elif t == "random_luck":
            lo = max(float(e.params.get("min", 1.0)), 1e-9)
            hi = max(float(e.params.get("max", 1.0)), lo)
            mods.artifact_luck *= math.exp(rng.uniform(math.log(lo), math.log(hi)))
            _consume(mods, e)

    # --- rarity guarantees (highest wins, only that one consumed) --------
    guarantees = [e for e in applicable if e.effect_type in ("min_rarity", "target_min_rarity")]
    if guarantees:
        best = max(guarantees, key=lambda e: (TIER_BY_KEY.get(str(e.params.get("tier")), 0), -e.id))
        mods.min_tier = TIER_BY_KEY.get(str(best.params.get("tier")), 0)
        _consume(mods, best)

    # --- special roll enhancers: consumed only on special rolls ----------
    if special:
        sp = [e for e in applicable if e.effect_type == "special_boost"]
        by_src: dict[tuple[str, str], list[EffectData]] = {}
        for e in sp:
            by_src.setdefault((e.stack_mode, e.source_key), []).append(e)
        for (mode, _), group in by_src.items():
            for e in _pick(group, mode):
                mods.special_extra += e.value
                _consume(mods, e)

    # --- table manipulation -------------------------------------------------
    flats = [e for e in applicable if e.effect_type == "table_flatten"]
    if flats:
        mods.flatten = min(max(min(e.value for e in flats), 0.05), 1.0)
        for e in flats:
            _consume(mods, e)

    forced = sorted([e for e in applicable if e.effect_type == "force_item"], key=lambda e: e.id)
    if forced:
        e = forced[0]
        if e.params.get("item_key"):
            mods.force_item_key = str(e.params["item_key"])
        elif e.params.get("tier"):
            mods.force_min_tier = TIER_BY_KEY.get(str(e.params["tier"]), 0)
        _consume(mods, e)

    bos = [e for e in applicable if e.effect_type == "best_of"]
    if bos:
        mods.best_of = max(2, int(max(e.value for e in bos)))
        for e in bos:
            _consume(mods, e)

    for e in applicable:
        if e.effect_type == "item_chance" and e.params.get("item_key"):
            k = str(e.params["item_key"])
            mods.item_mults[k] = mods.item_mults.get(k, 1.0) * max(e.value, 0.0)
            _consume(mods, e)
        elif e.effect_type == "rarity_chance" and e.params.get("tier"):
            tier = TIER_BY_KEY.get(str(e.params["tier"]), 0)
            if tier:
                mods.tier_mults[tier] = mods.tier_mults.get(tier, 1.0) * max(e.value, 0.0)
                _consume(mods, e)
        elif e.effect_type == "xp_mult":
            mods.xp_mult *= max(e.value, 0.0)
            _consume(mods, e)

    previews = sorted([e for e in applicable if e.effect_type == "preview" and e.params.get("u") is not None], key=lambda e: e.id)
    if previews:
        mods.preview_effect_id = previews[0].id
    dups = sorted([e for e in applicable if e.effect_type == "duplicate"], key=lambda e: e.id)
    if dups:
        mods.duplicate_effect_id = dups[0].id
    return mods


# ---------------------------------------------------------------------------
# Equipment & passives
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EquipData:
    key: str
    slot: str
    luck_bonus: float
    speed_bonus: float
    passives: list[dict[str, Any]]
    name: str = ""


def equipment_luck(equips: list[EquipData], mode: str = "additive") -> float:
    if mode == "multiplicative":
        v = 1.0
        for e in equips:
            v *= 1.0 + e.luck_bonus
        return v
    return 1.0 + sum(e.luck_bonus for e in equips)


def passive_luck(equips: list[EquipData], *, roll_number: int, biome_key: str, biome_state: str | None, now: datetime) -> float:
    mult = 1.0
    for e in equips:
        for p in e.passives:
            t = p.get("type")
            if t == "biome_luck" and p.get("biome") == biome_key:
                mult *= float(p.get("mult", 1))
            elif t == "nth_roll_luck":
                every = int(p.get("every", 0))
                if every > 0 and roll_number % every == 0:
                    mult *= float(p.get("mult", 1))
            elif t == "state_luck" and biome_state:
                mult *= float(p.get("mult", 1))
            elif t == "night_luck" and time_condition_ok({"tz": p.get("tz", "UTC"), "hours": p.get("hours", [0, 0])}, now):
                mult *= float(p.get("mult", 1))
            elif t == "luck_mult":
                mult *= float(p.get("mult", 1))
    return mult


def passive_sum(equips: list[EquipData], ptype: str, field_name: str = "add") -> float:
    return sum(float(p.get(field_name, 0)) for e in equips for p in e.passives if p.get("type") == ptype)


def passive_product(equips: list[EquipData], ptype: str, field_name: str = "mult") -> float:
    v = 1.0
    for e in equips:
        for p in e.passives:
            if p.get("type") == ptype:
                v *= float(p.get(field_name, 1))
    return v


def roll_cooldown(
    *, base_seconds: float, min_seconds: float, equips: list[EquipData], effects: list[EffectData], now: datetime
) -> float:
    speed = 1.0 + sum(e.speed_bonus for e in equips)
    factor = 1.0
    add_pct = 0.0
    for e in effects:
        if not e.active(now):
            continue
        if e.effect_type == "roll_speed":
            if e.stack_mode == "add":
                add_pct += e.value / 100.0
            else:
                speed *= 1.0 + e.value / 100.0
        elif e.effect_type == "cooldown_mult":
            factor *= max(e.value, 0.0)
    speed *= 1.0 + add_pct
    cd = base_seconds / max(speed, 0.01) * factor
    return max(min_seconds, cd)


def special_roll_info(
    roll_number: int, interval: int, interval_delta: int, hidden_specials: list[dict[str, Any]]
) -> tuple[bool, dict[str, Any] | None]:
    iv = max(2, interval + interval_delta)
    special = roll_number % iv == 0
    hidden = None
    for hs in sorted([h for h in hidden_specials if isinstance(h, dict)], key=lambda h: -int(h.get("every", 0) or 0)):
        every = int(hs.get("every", 0) or 0)
        if every > 1 and roll_number % every == 0:
            hidden = hs
            break
    return special, hidden
