"""Retention features: login bonus, events, season pass, shards, sets,
prestige and the fortune report. Server decides everything; claims are
idempotent."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import idempotency
from ..core.pubsub import commit_and_publish
from ..core.security import Principal, require_user
from ..db import get_db
from ..services import engagement as engagement_svc
from ..services import mastery as mastery_svc
from ..services import users as users_svc

router = APIRouter(prefix="/api", tags=["engage"])

USER = Depends(require_user("api"))
USER_ANY = Depends(require_user("api", allow_frozen=True, allow_maintenance=True))
WRITE = Depends(require_user("write"))


# ---------------------------------------------------------------------------
# Login bonus
# ---------------------------------------------------------------------------
@router.get("/daily")
async def daily(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await engagement_svc.daily_status(db, principal.user.id)
    await db.commit()  # lock_stats may create the stats row
    return res


@router.post("/daily/claim")
async def daily_claim(request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        await users_svc.lock_user(db, principal.user.id)
        return await engagement_svc.claim_daily(db, principal.user.id)

    return await idempotency.run(db, principal.user.id, request, "daily_claim", op)


# ---------------------------------------------------------------------------
# Events (public view; luck values, community goals)
# ---------------------------------------------------------------------------
@router.get("/events")
async def events(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await engagement_svc.public_events(db)


# ---------------------------------------------------------------------------
# Season pass
# ---------------------------------------------------------------------------
@router.get("/season/pass")
async def season_pass(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await engagement_svc.pass_state(db, principal.user.id)


class PassClaimBody(BaseModel):
    tier: int = Field(ge=0, le=100)


@router.post("/season/pass/claim")
async def season_pass_claim(body: PassClaimBody, request: Request, principal: Principal = WRITE,
                            db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        await users_svc.lock_user(db, principal.user.id)
        return await engagement_svc.claim_pass_tier(db, principal.user.id, body.tier)

    return await idempotency.run(db, principal.user.id, request, "pass_claim", op)


# ---------------------------------------------------------------------------
# Star shards
# ---------------------------------------------------------------------------
@router.get("/shards")
async def shards(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await mastery_svc.shards_state(db, principal.user.id)
    await db.commit()
    return res


@router.post("/shards/convert")
async def shards_convert(request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        await users_svc.lock_user(db, principal.user.id)
        return await mastery_svc.convert_duplicates(db, principal.user.id)

    return await idempotency.run(db, principal.user.id, request, "shards_convert", op)


class ExchangeBody(BaseModel):
    key: str = Field(min_length=1, max_length=64)


@router.post("/shards/exchange")
async def shards_exchange(body: ExchangeBody, request: Request, principal: Principal = WRITE,
                          db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        await users_svc.lock_user(db, principal.user.id)
        return await mastery_svc.exchange_shards(db, principal.user.id, body.key)

    return await idempotency.run(db, principal.user.id, request, "shards_exchange", op)


# ---------------------------------------------------------------------------
# Collection sets
# ---------------------------------------------------------------------------
@router.get("/sets")
async def sets(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await mastery_svc.sets_state(db, principal.user.id)


class SetClaimBody(BaseModel):
    set_key: str = Field(min_length=1, max_length=64)


@router.post("/sets/claim")
async def sets_claim(body: SetClaimBody, request: Request, principal: Principal = WRITE,
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        await users_svc.lock_user(db, principal.user.id)
        return await mastery_svc.claim_set(db, principal.user.id, body.set_key)

    return await idempotency.run(db, principal.user.id, request, "set_claim", op)


# ---------------------------------------------------------------------------
# Prestige
# ---------------------------------------------------------------------------
@router.get("/prestige")
async def prestige(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await mastery_svc.prestige_state(db, principal.user.id)
    await db.commit()
    return res


@router.post("/prestige/ascend")
async def prestige_ascend(request: Request, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        await users_svc.lock_user(db, principal.user.id)
        return await mastery_svc.do_prestige(db, principal.user.id)

    result = await idempotency.run(db, principal.user.id, request, "prestige", op)
    from ..core.pubsub import publish_queued

    await publish_queued(db)
    return result


# ---------------------------------------------------------------------------
# Fortune report
# ---------------------------------------------------------------------------
@router.get("/fortune")
async def fortune(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await mastery_svc.fortune_report(db, principal.user.id)
