"""Authentication, CSRF, origin checks, admin gating, IDOR and tampering resistance."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.conftest import ADMIN_DISCORD_ID, admin_action, login, make_client, set_user


async def test_unauthenticated_is_rejected(app):
    c = make_client(app)
    r = await c.get("/api/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "login_required"
    r = await c.post("/api/roll", json={})
    assert r.status_code == 401


async def test_session_cookie_is_httponly_and_hashed(app):
    c = make_client(app)
    r = await c.post("/api/auth/dev-login", json={"discord_id": 424242, "username": "cookie"})
    cookie = r.headers["set-cookie"]
    assert "crng_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    token = c.cookies.get("crng_session")
    from sqlalchemy import select

    from app.core.security import hash_token
    from app.db import session_scope
    from app.models import Session

    async with session_scope() as db:
        rows = (await db.execute(select(Session.id))).scalars().all()
    assert token not in rows and hash_token(token) in rows


async def test_csrf_required_for_writes(player):
    good = player.c.headers.pop("X-CSRF-Token")
    r = await player.post("/api/roll", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = await player.post("/api/roll", json={}, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    player.c.headers["X-CSRF-Token"] = good
    r = await player.post("/api/roll", json={})
    assert r.status_code == 200


async def test_foreign_origin_rejected(player):
    r = await player.post("/api/roll", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"


async def test_logout_revokes_session(app):
    p = await login(app)
    r = await p.post("/api/auth/logout")
    assert r.status_code == 200
    r = await p.get("/api/me")
    assert r.status_code == 401


async def test_oauth_callback_rejects_bad_state(app):
    c = make_client(app)
    r = await c.get("/api/auth/callback?code=abc&state=forged")
    assert r.status_code == 302 and "error=oauth_state" in r.headers["location"]


def test_safe_next_blocks_open_redirect():
    from app.api.auth import _safe_next

    assert _safe_next("//evil.com") == "/roll"
    assert _safe_next("https://evil.com") == "/roll"
    assert _safe_next("/\\evil") == "/roll"
    assert _safe_next("/inventory") == "/inventory"


async def test_admin_routes_hidden_from_players(player):
    for path in ("/api/admin/dashboard", "/api/admin/bootstrap", "/api/admin/users", "/api/admin/settings"):
        r = await player.get(path)
        assert r.status_code == 404, path
    r = await player.post(f"/api/admin/users/{player.id}/action", json={"action": "set_base_luck", "params": {"value": 1e9}, "reason": "x"})
    assert r.status_code == 404
    r = await player.post("/api/auth/admin-mode", json={"enabled": True})
    assert r.status_code == 404


async def test_admin_requires_admin_mode(app):
    a = await login(app, ADMIN_DISCORD_ID, "architect")
    r = await a.get("/api/admin/dashboard")
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_mode_required"
    assert (await a.get("/api/admin/bootstrap")).status_code == 200
    await a.post("/api/auth/admin-mode", json={"enabled": True})
    assert (await a.get("/api/admin/dashboard")).status_code == 200
    # admin mode switch is audited
    logs = (await a.get("/api/admin/audit?action=admin_mode")).json()["logs"]
    assert any(l["action"] == "admin_mode_on" for l in logs)


async def test_client_cannot_inject_luck_or_result(player):
    snap_item = "omega"
    r = await player.post("/api/roll", json={"luck": 1e12, "item_key": snap_item, "final_chance": 1, "odds": 1})
    assert r.status_code == 200
    roll = r.json()["roll"]
    assert roll["luck"]["final"] < 10
    assert roll["item"]["key"] != snap_item or roll["forced"]


async def test_idor_cannot_touch_other_players_items(player, player2):
    await player.roll()
    inv = (await player.get("/api/inventory")).json()
    item_id = inv["groups"][0]["item"]["id"]
    inst = (await player.get(f"/api/inventory/item/{item_id}")).json()["instances"][0]["id"]
    r = await player2.post("/api/inventory/sell", json={"instance_ids": [inst]}, idem=True)
    assert r.status_code == 404
    r = await player2.post("/api/inventory/flags", json={"instance_ids": [inst], "locked": True})
    assert r.json()["updated"] == 0
    r = await player2.put("/api/profile/showcase", json={"instance_ids": [inst]})
    assert r.status_code == 404


async def test_negative_and_absurd_values_rejected(player, player2):
    r = await player.post("/api/trades", json={"to_user_id": player2.id, "offer_stardust": -100}, idem=True)
    assert r.status_code == 422
    r = await player.post("/api/shop/purchase", json={"shop_item_key": "s_next_roll_boost", "quantity": 0}, idem=True)
    assert r.status_code == 422
    r = await player.post("/api/market/list", json={"instance_id": 1, "price": -5}, idem=True)
    assert r.status_code == 422


async def test_idempotency_key_required_and_replayed(player):
    await set_user(player.id, level=5, stardust=100000)
    r = await player.post("/api/shop/purchase", json={"shop_item_key": "s_starlight_candle"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "idempotency_key_required"
    key = uuid.uuid4().hex
    r1 = await player.post("/api/shop/purchase", json={"shop_item_key": "s_starlight_candle"}, headers={"Idempotency-Key": key})
    r2 = await player.post("/api/shop/purchase", json={"shop_item_key": "s_starlight_candle"}, headers={"Idempotency-Key": key})
    assert r1.status_code == 200 and r2.status_code == 200 and r2.json().get("replayed")
    boosts = (await player.get("/api/boosts")).json()["inventory"]
    assert next(b for b in boosts if b["key"] == "starlight_candle")["quantity"] == 1
    me = (await player.get("/api/me")).json()
    assert me["user"]["stardust"] == 100000 - 500


async def test_concurrent_duplicate_purchase_charged_once(player):
    await set_user(player.id, level=5, stardust=10000)
    key = uuid.uuid4().hex
    rs = await asyncio.gather(*[player.post("/api/shop/purchase", json={"shop_item_key": "s_starlight_candle"},
                                            headers={"Idempotency-Key": key}) for _ in range(4)])
    assert all(r.status_code in (200, 409) for r in rs)
    me = (await player.get("/api/me")).json()
    assert me["user"]["stardust"] == 10000 - 500


async def test_sql_injection_is_inert(player):
    r = await player.get("/api/inventory", params={"q": "'; DROP TABLE users; --", "sort": "name:asc;DROP TABLE users"})
    assert r.status_code == 200
    r = await player.get("/api/users/search", params={"q": "%' OR 1=1 --"})
    assert r.status_code == 200
    assert (await player.get("/api/me")).status_code == 200


async def test_banned_user_session_is_invalidated(admin, app):
    p = await login(app)
    await admin_action(admin, p.id, "ban", confirm=True)
    r = await p.get("/api/me")
    assert r.status_code == 401
    c = make_client(app)
    r = await c.post("/api/auth/dev-login", json={"discord_id": p.me["user"]["discord_id"], "username": "again"})
    assert r.status_code == 302 and "banned" in r.headers["location"]


async def test_frozen_user_can_read_but_not_act(admin, app):
    p = await login(app)
    await admin_action(admin, p.id, "freeze")
    assert (await p.get("/api/inventory")).status_code == 200
    r = await p.post("/api/roll", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_frozen"
    await admin_action(admin, p.id, "unrestrict")
    assert (await p.post("/api/roll", json={})).status_code == 200


async def test_rate_limit(player):
    from app.core.ratelimit import LIMITS, Limit, limiter

    limiter.enabled = True
    old = LIMITS["api"]
    LIMITS["api"] = Limit(rate=0.1, burst=3)
    try:
        codes = [(await player.get("/api/inventory")).status_code for _ in range(6)]
        assert 429 in codes
    finally:
        LIMITS["api"] = old
        limiter.enabled = False
        limiter.reset()


async def test_error_responses_do_not_leak_internals(player):
    r = await player.get("/api/items/999999999")
    body = r.json()
    assert r.status_code == 404 and "Traceback" not in r.text and "sqlalchemy" not in r.text.lower()
    assert set(body["error"]) >= {"code", "message"}


@pytest.mark.parametrize("path", ["/api/admin/backups", "/api/admin/rolls", "/api/admin/content/items"])
async def test_admin_endpoints_look_nonexistent(path, player):
    r = await player.get(path)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
