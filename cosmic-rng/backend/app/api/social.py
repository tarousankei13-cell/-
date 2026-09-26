"""Friends and guilds — the social layer.

Everything is decided server-side; the client only asks. Rate limiting rides
on the same require_user scopes the rest of the API uses.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import idempotency
from ..core.pubsub import commit_and_publish
from ..core.security import Principal, require_user
from ..db import get_db
from ..services import social as social_svc
from ..services import users as users_svc

router = APIRouter(prefix="/api", tags=["social"])

USER = Depends(require_user("api"))
USER_ANY = Depends(require_user("api", allow_frozen=True, allow_maintenance=True))
WRITE = Depends(require_user("write"))


# ---------------------------------------------------------------------------
# Friends
# ---------------------------------------------------------------------------
@router.get("/friends")
async def friends(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await social_svc.friend_list(db, principal.user.id)


@router.get("/friends/activity")
async def friends_activity(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"activity": await social_svc.friend_activity(db, principal.user.id)}


class FriendBody(BaseModel):
    user_id: int


@router.post("/friends/request")
async def friend_request(body: FriendBody, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await social_svc.request_friend(db, principal.user.id, body.user_id)
    await commit_and_publish(db)
    return res


class RespondBody(BaseModel):
    user_id: int
    accept: bool


@router.post("/friends/respond")
async def friend_respond(body: RespondBody, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await social_svc.respond_friend(db, principal.user.id, body.user_id, body.accept)
    await commit_and_publish(db)
    return res


@router.post("/friends/remove")
async def friend_remove(body: FriendBody, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await social_svc.remove_friend(db, principal.user.id, body.user_id)
    await db.commit()
    return res


# ---------------------------------------------------------------------------
# Guilds
# ---------------------------------------------------------------------------
@router.get("/guilds")
async def guilds(q: str = Query(default="", max_length=32), principal: Principal = USER_ANY,
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"guilds": await social_svc.list_guilds(db, q), "terms": social_svc.bonus_terms()}


@router.get("/guilds/mine")
async def guild_mine(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await social_svc.my_guild(db, principal.user.id)
    res["terms"] = social_svc.bonus_terms()
    return res


@router.get("/guilds/board")
async def guilds_board(principal: Principal = USER_ANY, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"board": await social_svc.guild_board(db)}


class GuildCreateBody(BaseModel):
    name: str = Field(min_length=2, max_length=24)
    tag: str = Field(min_length=2, max_length=5)
    description: str = Field(default="", max_length=200)


@router.post("/guilds/create")
async def guild_create(body: GuildCreateBody, request: Request, principal: Principal = WRITE,
                       db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    async def op() -> dict[str, Any]:
        await users_svc.lock_user(db, principal.user.id)
        return await social_svc.create_guild(db, principal.user.id, body.name, body.tag.upper(), body.description)

    return await idempotency.run(db, principal.user.id, request, "guild_create", op)


class GuildJoinBody(BaseModel):
    guild_id: int


@router.post("/guilds/join")
async def guild_join(body: GuildJoinBody, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await social_svc.join_guild(db, principal.user.id, body.guild_id)
    await db.commit()
    return res


@router.post("/guilds/leave")
async def guild_leave(principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await social_svc.leave_guild(db, principal.user.id)
    await db.commit()
    return res


class GuildKickBody(BaseModel):
    user_id: int


@router.post("/guilds/kick")
async def guild_kick(body: GuildKickBody, principal: Principal = WRITE, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    res = await social_svc.kick_member(db, principal.user.id, body.user_id)
    await commit_and_publish(db)
    return res
