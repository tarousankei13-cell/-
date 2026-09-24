"""Admin operations: dashboard, user management, game settings, logs, RNG simulation."""
from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, true as sa_true, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models as m
from ..content.registry import bump_content_version, get_registry
from ..content.settings_schema import DEF_MAP, definitions_public, validate_setting
from ..db import greatest, is_sqlite, upsert as insert
from ..core.errors import AppError, Forbidden, NotFound
from ..core.pubsub import queue_event
from ..core.security import Principal, compute_admin, revoke_user_sessions
from ..core.timeutil import utcnow
from ..rng import engine as rng_engine
from ..rng.engine import RollContext, compile_table
from . import admin_content
from . import artifacts as artifacts_svc
from . import audit
from . import biomes as biomes_svc
from . import effects as effects_svc
from . import equipment as equipment_svc
from . import feed as feed_svc
from . import inventory as inv_svc
from .constants import TIER_BY_KEY
from .metrics import metrics
from . import users as users_svc
from .users import avatar_url, lock_user, user_brief


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def _dense_series(rows: Any, now: datetime, buckets: int, step: timedelta, fmt: str) -> list[dict[str, Any]]:
    """Every bucket in the window, including the empty ones.

    A GROUP BY only returns the periods that had activity, so a chart built from
    it puts a busy minute from an hour ago right next to the present one and
    reads as if it just happened. Filling the gaps keeps the x axis a time axis.
    """
    counts = {str(r[0]): int(r[1]) for r in rows}
    start = (now - step * (buckets - 1)).replace(second=0, microsecond=0)
    if step >= timedelta(hours=1):
        start = start.replace(minute=0)
    out = []
    for i in range(buckets):
        label = (start + step * i).strftime(fmt)
        out.append({"t": label, "n": counts.get(label, 0)})
    return out



