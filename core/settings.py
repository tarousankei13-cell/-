"""
設定ストア

/config コマンドで変更した値を DB (bot_config) に保存する。
起動時に config.py の既定値を読み込み、DBの値で上書きする。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config as defaults
from db.models import BotConfig
from db.session import session_scope

log = logging.getLogger("bot.settings")

# 既定値（config.py の値をキー名に対応づける）
DEFAULTS: dict[str, Any] = {
    "subsidy_rate": float(defaults.DEFAULT_SUBSIDY_RATE),
    "monthly_subsidy_cap": defaults.DEFAULT_MONTHLY_SUBSIDY_CAP,
    "charge_min": defaults.CHARGE_MIN,
    "charge_max": defaults.CHARGE_MAX,
    "order_max_amount": defaults.ORDER_MAX_AMOUNT,
    "order_daily_limit": defaults.ORDER_DAILY_LIMIT,
    "menu_sync_interval_hours": defaults.MENU_SYNC_INTERVAL_HOURS,
    "menu_notify_diff": defaults.MENU_NOTIFY_DIFF,
    "feedback_gate": defaults.FEEDBACK_GATE_ENABLED,
    "maintenance": False,
    "achievement_fields": defaults.ACHIEVEMENT_FIELDS_DEFAULT,
    "channel_achievement": None,
    "channel_admin": None,
    "channel_charge": None,
    "backup_enabled": True,
    "backup_hour": 4,
}

_cache: dict[str, Any] = {}


async def load_all() -> dict[str, Any]:
    """起動時に呼ぶ。既定値にDBの値を重ねてキャッシュする。"""
    global _cache
    _cache = dict(DEFAULTS)
    async with session_scope() as s:
        rows = (await s.execute(select(BotConfig))).scalars().all()
    for row in rows:
        try:
            _cache[row.key] = json.loads(row.value)
        except ValueError:
            log.warning("設定 %s を読み込めませんでした", row.key)
    log.info("設定を読み込みました（%d件）", len(_cache))
    return _cache


def get(key: str, default: Any = None) -> Any:
    if key in _cache:
        return _cache[key]
    return DEFAULTS.get(key, default)


async def set_value(key: str, value: Any, *, updated_by: int | None = None) -> None:
    """設定を保存する。次回起動時も維持される。"""
    payload = json.dumps(value, ensure_ascii=False)
    async with session_scope() as s:
        row = await s.get(BotConfig, key)
        if row is None:
            s.add(BotConfig(key=key, value=payload, updated_by=updated_by))
        else:
            row.value = payload
            row.updated_by = updated_by
    _cache[key] = value
    log.info("設定を更新しました: %s = %s", key, value)


async def set_in_session(s: AsyncSession, key: str, value: Any, *, updated_by: int | None = None) -> None:
    """すでにトランザクションを持っている場合はこちら。"""
    payload = json.dumps(value, ensure_ascii=False)
    row = await s.get(BotConfig, key)
    if row is None:
        s.add(BotConfig(key=key, value=payload, updated_by=updated_by))
    else:
        row.value = payload
        row.updated_by = updated_by
    _cache[key] = value


def all_values() -> dict[str, Any]:
    return dict(_cache) if _cache else dict(DEFAULTS)
