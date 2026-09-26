"""Active temporary effects (boosts, artifact effects, admin effects)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, NotFound
from ..core.timeutil import utcnow
from ..models import ActiveEffect, Inventory
from ..rng.modifiers import EffectData

MAX_ACTIVE_EFFECTS = 200


def to_data(e: ActiveEffect) -> EffectData:
    return EffectData(
        id=e.id, effect_type=e.effect_type, value=e.value, stack_mode=e.stack_mode, remaining_rolls=e.remaining_rolls,
        expires_at=e.expires_at, biome_keys=list(e.biome_keys or []), params=dict(e.params or {}), source_key=e.source_key,
        name=e.name, source_type=e.source_type,
    )


async def load_active(db: AsyncSession, user_id: int, now: datetime | None = None, lock: bool = False) -> list[ActiveEffect]:
    now = now or utcnow()
    q = select(ActiveEffect).where(
        ActiveEffect.user_id == user_id,
        or_(ActiveEffect.expires_at.is_(None), ActiveEffect.expires_at > now),
        or_(ActiveEffect.remaining_rolls.is_(None), ActiveEffect.remaining_rolls > 0),
    ).order_by(ActiveEffect.id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return list((await db.execute(q)).scalars().all())


async def apply_consumption(db: AsyncSession, rows: list[ActiveEffect], consumed: dict[int, int],
                            param_updates: dict[int, dict[str, Any]]) -> list[int]:
    """Decrement roll-scoped effects; delete exhausted ones. Returns removed ids."""
    removed: list[int] = []
    by_id = {r.id: r for r in rows}
    for eid, n in consumed.items():
        r = by_id.get(eid)
        if r is None or r.remaining_rolls is None:
            continue
        r.remaining_rolls = max(0, r.remaining_rolls - n)
        if r.remaining_rolls <= 0:
            await db.delete(r)
            removed.append(eid)
    for eid, params in param_updates.items():
        r = by_id.get(eid)
        if r is not None and eid not in removed:
            r.params = params
    return removed


async def add_effect(
    db: AsyncSession,
    user_id: int,
    *,
    source_type: str,
    source_key: str,
    name: str,
    effect_type: str,
    value: float = 0.0,
    stack_mode: str = "add",
    rolls: int | None = None,
    duration_sec: int | None = None,
    biome_keys: list[str] | None = None,
    params: dict[str, Any] | None = None,
    granted_by: int | None = None,
    now: datetime | None = None,
) -> ActiveEffect:
    now = now or utcnow()
    count = len(await load_active(db, user_id, now))
    if count >= MAX_ACTIVE_EFFECTS:
        raise AppError("有効な効果が多すぎます", code="too_many_effects")
    e = ActiveEffect(
        user_id=user_id, source_type=source_type, source_key=source_key, name=name[:96], effect_type=effect_type,
        value=float(value), stack_mode=stack_mode, remaining_rolls=rolls,
        expires_at=(now + timedelta(seconds=int(duration_sec))) if duration_sec else None,
        biome_keys=biome_keys or [], params=params or {}, granted_by=granted_by,
    )
    db.add(e)
    await db.flush()
    return e


async def use_boost(db: AsyncSession, user_id: int, boost_key: str, quantity: int = 1) -> list[ActiveEffect]:
    snap = get_registry().snap
    boost = snap.boosts.get(boost_key)
    if boost is None or not boost.get("is_active", True):
        raise NotFound("Boostが見つかりません")
    if quantity < 1 or quantity > 50:
        raise AppError("数量が不正です", code="invalid_quantity")
    res = await db.execute(
        update(Inventory)
        .where(Inventory.user_id == user_id, Inventory.boost_id == boost["id"], Inventory.quantity >= quantity)
        .values(quantity=Inventory.quantity - quantity)
        .returning(Inventory.quantity)
    )
    if res.scalar_one_or_none() is None:
        raise AppError("Boostを所持していません", code="not_enough_boosts")
    out = []
    for _ in range(quantity):
        out.append(await add_effect(
            db, user_id, source_type="boost", source_key=boost["key"], name=boost["name"], effect_type=boost["effect_type"],
            value=boost["value"], stack_mode=boost["stack_mode"], rolls=boost["rolls"], duration_sec=boost["duration_sec"],
            biome_keys=boost["biome_keys"], params=boost["params"],
        ))
    return out


async def grant_boost_items(db: AsyncSession, user_id: int, boost_key: str, quantity: int) -> None:
    from ..db import upsert as insert

    snap = get_registry().snap
    boost = snap.boosts.get(boost_key)
    if boost is None:
        raise NotFound(f"Boost {boost_key} が見つかりません")
    stmt = insert(Inventory).values(user_id=user_id, boost_id=boost["id"], quantity=quantity)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id", "boost_id"], set_={"quantity": Inventory.quantity + quantity})
    await db.execute(stmt)


async def cleanup_expired(db: AsyncSession) -> int:
    now = utcnow()
    res = await db.execute(
        delete(ActiveEffect).where(or_(ActiveEffect.expires_at <= now, ActiveEffect.remaining_rolls <= 0))
    )
    await db.commit()
    return res.rowcount or 0


def public(e: ActiveEffect) -> dict[str, Any]:
    params = {k: v for k, v in (e.params or {}).items() if k not in ("u",)}
    return {
        "id": e.id, "name": e.name, "source_type": e.source_type, "source_key": e.source_key, "effect_type": e.effect_type,
        "value": e.value, "stack_mode": e.stack_mode, "remaining_rolls": e.remaining_rolls,
        "expires_at": e.expires_at.isoformat() if e.expires_at else None, "biome_keys": e.biome_keys or [], "params": params,
    }


async def boost_inventory(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    snap = get_registry().snap
    rows = (await db.execute(select(Inventory).where(Inventory.user_id == user_id, Inventory.quantity > 0))).scalars().all()
    out = []
    for r in rows:
        b = snap.boosts_by_id.get(r.boost_id)
        if not b:
            continue
        out.append({
            "key": b["key"], "name": b["name"], "description": b["description"], "effect_type": b["effect_type"],
            "value": b["value"], "rolls": b["rolls"], "duration_sec": b["duration_sec"], "stack_mode": b["stack_mode"],
            "biome_keys": b["biome_keys"], "rarity": b["rarity_key"], "visual": b["visual"], "quantity": r.quantity,
            "sell_value": b["sell_value"],
        })
    out.sort(key=lambda x: (snap.tier_of(x["rarity"]), x["name"]))
    return out
