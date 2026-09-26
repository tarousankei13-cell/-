"""The rarest thing a player owns is their best, however it reached them.

Rolled, procedurally generated, or handed over by an admin: all three must move
the record and show on the leaderboard. Generated items live outside the
in-memory snapshot, which is how the board used to show a rank next to nothing.
"""
from __future__ import annotations

import pytest

from tests.conftest import admin_action, set_user


async def _stats(player):
    r = await player.get(f"/api/profile/{player.id}")
    assert r.status_code == 200, r.text
    return r.json()["stats"]


@pytest.mark.asyncio
async def test_granted_item_becomes_the_best(admin, player):
    before = await _stats(player)
    await admin_action(admin, player.id, "give_item", item_key="omega", qty=1)
    after = await _stats(player)
    assert after["best_odds"] > before["best_odds"]
    assert after["best_item"] and after["best_item"]["key"] == "omega"


@pytest.mark.asyncio
async def test_a_cheaper_grant_does_not_lower_the_record(admin, player):
    await admin_action(admin, player.id, "give_item", item_key="omega", qty=1)
    await admin_action(admin, player.id, "give_item", item_key="cosmic_dust", qty=1)
    after = await _stats(player)
    assert after["best_item"]["key"] == "omega"


@pytest.mark.asyncio
async def test_generated_item_appears_on_the_board(admin, player):
    """Force a procedural slot to resolve, then the board must render the item
    it produced — not None — even though the snapshot does not hold it."""
    await set_user(player.id, next_roll_at=None)
    await admin_action(admin, player.id, "force_next_item", item_key="forged_relic", rolls=1)
    r = await player.post("/api/roll", json={}, idem=True)
    assert r.status_code == 200, r.text
    won = r.json()["roll"]["item"]
    assert won["kind"] == "generated"
    # composed from the parts' Japanese words, through the seeded database
    assert won.get("name_ja"), f"generated item has no Japanese name: {won['name']}"
    assert all(ord(ch) > 0x2E80 for ch in won["name_ja"]), won["name_ja"]

    st = await _stats(player)
    assert st["best_item"] and st["best_item"]["key"] == won["key"]

    board = (await player.get("/api/rankings/best")).json()
    mine = next(e for e in board["entries"] if e["user"]["id"] == player.id)
    assert mine["item"] is not None, "generated best item missing from the board"
    assert mine["item"]["name"] == won["name"]


@pytest.mark.asyncio
async def test_recompute_repairs_records_written_before_the_rule(admin, player):
    """Simulate a grant made under the old code (no record update), then the
    one-shot repair and the admin's 統計を再計算 must both catch it."""
    from app.services.stats import recompute_best_items
    from app.db import session_scope

    await admin_action(admin, player.id, "give_item", item_key="omega", qty=1)
    from app.models import UserStats
    async with session_scope() as db:
        st = await db.get(UserStats, player.id)
        st.best_odds, st.best_item_id = 0.0, None
        await db.commit()
    assert (await _stats(player))["best_item"] is None

    async with session_scope() as db:
        changed = await recompute_best_items(db)
    assert changed >= 1
    assert (await _stats(player))["best_item"]["key"] == "omega"
