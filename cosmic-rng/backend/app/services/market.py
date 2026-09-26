"""Player market: escrowed listings, atomic purchases, price history, anomaly detection."""
from __future__ import annotations

import statistics
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, Conflict, Forbidden, NotFound
from ..core.pubsub import queue_event
from ..core.timeutil import utcnow
from ..models import Item, ItemInstance, MarketListing, Rarity, User
from . import discord_notify
from . import feed as feed_svc
from . import inventory as inv_svc
from . import progress as progress_svc
from . import users as users_svc
from .progress import ProgressEvent


async def lock_users_ordered(db: AsyncSession, *ids: int) -> dict[int, User]:
    """Lock several user rows in a consistent order to avoid deadlocks."""
    out: dict[int, User] = {}
    for uid in sorted(set(ids)):
        out[uid] = await users_svc.lock_user(db, uid)
    return out


async def reference_price(db: AsyncSession, item_id: int, sell_value: int) -> tuple[float, int]:
    rows = (await db.execute(select(MarketListing.price).where(MarketListing.item_id == item_id, MarketListing.status == "sold")
                             .order_by(MarketListing.sold_at.desc()).limit(20))).all()
    prices = [r[0] for r in rows]
    if len(prices) >= 3:
        return float(statistics.median(prices)), len(prices)
    return float(max(sell_value, 1) * 3), len(prices)


async def detect_anomaly(db: AsyncSession, item_id: int, sell_value: int, price: int) -> str | None:
    reg = get_registry()
    ref, n = await reference_price(db, item_id, sell_value)
    hi = float(reg.setting("market.anomaly_high") or 80)
    lo = float(reg.setting("market.anomaly_low") or 0.02)
    if price >= ref * hi:
        return f"異常高値: {price:,} (参照 {ref:,.0f}, n={n})"
    if price <= ref * lo:
        return f"異常安値: {price:,} (参照 {ref:,.0f}, n={n})"
    return None


def listing_public(l: MarketListing, info: dict[str, Any] | None, seller: User | None = None) -> dict[str, Any]:
    return {
        "id": l.id, "instance_id": l.instance_id, "item": info or l.snapshot.get("item"), "price": l.price, "status": l.status,
        "seller": users_svc.user_brief(seller) if seller else {"id": l.seller_id}, "created_at": l.created_at.isoformat(),
        "expires_at": l.expires_at.isoformat(), "sold_at": l.sold_at.isoformat() if l.sold_at else None,
        "serial": l.snapshot.get("serial"), "fee": l.fee,
    }


async def browse(db: AsyncSession, *, q: str = "", rarity: str | None = None, item_id: int | None = None, sort: str = "newest",
                 page: int = 1, per_page: int = 40) -> dict[str, Any]:
    now = utcnow()
    per_page = max(1, min(per_page, 100))
    stmt = (select(MarketListing, User).join(User, User.id == MarketListing.seller_id).join(Item, Item.id == MarketListing.item_id)
            .join(Rarity, Rarity.key == Item.rarity_key)
            .where(MarketListing.status == "active", MarketListing.expires_at > now))
    if q:
        stmt = stmt.where(Item.name.ilike(f"%{inv_svc._escape_like(q[:64])}%", escape="\\"))  # noqa: SLF001
    if rarity:
        stmt = stmt.where(Item.rarity_key == rarity)
    if item_id:
        stmt = stmt.where(MarketListing.item_id == item_id)
    order = {"newest": MarketListing.id.desc(), "price_asc": MarketListing.price.asc(), "price_desc": MarketListing.price.desc(),
             "rarity": Rarity.tier.desc()}.get(sort, MarketListing.id.desc())
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (await db.execute(stmt.order_by(order, MarketListing.id.desc()).limit(per_page).offset((max(1, page) - 1) * per_page))).all()
    infos = await inv_svc.items_info(db, {l.item_id for l, _ in rows})
    return {"listings": [listing_public(l, infos.get(l.item_id), u) for l, u in rows], "total": total, "page": page}


