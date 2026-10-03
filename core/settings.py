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
    "menu_sync_interval_minutes": defaults.MENU_SYNC_INTERVAL_MINUTES,
    "store_refresh_minutes": defaults.STORE_REFRESH_MINUTES,
    "menu_notify_diff": defaults.MENU_NOTIFY_DIFF,
    "feedback_gate": defaults.FEEDBACK_GATE_ENABLED,
    "maintenance": False,
    # 注文方式: both（両方） / hex（注文コードのみ） / menu（メニューのみ）
    "order_mode": "both",
    "achievement_fields": defaults.ACHIEVEMENT_FIELDS_DEFAULT,
    "channel_achievement": None,
    "channel_admin": None,
    "channel_charge": None,
    "channel_store_updates": None,
    "channel_menu_updates": None,
    "channel_balance": None,
    "balance_panel": True,
    "balance_panel_fields": defaults.BALANCE_PANEL_FIELDS_DEFAULT,
    "backup_enabled": True,
    "backup_hour": 4,
    "web_enabled": defaults.WEB_ENABLED,
    "web_host": defaults.WEB_HOST,
    "web_port": defaults.WEB_PORT,
    "web_base_url": defaults.WEB_BASE_URL,
    "receipt_page_hours": defaults.RECEIPT_PAGE_HOURS,
    "web_push_url": defaults.WEB_PUSH_URL,
    "web_push_secret": defaults.WEB_PUSH_SECRET,
    "invite_enabled": defaults.INVITE_ENABLED,
    "invite_reward": defaults.INVITE_REWARD,
    "invite_reward_invitee": defaults.INVITE_REWARD_INVITEE,
    "invite_max_per_user": defaults.INVITE_MAX_PER_USER,
    "invite_budget": defaults.INVITE_BUDGET,
    "invite_condition": defaults.INVITE_CONDITION,
    "channel_invite": None,
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


async def set_value(
    key: str,
    value: Any,
    *,
    updated_by: int | None = None,
    actor_name: str | None = None,
    reason: str | None = None,
    audit: bool = True,
) -> None:
    """
    設定を保存する。次回起動時も維持される。

    ⚠️ 変更は自動的に監査ログへ残る。
       設定コマンドを増やすたびに記録を書き足す必要は無い。
       記録したくない内部的な更新だけ audit=False にする。
    """
    previous = _cache.get(key)
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

    if audit and updated_by:
        # 循環参照を避けるため、ここで取り込む
        from core import audit as audit_log

        await audit_log.record(
            actor_id=updated_by, actor_name=actor_name, action="config.set",
            target=key, before=previous, after=value, reason=reason,
        )


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
