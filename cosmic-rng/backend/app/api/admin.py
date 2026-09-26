"""Admin API. Every route requires a server-verified administrator (404 otherwise)
and, except for read-only bootstrap, an active Admin Mode session."""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import bump_content_version, get_registry
from ..core.errors import AppError
from ..core.pubsub import bus, commit_and_publish
from ..core.security import Principal, require_admin
from ..db import get_db, session_scope
from ..services import admin_content, admin_ops, artifacts as artifacts_svc, audit, backup as backup_svc
from ..services import biomes as biomes_svc
from ..services import feed as feed_svc

router = APIRouter(prefix="/api/admin", tags=["admin"])
ADMIN = Depends(require_admin())
ADMIN_ANY_MODE = Depends(require_admin(admin_mode=False))
SUPER = Depends(require_admin(super_admin=True))


class Reason(BaseModel):
    reason: str = Field(default="", max_length=2000)


@router.get("/bootstrap")
async def bootstrap(principal: Principal = ADMIN_ANY_MODE) -> dict[str, Any]:
    snap = get_registry().snap
    return {
        "admin_mode": principal.admin_mode, "is_super_admin": principal.is_super_admin,
        "content_types": admin_content.type_meta(),
        "artifacts": [artifacts_svc.artifact_public(a) for a in sorted(snap.artifacts.values(), key=lambda a: a["sort_order"])],
        "biomes": [{"key": b.key, "name": b.name, "kind": b.kind} for b in sorted(snap.biomes.values(), key=lambda b: b.sort_order)],
        "items": [{"key": i.key, "name": i.name, "rarity": i.rarity_key, "odds": i.odds} for i in
                  sorted(snap.items.values(), key=lambda i: (i.tier, i.odds or 0)) if i.kind != "admin_artifact"],
        "boosts": [{"key": b["key"], "name": b["name"], "name_ja": b.get("name_ja") or ""} for b in snap.boosts.values()],
        "equipment": [{"key": e["key"], "name": e["name"], "name_ja": e.get("name_ja") or "", "slot": e.get("slot")}
                      for e in sorted(snap.equipment.values(), key=lambda e: e.get("sort_order", 0))],
        "cosmetics": [{"key": c["key"], "name": c["name"], "name_ja": c.get("name_ja") or "", "kind": c.get("kind")}
                      for c in sorted(snap.cosmetics.values(), key=lambda c: (c.get("kind", ""), c.get("sort_order", 0)))],
        "achievements": [{"key": a["key"], "name": a["name"], "name_ja": a.get("name_ja") or ""}
                         for a in sorted(snap.achievements.values(), key=lambda a: a.get("sort_order", 0))],
        "effect_types": list(admin_content.EFFECTS),
        "user_actions": admin_ops.USER_ACTIONS,
        "bulk_operations": admin_ops.BULK_OPERATIONS,
    }


@router.get("/dashboard")
async def dashboard(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await admin_ops.dashboard(db)


@router.get("/retention")
async def retention(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db),
                    days: int = Query(default=14, ge=1, le=90)) -> dict[str, Any]:
    return await admin_ops.retention(db, days)


# --- Users -------------------------------------------------------------------
@router.get("/users")
async def users(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db), q: str = Query(default="", max_length=64)) -> dict[str, Any]:
    return {"users": await admin_ops.search_users(db, q)}


