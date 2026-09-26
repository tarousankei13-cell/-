"""Time away pays out whether or not the player ever found the Auto Roll switch."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.timeutil import utcnow
from tests.conftest import set_user


@pytest.mark.asyncio
async def test_offline_rolls_accrue_without_the_toggle(player):
    # one real roll so there is a "last roll" to measure from, then two hours away
    r = await player.post("/api/roll", json={}, idem=True)
    assert r.status_code == 200, r.text
    away = utcnow() - timedelta(hours=2)
    await set_user(player.id, auto_roll_enabled=False, last_roll_at=away, offline_processed_until=None, next_roll_at=None)

    r = await player.post("/api/offline/claim", json={})
    assert r.status_code == 200, r.text
    summary = r.json()["offline"]
    assert summary is not None, "no offline window although the player was away for two hours"
    assert summary["rolls"] > 0


@pytest.mark.asyncio
async def test_a_short_gap_is_not_time_away(player):
    r = await player.post("/api/roll", json={}, idem=True)
    assert r.status_code == 200, r.text
    await set_user(player.id, auto_roll_enabled=False, last_roll_at=utcnow() - timedelta(seconds=5), next_roll_at=None)
    r = await player.post("/api/offline/claim", json={})
    assert r.status_code == 200, r.text
    assert r.json()["offline"] is None


@pytest.mark.asyncio
async def test_toggling_auto_roll_does_not_forfeit_time_already_earned(player):
    r = await player.post("/api/roll", json={}, idem=True)
    assert r.status_code == 200, r.text
    # Auto Roll itself unlocks at level 3; accrual never needed it, this test just flips it
    await set_user(player.id, level=3, last_roll_at=utcnow() - timedelta(hours=1), offline_processed_until=None, next_roll_at=None)
    # flipping the switch used to move the marker to "now" and drop the hour
    r = await player.post("/api/roll/auto", json={"enabled": True})
    assert r.status_code == 200, r.text
    r = await player.post("/api/offline/claim", json={})
    assert r.json()["offline"] is not None and r.json()["offline"]["rolls"] > 0
