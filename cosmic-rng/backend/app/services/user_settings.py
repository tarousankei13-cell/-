"""Player settings: schema, defaults and persistence."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UserSettings

Tier = Literal["common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic"]


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AudioSettings(_M):
    master: float = Field(0.8, ge=0, le=1)
    bgm: float = Field(0.5, ge=0, le=1)
    sfx: float = Field(0.8, ge=0, le=1)
    muted: bool = False


class GraphicsSettings(_M):
    quality: Literal["high", "medium", "low", "minimal"] = "high"
    particles: float = Field(1.0, ge=0, le=1.5)
    reduced_motion: bool = False
    screen_shake: bool = True
    background_fx: bool = True


class RollSettings(_M):
    speed: Literal["normal", "fast", "ultra"] = "normal"
    cutscenes: bool = True
    full_cutscene_min_tier: Tier = "legendary"
    skip_confirm_min_tier: Tier = "secret"


class AutoSkipSettings(_M):
    enabled: bool = False
    threshold: float = Field(100, ge=2, le=1e12)


class AutoDeleteSettings(_M):
    enabled: bool = False
    max_odds: float = Field(10, ge=1, le=1e12)
    tiers: list[Tier] = Field(default_factory=list)
    mode: Literal["delete", "sell"] = "sell"
    protect_new: bool = True
    show_deleted: bool = False


class NotificationSettings(_M):
    world_feed: bool = True
    feed_min_tier: Tier = "legendary"
    toasts: bool = True
    trades: bool = True
    gifts: bool = True


class PrivacySettings(_M):
    public_profile: bool = True
    public_drops: bool = True
    show_inventory: bool = True


class UISettings(_M):
    font_scale: float = Field(1.0, ge=0.8, le=1.4)
    high_contrast: bool = False


class PlayerSettings(_M):
    audio: AudioSettings = Field(default_factory=AudioSettings)
    graphics: GraphicsSettings = Field(default_factory=GraphicsSettings)
    roll: RollSettings = Field(default_factory=RollSettings)
    auto_skip: AutoSkipSettings = Field(default_factory=AutoSkipSettings)
    auto_delete: AutoDeleteSettings = Field(default_factory=AutoDeleteSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    ui: UISettings = Field(default_factory=UISettings)


def parse(data: dict[str, Any] | None) -> PlayerSettings:
    try:
        return PlayerSettings.model_validate(data or {})
    except Exception:
        return PlayerSettings()


async def load(db: AsyncSession, user_id: int) -> PlayerSettings:
    row = await db.get(UserSettings, user_id)
    return parse(row.data if row else None)


async def save(db: AsyncSession, user_id: int, settings: PlayerSettings) -> None:
    row = await db.get(UserSettings, user_id)
    data = settings.model_dump()
    if row is None:
        db.add(UserSettings(user_id=user_id, data=data))
    else:
        row.data = data


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out