@router.get("/users/{user_id}")
async def user_detail(user_id: int, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await admin_ops.user_detail(db, user_id)


@router.get("/users/{user_id}/table")
async def user_table(user_id: int, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await admin_ops.user_table(db, user_id)
    await commit_and_publish(db)
    return result


class UserActionBody(BaseModel):
    action: str = Field(max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)


@router.post("/users/{user_id}/action")
async def user_action(user_id: int, body: UserActionBody, request: Request, principal: Principal = ADMIN,
                      db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await admin_ops.user_action(db, principal, user_id, body.action, body.params, body.reason,
                                         request.headers.get("user-agent"))
    await commit_and_publish(db)
    return result


class BulkBody(BaseModel):
    op: str = Field(max_length=48)
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=400)


@router.post("/bulk")
async def bulk_operation(body: BulkBody, request: Request, principal: Principal = ADMIN,
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Server-wide operations. Audited as one entry naming how many rows it touched."""
    return await admin_ops.bulk_operation(db, principal, body.op, body.params, body.reason,
                                          request.headers.get("user-agent"))


# --- Content -----------------------------------------------------------------
@router.get("/content/{type_key}")
async def content_list(type_key: str, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db),
                       q: str = Query(default="", max_length=64), page: int = Query(default=1, ge=1)) -> dict[str, Any]:
    return await admin_content.list_rows(db, type_key, q, page)


@router.get("/content/{type_key}/{key}")
async def content_get(type_key: str, key: str, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await admin_content.get_row(db, type_key, key)


class ContentBody(BaseModel):
    data: dict[str, Any]
    reason: str = Field(default="", max_length=2000)
    temporary_hours: float | None = Field(default=None, gt=0, le=24 * 90)


@router.post("/content/{type_key}")
async def content_create(type_key: str, body: ContentBody, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await admin_content.create_row(db, principal, type_key, body.data, body.reason)
    await db.commit()
    await _reload(db)
    return row


@router.put("/content/{type_key}/{key}")
async def content_update(type_key: str, key: str, body: ContentBody, principal: Principal = ADMIN,
                         db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    row = await admin_content.update_row(db, principal, type_key, key, body.data, body.reason, body.temporary_hours)
    await db.commit()
    await _reload(db)
    return row


@router.delete("/content/{type_key}/{key}")
async def content_delete(type_key: str, key: str, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db),
                         reason: str = Query(default="", max_length=2000)) -> dict[str, Any]:
    result = await admin_content.delete_row(db, principal, type_key, key, reason)
    await db.commit()
    await _reload(db)
    return result


@router.get("/overrides")
async def overrides(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"overrides": await admin_content.list_overrides(db)}


@router.delete("/overrides/{override_id}")
async def revoke_override(override_id: int, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db),
                          reason: str = Query(default="", max_length=2000)) -> dict[str, Any]:
    await admin_content.revoke_override(db, principal, override_id, reason)
    await db.commit()
    await _reload(db)
    return {"ok": True}


async def _reload(db: AsyncSession) -> None:
    from ..rng.engine import table_cache

    await get_registry().ensure_fresh(db, force=True)
    table_cache.clear()


# --- Settings ----------------------------------------------------------------
@router.get("/settings")
async def settings(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await admin_ops.get_settings(db)


class SettingsBody(BaseModel):
    values: dict[str, Any]
    reason: str = Field(default="", max_length=2000)


@router.put("/settings")
async def settings_update(body: SettingsBody, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await admin_ops.update_settings(db, principal, body.values, body.reason)
    await commit_and_publish(db)
    await _reload(db)
    return result


# --- Logs ----------------------------------------------------------------------
@router.get("/audit")
async def audit_logs(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db), admin_id: int | None = None,
                     target_user_id: int | None = None, action: str | None = Query(default=None, max_length=64),
                     entity_type: str | None = Query(default=None, max_length=32), before_id: int | None = None) -> dict[str, Any]:
    return {"logs": await audit.search(db, admin_id=admin_id, target_user_id=target_user_id, action=action, entity_type=entity_type,
                                       before_id=before_id)}


@router.get("/rolls")
async def rolls(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db), user_id: int | None = None,
                item_key: str | None = Query(default=None, max_length=128), min_tier: int | None = Query(default=None, ge=1, le=8),
                flags: int | None = None, before_id: int | None = None) -> dict[str, Any]:
    return {"rolls": await admin_ops.search_rolls(db, user_id=user_id, item_key=item_key, min_tier=min_tier, flags=flags, before_id=before_id)}


@router.get("/trades")
async def trades(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db), user_id: int | None = None,
                 item_key: str | None = Query(default=None, max_length=128), instance_id: int | None = None,
                 status: str | None = Query(default=None, max_length=16)) -> dict[str, Any]:
    return {"trades": await admin_ops.search_trades(db, user_id=user_id, item_key=item_key, instance_id=instance_id, status=status)}


@router.get("/market")
async def market(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db), flagged: bool = False,
                 user_id: int | None = None) -> dict[str, Any]:
    return {"listings": await admin_ops.search_market(db, flagged=flagged, user_id=user_id)}


class CancelListingBody(BaseModel):
    listing_id: int
    reason: str = Field(max_length=2000)


@router.post("/market/cancel")
async def market_cancel(body: CancelListingBody, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from ..services import market as market_svc

    await market_svc.cancel_listing(db, principal.user.id, body.listing_id, by_admin=True)
    await audit.record(db, principal, "market_cancel", entity_type="listing", entity_id=body.listing_id, reason=body.reason)
    await commit_and_publish(db)
    return {"ok": True}


@router.get("/errors")
async def errors(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"errors": await admin_ops.errors(db)}


@router.get("/notifications")
async def admin_notifications(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from sqlalchemy import select

    from ..models import Notification

    rows = (await db.execute(select(Notification).where(Notification.for_admins.is_(True)).order_by(Notification.id.desc()).limit(100))).scalars().all()
    return {"notifications": [feed_svc.notification_public(n) for n in rows]}


# --- RNG -------------------------------------------------------------------------
class SimBody(BaseModel):
    luck: float = Field(default=1.0, gt=0, lt=1e300)
    biome_key: str = Field(default="stellar_drift", max_length=48)
    state: str | None = Field(default=None, max_length=48)
    special: bool = False
    n: int = Field(default=100000, ge=1, le=2_000_000)
    min_tier: int = Field(default=0, ge=0, le=7)
    seed: int | None = None


@router.post("/rng/simulate")
async def simulate(body: SimBody, principal: Principal = ADMIN) -> dict[str, Any]:
    return await admin_ops.simulate(body.luck, body.biome_key, body.n, state=body.state, special=body.special,
                                    min_tier=body.min_tier, seed=body.seed)


# --- Admin self-service (biomes, artifacts) --------------------------------------
class TeleportBody(BaseModel):
    biome_key: str = Field(max_length=48)
    duration: int | None = Field(default=None, ge=1, le=86400)


@router.post("/biome")
async def admin_biome(body: TeleportBody, request: Request, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Admins move freely between biomes (including admin biomes)."""
    from ..core.pubsub import queue_event
    from ..services import users as users_svc

    user = await users_svc.lock_user(db, principal.user.id)
    ctx = await biomes_svc.force_biome(db, user, body.biome_key, body.duration, principal.user.id, allow_admin=True)
    await audit.record(db, principal, "admin_biome_self", target_user_id=user.id, entity_type="biome", entity_id=body.biome_key,
                       new={"duration": body.duration}, user_agent=request.headers.get("user-agent"))
    queue_event(db, "user", "biome", biomes_svc.public_state(ctx.row), user_id=user.id)
    await commit_and_publish(db)
    return {"biome": biomes_svc.public_state(ctx.row)}


class GrantBody(BaseModel):
    target_user_id: int
    artifact_key: str = Field(max_length=64)
    can_use: bool = False
    uses: int | None = Field(default=None, ge=1, le=1000)
    hours: float | None = Field(default=None, gt=0, le=24 * 365)
    reason: str = Field(max_length=2000)


@router.post("/artifacts/grant")
async def grant_artifact(body: GrantBody, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not body.reason.strip():
        raise AppError("理由を入力してください", code="reason_required")
    result = await artifacts_svc.grant(db, principal, body.target_user_id, body.artifact_key, can_use=body.can_use, uses=body.uses,
                                       hours=body.hours, reason=body.reason)
    await commit_and_publish(db)
    return result


class RecallBody(BaseModel):
    instance_id: int
    reason: str = Field(max_length=2000)


@router.post("/artifacts/recall")
async def recall_artifact(body: RecallBody, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if not body.reason.strip():
        raise AppError("理由を入力してください", code="reason_required")
    result = await artifacts_svc.recall(db, principal, body.instance_id, body.reason)
    await commit_and_publish(db)
    return result


@router.get("/artifacts/holders")
async def artifact_holders(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from sqlalchemy import select

    from ..models import ItemInstance, User
    from ..services.users import user_brief

    snap = get_registry().snap
    ids = list(snap.artifacts_by_item.keys())
    rows = (await db.execute(select(ItemInstance, User).join(User, User.id == ItemInstance.owner_id)
                             .where(ItemInstance.item_id.in_(ids or [0])).order_by(ItemInstance.id.desc()).limit(500))).all()
    return {"holders": [{"instance_id": i.id, "artifact": snap.artifacts_by_item[i.item_id]["key"], "state": i.state,
                         "user": user_brief(u), "obtained_at": i.obtained_at.isoformat(), "meta": i.meta} for i, u in rows]}


class BroadcastBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=1000)
    style: str = Field(default="info", pattern="^(info|warning|cosmic)$")


@router.post("/broadcast")
async def broadcast(body: BroadcastBody, principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await audit.record(db, principal, "broadcast", entity_type="world", new=body.model_dump())
    await feed_svc.add_world_event(db, "admin_broadcast", principal.user, body.model_dump(), True)
    await commit_and_publish(db)
    await bus.publish("all", "admin_event", {"kind": "broadcast", **body.model_dump()})
    return {"ok": True}


# --- Backup (super admin) ------------------------------------------------------------
@router.get("/backups")
async def backups(principal: Principal = ADMIN, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return {"backups": await backup_svc.list_backups(db)}


class BackupBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/backups")
async def create_backup(body: BackupBody, principal: Principal = SUPER, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await audit.record(db, principal, "backup_create", entity_type="backup", new={"note": body.note})
    await db.commit()
    return await backup_svc.create(db, kind="manual", by=principal.user.id, note=body.note)


class RestoreBody(BaseModel):
    filename: str = Field(max_length=128)
    confirm: str = Field(max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/backups/restore")
async def restore_backup(body: RestoreBody, principal: Principal = SUPER, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if body.confirm != f"RESTORE {body.filename}":
        raise AppError(f"確認文字列が一致しません。「RESTORE {body.filename}」と入力してください", code="confirmation_required")
    backup_svc.resolve_file(body.filename)
    await audit.record(db, principal, "backup_restore_start", entity_type="backup", entity_id=body.filename, reason=body.reason)
    await db.commit()
    await bus.publish("all", "maintenance", {"enabled": True, "message": "データベースを復元しています…"})
    ok, err = await backup_svc.restore(body.filename)
    async with session_scope() as s2:
        # The admin's session row may not exist in the restored data, so the audit entry carries the admin id explicitly.
        await audit.record(s2, None, "backup_restore_done" if ok else "backup_restore_failed", entity_type="backup",
                           entity_id=body.filename, new={"admin_id": principal.user.id, "error": err[-500:] if not ok else None},
                           reason=body.reason)
        await bump_content_version(s2, None)
        await s2.commit()
        await get_registry().ensure_fresh(s2, force=True)
    from ..rng.engine import table_cache

    table_cache.clear()
    await bus.publish("all", "maintenance", {"enabled": False, "message": ""})
    await bus.publish("all", "refresh", {"reason": "restore"})
    if not ok:
        raise AppError("復元に失敗しました。サーバーログを確認してください", code="restore_failed", status_code=500)
    if os.environ.get("COSMIC_RESTART_AFTER_RESTORE", "1") == "1":
        # Graceful restart under systemd (Restart=always) so every in-memory cache starts clean.
        asyncio.get_running_loop().call_later(2.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"ok": True, "restarting": True}


class DeleteBackupBody(BaseModel):
    filename: str = Field(max_length=128)
    confirm: str = Field(max_length=200)


@router.post("/backups/delete")
async def delete_backup(body: DeleteBackupBody, principal: Principal = SUPER, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if body.confirm != f"DELETE {body.filename}":
        raise AppError(f"確認文字列が一致しません。「DELETE {body.filename}」と入力してください", code="confirmation_required")
    await backup_svc.delete_file(db, body.filename)
    await audit.record(db, principal, "backup_delete", entity_type="backup", entity_id=body.filename)
    await db.commit()
    return {"ok": True}
