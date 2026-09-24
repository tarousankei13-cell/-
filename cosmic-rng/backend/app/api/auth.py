"""Authentication: Discord OAuth2 (authorization code flow), sessions, admin mode."""
from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..content.registry import get_registry
from ..core.errors import AppError, Forbidden, NotFound
from ..core.pubsub import commit_and_publish, queue_event
from ..core.ratelimit import limiter
from ..core.security import (
    OAUTH_COOKIE,
    Principal,
    check_origin,
    clear_session_cookie,
    client_ip,
    create_session,
    optional_principal,
    require_user,
    set_session_cookie,
    sign_value,
    unsign_value,
)
from ..db import get_db
from ..services import audit, progress as progress_svc, user_settings as settings_svc, users as users_svc

log = logging.getLogger("cosmic.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _safe_next(n: str | None) -> str:
    # Only same-site relative paths are allowed as post-login redirects (no open redirect).
    if not n or not n.startswith("/") or n.startswith("//") or "\\" in n:
        return "/roll"
    return n[:200]


@router.get("/login")
async def login(request: Request, next: str | None = Query(default=None)) -> RedirectResponse:
    s = get_settings()
    limiter.check("auth", client_ip(request))
    if not s.discord_client_id or not s.discord_redirect_uri:
        raise AppError("Discordログインが設定されていません", code="oauth_not_configured", status_code=503)
    state = secrets.token_urlsafe(24)
    params = {"client_id": s.discord_client_id, "redirect_uri": s.discord_redirect_uri, "response_type": "code",
              "scope": "identify", "state": state, "prompt": "none"}
    resp = RedirectResponse(f"{s.discord_oauth_authorize_url}?{urlencode(params)}", status_code=302)
    resp.set_cookie(OAUTH_COOKIE, sign_value(f"{state}~{_safe_next(next)}", 600), max_age=600, httponly=True,
                    secure=s.cookie_secure, samesite="lax", path=s.url("/api/auth"))
    return resp


async def _exchange_code(code: str) -> dict[str, Any]:
    s = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await client.post(f"{s.discord_api_base}/oauth2/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": s.discord_redirect_uri,
        }, auth=(s.discord_client_id, s.discord_client_secret), headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tok.status_code != 200:
            log.warning("discord token exchange failed: %s %s", tok.status_code, tok.text[:300])
            raise AppError("Discord認証に失敗しました", code="oauth_failed", status_code=400)
        access = tok.json().get("access_token")
        if not access:
            raise AppError("Discord認証に失敗しました", code="oauth_failed", status_code=400)
        me = await client.get(f"{s.discord_api_base}/users/@me", headers={"Authorization": f"Bearer {access}"})
        if me.status_code != 200:
            raise AppError("Discordプロフィールの取得に失敗しました", code="oauth_failed", status_code=400)
        profile = me.json()
        if not str(profile.get("id", "")).isdigit():
            raise AppError("Discordプロフィールが不正です", code="oauth_failed", status_code=400)
        return profile


async def _finish_login(db: AsyncSession, request: Request, profile: dict[str, Any], next_url: str) -> RedirectResponse:
    await get_registry().ensure_fresh(db)
    user, created = await users_svc.create_or_update_from_discord(db, profile)
    if user.status == "banned":
        from ..core.timeutil import utcnow

        if not user.status_until or user.status_until > utcnow():
            await db.commit()
            return RedirectResponse(get_settings().url("/?error=banned"), status_code=302)
    await progress_svc.ensure_quests(db, user)
    token = await create_session(db, user, request)
    await db.commit()
    resp = RedirectResponse(get_settings().url(next_url if not created else "/roll?welcome=1"), status_code=302)
    set_session_cookie(resp, token)
    resp.delete_cookie(OAUTH_COOKIE, path=get_settings().url("/api/auth"))
    return resp


@router.get("/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
                   db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    limiter.check("auth", client_ip(request))
    if error:
        return RedirectResponse(get_settings().url("/?error=oauth_denied"), status_code=302)
    raw = unsign_value(request.cookies.get(OAUTH_COOKIE))
    if not raw or not code or not state:
        return RedirectResponse(get_settings().url("/?error=oauth_state"), status_code=302)
    expected, _, next_url = raw.partition("~")
    if not secrets.compare_digest(expected, state):
        return RedirectResponse(get_settings().url("/?error=oauth_state"), status_code=302)
    try:
        profile = await _exchange_code(code)
    except AppError:
        return RedirectResponse(get_settings().url("/?error=oauth_failed"), status_code=302)
    try:
        return await _finish_login(db, request, profile, _safe_next(next_url))
    except AppError as e:
        return RedirectResponse(get_settings().url(f"/?error={e.code}"), status_code=302)


class DevLogin(BaseModel):
    discord_id: int = Field(ge=1, le=2**63 - 1)
    username: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.\-]+$")


async def _dev_login(body: DevLogin, request: Request, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    """Development-only login. The route is not registered at all in production, so the
    endpoint is indistinguishable from a non-existent one (even for malformed bodies)."""
    check_origin(request)
    limiter.check("auth", client_ip(request))
    return await _finish_login(db, request, {"id": str(body.discord_id), "username": body.username, "global_name": body.username}, "/roll")


def register_dev_login() -> None:
    s = get_settings()
    if s.dev_login_enabled and s.environment != "production":
        router.post("/dev-login")(_dev_login)
        log.warning("DEV LOGIN IS ENABLED — never use this configuration in production")


register_dev_login()


@router.post("/logout")
async def logout(principal: Principal = Depends(require_user("api", allow_frozen=True, allow_maintenance=True)),
                 db: AsyncSession = Depends(get_db)) -> Any:
    principal.session.revoked = True
    await db.commit()
    from fastapi.responses import JSONResponse

    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    return resp


class AdminModeBody(BaseModel):
    enabled: bool


@router.post("/admin-mode")
async def admin_mode(body: AdminModeBody, request: Request,
                     principal: Principal = Depends(require_user("api", allow_frozen=True, allow_maintenance=True)),
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not principal.is_admin:
        raise NotFound("Not Found")
    principal.session.admin_mode = body.enabled
    await audit.record(db, principal, "admin_mode_on" if body.enabled else "admin_mode_off", entity_type="session",
                       entity_id=principal.session_hash, user_agent=request.headers.get("user-agent"))
    queue_event(db, "user", "admin_mode", {"session": principal.session_hash, "admin_mode": body.enabled}, user_id=principal.user.id)
    await commit_and_publish(db)
    return {"admin_mode": body.enabled}


@router.get("/session")
async def session_info(principal: Principal | None = Depends(optional_principal)) -> dict[str, Any]:
    if principal is None:
        return {"authenticated": False}
    return {"authenticated": True, "user_id": principal.user.id}


def forbid_if_frozen(principal: Principal) -> None:
    if principal.user.status == "frozen":
        raise Forbidden("アカウントが凍結されています", code="account_frozen")


async def settings_public(db: AsyncSession, user_id: int) -> dict[str, Any]:
    return (await settings_svc.load(db, user_id)).model_dump()
