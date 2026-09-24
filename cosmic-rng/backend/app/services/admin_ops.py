"""Admin operations: dashboard, user management, game settings, logs, RNG simulation."""
from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models as m
from ..content.registry import bump_content_version, get_registry
from ..content.settings_schema import DEF_MAP, definitions_public, validate_setting
from ..db import is_sqlite, upsert as insert
from ..core.errors import AppError, Forbidden, NotFound
from ..core.pubsub import queue_event
from ..core.security import Principal, compute_admin, revoke_user_sessions
from ..core.timeutil import utcnow
from ..rng import engine as rng_engine
from ..rng.engine import RollContext, compile_table
from . import artifacts as artifacts_svc
from . import audit
from . import biomes as biomes_svc
from . import effects as effects_svc
from . import equipment as equipment_svc
from . import feed as feed_svc
from . import inventory as inv_svc
from .constants import TIER_BY_KEY
from .metrics import metrics
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
        "biomes": [{"key": k, "name": snap.biomes[k].name if k in snap.biomes else k, "count": c} for k, c in biome_rows],
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
        "biome_keys": [{"key": b.key, "name": b.name, "kind": b.kind} for b in sorted(snap.biomes.values(), key=lambda b: b.sort_order)],
    }


DANGEROUS = {"ban", "delete_instances", "set_role", "adjust_stardust", "reset_user"}


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
