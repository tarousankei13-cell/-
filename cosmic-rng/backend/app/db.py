"""Database engine / session management (SQLAlchemy 2.0 async).

Two backends are supported from the same models and the same service code:

* **PostgreSQL** (``postgresql+asyncpg://``) — the production target. Multi-worker,
  LISTEN/NOTIFY event bus, advisory-lock leader election, ``pg_dump`` backups.
* **SQLite** (``sqlite+aiosqlite://``) — a single-file, zero-infrastructure mode for
  hosts where no database server can be installed. Single worker, in-process event
  bus, file-copy backups. Everything else behaves identically; the dialect
  differences are confined to this module (type variants, upserts, row locking).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, event
from sqlalchemy.dialects.postgresql import insert as _pg_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


def is_sqlite(url: str | None = None) -> bool:
    return (url or get_settings().database_url).startswith("sqlite")


def sqlite_path() -> Path:
    """Filesystem path of the SQLite database (raises for any other backend)."""
    url = get_settings().database_url
    if not is_sqlite(url):
        raise RuntimeError("not a SQLite database")
    raw = url.split("///", 1)[1].split("?", 1)[0]
    return Path(raw).resolve()


def greatest(*args: Any) -> Any:
    """``GREATEST`` on PostgreSQL; SQLite spells the same thing ``max(a, b)``."""
    from sqlalchemy import func

    return func.max(*args) if is_sqlite() else func.greatest(*args)


def upsert(table: Any) -> Any:
    """``INSERT ... ON CONFLICT`` for whichever backend is configured.

    Both dialects expose the same ``on_conflict_do_nothing`` /
    ``on_conflict_do_update`` API, so call sites stay dialect-agnostic.
    """
    return _sqlite_insert(table) if is_sqlite() else _pg_insert(table)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _sqlite_engine(s: Any) -> AsyncEngine:
    """SQLite needs WAL plus a generous busy timeout, or concurrent requests
    (rolls, the scheduler, WebSocket pushes) collide on the single writer."""
    from sqlalchemy.pool import NullPool, StaticPool

    memory = ":memory:" in s.database_url
    kw: dict[str, Any] = {"echo": s.db_echo, "connect_args": {"timeout": 30}}
    if memory:
        kw["poolclass"] = StaticPool
    elif s.db_null_pool:
        kw["poolclass"] = NullPool
    else:
        kw["pool_size"] = s.db_pool_size
        kw["max_overflow"] = s.db_max_overflow
    engine = create_async_engine(s.database_url, **kw)

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        if not memory:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
        # Hand transaction control to us so every transaction can start as a writer.
        dbapi_conn.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin_immediate(conn: Any) -> None:
        # Services read a row, decide, then write it (buy, sell, trade, roll). On
        # PostgreSQL SELECT ... FOR UPDATE holds that row; SQLite has no row locks,
        # and a deferred transaction that upgrades from read to write deadlocks
        # instead of waiting out busy_timeout. Taking the write lock up front turns
        # those races into a short wait, which is what the FOR UPDATE calls intend.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        s = get_settings()
        if is_sqlite(s.database_url):
            _engine = _sqlite_engine(s)
        elif s.db_null_pool:
            from sqlalchemy.pool import NullPool

            _engine = create_async_engine(s.database_url, poolclass=NullPool, echo=s.db_echo)
        else:
            _engine = create_async_engine(
                s.database_url,
                pool_size=s.db_pool_size,
                max_overflow=s.db_max_overflow,
                pool_pre_ping=True,
                pool_recycle=1800,
                echo=s.db_echo,
            )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone session for background tasks. Caller commits explicitly."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request; rolled back unless committed."""
    async with get_sessionmaker()() as session:
        yield session
