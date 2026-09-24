"""RNG engine: probability model, luck curves, conditions, statistics."""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

import pytest

from app.rng.engine import (
    RollContext,
    compile_table,
    effective_luck,
    fortune,
    item_eligible,
    sample_many,
)
from app.rng.procedural import generate
from tests.snapshot_factory import make_snapshot

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)  # 21:00 JST, Wednesday


@pytest.fixture(scope="module")
def snap():
    return make_snapshot()


def ctx(**kw):
    base = dict(biome_key="stellar_drift", luck=1.0, now=NOW)
    base.update(kw)
    return RollContext(**base)


def test_distribution_sums_to_one(snap):
    for luck in (0.5, 1, 10, 1e3, 1e9):
        t = compile_table(snap, ctx(luck=luck))
        assert math.isclose(sum(t.probs), 1.0, rel_tol=1e-9)
        assert all(p >= 0 for p in t.probs)
        assert t.cum[-1] == pytest.approx(1.0)


def test_rarest_item_probability_matches_formula(snap):
    t = compile_table(snap, ctx(luck=1.0))
    top = t.items[0]
    assert top.key == "omega"
    assert t.probs[0] == pytest.approx(1 / 1e9, rel=1e-9)
    # with luck, the mythic exponent (0.8) is applied
    t2 = compile_table(snap, ctx(luck=1000.0))
    assert t2.prob_of(top.id) == pytest.approx(1000 ** 0.8 / 1e9, rel=1e-6)


def test_luck_raises_rare_and_lowers_common(snap):
    lo = compile_table(snap, ctx(luck=1))
    hi = compile_table(snap, ctx(luck=50))
    epic = snap.items_by_key["supernova_remnant"]
    dust = snap.items_by_key["cosmic_dust"]
    mote = snap.items_by_key["stardust_mote"]
    assert hi.prob_of(epic.id) > lo.prob_of(epic.id) * 20
    assert hi.prob_of(mote.id) < lo.prob_of(mote.id)
    assert hi.prob_of(dust.id) < lo.prob_of(dust.id)


def test_luck_curves():
    assert effective_luck(100, None, 1.0) == pytest.approx(100)
    assert effective_luck(100, None, 0.5) == pytest.approx(10)
    assert effective_luck(100, {"mode": "none"}, 1.0) == 1.0
    assert effective_luck(100, {"mode": "linear", "cap": 20}, 0.5) == 20
    assert effective_luck(math.e, {"mode": "log", "k": 2}, 1.0) == pytest.approx(3.0)
    # ultra rare responds less to luck than rare
    assert effective_luck(1000, None, 0.8) < effective_luck(1000, None, 1.0)


def test_biome_exclusive_items(snap):
    petal = snap.items_by_key["bloom_petal"]
    assert compile_table(snap, ctx()).prob_of(petal.id) == 0
    assert compile_table(snap, ctx(biome_key="nebula_bloom")).prob_of(petal.id) > 0
    # biome item boosts apply
    wisp = snap.items_by_key["nebula_wisp"]
    base_c = compile_table(snap, ctx()).chances[compile_table(snap, ctx()).index[wisp.id]]
    bloom = compile_table(snap, ctx(biome_key="nebula_bloom"))
    assert bloom.chances[bloom.index[wisp.id]] == pytest.approx(base_c * 4)


def test_state_exclusive_items(snap):
    crown = snap.items_by_key["event_horizon_crown"]
    assert compile_table(snap, ctx(biome_key="singularity")).prob_of(crown.id) == 0
    assert compile_table(snap, ctx(biome_key="singularity", biome_state="event_horizon")).prob_of(crown.id) > 0


def test_min_luck_condition(snap):
    edge = snap.items_by_key["fortunes_edge"]
    assert not item_eligible(edge, ctx(luck=9.99))
    assert item_eligible(edge, ctx(luck=10))


def test_time_condition(snap):
    comet = snap.items_by_key["midnight_comet"]
    assert not item_eligible(comet, ctx(now=NOW))  # 21:00 JST
    assert item_eligible(comet, ctx(now=datetime(2026, 9, 23, 16, 30, tzinfo=timezone.utc)))  # 01:30 JST
    weekend = snap.items_by_key["weekend_nebula"]
    assert not item_eligible(weekend, ctx(now=NOW))
    assert item_eligible(weekend, ctx(now=datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)))  # Saturday JST


def test_special_and_hidden_special(snap):
    shard = snap.items_by_key["resonance_shard"]
    relic = snap.items_by_key["lucky_seven_relic"]
    assert not item_eligible(shard, ctx())
    assert item_eligible(shard, ctx(special=True))
    assert not item_eligible(relic, ctx(special=True))
    assert item_eligible(relic, ctx(hidden_special="lucky_seven"))


