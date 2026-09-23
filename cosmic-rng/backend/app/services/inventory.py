"""Item instances (the player's inventory): creation, listing, protection, selling."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from sqlalchemy import Select, and_, case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, Forbidden, NotFound
from ..models import Item, ItemInstance, Rarity, UserItemPref, UserUnlock
from .constants import SERIAL_MIN_TIER

_GEN_CACHE: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_GEN_CACHE_MAX = 5000


def _item_row_public(it: Item) -> dict[str, Any]:
    snap = get_registry().snap
    r = snap.rarities.get(it.rarity_key)
    return {
        "id": it.id, "key": it.key, "name": it.name, "description": it.description, "lore": it.lore, "kind": it.kind,
        "rarity": it.rarity_key, "tier": r.tier if r else 1, "odds": it.odds, "display_odds": it.display_odds,
        "sell_value": it.sell_value, "visual": it.visual or {}, "animation": it.animation or (r.cutscene if r else None),
        "sound": it.sound, "tradeable": it.tradeable, "biomes": list(it.biome_keys or []),
    }


async def item_info(db: AsyncSession, item_id: int) -> dict[str, Any]:
    snap = get_registry().snap
    d = snap.items.get(item_id)
    if d is not None:
        return d.public(snap.rarities)
    cached = _GEN_CACHE.get(item_id)
    if cached is not None:
        _GEN_CACHE.move_to_end(item_id)
        return cached
    it = await db.get(Item, item_id)
    if it is None:
        raise NotFound("アイテムが見つかりません")
    info = _item_row_public(it)
    _GEN_CACHE[item_id] = info
    if len(_GEN_CACHE) > _GEN_CACHE_MAX:
        _GEN_CACHE.popitem(last=False)
    return info


async def items_info(db: AsyncSession, ids: set[int]) -> dict[int, dict[str, Any]]:
    snap = get_registry().snap
    out: dict[int, dict[str, Any]] = {}
    missing = []
    for i in ids:
        d = snap.items.get(i)
        if d is not None:
            out[i] = d.public(snap.rarities)
        elif i in _GEN_CACHE:
            out[i] = _GEN_CACHE[i]
        else:
            missing.append(i)
    if missing:
        for it in (await db.execute(select(Item).where(Item.id.in_(missing)))).scalars().all():
            info = _item_row_public(it)
            _GEN_CACHE[it.id] = info
            out[it.id] = info
    return out


async def capacity(db: AsyncSession, user_id: int) -> int:
    base = int(get_registry().setting("inventory.capacity") or 3000)
    row = await db.get(UserUnlock, (user_id, "inventory_expansion"))
    extra = int((row.value or {}).get("count", 1)) * 1000 if row else 0
    return base + extra


async def count_instances(db: AsyncSession, user_id: int) -> int:
    return int((await db.execute(select(func.count()).select_from(ItemInstance).where(ItemInstance.owner_id == user_id))).scalar_one())


async def next_serial(db: AsyncSession, item_id: int, n: int = 1) -> int:
    """Atomically reserve n serial numbers; returns the first one."""
    res = await db.execute(
        update(Item).where(Item.id == item_id).values(serial_counter=Item.serial_counter + n).returning(Item.serial_counter)
    )
    last = int(res.scalar_one())
    return last - n + 1


async def create_instances(
    db: AsyncSession, owner_id: int, item_id: int, count: int, source: str, *, tier: int = 1,
    meta: dict[str, Any] | None = None, roll_id: int | None = None,
) -> list[int]:
    if count <= 0:
        return []
    first_serial = await next_serial(db, item_id, count) if tier >= SERIAL_MIN_TIER else None
    rows = [
        {"item_id": item_id, "owner_id": owner_id, "original_owner_id": owner_id, "source": source,
         "serial": (first_serial + i) if first_serial is not None else None, "meta": meta or {}, "roll_id": roll_id}
        for i in range(count)
    ]
    res = await db.execute(insert(ItemInstance).returning(ItemInstance.id), rows)
    return [r[0] for r in res.all()]


async def item_favorites(db: AsyncSession, user_id: int) -> set[int]:
    rows = (await db.execute(select(UserItemPref.item_id).where(UserItemPref.user_id == user_id, UserItemPref.favorite.is_(True)))).all()
    return {r[0] for r in rows}


async def set_item_favorite(db: AsyncSession, user_id: int, item_id: int, favorite: bool) -> None:
    await item_info(db, item_id)
    stmt = insert(UserItemPref).values(user_id=user_id, item_id=item_id, favorite=favorite)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id", "item_id"], set_={"favorite": favorite})
    await db.execute(stmt)


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


SORT_FIELDS = {"rarity", "odds", "obtained", "price", "name", "count"}


def _parse_sort(sort: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for part in (sort or "rarity:desc").split(",")[:4]:
        field, _, direction = part.strip().partition(":")
        if field in SORT_FIELDS:
            out.append((field, direction.lower() != "asc"))
    return out or [("rarity", True)]


async def list_grouped(
    db: AsyncSession, user_id: int, *, q: str = "", sort: str = "rarity:desc", rarities: list[str] | None = None,
    kinds: list[str] | None = None, favorites_only: bool = False, page: int = 1, per_page: int = 60,
) -> dict[str, Any]:
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    cnt = func.count(ItemInstance.id).label("cnt")
    last = func.max(ItemInstance.obtained_at).label("last")
    locked = func.sum(case((ItemInstance.locked.is_(True), 1), else_=0)).label("locked")
    fav = func.sum(case((ItemInstance.favorite.is_(True), 1), else_=0)).label("fav")
    listed = func.sum(case((ItemInstance.state == "listed", 1), else_=0)).label("listed")
    stmt: Select[Any] = (
        select(ItemInstance.item_id, cnt, last, locked, fav, listed)
        .join(Item, Item.id == ItemInstance.item_id)
        .join(Rarity, Rarity.key == Item.rarity_key)
        .where(ItemInstance.owner_id == user_id)
        .group_by(ItemInstance.item_id, Item.name, Item.odds, Item.sell_value, Rarity.tier)
    )
    if q:
        stmt = stmt.where(Item.name.ilike(f"%{_escape_like(q[:64])}%", escape="\\"))
    if rarities:
        stmt = stmt.where(Item.rarity_key.in_(rarities[:10]))
    if kinds:
        stmt = stmt.where(Item.kind.in_(kinds[:10]))
    if favorites_only:
        stmt = stmt.having(func.sum(case((ItemInstance.favorite.is_(True), 1), else_=0)) > 0)
    order = []
    for field, desc in _parse_sort(sort):
        col = {
            "rarity": Rarity.tier, "odds": func.coalesce(Item.odds, 0), "obtained": last, "price": Item.sell_value,
            "name": Item.name, "count": cnt,
        }[field]
        order.append(col.desc() if desc else col.asc())
        if field == "rarity":
            order.append(func.coalesce(Item.odds, 0).desc() if desc else func.coalesce(Item.odds, 0).asc())
    stmt = stmt.order_by(*order, ItemInstance.item_id)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (await db.execute(stmt.limit(per_page).offset((page - 1) * per_page))).all()
    infos = await items_info(db, {r.item_id for r in rows})
    favs = await item_favorites(db, user_id)
    groups = [
        {"item": infos[r.item_id], "count": int(r.cnt), "last_obtained": r.last.isoformat() if r.last else None,
         "locked": int(r.locked or 0), "favorite_instances": int(r.fav or 0), "listed": int(r.listed or 0),
         "favorite": r.item_id in favs}
        for r in rows if r.item_id in infos
    ]
    return {"groups": groups, "total": total, "page": page, "per_page": per_page}


def instance_public(inst: ItemInstance, info: dict[str, Any] | None = None) -> dict[str, Any]:
    d = {
        "id": inst.id, "item_id": inst.item_id, "serial": inst.serial, "source": inst.source, "state": inst.state,
        "locked": inst.locked, "favorite": inst.favorite, "meta": inst.meta or {},
        "obtained_at": inst.obtained_at.isoformat() if inst.obtained_at else None,
    }
    if info is not None:
        d["item"] = info
    return d


async def list_instances(db: AsyncSession, user_id: int, item_id: int | None = None, page: int = 1, per_page: int = 100) -> dict[str, Any]:
    per_page = max(1, min(per_page, 500))
    stmt = select(ItemInstance).where(ItemInstance.owner_id == user_id)
    if item_id is not None:
        stmt = stmt.where(ItemInstance.item_id == item_id)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (await db.execute(stmt.order_by(ItemInstance.obtained_at.desc(), ItemInstance.id.desc())
                             .limit(per_page).offset((max(1, page) - 1) * per_page))).scalars().all()
    infos = await items_info(db, {r.item_id for r in rows})
    return {"instances": [instance_public(r, infos.get(r.item_id)) for r in rows], "total": total}


async def get_owned_instances(db: AsyncSession, user_id: int, ids: list[int], lock: bool = True) -> list[ItemInstance]:
    if not ids:
        return []
    uniq = list(dict.fromkeys(int(i) for i in ids))
    q = select(ItemInstance).where(ItemInstance.id.in_(uniq), ItemInstance.owner_id == user_id).order_by(ItemInstance.id)
    if lock:
        q = q.with_for_update()
    rows = list((await db.execute(q)).scalars().all())
    if len(rows) != len(uniq):
        raise NotFound("指定されたアイテムの一部が見つかりません", code="instance_not_found")
    return rows


def assert_transferable(inst: ItemInstance, info: dict[str, Any], action: str) -> None:
    """Common protection checks for sell / trade / gift / market."""
    snap = get_registry().snap
    if inst.item_id in snap.artifacts_by_item or info.get("kind") == "admin_artifact":
        raise Forbidden("Admin Artifactは移動・売却できません", code="admin_artifact_restricted")
    if inst.locked:
        raise Forbidden(f"ロック中のアイテムは{action}できません", code="locked")
    if inst.state != "owned":
        raise AppError(f"このアイテムは現在{action}できません（出品中・装備中など）", code="instance_busy")
    if action != "売却" and not info.get("tradeable", True):
        raise Forbidden(f"このアイテムは{action}できません", code="not_tradeable")


async def sell_instances(db: AsyncSession, user_id: int, ids: list[int], sell_bonus: float = 0.0) -> tuple[int, int]:
    if not ids or len(ids) > 5000:
        raise AppError("売却対象が不正です", code="invalid_selection")
    rows = await get_owned_instances(db, user_id, ids)
    infos = await items_info(db, {r.item_id for r in rows})
    mult = float(get_registry().setting("economy.sell_mult") or 1.0) * (1.0 + sell_bonus)
    total = 0
    for r in rows:
        info = infos[r.item_id]
        assert_transferable(r, info, "売却")
        if r.favorite:
            raise Forbidden("お気に入りのアイテムは売却できません（解除してください）", code="favorite")
        if info.get("sell_value", 0) <= 0:
            raise Forbidden(f"{info['name']} は売却できません", code="not_sellable")
        total += int(round(info["sell_value"] * mult))
    await db.execute(delete(ItemInstance).where(ItemInstance.id.in_([r.id for r in rows])))
    return total, len(rows)


async def select_bulk_sell(
    db: AsyncSession, user_id: int, *, keep: int = 1, max_tier: int = 2, item_ids: list[int] | None = None, limit: int = 5000,
) -> list[int]:
    """Instances eligible for 'sell duplicates': unlocked, unfavorited, owned, sellable, keeping ``keep`` per item."""
    snap = get_registry().snap
    rn = func.row_number().over(partition_by=ItemInstance.item_id, order_by=ItemInstance.obtained_at.asc()).label("rn")
    sub = (
        select(ItemInstance.id, ItemInstance.item_id, rn)
        .join(Item, Item.id == ItemInstance.item_id)
        .join(Rarity, Rarity.key == Item.rarity_key)
        .where(
            ItemInstance.owner_id == user_id, ItemInstance.locked.is_(False), ItemInstance.favorite.is_(False),
            ItemInstance.state == "owned", Rarity.tier <= max_tier, Item.sell_value > 0, Item.kind != "admin_artifact",
        )
    )
    if item_ids:
        sub = sub.where(ItemInstance.item_id.in_(item_ids[:500]))
    favs = await item_favorites(db, user_id)
    sub_q = sub.subquery()
    rows = (await db.execute(select(sub_q.c.id, sub_q.c.item_id).where(sub_q.c.rn > max(0, keep)).limit(limit))).all()
    return [r.id for r in rows if r.item_id not in favs and r.item_id not in snap.artifacts_by_item]


async def set_flags(db: AsyncSession, user_id: int, ids: list[int], *, locked: bool | None = None, favorite: bool | None = None) -> int:
    if not ids or len(ids) > 2000:
        raise AppError("対象が不正です", code="invalid_selection")
    values: dict[str, Any] = {}
    if locked is not None:
        values["locked"] = locked
    if favorite is not None:
        values["favorite"] = favorite
    if not values:
        return 0
    res = await db.execute(update(ItemInstance).where(and_(ItemInstance.id.in_(ids), ItemInstance.owner_id == user_id)).values(**values))
    return res.rowcount or 0


async def owned_counts(db: AsyncSession, user_id: int, item_ids: list[int]) -> dict[int, int]:
    if not item_ids:
        return {}
    rows = (await db.execute(
        select(ItemInstance.item_id, func.count()).where(ItemInstance.owner_id == user_id, ItemInstance.item_id.in_(item_ids),
                                                         ItemInstance.state == "owned", ItemInstance.locked.is_(False))
        .group_by(ItemInstance.item_id)
    )).all()
    return {r[0]: int(r[1]) for r in rows}


async def consume_items(db: AsyncSession, user_id: int, requirements: dict[int, int]) -> None:
    """Consume unlocked, unfavorited, owned instances (oldest first) for crafting."""
    for item_id, qty in requirements.items():
        rows = (await db.execute(
            select(ItemInstance.id).where(ItemInstance.owner_id == user_id, ItemInstance.item_id == item_id,
                                          ItemInstance.state == "owned", ItemInstance.locked.is_(False),
                                          ItemInstance.favorite.is_(False))
            .order_by(ItemInstance.obtained_at.asc()).limit(qty).with_for_update(skip_locked=True)
        )).all()
        if len(rows) < qty:
            raise AppError("素材が不足しています（ロック/お気に入りのアイテムは使用されません）", code="not_enough_materials")
        await db.execute(delete(ItemInstance).where(ItemInstance.id.in_([r[0] for r in rows])))
