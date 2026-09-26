"""Profiles, showcase, cosmetics and the collection book."""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, Forbidden, NotFound
from ..models import (
    Achievement,
    Collection,
    Item,
    ItemInstance,
    Showcase,
    User,
    UserAchievement,
    UserCosmetic,
    UserStats,
)
from . import equipment as equipment_svc
from . import inventory as inv_svc
from . import user_settings as settings_svc
from .users import avatar_url, user_brief


async def cosmetics_of(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    snap = get_registry().snap
    rows = (await db.execute(select(UserCosmetic).where(UserCosmetic.user_id == user_id))).scalars().all()
    out = []
    for r in rows:
        c = snap.cosmetics.get(r.cosmetic_key)
        if c:
            out.append({"key": c["key"], "kind": c["kind"], "name": c["name"], "rarity": c["rarity_key"], "visual": c["visual"],
                        "description": c["description"], "obtained_at": r.obtained_at.isoformat()})
    return out


async def showcase_of(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Showcase, ItemInstance).join(ItemInstance, ItemInstance.id == Showcase.instance_id)
                             .where(Showcase.user_id == user_id, ItemInstance.owner_id == user_id).order_by(Showcase.slot))).all()
    infos = await inv_svc.items_info(db, {i.item_id for _, i in rows})
    return [{"slot": s.slot, "instance": inv_svc.instance_public(i, infos.get(i.item_id))} for s, i in rows]


async def get_profile(db: AsyncSession, user_id: int, viewer_id: int | None, viewer_admin: bool = False) -> dict[str, Any]:
    snap = get_registry().snap
    user = await db.get(User, user_id)
    if user is None or (user.status == "banned" and not viewer_admin):
        raise NotFound("プロフィールが見つかりません")
    settings = await settings_svc.load(db, user.id)
    own = viewer_id == user.id
    public = settings.privacy.public_profile or own or viewer_admin
    stats = await db.get(UserStats, user.id)
    cos = await cosmetics_of(db, user.id)
    title = snap.cosmetics.get(user.title_key) if user.title_key else None
    base = {
        "user": {**user_brief(user), "avatar": avatar_url(user), "created_at": user.created_at.isoformat(), "bio": user.bio,
                 "title_name": title["name"] if title else None, "title_rarity": title["rarity_key"] if title else None,
                 "background": user.profile_background or "bg_default",
                 "badges": [c for c in cos if c["kind"] == "badge" and c["key"] in (user.badge_keys or [])]},
        "own": own, "private": not public,
    }
    if not public:
        return base
    best = snap.items.get(stats.best_item_id) if stats and stats.best_item_id else None
    if stats and stats.best_item_id and best is None:
        info = await inv_svc.item_info(db, stats.best_item_id)
    else:
        info = best.public(snap.rarities) if best else None
    ach_rows = (await db.execute(select(UserAchievement, Achievement).join(Achievement, Achievement.id == UserAchievement.achievement_id)
                                 .where(UserAchievement.user_id == user.id).order_by(UserAchievement.achieved_at.desc()))).all()
    total = len(snap.collectible_ids) or 1
    _, equip_visuals = await equipment_svc.load_equipped(db, user.id)
    return {
        **base,
        "stats": {
            "total_rolls": stats.total_rolls if stats else 0, "best_odds": stats.best_odds if stats else 0, "best_item": info,
            "discovered": stats.discovered_count if stats else 0, "collection_rate": (stats.discovered_count / total) if stats else 0,
            "assets": stats.net_worth if stats else 0, "stardust": user.stardust, "first_discoveries": stats.first_discoveries if stats else 0,
            "achievements": stats.achievements_count if stats else 0, "level": user.level, "special_rolls": stats.special_rolls if stats else 0,
            "rarity_counts": stats.rarity_counts if stats else {},
        },
        "equipment": equip_visuals,
        "showcase": await showcase_of(db, user.id),
        "achievements": [{"key": a.key, "name": a.name, "tier": a.tier, "category": a.category, "world_first": ua.world_first,
                          "achieved_at": ua.achieved_at.isoformat()} for ua, a in ach_rows[:60]],
        "titles": [c for c in cos if c["kind"] == "title"],
        "badges_all": [c for c in cos if c["kind"] == "badge"],
        "backgrounds": [c for c in cos if c["kind"] == "background"] if own else [],
    }


