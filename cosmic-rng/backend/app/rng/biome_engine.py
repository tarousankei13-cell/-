"""Per-player biome simulation.

Every second, each eligible natural biome *b* has a chance ``q_b = mult / N_b``
to start while the player is in the default biome. Instead of ticking every
player every second, the process is simulated event-to-event: the waiting time
until the first trigger is geometric with ``s = 1 - prod(1 - q_b)`` and the
triggered biome is chosen rarest-first (the rarest biome wins ties within the
same second). This is statistically identical to a per-second check, costs
O(transitions) instead of O(seconds) and lets offline windows be replayed
exactly. Biomes with special states (e.g. Singularity → Event Horizon) run the
same process for their states while active.

The pending next event is pre-sampled and stored server-side (never sent to
normal clients). Because the process is memoryless, it is re-sampled whenever
its parameters change (content version, biome chance multiplier, level).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..content.registry import BiomeDef, BiomeState, Snapshot
from .engine import RandomSource

FAR_FUTURE_SECONDS = 3600
MAX_TRANSITIONS = 200000
CATCHUP_HORIZON = timedelta(days=1)


@dataclass(slots=True)
class BiomeStateData:
    biome_key: str
    started_at: datetime
    ends_at: datetime | None
    next_eval_at: datetime
    next_biome_key: str | None = None
    state_key: str | None = None
    state_ends_at: datetime | None = None
    next_state_at: datetime | None = None
    next_state_key: str | None = None
    forced: bool = False
    forced_by: int | None = None
    locked_until: datetime | None = None
    sample_sig: str = ""
    evaluated_at: datetime | None = None


@dataclass(slots=True)
class Segment:
    biome_key: str
    state_key: str | None
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(slots=True)
class Transition:
    at: datetime
    kind: str  # biome / state
    from_key: str | None
    to_key: str | None


@dataclass(slots=True)
class AdvanceResult:
    segments: list[Segment] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    changed: bool = False


def eligible_natural(snap: Snapshot, level: int) -> list[BiomeDef]:
    return [b for b in snap.natural_biomes if b.min_level <= level]


def signature(snap: Snapshot, level: int, chance_mult: float) -> str:
    lvl_bucket = sum(1 for b in snap.natural_biomes if b.min_level <= level)
    return f"{snap.version}:{lvl_bucket}:{chance_mult:.6g}"


def _geometric_seconds(s: float, rng: RandomSource) -> int:
    if s >= 1.0:
        return 1
    if s <= 0.0:
        return -1
    u = rng.random()
    while u <= 0.0:
        u = rng.random()
    # number of Bernoulli(s) trials until first success
    return int(math.floor(math.log(u) / math.log1p(-s))) + 1


def _cascade_pick(options: list[tuple[str, float]], rng: RandomSource) -> tuple[float, str | None]:
    """options sorted rarest first as (key, q). Returns (s, chosen_key)."""
    none = 1.0
    weights: list[tuple[str, float]] = []
    for key, q in options:
        q = min(max(q, 0.0), 1.0)
        weights.append((key, none * q))
        none *= 1.0 - q
    s = 1.0 - none
    if s <= 0.0:
        return 0.0, None
    u = rng.random() * s
    acc = 0.0
    for key, w in weights:
        acc += w
        if u < acc:
            return s, key
    return s, weights[-1][0]


def sample_next_biome(snap: Snapshot, level: int, chance_mult: float, rng: RandomSource) -> tuple[int, str | None]:
    options = [(b.key, chance_mult / float(b.odds_per_sec or 1e18)) for b in eligible_natural(snap, level)]
    options.sort(key=lambda kv: kv[1])  # rarest first
    s, key = _cascade_pick(options, rng)
    if key is None:
        return -1, None
    return _geometric_seconds(s, rng), key


def sample_next_state(states: list[BiomeState], rng: RandomSource) -> tuple[int, str | None]:
    options = sorted([(st.key, 1.0 / max(st.odds_per_sec, 1.0)) for st in states], key=lambda kv: kv[1])
    s, key = _cascade_pick(options, rng)
    if key is None:
        return -1, None
    return _geometric_seconds(s, rng), key


def initial_state(snap: Snapshot, now: datetime, level: int, chance_mult: float, rng: RandomSource, enabled: bool = True) -> BiomeStateData:
    st = BiomeStateData(biome_key=snap.default_biome.key, started_at=now, ends_at=None, next_eval_at=now, evaluated_at=now)
    _schedule_natural(st, snap, now, level, chance_mult, rng, enabled)
    return st


def _schedule_natural(st: BiomeStateData, snap: Snapshot, t: datetime, level: int, chance_mult: float, rng: RandomSource,
                      enabled: bool) -> None:
    start = t
    if st.locked_until and st.locked_until > start:
        start = st.locked_until
    secs, key = sample_next_biome(snap, level, chance_mult, rng) if enabled else (-1, None)
    if key is None or secs < 0:
        st.next_eval_at = start + timedelta(seconds=FAR_FUTURE_SECONDS)
        st.next_biome_key = None
    else:
        st.next_eval_at = start + timedelta(seconds=secs)
        st.next_biome_key = key
    st.sample_sig = signature(snap, level, chance_mult)


def _schedule_state(st: BiomeStateData, biome: BiomeDef | None, t: datetime, rng: RandomSource) -> None:
    st.next_state_at = None
    st.next_state_key = None
    if biome is None or not biome.states:
        return
    secs, key = sample_next_state(biome.states, rng)
    if key is None or secs < 0:
        return
    at = t + timedelta(seconds=secs)
    if st.ends_at is not None and at >= st.ends_at:
        return
    st.next_state_at = at
    st.next_state_key = key


def enter_biome(st: BiomeStateData, snap: Snapshot, biome_key: str, t: datetime, rng: RandomSource,
                duration: int | None = None, forced: bool = False, forced_by: int | None = None) -> None:
    biome = snap.biomes.get(biome_key)
    if biome is None:
        return
    dur = max(1, int(duration if duration is not None else biome.duration_sec or 60))
    st.biome_key = biome_key
    st.started_at = t
    st.ends_at = t + timedelta(seconds=dur)
    if st.locked_until and st.locked_until > st.ends_at:
        st.ends_at = st.locked_until
    st.next_eval_at = st.ends_at
    st.next_biome_key = None
    st.state_key = None
    st.state_ends_at = None
    st.forced = forced
    st.forced_by = forced_by
    _schedule_state(st, biome, t, rng)


def _return_to_default(st: BiomeStateData, snap: Snapshot, t: datetime, level: int, chance_mult: float, rng: RandomSource,
                       enabled: bool) -> None:
    st.biome_key = snap.default_biome.key
    st.started_at = t
    st.ends_at = None
    st.state_key = None
    st.state_ends_at = None
    st.next_state_at = None
    st.next_state_key = None
    st.forced = False
    st.forced_by = None
    _schedule_natural(st, snap, t, level, chance_mult, rng, enabled)


def advance(
    st: BiomeStateData,
    snap: Snapshot,
    until: datetime,
    *,
    level: int,
    chance_mult: float,
    rng: RandomSource,
    enabled: bool = True,
    record_from: datetime | None = None,
) -> AdvanceResult:
    """Advance the biome process to ``until``. Segments are recorded from ``record_from``."""
    res = AdvanceResult()
    default_key = snap.default_biome.key

    # Unknown / deleted biome → reset.
    if st.biome_key not in snap.biomes:
        _return_to_default(st, snap, until, level, chance_mult, rng, enabled)
        res.changed = True
    # Parameters changed while waiting in the default biome → resample (memoryless).
    sig = signature(snap, level, chance_mult)
    if st.biome_key == default_key and st.sample_sig != sig:
        base = st.evaluated_at or st.started_at
        _schedule_natural(st, snap, min(base, until), level, chance_mult, rng, enabled)
        res.changed = True

    rec_from = record_from or until
    # Long-absent player: the pending event is far in the past. Everything before
    # the recording window is unobservable, so restart the (memoryless) process
    # shortly before it instead of replaying months of transitions.
    horizon = min(rec_from, until) - CATCHUP_HORIZON
    if st.next_eval_at < horizon:
        if st.locked_until and st.locked_until < horizon:
            st.locked_until = None
        _return_to_default(st, snap, horizon, level, chance_mult, rng, enabled)
        res.changed = True
    seg_start = rec_from
    iterations = 0
    while iterations < MAX_TRANSITIONS:
        iterations += 1
        candidates = [st.next_eval_at]
        if st.state_ends_at is not None:
            candidates.append(st.state_ends_at)
        if st.next_state_at is not None:
            candidates.append(st.next_state_at)
        t = min(candidates)
        if t > until:
            break
        if t > seg_start:
            res.segments.append(Segment(st.biome_key, st.state_key, seg_start, t))
            seg_start = t
        biome = snap.biomes.get(st.biome_key)
        if st.state_ends_at is not None and t == st.state_ends_at:
            prev = st.state_key
            st.state_key = None
            st.state_ends_at = None
            _schedule_state(st, biome, t, rng)
            res.transitions.append(Transition(t, "state", prev, None))
        elif st.next_state_at is not None and t == st.next_state_at:
            state = next((s for s in (biome.states if biome else []) if s.key == st.next_state_key), None)
            st.next_state_at = None
            st.next_state_key = None
            if state is not None:
                st.state_key = state.key
                end = t + timedelta(seconds=max(1, state.duration_sec))
                if st.ends_at is not None and end > st.ends_at:
                    end = st.ends_at
                st.state_ends_at = end
                res.transitions.append(Transition(t, "state", None, state.key))
        else:  # next_eval_at
            if st.biome_key == default_key:
                target = st.next_biome_key
                if target and target in snap.biomes and snap.biomes[target].is_active and enabled:
                    enter_biome(st, snap, target, t, rng)
                    res.transitions.append(Transition(t, "biome", default_key, target))
                else:
                    _schedule_natural(st, snap, t, level, chance_mult, rng, enabled)
            else:
                if st.locked_until and st.locked_until > t:
                    st.ends_at = st.locked_until
                    st.next_eval_at = st.locked_until
                    continue
                prev = st.biome_key
                _return_to_default(st, snap, t, level, chance_mult, rng, enabled)
                res.transitions.append(Transition(t, "biome", prev, default_key))
        res.changed = True
    if until > seg_start:
        res.segments.append(Segment(st.biome_key, st.state_key, seg_start, until))
    st.evaluated_at = until
    return res


def to_public(st: BiomeStateData, snap: Snapshot, reveal: bool = False) -> dict[str, Any]:
    biome = snap.biomes.get(st.biome_key) or snap.default_biome
    state = next((s for s in biome.states if s.key == st.state_key), None) if st.state_key else None
    out: dict[str, Any] = {
        "key": biome.key,
        "name": biome.name,
        "kind": biome.kind,
        "description": biome.description,
        "luck_mult": biome.luck_mult * (state.luck_mult if state else 1.0),
        "theme": {**biome.theme, **(state.theme if state else {})},
        "started_at": st.started_at.isoformat(),
        "ends_at": st.ends_at.isoformat() if st.ends_at else None,
        "forced": st.forced,
        "locked_until": st.locked_until.isoformat() if st.locked_until else None,
        "state": {"key": state.key, "name": state.name, "luck_mult": state.luck_mult, "description": state.description,
                  "ends_at": st.state_ends_at.isoformat() if st.state_ends_at else None} if state else None,
    }
    if reveal:
        out["reveal"] = {
            "next_change_at": st.next_eval_at.isoformat(),
            "next_biome": st.next_biome_key,
            "next_state_at": st.next_state_at.isoformat() if st.next_state_at else None,
            "next_state": st.next_state_key,
        }
    return out
