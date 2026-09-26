"""Idempotent content seeding.

By default only *missing* content (by key) is inserted, so admin edits made in
the panel are never overwritten by a redeploy. ``force=True`` resets seeded
rows back to the defaults shipped with the code.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models as m
from ..core.timeutil import utcnow
from . import seed_artifacts, seed_items, seed_names_ja, seed_progress, seed_world
from .registry import CONTENT_VERSION_KEY, bump_content_version

log = logging.getLogger("cosmic.seed")


def _clean(model: Any, data: dict[str, Any]) -> dict[str, Any]:
    cols = {c.key for c in model.__table__.columns}
    out = {k: v for k, v in data.items() if k in cols and k != "id"}
    if "name_ja" in cols and not out.get("name_ja"):
        ja = seed_names_ja.BY_TABLE.get(model.__tablename__, {}).get(data.get("key", ""))
        if ja:
            out["name_ja"] = ja
    return out


async def backfill_names_ja(db: AsyncSession) -> int:
    """Fill in Japanese names for rows seeded before the column existed.

    Only touches rows that have none, so a name edited in the admin panel is
    never overwritten.
    """
    filled = 0
    for model in (m.Rarity, m.Item, m.Biome, m.Equipment, m.Boost, m.Recipe, m.Shop,
                  m.ShopItem, m.Cosmetic, m.Quest, m.Achievement, m.Season, m.ItemPart):
        table = model.__tablename__
        names = seed_names_ja.BY_TABLE.get(table)
        if not names:
            continue
        for obj in (await db.execute(select(model).where(model.name_ja == ""))).scalars().all():
            ja = names.get(obj.key)
            if ja:
                obj.name_ja = ja
                filled += 1
    if filled:
        await db.commit()
        log.info("backfilled %s Japanese names", filled)
    return filled


async def seed_new_content(db: AsyncSession) -> None:
    """Insert content added by an upgrade into a database that already has rows.

    ``seed`` is insert-only unless forced, but it also bumps the content
    version and rewrites nothing, so on a live database it is only worth the
    round trip when something really is missing. The check below is cheap:
    one count per table that the shipped content can grow.
    """
    checks: list[tuple[Any, list[dict[str, Any]]]] = [
        (m.Item, seed_items.all_items()),
        (m.Equipment, seed_world.EQUIPMENT),
        (m.Boost, seed_world.BOOSTS),
        (m.Recipe, seed_world.RECIPES),
        (m.Shop, seed_world.SHOPS),
        (m.ShopItem, seed_world.SHOP_ITEMS),
        (m.Cosmetic, seed_world.COSMETICS),
        (m.Quest, seed_progress.QUESTS),
        (m.Achievement, seed_progress.ACHIEVEMENTS),
        (m.Biome, seed_world.BIOMES),
    ]
    missing = 0
    for model, rows in checks:
        have = {k for (k,) in (await db.execute(select(model.key))).all()}
        missing += sum(1 for r in rows if r["key"] not in have)
    if not missing:
        return
    log.info("upgrade: %s content rows are new — seeding them", missing)
    await seed(db)


async def backfill_lore(db: AsyncSession) -> int:
    """Give upper-tier items their lore on databases seeded before it existed.

    Only rows with no lore at all are touched, so admin-written text stays.
    """
    filled = 0
    for obj in (await db.execute(select(m.Item).where(m.Item.lore == ""))).scalars().all():
        lore = seed_items.LORE_JA.get(obj.key)
        if lore:
            obj.lore = lore
            filled += 1
    if filled:
        await db.commit()
        log.info("backfilled lore for %s items", filled)
    return filled


async def _upsert_by_key(db: AsyncSession, model: Any, rows: list[dict[str, Any]], force: bool, key_field: str = "key") -> dict[str, Any]:
    existing = {getattr(o, key_field): o for o in (await db.execute(select(model))).scalars().all()}
    out: dict[str, Any] = {}
    created = 0
    for r in rows:
        data = _clean(model, r)
        obj = existing.get(r[key_field])
        if obj is None:
            obj = model(**data)
            db.add(obj)
            created += 1
        elif force:
            for k, v in data.items():
                setattr(obj, k, v)
        out[r[key_field]] = obj
    await db.flush()
    if created:
        log.info("seeded %s new %s", created, model.__tablename__)
    return out


async def seed(db: AsyncSession, force: bool = False) -> None:
    # Rarities use explicit ids
    existing_r = {r.key: r for r in (await db.execute(select(m.Rarity))).scalars().all()}
    for r in seed_items.RARITIES:
        obj = existing_r.get(r["key"])
        if obj is None:
            db.add(m.Rarity(**r))
        elif force:
            for k, v in r.items():
                setattr(obj, k, v)
    await db.flush()

    await _upsert_by_key(db, m.Item, seed_items.all_items(), force)

    # Item parts are keyed by (part_type, key)
    existing_p = {(p.part_type, p.key): p for p in (await db.execute(select(m.ItemPart))).scalars().all()}
    for p in seed_items.ITEM_PARTS:
        obj = existing_p.get((p["part_type"], p["key"]))
        if obj is None:
            db.add(m.ItemPart(**p))
        elif force:
            for k, v in p.items():
                setattr(obj, k, v)
    await db.flush()

    await _upsert_by_key(db, m.Biome, seed_world.BIOMES, force)
    await _upsert_by_key(db, m.Equipment, seed_world.EQUIPMENT, force)
    await _upsert_by_key(db, m.Boost, seed_world.BOOSTS, force)
    await _upsert_by_key(db, m.Recipe, seed_world.RECIPES, force)
    await _upsert_by_key(db, m.Shop, seed_world.SHOPS, force)
    await _upsert_by_key(db, m.ShopItem, seed_world.SHOP_ITEMS, force)
    await _upsert_by_key(db, m.Cosmetic, seed_world.COSMETICS, force)
    await _upsert_by_key(db, m.Quest, seed_progress.QUESTS, force)
    await _upsert_by_key(db, m.Achievement, seed_progress.ACHIEVEMENTS, force)

    # Admin artifacts: item row first, then artifact row referencing it
    art_items = await _upsert_by_key(db, m.Item, [{**a["item"], "sort_order": 10000 + a["sort_order"]} for a in seed_artifacts.ARTIFACTS], force)
    art_rows = []
    for a in seed_artifacts.ARTIFACTS:
        row = {k: v for k, v in a.items() if k != "item"}
        row["item_id"] = art_items[a["item"]["key"]].id
        art_rows.append(row)
    await _upsert_by_key(db, m.AdminArtifact, art_rows, force)

    if (await db.execute(select(m.Season).limit(1))).scalar_one_or_none() is None:
        now = utcnow()
        db.add(m.Season(key="season_1", name="Season 1: First Light", description="最初のシーズン。",
                        starts_at=now, ends_at=now + timedelta(days=60), status="active"))

    if await db.get(m.GameSetting, CONTENT_VERSION_KEY) is None:
        db.add(m.GameSetting(key=CONTENT_VERSION_KEY, value=1))
    else:
        await bump_content_version(db)
    await db.commit()
    log.info("content seed complete (force=%s)", force)