async def update_profile(db: AsyncSession, user: User, *, title_key: str | None = None, background: str | None = None,
                         badges: list[str] | None = None, bio: str | None = None, clear_title: bool = False) -> None:
    owned = {c["key"]: c for c in await cosmetics_of(db, user.id)}
    if clear_title:
        user.title_key = None
    elif title_key is not None:
        if owned.get(title_key, {}).get("kind") != "title":
            raise Forbidden("未所持の称号です", code="not_owned")
        user.title_key = title_key
    if background is not None:
        if owned.get(background, {}).get("kind") != "background":
            raise Forbidden("未所持の背景です", code="not_owned")
        user.profile_background = background
    if badges is not None:
        if len(badges) > 5 or any(owned.get(b, {}).get("kind") != "badge" for b in badges):
            raise AppError("バッジの指定が不正です（最大5個）", code="invalid_badges")
        user.badge_keys = list(dict.fromkeys(badges))
    if bio is not None:
        user.bio = bio.strip()[:200] or None


async def set_showcase(db: AsyncSession, user_id: int, instance_ids: list[int | None]) -> list[dict[str, Any]]:
    if len(instance_ids) > 6:
        raise AppError("Showcaseは最大6個です", code="too_many")
    ids = [i for i in instance_ids if i]
    if len(set(ids)) != len(ids):
        raise AppError("同じアイテムは複数配置できません", code="duplicate")
    if ids:
        await inv_svc.get_owned_instances(db, user_id, ids, lock=False)
    await db.execute(delete(Showcase).where(Showcase.user_id == user_id))
    for slot, iid in enumerate(instance_ids):
        if iid:
            db.add(Showcase(user_id=user_id, slot=slot, instance_id=iid))
    await db.flush()
    return await showcase_of(db, user_id)


async def collection_book(db: AsyncSession, user_id: int, reveal_hidden: bool = False) -> dict[str, Any]:
    snap = get_registry().snap
    coll = {r.item_id: r for r in (await db.execute(select(Collection).where(Collection.user_id == user_id))).scalars().all()}
    owned = {r[0]: int(r[1]) for r in (await db.execute(select(ItemInstance.item_id, func.count()).where(ItemInstance.owner_id == user_id)
                                                        .group_by(ItemInstance.item_id))).all()}
    item_rows = {i.id: i for i in (await db.execute(select(Item).where(Item.id.in_(list(snap.collectible_ids) or [0])))).scalars().all()}
    discoverer_ids = {i.first_discoverer_id for i in item_rows.values() if i.first_discoverer_id}
    discoverers = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(discoverer_ids)))).scalars().all()} if discoverer_ids else {}
    entries = []
    for it in sorted([snap.items[i] for i in snap.collectible_ids], key=lambda x: (x.tier, x.odds or 0)):
        c = coll.get(it.id)
        row = item_rows.get(it.id)
        state = "owned" if owned.get(it.id) else ("discovered" if c else "undiscovered")
        biomes = sorted(it.biome_keys)
        if state == "undiscovered":
            entries.append({
                "id": it.id, "state": state, "rarity": it.rarity_key, "tier": it.tier,
                "name": it.name if reveal_hidden else ("???" if (it.hidden or it.tier >= 5) else _mask(it.name)),
                "name_ja": it.name_ja if reveal_hidden else "",
                "odds": None if (it.hidden and not reveal_hidden) else it.odds, "display_odds": it.display_odds,
                "biomes": biomes if not it.hidden or reveal_hidden else [], "visual": {"shape": it.visual.get("shape"), "silhouette": True},
                "hint": _hint(it) if (it.hidden or reveal_hidden) else None,
                "discovered_by_world": bool(row and row.first_discoverer_id),
            })
            continue
        first = discoverers.get(row.first_discoverer_id) if row and row.first_discoverer_id else None
        entries.append({
            "id": it.id, "state": state, "key": it.key, "name": it.name, "name_ja": it.name_ja,
            "description": it.description, "lore": it.lore,
            "rarity": it.rarity_key, "tier": it.tier, "odds": it.odds, "display_odds": it.display_odds, "visual": it.visual,
            "biomes": biomes, "owned": owned.get(it.id, 0), "times_obtained": c.times_obtained if c else 0,
            "first_obtained_at": c.first_obtained_at.isoformat() if c else None,
            "world": {"discovery_count": row.discovery_count if row else 0, "owner_count": row.owner_count if row else 0,
                      "trade_count": row.trade_count if row else 0,
                      "first_discoverer": user_brief(first) if first else None,
                      "first_discovered_at": row.first_discovered_at.isoformat() if row and row.first_discovered_at else None},
        })
    generated = (await db.execute(select(Item, Collection).join(Collection, Collection.item_id == Item.id)
                                  .where(Collection.user_id == user_id, Item.kind == "generated").order_by(Item.odds.desc()).limit(200))).all()
    gen = [{**inv_svc._item_row_public(i), "times_obtained": c.times_obtained, "owned": owned.get(i.id, 0),  # noqa: SLF001
            "first_discoverer_is_me": i.first_discoverer_id == user_id} for i, c in generated]
    discovered = sum(1 for e in entries if e["state"] != "undiscovered")
    return {"entries": entries, "discovered": discovered, "total": len(entries), "rate": discovered / max(1, len(entries)),
            "generated": gen}


