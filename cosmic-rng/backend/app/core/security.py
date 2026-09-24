"""Sessions, CSRF protection, authentication and authorization dependencies.

* Session tokens are random 256-bit values stored only in an HttpOnly cookie;
  the database stores a SHA-256 hash so a DB leak does not leak sessions.
* Every state changing request must carry ``X-CSRF-Token`` matching the
  session's CSRF token (double submit bound to the server-side session) and,
  when present, an allowed ``Origin``.
* Admin checks are server-side only. Non-admins get 404 from admin endpoints so
  the admin surface is indistinguishable from a non-existent route.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from fastapi import Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..models import Session as DBSession
from ..models import User
from .errors import AppError, Forbidden, NotFound, Unauthorized
from .ratelimit import limiter
from .timeutil import utcnow

SESSION_COOKIE = "crng_session"
OAUTH_COOKIE = "crng_oauth"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: Request) -> str:
    """Client IP, honouring X-Forwarded-For only from trusted reverse proxies."""
    peer = request.client.host if request.client else "unknown"
    s = get_settings()
    if peer in s.trusted_proxy_set:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            # right-most untrusted address
            for part in reversed([p.strip() for p in fwd.split(",") if p.strip()]):
                if part not in s.trusted_proxy_set:
                    return part[:64]
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()[:64]
    return peer[:64]


# ---------------------------------------------------------------------------
# Signed short-lived values (OAuth state cookie)
# ---------------------------------------------------------------------------
def sign_value(value: str, ttl_seconds: int) -> str:
    exp = str(int(time.time()) + ttl_seconds)
    body = f"{value}|{exp}"
    sig = hmac.new(get_settings().secret_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}"


def unsign_value(signed: str | None) -> str | None:
    if not signed or signed.count("|") < 2:
        return None
    body, sig = signed.rsplit("|", 1)
    expected = hmac.new(get_settings().secret_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    value, exp = body.rsplit("|", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return None
    return value


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
async def create_session(db: AsyncSession, user: User, request: Request) -> str:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    db.add(
        DBSession(
            id=hash_token(token),
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(24),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:256],
            expires_at=utcnow() + timedelta(days=s.session_ttl_days),
        )
    )
    return token


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=s.session_ttl_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path=s.cookie_path,
        domain=s.cookie_domain,
    )


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(SESSION_COOKIE, path=s.cookie_path, domain=s.cookie_domain, secure=s.cookie_secure, httponly=True)


async def revoke_user_sessions(db: AsyncSession, user_id: int) -> None:
    await db.execute(update(DBSession).where(DBSession.user_id == user_id).values(revoked=True))


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
@dataclass
class Principal:
    user: User
    session: DBSession
    is_admin: bool
    is_super_admin: bool
    ip: str

    @property
    def admin_mode(self) -> bool:
        return self.is_admin and self.session.admin_mode

    @property
    def session_hash(self) -> str:
        return self.session.id[:16]


def compute_admin(user: User) -> tuple[bool, bool]:
    """(is_admin, is_super_admin).

    Super-admin is a property of the deployment configuration, never of a database
    row: it is the account named in the launcher config (or an ADMIN_DISCORD_IDS
    entry). An administrator promoted from inside the game therefore cannot raise
    themselves to super-admin, which is what gates restores and role changes.
    """
    s = get_settings()
    super_admin = (user.discord_id is not None and user.discord_id in s.admin_ids) or (
        bool(s.admin_email) and bool(user.email) and user.email == s.admin_email.strip().lower()
    )
    return (super_admin or user.role == "admin"), super_admin


async def load_principal(db: AsyncSession, token: str | None, ip: str = "") -> Principal | None:
    if not token or len(token) > 128:
        return None
    sess = await db.get(DBSession, hash_token(token))
    now = utcnow()
    if sess is None or sess.revoked or sess.expires_at <= now:
        return None
    user = await db.get(User, sess.user_id)
    if user is None:
        return None
    if user.status == "banned":
        if user.status_until and user.status_until <= now:
            user.status = "active"
            user.status_reason = None
            user.status_until = None
        else:
            return None
    if user.status == "frozen" and user.status_until and user.status_until <= now:
        user.status = "active"
        user.status_reason = None
        user.status_until = None
    if (now - sess.last_used_at).total_seconds() > 300:
        sess.last_used_at = now
    is_admin, is_super = compute_admin(user)
    if not is_admin and sess.admin_mode:
        sess.admin_mode = False
    return Principal(user=user, session=sess, is_admin=is_admin, is_super_admin=is_super, ip=ip)


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = get_settings().origins
    if origin.rstrip("/") not in allowed:
        raise Forbidden("不正なリクエスト元です", code="bad_origin")


def check_csrf(request: Request, principal: Principal) -> None:
    if request.method in SAFE_METHODS:
        return
    check_origin(request)
    header = request.headers.get("x-csrf-token", "")
    if not header or not hmac.compare_digest(header, principal.session.csrf_token):
        raise Forbidden("CSRFトークンが無効です。ページを再読み込みしてください。", code="csrf_failed")


async def optional_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal | None:
    principal = await load_principal(db, request.cookies.get(SESSION_COOKIE), client_ip(request))
    if principal:
        request.state.user_id = principal.user.id
        await db.commit()
    return principal


def require_user(bucket: str = "api", *, allow_frozen: bool = False, allow_maintenance: bool = False) -> Callable[..., Any]:
    """Dependency factory: authenticated (and by default, unfrozen) user with CSRF + rate limit."""

    async def dep(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
        ip = client_ip(request)
        principal = await load_principal(db, request.cookies.get(SESSION_COOKIE), ip)
        if principal is None:
            limiter.check("auth", ip, cost=0.2)
            raise Unauthorized("ログインが必要です", code="login_required")
        request.state.user_id = principal.user.id
        if db.dirty:
            await db.commit()
        check_csrf(request, principal)
        limiter.check(bucket, str(principal.user.id))
        if request.method not in SAFE_METHODS:
            if principal.user.status == "frozen" and not allow_frozen:
                raise Forbidden("アカウントが凍結されています", code="account_frozen")
            if not allow_maintenance and not principal.is_admin:
                from ..content.registry import get_registry

                reg = get_registry()
                if reg.setting("features.maintenance_mode"):
                    raise AppError(
                        reg.setting("features.maintenance_message") or "メンテナンス中です",
                        code="maintenance",
                        status_code=503,
                    )
        return principal

    return dep


def require_admin(*, admin_mode: bool = True, super_admin: bool = False) -> Callable[..., Any]:
    """Admin dependency. Non-admins receive 404 so the surface stays hidden."""

    async def dep(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
        ip = client_ip(request)
        principal = await load_principal(db, request.cookies.get(SESSION_COOKIE), ip)
        if principal is None or not principal.is_admin:
            limiter.check("auth", ip, cost=0.5)
            raise NotFound("Not Found")
        request.state.user_id = principal.user.id
        if db.dirty:
            await db.commit()
        check_csrf(request, principal)
        limiter.check("admin", str(principal.user.id))
        if super_admin and not principal.is_super_admin:
            raise Forbidden("この操作はスーパー管理者のみ実行できます", code="super_admin_required")
        if admin_mode and not principal.session.admin_mode:
            raise Forbidden("Admin Modeを有効にしてください", code="admin_mode_required")
        return principal

    return dep


def principal_public(principal: Principal) -> dict[str, Any]:
    return {"is_admin": principal.is_admin, "admin_mode": principal.admin_mode}


async def find_session_user(db: AsyncSession, token: str | None) -> Principal | None:
    """Used by the WebSocket endpoint (no CSRF; origin is checked separately)."""
    return await load_principal(db, token)


async def touch_last_seen(db: AsyncSession, user_id: int) -> None:
    await db.execute(update(User).where(User.id == user_id).values(last_seen_at=utcnow()))


async def get_user_by_discord(db: AsyncSession, discord_id: int) -> User | None:
    return (await db.execute(select(User).where(User.discord_id == discord_id))).scalar_one_or_none()
