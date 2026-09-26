"""The second-wave features, exercised through the HTTP API a player uses.

Each test pins down a rule the server must enforce on its own: a claim that
can only happen once, a limit the client cannot talk its way past, a reward
that only arrives when the requirement is genuinely met.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, update

from .conftest import Player, TEST_PASSWORD, login, make_client, set_user

pytestmark = pytest.mark.asyncio


async def _stats(user_id: int) -> Any:
    from app.db import session_scope
    from app.models import UserStats

    async with session_scope() as db:
        return (await db.execute(select(UserStats).where(UserStats.user_id == user_id))).scalar_one_or_none()


async def _set_stats(user_id: int, **values: Any) -> None:
    from app.db import session_scope
    from app.models import UserStats

    async with session_scope() as db:
        row = (await db.execute(select(UserStats).where(UserStats.user_id == user_id))).scalar_one_or_none()
        if row is None:
            db.add(UserStats(user_id=user_id, **values))
        else:
            await db.execute(update(UserStats).where(UserStats.user_id == user_id).values(**values))
        await db.commit()


# ---------------------------------------------------------------------------
# Login bonus
# ---------------------------------------------------------------------------
async def test_daily_bonus_pays_once_per_day(player: Player) -> None:
    before = (await player.get("/api/daily")).json()
    assert before["available"] is True
    assert before["reward"]["stardust"] > 0

    r = await player.post("/api/daily/claim", idem=True)
    assert r.status_code == 200, r.text
    claimed = r.json()
    assert claimed["streak"] == 1
    assert claimed["granted"]["stardust"] > 0

    again = await player.post("/api/daily/claim", idem=True)
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "already_claimed"

    after = (await player.get("/api/daily")).json()
    assert after["available"] is False
    assert after["streak"] == 1


async def test_daily_bonus_streak_continues_from_yesterday(player: Player) -> None:
    from datetime import timedelta

    from app.services.engagement import _tz, local_day
    from app.core.timeutil import utcnow

    yesterday = (utcnow().astimezone(_tz()).date() - timedelta(days=1)).isoformat()
    await _set_stats(player.id, last_bonus_day=yesterday, login_streak=3)
    r = await player.post("/api/daily/claim", idem=True)
    assert r.status_code == 200, r.text
    assert r.json()["streak"] == 4
    assert local_day() != yesterday


async def test_daily_bonus_streak_resets_after_a_gap(player: Player) -> None:
    await _set_stats(player.id, last_bonus_day="2000-01-01", login_streak=6)
    r = await player.post("/api/daily/claim", idem=True)
    assert r.status_code == 200, r.text
    assert r.json()["streak"] == 1


# ---------------------------------------------------------------------------
# Weekly boards
# ---------------------------------------------------------------------------
async def test_rolling_feeds_the_weekly_board(player: Player) -> None:
    await player.roll()
    board = (await player.get("/api/rankings/weekly_rolls")).json()
    assert board["board"] == "weekly_rolls"
    assert any(e["user"]["id"] == player.id for e in board["entries"])
    best = (await player.get("/api/rankings/weekly_best")).json()
    assert best["title"]


# ---------------------------------------------------------------------------
# Season pass
# ---------------------------------------------------------------------------
async def _season_points(user_id: int, points: int) -> None:
    from app.db import session_scope
    from app.services import seasons as seasons_svc

    async with session_scope() as db:
        await seasons_svc.add_stats(db, user_id, rolls=0, points=points, best_odds=0, first_discoveries=0)
        await db.commit()


async def test_season_pass_tier_claims_once(player: Player) -> None:
    await _season_points(player.id, 500)
    state = (await player.get("/api/season/pass")).json()
    assert state["points"] >= 500
    assert state["tiers"][0]["reached"] is True
    assert state["tiers"][0]["claimed"] is False

    r = await player.post("/api/season/pass/claim", json={"tier": 0}, idem=True)
    assert r.status_code == 200, r.text
    again = await player.post("/api/season/pass/claim", json={"tier": 0}, idem=True)
    assert again.status_code in (400, 409), again.text
    assert again.json()["error"]["code"] == "already_claimed"


async def test_season_pass_rejects_an_unreached_tier(player: Player) -> None:
    r = await player.post("/api/season/pass/claim", json={"tier": 19}, idem=True)
    assert r.status_code in (400, 403), r.text
    assert r.json()["error"]["code"] == "not_reached"


# ---------------------------------------------------------------------------
# Friends
# ---------------------------------------------------------------------------
async def test_friend_request_and_accept(player: Player, player2: Player) -> None:
    r = await player.post("/api/friends/request", json={"user_id": player2.id})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"

    incoming = (await player2.get("/api/friends")).json()
    assert [f["id"] for f in incoming["incoming"]] == [player.id]

    r = await player2.post("/api/friends/respond", json={"user_id": player.id, "accept": True})
    assert r.status_code == 200, r.text
    assert [f["id"] for f in (await player.get("/api/friends")).json()["friends"]] == [player2.id]

    r = await player.post("/api/friends/remove", json={"user_id": player2.id})
    assert r.status_code == 200, r.text
    assert (await player.get("/api/friends")).json()["friends"] == []


async def test_cannot_friend_yourself(player: Player) -> None:
    r = await player.post("/api/friends/request", json={"user_id": player.id})
    assert r.status_code in (400, 403), r.text


async def test_reverse_request_auto_accepts(player: Player, player2: Player) -> None:
    await player.post("/api/friends/request", json={"user_id": player2.id})
    r = await player2.post("/api/friends/request", json={"user_id": player.id})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# Guilds
# ---------------------------------------------------------------------------
async def _rich(user_id: int, stardust: int = 100_000) -> None:
    await set_user(user_id, stardust=stardust)


async def test_guild_creation_costs_stardust_and_one_per_player(player: Player, player2: Player) -> None:
    await _rich(player.id)
    r = await player.post("/api/guilds/create", json={"name": "テスト団", "tag": "TST", "description": "テスト"}, idem=True)
    assert r.status_code == 200, r.text
    gid = r.json()["guild"]["id"]

    mine = (await player.get("/api/guilds/mine")).json()
    assert mine["guild"]["id"] == gid
    assert mine["guild"]["members"] == 1

    await _rich(player.id)
    second = await player.post("/api/guilds/create", json={"name": "二つ目", "tag": "TWO"}, idem=True)
    assert second.status_code in (400, 403, 409), second.text

    r = await player2.post("/api/guilds/join", json={"guild_id": gid})
    assert r.status_code == 200, r.text
    assert (await player2.get("/api/guilds/mine")).json()["guild"]["members"] == 2

    r = await player2.post("/api/guilds/leave")
    assert r.status_code == 200, r.text
    assert (await player2.get("/api/guilds/mine")).json()["guild"] is None


async def test_guild_creation_needs_the_fee(player: Player) -> None:
    await set_user(player.id, stardust=10)
    r = await player.post("/api/guilds/create", json={"name": "無一文団", "tag": "NIL"}, idem=True)
    assert r.status_code in (400, 402, 403), r.text
    assert r.json()["error"]["code"] == "insufficient_funds"


async def test_only_the_owner_can_kick(player: Player, player2: Player) -> None:
    await _rich(player.id)
    gid = (await player.post("/api/guilds/create", json={"name": "追放団", "tag": "KIK"}, idem=True)).json()["guild"]["id"]
    await player2.post("/api/guilds/join", json={"guild_id": gid})
    r = await player2.post("/api/guilds/kick", json={"user_id": player.id})
    assert r.status_code in (400, 403), r.text
    r = await player.post("/api/guilds/kick", json={"user_id": player2.id})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Star shards
# ---------------------------------------------------------------------------
async def test_shard_exchange_requires_shards(player: Player) -> None:
    state = (await player.get("/api/shards")).json()
    assert state["shards"] == 0
    key = state["exchanges"][0]["key"]
    r = await player.post("/api/shards/exchange", json={"key": key}, idem=True)
    assert r.status_code in (400, 403), r.text
    assert r.json()["error"]["code"] == "insufficient_shards"


async def test_shard_exchange_grants_an_effect_and_never_goes_negative(player: Player) -> None:
    await _set_stats(player.id, shards=50_000)
    state = (await player.get("/api/shards")).json()
    ex = state["exchanges"][0]
    r = await player.post("/api/shards/exchange", json={"key": ex["key"]}, idem=True)
    assert r.status_code == 200, r.text
    left = r.json()["shards"]
    assert left == 50_000 - ex["cost"] >= 0
    boosts = (await player.get("/api/boosts")).json()
    assert any(e.get("source_type") == "shards" or e.get("type") == "min_rarity" for e in boosts["active"])


async def test_converting_duplicates_says_so_when_there_are_none(player: Player) -> None:
    r = await player.post("/api/shards/convert", idem=True)
    assert r.status_code in (400, 409), r.text
    assert r.json()["error"]["code"] == "nothing_to_convert"


# ---------------------------------------------------------------------------
# Collection sets
# ---------------------------------------------------------------------------
async def test_set_cannot_be_claimed_before_it_is_complete(player: Player) -> None:
    state = (await player.get("/api/sets")).json()
    incomplete = next(s for s in state["sets"] if not s["complete"])
    r = await player.post("/api/sets/claim", json={"set_key": incomplete["key"]}, idem=True)
    assert r.status_code in (400, 403), r.text
    assert r.json()["error"]["code"] == "incomplete"


async def test_completing_a_set_pays_a_permanent_luck_bonus(player: Player, admin: Player) -> None:
    from app.content.collection_sets import SET_MAP

    target = SET_MAP["set_bloom"]
    for key in target["items"]:
        r = await admin.post(f"/api/admin/users/{player.id}/action",
                             json={"action": "give_item", "params": {"item_key": key, "qty": 1}, "reason": "test"})
        assert r.status_code == 200, r.text

    state = (await player.get("/api/sets")).json()
    bloom = next(s for s in state["sets"] if s["key"] == "set_bloom")
    assert bloom["complete"] is True

    r = await player.post("/api/sets/claim", json={"set_key": "set_bloom"}, idem=True)
    assert r.status_code == 200, r.text
    assert r.json()["luck_pct"] == target["luck_pct"]

    again = await player.post("/api/sets/claim", json={"set_key": "set_bloom"}, idem=True)
    assert again.status_code in (400, 409), again.text

    # The bonus is a permanent effect row, so it shows up with no expiry.
    active = (await player.get("/api/boosts")).json()["active"]
    bonus = next(e for e in active if e["source_type"] == "set")
    assert bonus["expires_at"] is None
    assert bonus["value"] == target["luck_pct"]


# ---------------------------------------------------------------------------
# Prestige
# ---------------------------------------------------------------------------
async def test_prestige_is_gated_on_max_level(player: Player) -> None:
    state = (await player.get("/api/prestige")).json()
    assert state["ready"] is False
    r = await player.post("/api/prestige/ascend", idem=True)
    assert r.status_code in (400, 403), r.text
    assert r.json()["error"]["code"] == "level_required"


async def test_prestige_resets_level_and_grants_permanent_luck(player: Player) -> None:
    state = (await player.get("/api/prestige")).json()
    await set_user(player.id, level=state["max_level"], xp=0)
    r = await player.post("/api/prestige/ascend", idem=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prestige"] == 1
    assert body["level"] == 1
    after = (await player.get("/api/prestige")).json()
    assert after["prestige"] == 1
    assert after["current_bonus_pct"] == state["luck_pct_each"]


# ---------------------------------------------------------------------------
# Fortune report
# ---------------------------------------------------------------------------
async def test_fortune_report_reads_for_a_fresh_account(player: Player) -> None:
    await player.roll()
    r = await player.get("/api/fortune")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_rolls"] >= 1
    assert 0 < body["top_percent"] <= 100
    assert len(body["daily"]) == 14
    assert body["tiers"]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
async def test_events_endpoint_is_a_list(player: Player) -> None:
    r = await player.get("/api/events")
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["active"], list)
    assert isinstance(r.json()["upcoming"], list)


# ---------------------------------------------------------------------------
# Cosmetic shop
# ---------------------------------------------------------------------------
async def test_cosmetic_purchase_grants_once(player: Player) -> None:
    await set_user(player.id, stardust=10_000_000, level=20)
    shops = (await player.get("/api/shop")).json()
    atelier = next(s for s in shops["shops"] if s["key"] == "cosmic_atelier")
    item = next(i for i in atelier["items"] if i["product_type"] == "cosmetic" and not i["locked_reason"])

    r = await player.post("/api/shop/purchase", json={"shop_item_key": item["key"], "quantity": 1}, idem=True)
    assert r.status_code == 200, r.text
    assert r.json()["delivered"]["type"] == "cosmetic"

    again = await player.post("/api/shop/purchase", json={"shop_item_key": item["key"], "quantity": 1}, idem=True)
    assert again.status_code in (400, 409), again.text
    assert again.json()["error"]["code"] in ("already_owned", "limit_reached")

    me = (await player.get("/api/me")).json()
    owned = {c["key"] for c in me.get("cosmetics", [])} if isinstance(me.get("cosmetics"), list) else set()
    if owned:
        assert item["product_key"] in owned


async def test_featured_item_is_discounted_and_the_same_for_everyone(player: Player, player2: Player) -> None:
    a = (await player.get("/api/shop")).json()
    b = (await player2.get("/api/shop")).json()
    assert a["featured"] and a["featured"]["key"] == b["featured"]["key"]
    atelier = next(s for s in a["shops"] if s["key"] == "cosmic_atelier")
    featured = next(i for i in atelier["items"] if i["key"] == a["featured"]["key"])
    assert featured["price"] < featured["base_price"]
    assert featured["discount_pct"] == a["featured"]["discount_pct"]


# ---------------------------------------------------------------------------
# Guest play
# ---------------------------------------------------------------------------
async def test_guest_gets_ten_rolls_then_is_asked_to_register(app: Any) -> None:
    from app.services.guest import MAX_ROLLS

    c = make_client(app)
    try:
        state = (await c.get("/api/guest/state")).json()
        assert state["remaining"] == MAX_ROLLS
        for i in range(MAX_ROLLS):
            r = await c.post("/api/guest/roll")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["guest"] is True
            assert body["item"]["name"]
            assert body["remaining"] == MAX_ROLLS - i - 1
        over = await c.post("/api/guest/roll")
        assert over.status_code == 403
        assert over.json()["error"]["code"] == "guest_limit"
    finally:
        await c.aclose()


async def test_guest_rolls_carry_over_on_registration(app: Any) -> None:
    import uuid

    c = make_client(app)
    try:
        for _ in range(3):
            assert (await c.post("/api/guest/roll")).status_code == 200
        name = f"g{uuid.uuid4().hex[:10]}"
        r = await c.post("/api/auth/register", json={"email": f"{name}@example.test", "username": name,
                                                     "password": TEST_PASSWORD})
        assert r.status_code == 200, r.text
        me = (await c.get("/api/me")).json()
        c.headers["X-CSRF-Token"] = me["csrf"]
        inv = (await c.get("/api/inventory")).json()
        assert sum(g["count"] for g in inv["groups"]) >= 3
    finally:
        await c.aclose()


async def test_a_forged_guest_cookie_is_ignored(app: Any) -> None:
    c = make_client(app)
    try:
        c.cookies.set("cosmic_guest", "99|1,2,3|deadbeef", domain="testserver")
        state = (await c.get("/api/guest/state")).json()
        assert state["used"] == 0 and state["remaining"] == 10
    finally:
        await c.aclose()


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------
async def test_share_page_needs_a_valid_signature(app: Any, player: Player) -> None:
    from app.services.share import share_sig

    res = await player.roll()
    roll_id = res["roll"]["id"]
    c = make_client(app)
    try:
        bad = await c.get(f"/share/r/{roll_id}?s={'0' * 24}")
        assert bad.status_code == 404
        good = await c.get(f"/share/r/{roll_id}?s={share_sig(roll_id)}")
        assert good.status_code == 200
        assert "og:image" in good.text
        img = await c.get(f"/share/r/{roll_id}/og.png?s={share_sig(roll_id)}")
        assert img.status_code == 200
        assert img.headers["content-type"] == "image/png"
        assert len(img.content) > 1000
    finally:
        await c.aclose()


# ---------------------------------------------------------------------------
# Velocity limits
# ---------------------------------------------------------------------------
async def _setting(key: str, value: Any) -> None:
    from app.content.registry import get_registry
    from app.db import session_scope
    from app.models import GameSetting

    async with session_scope() as db:
        await db.merge(GameSetting(key=key, value=value))
        await db.commit()
        await get_registry().reload(db)


async def test_market_buy_limit_is_enforced(player: Player, player2: Player) -> None:
    for p in (player, player2):
        await set_user(p.id, level=30, stardust=1_000_000)
    inst = None
    for _ in range(20):
        res = (await player2.roll())["roll"]
        if res.get("instance_ids"):
            inst = res["instance_ids"][0]
            break
    assert inst is not None, "twenty rolls produced nothing to list"
    listed = await player2.post("/api/market/list", json={"instance_id": inst, "price": 100}, idem=True)
    assert listed.status_code == 200, listed.text
    listing_id = listed.json()["id"]

    await _setting("market.daily_buy_limit", 0)
    try:
        r = await player.post("/api/market/buy", json={"listing_id": listing_id}, idem=True)
        assert r.status_code == 429, r.text
        assert r.json()["error"]["code"] == "market_daily_limit"
    finally:
        await _setting("market.daily_buy_limit", 120)

    ok = await player.post("/api/market/buy", json={"listing_id": listing_id}, idem=True)
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# Admin retention dashboard
# ---------------------------------------------------------------------------
async def test_retention_dashboard_reports_the_funnel(admin: Player) -> None:
    r = await admin.get("/api/admin/retention?days=14")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["step"] for s in body["funnel"]][0] == "登録"
    assert isinstance(body["dau"], list)
    assert isinstance(body["cohorts"], list)


async def test_retention_dashboard_is_invisible_to_players(player: Player) -> None:
    r = await player.get("/api/admin/retention")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Scheduler jobs
# ---------------------------------------------------------------------------
async def test_community_goal_completes_and_pays_everyone(player: Player) -> None:
    """A goal that the server's own roll count has already passed must finish
    on the next check, publish a luck window, and never fire twice."""
    from sqlalchemy import select

    from app.content.registry import get_registry
    from app.db import session_scope
    from app.models import GameEvent
    from app.services import engagement as engagement_svc

    await player.roll()
    async with session_scope() as db:
        db.add(GameEvent(key="test_goal", name="テスト世界目標", description="", type="community_goal",
                         params={"target": 1, "reward_mult": 2.0, "reward_hours": 1}, is_active=True))
        await db.commit()
        await get_registry().reload(db)

    async with session_scope() as db:
        await engagement_svc.check_community_goals(db)  # seeds the baseline
        await db.commit()
    await player.roll()
    async with session_scope() as db:
        await engagement_svc.check_community_goals(db)  # crosses the target
        await db.commit()

    async with session_scope() as db:
        goal = (await db.execute(select(GameEvent).where(GameEvent.key == "test_goal"))).scalar_one()
        assert goal.params.get("completed") is True
        rewards = (await db.execute(select(GameEvent).where(GameEvent.type == "luck_multiplier",
                                                            GameEvent.key.like("test_goal_reward%")))).scalars().all()
        assert len(rewards) == 1
        await engagement_svc.check_community_goals(db)
        await db.commit()
        again = (await db.execute(select(GameEvent).where(GameEvent.type == "luck_multiplier",
                                                          GameEvent.key.like("test_goal_reward%")))).scalars().all()
        assert len(again) == 1, "a completed goal must not pay out twice"

    async with session_scope() as db:
        await db.delete((await db.execute(select(GameEvent).where(GameEvent.key == "test_goal"))).scalar_one())
        for r in (await db.execute(select(GameEvent).where(GameEvent.key.like("test_goal_reward%")))).scalars().all():
            await db.delete(r)
        await db.commit()
        await get_registry().reload(db)


async def test_scheduled_backup_runs_once_per_day() -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Backup
    from app.tasks.scheduler import Scheduler

    s = Scheduler()
    s.is_leader = True
    await s._auto_backup()
    async with session_scope() as db:
        rows = (await db.execute(select(Backup).where(Backup.kind == "scheduled"))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "done", [r.status for r in rows]

    await s._auto_backup()
    async with session_scope() as db:
        rows = (await db.execute(select(Backup).where(Backup.kind == "scheduled"))).scalars().all()
    assert len(rows) == 1, "a second run on the same day must be a no-op"
