"""Persistence + orchestration for the per-player biome process."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import Snapshot, get_registry
from ..core.errors import AppError, NotFound
from ..core.timeutil import utcnow
from ..models import ActiveEffect, User, UserBiome
from ..rng import biome_engine as be
from ..rng.engine import system_rng
from ..rng.modifiers import EquipData, passive_product


@dataclass(slots=True)
class BiomeContext:
    row: UserBiome
    state: be.BiomeStateData
    result: be.AdvanceResult


def row_to_state(r: UserBiome) -> be.BiomeStateData:
    return be.BiomeStateData(
        biome_key=r.biome_key, started_at=r.started_at, ends_at=r.ends_at, next_eval_at=r.next_eval_at,
        next_biome_key=r.next_biome_key, state_key=r.state_key, state_ends_at=r.state_ends_at, next_state_at=r.next_state_at,
        next_state_key=r.next_state_key, forced=r.forced, forced_by=r.forced_by, locked_until=r.locked_until,
        sample_sig=r.sample_sig, evaluated_at=r.evaluated_at,
    )


def state_to_row(st: be.BiomeStateData, r: UserBiome) -> None:
    r.biome_key = st.biome_key
    r.started_at = st.started_at
    r.ends_at = st.ends_at
    r.next_eval_at = st.next_eval_at
    r.next_biome_key = st.next_biome_key
    r.state_key = st.state_key
    r.state_ends_at = st.state_ends_at
    r.next_state_at = st.next_state_at
    r.next_state_key = st.next_state_key
    r.forced = st.forced
    r.forced_by = st.forced_by
    r.locked_until = st.locked_until
    r.sample_sig = st.sample_sig
    r.evaluated_at = st.evaluated_at or utcnow()


def chance_multiplier(snap: Snapshot, equips: list[EquipData], effects: list[ActiveEffect], now: datetime) -> float:
    mult = float(snap.settings.get("biome.global_chance_mult", 1.0) or 0.0)
    mult *= passive_product(equips, "biome_chance")
    best_by_source: dict[str, float] = {}
    for e in effects:
        if e.effect_type != "biome_chance":
            continue
        if e.expires_at is not None and e.expires_at <= now:
            continue
        best_by_source[e.source_key] = max(best_by_source.get(e.source_key, 0.0), float(e.value))
    for v in best_by_source.values():
        mult *= max(v, 0.0)
    for ev in snap.active_events(now):
        if ev["type"] == "biome_chance":
            mult *= float((ev.get("params") or {}).get("mult", 1.0))
    return mult


async def load_row(db: AsyncSession, user: User, lock: bool = True) -> UserBiome:
    q = select(UserBiome).where(UserBiome.user_id == user.id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None:
        snap = get_registry().snap
        now = utcnow()
        st = be.initial_state(snap, now, user.level, 1.0, system_rng())
        row = UserBiome(user_id=user.id, biome_key=st.biome_key, started_at=now, next_eval_at=st.next_eval_at,
                        next_biome_key=st.next_biome_key, sample_sig=st.sample_sig, evaluated_at=now)
        db.add(row)
        await db.flush()
    return row


async def advance_user(
    db: AsyncSession, user: User, now: datetime, *, chance_mult: float, record_from: datetime | None = None,
    row: UserBiome | None = None,
) -> BiomeContext:
    snap = get_registry().snap
    row = row or await load_row(db, user)
    st = row_to_state(row)
    res = be.advance(
        st, snap, now, level=user.level, chance_mult=chance_mult, rng=system_rng(),
        enabled=bool(snap.settings.get("features.biome_enabled", True)), record_from=record_from,
    )
    state_to_row(st, row)
    return BiomeContext(row=row, state=st, result=res)


async def force_biome(db: AsyncSession, user: User, biome_key: str, duration: int | None, by: int | None,
                      allow_admin: bool = False) -> BiomeContext:
    snap = get_registry().snap
    biome = snap.biomes.get(biome_key)
    if biome is None:
        raise NotFound("Biomeが見つかりません")
    if biome.kind == "admin" and not allow_admin:
        raise AppError("このBiomeは付与できません", code="biome_restricted")
    now = utcnow()
    row = await load_row(db, user)
    st = row_to_state(row)
    if biome.kind == "default":
        be._return_to_default(st, snap, now, user.level, 1.0, system_rng(), True)  # noqa: SLF001
    else:
        be.enter_biome(st, snap, biome_key, now, system_rng(), duration=duration, forced=True, forced_by=by)
    st.evaluated_at = now
    state_to_row(st, row)
    return BiomeContext(row=row, state=st, result=be.AdvanceResult(changed=True))


async def lock_biome(db: AsyncSession, user: User, seconds: int) -> UserBiome:
    row = await load_row(db, user)
    now = utcnow()
    until = now + timedelta(seconds=seconds)
    row.locked_until = max(row.locked_until or now, until)
    if row.ends_at is not None and row.ends_at < row.locked_until:
        row.ends_at = row.locked_until
        row.next_eval_at = row.locked_until
    elif row.ends_at is None and row.next_eval_at < row.locked_until:
        row.next_eval_at = row.locked_until
    return row


def public_state(ctx_row: UserBiome, reveal: bool = False) -> dict[str, Any]:
    snap = get_registry().snap
    return be.to_public(row_to_state(ctx_row), snap, reveal)


def biome_luck(snap: Snapshot, st: be.BiomeStateData) -> float:
    biome = snap.biomes.get(st.biome_key) or snap.default_biome
    mult = biome.luck_mult
    if st.state_key:
        state = next((s for s in biome.states if s.key == st.state_key), None)
        if state:
            mult *= state.luck_mult
    return mult


def is_announce_worthy(snap: Snapshot, biome_key: str) -> bool:
    b = snap.biomes.get(biome_key)
    if b is None or b.kind == "default":
        return False
    if b.kind == "admin":
        return b.announce
    return b.announce or (b.odds_per_sec or 0) >= float(snap.settings.get("biome.announce_min_odds", 50000))
