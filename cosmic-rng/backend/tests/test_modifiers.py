"""Effects stacking, consumption, equipment passives, cooldown and special rolls."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.rng.modifiers import (
    EffectData,
    EquipData,
    combine_effects,
    equipment_luck,
    passive_luck,
    roll_cooldown,
    special_roll_info,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
RNG = random.Random(1)


def eff(i, t="luck", v=100.0, mode="add", rolls=1, src="b", **kw):
    return EffectData(id=i, effect_type=t, value=v, stack_mode=mode, remaining_rolls=rolls, source_key=src, name=f"e{i}", **kw)


def combine(effects, biome="stellar_drift", special=False):
    return combine_effects(effects, now=NOW, biome_key=biome, special=special, rng=RNG)


def test_add_stacking():
    m = combine([eff(1, v=1000), eff(2, v=1000)])
    assert m.temporary_luck == pytest.approx(21.0)
    assert m.consumed == {1: 1, 2: 1}


def test_multiply_stacking():
    m = combine([eff(1, v=1000, mode="multiply", src="s"), eff(2, v=1000, mode="multiply", src="s")])
    assert m.temporary_luck == pytest.approx(121.0)


def test_mixed_add_and_multiply():
    m = combine([eff(1, v=100), eff(2, v=100, src="c"), eff(3, v=10000, mode="multiply", src="surge")])
    assert m.temporary_luck == pytest.approx(3.0 * 101.0)


def test_queue_only_oldest_applies():
    m = combine([eff(5, v=200, mode="queue", rolls=5, src="stock"), eff(3, v=200, mode="queue", rolls=5, src="stock")])
    assert m.temporary_luck == pytest.approx(3.0)
    assert m.consumed == {3: 1}


def test_highest_only_strongest_applies():
    m = combine([eff(1, v=50, mode="highest", src="h"), eff(2, v=300, mode="highest", src="h")])
    assert m.temporary_luck == pytest.approx(4.0)
    assert m.consumed == {2: 1}


def test_time_based_not_consumed_and_expiry():
    e = eff(1, v=100, rolls=None, expires_at=NOW + timedelta(minutes=5))
    m = combine([e])
    assert m.temporary_luck == pytest.approx(2.0)
    assert m.consumed == {}
    expired = eff(2, v=100, rolls=None, expires_at=NOW - timedelta(seconds=1))
    assert combine([expired]).temporary_luck == 1.0


def test_biome_restricted_effect():
    e = eff(1, v=800, biome_keys=["void_rift"])
    m = combine([e])
    assert m.temporary_luck == 1.0 and m.consumed == {}
    m2 = combine([e], biome="void_rift")
    assert m2.temporary_luck == pytest.approx(9.0) and m2.consumed == {1: 1}


def test_special_boost_consumed_only_on_special():
    e = eff(1, t="special_boost", v=2.0, mode="queue")
    assert combine([e]).consumed == {}
    m = combine([e], special=True)
    assert m.special_extra == 2.0 and m.consumed == {1: 1}


def test_min_rarity_highest_consumed_only():
    a = eff(1, t="min_rarity", mode="highest", params={"tier": "rare"}, src="r")
    b = eff(2, t="min_rarity", mode="highest", params={"tier": "epic"}, src="e")
    m = combine([a, b])
    assert m.min_tier == 3
    assert m.consumed == {2: 1}


def test_artifact_effects():
    m = combine([
        eff(1, t="luck_mult", v=1e6, rolls=None, expires_at=NOW + timedelta(seconds=60)),
        eff(2, t="compounding_luck", v=0.25, rolls=30, params={"count": 2}),
        eff(3, t="table_flatten", v=0.6, rolls=15),
        eff(4, t="best_of", v=2, rolls=10),
        eff(5, t="force_item", rolls=1, params={"item_key": "omega"}),
    ])
    assert m.artifact_luck == pytest.approx(1e6 * 1.25 ** 2)
    assert m.param_updates[2]["count"] == 3
    assert m.flatten == 0.6 and m.best_of == 2 and m.force_item_key == "omega"
    assert set(m.consumed) == {2, 3, 4, 5}


def test_random_luck_bounds():
    for _ in range(50):
        m = combine([eff(1, t="random_luck", params={"min": 1, "max": 1e6})])
        assert 1 <= m.artifact_luck <= 1e6


def test_equipment_and_passives():
    eq = [EquipData("a", "gauntlet", 1.2, 0, [{"type": "nth_roll_luck", "every": 5, "mult": 1.3}]),
          EquipData("b", "relic", 0.8, 0, [{"type": "biome_luck", "biome": "nebula_bloom", "mult": 1.5}])]
    assert equipment_luck(eq) == pytest.approx(3.0)
    assert equipment_luck(eq, "multiplicative") == pytest.approx(2.2 * 1.8)
    assert passive_luck(eq, roll_number=10, biome_key="nebula_bloom", biome_state=None, now=NOW) == pytest.approx(1.95)
    assert passive_luck(eq, roll_number=11, biome_key="stellar_drift", biome_state=None, now=NOW) == 1.0


def test_cooldown():
    eq = [EquipData("c", "core", 0, 1.0, [])]
    assert roll_cooldown(base_seconds=1.0, min_seconds=0.08, equips=eq, effects=[], now=NOW) == pytest.approx(0.5)
    fast = [eff(1, t="cooldown_mult", v=0.05, rolls=None, expires_at=NOW + timedelta(seconds=10))]
    assert roll_cooldown(base_seconds=1.0, min_seconds=0.08, equips=[], effects=fast, now=NOW) == pytest.approx(0.08)


def test_special_roll_info():
    hs = [{"key": "lucky_seven", "every": 777, "luck_mult": 2.0}]
    assert special_roll_info(10, 10, 0, hs) == (True, None)
    assert special_roll_info(9, 10, -1, hs)[0] is True
    assert special_roll_info(11, 10, 0, hs) == (False, None)
    assert special_roll_info(777, 10, 0, hs)[1]["key"] == "lucky_seven"
