"""Biome process: event-driven simulation vs theory, locks, forced biomes, states."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.rng.biome_engine import advance, enter_biome, initial_state, sample_next_biome
from tests.snapshot_factory import make_snapshot

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def snap():
    return make_snapshot()


def test_initial_state_is_default(snap):
    st = initial_state(snap, T0, 1, 1.0, random.Random(1))
    assert st.biome_key == "stellar_drift"
    assert st.next_eval_at > T0
    assert st.next_biome_key in snap.biomes


def test_level_gates_advanced_biomes(snap):
    rng = random.Random(3)
    for _ in range(3000):
        _, key = sample_next_biome(snap, 1, 1.0, rng)
        assert snap.biomes[key].min_level <= 1


def test_segments_cover_window_exactly(snap):
    rng = random.Random(5)
    st = initial_state(snap, T0, 30, 1.0, rng)
    end = T0 + timedelta(hours=6)
    res = advance(st, snap, end, level=30, chance_mult=1.0, rng=rng, record_from=T0)
    assert res.segments[0].start == T0
    assert res.segments[-1].end == end
    for a, b in zip(res.segments, res.segments[1:]):
        assert a.end == b.start
    assert sum(s.seconds for s in res.segments) == pytest.approx(6 * 3600)


def test_long_run_frequencies_match_theory(snap):
    """Entry rate of each biome ≈ q_b per default-second (Poisson thinning)."""
    rng = random.Random(11)
    level = 1
    st = initial_state(snap, T0, level, 1.0, rng)
    days = 60
    end = T0 + timedelta(days=days)
    res = advance(st, snap, end, level=level, chance_mult=1.0, rng=rng, record_from=T0)
    default_seconds = sum(s.seconds for s in res.segments if s.biome_key == "stellar_drift")
    entries: dict[str, int] = {}
    for tr in res.transitions:
        if tr.kind == "biome" and tr.from_key == "stellar_drift":
            entries[tr.to_key] = entries.get(tr.to_key, 0) + 1
    for b in snap.natural_biomes:
        if b.min_level > level:
            assert entries.get(b.key, 0) == 0
            continue
        expected = default_seconds / b.odds_per_sec
        if expected < 50:
            continue
        observed = entries.get(b.key, 0)
        assert abs(observed - expected) < 5 * expected ** 0.5, (b.key, observed, expected)


def test_biome_durations(snap):
    rng = random.Random(2)
    st = initial_state(snap, T0, 1, 1.0, rng)
    res = advance(st, snap, T0 + timedelta(days=2), level=1, chance_mult=1.0, rng=rng, record_from=T0)
    runs: dict[str, float] = {}
    for s in res.segments:
        runs.setdefault(s.biome_key, 0)
    # every non-default segment ends no later than its biome duration after it started
    for s in res.segments:
        if s.biome_key != "stellar_drift":
            assert s.seconds <= snap.biomes[s.biome_key].duration_sec + 1e-6


def test_chance_multiplier_increases_frequency(snap):
    def rate(mult):
        rng = random.Random(9)
        st = initial_state(snap, T0, 1, mult, rng)
        res = advance(st, snap, T0 + timedelta(days=3), level=1, chance_mult=mult, rng=rng, record_from=T0)
        entries = sum(1 for t in res.transitions if t.kind == "biome" and t.from_key == "stellar_drift")
        default_seconds = sum(s.seconds for s in res.segments if s.biome_key == "stellar_drift")
        return entries / default_seconds
    assert rate(3.0) == pytest.approx(rate(1.0) * 3.0, rel=0.1)


def test_long_absence_catch_up_is_bounded(snap):
    rng = random.Random(12)
    st = initial_state(snap, T0, 30, 1.0, rng)
    until = T0 + timedelta(days=365)
    res = advance(st, snap, until, level=30, chance_mult=1.0, rng=rng)
    assert len(res.transitions) < 5000
    assert st.evaluated_at == until and st.next_eval_at > until - timedelta(days=1)


def test_forced_biome_returns_to_default(snap):
    rng = random.Random(4)
    st = initial_state(snap, T0, 1, 1.0, rng)
    enter_biome(st, snap, "void_sanctum", T0, rng, duration=300, forced=True, forced_by=1)
    advance(st, snap, T0 + timedelta(seconds=299), level=1, chance_mult=1.0, rng=rng)
    assert st.biome_key == "void_sanctum"
    advance(st, snap, T0 + timedelta(seconds=301), level=1, chance_mult=1.0, rng=rng)
    assert st.biome_key == "stellar_drift" and not st.forced


def test_lock_extends_current_biome(snap):
    rng = random.Random(6)
    st = initial_state(snap, T0, 1, 1.0, rng)
    enter_biome(st, snap, "aurora_veil", T0, rng)
    st.locked_until = T0 + timedelta(hours=1)
    advance(st, snap, T0 + timedelta(minutes=59), level=1, chance_mult=1.0, rng=rng)
    assert st.biome_key == "aurora_veil"
    advance(st, snap, T0 + timedelta(minutes=61), level=1, chance_mult=1.0, rng=rng)
    assert st.biome_key == "stellar_drift"


def test_special_states_occur_within_biome(snap):
    rng = random.Random(8)
    st = initial_state(snap, T0, 30, 1.0, rng)
    enter_biome(st, snap, "singularity", T0, rng)
    res = advance(st, snap, T0 + timedelta(seconds=300), level=30, chance_mult=1.0, rng=rng, record_from=T0)
    states = [s for s in res.segments if s.state_key]
    for s in states:
        assert s.biome_key == "singularity" and s.state_key == "event_horizon"
    # Over many trials a 300s singularity sees the state most of the time (1/60 per sec)
    hits = 0
    for seed in range(200):
        r = random.Random(seed)
        st = initial_state(snap, T0, 30, 1.0, r)
        enter_biome(st, snap, "singularity", T0, r)
        res = advance(st, snap, T0 + timedelta(seconds=300), level=30, chance_mult=1.0, rng=r, record_from=T0)
        hits += any(s.state_key for s in res.segments)
    assert hits > 150


def test_resample_on_signature_change(snap):
    rng = random.Random(10)
    st = initial_state(snap, T0, 1, 1.0, rng)
    old_sig = st.sample_sig
    res = advance(st, snap, T0 + timedelta(seconds=1), level=1, chance_mult=50.0, rng=rng)
    assert st.sample_sig != old_sig
    assert res.changed
