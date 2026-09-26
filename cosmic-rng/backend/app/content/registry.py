"""In-memory content registry.

All game content (items, biomes, boosts, ...) and game settings are loaded
into an immutable snapshot so the roll hot path never touches the DB for
content. Each worker polls ``game_settings._content_version`` (and listens for
``content_changed`` notifications) and atomically swaps in a fresh snapshot
when admins change content. Temporary admin overrides (content_overrides) are
applied on top of the base rows while active.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.timeutil import utcnow
from .. import models as m
from .settings_schema import DEFAULTS

log = logging.getLogger("cosmic.registry")

CONTENT_VERSION_KEY = "_content_version"


@dataclass(slots=True, frozen=True)
class RarityDef:
    key: str
    name: str
    tier: int
    min_odds: float
    color: str
    color2: str
    luck_exponent: float
    xp: int
    season_points: int
    cutscene: str
    announce: bool
    name_ja: str = ""



@dataclass(slots=True)
class ItemDef:
    id: int
    key: str
    name: str
    description: str
    lore: str
    kind: str
    rarity_key: str
    tier: int
    odds: float | None
    display_odds: str | None
    rollable: bool
    sell_value: int
    biome_keys: frozenset[str]
    excluded_biome_keys: frozenset[str]
    min_luck: float | None
    conditions: dict[str, Any]
    luck_curve: dict[str, Any] | None
    visual: dict[str, Any]
    animation: str | None
    sound: str | None
    tradeable: bool
    hidden: bool
    procedural: dict[str, Any] | None
    is_active: bool
    sort_order: int
    first_discoverer_id: int | None = None
    name_ja: str = ""


    def public(self, rarities: dict[str, RarityDef]) -> dict[str, Any]:
        r = rarities.get(self.rarity_key)
        return {
            "id": self.id, "key": self.key, "name": self.name, "name_ja": self.name_ja,
            "description": self.description, "lore": self.lore,
            "kind": self.kind, "rarity": self.rarity_key, "tier": self.tier, "odds": self.odds,
            "display_odds": self.display_odds, "sell_value": self.sell_value, "visual": self.visual,
            "animation": self.animation or (r.cutscene if r else None), "sound": self.sound,
            "tradeable": self.tradeable, "biomes": sorted(self.biome_keys),
        }


@dataclass(slots=True)
class BiomeState:
    key: str
    name: str
    odds_per_sec: float
    duration_sec: int
    luck_mult: float
    description: str
    theme: dict[str, Any]


@dataclass(slots=True)
class BiomeDef:
    id: int
    key: str
    name: str
    description: str
    kind: str
    odds_per_sec: float | None
    duration_sec: int
    luck_mult: float
    min_level: int
    item_boosts: dict[str, float]
    theme: dict[str, Any]
    states: list[BiomeState]
    announce: bool
    hidden: bool
    sort_order: int
    is_active: bool
    name_ja: str = ""


    def public(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "name_ja": self.name_ja, "description": self.description, "kind": self.kind,
            "odds_per_sec": self.odds_per_sec, "duration_sec": self.duration_sec, "luck_mult": self.luck_mult,
            "min_level": self.min_level, "theme": self.theme, "hidden": self.hidden,
            "states": [{"key": s.key, "name": s.name, "luck_mult": s.luck_mult, "description": s.description, "theme": s.theme}
                       for s in self.states],
        }


@dataclass(slots=True)
class Snapshot:
    version: int
    loaded_at: float
    rarities: dict[str, RarityDef]
    tier_to_rarity: dict[int, RarityDef]
    items: dict[int, ItemDef]
    items_by_key: dict[str, ItemDef]
    rollable: list[ItemDef]  # sorted rarest first
    biomes: dict[str, BiomeDef]
    default_biome: BiomeDef
    natural_biomes: list[BiomeDef]
    boosts: dict[str, dict[str, Any]]
    boosts_by_id: dict[int, dict[str, Any]]
    equipment: dict[str, dict[str, Any]]
    equipment_by_id: dict[int, dict[str, Any]]
    recipes: dict[str, dict[str, Any]]
    shops: dict[str, dict[str, Any]]
    shop_items: dict[str, dict[str, Any]]
    shop_items_by_id: dict[int, dict[str, Any]]
    quests: dict[str, dict[str, Any]]
    quests_by_id: dict[int, dict[str, Any]]
    achievements: dict[str, dict[str, Any]]
    achievements_by_id: dict[int, dict[str, Any]]
    cosmetics: dict[str, dict[str, Any]]
    artifacts: dict[str, dict[str, Any]]
    artifacts_by_item: dict[int, dict[str, Any]]
    parts: dict[str, list[dict[str, Any]]]
    events: list[dict[str, Any]]
    settings: dict[str, Any]
    collectible_ids: frozenset[int] = field(default_factory=frozenset)

    def tier_of(self, rarity_key: str) -> int:
        r = self.rarities.get(rarity_key)
        return r.tier if r else 1

    def rarity_for_odds(self, odds: float) -> RarityDef:
        best = self.tier_to_rarity[1]
        for r in sorted(self.rarities.values(), key=lambda x: x.tier):
            if r.key == "admin":
                continue
            if odds >= r.min_odds:
                best = r
        return best

    def active_events(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        out = []
        for e in self.events:
            if not e["is_active"]:
                continue
            if e["starts_at"] and e["starts_at"] > now:
                continue
            if e["ends_at"] and e["ends_at"] <= now:
                continue
            out.append(e)
        return out


def _row_dict(obj: Any) -> dict[str, Any]:
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}


def _apply_overrides(rows: list[dict[str, Any]], overrides: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not overrides:
        return rows
    out = []
    for r in rows:
        patches = overrides.get(str(r.get("key")))
        if patches:
            r = dict(r)
            for p in patches:
                for k, v in p.items():
                    if k in r and k not in ("id", "key"):
                        r[k] = v
            r["_overridden"] = True
        out.append(r)
    return out


async def build_snapshot(db: AsyncSession) -> Snapshot:
    now = utcnow()
    settings_rows = (await db.execute(select(m.GameSetting))).scalars().all()
    settings = dict(DEFAULTS)
    version = 0
    for s in settings_rows:
        if s.key == CONTENT_VERSION_KEY:
            version = int(s.value or 0)
        else:
            settings[s.key] = s.value

    ov_rows = (
        await db.execute(
            select(m.ContentOverride).where(
                m.ContentOverride.active.is_(True), m.ContentOverride.starts_at <= now, m.ContentOverride.expires_at > now
            ).order_by(m.ContentOverride.id)
        )
    ).scalars().all()
    overrides: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for o in ov_rows:
        overrides.setdefault(o.entity_type, {}).setdefault(o.entity_key, []).append(o.patch)

    async def load(model: Any, etype: str) -> list[dict[str, Any]]:
        rows = [_row_dict(x) for x in (await db.execute(select(model))).scalars().all()]
        return _apply_overrides(rows, overrides.get(etype, {}))

    rar_rows = await load(m.Rarity, "rarity")
    rarities = {
        r["key"]: RarityDef(
            key=r["key"], name=r["name"], name_ja=r.get("name_ja") or "", tier=r["tier"], min_odds=r["min_odds"], color=r["color"], color2=r["color2"],
            luck_exponent=r["luck_exponent"], xp=r["xp"], season_points=r["season_points"], cutscene=r["cutscene"],
            announce=r["announce"],
        )
        for r in rar_rows
    }
    tier_to_rarity = {r.tier: r for r in rarities.values()}

    # Only fixed items are held in memory; procedurally generated items are
    # fetched on demand (they can be numerous).
    item_rows = [
        _row_dict(x)
        for x in (await db.execute(select(m.Item).where(m.Item.kind != "generated"))).scalars().all()
    ]
    item_rows = _apply_overrides(item_rows, overrides.get("item", {}))
    items: dict[int, ItemDef] = {}
    for r in item_rows:
        tier = rarities[r["rarity_key"]].tier if r["rarity_key"] in rarities else 1
        items[r["id"]] = ItemDef(
            id=r["id"], key=r["key"], name=r["name"], name_ja=r.get("name_ja") or "",
            description=r["description"], lore=r["lore"], kind=r["kind"],
            rarity_key=r["rarity_key"], tier=tier, odds=r["odds"], display_odds=r["display_odds"], rollable=r["rollable"],
            sell_value=r["sell_value"], biome_keys=frozenset(r["biome_keys"] or []),
            excluded_biome_keys=frozenset(r["excluded_biome_keys"] or []), min_luck=r["min_luck"],
            conditions=r["conditions"] or {}, luck_curve=r["luck_curve"], visual=r["visual"] or {}, animation=r["animation"],
            sound=r["sound"], tradeable=r["tradeable"], hidden=r["hidden"], procedural=r["procedural"],
            is_active=r["is_active"], sort_order=r["sort_order"], first_discoverer_id=r["first_discoverer_id"],
        )
    items_by_key = {i.key: i for i in items.values()}
    rollable = sorted(
        [i for i in items.values() if i.rollable and i.is_active and i.odds and i.kind in ("standard", "biome", "procedural_slot")],
        key=lambda i: (-(i.odds or 0), i.id),
    )

    biome_rows = await load(m.Biome, "biome")
    biomes: dict[str, BiomeDef] = {}
    for r in biome_rows:
        states = [
            BiomeState(
                key=s["key"], name=s.get("name", s["key"]), odds_per_sec=float(s.get("odds_per_sec", 60)),
                duration_sec=int(s.get("duration_sec", 20)), luck_mult=float(s.get("luck_mult", 1)),
                description=s.get("description", ""), theme=s.get("theme", {}),
            )
            for s in (r["special_states"] or [])
            if isinstance(s, dict) and s.get("key")
        ]
        biomes[r["key"]] = BiomeDef(
            id=r["id"], key=r["key"], name=r["name"], name_ja=r.get("name_ja") or "",
            description=r["description"], kind=r["kind"],
            odds_per_sec=r["odds_per_sec"], duration_sec=r["duration_sec"], luck_mult=r["luck_mult"],
            min_level=r["min_level"], item_boosts=r["item_boosts"] or {}, theme=r["theme"] or {}, states=states,
            announce=r["announce"], hidden=r["hidden"], sort_order=r["sort_order"], is_active=r["is_active"],
        )
    defaults = [b for b in biomes.values() if b.kind == "default" and b.is_active]
    if not defaults:
        raise RuntimeError("No default biome configured")
    default_biome = sorted(defaults, key=lambda b: b.sort_order)[0]
    natural = sorted(
        [b for b in biomes.values() if b.kind == "natural" and b.is_active and b.odds_per_sec], key=lambda b: -(b.odds_per_sec or 0)
    )

    boosts_l = await load(m.Boost, "boost")
    equip_l = await load(m.Equipment, "equipment")
    recipes_l = await load(m.Recipe, "recipe")
    shops_l = await load(m.Shop, "shop")
    shop_items_l = await load(m.ShopItem, "shop_item")
    quests_l = await load(m.Quest, "quest")
    ach_l = await load(m.Achievement, "achievement")
    cos_l = await load(m.Cosmetic, "cosmetic")
    art_l = await load(m.AdminArtifact, "artifact")
    parts_l = [_row_dict(x) for x in (await db.execute(select(m.ItemPart).where(m.ItemPart.is_active.is_(True)))).scalars().all()]
    events_l = [_row_dict(x) for x in (await db.execute(select(m.GameEvent))).scalars().all()]

    parts: dict[str, list[dict[str, Any]]] = {}
    for p in parts_l:
        parts.setdefault(p["part_type"], []).append(p)

    admin_biomes = {b.key for b in biomes.values() if b.kind == "admin"}
    collectible = frozenset(
        i.id for i in items.values()
        if i.kind in ("standard", "biome") and i.rollable and i.is_active and not (i.biome_keys and i.biome_keys <= admin_biomes)
    )

    return Snapshot(
        version=version,
        loaded_at=time.time(),
        rarities=rarities,
        tier_to_rarity=tier_to_rarity,
        items=items,
        items_by_key=items_by_key,
        rollable=rollable,
        biomes=biomes,
        default_biome=default_biome,
        natural_biomes=natural,
        boosts={b["key"]: b for b in boosts_l},
        boosts_by_id={b["id"]: b for b in boosts_l},
        equipment={e["key"]: e for e in equip_l},
        equipment_by_id={e["id"]: e for e in equip_l},
        recipes={r["key"]: r for r in recipes_l},
        shops={s["key"]: s for s in shops_l},
        shop_items={s["key"]: s for s in shop_items_l},
        shop_items_by_id={s["id"]: s for s in shop_items_l},
        quests={q["key"]: q for q in quests_l},
        quests_by_id={q["id"]: q for q in quests_l},
        achievements={a["key"]: a for a in ach_l},
        achievements_by_id={a["id"]: a for a in ach_l},
        cosmetics={c["key"]: c for c in cos_l},
        artifacts={a["key"]: a for a in art_l},
        artifacts_by_item={a["item_id"]: a for a in art_l},
        parts=parts,
        events=events_l,
        settings=settings,
        collectible_ids=collectible,
    )


class Registry:
    def __init__(self) -> None:
        self._snap: Snapshot | None = None
        self._lock = asyncio.Lock()
        self._last_check = 0.0
        self.check_interval = 2.0

    @property
    def snap(self) -> Snapshot:
        if self._snap is None:
            raise RuntimeError("Content registry not loaded")
        return self._snap

    @property
    def loaded(self) -> bool:
        return self._snap is not None

    def setting(self, key: str) -> Any:
        if self._snap is None:
            return DEFAULTS.get(key)
        return self._snap.settings.get(key, DEFAULTS.get(key))

    async def reload(self, db: AsyncSession) -> Snapshot:
        async with self._lock:
            snap = await build_snapshot(db)
            self._snap = snap
            self._last_check = time.monotonic()
            log.info("content snapshot loaded version=%s items=%s biomes=%s", snap.version, len(snap.items), len(snap.biomes))
            return snap

    async def ensure_fresh(self, db: AsyncSession, force: bool = False) -> Snapshot:
        if self._snap is None:
            return await self.reload(db)
        now = time.monotonic()
        if not force and now - self._last_check < self.check_interval:
            return self._snap
        self._last_check = now
        row = await db.get(m.GameSetting, CONTENT_VERSION_KEY)
        current = int(row.value) if row else 0
        if current != self._snap.version:
            return await self.reload(db)
        return self._snap

    def mark_discovered(self, item_id: int, user_id: int) -> None:
        if self._snap and item_id in self._snap.items:
            self._snap.items[item_id].first_discoverer_id = user_id


_registry = Registry()


def get_registry() -> Registry:
    return _registry


def snap() -> Snapshot:
    return _registry.snap


async def bump_content_version(db: AsyncSession, admin_id: int | None = None) -> int:
    """Increment the content version so every worker reloads its snapshot (caller commits)."""
    row = (
        await db.execute(select(m.GameSetting).where(m.GameSetting.key == CONTENT_VERSION_KEY).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        db.add(m.GameSetting(key=CONTENT_VERSION_KEY, value=1, updated_by=admin_id))
        return 1
    new = int(row.value or 0) + 1
    row.value = new
    row.updated_by = admin_id
    return new