def test_min_tier_guarantee_renormalises(snap):
    t = compile_table(snap, ctx(min_tier=2))
    assert all(it.tier >= 2 for it in t.items)
    assert math.isclose(sum(t.probs), 1.0, rel_tol=1e-9)


def test_flatten_boosts_rare(snap):
    base = compile_table(snap, ctx())
    flat = compile_table(snap, ctx(flatten=0.6))
    leg = snap.items_by_key["galaxy_in_a_bottle"]
    assert flat.prob_of(leg.id) > base.prob_of(leg.id) * 10


def test_item_and_tier_multipliers(snap):
    it = snap.items_by_key["primordial_star"]
    base = compile_table(snap, ctx()).prob_of(it.id)
    assert compile_table(snap, ctx(item_mults={it.id: 10})).prob_of(it.id) == pytest.approx(base * 10, rel=1e-3)
    assert compile_table(snap, ctx(tier_mults={5: 10})).prob_of(it.id) == pytest.approx(base * 10, rel=1e-3)


def test_compiled_equals_cascade_statistically(snap):
    """The compiled categorical distribution must match a literal per-item cascade."""
    rng = random.Random(1234)
    c = ctx(luck=3.0)
    t = compile_table(snap, c)
    n = 200_000
    cascade_counts = [0] * len(t.items)
    for _ in range(n):
        for idx, ch in enumerate(t.chances):
            if rng.random() < ch:
                cascade_counts[idx] += 1
                break
        else:
            cascade_counts[t.items.index(snap.items_by_key["cosmic_dust"])] += 1
    _chi_square_ok(cascade_counts, t.probs, n)


def test_sampling_matches_expected_distribution(snap):
    rng = random.Random(42)
    for c in (ctx(), ctx(luck=25, biome_key="aurora_veil"), ctx(min_tier=3)):
        t = compile_table(snap, c)
        n = 300_000
        counts = sample_many(t, n, rng)
        observed = [counts.get(i, 0) for i in range(len(t.items))]
        _chi_square_ok(observed, t.probs, n)


def _chi_square_ok(observed, probs, n):
    # merge bins with expected < 5 into one tail bin
    obs_b, exp_b = [], []
    o_tail = e_tail = 0.0
    for o, p in zip(observed, probs):
        e = p * n
        if e < 5:
            o_tail += o
            e_tail += e
        else:
            obs_b.append(o)
            exp_b.append(e)
    if e_tail >= 5:
        obs_b.append(o_tail)
        exp_b.append(e_tail)
    chi2 = sum((o - e) ** 2 / e for o, e in zip(obs_b, exp_b))
    dof = max(1, len(obs_b) - 1)
    # Wilson–Hilferty approximation of the 99.9% quantile
    z = 3.09
    crit = dof * (1 - 2 / (9 * dof) + z * math.sqrt(2 / (9 * dof))) ** 3
    assert chi2 < crit, f"chi2={chi2:.1f} dof={dof} crit={crit:.1f}"


def test_fortune_bands():
    assert fortune(0.9)["key"] == "ordinary"
    assert fortune(0.05)["key"] == "lucky"
    assert fortune(5e-5)["key"] == "miraculous"
    assert fortune(1e-9)["key"] == "cosmic"
    assert fortune(0.001)["top_percent"] == pytest.approx(0.1)


def test_procedural_generation(snap):
    rng = random.Random(7)
    slot = snap.items_by_key["forged_relic"]
    seen = set()
    for _ in range(500):
        spec = generate(slot, snap, rng)
        assert spec.odds >= slot.odds
        assert spec.key.startswith("gen:forged_relic:")
        assert spec.name
        seen.add(spec.key)
    assert len(seen) > 50
    rare = generate(slot, snap, rng, min_odds=1e6)
    assert rare.odds >= 1e6
    # deterministic key for the same parts
    a = generate(slot, snap, random.Random(99))
    b = generate(slot, snap, random.Random(99))
    assert a.key == b.key and a.odds == b.odds


def test_every_fortune_band_carries_both_labels():
    """The band is the one number-free summary of how lucky a roll was, so it is
    shown to players; a missing Japanese label would put bare English on the
    reveal screen."""
    from app.rng.engine import FORTUNE_BANDS, fortune

    for _, key, label, label_ja in FORTUNE_BANDS:
        assert label and label_ja, key
        assert label_ja != label, key
    for p in (0.9, 0.3, 0.05, 5e-3, 5e-4, 5e-6, 5e-7, 1e-12):
        f = fortune(p)
        assert f["label_ja"], p
