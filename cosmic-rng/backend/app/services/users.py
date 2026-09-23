"""User lifecycle, levels and feature progression."""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, FeatureLocked, NotFound
from ..core.timeutil import utcnow
from ..models import User, UserBiome, UserCosmetic, UserSettings, UserStats
from ..rng.biome_engine import initial_state
from ..rng.engine import system_rng


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
        from ..config import get_settings

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
