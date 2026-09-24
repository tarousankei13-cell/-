"""Integration test fixtures: a real database, migrated and seeded once per session.

Defaults to PostgreSQL. Point ``TEST_DATABASE_URL`` at a ``sqlite+aiosqlite://``
file to run the same suite against the single-file backend.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://cosmic:cosmic@127.0.0.1:5432/cosmic_rng_test")
os.environ.update({
    "ENVIRONMENT": "test",
    "DATABASE_URL": TEST_DB,
    "EVENT_BUS": "local",
    "RUN_SCHEDULER": "false",
    "COOKIE_SECURE": "false",
    "DEV_LOGIN_ENABLED": "true",
    "ADMIN_DISCORD_IDS": "999000001",
    "PUBLIC_BASE_URL": "http://testserver",
    "ALLOWED_ORIGINS": "http://testserver",
    "DB_NULL_POOL": "true",
    "BACKUP_DIR": tempfile.mkdtemp(prefix="cosmic-backup-"),
    "DISCORD_BOT_TOKEN": "",
    "SECRET_KEY": "test-secret-key-0123456789abcdef0123456789",
})

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
ADMIN_DISCORD_ID = 999000001


def _reset_database() -> None:
    import asyncio

    if TEST_DB.startswith("sqlite"):
        for suffix in ("", "-wal", "-shm"):
            f = Path(TEST_DB.split("///", 1)[1] + suffix)
            if f.exists():
                f.unlink()
    else:
        import asyncpg

        async def reset() -> None:
            conn = await asyncpg.connect(TEST_DB.replace("postgresql+asyncpg://", "postgresql://"))
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            await conn.close()

        asyncio.run(reset())
    subprocess.run([sys.executable, "-m", "app.cli", "migrate"], cwd=BACKEND, env=dict(os.environ), check=True,
                   stdout=subprocess.DEVNULL)


def pytest_sessionstart(session: Any) -> None:
    # Pure unit-test runs (e.g. -k rng) don't need the database, but resetting is cheap and keeps runs deterministic.
    if os.environ.get("SKIP_DB_RESET") != "1":
        _reset_database()


@pytest_asyncio.fixture(scope="session")
async def app() -> AsyncIterator[Any]:
    from app.content.registry import get_registry
    from app.core.ratelimit import limiter
    from app.db import session_scope
    from app.main import create_app, ensure_content
    from app.models import GameSetting

    await ensure_content()
    async with session_scope() as db:
        for k, v in {"roll.base_seconds": 0.05, "roll.min_seconds": 0.01, "roll.tolerance_ms": 50, "gift.cooldown_seconds": 60}.items():
            await db.merge(GameSetting(key=k, value=v))
        await db.commit()
        await get_registry().reload(db)
    limiter.enabled = False
    application = create_app()
    yield application


class Player:
    def __init__(self, client: httpx.AsyncClient, me: dict[str, Any]) -> None:
        self.c = client
        self.me = me
        self.id: int = me["user"]["id"]

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.c.get(url, **kw)

    async def post(self, url: str, json: Any = None, idem: bool = False, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        if idem:
            headers["Idempotency-Key"] = uuid.uuid4().hex
        return await self.c.post(url, json=json, headers=headers, **kw)

    async def put(self, url: str, json: Any = None) -> httpx.Response:
        return await self.c.put(url, json=json)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.c.delete(url, **kw)

    async def roll(self, **body: Any) -> dict[str, Any]:
        import asyncio

        for _ in range(40):
            r = await self.post("/api/roll", json=body)
            if r.status_code == 429 and r.json()["error"]["code"] == "cooldown":
                await asyncio.sleep(0.06)
                continue
            assert r.status_code == 200, r.text
            return r.json()
        raise AssertionError("cooldown never expired")


def make_client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", headers={"Origin": "http://testserver"})


async def login(app: Any, discord_id: int | None = None, name: str | None = None) -> Player:
    discord_id = discord_id or int(uuid.uuid4().int % 10**15) + 10**16
    name = name or f"p{discord_id % 100000}"
    c = make_client(app)
    r = await c.post("/api/auth/dev-login", json={"discord_id": discord_id, "username": name})
    assert r.status_code == 302, r.text
    me = (await c.get("/api/me")).json()
    c.headers["X-CSRF-Token"] = me["csrf"]
    return Player(c, me)


@pytest_asyncio.fixture
async def player(app: Any) -> AsyncIterator[Player]:
    p = await login(app)
    yield p
    await p.c.aclose()


@pytest_asyncio.fixture
async def player2(app: Any) -> AsyncIterator[Player]:
    p = await login(app)
    yield p
    await p.c.aclose()


@pytest_asyncio.fixture
async def admin(app: Any) -> AsyncIterator[Player]:
    p = await login(app, ADMIN_DISCORD_ID, "architect")
    r = await p.post("/api/auth/admin-mode", json={"enabled": True})
    assert r.status_code == 200, r.text
    yield p
    await p.c.aclose()


async def set_user(user_id: int, **values: Any) -> None:
    from sqlalchemy import update

    from app.db import session_scope
    from app.models import User

    async with session_scope() as db:
        await db.execute(update(User).where(User.id == user_id).values(**values))
        await db.commit()


async def admin_action(admin: Player, user_id: int, action: str, **params: Any) -> dict[str, Any]:
    r = await admin.post(f"/api/admin/users/{user_id}/action", json={"action": action, "params": params, "reason": "test"})
    assert r.status_code == 200, r.text
    return r.json()
