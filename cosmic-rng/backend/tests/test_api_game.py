"""Gameplay integration tests: rolls, filters, boosts, offline, quests, crafting, economy, artifacts."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.timeutil import utcnow
from app.db import session_scope
from app.models import Collection, ItemInstance, Roll, UserStats
from tests.conftest import admin_action, set_user


async def _owned(user_id: int, item_key: str | None = None, source: str | None = None) -> int:
    from app.content.registry import get_registry

    async with session_scope() as db:
        q = select(func.count()).select_from(ItemInstance).where(ItemInstance.owner_id == user_id)
        if source:
            q = q.where(ItemInstance.source == source)
        if item_key:
            q = q.where(ItemInstance.item_id == get_registry().snap.items_by_key[item_key].id)
        return int((await db.execute(q)).scalar_one())


async def test_roll_persists_everything(player):
    data = await player.roll()
    roll = data["roll"]
    assert roll["item"]["name"] and roll["odds"] >= 1 and 0 < roll["final_chance"] <= 1
    assert roll["luck"]["final"] > 0 and roll["fortune"]["label"]
    assert data["state"]["roll_counter"] == 1
    async with session_scope() as db:
        r = (await db.execute(select(Roll).where(Roll.user_id == player.id))).scalar_one()
        assert r.rng_version >= 1 and r.content_version >= 1
        assert (await db.get(Collection, (player.id, r.item_id))) is not None
    assert await _owned(player.id, source="roll") == 1


async def test_cooldown_enforced_server_side(player):
    from app.content.registry import get_registry
    from app.models import GameSetting

    async with session_scope() as db:
        await db.merge(GameSetting(key="roll.base_seconds", value=30.0))
        await db.commit()
        await get_registry().reload(db)
    try:
        await player.post("/api/roll", json={})
        r = await player.post("/api/roll", json={})
        assert r.status_code == 429 and r.json()["error"]["code"] == "cooldown"
        assert r.json()["error"]["data"]["retry_after_ms"] > 20000
    finally:
        async with session_scope() as db:
            await db.merge(GameSetting(key="roll.base_seconds", value=0.05))
            await db.commit()
            await get_registry().reload(db)


async def test_special_roll_every_tenth(player):
    specials = []
    for _ in range(10):
        specials.append((await player.roll())["roll"]["special"])
    assert specials == [False] * 9 + [True]


async def achievement_stardust_since(user_id: int, since) -> int:
    """Stardust granted by achievements unlocked after ``since`` (rewards are automatic)."""
    from app.content.registry import get_registry
    from app.models import UserAchievement

    snap = get_registry().snap
    async with session_scope() as db:
        rows = (await db.execute(select(UserAchievement.achievement_id).where(UserAchievement.user_id == user_id,
                                                                             UserAchievement.achieved_at >= since))).all()
    return sum(int((snap.achievements_by_id[r[0]]["rewards"] or {}).get("stardust", 0)) for r in rows)


async def _db_now():
    """Server clock as the database sees it.

    PostgreSQL may run on another host, so its clock is the authority. SQLite is
    in-process, where the app clock is the same clock.
    """
    from sqlalchemy import text

    from app.db import is_sqlite

    if is_sqlite():
        return utcnow()
    async with session_scope() as db:
        return (await db.execute(text("SELECT now()"))).scalar_one()


def data_rewards(roll: dict) -> int:
    return sum(a["rewards"].get("stardust", 0) for a in roll["progress"]["achievements"])


async def test_auto_delete_filter(player):
    await set_user(player.id, level=5)
    r = await player.put("/api/settings", json={"settings": {"auto_delete": {"enabled": True, "max_odds": 1e12, "mode": "sell", "protect_new": False}}})
    assert r.json()["settings"]["auto_delete"]["enabled"]
    before = (await player.get("/api/me")).json()["user"]["stardust"]
    res = (await player.roll())["roll"]
    assert res["auto_deleted"] and res["auto_sold"] > 0
    assert await _owned(player.id, source="roll") == 0
    async with session_scope() as db:
        assert (await db.execute(select(func.count()).select_from(Collection).where(Collection.user_id == player.id))).scalar_one() == 0
        stats = await db.get(UserStats, player.id)
        assert stats.total_rolls == 1 and stats.items_auto_deleted == 1
    after = (await player.get("/api/me")).json()["user"]["stardust"]
    assert after == before + res["auto_sold"] + data_rewards(res)


async def test_auto_delete_respects_favorites_and_new_items(admin, player):
    await set_user(player.id, level=5)
    await player.put("/api/settings", json={"settings": {"auto_delete": {"enabled": True, "max_odds": 1e12, "mode": "delete", "protect_new": True}}})
    await admin_action(admin, player.id, "force_next_item", item_key="plasma_orb", rolls=3)
    first = (await player.roll())["roll"]
    assert first["item"]["key"] == "plasma_orb" and not first["auto_deleted"]  # never seen before → protected
    await player.post("/api/inventory/item-favorite", json={"item_id": first["item"]["id"], "favorite": True})
    second = (await player.roll())["roll"]
    assert not second["auto_deleted"]  # favourite item types always survive
    await player.post("/api/inventory/item-favorite", json={"item_id": first["item"]["id"], "favorite": False})
    third = (await player.roll())["roll"]
    assert third["auto_deleted"]
    assert await _owned(player.id, "plasma_orb") == 2


async def test_boost_purchase_and_consumption(player):
    await set_user(player.id, level=5, stardust=50000)
    r = await player.post("/api/shop/purchase", json={"shop_item_key": "s_next_roll_boost"}, idem=True)
    assert r.status_code == 200, r.text
    r = await player.post("/api/boosts/use", json={"boost_key": "next_roll_boost"}, idem=True)
    assert r.status_code == 200, r.text
    roll = (await player.roll())["roll"]
    assert roll["luck"]["temporary"] == 11.0 and "NEXT ROLL BOOST" in roll["effects_applied"]
    roll2 = (await player.roll())["roll"]
    assert roll2["luck"]["temporary"] == 1.0
    r = await player.post("/api/boosts/use", json={"boost_key": "next_roll_boost"}, idem=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_enough_boosts"


async def test_feature_gating(player):
    r = await player.post("/api/market/list", json={"instance_id": 1, "price": 10}, idem=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "feature_locked"
    r = await player.post("/api/roll/auto", json={"enabled": True})
    assert r.status_code == 403


async def test_offline_auto_roll_is_server_computed_and_idempotent(player):
    await set_user(player.id, level=5)
    assert (await player.post("/api/roll/auto", json={"enabled": True})).status_code == 200
    past = utcnow() - timedelta(hours=1)
    await set_user(player.id, last_roll_at=past, auto_roll_since=past - timedelta(minutes=1), offline_processed_until=past)
    r = await player.post("/api/offline/claim")
    assert r.status_code == 200, r.text
    summary = r.json()["offline"]
    assert summary and summary["rolls"] > 1000  # 3600s * 0.6 / 0.05s, capped by offline.max_rolls
    assert summary["rolls"] <= 40000
    stats_rolls = r.json()["state"]["roll_counter"]
    assert stats_rolls == summary["rolls"]
    # second claim immediately after: nothing more to process
    r2 = await player.post("/api/offline/claim")
    assert r2.json()["offline"] is None
    assert (await player.get("/api/state")).json()["roll_counter"] == stats_rolls


async def test_offline_window_is_capped(player):
    await set_user(player.id, level=5)
    await player.post("/api/roll/auto", json={"enabled": True})
    long_ago = utcnow() - timedelta(days=30)
    await set_user(player.id, last_roll_at=long_ago, auto_roll_since=long_ago, offline_processed_until=long_ago)
    summary = (await player.post("/api/offline/claim")).json()["offline"]
    assert summary["seconds"] <= 8 * 3600 + 5


async def test_achievement_and_quests(player):
    res = await player.roll()
    keys = [a["key"] for a in res["roll"]["progress"]["achievements"]]
    assert "rolls_1" in keys
    q = (await player.get("/api/quests")).json()
    assert q["chains"] and q["chains"][0]["key"] == "c_star_1"
    assert q["hidden"] and all(h["name"] == "???" for h in q["hidden"] if h["status"] == "active")
    for _ in range(25):
        await player.roll()
    q = (await player.get("/api/quests")).json()
    step = next(c for c in q["chains"] if c["key"] == "c_star_1")
    assert step["status"] == "completed"
    r = await player.post("/api/quests/claim", json={"user_quest_id": step["id"]}, idem=True)
    assert r.status_code == 200 and r.json()["granted"]["stardust"] == 300
    q = (await player.get("/api/quests")).json()
    assert any(c["key"] == "c_star_2" for c in q["chains"])


async def test_crafting_and_secret_recipe(admin, player):
    await set_user(player.id, level=10, stardust=100000)
    await admin_action(admin, player.id, "give_item", item_key="stardust_mote", qty=10)
    await admin_action(admin, player.id, "give_item", item_key="space_pebble", qty=5)
    r = await player.post("/api/crafting/craft", json={"recipe_key": "r_stargazer_glove"}, idem=True)
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["type"] == "equipment"
    assert await _owned(player.id, "stardust_mote") == 0
    # secret recipe: wrong combination consumes nothing
    await admin_action(admin, player.id, "give_item", item_key="nebula_rose", qty=1)
    await admin_action(admin, player.id, "give_item", item_key="cosmic_lotus", qty=1)
    await admin_action(admin, player.id, "give_item", item_key="moonstone_heart", qty=1)
    r = await player.post("/api/crafting/experiment", json={"ingredients": [{"item_key": "nebula_rose", "qty": 1},
                                                                           {"item_key": "cosmic_lotus", "qty": 1}]}, idem=True)
    assert r.json()["result"] == "nothing"
    assert await _owned(player.id, "nebula_rose") == 1
    r = await player.post("/api/crafting/experiment", json={"ingredients": [
        {"item_key": "nebula_rose", "qty": 1}, {"item_key": "cosmic_lotus", "qty": 1}, {"item_key": "moonstone_heart", "qty": 1}]}, idem=True)
    assert r.json()["result"] == "discovered"
    assert await _owned(player.id, "everbloom") == 1
    recipes = (await player.get("/api/crafting")).json()["recipes"]
    assert any(x["key"] == "r_everbloom" for x in recipes)


async def test_equipment_affects_luck(admin, player):
    await set_user(player.id, level=10)
    await admin_action(admin, player.id, "give_item", item_key="stardust_mote", qty=10)
    await admin_action(admin, player.id, "give_item", item_key="space_pebble", qty=5)
    await set_user(player.id, stardust=1000)
    r = await player.post("/api/crafting/craft", json={"recipe_key": "r_stargazer_glove"}, idem=True)
    inst = r.json()["results"][0]["instances"][0]
    r = await player.post("/api/equipment/equip", json={"instance_id": inst["id"]})
    assert r.status_code == 200
    roll = (await player.roll())["roll"]
    assert roll["luck"]["equipment"] == 1 + inst["luck_bonus"]


async def test_market_flow(admin, player, player2):
    await set_user(player.id, level=10)
    await set_user(player2.id, level=10, stardust=100000)
    await admin_action(admin, player.id, "give_item", item_key="quasar_shard", qty=1)
    inv = (await player.get("/api/inventory")).json()
    item_id = next(g["item"]["id"] for g in inv["groups"] if g["item"]["key"] == "quasar_shard")
    inst = (await player.get(f"/api/inventory/item/{item_id}")).json()["instances"][0]["id"]
    r = await player.post("/api/market/list", json={"instance_id": inst, "price": 1000}, idem=True)
    assert r.status_code == 200, r.text
    listing = r.json()["id"]
    # listed items are escrowed
    r = await player.post("/api/inventory/sell", json={"instance_ids": [inst]}, idem=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "instance_busy"
    r = await player.post("/api/market/buy", json={"listing_id": listing}, idem=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == "own_listing"
    seller_before = (await player.get("/api/me")).json()["user"]["stardust"]
    t0 = await _db_now()
    r = await player2.post("/api/market/buy", json={"listing_id": listing}, idem=True)
    assert r.status_code == 200, r.text
    assert await _owned(player2.id, "quasar_shard") == 1
    seller_after = (await player.get("/api/me")).json()["user"]["stardust"]
    assert seller_after == seller_before + 1000 - 50 + await achievement_stardust_since(player.id, t0)  # 5% fee
    r = await player2.post("/api/market/buy", json={"listing_id": listing}, idem=True)
    assert r.status_code == 409


async def test_market_double_buy_race(admin, app, player, player2):
    from tests.conftest import login

    p3 = await login(app)
    for p in (player2, p3):
        await set_user(p.id, level=10, stardust=100000)
    await set_user(player.id, level=10)
    await admin_action(admin, player.id, "give_item", item_key="pulsar_core", qty=1)
    inv = (await player.get("/api/inventory")).json()
    item_id = next(g["item"]["id"] for g in inv["groups"] if g["item"]["key"] == "pulsar_core")
    inst = (await player.get(f"/api/inventory/item/{item_id}")).json()["instances"][0]["id"]
    listing = (await player.post("/api/market/list", json={"instance_id": inst, "price": 500}, idem=True)).json()["id"]
    import asyncio

    t0 = await _db_now()
    r1, r2 = await asyncio.gather(player2.post("/api/market/buy", json={"listing_id": listing}, idem=True),
                                  p3.post("/api/market/buy", json={"listing_id": listing}, idem=True))
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    rewards = await achievement_stardust_since(player2.id, t0) + await achievement_stardust_since(p3.id, t0)
    total_spent = (100000 - (await player2.get("/api/me")).json()["user"]["stardust"]) + (100000 - (await p3.get("/api/me")).json()["user"]["stardust"])
    assert total_spent == 500 - rewards
    assert await _owned(player2.id, "pulsar_core") + await _owned(p3.id, "pulsar_core") == 1


async def test_trade_requires_matching_revision(admin, player, player2):
    await set_user(player.id, level=10)
    await set_user(player2.id, level=10, stardust=5000)
    await admin_action(admin, player.id, "give_item", item_key="cosmic_lotus", qty=1)
    inv = (await player.get("/api/inventory")).json()
    item_id = next(g["item"]["id"] for g in inv["groups"] if g["item"]["key"] == "cosmic_lotus")
    inst = (await player.get(f"/api/inventory/item/{item_id}")).json()["instances"][0]["id"]
    r = await player.post("/api/trades", json={"to_user_id": player2.id, "offer_items": [inst], "request_stardust": 2000}, idem=True)
    assert r.status_code == 200, r.text
    trade = r.json()
    r = await player.post(f"/api/trades/{trade['id']}/respond", json={"action": "accept", "revision": trade["revision"]}, idem=True)
    assert r.status_code == 403  # sender cannot accept
    r = await player2.post(f"/api/trades/{trade['id']}/respond", json={"action": "accept", "revision": 999}, idem=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "revision_mismatch"
    t0 = await _db_now()
    r = await player2.post(f"/api/trades/{trade['id']}/respond", json={"action": "accept", "revision": trade["revision"]}, idem=True)
    assert r.status_code == 200, r.text
    assert await _owned(player2.id, "cosmic_lotus") == 1
    assert await _owned(player.id, "cosmic_lotus") == 0
    assert (await player2.get("/api/me")).json()["user"]["stardust"] == 3000 + await achievement_stardust_since(player2.id, t0)


async def test_gift_cooldown(admin, player, player2):
    await set_user(player.id, level=10, stardust=1000)
    r = await player.post("/api/gifts", json={"to_user_id": player2.id, "stardust": 100}, idem=True)
    assert r.status_code == 200, r.text
    r = await player.post("/api/gifts", json={"to_user_id": player2.id, "stardust": 100}, idem=True)
    assert r.status_code == 429 and r.json()["error"]["code"] == "gift_cooldown"
    assert (await player2.get("/api/me")).json()["user"]["stardust"] >= 350


async def test_admin_artifact_restrictions(admin, player):
    await set_user(player.id, level=20)
    g = (await admin.post("/api/admin/artifacts/grant", json={"target_user_id": player.id, "artifact_key": "infinite_luck",
                                                              "reason": "test"})).json()
    inst = g["instance_id"]
    r = await player.post("/api/inventory/sell", json={"instance_ids": [inst]}, idem=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_artifact_restricted"
    r = await player.post("/api/market/list", json={"instance_id": inst, "price": 100}, idem=True)
    assert r.status_code == 403
    r = await player.post("/api/artifacts/use", json={"instance_id": inst}, idem=True)
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_only"
    # equip for the aura (passive) is allowed
    assert (await player.post("/api/artifacts/equip", json={"instance_id": inst})).status_code == 200
    r = await admin.post("/api/admin/artifacts/recall", json={"instance_id": inst, "reason": "test"})
    assert r.status_code == 200
    assert await _owned(player.id, "aa_infinite_luck") == 0
    logs = (await admin.get(f"/api/admin/audit?target_user_id={player.id}")).json()["logs"]
    assert {"artifact_grant", "artifact_recall"} <= {l["action"] for l in logs}


async def test_player_usable_artifact_with_grant(admin, player):
    g = (await admin.post("/api/admin/artifacts/grant", json={"target_user_id": player.id, "artifact_key": "chrono_loop",
                                                              "can_use": True, "uses": 1, "reason": "gift"})).json()
    r = await player.post("/api/artifacts/use", json={"instance_id": g["instance_id"]}, idem=True)
    assert r.status_code == 200, r.text
    assert r.json()["cinematic"]["artifact"] == "chrono_loop"
    roll = (await player.roll())["roll"]
    assert roll["best_of"] == 2
    r = await player.post("/api/artifacts/use", json={"instance_id": g["instance_id"]}, idem=True)
    assert r.status_code in (403, 429)


async def test_admin_force_and_first_discovery(admin, player):
    await admin_action(admin, player.id, "force_next_item", item_key="genesis_codex")
    roll = (await player.roll())["roll"]
    assert roll["item"]["key"] == "genesis_codex" and roll["forced"]
    assert roll["first_discovery"] is not None
    feed = (await player.get("/api/feed")).json()["events"]
    assert any(e["type"] == "first_discovery" and e["payload"]["item"]["key"] == "genesis_codex" for e in feed)


async def test_admin_content_change_applies_to_rolls(admin, player):
    await admin_action(admin, player.id, "set_base_luck", value=1e10)
    table = (await admin.get(f"/api/admin/users/{player.id}/table")).json()
    p_default = next(i["p"] for i in table["items"] if i["key"] == "omega")
    assert p_default < 0.2  # mythic luck exponent 0.8 dampens luck
    r = await admin.put("/api/admin/content/items/omega", json={"data": {"luck_curve": {"mode": "linear"}},
                                                               "reason": "test", "temporary_hours": 0.05})
    assert r.status_code == 200, r.text
    roll = (await player.roll())["roll"]
    assert roll["item"]["key"] == "omega"  # linear curve: c = min(1, 1e10 / 1e9) = 1 and omega is checked first
    overrides = (await admin.get("/api/admin/overrides")).json()["overrides"]
    ov = next(o for o in overrides if o["entity_key"] == "omega" and o["active"])
    assert (await admin.delete(f"/api/admin/overrides/{ov['id']}?reason=revert")).status_code == 200
    table = (await admin.get(f"/api/admin/users/{player.id}/table")).json()
    assert next(i["p"] for i in table["items"] if i["key"] == "omega") == pytest.approx(p_default)


async def test_admin_simulation_matches(admin):
    r = await admin.post("/api/admin/rng/simulate", json={"luck": 3, "biome_key": "aurora_veil", "n": 300000, "seed": 7})
    assert r.status_code == 200
    assert r.json()["verdict"] == "OK"


async def test_admin_settings_validation(admin):
    r = await admin.put("/api/admin/settings", json={"values": {"roll.special_interval": 1}, "reason": "x"})
    assert r.status_code == 400
    r = await admin.put("/api/admin/settings", json={"values": {"unknown.key": 1}, "reason": "x"})
    assert r.status_code == 400
    r = await admin.put("/api/admin/settings", json={"values": {"roll.special_interval": 12}, "reason": "tune"})
    assert r.status_code == 200
    await admin.put("/api/admin/settings", json={"values": {"roll.special_interval": 10}, "reason": "revert"})


async def test_admin_dashboard(admin, player):
    await player.roll()
    d = (await admin.get("/api/admin/dashboard")).json()
    assert d["total_rolls"] >= 1 and "rolls_per_minute" in d and "biomes" in d
