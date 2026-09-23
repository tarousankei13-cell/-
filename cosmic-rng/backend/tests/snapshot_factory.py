"""Build a content Snapshot from seed data without a database (for pure unit tests)."""
from __future__ import annotations

import time
from typing import Any

from app.content import seed_artifacts, seed_items, seed_progress, seed_world
from app.content.registry import BiomeDef, BiomeState, ItemDef, RarityDef, Snapshot
from app.content.settings_schema import DEFAULTS


def make_snapshot(settings: dict[str, Any] | None = None, version: int = 1) -> Snapshot:
    rarities = {r["key"]: RarityDef(**{k: r[k] for k in (
        "key", "name", "tier", "min_odds", "color", "color2", "luck_exponent", "xp", "season_points", "cutscene", "announce")})
        for r in seed_items.RARITIES}
    items: dict[int, ItemDef] = {}
    all_items = seed_items.all_items() + [dict(a["item"], sort_order=9999) for a in seed_artifacts.ARTIFACTS]
    for idx, r in enumerate(all_items, start=1):
        items[idx] = ItemDef(
            id=idx, key=r["key"], name=r["name"], description=r.get("description", ""), lore=r.get("lore", ""),
            kind=r.get("kind", "standard"), rarity_key=r["rarity_key"], tier=rarities[r["rarity_key"]].tier,
            odds=r.get("odds"), display_odds=r.get("display_odds"), rollable=r.get("rollable", True),
            sell_value=r.get("sell_value", 0), biome_keys=frozenset(r.get("biome_keys") or []),
            excluded_biome_keys=frozenset(r.get("excluded_biome_keys") or []), min_luck=r.get("min_luck"),
            conditions=r.get("conditions") or {}, luck_curve=r.get("luck_curve"), visual=r.get("visual") or {},
            animation=r.get("animation"), sound=r.get("sound"), tradeable=r.get("tradeable", True), hidden=r.get("hidden", False),
            procedural=r.get("procedural"), is_active=True, sort_order=r.get("sort_order", 0),
        )
    by_key = {i.key: i for i in items.values()}
    rollable = sorted([i for i in items.values() if i.rollable and i.odds and i.kind in ("standard", "biome", "procedural_slot")],
                      key=lambda i: (-(i.odds or 0), i.id))
    biomes: dict[str, BiomeDef] = {}
    for idx, b in enumerate(seed_world.BIOMES, start=1):
        biomes[b["key"]] = BiomeDef(
            id=idx, key=b["key"], name=b["name"], description=b.get("description", ""), kind=b["kind"],
            odds_per_sec=b.get("odds_per_sec"), duration_sec=b.get("duration_sec", 60), luck_mult=b.get("luck_mult", 1.0),
            min_level=b.get("min_level", 1), item_boosts=b.get("item_boosts", {}), theme=b.get("theme", {}),
            states=[BiomeState(key=s["key"], name=s["name"], odds_per_sec=s["odds_per_sec"], duration_sec=s["duration_sec"],
                               luck_mult=s["luck_mult"], description=s.get("description", ""), theme=s.get("theme", {}))
                    for s in b.get("special_states", [])],
            announce=b.get("announce", False), hidden=b.get("hidden", False), sort_order=b["sort_order"], is_active=True,
        )
    default = next(b for b in biomes.values() if b.kind == "default")
    natural = sorted([b for b in biomes.values() if b.kind == "natural"], key=lambda b: -(b.odds_per_sec or 0))
    parts: dict[str, list[dict[str, Any]]] = {}
    for idx, p in enumerate(seed_items.ITEM_PARTS, start=1):
        parts.setdefault(p["part_type"], []).append({**p, "id": idx})
    boosts = {b["key"]: {**b, "id": i} for i, b in enumerate(seed_world.BOOSTS, start=1)}
    equipment = {e["key"]: {**e, "id": i} for i, e in enumerate(seed_world.EQUIPMENT, start=1)}
    st = dict(DEFAULTS)
    st.update(settings or {})
    return Snapshot(
        version=version, loaded_at=time.time(), rarities=rarities, tier_to_rarity={r.tier: r for r in rarities.values()},
        items=items, items_by_key=by_key, rollable=rollable, biomes=biomes, default_biome=default, natural_biomes=natural,
        boosts=boosts, boosts_by_id={b["id"]: b for b in boosts.values()}, equipment=equipment,
        equipment_by_id={e["id"]: e for e in equipment.values()}, recipes={r["key"]: r for r in seed_world.RECIPES},
        shops={s["key"]: s for s in seed_world.SHOPS}, shop_items={s["key"]: s for s in seed_world.SHOP_ITEMS},
        shop_items_by_id={}, quests={q["key"]: q for q in seed_progress.QUESTS}, quests_by_id={},
        achievements={a["key"]: a for a in seed_progress.ACHIEVEMENTS}, achievements_by_id={},
        cosmetics={c["key"]: c for c in seed_world.COSMETICS}, artifacts={}, artifacts_by_item={}, parts=parts, events=[],
        settings=st, collectible_ids=frozenset(i.id for i in items.values() if i.kind in ("standard", "biome")),
    )
