"""Market, trade, gift and artifact endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import idempotency
from ..core.pubsub import commit_and_publish, publish_queued
from ..core.security import Principal, require_user
from ..db import get_db
from ..services import artifacts as artifacts_svc
from ..services import equipment as equipment_svc
from ..services import market as market_svc
from ..services import trades as trades_svc
from ..services import users as users_svc

router = APIRouter(prefix="/api", tags=["economy"])
READ = Depends(require_user("api", allow_frozen=True, allow_maintenance=True))
WRITE = Depends(require_user("write"))


async def _idem(db: AsyncSession, principal: Principal, request: Request, name: str, op: Any) -> dict[str, Any]:
    result = await idempotency.run(db, principal.user.id, request, name, op)
    await publish_queued(db)
    return result


# --- Market ----------------------------------------------------------------
@router.get("/market")
async def market(principal: Principal = READ, db: AsyncSession = Depends(get_db), q: str = Query(default="", max_length=64),
                 rarity: str | None = Query(default=None, max_length=32), item_id: int | None = None,
                 sort: str = Query(default="newest", pattern="^(newest|price_asc|price_desc|rarity)$"),
                 page: int = Query(default=1, ge=1, le=10000)) -> dict[str, Any]:
    data = await market_svc.browse(db, q=q, rarity=rarity, item_id=item_id, sort=sort, page=page)
    data["unlocked"] = users_svc.feature_unlocked(principal.user, "market")
    data["unlock_level"] = users_svc.feature_level("market")
    return data


@router.get("/market/mine")
async def market_mine(principal: Principal = READ, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"listings": await market_svc.my_listings(db, principal.user.id)}


@router.get("/market/history/{item_id}")
async def market_history(item_id: int, principal: Principal = READ, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await market_svc.price_history(db, item_id)


class ListBody(BaseModel):
    instance_id: int
    price: int = Field(ge=1, le=9_000_000_000_000_000)


@router.post("/market/list")
async def market_list(body: ListBody, request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await _idem(db, principal, request, "market_list",
                       lambda: market_svc.create_listing(db, principal.user.id, body.instance_id, body.price))


class ListingBody(BaseModel):
    listing_id: int


@router.post("/market/cancel")
async def market_cancel(body: ListingBody, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await market_svc.cancel_listing(db, principal.user.id, body.listing_id)
    await commit_and_publish(db)
    return result


@router.post("/market/buy")
async def market_buy(body: ListingBody, request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await _idem(db, principal, request, "market_buy", lambda: market_svc.buy(db, principal.user.id, body.listing_id))


# --- Trade -----------------------------------------------------------------
@router.get("/trades")
async def trades(principal: Principal = READ, db: AsyncSession = Depends(get_db),
                 status: str | None = Query(default=None, pattern="^(pending|accepted|declined|cancelled|expired)$")) -> dict[str, Any]:
    return {"trades": await trades_svc.list_trades(db, principal.user.id, status),
            "unlocked": users_svc.feature_unlocked(principal.user, "trade"), "unlock_level": users_svc.feature_level("trade")}


class TradeCreateBody(BaseModel):
    to_user_id: int
    offer_items: list[int] = Field(default_factory=list, max_length=200)
    offer_stardust: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    request_items: list[int] = Field(default_factory=list, max_length=200)
    request_stardust: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    message: str | None = Field(default=None, max_length=200)


@router.post("/trades")
async def trade_create(body: TradeCreateBody, request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await _idem(db, principal, request, "trade_create", lambda: trades_svc.create_trade(
        db, principal.user.id, body.to_user_id, body.offer_items, body.offer_stardust, body.request_items, body.request_stardust, body.message))


class TradeRespondBody(BaseModel):
    action: str = Field(pattern="^(accept|decline|cancel)$")
    revision: int | None = None


@router.post("/trades/{trade_id}/respond")
async def trade_respond(trade_id: int, body: TradeRespondBody, request: Request, principal: Principal = WRITE,
                        db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await _idem(db, principal, request, "trade_respond",
                       lambda: trades_svc.respond(db, principal.user.id, trade_id, body.action, body.revision))


@router.get("/partner-inventory/{user_id}")
async def partner_inventory(user_id: int, principal: Principal = READ, db: AsyncSession = Depends(get_db),
                            q: str = Query(default="", max_length=64)) -> dict[str, Any]:
    """Tradeable instances of another player (respecting their privacy setting)."""
    from sqlalchemy import select

    from ..models import ItemInstance, User
    from ..services import inventory as inv_svc
    from ..services import user_settings as settings_svc

    target = await db.get(User, user_id)
    if target is None or target.status == "banned":
        from ..core.errors import NotFound

        raise NotFound("ユーザーが見つかりません")
    settings = await settings_svc.load(db, target.id)
    if not settings.privacy.show_inventory:
        return {"instances": [], "private": True, "user": users_svc.user_brief(target)}
    rows = (await db.execute(select(ItemInstance).where(ItemInstance.owner_id == target.id, ItemInstance.state == "owned",
                                                        ItemInstance.locked.is_(False)).order_by(ItemInstance.id.desc()).limit(500))).scalars().all()
    infos = await inv_svc.items_info(db, {r.item_id for r in rows})
    out = [inv_svc.instance_public(r, infos.get(r.item_id)) for r in rows
           if infos.get(r.item_id, {}).get("tradeable", True) and infos.get(r.item_id, {}).get("kind") != "admin_artifact"]
    if q:
        out = [x for x in out if q.lower() in x["item"]["name"].lower()]
    out.sort(key=lambda x: (-x["item"]["tier"], -(x["item"]["odds"] or 0)))
    return {"instances": out[:300], "private": False, "user": users_svc.user_brief(target)}


# --- Gift ------------------------------------------------------------------
class GiftBody(BaseModel):
    to_user_id: int
    instance_ids: list[int] = Field(default_factory=list, max_length=10)
    stardust: int = Field(default=0, ge=0, le=9_000_000_000_000_000)
    message: str | None = Field(default=None, max_length=200)


@router.post("/gifts")
async def gift(body: GiftBody, request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await _idem(db, principal, request, "gift", lambda: trades_svc.send_gift(
        db, principal.user.id, body.to_user_id, body.instance_ids, body.stardust, body.message))


@router.get("/gifts")
async def gifts(principal: Principal = READ, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"gifts": await trades_svc.gift_history(db, principal.user.id)}


# --- Artifacts ---------------------------------------------------------------
@router.get("/artifacts")
async def my_artifacts(principal: Principal = READ, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"artifacts": await artifacts_svc.my_artifacts(db, principal.user.id, principal.admin_mode)}


class ArtifactUseBody(BaseModel):
    instance_id: int
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/artifacts/use")
async def artifact_use(body: ArtifactUseBody, request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    ua = request.headers.get("user-agent")
    return await _idem(db, principal, request, "artifact_use", lambda: artifacts_svc.use(db, principal, body.instance_id, body.params, ua))


class ArtifactEquipBody(BaseModel):
    instance_id: int | None = None


@router.post("/artifacts/equip")
async def artifact_equip(body: ArtifactEquipBody, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if body.instance_id is None:
        await equipment_svc.unequip(db, principal.user, equipment_svc.ARTIFACT_SLOT)
    else:
        await equipment_svc.equip_artifact(db, principal.user, body.instance_id)
    await db.commit()
    return {"ok": True}
