"""User lifecycle, authentication, levels and feature progression."""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..content.registry import get_registry
from ..core import passwords
from ..core.errors import AppError, FeatureLocked, NotFound
from ..core.timeutil import utcnow
from ..models import Session as DBSession, User, UserBiome, UserCosmetic, UserSettings, UserStats
from ..rng.biome_engine import initial_state
from ..rng.engine import system_rng

log = logging.getLogger("cosmic.users")

# Compared against when the account does not exist, so a wrong address costs the
# same time as a wrong password.
_DUMMY_HASH = passwords.hash_password("cosmic-rng-timing-equaliser")


def level_for_xp(xp: int) -> int:
    reg = get_registry()
    base = max(1, int(reg.setting("progression.xp_base") or 60))
    mx = int(reg.setting("progression.max_level") or 200)
    return max(1, min(mx, int(math.floor(math.sqrt(max(xp, 0) / base))) + 1))


def xp_for_level(level: int) -> int:
    base = max(1, int(get_registry().setting("progression.xp_base") or 60))
    return int(base * (level - 1) ** 2)


def feature_level(feature: str) -> int:
    unlocks = get_registry().setting("progression.unlocks") or {}
    return int(unlocks.get(feature, 1))


def feature_unlocked(user: User, feature: str) -> bool:
    return user.level >= feature_level(feature)


def require_feature(user: User, feature: str, enabled_setting: str | None = None) -> None:
    reg = get_registry()
    if enabled_setting and not reg.setting(enabled_setting):
        raise AppError("この機能は現在停止中です", code="feature_disabled", status_code=503)
    need = feature_level(feature)
    if user.level < need:
        raise FeatureLocked(f"この機能はレベル{need}で解放されます", data={"feature": feature, "level": need})


def unlocked_features(level: int) -> list[str]:
    unlocks = get_registry().setting("progression.unlocks") or {}
    return sorted(k for k, v in unlocks.items() if level >= int(v))


def newly_unlocked(old_level: int, new_level: int) -> list[str]:
    unlocks = get_registry().setting("progression.unlocks") or {}
    return sorted(k for k, v in unlocks.items() if old_level < int(v) <= new_level)


# ---------------------------------------------------------------------------
# Local accounts (email + username + password)
# ---------------------------------------------------------------------------
LOCKOUT_THRESHOLD = 8
LOCKOUT_MINUTES = 15


def _norm_email(email: str) -> str:
    return email.strip().lower()


async def find_by_login(db: AsyncSession, identifier: str) -> User | None:
    """Accept either the email address or the username, case-insensitively."""
    ident = identifier.strip()
    if not ident:
        return None
    if "@" in ident:
        return (await db.execute(select(User).where(User.email == _norm_email(ident)))).scalar_one_or_none()
    return (await db.execute(
        select(User).where(func.lower(User.username) == ident.lower())
    )).scalar_one_or_none()


