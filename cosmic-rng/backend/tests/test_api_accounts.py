"""Local account registration, login, lockout and password change."""
from __future__ import annotations

import pytest

from tests.conftest import (ADMIN_PASSWORD, ADMIN_USERNAME, TEST_PASSWORD, admin_action, login,
                            make_client, set_user)

pytestmark = pytest.mark.asyncio


async def _register(c, name, password=TEST_PASSWORD, email=None):
    return await c.post("/api/auth/register",
                        json={"email": email or f"{name}@example.test", "username": name, "password": password})


async def test_register_creates_a_playable_account(app):
    c = make_client(app)
    r = await _register(c, "newcomer")
    assert r.status_code == 200, r.text
    me = (await c.get("/api/me")).json()
    assert me["user"]["username"] == "newcomer"
    assert me["is_admin"] is False
    c.headers["X-CSRF-Token"] = me["csrf"]
    assert (await c.post("/api/roll", json={})).status_code == 200
    await c.aclose()


async def test_password_is_never_stored_or_returned(app):
    c = make_client(app)
    await _register(c, "secretive")
    body = (await c.get("/api/me")).text
    assert TEST_PASSWORD not in body and "password" not in body.lower()

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import User

    async with session_scope() as db:
        u = (await db.execute(select(User).where(User.username == "secretive"))).scalar_one()
        assert u.password_hash and TEST_PASSWORD not in u.password_hash
        assert u.password_hash.startswith("scrypt$")
    await c.aclose()


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"email": "bad-address", "username": "okname", "password": TEST_PASSWORD}, "invalid_input"),
        ({"email": "a@b.co", "username": "x", "password": TEST_PASSWORD}, None),          # too short: 422
        ({"email": "a@b.co", "username": "okname2", "password": "short"}, "invalid_input"),
    ],
)
async def test_registration_rejects_bad_input(app, payload, code):
    c = make_client(app)
    r = await c.post("/api/auth/register", json=payload)
    assert r.status_code in (400, 422)
    if code:
        assert r.json()["error"]["code"] == code
    await c.aclose()


async def test_duplicate_email_and_username_are_rejected(app):
    c = make_client(app)
    assert (await _register(c, "twin", email="twin@example.test")).status_code == 200
    other = make_client(app)
    r = await _register(other, "twin2", email="twin@example.test")
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"
    r = await _register(other, "TWIN", email="different@example.test")
    assert r.status_code == 409 and r.json()["error"]["code"] == "username_taken"
    await c.aclose()
    await other.aclose()


async def test_login_by_email_or_username_and_wrong_password(app):
    c = make_client(app)
    await _register(c, "returner")
    for ident in ("returner", "RETURNER", "returner@example.test", "Returner@Example.test"):
        fresh = make_client(app)
        r = await fresh.post("/api/auth/login", json={"login": ident, "password": TEST_PASSWORD})
        assert r.status_code == 200, f"{ident}: {r.text}"
        assert (await fresh.get("/api/me")).status_code == 200
        await fresh.aclose()
    fresh = make_client(app)
    r = await fresh.post("/api/auth/login", json={"login": "returner", "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    # The same message for an account that does not exist at all.
    r2 = await fresh.post("/api/auth/login", json={"login": "ghost", "password": "wrong-password"})
    assert r2.status_code == 401 and r2.json()["error"]["message"] == r.json()["error"]["message"]
    await fresh.aclose()
    await c.aclose()


async def test_repeated_failures_lock_the_account(app):
    c = make_client(app)
    await _register(c, "bruteforced")
    await c.aclose()
    attacker = make_client(app)
    codes = []
    for _ in range(9):
        r = await attacker.post("/api/auth/login", json={"login": "bruteforced", "password": "nope"})
        codes.append(r.json()["error"]["code"])
    assert "account_locked" in codes, codes
    # The lockout applies to the real password too, so guessing cannot be resumed.
    r = await attacker.post("/api/auth/login", json={"login": "bruteforced", "password": TEST_PASSWORD})
    assert r.status_code == 429 and r.json()["error"]["code"] == "account_locked"
    await attacker.aclose()


async def test_change_password_revokes_other_sessions(app):
    first = make_client(app)
    await _register(first, "rotator")
    me = (await first.get("/api/me")).json()
    first.headers["X-CSRF-Token"] = me["csrf"]

    second = make_client(app)
    r = await second.post("/api/auth/login", json={"login": "rotator", "password": TEST_PASSWORD})
    assert r.status_code == 200
    assert (await second.get("/api/me")).status_code == 200

    r = await first.post("/api/auth/password",
                         json={"current_password": TEST_PASSWORD, "new_password": "brand-new-password-77"})
    assert r.status_code == 200, r.text
    # The device that changed it stays signed in; the other one does not.
    assert (await first.get("/api/me")).status_code == 200
    assert (await second.get("/api/me")).status_code == 401

    third = make_client(app)
    assert (await third.post("/api/auth/login", json={"login": "rotator", "password": TEST_PASSWORD})).status_code == 401
    assert (await third.post("/api/auth/login",
                             json={"login": "rotator", "password": "brand-new-password-77"})).status_code == 200
    for c in (first, second, third):
        await c.aclose()


async def test_change_password_requires_the_current_one(app):
    p = await login(app)
    r = await p.post("/api/auth/password", json={"current_password": "not-it", "new_password": "another-password-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = await p.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": "short"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_input"
    await p.c.aclose()


async def test_configured_admin_exists_and_is_admin(app):
    """The bootstrap account from the launcher config can sign in and reach the panel."""
    c = make_client(app)
    r = await c.post("/api/auth/login", json={"login": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    me = (await c.get("/api/me")).json()
    assert me["is_admin"] is True
    # Super-admin follows the deployment config, so restores and role changes work.
    assert me["is_super_admin"] is True
    c.headers["X-CSRF-Token"] = me["csrf"]
    assert (await c.get("/api/admin/dashboard")).status_code == 403  # admin mode still required
    await c.post("/api/auth/admin-mode", json={"enabled": True})
    assert (await c.get("/api/admin/dashboard")).status_code == 200
    await c.aclose()


async def test_registration_can_be_closed_by_an_admin(admin, app):
    r = await admin.put("/api/admin/settings", json={"values": {"features.registration_open": False}})
    assert r.status_code == 200, r.text
    try:
        c = make_client(app)
        r = await _register(c, "latecomer")
        assert r.status_code == 403 and r.json()["error"]["code"] == "registration_closed"
        await c.aclose()
    finally:
        await admin.put("/api/admin/settings", json={"values": {"features.registration_open": True}})


async def test_promoted_admin_is_not_a_super_admin(admin, app):
    """An in-game promotion must not hand out the super-admin-only operations."""
    p = await login(app)
    await admin_action(admin, p.id, "set_role", role="admin", confirm=True)
    me = (await p.get("/api/me")).json()
    assert me["is_admin"] is True and me["is_super_admin"] is False
    await p.post("/api/auth/admin-mode", json={"enabled": True})
    r = await p.post("/api/admin/backups/restore", json={"filename": "cosmic-rng-20260101-000000-manual.sqlite3",
                                                         "confirm": "RESTORE"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "super_admin_required"
    await p.c.aclose()


async def test_banned_account_cannot_sign_in(admin, app):
    p = await login(app)
    await set_user(p.id, status="banned", status_reason="test")
    c = make_client(app)
    r = await c.post("/api/auth/login", json={"login": p.me["user"]["username"], "password": TEST_PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "banned"
    await c.aclose()
    await p.c.aclose()