async def my_listings(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    rows = (await db.execute(select(MarketListing).where(MarketListing.seller_id == user_id)
                             .order_by(MarketListing.id.desc()).limit(100))).scalars().all()
    infos = await inv_svc.items_info(db, {l.item_id for l in rows})
    return [listing_public(l, infos.get(l.item_id)) for l in rows]


async def price_history(db: AsyncSession, item_id: int, limit: int = 60) -> dict[str, Any]:
    info = await inv_svc.item_info(db, item_id)
    rows = (await db.execute(select(MarketListing.price, MarketListing.sold_at).where(
        MarketListing.item_id == item_id, MarketListing.status == "sold").order_by(MarketListing.sold_at.desc()).limit(min(limit, 200)))).all()
    ref, n = await reference_price(db, item_id, int(info.get("sell_value", 0)))
    return {"item": info, "sales": [{"price": r[0], "at": r[1].isoformat()} for r in rows], "reference": ref, "samples": n}


async def create_listing(db: AsyncSession, user_id: int, instance_id: int, price: int) -> dict[str, Any]:
    reg = get_registry()
    user = await users_svc.lock_user(db, user_id)
    users_svc.require_feature(user, "market", "features.market_enabled")
    if price < 1 or price > int(reg.setting("market.max_price") or 10**12):
        raise AppError("価格が範囲外です", code="invalid_price")
    active = int((await db.execute(select(func.count()).select_from(MarketListing).where(
        MarketListing.seller_id == user.id, MarketListing.status == "active"))).scalar_one())
    if active >= int(reg.setting("market.max_listings") or 25):
        raise AppError("出品数の上限に達しています", code="listing_limit")
    inst = (await inv_svc.get_owned_instances(db, user.id, [instance_id]))[0]
    info = await inv_svc.item_info(db, inst.item_id)
    inv_svc.assert_transferable(inst, info, "出品")
    if inst.favorite:
        raise Forbidden("お気に入りのアイテムは出品できません", code="favorite")
    inst.state = "listed"
    now = utcnow()
    listing = MarketListing(seller_id=user.id, instance_id=inst.id, item_id=inst.item_id, price=price,
                            expires_at=now + timedelta(days=int(reg.setting("market.listing_days") or 7)),
                            snapshot={"item": info, "serial": inst.serial})
    reason = await detect_anomaly(db, inst.item_id, int(info.get("sell_value", 0)), price)
    if reason:
        listing.flagged = True
        listing.flag_reason = reason
    db.add(listing)
    await db.flush()
    if reason:
        await feed_svc.notify_admins(db, "market_anomaly", f"Market異常検知: {info['name']}",
                                     f"{user.display_name} (#{user.id}) の出品 #{listing.id}: {reason}",
                                     {"listing_id": listing.id, "user_id": user.id})
        await discord_notify.enqueue_alert(db, "Market異常検知", f"{user.display_name} (#{user.id}) {info['name']} #{listing.id}: {reason}")
    queue_event(db, "all", "market", {"action": "listed", "listing": listing_public(listing, info, user)})
    return listing_public(listing, info, user)


async def cancel_listing(db: AsyncSession, user_id: int, listing_id: int, *, by_admin: bool = False) -> dict[str, Any]:
    listing = (await db.execute(select(MarketListing).where(MarketListing.id == listing_id).with_for_update())).scalar_one_or_none()
    if listing is None or (listing.seller_id != user_id and not by_admin):
        raise NotFound("出品が見つかりません")
    if listing.status != "active":
        raise Conflict("この出品は既に終了しています", code="listing_closed")
    listing.status = "cancelled"
    listing.closed_at = utcnow()
    await db.execute(update(ItemInstance).where(ItemInstance.id == listing.instance_id, ItemInstance.state == "listed")
                     .values(state="owned"))
    queue_event(db, "all", "market", {"action": "cancelled", "listing_id": listing.id})
    return {"ok": True}


async def buy(db: AsyncSession, buyer_id: int, listing_id: int) -> dict[str, Any]:
    reg = get_registry()
    listing = (await db.execute(select(MarketListing).where(MarketListing.id == listing_id).with_for_update())).scalar_one_or_none()
    if listing is None:
        raise NotFound("出品が見つかりません")
    now = utcnow()
    if listing.status != "active" or listing.expires_at <= now:
        raise Conflict("この出品は既に終了しています", code="listing_closed")
    if listing.seller_id == buyer_id:
        raise AppError("自分の出品は購入できません", code="own_listing")
    users = await lock_users_ordered(db, buyer_id, listing.seller_id)
    buyer, seller = users[buyer_id], users[listing.seller_id]
    users_svc.require_feature(buyer, "market", "features.market_enabled")
    if buyer.status != "active":
        raise Forbidden("このアカウントでは購入できません", code="account_restricted")
    if buyer.stardust < listing.price:
        raise AppError("Stardustが足りません", code="insufficient_funds")
    # Velocity cap: one account can only absorb so many listings per day, which
    # blunts both bulk laundering and a scripted buyer sweeping the board.
    bought_today = int((await db.execute(select(func.count()).select_from(MarketListing).where(
        MarketListing.buyer_id == buyer.id, MarketListing.status == "sold",
        MarketListing.sold_at > now - timedelta(days=1)))).scalar_one())
    if bought_today >= int(reg.setting("market.daily_buy_limit") or 120):
        raise AppError("本日のMarket購入上限に達しています", code="market_daily_limit", status_code=429)
    inst = (await db.execute(select(ItemInstance).where(ItemInstance.id == listing.instance_id).with_for_update())).scalar_one_or_none()
    if inst is None or inst.owner_id != seller.id or inst.state != "listed":
        listing.status = "cancelled"
        listing.closed_at = now
        raise Conflict("出品アイテムが無効になっています", code="listing_invalid")
    fee = int(listing.price * float(reg.setting("market.fee_pct") or 0) / 100.0)
    buyer.stardust -= listing.price
    seller.stardust += listing.price - fee
    inst.owner_id = buyer.id
    inst.state = "owned"
    inst.locked = False
    inst.favorite = False
    listing.status = "sold"
    listing.buyer_id = buyer.id
    listing.sold_at = now
    listing.closed_at = now
    listing.fee = fee
    await db.execute(update(Item).where(Item.id == inst.item_id).values(trade_count=Item.trade_count + 1))
    from .rolls import collection_upsert

    snap = get_registry().snap
    new_coll = await collection_upsert(db, buyer.id, inst.item_id, 1)
    bstats = await users_svc.lock_stats(db, buyer.id)
    sstats = await users_svc.lock_stats(db, seller.id)
    if new_coll and inst.item_id in snap.collectible_ids:
        bstats.discovered_count += 1
    bstats.market_buys += 1
    sstats.market_sales += 1
    sstats.stardust_earned += listing.price - fee
    # wash trading heuristic: repeated trades between the same pair
    pair = int((await db.execute(select(func.count()).select_from(MarketListing).where(
        MarketListing.status == "sold", MarketListing.sold_at > now - timedelta(hours=24),
        or_(and_(MarketListing.seller_id == seller.id, MarketListing.buyer_id == buyer.id),
            and_(MarketListing.seller_id == buyer.id, MarketListing.buyer_id == seller.id))))).scalar_one())
    threshold = int(reg.setting("market.wash_threshold") or 5)
    if pair == threshold:
        msg = f"{seller.display_name}(#{seller.id}) ⇄ {buyer.display_name}(#{buyer.id}) が24時間で{pair}回取引"
        await feed_svc.notify_admins(db, "market_wash", "Market: 同一ペアの反復取引", msg, {"users": [seller.id, buyer.id]})
        await discord_notify.enqueue_alert(db, "Market反復取引の検知", msg)
    info = listing.snapshot.get("item") or await inv_svc.item_info(db, inst.item_id)
    brs = await progress_svc.handle_events(db, buyer, bstats, [ProgressEvent("obtain", params={"tier": info.get("tier", 1), "item_key": info.get("key")})])
    await progress_svc.handle_events(db, seller, sstats, [ProgressEvent("market_sell"), ProgressEvent("earn", params={"amount": listing.price - fee})])
    await feed_svc.notify_user(db, seller.id, "market_sold", f"{info.get('name')} が売れました",
                               f"{buyer.display_name} が {listing.price:,} Stardustで購入しました（手数料 {fee:,}）",
                               {"listing_id": listing.id})
    queue_event(db, "all", "market", {"action": "sold", "listing_id": listing.id, "item_id": inst.item_id, "price": listing.price})
    return {"ok": True, "instance_id": inst.id, "price": listing.price, "stardust": buyer.stardust, "progress": brs.public()}


async def expire_listings(db: AsyncSession) -> int:
    now = utcnow()
    rows = (await db.execute(select(MarketListing).where(MarketListing.status == "active", MarketListing.expires_at <= now)
                             .limit(500).with_for_update(skip_locked=True))).scalars().all()
    for l in rows:
        l.status = "expired"
        l.closed_at = now
        await db.execute(update(ItemInstance).where(ItemInstance.id == l.instance_id, ItemInstance.state == "listed").values(state="owned"))
    await db.commit()
    return len(rows)
