"""Try it before you sign up: a handful of real rolls with no account."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..content.registry import get_registry
from ..core.errors import AppError
from ..core.ratelimit import limiter
from ..core.security import client_ip
from ..db import get_db
from ..services import guest as guest_svc

router = APIRouter(prefix="/api/guest", tags=["guest"])


def _enabled() -> None:
    reg = get_registry()
    if reg.setting("features.guest_rolls") is False:
        raise AppError("お試しRollは現在停止しています", code="guest_disabled", status_code=403)
    if not reg.setting("features.registration_open"):
        raise AppError("現在、新規登録を停止しています", code="registration_closed", status_code=403)


def _set_cookie(resp: JSONResponse, value: str) -> None:
    s = get_settings()
    resp.set_cookie(guest_svc.COOKIE, value, max_age=guest_svc.COOKIE_TTL, httponly=True,
                    secure=s.cookie_secure, samesite="lax", path=s.url("/"))


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    enabled = get_registry().setting("features.guest_rolls") is not False
    st = guest_svc.status(request.cookies.get(guest_svc.COOKIE))
    st["enabled"] = bool(enabled and get_registry().setting("features.registration_open"))
    return st


@router.post("/roll")
async def guest_roll(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    _enabled()
    limiter.check("guest_roll", client_ip(request))
    body, cookie = await guest_svc.roll(db, request.cookies.get(guest_svc.COOKIE))
    resp = JSONResponse(body)
    _set_cookie(resp, cookie)
    return resp
