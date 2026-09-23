"""Player-to-player trades and gifts.

Trades are offers (A gives X + stardust, asks for Y + stardust). The target
accepts with the offer's ``revision`` so a modified offer can never be
accepted by mistake (bait-and-switch). Acceptance validates ownership and
protections of every item under row locks and swaps atomically.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, Conflict, Forbidden, NotFound
from ..core.pubsub import queue_event
from ..core.timeutil import utcnow
from ..models import Gift, Item, ItemInstance, Trade, TradeItem, User
from . import feed as feed_svc
from . import inventory as inv_svc
from . import progress as progress_svc
from . import users as users_svc
from .market import lock_users_ordered
from .progress import ProgressEvent


async def _validate_items(db: AsyncSession, owner_id: int, ids: list[int], action: str, lock: bool) -> tuple[list[ItemInstance], dict[int, dict[str, Any]]]:
    rows = await inv_svc.get_owned_instances(db, owner_id, ids, lock=lock)
    infos = await inv_svc.items_info(db, {r.item_id for r in rows})
    for r in rows:
        inv_svc.assert_transferable(r, infos[r.item_id], action)
    return rows, infos


def _item_snapshot(rows: list[ItemInstance], infos: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"instance_id": r.id, "item_id": r.item_id, "serial": r.serial, "item": infos[r.item_id]} for r in rows]


def trade_public(t: Trade, from_user: User | None = None, to_user: User | None = None) -> dict[str, Any]:
    return {
        "id": t.id, "status": t.status, "revision": t.revision, "message": t.message,
        "from": users_svc.user_brief(from_user) if from_user else {"id": t.from_user_id},
        "to": users_svc.user_brief(to_user) if to_user else {"id": t.to_user_id},
        "offer": {"items": t.snapshot.get("offer", []), "stardust": t.offer_stardust},
        "request": {"items": t.snapshot.get("request", []), "stardust": t.request_stardust},
        "created_at": t.created_at.isoformat(), "expires_at": t.expires_at.isoformat(),
        "completed_at": t.completed_at.isoformat() if t.completed_at else None, "failure_reason": t.failure_reason,
    }


async def create_trade(db: AsyncSession, user_id: int, to_user_id: int, offer_items: list[int], offer_stardust: int,
                       request_items: list[int], request_stardust: int, message: str | None) -> dict[str, Any]:
    reg = get_registry()
    user = await users_svc.lock_user(db, user_id)
    users_svc.require_feature(user, "trade", "features.trade_enabled")
    if to_user_id == user_id:
        raise AppError("自分とはTradeできません", code="self_trade")
    target = await db.get(User, to_user_id)
    if target is None or target.status == "banned":
        raise NotFound("相手が見つかりません")
    if target.level < users_svc.feature_level("trade"):
        raise AppError("相手はまだTradeを解放していません", code="target_locked")
    max_items = int(reg.setting("trade.max_items") or 20)
    if len(offer_items) > max_items or len(request_items) > max_items:
        raise AppError(f"アイテムは片側{max_items}個までです", code="too_many_items")
    if offer_stardust < 0 or request_stardust < 0:
        raise AppError("Stardustが不正です", code="invalid_amount")
    if not offer_items and not request_items and not offer_stardust and not request_stardust:
        raise AppError("空のTradeは作成できません", code="empty_trade")
    pending = int((await db.execute(select(func.count()).select_from(Trade).where(Trade.from_user_id == user.id, Trade.status == "pending"))).scalar_one())
    if pending >= int(reg.setting("trade.max_pending") or 10):
        raise AppError("送信中のTradeが多すぎます", code="too_many_pending")
    if user.stardust < offer_stardust:
        raise AppError("Stardustが足りません", code="insufficient_funds")
    o_rows, o_infos = await _validate_items(db, user.id, offer_items, "Trade", lock=False)
    r_rows, r_infos = await _validate_items(db, target.id, request_items, "Trade", lock=False)
    t = Trade(from_user_id=user.id, to_user_id=target.id, offer_items=[r.id for r in o_rows], request_items=[r.id for r in r_rows],
              offer_stardust=offer_stardust, request_stardust=request_stardust, message=(message or "")[:200] or None,
              expires_at=utcnow() + timedelta(hours=int(reg.setting("trade.expiry_hours") or 24)),
              snapshot={"offer": _item_snapshot(o_rows, o_infos), "request": _item_snapshot(r_rows, r_infos)})
    db.add(t)
    await db.flush()
    for side, rows in (("offer", o_rows), ("request", r_rows)):
        for r in rows:
            db.add(TradeItem(trade_id=t.id, side=side, instance_id=r.id, item_id=r.item_id))
    pub = trade_public(t, user, target)
    await feed_svc.notify_user(db, target.id, "trade_offer", f"{user.display_name} からTradeの提案", t.message or "", {"trade_id": t.id})
    queue_event(db, "user", "trade", {"action": "created", "trade": pub}, user_id=target.id)
    queue_event(db, "user", "trade", {"action": "created", "trade": pub}, user_id=user.id)
    return pub


async def list_trades(db: AsyncSession, user_id: int, status: str | None = None) -> list[dict[str, Any]]:
    q = select(Trade).where(or_(Trade.from_user_id == user_id, Trade.to_user_id == user_id))
    if status:
        q = q.where(Trade.status == status)
    rows = (await db.execute(q.order_by(Trade.id.desc()).limit(100))).scalars().all()
    uids = {r.from_user_id for r in rows} | {r.to_user_id for r in rows}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all()} if uids else {}
    return [trade_public(t, users.get(t.from_user_id), users.get(t.to_user_id)) for t in rows]


async def _lock_trade(db: AsyncSession, trade_id: int) -> Trade:
    t = (await db.execute(select(Trade).where(Trade.id == trade_id).with_for_update())).scalar_one_or_none()
    if t is None:
        raise NotFound("Tradeが見つかりません")
    return t


async def respond(db: AsyncSession, user_id: int, trade_id: int, action: str, revision: int | None = None) -> dict[str, Any]:
    t = await _lock_trade(db, trade_id)
    if user_id not in (t.from_user_id, t.to_user_id):
        raise NotFound("Tradeが見つかりません")
    if t.status != "pending":
        raise Conflict("このTradeは既に終了しています", code="trade_closed")
    now = utcnow()
    if t.expires_at <= now:
        t.status = "expired"
        raise Conflict("このTradeは期限切れです", code="trade_expired")
    if action == "cancel":
        if user_id != t.from_user_id:
            raise Forbidden("送信者のみキャンセルできます", code="not_sender")
        t.status = "cancelled"
    elif action == "decline":
        if user_id != t.to_user_id:
            raise Forbidden("受信者のみ拒否できます", code="not_recipient")
        t.status = "declined"
    elif action == "accept":
        if user_id != t.to_user_id:
            raise Forbidden("受信者のみ承認できます", code="not_recipient")
        if revision is None or revision != t.revision:
            raise Conflict("Tradeの内容が変更されています。再確認してください", code="revision_mismatch")
        await _execute(db, t)
    else:
        raise AppError("不正な操作です", code="invalid_action")
    t.updated_at = now
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_([t.from_user_id, t.to_user_id])))).scalars().all()}
    pub = trade_public(t, users.get(t.from_user_id), users.get(t.to_user_id))
    for uid in (t.from_user_id, t.to_user_id):
        queue_event(db, "user", "trade", {"action": action, "trade": pub}, user_id=uid)
    other = t.from_user_id if user_id == t.to_user_id else t.to_user_id
    labels = {"accept": "成立しました", "decline": "拒否されました", "cancel": "キャンセルされました"}
    await feed_svc.notify_user(db, other, f"trade_{action}", f"Trade #{t.id} が{labels[action]}", "", {"trade_id": t.id})
    return pub


async def _execute(db: AsyncSession, t: Trade) -> None:
    users = await lock_users_ordered(db, t.from_user_id, t.to_user_id)
    a, b = users[t.from_user_id], users[t.to_user_id]
    for u in (a, b):
        if u.status != "active":
            raise Forbidden("取引できないアカウントが含まれています", code="account_restricted")
        users_svc.require_feature(u, "trade", "features.trade_enabled")
    if a.stardust < t.offer_stardust or b.stardust < t.request_stardust:
        raise AppError("Stardustが不足しているためTradeできません", code="insufficient_funds")
    o_rows, o_infos = await _validate_items(db, a.id, list(t.offer_items), "Trade", lock=True)
    r_rows, r_infos = await _validate_items(db, b.id, list(t.request_items), "Trade", lock=True)
    a.stardust += t.request_stardust - t.offer_stardust
    b.stardust += t.offer_stardust - t.request_stardust
    for r in o_rows:
        r.owner_id, r.favorite, r.locked = b.id, False, False
    for r in r_rows:
        r.owner_id, r.favorite, r.locked = a.id, False, False
    item_ids = [r.item_id for r in o_rows + r_rows]
    if item_ids:
        for iid in set(item_ids):
            await db.execute(update(Item).where(Item.id == iid).values(trade_count=Item.trade_count + item_ids.count(iid)))
    t.status = "accepted"
    t.completed_at = utcnow()
    from .rolls import collection_upsert

    snap = get_registry().snap
    for user, rows, infos in ((b, o_rows, o_infos), (a, r_rows, r_infos)):
        stats = await users_svc.lock_stats(db, user.id)
        stats.trades += 1
        events = [ProgressEvent("trade")]
        for r in rows:
            if await collection_upsert(db, user.id, r.item_id, 1) and r.item_id in snap.collectible_ids:
                stats.discovered_count += 1
            events.append(ProgressEvent("obtain", params={"tier": infos[r.item_id].get("tier", 1), "item_key": infos[r.item_id].get("key")}))
        await progress_svc.handle_events(db, user, stats, events)


async def expire_trades(db: AsyncSession) -> int:
    res = await db.execute(update(Trade).where(Trade.status == "pending", Trade.expires_at <= utcnow()).values(status="expired"))
    await db.commit()
    return res.rowcount or 0


# ---------------------------------------------------------------------------
# Gifts
# ---------------------------------------------------------------------------
async def send_gift(db: AsyncSession, user_id: int, to_user_id: int, instance_ids: list[int], stardust: int, message: str | None) -> dict[str, Any]:
    reg = get_registry()
    if to_user_id == user_id:
        raise AppError("自分には贈れません", code="self_gift")
    if stardust < 0 or (not instance_ids and stardust == 0) or len(instance_ids) > 10:
        raise AppError("贈り物の内容が不正です", code="invalid_gift")
    users = await lock_users_ordered(db, user_id, to_user_id)
    sender, target = users.get(user_id), users.get(to_user_id)
    if sender is None or target is None or target.status == "banned":
        raise NotFound("相手が見つかりません")
    users_svc.require_feature(sender, "gift", "features.gift_enabled")
    if sender.status != "active":
        raise Forbidden("このアカウントでは贈れません", code="account_restricted")
    now = utcnow()
    last = (await db.execute(select(func.max(Gift.created_at)).where(Gift.from_user_id == sender.id))).scalar_one_or_none()
    cd = int(reg.setting("gift.cooldown_seconds") or 0)
    if last and (now - last).total_seconds() < cd:
        wait = cd - (now - last).total_seconds()
        raise AppError(f"Giftのクールダウン中です（あと{int(wait) + 1}秒）", code="gift_cooldown", status_code=429,
                       data={"retry_after": wait})
    today = int((await db.execute(select(func.count()).select_from(Gift).where(Gift.from_user_id == sender.id,
                                                                              Gift.created_at > now - timedelta(days=1)))).scalar_one())
    if today >= int(reg.setting("gift.daily_limit") or 20):
        raise AppError("本日のGift上限に達しています", code="gift_limit")
    if sender.stardust < stardust:
        raise AppError("Stardustが足りません", code="insufficient_funds")
    rows, infos = await _validate_items(db, sender.id, instance_ids, "Gift", lock=True)
    sender.stardust -= stardust
    target.stardust += stardust
    for r in rows:
        r.owner_id, r.favorite, r.locked = target.id, False, False
    snapshot = _item_snapshot(rows, infos)
    if rows:
        for r in rows:
            db.add(Gift(from_user_id=sender.id, to_user_id=target.id, instance_id=r.id, item_id=r.item_id, stardust=0,
                        message=(message or "")[:200] or None, snapshot={"item": infos[r.item_id]}))
    if stardust:
        db.add(Gift(from_user_id=sender.id, to_user_id=target.id, stardust=stardust, message=(message or "")[:200] or None, snapshot={}))
    from .rolls import collection_upsert

    snap = get_registry().snap
    tstats = await users_svc.lock_stats(db, target.id)
    for r in rows:
        if await collection_upsert(db, target.id, r.item_id, 1) and r.item_id in snap.collectible_ids:
            tstats.discovered_count += 1
    sstats = await users_svc.lock_stats(db, sender.id)
    sstats.gifts_sent += 1
    res = await progress_svc.handle_events(db, sender, sstats, [ProgressEvent("gift")])
    names = ", ".join(s["item"]["name"] for s in snapshot[:3]) + ("..." if len(snapshot) > 3 else "")
    body = " / ".join(x for x in [names, f"{stardust:,} Stardust" if stardust else ""] if x)
    await feed_svc.notify_user(db, target.id, "gift", f"{sender.display_name} から贈り物が届きました", body,
                               {"from": sender.id, "items": snapshot, "stardust": stardust, "message": message})
    return {"ok": True, "items": snapshot, "stardust": stardust, "balance": sender.stardust, "progress": res.public()}


async def gift_history(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Gift).where(or_(Gift.from_user_id == user_id, Gift.to_user_id == user_id))
                             .order_by(Gift.id.desc()).limit(100))).scalars().all()
    return [{"id": g.id, "from": g.from_user_id, "to": g.to_user_id, "item": g.snapshot.get("item"), "stardust": g.stardust,
             "message": g.message, "created_at": g.created_at.isoformat()} for g in rows]
