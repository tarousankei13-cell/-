"""
データベース接続

DATABASE_URL を差し替えるだけで SQLite ⇄ PostgreSQL を切り替えられる。
  SQLite     : sqlite+aiosqlite:///./data/bot.db
  PostgreSQL : postgresql+asyncpg://user:pass@host/dbname
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from core.locks import lock_user
from db.models import Base

log = logging.getLogger("bot.db")

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


async def init_db(database_url: str) -> AsyncEngine:
    """エンジンを作り、テーブルが無ければ作成する。"""
    global _engine, _sessionmaker

    if _is_sqlite(database_url):
        # ./data/bot.db のようなパスの親ディレクトリを先に作る
        path = database_url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(database_url, echo=False, future=True)

        # SQLite を同時アクセスに耐えるようにする
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")    # 読み書きの並行性を上げる
            cur.execute("PRAGMA synchronous=NORMAL")  # 速度と安全性の折衷
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")   # ロック待ちを5秒許容
            cur.close()
    else:
        _engine = create_async_engine(
            database_url, echo=False, future=True,
            pool_size=10, max_overflow=20, pool_pre_ping=True,
        )

    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _is_sqlite(database_url):
            await _add_missing_columns(conn)

    log.info("データベースに接続しました (%s)", "SQLite" if _is_sqlite(database_url) else "PostgreSQL")
    return _engine


async def _add_missing_columns(conn) -> None:
    """
    既存のDBに、あとから増えた列を足す。

    本格的なマイグレーションは使っていないので、列の追加だけ面倒を見る。
    （列の削除や型変更は扱わない）
    """
    for table in Base.metadata.sorted_tables:
        rows = (await conn.exec_driver_sql(f"PRAGMA table_info({table.name})")).fetchall()
        if not rows:
            continue
        existing = {r[1] for r in rows}
        for col in table.columns:
            if col.name in existing:
                continue
            try:
                col_type = col.type.compile(conn.dialect)
                # ⚠️ ALTER TABLE ADD COLUMN は、既存の行を **NULL** にする。
                #    「0 を入れておく」つもりの列が NULL になると、
                #    足し算で落ちたり、件数の数え上げから漏れたりする。
                #    モデルに既定値があるなら、それを使って必ず埋める。
                default = _scalar_default(col)
                suffix = "" if default is None else f" DEFAULT {default}"
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}{suffix}"
                )
                if default is not None:
                    await conn.exec_driver_sql(
                        f"UPDATE {table.name} SET {col.name} = {default} "
                        f"WHERE {col.name} IS NULL"
                    )
                log.info("列を追加しました: %s.%s", table.name, col.name)
            except Exception as e:
                log.warning("列 %s.%s を追加できませんでした: %s", table.name, col.name, e)


def _scalar_default(col) -> str | None:
    """
    その列の既定値を、SQL にそのまま書ける形で返す。

    関数で決まる既定値（作成日時など）は、あとから一律に入れる意味が
    ないので扱わない（NULL のままにする）。
    """
    d = getattr(col, "default", None)
    if d is None or getattr(d, "is_callable", False):
        return None
    arg = getattr(d, "arg", None)
    if callable(arg) or arg is None:
        return None
    if isinstance(arg, bool):
        return "1" if arg else "0"
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, str):
        escaped = arg.replace("'", "''")
        return f"'{escaped}'"
    return None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("init_db() がまだ呼ばれていません")
    return _sessionmaker


@asynccontextmanager
async def session_scope():
    """
    トランザクション境界。

        async with session_scope() as s:
            ...          # 例外が出れば自動でロールバック
    """
    sm = get_sessionmaker()
    async with sm() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def _pg_advisory_lock(session: AsyncSession, discord_id: int) -> None:
    """PostgreSQL の場合だけ、トランザクション内のアドバイザリロックも取る。"""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": discord_id & 0x7FFFFFFFFFFFFFFF},
        )


@asynccontextmanager
async def user_scope(discord_id: int):
    """
    残高を変更するときは必ずこれを使う。

        async with user_scope(discord_id) as s:
            await ledger.hold(s, discord_id, 354, order_id=...)

    利用者ごとのロックを取ってからトランザクションを開くため、
    「残高を読む → 判定 → 記帳」が他の処理に割り込まれない。
    これを使わずに残高を変更しようとすると LockError で止まる。
    """
    async with lock_user(discord_id):
        async with session_scope() as s:
            await _pg_advisory_lock(s, discord_id)
            yield s


async def close_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        log.info("データベース接続を閉じました")
    _engine = None
    _sessionmaker = None
