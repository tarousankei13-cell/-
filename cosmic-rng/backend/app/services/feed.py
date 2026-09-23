"""World feed (rare drops, first discoveries, world firsts, admin events) and notifications."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.pubsub import queue_event
from ..models import Notification, User, WorldEvent
from .users import user_brief


async def add_world_event(db: AsyncSession, type_: str, user: User | None, payload: dict[str, Any], public_user: bool = True) -> None:
    if not get_registry().setting("features.world_feed_enabled"):
        return
    ev = WorldEvent(type=type_, user_id=user.id if user else None, public=public_user, payload=payload)
    db.add(ev)
    await db.flush()
    queue_event(db, "all", "feed", event_public(ev, user))


def event_public(ev: WorldEvent, user: User | None) -> dict[str, Any]:
    return {
        "id": ev.id, "type": ev.type, "payload": ev.payload,
        "user": user_brief(user) if (user is not None and ev.public) else None,
        "anonymous": user is not None and not ev.public,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


async def recent(db: AsyncSession, limit: int | None = None, types: list[str] | None = None) -> list[dict[str, Any]]:
    limit = min(int(limit or get_registry().setting("feed.size") or 50), 200)
    q = select(WorldEvent, User).outerjoin(User, User.id == WorldEvent.user_id)
    if types:
        q = q.where(WorldEvent.type.in_(types))
    rows = (await db.execute(q.order_by(WorldEvent.id.desc()).limit(limit))).all()
    return [event_public(ev, u) for ev, u in rows]


async def notify_user(db: AsyncSession, user_id: int, type_: str, title: str, body: str = "", data: dict[str, Any] | None = None) -> None:
    n = Notification(user_id=user_id, type=type_, title=title[:160], body=body, data=data or {})
    db.add(n)
    await db.flush()
    queue_event(db, "user", "notify", notification_public(n), user_id=user_id)


async def notify_admins(db: AsyncSession, type_: str, title: str, body: str = "", data: dict[str, Any] | None = None) -> None:
    n = Notification(for_admins=True, type=type_, title=title[:160], body=body, data=data or {})
    db.add(n)
    await db.flush()
    queue_event(db, "admins", "admin_notify", notification_public(n))


def notification_public(n: Notification) -> dict[str, Any]:
    return {"id": n.id, "type": n.type, "title": n.title, "body": n.body, "data": n.data, "read": n.read,
            "created_at": n.created_at.isoformat() if n.created_at else None}


async def list_notifications(db: AsyncSession, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Notification).where(Notification.user_id == user_id)
                             .order_by(Notification.id.desc()).limit(min(limit, 200)))).scalars().all()
    return [notification_public(n) for n in rows]


async def mark_read(db: AsyncSession, user_id: int, ids: list[int] | None) -> None:
    q = update(Notification).where(Notification.user_id == user_id)
    if ids:
        q = q.where(Notification.id.in_(ids[:500]))
    await db.execute(q.values(read=True))


async def unread_count(db: AsyncSession, user_id: int) -> int:
    from sqlalchemy import func

    return int((await db.execute(select(func.count()).select_from(Notification)
                                 .where(Notification.user_id == user_id, Notification.read.is_(False)))).scalar_one())