def _mask(name: str) -> str:
    return "".join(ch if (i == 0 or ch == " ") else "•" for i, ch in enumerate(name))


def _hint(it: Any) -> str | None:
    c = it.conditions or {}
    if c.get("time"):
        return "特定の時間帯にだけ観測されるらしい"
    if c.get("special_only"):
        return "特別なRollの時だけ現れるらしい"
    if c.get("hidden_special"):
        return "ある数字が重なる時に…"
    if c.get("biome_state"):
        return "特殊Biomeの中の、さらに特別な瞬間に"
    if it.min_luck:
        return "強い運の持ち主の前にだけ姿を見せる"
    return None


async def item_detail(db: AsyncSession, item_id: int, viewer_id: int) -> dict[str, Any]:
    snap = get_registry().snap
    row = await db.get(Item, item_id)
    if row is None or row.kind == "admin_artifact" and item_id not in snap.artifacts_by_item:
        raise NotFound("アイテムが見つかりません")
    coll = await db.get(Collection, (viewer_id, item_id))
    it = snap.items.get(item_id)
    if coll is None and (it is None or it.hidden or row.kind == "admin_artifact"):
        owns = (await db.execute(select(func.count()).select_from(ItemInstance).where(ItemInstance.owner_id == viewer_id,
                                                                                      ItemInstance.item_id == item_id))).scalar_one()
        if not owns:
            raise NotFound("未発見のアイテムです")
    first = await db.get(User, row.first_discoverer_id) if row.first_discoverer_id else None
    fastest = await db.get(User, row.fastest_user_id) if row.fastest_user_id else None
    info = await inv_svc.item_info(db, item_id)
    art = snap.artifacts_by_item.get(item_id)
    return {
        "item": info, "lore": row.lore,
        "world": {"discovery_count": row.discovery_count, "owner_count": row.owner_count, "trade_count": row.trade_count,
                  "first_discoverer": user_brief(first) if first else None,
                  "first_discovered_at": row.first_discovered_at.isoformat() if row.first_discovered_at else None,
                  "fastest": {"user": user_brief(fastest), "rolls": row.fastest_roll_count} if fastest else None,
                  "market_value": row.market_value, "serials_issued": row.serial_counter},
        "mine": {"times_obtained": coll.times_obtained if coll else 0,
                 "first_obtained_at": coll.first_obtained_at.isoformat() if coll else None},
        "artifact": {"ability": art["ability"], "theme": art["theme"], "tier": art["tier"], "target": art["target"],
                     "cooldown_sec": art["cooldown_sec"], "duration_sec": art["duration_sec"], "player_usable": art["player_usable"],
                     "transfer_rules": art["transfer_rules"], "equip_passive": art["equip_passive"]} if art else None,
    }