async def register_local(db: AsyncSession, *, email: str, username: str, password: str,
                         allow_when_closed: bool = False) -> User:
    for problem in (passwords.check_email(email), passwords.check_username(username),
                    passwords.check_password(password)):
        if problem:
            raise AppError(problem, code="invalid_input")
    email = _norm_email(email)
    reg = get_registry()
    if not allow_when_closed and not reg.setting("features.registration_open"):
        raise AppError("現在、新規登録を停止しています", code="registration_closed", status_code=403)
    if (await db.execute(select(User.id).where(User.email == email))).first() is not None:
        raise AppError("このメールアドレスは登録済みです", code="email_taken", status_code=409)
    if (await db.execute(select(User.id).where(func.lower(User.username) == username.lower()))).first() is not None:
        raise AppError("このユーザー名は使用されています", code="username_taken", status_code=409)
    user = User(
        email=email, username=username, display_name=username,
        password_hash=passwords.hash_password(password),
        stardust=int(reg.setting("economy.starting_stardust") or 0),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:  # two registrations racing for the same name
        await db.rollback()
        raise AppError("このメールアドレスまたはユーザー名は使用されています", code="account_taken",
                       status_code=409) from exc
    await init_user_rows(db, user)
    return user


async def authenticate(db: AsyncSession, identifier: str, password: str) -> User:
    """Verify credentials. The failure message never says which half was wrong."""
    generic = AppError("メールアドレス（またはユーザー名）かパスワードが違います",
                       code="invalid_credentials", status_code=401)
    user = await find_by_login(db, identifier)
    if user is None or not user.password_hash:
        # Spend comparable time on an unknown account so timing does not reveal
        # which addresses are registered.
        passwords.verify_password(password, _DUMMY_HASH)
        raise generic
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        wait = int((user.locked_until - now).total_seconds() // 60) + 1
        raise AppError(f"ログイン試行が多すぎます。約{wait}分後にもう一度お試しください",
                       code="account_locked", status_code=429)
    if not passwords.verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= LOCKOUT_THRESHOLD:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
            log.warning("account %s locked after repeated failed logins", user.id)
        await db.commit()
        raise generic
    if user.failed_logins or user.locked_until:
        user.failed_logins = 0
        user.locked_until = None
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    return user


async def set_password(db: AsyncSession, user: User, new_password: str) -> None:
    problem = passwords.check_password(new_password)
    if problem:
        raise AppError(problem, code="invalid_input")
    user.password_hash = passwords.hash_password(new_password)
    user.failed_logins = 0
    user.locked_until = None
    # Other devices keep a session signed with the old password; drop them all.
    await db.execute(update(DBSession).where(DBSession.user_id == user.id).values(revoked=True))


async def ensure_admin_account(db: AsyncSession) -> str | None:
    """Create the configured administrator on first boot.

    Returns a short status for the boot log, or None when nothing is configured.
    An existing account is never silently re-passworded: that needs
    ADMIN_RESET_PASSWORD, so an admin who changes their password in-game keeps it.
    """
    s = get_settings()
    username, email, password = s.admin_username.strip(), s.admin_email.strip(), s.admin_password
    if not (username and email and password):
        return None
    existing = (await db.execute(
        select(User).where((User.email == _norm_email(email)) | (func.lower(User.username) == username.lower()))
    )).scalars().first()
    if existing is None:
        user = await register_local(db, email=email, username=username, password=password,
                                    allow_when_closed=True)
        user.role = "admin"
        await db.commit()
        return f"管理者アカウントを作成しました: {username} <{_norm_email(email)}>"
    changed = []
    if existing.role != "admin":
        existing.role = "admin"
        changed.append("権限を管理者に更新")
    if s.admin_reset_password:
        await set_password(db, existing, password)
        changed.append("パスワードを再設定（既存セッションは無効化）")
    if changed:
        await db.commit()
        return f"管理者アカウント {existing.username}: " + " / ".join(changed)
    return f"管理者アカウント: {existing.username} <{existing.email}>"


async def create_or_update_from_discord(db: AsyncSession, profile: dict[str, Any]) -> tuple[User, bool]:
    discord_id = int(profile["id"])
    username = str(profile.get("username") or "traveler")[:64]
    display = str(profile.get("global_name") or username)[:64]
    avatar = profile.get("avatar")
    user = (await db.execute(select(User).where(User.discord_id == discord_id))).scalar_one_or_none()
    if user is not None:
        user.username = username
        user.display_name = display
        user.avatar = avatar
        return user, False
    reg = get_registry()
    if not reg.setting("features.registration_open"):
        if discord_id not in get_settings().admin_ids:
            raise AppError("現在、新規登録を停止しています", code="registration_closed", status_code=403)
    user = User(
        discord_id=discord_id, username=username, display_name=display, avatar=avatar,
        stardust=int(reg.setting("economy.starting_stardust") or 0),
    )
    db.add(user)
    await db.flush()
    await init_user_rows(db, user)
    return user, True


async def init_user_rows(db: AsyncSession, user: User) -> None:
    snap = get_registry().snap
    now = utcnow()
    db.add(UserStats(user_id=user.id))
    db.add(UserSettings(user_id=user.id, data={}))
    st = initial_state(snap, now, user.level, 1.0, system_rng(), bool(snap.settings.get("features.biome_enabled", True)))
    db.add(UserBiome(
        user_id=user.id, biome_key=st.biome_key, started_at=st.started_at, ends_at=st.ends_at, next_eval_at=st.next_eval_at,
        next_biome_key=st.next_biome_key, sample_sig=st.sample_sig, evaluated_at=now,
    ))
    db.add(UserCosmetic(user_id=user.id, cosmetic_key="bg_default", source="default"))
    await db.flush()


async def get_user(db: AsyncSession, user_id: int) -> User:
    u = await db.get(User, user_id)
    if u is None:
        raise NotFound("ユーザーが見つかりません")
    return u


async def lock_user(db: AsyncSession, user_id: int) -> User:
    u = (
        await db.execute(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if u is None:
        raise NotFound("ユーザーが見つかりません")
    return u


async def lock_stats(db: AsyncSession, user_id: int) -> UserStats:
    s = (
        await db.execute(select(UserStats).where(UserStats.user_id == user_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if s is None:
        s = UserStats(user_id=user_id)
        db.add(s)
        await db.flush()
    return s


def avatar_url(user: User) -> str | None:
    if not user.avatar:
        return None
    ext = "gif" if str(user.avatar).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user.discord_id}/{user.avatar}.{ext}?size=128"


def user_brief(user: User) -> dict[str, Any]:
    return {
        "id": user.id, "name": user.display_name, "username": user.username, "avatar": avatar_url(user),
        "level": user.level, "title": user.title_key,
    }
