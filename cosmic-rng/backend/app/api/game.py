"""Player-facing game API."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..content.registry import get_registry
from ..core.errors import AppError
from ..core import idempotency
from ..core.pubsub import commit_and_publish
from ..core.security import Principal, optional_principal, require_user
from ..db import get_db
from ..models import Achievement, UserAchievement, UserQuest
from ..rng.engine import RNG_VERSION
from ..services import crafting as crafting_svc
from ..services import effects as effects_svc
from ..services import equipment as equipment_svc
from ..services import feed as feed_svc
from ..services import inventory as inv_svc
from ..services import profile as profile_svc
from ..services import progress as progress_svc
from ..services import rankings as rankings_svc
from ..services import rolls as rolls_svc
from ..services import seasons as seasons_svc
from ..services import shop as shop_svc
from ..services import user_settings as settings_svc
from ..services import users as users_svc
from ..services.progress import ProgressEvent

router = APIRouter(prefix="/api", tags=["game"])

USER = Depends(require_user("api"))
USER_ANY = Depends(require_user("api", allow_frozen=True, allow_maintenance=True))


# ---------------------------------------------------------------------------
# Public config & bootstrap
# ---------------------------------------------------------------------------
@router.get("/config")
async def public_config() -> dict[str, Any]:
    reg = get_registry()
    snap = reg.snap
    s = get_settings()
    return {
        "rarities": [{"key": r.key, "name": r.name, "tier": r.tier, "min_odds": r.min_odds, "color": r.color, "color2": r.color2,
                      "cutscene": r.cutscene} for r in sorted(snap.rarities.values(), key=lambda r: r.tier)],
        "biomes": [{"key": b.key, "name": "???" if b.hidden else b.name, "kind": b.kind, "theme": b.theme, "hidden": b.hidden,
                    "states": [{"key": st.key, "name": st.name, "theme": st.theme} for st in b.states]}
                   for b in sorted(snap.biomes.values(), key=lambda b: b.sort_order) if b.kind != "admin"],
        "unlocks": reg.setting("progression.unlocks"),
        "special_interval": reg.setting("roll.special_interval"),
        "features": {k.split(".", 1)[1]: v for k, v in snap.settings.items() if k.startswith("features.") and isinstance(v, bool)},
        "maintenance": {"enabled": bool(reg.setting("features.maintenance_mode")), "message": reg.setting("features.maintenance_message")},
        "discord_login": bool(s.discord_client_id and s.discord_redirect_uri),
        "registration_open": bool(reg.setting("features.registration_open")),
        "rng_version": RNG_VERSION, "content_version": snap.version,
    }


@router.get("/me")
async def me(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    user = principal.user
    await progress_svc.ensure_quests(db, user)
    settings = await settings_svc.load(db, user.id)
    unread = await feed_svc.unread_count(db, user.id)
    await db.commit()
    return {
        # The account's own email is returned only here, to the owner of the session.
        "user": {**users_svc.user_brief(user), "email": user.email,
                 "discord_id": str(user.discord_id) if user.discord_id else None, "status": user.status,
                 "status_reason": user.status_reason, "stardust": user.stardust, "xp": user.xp, "level": user.level,
                 "roll_counter": user.roll_counter, "auto_roll": user.auto_roll_enabled, "title_key": user.title_key,
                 "background": user.profile_background, "created_at": user.created_at.isoformat()},
        "csrf": principal.session.csrf_token, "is_admin": principal.is_admin, "is_super_admin": principal.is_super_admin,
        "admin_mode": principal.admin_mode, "settings": settings.model_dump(), "unread": unread,
        "unlocked": users_svc.unlocked_features(user.level),
    }


# ---------------------------------------------------------------------------
# Roll
# ---------------------------------------------------------------------------
class RollBody(BaseModel):
    auto: bool = False


@router.post("/roll")
async def roll(body: RollBody, principal: Principal = Depends(require_user("roll")), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await rolls_svc.perform_roll(db, principal.user.id, auto=body.auto)
    await commit_and_publish(db)
    return result


@router.get("/state")
async def state(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await rolls_svc.get_state(db, principal.user.id)
    await commit_and_publish(db)
    return result


@router.post("/offline/claim")
async def offline_claim(principal: Principal = USER, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await rolls_svc.claim_offline(db, principal.user.id)
    await commit_and_publish(db)
    return result


class AutoBody(BaseModel):
    enabled: bool


@router.post("/roll/auto")
async def auto_roll(body: AutoBody, principal: Principal = USER, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await rolls_svc.set_auto_roll(db, principal.user.id, body.enabled)
    await db.commit()
    return result


@router.get("/roll/table")
async def roll_table(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await rolls_svc.table_preview(db, principal.user.id)
    await commit_and_publish(db)
    return result


@router.get("/roll/history")
async def roll_history(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db),
                       before_id: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
    from ..models import Roll, RollBatch

    q = select(Roll).where(Roll.user_id == principal.user.id)
    if before_id:
        q = q.where(Roll.id < before_id)
    rows = (await db.execute(q.order_by(Roll.id.desc()).limit(100))).scalars().all()
    infos = await inv_svc.items_info(db, {r.item_id for r in rows})
    batches = (await db.execute(select(RollBatch).where(RollBatch.user_id == principal.user.id).order_by(RollBatch.id.desc()).limit(10))).scalars().all()
    return {
        "rolls": [{"id": r.id, "number": r.roll_number, "item": infos.get(r.item_id), "tier": r.tier, "luck": r.luck, "biome": r.biome_key,
                   "final_chance": r.final_chance, "flags": r.flags, "created_at": r.created_at.isoformat()} for r in rows],
        "batches": [{"id": b.id, "kind": b.kind, "rolls": b.roll_count, "window_start": b.window_start.isoformat(),
                     "window_end": b.window_end.isoformat(), "summary": b.summary} for b in batches],
    }


# ---------------------------------------------------------------------------
# Boosts / effects
# ---------------------------------------------------------------------------
@router.get("/boosts")
async def boosts(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    active = await effects_svc.load_active(db, principal.user.id)
    return {"inventory": await effects_svc.boost_inventory(db, principal.user.id), "active": [effects_svc.public(e) for e in active]}


class UseBoostBody(BaseModel):
    boost_key: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, ge=1, le=50)


@router.post("/boosts/use")
async def use_boost(body: UseBoostBody, request: Request, principal: Principal = Depends(require_user("write")),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        user = await users_svc.lock_user(db, principal.user.id)
        rows = await effects_svc.use_boost(db, user.id, body.boost_key, body.quantity)
        stats = await users_svc.lock_stats(db, user.id)
        stats.boosts_used += body.quantity
        res = await progress_svc.handle_events(db, user, stats, [ProgressEvent("boost_use", count=body.quantity)])
        return {"activated": [effects_svc.public(r) for r in rows], "progress": res.public()}

    result = await idempotency.run(db, principal.user.id, request, "boost_use", op)
    return result


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
@router.get("/inventory")
async def inventory(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db),
                    q: str = Query(default="", max_length=64), sort: str = Query(default="rarity:desc", max_length=80),
                    rarity: list[str] = Query(default=[]), kind: list[str] = Query(default=[]), favorites: bool = False,
                    page: int = Query(default=1, ge=1, le=10000), per_page: int = Query(default=60, ge=1, le=200)) -> dict[str, Any]:
    data = await inv_svc.list_grouped(db, principal.user.id, q=q, sort=sort, rarities=rarity, kinds=kind, favorites_only=favorites,
                                      page=page, per_page=per_page)
    data["capacity"] = await inv_svc.capacity(db, principal.user.id)
    data["count"] = await inv_svc.count_instances(db, principal.user.id)
    return data


@router.get("/inventory/item/{item_id}")
async def inventory_instances(item_id: int, principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db),
                              page: int = Query(default=1, ge=1)) -> dict[str, Any]:
    return await inv_svc.list_instances(db, principal.user.id, item_id, page=page)


class SellBody(BaseModel):
    instance_ids: list[int] = Field(default_factory=list, max_length=5000)


async def _sell_ids(db: AsyncSession, user: Any, ids: list[int]) -> dict[str, Any]:
    from ..rng.modifiers import passive_sum

    equips, _ = await equipment_svc.load_equipped(db, user.id)
    rows = await inv_svc.get_owned_instances(db, user.id, ids, lock=False)
    infos = await inv_svc.items_info(db, {r.item_id for r in rows})
    total, n = await inv_svc.sell_instances(db, user.id, ids, passive_sum(equips, "sell_bonus"))
    user.stardust += total
    stats = await users_svc.lock_stats(db, user.id)
    stats.items_sold += n
    stats.stardust_earned += total
    max_value = max((int(i.get("sell_value", 0)) for i in infos.values()), default=0)
    res = await progress_svc.handle_events(db, user, stats, [ProgressEvent("sell", count=n, params={"max_value": max_value}),
                                                             ProgressEvent("earn", params={"amount": total})])
    return {"sold": n, "earned": total, "stardust": user.stardust, "progress": res.public()}


@router.post("/inventory/sell")
async def sell(body: SellBody, request: Request, principal: Principal = Depends(require_user("write")),
               db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        user = await users_svc.lock_user(db, principal.user.id)
        return await _sell_ids(db, user, body.instance_ids)

    return await idempotency.run(db, principal.user.id, request, "sell", op)


class BulkSellBody(BaseModel):
    keep: int = Field(default=1, ge=0, le=1000)
    max_tier: int = Field(default=2, ge=1, le=7)
    item_ids: list[int] = Field(default_factory=list, max_length=500)
    preview: bool = False


@router.post("/inventory/sell-bulk")
async def sell_bulk(body: BulkSellBody, request: Request, principal: Principal = Depends(require_user("write")),
                    db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if body.preview:
        ids = await inv_svc.select_bulk_sell(db, principal.user.id, keep=body.keep, max_tier=body.max_tier, item_ids=body.item_ids or None)
        rows = await inv_svc.get_owned_instances(db, principal.user.id, ids, lock=False) if ids else []
        infos = await inv_svc.items_info(db, {r.item_id for r in rows}) if rows else {}
        return {"count": len(ids), "estimated": sum(int(infos[r.item_id].get("sell_value", 0)) for r in rows)}

    async def op() -> dict[str, Any]:
        user = await users_svc.lock_user(db, principal.user.id)
        ids = await inv_svc.select_bulk_sell(db, user.id, keep=body.keep, max_tier=body.max_tier, item_ids=body.item_ids or None)
        if not ids:
            return {"sold": 0, "earned": 0, "stardust": user.stardust}
        return await _sell_ids(db, user, ids)

    return await idempotency.run(db, principal.user.id, request, "sell_bulk", op)


class FlagsBody(BaseModel):
    instance_ids: list[int] = Field(min_length=1, max_length=2000)
    locked: bool | None = None
    favorite: bool | None = None


@router.post("/inventory/flags")
async def set_flags(body: FlagsBody, principal: Principal = Depends(require_user("write")), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    n = await inv_svc.set_flags(db, principal.user.id, body.instance_ids, locked=body.locked, favorite=body.favorite)
    await db.commit()
    return {"updated": n}


class ItemFavBody(BaseModel):
    item_id: int
    favorite: bool


@router.post("/inventory/item-favorite")
async def item_favorite(body: ItemFavBody, principal: Principal = Depends(require_user("write")), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await inv_svc.set_item_favorite(db, principal.user.id, body.item_id, body.favorite)
    await db.commit()
    return {"ok": True}


@router.get("/items/{item_id}")
async def item_detail(item_id: int, principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await profile_svc.item_detail(db, item_id, principal.user.id)


# ---------------------------------------------------------------------------
# Collection & biomes
# ---------------------------------------------------------------------------
@router.get("/collection")
async def collection(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..models import ActiveEffect
    from ..core.timeutil import utcnow

    reveal = (await db.execute(select(func.count()).select_from(ActiveEffect).where(
        ActiveEffect.user_id == principal.user.id, ActiveEffect.effect_type == "reveal_secrets", ActiveEffect.expires_at > utcnow()))).scalar_one() > 0
    return await profile_svc.collection_book(db, principal.user.id, reveal_hidden=bool(reveal))


@router.get("/biomes")
async def biomes(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..models import UserStats

    snap = get_registry().snap
    stats = await db.get(UserStats, principal.user.id)
    seen = set(stats.biomes_seen or []) if stats else set()
    out = []
    for b in sorted(snap.biomes.values(), key=lambda x: x.sort_order):
        if b.kind == "admin" and b.key not in seen and not principal.is_admin:
            continue
        known = b.kind == "default" or b.key in seen or not b.hidden
        exclusive = [i for i in snap.items.values() if i.biome_keys and b.key in i.biome_keys and i.is_active and i.rollable]
        out.append({
            **(b.public() if known else {"key": b.key, "name": "???", "kind": b.kind, "hidden": True, "theme": {}, "states": []}),
            "seen": b.key in seen or b.kind == "default", "locked": principal.user.level < b.min_level,
            "exclusive_items": [{"id": i.id, "name": i.name if (b.key in seen and not i.hidden) else "???", "rarity": i.rarity_key,
                                 "odds": i.odds if not i.hidden else None} for i in sorted(exclusive, key=lambda i: i.odds or 0)] if known else [],
        })
    return {"biomes": out}


# ---------------------------------------------------------------------------
# Equipment & crafting
# ---------------------------------------------------------------------------
@router.get("/equipment")
async def equipment(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    _, visuals = await equipment_svc.load_equipped(db, principal.user.id)
    return {"items": await equipment_svc.list_for_user(db, principal.user.id), "equipped": visuals,
            "slots": equipment_svc.SLOTS, "relic_unlocked": users_svc.feature_unlocked(principal.user, "relic_slot")}


class EquipBody(BaseModel):
    instance_id: int


@router.post("/equipment/equip")
async def equip(body: EquipBody, principal: Principal = Depends(require_user("write")), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    user = await users_svc.lock_user(db, principal.user.id)
    inst = await equipment_svc.equip(db, user, body.instance_id)
    stats = await users_svc.lock_stats(db, user.id)
    res = await progress_svc.handle_events(db, user, stats, [ProgressEvent("equip")])
    await commit_and_publish(db)
    return {"equipped": equipment_svc.public(inst), "progress": res.public()}


class UnequipBody(BaseModel):
    slot: Literal["gauntlet", "core", "relic", "artifact"]


@router.post("/equipment/unequip")
async def unequip(body: UnequipBody, principal: Principal = Depends(require_user("write")), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await equipment_svc.unequip(db, principal.user, body.slot)
    await db.commit()
    return {"ok": True}


class EquipSellBody(BaseModel):
    instance_ids: list[int] = Field(min_length=1, max_length=200)


@router.post("/equipment/sell")
async def equipment_sell(body: EquipSellBody, request: Request, principal: Principal = Depends(require_user("write")),
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        user = await users_svc.lock_user(db, principal.user.id)
        total = await equipment_svc.sell(db, user, body.instance_ids)
        user.stardust += total
        return {"earned": total, "stardust": user.stardust}

    return await idempotency.run(db, principal.user.id, request, "equipment_sell", op)


class EquipLockBody(BaseModel):
    instance_id: int
    locked: bool


@router.post("/equipment/lock")
async def equipment_lock(body: EquipLockBody, principal: Principal = Depends(require_user("write")), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    inst = await equipment_svc.get_owned(db, principal.user.id, body.instance_id)
    inst.locked = body.locked
    await db.commit()
    return {"ok": True}


@router.get("/crafting")
async def crafting(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await crafting_svc.list_recipes(db, principal.user)


class CraftBody(BaseModel):
    recipe_key: str = Field(min_length=1, max_length=64)
    times: int = Field(default=1, ge=1, le=20)


@router.post("/crafting/craft")
async def craft(body: CraftBody, request: Request, principal: Principal = Depends(require_user("write")),
                db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        return await crafting_svc.craft(db, principal.user.id, body.recipe_key, body.times)

    result = await idempotency.run(db, principal.user.id, request, "craft", op)
    from ..core.pubsub import publish_queued

    await publish_queued(db)
    return result


class ExperimentBody(BaseModel):
    ingredients: list[dict[str, Any]] = Field(min_length=1, max_length=8)


@router.post("/crafting/experiment")
async def experiment(body: ExperimentBody, request: Request, principal: Principal = Depends(require_user("write")),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        return await crafting_svc.experiment(db, principal.user.id, body.ingredients)

    return await idempotency.run(db, principal.user.id, request, "experiment", op)


# ---------------------------------------------------------------------------
# Shop
# ---------------------------------------------------------------------------
@router.get("/shop")
async def shop(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await shop_svc.list_shops(db, principal.user)


class PurchaseBody(BaseModel):
    shop_item_key: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, ge=1, le=99)


@router.post("/shop/purchase")
async def purchase(body: PurchaseBody, request: Request, principal: Principal = Depends(require_user("write")),
                   db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        return await shop_svc.purchase(db, principal.user.id, body.shop_item_key, body.quantity)

    return await idempotency.run(db, principal.user.id, request, "purchase", op)


# ---------------------------------------------------------------------------
# Quests & achievements
# ---------------------------------------------------------------------------
@router.get("/quests")
async def quests(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..core.timeutil import next_reset, period_key, utcnow
    from ..models import ActiveEffect

    snap = get_registry().snap
    user = principal.user
    await progress_svc.ensure_quests(db, user)
    await db.commit()
    tz = str(get_registry().setting("quests.reset_timezone") or "Asia/Tokyo")
    today = period_key(utcnow(), tz)
    reveal = (await db.execute(select(func.count()).select_from(ActiveEffect).where(
        ActiveEffect.user_id == user.id, ActiveEffect.effect_type == "reveal_secrets", ActiveEffect.expires_at > utcnow()))).scalar_one() > 0
    rows = (await db.execute(select(UserQuest).where(UserQuest.user_id == user.id).order_by(UserQuest.id))).scalars().all()
    daily, chain, hidden = [], [], []
    for uq in rows:
        q = snap.quests_by_id.get(uq.quest_id)
        if q is None:
            continue
        entry = {"id": uq.id, "key": q["key"], "name": q["name"], "kind": q["kind"], "progress": uq.progress, "target": uq.target,
                 "status": uq.status, "rewards": uq.rewards, "chain_key": q["chain_key"], "chain_step": q["chain_step"],
                 "description": q["description"].replace("{target}", f"{uq.target:,}")}
        if q["kind"] == "daily":
            if uq.period_key == today:
                daily.append(entry)
        elif q["kind"] == "chain":
            if uq.status != "claimed":
                total = sum(1 for x in snap.quests.values() if x["chain_key"] == q["chain_key"])
                chain.append({**entry, "chain_total": total})
        else:
            if uq.status == "active" and not reveal:
                entry.update({"name": "???", "description": None, "progress": None, "target": None})
            entry["hint"] = q["hint"]
            hidden.append(entry)
    return {"daily": daily, "chains": chain, "hidden": hidden, "reset_at": next_reset(utcnow(), tz).isoformat(),
            "unlocked": users_svc.feature_unlocked(user, "quests"), "unlock_level": users_svc.feature_level("quests")}


class ClaimBody(BaseModel):
    user_quest_id: int


@router.post("/quests/claim")
async def claim(body: ClaimBody, request: Request, principal: Principal = Depends(require_user("write")),
                db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        user = await users_svc.lock_user(db, principal.user.id)
        return await progress_svc.claim_quest(db, user, body.user_quest_id)

    result = await idempotency.run(db, principal.user.id, request, "quest_claim", op)
    from ..core.pubsub import publish_queued

    await publish_queued(db)
    return result


@router.get("/achievements")
async def achievements(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..models import User

    snap = get_registry().snap
    mine = {r.achievement_id: r for r in (await db.execute(select(UserAchievement).where(UserAchievement.user_id == principal.user.id))).scalars().all()}
    rows = (await db.execute(select(Achievement))).scalars().all()
    firsts = {a.first_achiever_id for a in rows if a.first_achiever_id}
    names = {u.id: users_svc.user_brief(u) for u in (await db.execute(select(User).where(User.id.in_(firsts)))).scalars().all()} if firsts else {}
    live = {a.id: a for a in rows}
    out = []
    for a in sorted(snap.achievements.values(), key=lambda x: x["sort_order"]):
        if not a["is_active"]:
            continue
        got = mine.get(a["id"])
        la = live.get(a["id"])
        hide = a["hidden"] and got is None
        out.append({
            "key": a["key"], "name": "???" if hide else a["name"], "description": None if hide else a["description"],
            "hint": a["hint"] if hide else None, "category": a["category"], "tier": a["tier"], "hidden": a["hidden"],
            "achieved": got is not None, "achieved_at": got.achieved_at.isoformat() if got else None,
            "world_first": bool(got and got.world_first), "rewards": None if hide else a["rewards"],
            "achiever_count": la.achiever_count if la else 0,
            "first_achiever": names.get(la.first_achiever_id) if la and la.first_achiever_id else None,
        })
    return {"achievements": out, "achieved": len(mine), "total": len(out)}


# ---------------------------------------------------------------------------
# Feed, notifications, rankings, seasons
# ---------------------------------------------------------------------------
@router.get("/feed")
async def feed(principal: Principal | None = Depends(optional_principal), db: AsyncSession = Depends(get_db),
               limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return {"events": await feed_svc.recent(db, limit)}


@router.get("/notifications")
async def notifications(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"notifications": await feed_svc.list_notifications(db, principal.user.id), "unread": await feed_svc.unread_count(db, principal.user.id)}


class ReadBody(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=500)


@router.post("/notifications/read")
async def notifications_read(body: ReadBody, principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await feed_svc.mark_read(db, principal.user.id, body.ids)
    await db.commit()
    return {"ok": True}


@router.get("/rankings/{board}")
async def rankings(board: str, principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db),
                   metric: str = Query(default="points", pattern="^(points|rolls|best|firsts)$"),
                   season_id: int | None = None) -> dict[str, Any]:
    if board == "season":
        data = await rankings_svc.season_board(db, season_id, metric)
    else:
        data = await rankings_svc.board(db, board)
        data["me"] = await rankings_svc.user_rank(db, board, principal.user.id)
    return data


@router.get("/seasons")
async def seasons(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"seasons": await seasons_svc.list_seasons(db), "active": await seasons_svc.active_season_id(db)}


# ---------------------------------------------------------------------------
# Settings & profile
# ---------------------------------------------------------------------------
@router.get("/settings")
async def get_settings_(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..models import UserUnlock

    unlocks = {r[0] for r in (await db.execute(select(UserUnlock.unlock_key).where(UserUnlock.user_id == principal.user.id))).all()}
    return {"settings": (await settings_svc.load(db, principal.user.id)).model_dump(), "unlocks": sorted(unlocks)}


class SettingsBody(BaseModel):
    settings: dict[str, Any]


@router.put("/settings")
async def put_settings(body: SettingsBody, principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..models import UserUnlock

    current = await settings_svc.load(db, principal.user.id)
    merged = settings_svc.deep_merge(current.model_dump(), body.settings)
    try:
        new = settings_svc.PlayerSettings.model_validate(merged)
    except Exception as e:
        raise AppError("設定値が不正です", code="invalid_settings") from e
    unlocks = {r[0] for r in (await db.execute(select(UserUnlock.unlock_key).where(UserUnlock.user_id == principal.user.id))).all()}
    # Server-side enforcement of purchased features
    if new.roll.speed == "fast" and "fast_mode" not in unlocks:
        new.roll.speed = current.roll.speed if current.roll.speed != "fast" else "normal"
    if new.roll.speed == "ultra" and "ultra_fast" not in unlocks:
        new.roll.speed = "fast" if "fast_mode" in unlocks else "normal"
    if new.auto_skip.enabled:
        allowed = [t for u, t in (("auto_skip_100", 100), ("auto_skip_1000", 1000), ("auto_skip_10000", 10000)) if u in unlocks]
        if "auto_skip_custom" in unlocks:
            pass
        elif not allowed:
            new.auto_skip.enabled = False
        elif new.auto_skip.threshold not in allowed:
            new.auto_skip.threshold = max(allowed)
    if new.auto_delete.enabled and not users_svc.feature_unlocked(principal.user, "auto_delete"):
        new.auto_delete.enabled = False
    await settings_svc.save(db, principal.user.id, new)
    await db.commit()
    return {"settings": new.model_dump()}


@router.get("/profile/{user_id}")
async def profile(user_id: int, principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await profile_svc.get_profile(db, user_id, principal.user.id, principal.is_admin)


class ProfileBody(BaseModel):
    title_key: str | None = Field(default=None, max_length=64)
    clear_title: bool = False
    background: str | None = Field(default=None, max_length=64)
    badges: list[str] | None = Field(default=None, max_length=5)
    bio: str | None = Field(default=None, max_length=200)


@router.put("/profile")
async def update_profile(body: ProfileBody, principal: Principal = USER, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await profile_svc.update_profile(db, principal.user, title_key=body.title_key, background=body.background, badges=body.badges,
                                     bio=body.bio, clear_title=body.clear_title)
    await db.commit()
    return {"ok": True}


class ShowcaseBody(BaseModel):
    instance_ids: list[int | None] = Field(max_length=6)


@router.put("/profile/showcase")
async def showcase(body: ShowcaseBody, principal: Principal = USER, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await profile_svc.set_showcase(db, principal.user.id, body.instance_ids)
    await db.commit()
    return {"showcase": result}


@router.get("/users/search")
async def user_search(q: str = Query(min_length=1, max_length=32), principal: Principal = Depends(require_user("search")),
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..models import User

    stmt = select(User).where(User.status != "banned")
    if q.isdigit() and len(q) < 18:
        stmt = stmt.where((User.id == int(q)) | User.display_name.ilike(f"%{q}%"))
    else:
        stmt = stmt.where(User.display_name.ilike(f"%{inv_svc._escape_like(q)}%", escape="\\"))  # noqa: SLF001
    rows = (await db.execute(stmt.limit(20))).scalars().all()
    return {"users": [users_svc.user_brief(u) for u in rows]}

