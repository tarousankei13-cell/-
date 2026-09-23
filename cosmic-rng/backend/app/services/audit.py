"""Audit logging for every privileged operation."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import Principal
from ..models import AuditLog, User


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


async def record(db: AsyncSession, principal: Principal | None, action: str, *, target_user_id: int | None = None,
                 entity_type: str | None = None, entity_id: Any = None, old: Any = None, new: Any = None, reason: str = "",
                 user_agent: str | None = None) -> AuditLog:
    log = AuditLog(
        admin_id=principal.user.id if principal else None, target_user_id=target_user_id, action=action[:64],
        entity_type=entity_type, entity_id=str(entity_id)[:128] if entity_id is not None else None,
        old_value=_jsonable(old), new_value=_jsonable(new), reason=(reason or "")[:2000],
        admin_mode=bool(principal and principal.admin_mode), ip=principal.ip if principal else None,
        session_hash=principal.session_hash if principal else None, user_agent=(user_agent or "")[:256] or None,
    )
    db.add(log)
    await db.flush()
    return log


async def search(db: AsyncSession, *, admin_id: int | None = None, target_user_id: int | None = None, action: str | None = None,
                 entity_type: str | None = None, before_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    conds = []
    if admin_id:
        conds.append(AuditLog.admin_id == admin_id)
    if target_user_id:
        conds.append(AuditLog.target_user_id == target_user_id)
    if action:
        conds.append(AuditLog.action.ilike(f"%{action[:64]}%"))
    if entity_type:
        conds.append(AuditLog.entity_type == entity_type)
    if before_id:
        conds.append(AuditLog.id < before_id)
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))
    if conds:
        q = q.where(and_(*conds))
    rows = (await db.execute(q)).scalars().all()
    uids = {r.admin_id for r in rows if r.admin_id} | {r.target_user_id for r in rows if r.target_user_id}
    names = {u.id: u.display_name for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all()} if uids else {}
    return [{
        "id": r.id, "admin_id": r.admin_id, "admin_name": names.get(r.admin_id), "target_user_id": r.target_user_id,
        "target_name": names.get(r.target_user_id), "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
        "old": r.old_value, "new": r.new_value, "reason": r.reason, "admin_mode": r.admin_mode, "ip": r.ip,
        "session": r.session_hash, "created_at": r.created_at.isoformat(),
    } for r in rows]