async def dashboard(db: AsyncSession) -> dict[str, Any]:
    now = utcnow()
    snap = get_registry().snap
    online_cut = now - timedelta(seconds=90)
    day = now - timedelta(days=1)
    q = lambda stmt, params=None: db.execute(stmt, params)  # noqa: E731
    online = (await q(select(func.count()).select_from(m.User).where(m.User.last_seen_at > online_cut))).scalar_one()
    users_total = (await q(select(func.count()).select_from(m.User))).scalar_one()
    rolls_10s = (await q(select(func.count()).select_from(m.Roll).where(m.Roll.created_at > now - timedelta(seconds=10)))).scalar_one()
    total_rolls = (await q(select(func.coalesce(func.sum(m.UserStats.total_rolls), 0)))).scalar_one()
    rare_24h = (await q(select(func.count()).select_from(m.Roll).where(m.Roll.tier >= 4, m.Roll.created_at > day))).scalar_one()
    firsts_24h = (await q(select(func.count()).select_from(m.WorldEvent).where(m.WorldEvent.type == "first_discovery",
                                                                              m.WorldEvent.created_at > day))).scalar_one()
    mv = (await q(select(func.count(), func.coalesce(func.sum(m.MarketListing.price), 0)).where(
        m.MarketListing.status == "sold", m.MarketListing.sold_at > day))).one()
    trades_24h = (await q(select(func.count()).select_from(m.Trade).where(m.Trade.status == "accepted", m.Trade.completed_at > day))).scalar_one()
    errors_24h = (await q(select(func.count()).select_from(m.ErrorLog).where(m.ErrorLog.created_at > day))).scalar_one()
    active_boosts = (await q(select(func.count()).select_from(m.ActiveEffect).where(
        or_(m.ActiveEffect.expires_at.is_(None), m.ActiveEffect.expires_at > now),
        or_(m.ActiveEffect.remaining_rolls.is_(None), m.ActiveEffect.remaining_rolls > 0)))).scalar_one()
    biome_rows = (await q(select(m.UserBiome.biome_key, func.count()).join(m.User, m.User.id == m.UserBiome.user_id)
                          .where(m.User.last_seen_at > online_cut, or_(m.UserBiome.ends_at.is_(None), m.UserBiome.ends_at > now))
                          .group_by(m.UserBiome.biome_key))).all()
    if is_sqlite():
        per_min = (await q(text(
            "SELECT strftime('%H:%M', created_at) AS t, count(*) AS n FROM rolls "
            "WHERE created_at > :cut GROUP BY t ORDER BY 1"), {"cut": now - timedelta(minutes=60)})).all()
        rare_hour = (await q(text(
            "SELECT strftime('%m-%d %H:00', created_at) AS t, count(*) AS n FROM rolls "
            "WHERE tier >= 4 AND created_at > :cut GROUP BY t ORDER BY 1"), {"cut": day})).all()
    else:
        per_min = (await q(text(
            "SELECT to_char(date_trunc('minute', created_at), 'HH24:MI') AS t, count(*) AS n FROM rolls "
            "WHERE created_at > :cut GROUP BY date_trunc('minute', created_at) ORDER BY 1"),
            {"cut": now - timedelta(minutes=60)})).all()
        rare_hour = (await q(text(
            "SELECT to_char(date_trunc('hour', created_at), 'MM-DD HH24:00') AS t, count(*) AS n FROM rolls "
            "WHERE tier >= 4 AND created_at > :cut GROUP BY date_trunc('hour', created_at) ORDER BY 1"),
            {"cut": day})).all()
    tier_dist = (await q(select(m.Roll.tier, func.count()).where(m.Roll.created_at > now - timedelta(hours=1)).group_by(m.Roll.tier))).all()
    recent_errors = (await q(select(m.ErrorLog).order_by(m.ErrorLog.id.desc()).limit(8))).scalars().all()
    notes = (await q(select(m.Notification).where(m.Notification.for_admins.is_(True)).order_by(m.Notification.id.desc()).limit(12))).scalars().all()
    flagged = (await q(select(func.count()).select_from(m.MarketListing).where(m.MarketListing.flagged.is_(True),
                                                                              m.MarketListing.created_at > day))).scalar_one()
    return {
        "online": online, "users": users_total, "rolls_per_sec": round(rolls_10s / 10.0, 2), "worker_rolls_per_sec": round(metrics.rolls_per_sec(), 2),
        "total_rolls": int(total_rolls), "rare_drops_24h": rare_24h, "first_discoveries_24h": firsts_24h,
        "market_24h": {"count": mv[0], "volume": int(mv[1])}, "trades_24h": trades_24h, "errors_24h": errors_24h,
        "active_boosts": active_boosts, "flagged_listings_24h": flagged,
        "biomes": [{"key": k, "name": snap.biomes[k].name if k in snap.biomes else k,
                    "name_ja": snap.biomes[k].name_ja if k in snap.biomes else "", "count": c} for k, c in biome_rows],
        "rolls_per_minute": _dense_series(per_min, now, 60, timedelta(minutes=1), "%H:%M"),
        "rare_per_hour": _dense_series(rare_hour, now, 24, timedelta(hours=1), "%m-%d %H:00"),
        "tier_distribution_1h": [{"tier": t, "n": n} for t, n in tier_dist],
        "recent_errors": [{"error_id": e.error_id, "path": e.path, "type": e.exc_type, "message": e.message[:200],
                           "created_at": e.created_at.isoformat()} for e in recent_errors],
        "notifications": [feed_svc.notification_public(n) for n in notes],
        "content_version": snap.version, "server_time": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
async def search_users(db: AsyncSession, q: str, limit: int = 50) -> list[dict[str, Any]]:
    stmt = select(m.User, m.UserStats).outerjoin(m.UserStats, m.UserStats.user_id == m.User.id)
    q = (q or "").strip()
    if q:
        conds = [m.User.display_name.ilike(f"%{q[:64]}%"), m.User.username.ilike(f"%{q[:64]}%")]
        if q.isdigit():
            conds += [m.User.id == int(q) if int(q) < 2**62 else m.User.id == -1, m.User.discord_id == int(q)]
        stmt = stmt.where(or_(*conds))
    rows = (await db.execute(stmt.order_by(m.User.last_seen_at.desc().nullslast()).limit(min(limit, 200)))).all()
    return [{**user_brief(u), "discord_id": str(u.discord_id), "status": u.status, "role": u.role, "stardust": u.stardust,
             "total_rolls": s.total_rolls if s else 0, "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
             "created_at": u.created_at.isoformat()} for u, s in rows]


async def user_detail(db: AsyncSession, user_id: int) -> dict[str, Any]:
    snap = get_registry().snap
    u = await db.get(m.User, user_id)
    if u is None:
        raise NotFound("ユーザーが見つかりません")
    stats = await db.get(m.UserStats, u.id)
    biome = await db.get(m.UserBiome, u.id)
    effects = await effects_svc.load_active(db, u.id)
    rolls = (await db.execute(select(m.Roll).where(m.Roll.user_id == u.id).order_by(m.Roll.id.desc()).limit(40))).scalars().all()
    infos = await inv_svc.items_info(db, {r.item_id for r in rolls})
    trades = (await db.execute(select(m.Trade).where(or_(m.Trade.from_user_id == u.id, m.Trade.to_user_id == u.id))
                               .order_by(m.Trade.id.desc()).limit(20))).scalars().all()
    is_admin, is_super = compute_admin(u)
    return {
        "user": {**user_brief(u), "avatar": avatar_url(u), "discord_id": str(u.discord_id), "role": u.role, "status": u.status,
                 "status_reason": u.status_reason, "status_until": u.status_until.isoformat() if u.status_until else None,
                 "stardust": u.stardust, "xp": u.xp, "base_luck": u.base_luck, "roll_counter": u.roll_counter,
                 "auto_roll": u.auto_roll_enabled, "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
                 "created_at": u.created_at.isoformat(), "is_admin": is_admin, "is_super_admin": is_super},
        "stats": {c.key: getattr(stats, c.key) for c in m.UserStats.__table__.columns if c.key != "updated_at"} if stats else {},
        "biome": biomes_svc.public_state(biome, reveal=True) if biome else None,
        "effects": [effects_svc.public(e) for e in effects],
        "equipment": await equipment_svc.list_for_user(db, u.id),
        "inventory": await inv_svc.list_grouped(db, u.id, per_page=100),
        "boosts": await effects_svc.boost_inventory(db, u.id),
        "artifacts": await artifacts_svc.my_artifacts(db, u.id, True),
        "rolls": [{"id": r.id, "number": r.roll_number, "item": infos.get(r.item_id), "tier": r.tier, "luck": r.luck, "biome": r.biome_key,
                   "flags": r.flags, "final_chance": r.final_chance, "created_at": r.created_at.isoformat()} for r in rolls],
        "trades": [{"id": t.id, "status": t.status, "from": t.from_user_id, "to": t.to_user_id, "created_at": t.created_at.isoformat()}
                   for t in trades],
        "audit": await audit.search(db, target_user_id=u.id, limit=30),
        "biome_keys": [{"key": b.key, "name": b.name, "name_ja": b.name_ja, "kind": b.kind} for b in sorted(snap.biomes.values(), key=lambda b: b.sort_order)],
    }


DANGEROUS = {"ban", "delete_instances", "set_role", "adjust_stardust", "reset_user",
             "clear_inventory", "revoke_achievement"}


def _a(action: str, label: str, group: str, fields: list[dict[str, Any]], *, help: str = "") -> dict[str, Any]:
    return {"action": action, "label": label, "group": group, "fields": fields,
            "dangerous": action in DANGEROUS, "help": help}


def _f(name: str, label: str, type: str = "str", **kw: Any) -> dict[str, Any]:
    return {"name": name, "label": label, "type": type, **kw}


# The panel builds its forms from this, so an action can never exist in the API
# without a way to reach it, and the two can never disagree about its arguments.
USER_ACTIONS: list[dict[str, Any]] = [
    _a("give_item", "アイテムを付与", "所持品",
       [_f("item_key", "アイテム", "item", required=True), _f("qty", "個数", "int", default=1, min=1, max=10000)]),
    _a("delete_instances", "所持アイテムを削除", "所持品",
       [_f("instance_ids", "インスタンスID（カンマ区切り）", "ids", required=True)]),
    _a("clear_inventory", "所持品を全消去", "所持品", [], help="装備とBoostは残ります"),
    _a("give_boost_items", "Boostを付与", "所持品",
       [_f("boost_key", "Boost", "boost", required=True), _f("qty", "個数", "int", default=1, min=1, max=1000)]),
    _a("give_equipment", "装備を付与", "装備",
       [_f("equipment_key", "装備", "equipment", required=True),
        _f("quality_tier", "品質ティア", "enum", choices=["normal", "fine", "superior", "god"], default="normal"),
        _f("quality", "品質倍率", "float", default=1.0, min=0.1, max=5.0)]),
    _a("remove_equipment", "装備を削除", "装備", [_f("equipment_id", "装備インスタンスID", "int", required=True)]),
    _a("adjust_stardust", "スターダストを増減", "経済",
       [_f("delta", "増減量", "int", required=True)], help="負の値で減らせます"),
    _a("set_level", "レベルを設定", "進行",
       [_f("level", "レベル", "int", required=True, min=1, max=500)], help="XPも整合するよう設定されます"),
    _a("add_xp", "XPを増減", "進行", [_f("amount", "増減量", "int", required=True)]),
    _a("set_base_luck", "基礎Luckを設定", "進行", [_f("value", "倍率", "float", required=True, min=0)]),
    _a("set_roll_counter", "抽選回数を設定", "進行", [_f("value", "回数", "int", required=True, min=0)]),
    _a("grant_achievement", "実績を付与", "進行", [_f("achievement_key", "実績", "achievement", required=True)]),
    _a("revoke_achievement", "実績を剥奪", "進行", [_f("achievement_key", "実績", "achievement", required=True)]),
    _a("grant_cosmetic", "称号・背景を付与", "進行", [_f("cosmetic_key", "コスメティック", "cosmetic", required=True)]),
    _a("unlock_feature", "機能を解放", "進行", [_f("unlock_key", "解放キー", "str", required=True)]),
    _a("set_biome", "Biomeを変更", "世界",
       [_f("biome_key", "Biome", "biome", required=True), _f("duration", "継続（秒・空で既定）", "int", min=1)]),
    _a("add_effect", "一時効果を付与", "世界",
       [_f("effect_type", "効果", "enum", choices=list(admin_content.EFFECTS), required=True),
        _f("value", "値", "float", required=True), _f("name", "表示名", "str"),
        _f("stack_mode", "重ねかた", "enum", choices=["add", "multiply", "queue", "highest"], default="add"),
        _f("rolls", "適用回数", "int", min=1), _f("duration", "継続（秒）", "int", min=1)]),
    _a("clear_effects", "一時効果を全解除", "世界", []),
    _a("force_next_item", "次の抽選結果を固定", "抽選",
       [_f("item_key", "アイテム", "item", required=True), _f("rolls", "適用回数", "int", default=1, min=1)]),
    _a("set_item_chance", "特定アイテムの確率を上書き", "抽選",
       [_f("item_key", "アイテム", "item", required=True), _f("mult", "倍率", "float", required=True, min=0),
        _f("rolls", "適用回数", "int", min=1), _f("duration", "継続（秒）", "int", min=1)]),
    _a("set_rarity_chance", "レア度の確率を上書き", "抽選",
       [_f("tier", "レア度", "rarity", required=True), _f("mult", "倍率", "float", required=True, min=0),
        _f("rolls", "適用回数", "int", min=1), _f("duration", "継続（秒）", "int", min=1)]),
    _a("reset_cooldowns", "クールダウンを解除", "抽選", []),
    _a("set_auto_roll", "Auto Rollを切替", "抽選", [_f("enabled", "有効にする", "bool", default=True)]),
    _a("grant_artifact", "Admin Artifactを付与", "管理者遺物",
       [_f("artifact_key", "遺物", "artifact", required=True), _f("can_use", "本人が使用可能", "bool", default=False),
        _f("uses", "使用回数", "int", min=1), _f("hours", "有効時間", "float", min=0)]),
    _a("recall_artifact", "Admin Artifactを回収", "管理者遺物",
       [_f("instance_id", "インスタンスID", "int", required=True)]),
    _a("rename", "表示名を変更", "アカウント", [_f("display_name", "表示名", "str", required=True)]),
    _a("set_title", "称号を設定", "アカウント", [_f("title_key", "称号（空で解除）", "cosmetic")]),
    _a("freeze", "凍結（閲覧のみ可）", "アカウント", [_f("hours", "期間（時間・空で無期限）", "float", min=0)]),
    _a("ban", "利用停止", "アカウント", [_f("hours", "期間（時間・空で無期限）", "float", min=0)]),
    _a("unrestrict", "制限を解除", "アカウント", []),
    _a("set_role", "権限を変更", "アカウント",
       [_f("role", "権限", "enum", choices=["player", "admin"], required=True)],
       help="スーパー管理者は設定ファイル側でのみ決まります"),
    _a("revoke_sessions", "全端末からログアウト", "アカウント", []),
    _a("reset_user", "進行をすべて初期化", "アカウント", [], help="アカウントは残り、進行だけが消えます"),
]

BULK_OPERATIONS: list[dict[str, Any]] = [
    _a("give_stardust", "全員にスターダスト", "配布", [_f("amount", "増減量", "int", required=True)]),
    _a("give_item", "全員にアイテム", "配布",
       [_f("item_key", "アイテム", "item", required=True), _f("qty", "個数", "int", default=1, min=1, max=100)]),
    _a("give_boost", "全員にBoost", "配布",
       [_f("boost_key", "Boost", "boost", required=True), _f("qty", "個数", "int", default=1, min=1, max=100)]),
    _a("grant_cosmetic", "全員に称号・背景", "配布", [_f("cosmetic_key", "コスメティック", "cosmetic", required=True)]),
    _a("reset_all_cooldowns", "全員のクールダウン解除", "運用", []),
    _a("clear_all_effects", "全員の一時効果を解除", "運用", []),
    _a("wipe_market", "出品をすべて取り下げ", "運用", [], help="出品中のアイテムは出品者に戻ります"),
    _a("recompute_stats", "統計を再計算", "運用", [], help="所有数・相場・総資産を作り直します"),
]

# BULK_DANGEROUS is defined with the operation itself, further down.
_BULK_DANGEROUS_NAMES = {"wipe_market", "clear_all_effects", "reset_all_cooldowns"}
for _op in BULK_OPERATIONS:
    _op["dangerous"] = _op["action"] in _BULK_DANGEROUS_NAMES


async def user_action(db: AsyncSession, principal: Principal, user_id: int, action: str, p: dict[str, Any], reason: str,
                      user_agent: str | None = None) -> dict[str, Any]:
    snap = get_registry().snap
    if not reason.strip():
        raise AppError("理由を入力してください", code="reason_required")
    if action in DANGEROUS and not p.get("confirm"):
        raise AppError("危険な操作です。確認が必要です", code="confirmation_required")
    user = await lock_user(db, user_id)
    target_admin, target_super = compute_admin(user)
    if target_super and principal.user.id != user.id and action in ("ban", "freeze", "set_role", "reset_user"):
        raise Forbidden("スーパー管理者は対象にできません", code="protected_user")
    old: Any = None
    new: Any = None
    result: dict[str, Any] = {"ok": True}

    async def rec() -> None:
        await audit.record(db, principal, f"user_{action}", target_user_id=user.id, entity_type="user", entity_id=user.id,
                           old=old, new=new, reason=reason, user_agent=user_agent)

    if action == "give_item":
        item = snap.items_by_key.get(str(p.get("item_key")))
        qty = int(p.get("qty", 1))
        if item is None or item.kind == "admin_artifact":
            raise AppError("アイテムが不正です（Admin Artifactは専用操作で付与）", code="invalid_item")
        if qty < 1 or qty > 10000:
            raise AppError("数量が不正です", code="invalid_quantity")
        ids = await inv_svc.create_instances(db, user.id, item.id, qty, "admin", tier=item.tier, meta={"granted_by": principal.user.id})
        from .rolls import collection_upsert

        await collection_upsert(db, user.id, item.id, qty)
        new = {"item": item.key, "qty": qty, "instance_ids": ids[:20]}
        await feed_svc.notify_user(db, user.id, "admin_gift", f"管理者から {item.name} ×{qty} が付与されました", reason)
    elif action == "delete_instances":
        ids = [int(i) for i in p.get("instance_ids", [])][:5000]
        rows = (await db.execute(select(m.ItemInstance).where(m.ItemInstance.id.in_(ids), m.ItemInstance.owner_id == user.id))).scalars().all()
        old = [{"id": r.id, "item_id": r.item_id, "serial": r.serial} for r in rows]
        await db.execute(delete(m.ItemInstance).where(m.ItemInstance.id.in_([r.id for r in rows])))
        new = {"deleted": len(rows)}
    elif action == "set_base_luck":
        v = float(p.get("value", 1.0))
        if not (0 <= v <= 1e15) or math.isnan(v):
            raise AppError("Luckが不正です", code="invalid_value")
        old, user.base_luck, new = user.base_luck, v, v
    elif action == "set_biome":
        key = str(p.get("biome_key"))
        duration = int(p["duration"]) if p.get("duration") else None
        old = (await db.get(m.UserBiome, user.id)).biome_key if await db.get(m.UserBiome, user.id) else None
        ctx = await biomes_svc.force_biome(db, user, key, duration, principal.user.id, allow_admin=True)
        new = {"biome": key, "duration": duration}
        queue_event(db, "user", "biome", biomes_svc.public_state(ctx.row), user_id=user.id)
    elif action == "give_boost_items":
        key = str(p.get("boost_key"))
        qty = int(p.get("qty", 1))
        if qty < 1 or qty > 10000:
            raise AppError("数量が不正です", code="invalid_quantity")
        await effects_svc.grant_boost_items(db, user.id, key, qty)
        new = {"boost": key, "qty": qty}
    elif action == "add_effect":
        etype = str(p.get("effect_type"))
        from .admin_content import EFFECTS

        if etype not in EFFECTS:
            raise AppError("効果タイプが不正です", code="invalid_effect")
        e = await effects_svc.add_effect(
            db, user.id, source_type="admin", source_key=f"admin_{principal.user.id}", name=str(p.get("name") or f"Admin: {etype}"),
            effect_type=etype, value=float(p.get("value", 0)), stack_mode=str(p.get("stack_mode", "multiply")),
            rolls=int(p["rolls"]) if p.get("rolls") else None, duration_sec=int(p["duration"]) if p.get("duration") else None,
            biome_keys=list(p.get("biome_keys") or []), params=dict(p.get("params") or {}), granted_by=principal.user.id)
        new = effects_svc.public(e)
    elif action == "force_next_item":
        item = snap.items_by_key.get(str(p.get("item_key")))
        if item is None or item.kind == "admin_artifact":
            raise AppError("アイテムが不正です", code="invalid_item")
        e = await effects_svc.add_effect(db, user.id, source_type="admin", source_key=f"admin_{principal.user.id}",
                                         name="Fate Rewritten", effect_type="force_item", rolls=int(p.get("rolls", 1)),
                                         params={"item_key": item.key}, granted_by=principal.user.id)
        new = effects_svc.public(e)
    elif action == "set_item_chance":
        item = snap.items_by_key.get(str(p.get("item_key")))
        if item is None:
            raise AppError("アイテムが不正です", code="invalid_item")
        e = await effects_svc.add_effect(db, user.id, source_type="admin", source_key=f"admin_chance_{item.key}",
                                         name=f"Probability: {item.name}", effect_type="item_chance", value=float(p.get("mult", 1)),
                                         stack_mode="multiply", rolls=int(p["rolls"]) if p.get("rolls") else None,
                                         duration_sec=int(p["duration"]) if p.get("duration") else None, params={"item_key": item.key},
                                         granted_by=principal.user.id)
        new = effects_svc.public(e)
    elif action == "set_rarity_chance":
        tier = str(p.get("tier"))
        if tier not in TIER_BY_KEY:
            raise AppError("レア度が不正です", code="invalid_tier")
        e = await effects_svc.add_effect(db, user.id, source_type="admin", source_key=f"admin_rarity_{tier}", name=f"Probability: {tier}",
                                         effect_type="rarity_chance", value=float(p.get("mult", 1)), stack_mode="multiply",
                                         rolls=int(p["rolls"]) if p.get("rolls") else None,
                                         duration_sec=int(p["duration"]) if p.get("duration") else None, params={"tier": tier},
                                         granted_by=principal.user.id)
        new = effects_svc.public(e)
    elif action == "clear_effects":
        ids = p.get("effect_ids")
        stmt = delete(m.ActiveEffect).where(m.ActiveEffect.user_id == user.id)
        if ids:
            stmt = stmt.where(m.ActiveEffect.id.in_([int(i) for i in ids]))
        res = await db.execute(stmt)
        new = {"cleared": res.rowcount}
    elif action == "adjust_stardust":
        delta = int(p.get("delta", 0))
        if user.stardust + delta < 0:
            raise AppError("残高が負になります", code="invalid_value")
        old = user.stardust
        user.stardust += delta
        new = user.stardust
    elif action == "freeze":
        old = user.status
        user.status = "frozen"
        user.status_reason = reason
        user.status_until = utcnow() + timedelta(hours=float(p["hours"])) if p.get("hours") else None
        new = {"status": "frozen", "until": user.status_until}
    elif action == "ban":
        old = user.status
        user.status = "banned"
        user.status_reason = reason
        user.status_until = utcnow() + timedelta(hours=float(p["hours"])) if p.get("hours") else None
        await revoke_user_sessions(db, user.id)
        new = {"status": "banned", "until": user.status_until}
        queue_event(db, "user", "force_logout", {"reason": "banned"}, user_id=user.id)
    elif action == "unrestrict":
        old = user.status
        user.status, user.status_reason, user.status_until = "active", None, None
        new = "active"
    elif action == "set_role":
        if not principal.is_super_admin:
            raise Forbidden("スーパー管理者のみ実行できます", code="super_admin_required")
        role = str(p.get("role"))
        if role not in ("player", "admin"):
            raise AppError("ロールが不正です", code="invalid_role")
        old, user.role, new = user.role, role, role
        if role == "player":
            await db.execute(update(m.Session).where(m.Session.user_id == user.id).values(admin_mode=False))
    elif action == "grant_artifact":
        result = await artifacts_svc.grant(db, principal, user.id, str(p.get("artifact_key")), can_use=bool(p.get("can_use")),
                                           uses=int(p["uses"]) if p.get("uses") else None,
                                           hours=float(p["hours"]) if p.get("hours") else None, reason=reason)
        return result
    elif action == "recall_artifact":
        return await artifacts_svc.recall(db, principal, int(p.get("instance_id", 0)), reason)
    elif action == "reset_cooldowns":
        await db.execute(delete(m.ArtifactCooldown).where(m.ArtifactCooldown.user_id == user.id))
        user.next_roll_at = None
        new = "reset"
    elif action == "revoke_sessions":
        await revoke_user_sessions(db, user.id)
        queue_event(db, "user", "force_logout", {"reason": "sessions_revoked"}, user_id=user.id)
        new = "revoked"

    # --- progression ---------------------------------------------------------
    elif action == "set_level":
        level = int(p.get("level", 1))
        if not 1 <= level <= 500:
            raise AppError("レベルは1〜500です", code="invalid_level")
        old = {"level": user.level, "xp": user.xp}
        # XP has to follow, or the next roll immediately recomputes the level back.
        user.level, user.xp = level, users_svc.xp_for_level(level)
        new = {"level": level, "xp": user.xp}

    elif action == "add_xp":
        amount = int(p.get("amount", 0))
        if abs(amount) > 10**12:
            raise AppError("XPの値が大きすぎます", code="invalid_amount")
        old = {"xp": user.xp, "level": user.level}
        user.xp = max(0, user.xp + amount)
        user.level = users_svc.level_for_xp(user.xp)
        new = {"xp": user.xp, "level": user.level}

    elif action == "set_roll_counter":
        n = int(p.get("value", 0))
        if not 0 <= n <= 10**12:
            raise AppError("値が不正です", code="invalid_value")
        old, user.roll_counter, new = user.roll_counter, n, n

    # --- inventory and equipment --------------------------------------------
    elif action == "give_equipment":
        eq = snap.equipment.get(str(p.get("equipment_key")))
        if eq is None:
            raise AppError("装備が見つかりません", code="invalid_equipment")
        tier = str(p.get("quality_tier") or "normal")
        q = float(p.get("quality") or 1.0)
        if not 0.1 <= q <= 5.0:
            raise AppError("品質は0.1〜5.0です", code="invalid_quality")
        row = await equipment_svc.create_instance(db, user.id, eq["key"], "admin", quality=(tier, q))
        new = {"equipment": eq["key"], "quality_tier": tier, "quality": q, "id": row.id}
        result["equipment_id"] = row.id

    elif action == "remove_equipment":
        eid = int(p.get("equipment_id", 0))
        row = (await db.execute(select(m.UserEquipment).where(m.UserEquipment.id == eid,
                                                              m.UserEquipment.user_id == user.id))).scalar_one_or_none()
        if row is None:
            raise NotFound("その装備を所持していません")
        old = {"equipment": row.equipment_key, "id": row.id}
        await db.delete(row)
        new = "removed"

    elif action == "clear_inventory":
        res = await db.execute(delete(m.ItemInstance).where(m.ItemInstance.owner_id == user.id,
                                                            m.ItemInstance.state == "owned"))
        new = {"deleted": res.rowcount or 0}

    # --- unlocks and cosmetics ----------------------------------------------
    elif action == "grant_achievement":
        ach = snap.achievements.get(str(p.get("achievement_key")))
        if ach is None:
            raise AppError("実績が見つかりません", code="invalid_achievement")
        await db.execute(insert(m.UserAchievement).values(user_id=user.id, achievement_id=ach["id"])
                         .on_conflict_do_nothing())
        new = {"achievement": ach["key"]}

    elif action == "revoke_achievement":
        ach = snap.achievements.get(str(p.get("achievement_key")))
        if ach is None:
            raise AppError("実績が見つかりません", code="invalid_achievement")
        await db.execute(delete(m.UserAchievement).where(m.UserAchievement.user_id == user.id,
                                                         m.UserAchievement.achievement_id == ach["id"]))
        old, new = ach["key"], "revoked"

    elif action == "grant_cosmetic":
        key = str(p.get("cosmetic_key"))
        if key not in snap.cosmetics:
            raise AppError("コスメティックが見つかりません", code="invalid_cosmetic")
        await db.execute(insert(m.UserCosmetic).values(user_id=user.id, cosmetic_key=key, source="admin")
                         .on_conflict_do_nothing())
        new = {"cosmetic": key}

    elif action == "unlock_feature":
        key = str(p.get("unlock_key"))
        if not key:
            raise AppError("解放キーを指定してください", code="invalid_unlock")
        await db.execute(insert(m.UserUnlock).values(user_id=user.id, unlock_key=key, value={})
                         .on_conflict_do_nothing())
        new = {"unlock": key}

    # --- identity -------------------------------------------------------------
    elif action == "rename":
        name = str(p.get("display_name") or "").strip()
        if not 1 <= len(name) <= 64:
            raise AppError("表示名は1〜64文字です", code="invalid_name")
        old, user.display_name, new = user.display_name, name, name

    elif action == "set_title":
        key = str(p.get("title_key") or "") or None
        if key and key not in snap.cosmetics:
            raise AppError("称号が見つかりません", code="invalid_title")
        old, user.title_key, new = user.title_key, key, key

    # --- automation -----------------------------------------------------------
    elif action == "set_auto_roll":
        on = bool(p.get("enabled"))
        old = user.auto_roll_enabled
        user.auto_roll_enabled = on
        user.auto_roll_since = utcnow() if on else None
        new = on

    elif action == "reset_user":
        # Everything except the account itself: the player starts over but keeps
        # their login, so this is recoverable by re-granting rather than by restore.
        await db.execute(delete(m.ItemInstance).where(m.ItemInstance.owner_id == user.id))
        for model in (m.Collection, m.UserAchievement, m.UserQuest, m.UserEquipment, m.Inventory,
                      m.ActiveEffect, m.UserUnlock, m.UserRecipe, m.UserItemPref):
            await db.execute(delete(model).where(model.user_id == user.id))
        old = {"level": user.level, "stardust": user.stardust, "rolls": user.roll_counter}
        user.level, user.xp, user.stardust, user.roll_counter = 1, 0, 0, 0
        user.title_key, user.profile_background, user.base_luck = None, None, 1.0
        stats = await users_svc.lock_stats(db, user.id)
        for col in ("total_rolls", "offline_rolls", "special_rolls", "items_obtained", "items_auto_deleted",
                    "stardust_earned", "items_sold", "net_worth"):
            if hasattr(stats, col):
                setattr(stats, col, 0)
        new = "reset"
    else:
        raise AppError("不明な操作です", code="unknown_action")
    await rec()
    queue_event(db, "user", "state_changed", {"by": "admin", "action": action}, user_id=user.id)
    result["new"] = new
    return result


# ---------------------------------------------------------------------------
# Game settings
# ---------------------------------------------------------------------------
async def get_settings(db: AsyncSession) -> dict[str, Any]:
    reg = get_registry()
    return {"definitions": definitions_public(), "values": {d["key"]: reg.setting(d["key"]) for d in definitions_public()}}


async def update_settings(db: AsyncSession, principal: Principal, values: dict[str, Any], reason: str) -> dict[str, Any]:
    reg = get_registry()
    changed: dict[str, Any] = {}
    old: dict[str, Any] = {}
    for k, v in values.items():
        vv = validate_setting(k, v)
        if DEF_MAP[k].dangerous and not reason.strip():
            raise AppError("この設定の変更には理由が必要です", code="reason_required")
        old[k] = reg.setting(k)
        await db.execute(insert(m.GameSetting).values(key=k, value=vv, updated_by=principal.user.id)
                         .on_conflict_do_update(index_elements=["key"], set_={"value": vv, "updated_by": principal.user.id}))
        changed[k] = vv
    if not changed:
        raise AppError("変更がありません", code="no_changes")
    await bump_content_version(db, principal.user.id)
    await audit.record(db, principal, "settings_update", entity_type="settings", entity_id=",".join(changed)[:128], old=old, new=changed,
                       reason=reason)
    if "features.maintenance_mode" in changed:
        queue_event(db, "all", "maintenance", {"enabled": changed["features.maintenance_mode"],
                                               "message": reg.setting("features.maintenance_message")})
    return {"ok": True, "changed": changed}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
async def search_rolls(db: AsyncSession, *, user_id: int | None = None, item_key: str | None = None, min_tier: int | None = None,
                       flags: int | None = None, before_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    snap = get_registry().snap
    conds = []
    if user_id:
        conds.append(m.Roll.user_id == user_id)
    if item_key:
        it = snap.items_by_key.get(item_key)
        item_id = it.id if it else (await db.execute(select(m.Item.id).where(m.Item.key == item_key))).scalar_one_or_none()
        conds.append(m.Roll.item_id == (item_id or -1))
    if min_tier:
        conds.append(m.Roll.tier >= min_tier)
    if flags:
        conds.append(m.Roll.flags.op("&")(flags) != 0)
    if before_id:
        conds.append(m.Roll.id < before_id)
    q = select(m.Roll, m.User).join(m.User, m.User.id == m.Roll.user_id).order_by(m.Roll.id.desc()).limit(min(limit, 500))
    if conds:
        q = q.where(and_(*conds))
    rows = (await db.execute(q)).all()
    infos = await inv_svc.items_info(db, {r.item_id for r, _ in rows})
    return [{"id": r.id, "user": user_brief(u), "number": r.roll_number, "item": infos.get(r.item_id), "tier": r.tier,
             "biome": r.biome_key, "state": r.biome_state, "luck": r.luck, "base_odds": r.base_odds, "final_chance": r.final_chance,
             "equipment_mult": r.equipment_mult, "boost_mult": r.boost_mult, "flags": r.flags, "rng_version": r.rng_version,
             "content_version": r.content_version, "batch_id": r.batch_id, "detail": r.detail, "created_at": r.created_at.isoformat()}
            for r, u in rows]


async def search_trades(db: AsyncSession, *, user_id: int | None = None, item_key: str | None = None, instance_id: int | None = None,
                        status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    from .trades import trade_public

    q = select(m.Trade)
    if user_id:
        q = q.where(or_(m.Trade.from_user_id == user_id, m.Trade.to_user_id == user_id))
    if status:
        q = q.where(m.Trade.status == status)
    if instance_id:
        q = q.where(m.Trade.id.in_(select(m.TradeItem.trade_id).where(m.TradeItem.instance_id == instance_id)))
    if item_key:
        it = get_registry().snap.items_by_key.get(item_key)
        q = q.where(m.Trade.id.in_(select(m.TradeItem.trade_id).where(m.TradeItem.item_id == (it.id if it else -1))))
    rows = (await db.execute(q.order_by(m.Trade.id.desc()).limit(min(limit, 500)))).scalars().all()
    uids = {t.from_user_id for t in rows} | {t.to_user_id for t in rows}
    users = {u.id: u for u in (await db.execute(select(m.User).where(m.User.id.in_(uids)))).scalars().all()} if uids else {}
    return [trade_public(t, users.get(t.from_user_id), users.get(t.to_user_id)) for t in rows]


async def search_market(db: AsyncSession, *, flagged: bool = False, user_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    from .market import listing_public

    q = select(m.MarketListing, m.User).join(m.User, m.User.id == m.MarketListing.seller_id)
    if flagged:
        q = q.where(m.MarketListing.flagged.is_(True))
    if user_id:
        q = q.where(or_(m.MarketListing.seller_id == user_id, m.MarketListing.buyer_id == user_id))
    rows = (await db.execute(q.order_by(m.MarketListing.id.desc()).limit(min(limit, 500)))).all()
    return [{**listing_public(l, None, u), "flagged": l.flagged, "flag_reason": l.flag_reason, "buyer_id": l.buyer_id} for l, u in rows]


async def errors(db: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    rows = (await db.execute(select(m.ErrorLog).order_by(m.ErrorLog.id.desc()).limit(min(limit, 500)))).scalars().all()
    return [{"id": e.id, "error_id": e.error_id, "path": e.path, "method": e.method, "user_id": e.user_id, "type": e.exc_type,
             "message": e.message, "traceback": e.traceback, "created_at": e.created_at.isoformat()} for e in rows]


# ---------------------------------------------------------------------------
# RNG simulation
# ---------------------------------------------------------------------------
def _simulate_sync(table: rng_engine.CompiledTable, n: int, seed: int | None) -> dict[int, int]:
    rng = random.Random(seed)
    return rng_engine.sample_many(table, n, rng)


async def simulate(luck: float, biome_key: str, n: int, *, state: str | None = None, special: bool = False, level: int = 999,
                   min_tier: int = 0, seed: int | None = None) -> dict[str, Any]:
    snap = get_registry().snap
    if biome_key not in snap.biomes:
        raise AppError("Biomeが不正です", code="invalid_biome")
    if not (1 <= n <= 2_000_000):
        raise AppError("試行回数は1〜2,000,000です", code="invalid_n")
    if not (0 < luck < 1e300):
        raise AppError("Luckが不正です", code="invalid_luck")
    ctx = RollContext(biome_key=biome_key, luck=luck, biome_state=state, level=level, special=special, now=utcnow(), min_tier=min_tier)
    table = compile_table(snap, ctx)
    counts = await asyncio.get_running_loop().run_in_executor(None, _simulate_sync, table, n, seed)
    rows = []
    chi2 = 0.0
    dof = 0
    tail_o = tail_e = 0.0
    for i, (it, p) in enumerate(zip(table.items, table.probs)):
        o = counts.get(i, 0)
        e = p * n
        sd = math.sqrt(max(e * (1 - p), 1e-12))
        rows.append({"key": it.key, "name": it.name, "rarity": it.rarity_key, "odds": it.odds, "p": p, "expected": e, "observed": o,
                     "z": (o - e) / sd if sd > 0 else 0.0})
        if e >= 5:
            chi2 += (o - e) ** 2 / e
            dof += 1
        else:
            tail_o += o
            tail_e += e
    if tail_e >= 5:
        chi2 += (tail_o - tail_e) ** 2 / tail_e
        dof += 1
    dof = max(1, dof - 1)
    z = (chi2 - dof) / math.sqrt(2 * dof)
    return {"n": n, "luck": luck, "biome": biome_key, "items": rows, "chi2": chi2, "dof": dof, "chi2_z": z,
            "verdict": "OK" if abs(z) < 3.5 else "DEVIATION"}


async def user_table(db: AsyncSession, user_id: int) -> dict[str, Any]:
    """Exact current probability table for a player (after all modifiers)."""
    from . import rolls as rolls_svc
    from ..rng.engine import system_rng
    from ..rng.modifiers import combine_effects

    user = await lock_user(db, user_id)
    now = utcnow()
    env = await rolls_svc.load_env(db, user, now)
    st = env.biome.state
    special, hidden = rolls_svc.special_for(env, user.roll_counter + 1)
    mods = combine_effects(env.effects, now=now, biome_key=st.biome_key, special=special, rng=system_rng())
    lb = rolls_svc.compute_luck(env, biome_key=st.biome_key, state_key=st.state_key, mods=mods, special=special, hidden=hidden,
                                roll_number=user.roll_counter + 1, at=now)
    ctx = rolls_svc.build_ctx(env, biome_key=st.biome_key, state_key=st.state_key, lb=lb, special=special,
                              hidden_key=hidden["key"] if hidden else None, mods=mods, at=now)
    table = compile_table(env.snap, ctx)
    await rolls_svc._finish_progress(db, env)  # noqa: SLF001
    return {"luck": lb.as_dict(), "biome": st.biome_key, "state": st.state_key, "special": special, "forced": mods.force_item_key,
            "min_tier": ctx.min_tier, "flatten": ctx.flatten,
            "items": [{"key": it.key, "name": it.name, "rarity": it.rarity_key, "odds": it.odds, "p": p}
                      for it, p in zip(table.items, table.probs)]}


# ---------------------------------------------------------------------------
# Server-wide operations
#
# Everything here touches every player at once, so each one is audited as a
# single entry with the row count it affected, and the destructive ones need an
# explicit confirmation from the caller.
# ---------------------------------------------------------------------------
BULK_DANGEROUS = _BULK_DANGEROUS_NAMES


async def bulk_operation(db: AsyncSession, principal: Principal, op: str, p: dict[str, Any], reason: str,
                         user_agent: str | None = None) -> dict[str, Any]:
    if not reason.strip():
        raise AppError("理由を入力してください", code="reason_required")
    if op in BULK_DANGEROUS and not p.get("confirm"):
        raise AppError("全プレイヤーに影響します。確認が必要です", code="confirmation_required")

    snap = get_registry().snap
    only_active = bool(p.get("only_active"))
    cutoff = utcnow() - timedelta(days=int(p.get("active_days", 30)))
    # Set-based operations reuse the predicate so a large server never builds a
    # giant IN clause; per-row ones need the ids and take them from the same filter.
    audience = (m.User.last_seen_at > cutoff) if only_active else sa_true()
    ids = [r[0] for r in (await db.execute(select(m.User.id).where(audience))).all()]
    detail: dict[str, Any] = {"op": op, "targets": len(ids), "only_active": only_active}

    if op == "give_stardust":
        amount = int(p.get("amount", 0))
        if not -10**12 <= amount <= 10**12:
            raise AppError("金額が不正です", code="invalid_amount")
        await db.execute(update(m.User).where(audience)
                         .values(stardust=greatest(m.User.stardust + amount, 0)))
        detail["amount"] = amount

    elif op == "give_item":
        item = snap.items_by_key.get(str(p.get("item_key")))
        qty = int(p.get("qty", 1))
        if item is None or item.kind == "admin_artifact":
            raise AppError("アイテムが不正です", code="invalid_item")
        if not 1 <= qty <= 100:
            raise AppError("一括付与は1〜100個までです", code="invalid_quantity")
        from .rolls import collection_upsert

        for uid in ids:
            await inv_svc.create_instances(db, uid, item.id, qty, "admin", tier=item.tier,
                                           meta={"granted_by": principal.user.id, "bulk": True})
            await collection_upsert(db, uid, item.id, qty)
        detail.update({"item": item.key, "qty": qty})

    elif op == "give_boost":
        boost = snap.boosts.get(str(p.get("boost_key")))
        qty = int(p.get("qty", 1))
        if boost is None:
            raise AppError("Boostが見つかりません", code="invalid_boost")
        if not 1 <= qty <= 100:
            raise AppError("一括付与は1〜100個までです", code="invalid_quantity")
        for uid in ids:
            await effects_svc.grant_boost_items(db, uid, boost["key"], qty)
        detail.update({"boost": boost["key"], "qty": qty})

    elif op == "grant_cosmetic":
        key = str(p.get("cosmetic_key"))
        if key not in snap.cosmetics:
            raise AppError("コスメティックが見つかりません", code="invalid_cosmetic")
        for uid in ids:
            await db.execute(insert(m.UserCosmetic).values(user_id=uid, cosmetic_key=key, source="admin")
                             .on_conflict_do_nothing())
        detail["cosmetic"] = key

    elif op == "clear_all_effects":
        res = await db.execute(delete(m.ActiveEffect))
        detail["removed"] = res.rowcount or 0

    elif op == "reset_all_cooldowns":
        await db.execute(update(m.User).where(audience).values(next_roll_at=None))
        detail["reset"] = len(ids)

    elif op == "wipe_market":
        res = await db.execute(update(m.MarketListing).where(m.MarketListing.status == "active")
                               .values(status="cancelled", closed_at=utcnow()))
        # Listed items go back to their sellers rather than vanishing.
        await db.execute(update(m.ItemInstance).where(m.ItemInstance.state == "listed").values(state="owned"))
        detail["cancelled"] = res.rowcount or 0

    elif op == "recompute_stats":
        from . import stats as stats_svc

        await stats_svc.recompute_owner_counts(db)
        await stats_svc.recompute_market_values(db)
        await stats_svc.recompute_net_worth(db, active_minutes=10**6)
        detail["recomputed"] = True

    else:
        raise AppError("不明な操作です", code="unknown_operation")

    await audit.record(db, principal, f"bulk_{op}", entity_type="server", entity_id=op,
                       new=detail, reason=reason, user_agent=user_agent)
    await db.commit()
    queue_event(db, "all", "admin_event", {"kind": "bulk", "op": op})
    return {"ok": True, **detail}
