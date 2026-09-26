"""Shop: listing and purchasing (idempotent, limit-checked, biome-restricted)."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..db import upsert as insert
from ..core.errors import AppError, Forbidden, NotFound
from ..core.timeutil import utcnow
from ..models import ShopPurchase, User, UserBiome, UserCosmetic, UserUnlock
from . import effects as effects_svc
from . import equipment as equipment_svc
from . import inventory as inv_svc
from . import progress as progress_svc
from . import users as users_svc


async def current_biome_key(db: AsyncSession, user_id: int, now: datetime) -> str:
    snap = get_registry().snap
    row = await db.get(UserBiome, user_id)
    if row is None:
        return snap.default_biome.key
    if row.ends_at is not None and row.ends_at <= now:
        return snap.default_biome.key
    return row.biome_key


def _period_start(period: str | None, now: datetime) -> datetime | None:
    if period == "daily":
        tz = ZoneInfo(str(get_registry().setting("quests.reset_timezone") or "Asia/Tokyo"))
        local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return local
    return None


async def _purchased_count(db: AsyncSession, user_id: int, shop_item_id: int, period: str | None, now: datetime) -> int:
    q = select(func.coalesce(func.sum(ShopPurchase.quantity), 0)).where(ShopPurchase.user_id == user_id,
                                                                       ShopPurchase.shop_item_id == shop_item_id)
    start = _period_start(period, now)
    if start is not None:
        q = q.where(ShopPurchase.created_at >= start)
    return int((await db.execute(q)).scalar_one())


FEATURED_SHOP = "cosmic_atelier"
FEATURED_DISCOUNT = 30  # percent off, one item per day


def _local_date(now: datetime) -> str:
    tz = ZoneInfo(str(get_registry().setting("quests.reset_timezone") or "Asia/Tokyo"))
    return now.astimezone(tz).date().isoformat()


def featured_key(now: datetime) -> str | None:
    """Today's discounted Atelier item.

    Chosen by hashing the local date, so every player sees the same one and
    nobody can reroll it — and it moves at the same reset as daily quests.
    """
    snap = get_registry().snap
    pool = sorted(k for k, p in snap.shop_items.items()
                  if p["shop_key"] == FEATURED_SHOP and p["is_active"])
    if not pool:
        return None
    digest = hashlib.sha256(_local_date(now).encode()).digest()
    return pool[int.from_bytes(digest[:8], "big") % len(pool)]


def _priced(p: dict[str, Any], featured: str | None) -> tuple[int, bool]:
    """The price a player actually pays, and whether the discount applied."""
    price = int(p["price"])
    if featured is not None and p["key"] == featured:
        return max(1, price * (100 - FEATURED_DISCOUNT) // 100), True
    return price, False


def _product_info(p: dict[str, Any]) -> dict[str, Any]:
    snap = get_registry().snap
    t = p["product_type"]
    if t == "boost":
        b = snap.boosts.get(p["product_key"]) or {}
        return {"rarity": b.get("rarity_key", "common"), "visual": b.get("visual", {}), "detail": b.get("description", "")}
    if t == "equipment":
        e = snap.equipment.get(p["product_key"]) or {}
        return {"rarity": e.get("rarity_key", "common"), "visual": e.get("visual", {}), "detail": e.get("description", ""),
                "luck_bonus": e.get("luck_bonus"), "speed_bonus": e.get("speed_bonus"), "slot": e.get("slot")}
    if t == "item":
        it = snap.items_by_key.get(p["product_key"])
        return {"rarity": it.rarity_key if it else "common", "visual": it.visual if it else {}, "detail": it.description if it else ""}
    if t == "cosmetic":
        c = snap.cosmetics.get(p["product_key"]) or {}
        return {"rarity": c.get("rarity_key", "epic"), "visual": c.get("visual") or {}, "detail": c.get("description", ""),
                "cosmetic_kind": c.get("kind")}
    return {"rarity": "epic", "visual": p.get("visual") or {"shape": "core", "colors": ["#8ab4ff", "#b58cff", "#ffffff"]},
            "detail": p.get("description", "")}


async def list_shops(db: AsyncSession, user: User) -> dict[str, Any]:
    snap = get_registry().snap
    now = utcnow()
    biome = await current_biome_key(db, user.id, now)
    unlocks = {r[0] for r in (await db.execute(select(UserUnlock.unlock_key).where(UserUnlock.user_id == user.id))).all()}
    counts = {r[0]: int(r[1]) for r in (await db.execute(
        select(ShopPurchase.shop_item_id, func.sum(ShopPurchase.quantity)).where(ShopPurchase.user_id == user.id)
        .group_by(ShopPurchase.shop_item_id))).all()}
    owned_cosmetics = {r[0] for r in (await db.execute(
        select(UserCosmetic.cosmetic_key).where(UserCosmetic.user_id == user.id))).all()}
    featured = featured_key(now)
    shops = []
    for s in sorted(snap.shops.values(), key=lambda x: x["sort_order"]):
        if not s["is_active"]:
            continue
        available = (not s["biome_key"] or s["biome_key"] == biome) and user.level >= s["min_level"]
        if s["biome_key"] and s["biome_key"] != biome:
            # Biome shops are only revealed while their biome is active.
            shops.append({"key": s["key"], "name": s["name"], "description": s["description"], "biome_key": s["biome_key"],
                          "available": False, "items": [], "locked_reason": "biome"})
            continue
        items = []
        for p in sorted([p for p in snap.shop_items.values() if p["shop_key"] == s["key"] and p["is_active"]], key=lambda x: x["sort_order"]):
            bought = counts.get(p["id"], 0)
            daily = 0
            if p["limit_period"] == "daily":
                daily = await _purchased_count(db, user.id, p["id"], "daily", now)
            used = daily if p["limit_period"] == "daily" else bought
            remaining = None if p["limit_count"] is None else max(0, p["limit_count"] - used)
            reason = None
            if user.level < p["min_level"]:
                reason = f"Lv.{p['min_level']}"
            elif p["requires_unlock"] and p["requires_unlock"] not in unlocks:
                req = next((x for x in snap.shop_items.values() if x["product_key"] == p["requires_unlock"]), None)
                reason = f"要: {req['name'] if req else p['requires_unlock']}"
            elif p["biome_key"] and p["biome_key"] != biome:
                reason = "biome"
            price, is_featured = _priced(p, featured)
            owned = (p["product_type"] == "unlock" and p["product_key"] in unlocks and p["product_key"] != "inventory_expansion") \
                or (p["product_type"] == "cosmetic" and p["product_key"] in owned_cosmetics)
            items.append({
                "key": p["key"], "name": p["name"], "description": p["description"], "product_type": p["product_type"],
                "product_key": p["product_key"], "quantity": p["quantity"], "price": price, "base_price": int(p["price"]),
                "featured": is_featured, "discount_pct": FEATURED_DISCOUNT if is_featured else 0,
                "limit": p["limit_count"], "period": p["limit_period"], "remaining": remaining, "locked_reason": reason,
                "owned": owned,
                **_product_info(p),
            })
        shops.append({"key": s["key"], "name": s["name"], "description": s["description"], "biome_key": s["biome_key"],
                      "available": available, "items": items})
    return {"shops": shops, "biome": biome, "stardust": user.stardust,
            "featured": {"key": featured, "discount_pct": FEATURED_DISCOUNT, "date": _local_date(now)} if featured else None}


async def purchase(db: AsyncSession, user_id: int, shop_item_key: str, quantity: int) -> dict[str, Any]:
    snap = get_registry().snap
    now = utcnow()
    if quantity < 1 or quantity > 99:
        raise AppError("数量が不正です", code="invalid_quantity")
    user = await users_svc.lock_user(db, user_id)
    users_svc.require_feature(user, "shop", "features.shop_enabled")
    p = snap.shop_items.get(shop_item_key)
    if p is None or not p["is_active"]:
        raise NotFound("商品が見つかりません")
    shop = snap.shops.get(p["shop_key"])
    if shop is None or not shop["is_active"]:
        raise NotFound("ショップが見つかりません")
    biome = await current_biome_key(db, user.id, now)
    if (shop["biome_key"] and shop["biome_key"] != biome) or (p["biome_key"] and p["biome_key"] != biome):
        raise Forbidden("このBiomeでは購入できません", code="wrong_biome")
    if user.level < max(p["min_level"], shop["min_level"]):
        raise Forbidden("レベルが不足しています", code="level_required")
    if p["requires_unlock"] and await db.get(UserUnlock, (user.id, p["requires_unlock"])) is None:
        raise Forbidden("前提となるアップグレードが必要です", code="prerequisite_required")
    if p["product_type"] == "unlock" and p["product_key"] != "inventory_expansion":
        if await db.get(UserUnlock, (user.id, p["product_key"])) is not None:
            raise AppError("既に所持しています", code="already_owned")
        quantity = 1
    if p["product_type"] == "cosmetic":
        if p["product_key"] not in snap.cosmetics:
            raise NotFound("商品が見つかりません")
        if await db.get(UserCosmetic, (user.id, p["product_key"])) is not None:
            raise AppError("既に所持しています", code="already_owned")
        quantity = 1
    if p["limit_count"] is not None:
        used = await _purchased_count(db, user.id, p["id"], p["limit_period"], now)
        if used + quantity > p["limit_count"]:
            raise AppError("購入上限に達しています", code="limit_reached")
    unit, _featured = _priced(p, featured_key(now))  # the discount is recomputed here, never sent by the client
    total = unit * quantity
    if user.stardust < total:
        raise AppError("Stardustが足りません", code="insufficient_funds")
    user.stardust -= total
    delivered: dict[str, Any] = {"type": p["product_type"], "key": p["product_key"], "quantity": quantity * p["quantity"]}
    t = p["product_type"]
    if t == "boost":
        await effects_svc.grant_boost_items(db, user.id, p["product_key"], quantity * p["quantity"])
    elif t == "equipment":
        delivered["instances"] = []
        for _ in range(quantity * p["quantity"]):
            inst = await equipment_svc.create_instance(db, user.id, p["product_key"], "shop")
            delivered["instances"].append(equipment_svc.public(inst))
    elif t == "unlock":
        if p["product_key"] == "inventory_expansion":
            row = await db.get(UserUnlock, (user.id, "inventory_expansion"))
            if row is None:
                db.add(UserUnlock(user_id=user.id, unlock_key="inventory_expansion", value={"count": quantity}))
            else:
                row.value = {"count": int((row.value or {}).get("count", 1)) + quantity}
        else:
            await db.execute(insert(UserUnlock).values(user_id=user.id, unlock_key=p["product_key"], value={}).on_conflict_do_nothing())
    elif t == "cosmetic":
        if not await progress_svc.grant_cosmetic(db, user.id, p["product_key"], "shop"):
            raise AppError("既に所持しています", code="already_owned")
    elif t == "item":
        item = snap.items_by_key.get(p["product_key"])
        if item is None:
            raise NotFound("商品が見つかりません")
        await inv_svc.create_instances(db, user.id, item.id, quantity * p["quantity"], "shop", tier=item.tier)
    else:
        raise AppError("不正な商品です", code="invalid_product")
    db.add(ShopPurchase(user_id=user.id, shop_item_id=p["id"], quantity=quantity, price_total=total))
    return {"ok": True, "spent": total, "stardust": user.stardust, "delivered": delivered}
