"""SQLite データアクセス層。

設計方針
--------
* 単一コネクション + 単一ワーカースレッド + ``asyncio.Lock`` で全アクセスを直列化し、
  イベントループをブロックせずに整合性を担保する。
* 金額は INTEGER、チャージ率は Decimal 文字列 (TEXT) で保持し float を使わない。
* 残高更新と履歴保存は必ず同一トランザクション内で行う。
* 二重残高付与は「BEGIN IMMEDIATE 内での存在確認」+「UNIQUE 制約」の二重で防ぐ。
* Source of Truth: 現在残高=balances / 履歴=balance_history / 取引=charge_transactions
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import config
import utils

logger = logging.getLogger(config.LOGGER_DB)


class DatabaseError(RuntimeError):
    """DB 操作の失敗。"""


class IllegalStateTransition(DatabaseError):
    """許可されていない Transaction 状態遷移。"""


class AlreadyCredited(DatabaseError):
    """既に残高付与済み (冪等性により2回目以降は付与しない)。"""


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class GuildSettings:
    """サーバーごとの運用設定 (すべて Discord コマンドから変更可能)。"""

    guild_id: int
    charge_rate: Decimal = field(default_factory=lambda: Decimal(config.DEFAULT_CHARGE_RATE))
    minimum_charge: int = config.DEFAULT_MINIMUM_CHARGE
    maximum_charge: int = config.DEFAULT_MAXIMUM_CHARGE
    daily_limit: int = config.DEFAULT_DAILY_LIMIT
    guild_daily_limit: int = config.DEFAULT_GUILD_DAILY_LIMIT
    admin_role_id: int | None = None
    achievement_channel_id: int | None = None
    log_channel_id: int | None = None
    maintenance: bool = False
    emergency_stop: bool = False
    ranking_enabled: bool = True
    ranking_limit: int = config.DEFAULT_RANKING_LIMIT
    ranking_interval: int = config.DEFAULT_RANKING_INTERVAL
    ranking_hide_absent: bool = False
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GuildSettings":
        return cls(
            guild_id=row["guild_id"],
            charge_rate=utils.to_decimal(row["charge_rate"]) or Decimal(config.DEFAULT_CHARGE_RATE),
            minimum_charge=row["minimum_charge"],
            maximum_charge=row["maximum_charge"],
            daily_limit=row["daily_limit"],
            guild_daily_limit=row["guild_daily_limit"],
            admin_role_id=row["admin_role_id"],
            achievement_channel_id=row["achievement_channel_id"],
            log_channel_id=row["log_channel_id"],
            maintenance=bool(row["maintenance"]),
            emergency_stop=bool(row["emergency_stop"]),
            ranking_enabled=bool(row["ranking_enabled"]),
            ranking_limit=row["ranking_limit"],
            ranking_interval=row["ranking_interval"],
            ranking_hide_absent=bool(row["ranking_hide_absent"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(slots=True)
class KyashAccountRecord:
    """受取用 Kyash アカウントの保存情報 (パスワードは保存しない)。"""

    email: str | None = None
    client_uuid: str | None = None
    installation_uuid: str | None = None
    access_token_enc: str | None = None
    status: str = config.KyashAccountStatus.UNCONFIGURED
    username: str | None = None
    wallet_uuid: str | None = None
    last_checked_at: int | None = None
    last_error: str | None = None
    updated_at: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> "KyashAccountRecord":
        if row is None:
            return cls()
        return cls(
            email=row["email"],
            client_uuid=row["client_uuid"],
            installation_uuid=row["installation_uuid"],
            access_token_enc=row["access_token_enc"],
            status=row["status"] or config.KyashAccountStatus.UNCONFIGURED,
            username=row["username"],
            wallet_uuid=row["wallet_uuid"],
            last_checked_at=row["last_checked_at"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# スキーマ
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS system_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS allowed_guilds (
        guild_id   INTEGER PRIMARY KEY,
        allowed_by INTEGER,
        allowed_at INTEGER NOT NULL DEFAULT 0,
        status     TEXT NOT NULL DEFAULT 'ALLOWED',
        note       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id               INTEGER PRIMARY KEY,
        charge_rate            TEXT    NOT NULL,
        minimum_charge         INTEGER NOT NULL,
        maximum_charge         INTEGER NOT NULL,
        daily_limit            INTEGER NOT NULL,
        guild_daily_limit      INTEGER NOT NULL DEFAULT 0,
        admin_role_id          INTEGER,
        achievement_channel_id INTEGER,
        log_channel_id         INTEGER,
        maintenance            INTEGER NOT NULL DEFAULT 0,
        emergency_stop         INTEGER NOT NULL DEFAULT 0,
        ranking_enabled        INTEGER NOT NULL DEFAULT 1,
        ranking_limit          INTEGER NOT NULL DEFAULT 10,
        ranking_interval       INTEGER NOT NULL DEFAULT 300,
        ranking_hide_absent    INTEGER NOT NULL DEFAULT 0,
        created_at             INTEGER NOT NULL DEFAULT 0,
        updated_at             INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        guild_id      INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        frozen        INTEGER NOT NULL DEFAULT 0,
        frozen_reason TEXT,
        frozen_by     INTEGER,
        frozen_at     INTEGER,
        created_at    INTEGER NOT NULL DEFAULT 0,
        updated_at    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS balances (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        balance    INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS charge_transactions (
        id                     TEXT PRIMARY KEY,
        guild_id               INTEGER NOT NULL,
        user_id                INTEGER NOT NULL,
        requested_amount       INTEGER NOT NULL,
        received_amount        INTEGER,
        charge_rate            TEXT    NOT NULL,
        credited_amount        INTEGER,
        link_hash              TEXT UNIQUE,
        link_uuid              TEXT UNIQUE,
        status                 TEXT    NOT NULL,
        retry_count            INTEGER NOT NULL DEFAULT 0,
        error_code             TEXT,
        error_message          TEXT,
        balance_before         INTEGER,
        balance_after          INTEGER,
        wallet_before          INTEGER,
        source                 TEXT    NOT NULL DEFAULT 'AUTOMATIC',
        achievement_channel_id INTEGER,
        achievement_message_id INTEGER,
        expires_at             INTEGER,
        processing_started_at  INTEGER,
        created_at             INTEGER NOT NULL DEFAULT 0,
        updated_at             INTEGER NOT NULL DEFAULT 0,
        completed_at           INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tx_guild_user ON charge_transactions(guild_id, user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tx_status ON charge_transactions(status)",
    "CREATE INDEX IF NOT EXISTS idx_tx_created ON charge_transactions(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tx_guild_status ON charge_transactions(guild_id, status)",
    """
    CREATE TABLE IF NOT EXISTS balance_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id       INTEGER NOT NULL,
        user_id        INTEGER NOT NULL,
        change_amount  INTEGER NOT NULL,
        balance_before INTEGER NOT NULL,
        balance_after  INTEGER NOT NULL,
        type           TEXT    NOT NULL,
        transaction_id TEXT,
        operator_id    INTEGER,
        reason         TEXT,
        created_at     INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_guild_user ON balance_history(guild_id, user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_history_tx ON balance_history(transaction_id)",
    # 同一 Transaction では1度しか残高付与できない (二重付与の最終防壁)
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_history_tx_type
        ON balance_history(transaction_id, type) WHERE transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS panels (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL UNIQUE,
        panel_type TEXT    NOT NULL DEFAULT 'CHARGE',
        active     INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_panels_guild ON panels(guild_id, active)",
    """
    CREATE TABLE IF NOT EXISTS ranking_panels (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id        INTEGER NOT NULL,
        channel_id      INTEGER NOT NULL,
        message_id      INTEGER NOT NULL UNIQUE,
        active          INTEGER NOT NULL DEFAULT 1,
        last_signature  TEXT,
        last_updated_at INTEGER,
        created_at      INTEGER NOT NULL DEFAULT 0,
        updated_at      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ranking_panels_guild ON ranking_panels(guild_id, active)",
    """
    CREATE TABLE IF NOT EXISTS admin_audit_logs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id   TEXT,
        guild_id       INTEGER,
        actor_id       INTEGER NOT NULL,
        action         TEXT    NOT NULL,
        target_user_id INTEGER,
        detail         TEXT,
        created_at     INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_guild ON admin_audit_logs(guild_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_logs(action)",
    """
    CREATE TABLE IF NOT EXISTS charge_queue (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id  TEXT    NOT NULL UNIQUE
            REFERENCES charge_transactions(id) ON DELETE CASCADE,
        status          TEXT    NOT NULL DEFAULT 'QUEUED',
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL DEFAULT 0,
        enqueued_at     INTEGER NOT NULL DEFAULT 0,
        updated_at      INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_queue_ready ON charge_queue(status, next_attempt_at, enqueued_at)",
    """
    CREATE TABLE IF NOT EXISTS kyash_account (
        id                INTEGER PRIMARY KEY CHECK (id = 1),
        email             TEXT,
        client_uuid       TEXT,
        installation_uuid TEXT,
        access_token_enc  TEXT,
        status            TEXT NOT NULL DEFAULT 'UNCONFIGURED',
        username          TEXT,
        wallet_uuid       TEXT,
        last_checked_at   INTEGER,
        last_error        TEXT,
        created_at        INTEGER NOT NULL DEFAULT 0,
        updated_at        INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_queue (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        kind            TEXT    NOT NULL,
        guild_id        INTEGER,
        user_id         INTEGER,
        channel_id      INTEGER,
        transaction_id  TEXT,
        payload         TEXT,
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL DEFAULT 0,
        status          TEXT    NOT NULL DEFAULT 'PENDING',
        last_error      TEXT,
        created_at      INTEGER NOT NULL DEFAULT 0,
        updated_at      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notify_ready ON notification_queue(status, next_attempt_at)",
)


class Database:
    """SQLite への非同期アクセスラッパ。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self._settings_cache: dict[int, GuildSettings] = {}
        self._allowed_cache: dict[int, bool] = {}

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> None:
        """コネクションを開き、PRAGMA を適用してスキーマを作成する。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                isolation_level=None,  # トランザクションを明示制御する
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA busy_timeout = 30000")
            return conn

        loop = asyncio.get_running_loop()
        self._conn = await loop.run_in_executor(self._executor, _open)
        logger.info("データベースへ接続しました: %s", self._path)
        await self.initialize_schema()

    async def close(self) -> None:
        """WAL を切り詰めてコネクションを閉じる。"""
        if self._conn is None:
            return
        conn = self._conn

        def _close() -> None:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            conn.close()

        loop = asyncio.get_running_loop()
        async with self._lock:
            await loop.run_in_executor(self._executor, _close)
            self._conn = None
        self._executor.shutdown(wait=True)
        logger.info("データベース接続を閉じました")

    # ------------------------------------------------------------------
    # 実行ヘルパ
    # ------------------------------------------------------------------
    async def run(self, fn: Callable[[sqlite3.Connection], Any], *, write: bool = False) -> Any:
        """``fn(conn)`` を専用スレッドで実行する。

        Args:
            fn: コネクションを受け取る同期関数。
            write: True の場合 ``BEGIN IMMEDIATE`` ～ ``COMMIT`` で囲む。
                   例外発生時は必ず ROLLBACK する。
        """
        if self._conn is None:
            raise DatabaseError("データベースへ接続していません")
        conn = self._conn
        loop = asyncio.get_running_loop()

        def _runner() -> Any:
            if not write:
                return fn(conn)
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = fn(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        async with self._lock:
            return await loop.run_in_executor(self._executor, _runner)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return await self.run(lambda c: c.execute(sql, tuple(params)).fetchone())

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await self.run(lambda c: c.execute(sql, tuple(params)).fetchall())

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        def _fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount

        return await self.run(_fn, write=True)

    # ------------------------------------------------------------------
    # スキーマ / マイグレーション
    # ------------------------------------------------------------------
    async def initialize_schema(self) -> None:
        """テーブル作成とスキーマバージョンの適用。"""

        def _fn(conn: sqlite3.Connection) -> int:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key='schema_version'"
            ).fetchone()
            current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
            if current > config.SCHEMA_VERSION:
                raise DatabaseError(
                    f"DBのスキーマバージョン({current})がBot({config.SCHEMA_VERSION})より新しいため起動できません"
                )
            # 将来のバージョンアップではここに ALTER TABLE 等を追加する。
            # 既存列の欠落は _ensure_column で前方互換的に補う。
            for table, column, ddl in _FORWARD_COMPAT_COLUMNS:
                _ensure_column(conn, table, column, ddl)
            if current != config.SCHEMA_VERSION:
                conn.execute(
                    "INSERT INTO system_settings(key, value, updated_at) VALUES('schema_version', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (str(config.SCHEMA_VERSION), utils.now_ts()),
                )
            return current

        previous = await self.run(_fn, write=True)
        if previous != config.SCHEMA_VERSION:
            logger.info("スキーマを v%s から v%s へ適用しました", previous, config.SCHEMA_VERSION)
        else:
            logger.info("スキーマ v%s を確認しました", config.SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # system_settings
    # ------------------------------------------------------------------
    async def get_system_value(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM system_settings WHERE key=?", (key,))
        return row["value"] if row else None

    async def set_system_value(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utils.now_ts()),
        )

    # ------------------------------------------------------------------
    # allowed_guilds
    # ------------------------------------------------------------------
    async def is_guild_allowed(self, guild_id: int) -> bool:
        cached = self._allowed_cache.get(guild_id)
        if cached is not None:
            return cached
        row = await self.fetchone(
            "SELECT status FROM allowed_guilds WHERE guild_id=?", (guild_id,)
        )
        allowed = bool(row and row["status"] == "ALLOWED")
        self._allowed_cache[guild_id] = allowed
        return allowed

    async def set_guild_permission(
        self, guild_id: int, status: str, actor_id: int, note: str | None = None
    ) -> None:
        await self.execute(
            "INSERT INTO allowed_guilds(guild_id, allowed_by, allowed_at, status, note) VALUES(?,?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET allowed_by=excluded.allowed_by, "
            "allowed_at=excluded.allowed_at, status=excluded.status, note=excluded.note",
            (guild_id, actor_id, utils.now_ts(), status, note),
        )
        self._allowed_cache[guild_id] = status == "ALLOWED"

    async def list_guild_permissions(self) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT guild_id, allowed_by, allowed_at, status, note FROM allowed_guilds "
            "ORDER BY status ASC, allowed_at DESC"
        )

    async def count_allowed_guilds(self) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS c FROM allowed_guilds WHERE status='ALLOWED'"
        )
        return int(row["c"]) if row else 0

    def invalidate_guild_cache(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            self._allowed_cache.clear()
        else:
            self._allowed_cache.pop(guild_id, None)

    # ------------------------------------------------------------------
    # guild_settings
    # ------------------------------------------------------------------
    async def get_settings(self, guild_id: int) -> GuildSettings:
        """設定を取得する (無ければデフォルト値で作成)。"""
        cached = self._settings_cache.get(guild_id)
        if cached is not None:
            return cached

        def _fn(conn: sqlite3.Connection) -> sqlite3.Row:
            row = conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,)
            ).fetchone()
            if row is None:
                now = utils.now_ts()
                conn.execute(
                    "INSERT INTO guild_settings("
                    "guild_id, charge_rate, minimum_charge, maximum_charge, daily_limit, "
                    "guild_daily_limit, ranking_enabled, ranking_limit, ranking_interval, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        guild_id,
                        config.DEFAULT_CHARGE_RATE,
                        config.DEFAULT_MINIMUM_CHARGE,
                        config.DEFAULT_MAXIMUM_CHARGE,
                        config.DEFAULT_DAILY_LIMIT,
                        config.DEFAULT_GUILD_DAILY_LIMIT,
                        1,
                        config.DEFAULT_RANKING_LIMIT,
                        config.DEFAULT_RANKING_INTERVAL,
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,)
                ).fetchone()
            return row

        row = await self.run(_fn, write=True)
        settings = GuildSettings.from_row(row)
        self._settings_cache[guild_id] = settings
        return settings

    #: 更新を許可する列 (SQL インジェクション対策として列名はホワイトリスト経由のみ)
    UPDATABLE_SETTINGS: frozenset[str] = frozenset({
        "charge_rate", "minimum_charge", "maximum_charge", "daily_limit",
        "guild_daily_limit", "admin_role_id", "achievement_channel_id",
        "log_channel_id", "maintenance", "emergency_stop", "ranking_enabled",
        "ranking_limit", "ranking_interval", "ranking_hide_absent",
    })

    async def update_settings(self, guild_id: int, **values: Any) -> GuildSettings:
        """設定を更新する。列名はホワイトリストで検証する。"""
        await self.get_settings(guild_id)  # 行の存在を保証
        invalid = set(values) - self.UPDATABLE_SETTINGS
        if invalid:
            raise DatabaseError(f"更新できない設定項目です: {sorted(invalid)}")
        if not values:
            return await self.get_settings(guild_id)
        assignments = ", ".join(f"{name}=?" for name in values)
        params = list(values.values()) + [utils.now_ts(), guild_id]
        await self.execute(
            f"UPDATE guild_settings SET {assignments}, updated_at=? WHERE guild_id=?", params
        )
        self._settings_cache.pop(guild_id, None)
        return await self.get_settings(guild_id)

    def invalidate_settings_cache(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            self._settings_cache.clear()
        else:
            self._settings_cache.pop(guild_id, None)

    async def list_guilds_with_settings(self) -> list[int]:
        rows = await self.fetchall("SELECT guild_id FROM guild_settings")
        return [int(r["guild_id"]) for r in rows]

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    async def ensure_user(self, guild_id: int, user_id: int) -> None:
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO users(guild_id, user_id, created_at, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO NOTHING",
            (guild_id, user_id, now, now),
        )

    async def get_user(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        return await self.fetchone(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )

    async def is_frozen(self, guild_id: int, user_id: int) -> bool:
        row = await self.fetchone(
            "SELECT frozen FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        return bool(row and row["frozen"])

    async def set_frozen(
        self, guild_id: int, user_id: int, frozen: bool, actor_id: int, reason: str | None
    ) -> None:
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO users(guild_id, user_id, frozen, frozen_reason, frozen_by, frozen_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET frozen=excluded.frozen, "
            "frozen_reason=excluded.frozen_reason, frozen_by=excluded.frozen_by, "
            "frozen_at=excluded.frozen_at, updated_at=excluded.updated_at",
            (
                guild_id, user_id, 1 if frozen else 0, reason, actor_id,
                now if frozen else None, now, now,
            ),
        )

    async def list_frozen_user_ids(self, guild_id: int) -> set[int]:
        rows = await self.fetchall(
            "SELECT user_id FROM users WHERE guild_id=? AND frozen=1", (guild_id,)
        )
        return {int(r["user_id"]) for r in rows}

    # ------------------------------------------------------------------
    # balances / ranking
    # ------------------------------------------------------------------
    async def get_balance(self, guild_id: int, user_id: int) -> int:
        row = await self.fetchone(
            "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        return int(row["balance"]) if row else 0

    async def get_ranking(self, guild_id: int, limit: int) -> list[sqlite3.Row]:
        """残高ランキング (balance DESC, user_id ASC の決定的な並び)。

        凍結ユーザーは対象外。Bot ユーザーの除外は呼び出し側 (Discord情報が必要)。
        """
        return await self.fetchall(
            "SELECT b.user_id, b.balance FROM balances b "
            "LEFT JOIN users u ON u.guild_id = b.guild_id AND u.user_id = b.user_id "
            "WHERE b.guild_id=? AND b.balance > 0 AND COALESCE(u.frozen, 0) = 0 "
            "ORDER BY b.balance DESC, b.user_id ASC LIMIT ?",
            (guild_id, max(1, int(limit))),
        )

    async def get_user_rank(self, guild_id: int, user_id: int) -> tuple[int | None, int, int]:
        """(順位, 残高, 対象人数) を返す。対象外なら順位は None。"""

        def _fn(conn: sqlite3.Connection) -> tuple[int | None, int, int]:
            total_row = conn.execute(
                "SELECT COUNT(*) AS c FROM balances b "
                "LEFT JOIN users u ON u.guild_id=b.guild_id AND u.user_id=b.user_id "
                "WHERE b.guild_id=? AND b.balance > 0 AND COALESCE(u.frozen,0)=0",
                (guild_id,),
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            me = conn.execute(
                "SELECT b.balance, COALESCE(u.frozen,0) AS frozen FROM balances b "
                "LEFT JOIN users u ON u.guild_id=b.guild_id AND u.user_id=b.user_id "
                "WHERE b.guild_id=? AND b.user_id=?",
                (guild_id, user_id),
            ).fetchone()
            if me is None:
                return None, 0, total
            balance = int(me["balance"])
            if me["frozen"] or balance <= 0:
                return None, balance, total
            higher = conn.execute(
                "SELECT COUNT(*) AS c FROM balances b "
                "LEFT JOIN users u ON u.guild_id=b.guild_id AND u.user_id=b.user_id "
                "WHERE b.guild_id=? AND COALESCE(u.frozen,0)=0 AND b.balance > 0 AND "
                "(b.balance > ? OR (b.balance = ? AND b.user_id < ?))",
                (guild_id, balance, balance, user_id),
            ).fetchone()
            return int(higher["c"]) + 1, balance, total

        return await self.run(_fn)

    async def count_guild_users(self, guild_id: int) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS c FROM balances WHERE guild_id=?", (guild_id,)
        )
        return int(row["c"]) if row else 0

    async def sum_guild_balance(self, guild_id: int) -> int:
        row = await self.fetchone(
            "SELECT COALESCE(SUM(balance),0) AS s FROM balances WHERE guild_id=?", (guild_id,)
        )
        return int(row["s"]) if row else 0

    # ------------------------------------------------------------------
    # charge_transactions
    # ------------------------------------------------------------------
    async def create_transaction(
        self,
        *,
        guild_id: int,
        user_id: int,
        requested_amount: int,
        charge_rate: Decimal,
        expires_at: int,
        status: str = config.TxStatus.WAITING_LINK,
        source: str = config.TxSource.AUTOMATIC,
    ) -> str:
        """Transaction を作成し ID を返す。チャージ率は作成時点の値を保存する。"""
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> str:
            for _ in range(8):
                tx_id = utils.new_transaction_id()
                try:
                    conn.execute(
                        "INSERT INTO charge_transactions("
                        "id, guild_id, user_id, requested_amount, charge_rate, status, "
                        "source, expires_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            tx_id, guild_id, user_id, requested_amount,
                            utils.rate_to_db(charge_rate), status, source, expires_at, now, now,
                        ),
                    )
                    return tx_id
                except sqlite3.IntegrityError:
                    continue  # ID 衝突 → 再生成
            raise DatabaseError("取引IDの生成に失敗しました")

        return await self.run(_fn, write=True)

    async def get_transaction(self, tx_id: str) -> sqlite3.Row | None:
        return await self.fetchone("SELECT * FROM charge_transactions WHERE id=?", (tx_id,))

    async def get_active_transaction(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        """利用者のアクティブな Transaction (1件のみ許可) を返す。"""
        placeholders = ",".join("?" for _ in config.ACTIVE_STATUSES)
        return await self.fetchone(
            f"SELECT * FROM charge_transactions WHERE guild_id=? AND user_id=? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            (guild_id, user_id, *config.ACTIVE_STATUSES),
        )

    async def count_active_transactions(self, guild_id: int, user_id: int) -> int:
        placeholders = ",".join("?" for _ in config.ACTIVE_STATUSES)
        row = await self.fetchone(
            f"SELECT COUNT(*) AS c FROM charge_transactions WHERE guild_id=? AND user_id=? "
            f"AND status IN ({placeholders})",
            (guild_id, user_id, *config.ACTIVE_STATUSES),
        )
        return int(row["c"]) if row else 0

    async def transition_status(
        self,
        tx_id: str,
        new_status: str,
        *,
        expected: Iterable[str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        **columns: Any,
    ) -> sqlite3.Row:
        """状態遷移を検証しつつ Transaction を更新する。

        Raises:
            IllegalStateTransition: 現在状態から ``new_status`` への遷移が許可されていない場合。
        """
        allowed_columns = {
            "received_amount", "credited_amount", "link_hash", "link_uuid",
            "retry_count", "balance_before", "balance_after", "wallet_before",
            "achievement_channel_id", "achievement_message_id", "expires_at",
            "processing_started_at", "completed_at",
        }
        invalid = set(columns) - allowed_columns
        if invalid:
            raise DatabaseError(f"更新できない列です: {sorted(invalid)}")

        def _fn(conn: sqlite3.Connection) -> sqlite3.Row:
            row = conn.execute(
                "SELECT * FROM charge_transactions WHERE id=?", (tx_id,)
            ).fetchone()
            if row is None:
                raise DatabaseError(f"取引が見つかりません: {tx_id}")
            current = row["status"]
            if current == new_status and not columns and error_code is None:
                return row
            if expected is not None and current not in tuple(expected):
                raise IllegalStateTransition(
                    f"{tx_id}: 現在状態 {current} は想定 {tuple(expected)} と一致しません"
                )
            if new_status != current and new_status not in config.ALLOWED_TRANSITIONS.get(current, ()):
                raise IllegalStateTransition(f"{tx_id}: {current} → {new_status} は許可されていません")
            sets = ["status=?", "updated_at=?"]
            params: list[Any] = [new_status, utils.now_ts()]
            # エラー列は「明示指定された場合」または「失敗系へ遷移した場合」のみ書き換える。
            # (実績メッセージIDの保存など、同一状態での列更新でエラー情報を消さない)
            if error_code is not None or (
                new_status != current
                and new_status in (config.TxStatus.FAILED, config.TxStatus.MANUAL_REVIEW)
            ):
                sets.append("error_code=?")
                params.append(error_code)
                sets.append("error_message=?")
                params.append(
                    utils.sanitize_for_log(error_message, limit=900) if error_message else None
                )
            for name, value in columns.items():
                sets.append(f"{name}=?")
                params.append(value)
            params.append(tx_id)
            cur = conn.execute(
                f"UPDATE charge_transactions SET {', '.join(sets)} WHERE id=?", params
            )
            if cur.rowcount != 1:
                raise DatabaseError(f"取引の更新に失敗しました: {tx_id}")
            if new_status in config.TERMINAL_STATUSES:
                conn.execute("DELETE FROM charge_queue WHERE transaction_id=?", (tx_id,))
            return conn.execute(
                "SELECT * FROM charge_transactions WHERE id=?", (tx_id,)
            ).fetchone()

        return await self.run(_fn, write=True)

    async def attach_link(
        self, tx_id: str, *, link_hash_value: str, link_uuid: str, received_amount: int
    ) -> sqlite3.Row:
        """検証済みリンク情報を保存して QUEUED にし、同時にキューへ登録する。

        ``link_hash`` / ``link_uuid`` の UNIQUE 制約により、同じリンクが二重に
        受取対象となることを DB レベルで防ぐ。完全なURLは保存しない。
        """
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> sqlite3.Row:
            row = conn.execute(
                "SELECT status FROM charge_transactions WHERE id=?", (tx_id,)
            ).fetchone()
            if row is None:
                raise DatabaseError(f"取引が見つかりません: {tx_id}")
            current = row["status"]
            if config.TxStatus.QUEUED not in config.ALLOWED_TRANSITIONS.get(current, ()):
                raise IllegalStateTransition(f"{tx_id}: {current} → QUEUED は許可されていません")
            conn.execute(
                "UPDATE charge_transactions SET status=?, link_hash=?, link_uuid=?, "
                "received_amount=?, updated_at=? WHERE id=?",
                (config.TxStatus.QUEUED, link_hash_value, link_uuid, received_amount, now, tx_id),
            )
            conn.execute(
                "INSERT INTO charge_queue(transaction_id, status, attempts, next_attempt_at, "
                "enqueued_at, updated_at) VALUES(?,?,?,?,?,?)",
                (tx_id, "QUEUED", 0, now, now, now),
            )
            return conn.execute(
                "SELECT * FROM charge_transactions WHERE id=?", (tx_id,)
            ).fetchone()

        return await self.run(_fn, write=True)

    async def find_transaction_by_link_hash(self, link_hash_value: str) -> sqlite3.Row | None:
        return await self.fetchone(
            "SELECT id, guild_id, user_id, status FROM charge_transactions WHERE link_hash=?",
            (link_hash_value,),
        )

    async def find_transaction_by_link_uuid(self, link_uuid: str) -> sqlite3.Row | None:
        return await self.fetchone(
            "SELECT id, guild_id, user_id, status FROM charge_transactions WHERE link_uuid=?",
            (link_uuid,),
        )

    async def list_transactions_by_status(
        self, statuses: Sequence[str], *, limit: int = 200
    ) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in statuses)
        return await self.fetchall(
            f"SELECT * FROM charge_transactions WHERE status IN ({placeholders}) "
            f"ORDER BY created_at ASC LIMIT ?",
            (*statuses, limit),
        )

    async def list_user_transactions(
        self, guild_id: int, user_id: int, *, offset: int = 0, limit: int = 5
    ) -> tuple[list[sqlite3.Row], int]:
        def _fn(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM charge_transactions WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM charge_transactions WHERE guild_id=? AND user_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (guild_id, user_id, limit, offset),
            ).fetchall()
            return rows, int(total)

        return await self.run(_fn)

    async def search_transactions(
        self,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
        tx_id: str | None = None,
        status: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        amount_min: int | None = None,
        amount_max: int | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> tuple[list[sqlite3.Row], int]:
        """管理者向け取引検索 (条件はすべてプレースホルダで束縛)。"""
        clauses: list[str] = []
        params: list[Any] = []
        if guild_id is not None:
            clauses.append("guild_id=?")
            params.append(guild_id)
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if tx_id:
            clauses.append("id=?")
            params.append(tx_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if date_from is not None:
            clauses.append("created_at>=?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("created_at<?")
            params.append(date_to)
        if amount_min is not None:
            clauses.append("requested_amount>=?")
            params.append(amount_min)
        if amount_max is not None:
            clauses.append("requested_amount<=?")
            params.append(amount_max)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        def _fn(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM charge_transactions {where}", tuple(params)
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM charge_transactions {where} "
                f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return rows, int(total)

        return await self.run(_fn)

    async def sum_daily_charge(self, guild_id: int, user_id: int | None, since: int) -> int:
        """日次上限判定用の合計額 (失敗・キャンセル・期限切れは除外)。"""
        placeholders = ",".join("?" for _ in config.DAILY_LIMIT_STATUSES)
        sql = (
            "SELECT COALESCE(SUM(COALESCE(received_amount, requested_amount)),0) AS s "
            f"FROM charge_transactions WHERE guild_id=? AND created_at>=? AND status IN ({placeholders})"
        )
        params: list[Any] = [guild_id, since, *config.DAILY_LIMIT_STATUSES]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        row = await self.fetchone(sql, params)
        return int(row["s"]) if row else 0

    async def expire_stale_transactions(self, now: int | None = None) -> list[sqlite3.Row]:
        """期限切れの「リンク待ち」Transaction を EXPIRED にする。"""
        now = now or utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            rows = conn.execute(
                "SELECT * FROM charge_transactions WHERE status IN (?,?) "
                "AND expires_at IS NOT NULL AND expires_at < ?",
                (config.TxStatus.CREATED, config.TxStatus.WAITING_LINK, now),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE charge_transactions SET status=?, error_code=?, error_message=?, "
                    "updated_at=? WHERE id=? AND status IN (?,?)",
                    (
                        config.TxStatus.EXPIRED, config.ErrorCode.TRANSACTION_EXPIRED,
                        "送金リンクの入力期限を超過しました", now, row["id"],
                        config.TxStatus.CREATED, config.TxStatus.WAITING_LINK,
                    ),
                )
                conn.execute("DELETE FROM charge_queue WHERE transaction_id=?", (row["id"],))
            return rows

        return await self.run(_fn, write=True)

    async def expire_abandoned_validating(self, threshold: int) -> list[sqlite3.Row]:
        """検証途中で放置された VALIDATING 行を期限切れにする (Bot 異常終了時の回収)。"""
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            rows = conn.execute(
                "SELECT * FROM charge_transactions WHERE status=? AND updated_at < ?",
                (config.TxStatus.VALIDATING, threshold),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE charge_transactions SET status=?, error_code=?, error_message=?, "
                    "updated_at=? WHERE id=? AND status=?",
                    (
                        config.TxStatus.EXPIRED, config.ErrorCode.TRANSACTION_EXPIRED,
                        "リンク検証が完了しないまま期限切れになりました", now, row["id"],
                        config.TxStatus.VALIDATING,
                    ),
                )
            return rows

        return await self.run(_fn, write=True)

    async def find_stuck_transactions(self, threshold: int) -> list[sqlite3.Row]:
        """一定時間以上 PROCESSING / RECEIVED / CREDITING のまま残った Transaction。"""
        return await self.fetchall(
            "SELECT * FROM charge_transactions WHERE status IN (?,?,?) AND updated_at < ? "
            "ORDER BY updated_at ASC",
            (config.TxStatus.PROCESSING, config.TxStatus.RECEIVED,
             config.TxStatus.CREDITING, threshold),
        )

    # ------------------------------------------------------------------
    # 残高更新 (冪等・単一トランザクション)
    # ------------------------------------------------------------------
    async def create_proxy_transaction(
        self,
        *,
        guild_id: int,
        user_id: int,
        requested_amount: int,
        received_amount: int,
        charge_rate: Decimal,
    ) -> str:
        """管理者代理実績用の Transaction を RECEIVED 状態で作成する。

        通常のチャージと同じ冪等な残高付与経路 (``credit_transaction``) を使うため、
        DB 上では ``source='ADMIN_PROXY'`` で区別する。
        """
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> str:
            for _ in range(8):
                tx_id = utils.new_transaction_id()
                try:
                    conn.execute(
                        "INSERT INTO charge_transactions("
                        "id, guild_id, user_id, requested_amount, received_amount, charge_rate, "
                        "status, source, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            tx_id, guild_id, user_id, requested_amount, received_amount,
                            utils.rate_to_db(charge_rate), config.TxStatus.RECEIVED,
                            config.TxSource.ADMIN_PROXY, now, now,
                        ),
                    )
                    return tx_id
                except sqlite3.IntegrityError:
                    continue
            raise DatabaseError("取引IDの生成に失敗しました")

        return await self.run(_fn, write=True)

    async def credit_transaction(
        self,
        tx_id: str,
        credited_amount: int,
        *,
        change_type: str = config.BalanceChangeType.CHARGE,
        reason: str = "チャージ完了",
        operator_id: int | None = None,
    ) -> dict[str, Any]:
        """チャージ完了処理を単一トランザクションで実行する。

        1. 取引の状態確認 (RECEIVED / CREDITING / MANUAL_REVIEW のみ)
        2. 現在残高取得 → balance_before
        3. credited_amount 加算 → balance_after
        4. balance_history 保存
        5. charge_transactions を COMPLETED
        6. キューから削除
        7. commit

        同じ Transaction ID では一度しか残高が増えない (冪等)。
        """
        if credited_amount < 0:
            raise DatabaseError("付与額が負数です")
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM charge_transactions WHERE id=?", (tx_id,)
            ).fetchone()
            if row is None:
                raise DatabaseError(f"取引が見つかりません: {tx_id}")
            guild_id = int(row["guild_id"])
            user_id = int(row["user_id"])

            existing = conn.execute(
                "SELECT id, balance_before, balance_after, change_amount FROM balance_history "
                "WHERE transaction_id=? AND type=?",
                (tx_id, change_type),
            ).fetchone()
            if existing is not None:
                # 既に付与済み → 状態だけ COMPLETED へ整合させ、残高は触らない
                if row["status"] != config.TxStatus.COMPLETED:
                    conn.execute(
                        "UPDATE charge_transactions SET status=?, completed_at=COALESCE(completed_at,?), "
                        "updated_at=? WHERE id=?",
                        (config.TxStatus.COMPLETED, now, now, tx_id),
                    )
                    conn.execute("DELETE FROM charge_queue WHERE transaction_id=?", (tx_id,))
                return {
                    "already_credited": True,
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "credited_amount": int(existing["change_amount"]),
                    "balance_before": int(existing["balance_before"]),
                    "balance_after": int(existing["balance_after"]),
                }

            if row["status"] not in (
                config.TxStatus.RECEIVED, config.TxStatus.CREDITING, config.TxStatus.MANUAL_REVIEW
            ):
                raise IllegalStateTransition(
                    f"{tx_id}: 状態 {row['status']} では残高付与できません"
                )

            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            before = int(bal_row["balance"]) if bal_row else 0
            after = before + int(credited_amount)

            conn.execute(
                "INSERT INTO users(guild_id, user_id, created_at, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO NOTHING",
                (guild_id, user_id, now, now),
            )
            conn.execute(
                "INSERT INTO balances(guild_id, user_id, balance, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET balance=excluded.balance, "
                "updated_at=excluded.updated_at",
                (guild_id, user_id, after, now),
            )
            conn.execute(
                "INSERT INTO balance_history(guild_id, user_id, change_amount, balance_before, "
                "balance_after, type, transaction_id, operator_id, reason, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    guild_id, user_id, int(credited_amount), before, after,
                    change_type, tx_id, operator_id, utils.truncate(reason, 500), now,
                ),
            )
            cur = conn.execute(
                "UPDATE charge_transactions SET status=?, credited_amount=?, balance_before=?, "
                "balance_after=?, completed_at=?, updated_at=?, error_code=NULL, error_message=NULL "
                "WHERE id=? AND status IN (?,?,?)",
                (
                    config.TxStatus.COMPLETED, int(credited_amount), before, after, now, now,
                    tx_id, config.TxStatus.RECEIVED, config.TxStatus.CREDITING,
                    config.TxStatus.MANUAL_REVIEW,
                ),
            )
            if cur.rowcount != 1:
                raise IllegalStateTransition(f"{tx_id}: 残高付与中に状態が変化しました")
            conn.execute("DELETE FROM charge_queue WHERE transaction_id=?", (tx_id,))
            return {
                "already_credited": False,
                "guild_id": guild_id,
                "user_id": user_id,
                "credited_amount": int(credited_amount),
                "balance_before": before,
                "balance_after": after,
            }

        return await self.run(_fn, write=True)

    async def adjust_balance(
        self,
        *,
        guild_id: int,
        user_id: int,
        change_type: str,
        amount: int,
        operator_id: int,
        reason: str,
        transaction_id: str | None = None,
    ) -> dict[str, int]:
        """管理者操作による残高変更 (加算 / 減算 / 設定)。

        ``change_type``:
            ADMIN_ADD → amount を加算 / ADMIN_REMOVE → amount を減算 (0未満にはしない)
            ADMIN_SET → amount を設定値とする / PROXY_ACHIEVEMENT → amount を加算
        """
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> dict[str, int]:
            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            before = int(bal_row["balance"]) if bal_row else 0
            if change_type in (config.BalanceChangeType.ADMIN_ADD,
                               config.BalanceChangeType.PROXY_ACHIEVEMENT):
                after = before + int(amount)
            elif change_type == config.BalanceChangeType.ADMIN_REMOVE:
                after = max(0, before - int(amount))
            elif change_type == config.BalanceChangeType.ADMIN_SET:
                after = max(0, int(amount))
            else:
                raise DatabaseError(f"未対応の残高変更種別です: {change_type}")

            conn.execute(
                "INSERT INTO users(guild_id, user_id, created_at, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO NOTHING",
                (guild_id, user_id, now, now),
            )
            conn.execute(
                "INSERT INTO balances(guild_id, user_id, balance, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET balance=excluded.balance, "
                "updated_at=excluded.updated_at",
                (guild_id, user_id, after, now),
            )
            conn.execute(
                "INSERT INTO balance_history(guild_id, user_id, change_amount, balance_before, "
                "balance_after, type, transaction_id, operator_id, reason, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    guild_id, user_id, after - before, before, after, change_type,
                    transaction_id, operator_id, utils.truncate(reason, 500), now,
                ),
            )
            return {"balance_before": before, "balance_after": after, "change": after - before}

        return await self.run(_fn, write=True)

    async def list_balance_history(
        self, guild_id: int, user_id: int, *, offset: int = 0, limit: int = 10
    ) -> tuple[list[sqlite3.Row], int]:
        def _fn(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM balance_history WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM balance_history WHERE guild_id=? AND user_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (guild_id, user_id, limit, offset),
            ).fetchall()
            return rows, int(total)

        return await self.run(_fn)

    async def get_user_charge_summary(self, guild_id: int, user_id: int) -> dict[str, int]:
        """利用者向けの累計 (完了した取引のみ)。"""
        row = await self.fetchone(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(received_amount),0) AS sent, "
            "COALESCE(SUM(credited_amount),0) AS credited FROM charge_transactions "
            "WHERE guild_id=? AND user_id=? AND status=?",
            (guild_id, user_id, config.TxStatus.COMPLETED),
        )
        return {
            "count": int(row["cnt"]) if row else 0,
            "sent": int(row["sent"]) if row else 0,
            "credited": int(row["credited"]) if row else 0,
        }

    # ------------------------------------------------------------------
    # キュー
    # ------------------------------------------------------------------
    async def fetch_next_queue_item(self, now: int | None = None) -> sqlite3.Row | None:
        """処理可能な次のキュー項目を FIFO で取得する (期限切れ等はスキップ)。"""
        now = now or utils.now_ts()
        return await self.fetchone(
            "SELECT q.*, t.guild_id, t.user_id, t.status AS tx_status, t.requested_amount, "
            "t.received_amount, t.link_uuid, t.retry_count "
            "FROM charge_queue q JOIN charge_transactions t ON t.id = q.transaction_id "
            "WHERE q.status='QUEUED' AND q.next_attempt_at <= ? AND t.status = ? "
            "ORDER BY q.enqueued_at ASC, q.id ASC LIMIT 1",
            (now, config.TxStatus.QUEUED),
        )

    async def list_queue(self, *, limit: int = 25) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT q.*, t.guild_id, t.user_id, t.status AS tx_status, t.requested_amount, "
            "t.created_at AS tx_created_at FROM charge_queue q "
            "JOIN charge_transactions t ON t.id=q.transaction_id "
            "ORDER BY CASE WHEN t.status='PROCESSING' THEN 0 ELSE 1 END, q.enqueued_at ASC LIMIT ?",
            (limit,),
        )

    async def count_queue(self) -> dict[str, int]:
        rows = await self.fetchall(
            "SELECT t.status AS status, COUNT(*) AS c FROM charge_queue q "
            "JOIN charge_transactions t ON t.id=q.transaction_id GROUP BY t.status"
        )
        return {str(r["status"]): int(r["c"]) for r in rows}

    async def mark_queue_attempt(
        self, tx_id: str, *, attempts: int, next_attempt_at: int, last_error: str | None
    ) -> None:
        await self.execute(
            "UPDATE charge_queue SET attempts=?, next_attempt_at=?, last_error=?, updated_at=? "
            "WHERE transaction_id=?",
            (attempts, next_attempt_at,
             utils.sanitize_for_log(last_error, limit=500) if last_error else None,
             utils.now_ts(), tx_id),
        )

    async def remove_from_queue(self, tx_id: str) -> None:
        await self.execute("DELETE FROM charge_queue WHERE transaction_id=?", (tx_id,))

    async def requeue_transaction(self, tx_id: str, *, next_attempt_at: int) -> None:
        """キューに存在しない Transaction を再登録する (再起動復旧用)。"""
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO charge_queue(transaction_id, status, attempts, next_attempt_at, "
            "enqueued_at, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(transaction_id) DO UPDATE SET status='QUEUED', "
            "next_attempt_at=excluded.next_attempt_at, updated_at=excluded.updated_at",
            (tx_id, "QUEUED", 0, next_attempt_at, now, now),
        )

    async def cleanup_orphan_queue_items(self) -> int:
        """終了済み Transaction のキュー項目を削除する。"""
        placeholders = ",".join("?" for _ in config.TERMINAL_STATUSES)
        return await self.execute(
            "DELETE FROM charge_queue WHERE transaction_id IN ("
            f"SELECT id FROM charge_transactions WHERE status IN ({placeholders}))",
            tuple(config.TERMINAL_STATUSES),
        )

    # ------------------------------------------------------------------
    # パネル
    # ------------------------------------------------------------------
    async def add_panel(
        self, guild_id: int, channel_id: int, message_id: int, panel_type: str
    ) -> int:
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO panels(guild_id, channel_id, message_id, panel_type, active, "
                "created_at, updated_at) VALUES(?,?,?,?,1,?,?)",
                (guild_id, channel_id, message_id, panel_type, now, now),
            )
            return int(cur.lastrowid)

        return await self.run(_fn, write=True)

    async def list_panels(
        self, guild_id: int | None = None, *, active_only: bool = True
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if guild_id is not None:
            clauses.append("guild_id=?")
            params.append(guild_id)
        if active_only:
            clauses.append("active=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self.fetchall(
            f"SELECT * FROM panels {where} ORDER BY created_at DESC", params
        )

    async def deactivate_panel(self, *, message_id: int | None = None, panel_id: int | None = None) -> int:
        if message_id is not None:
            return await self.execute(
                "UPDATE panels SET active=0, updated_at=? WHERE message_id=?",
                (utils.now_ts(), message_id),
            )
        if panel_id is not None:
            return await self.execute(
                "UPDATE panels SET active=0, updated_at=? WHERE id=?", (utils.now_ts(), panel_id)
            )
        return 0

    async def find_panel_by_message(self, message_id: int) -> str | None:
        """メッセージIDが登録済みパネルかどうかを判定する。

        Returns:
            ``"CHARGE"`` / ``"RANKING"`` / ``None``。メッセージ削除イベントごとに
            書き込みを行わないよう、まず読み取りだけで判定するために使う。
        """
        row = await self.fetchone(
            "SELECT 'CHARGE' AS kind FROM panels WHERE message_id=? "
            "UNION ALL SELECT 'RANKING' AS kind FROM ranking_panels WHERE message_id=? LIMIT 1",
            (message_id, message_id),
        )
        return str(row["kind"]) if row else None

    async def add_ranking_panel(self, guild_id: int, channel_id: int, message_id: int) -> int:
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO ranking_panels(guild_id, channel_id, message_id, active, "
                "created_at, updated_at) VALUES(?,?,?,1,?,?)",
                (guild_id, channel_id, message_id, now, now),
            )
            return int(cur.lastrowid)

        return await self.run(_fn, write=True)

    async def list_ranking_panels(
        self, guild_id: int | None = None, *, active_only: bool = True
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if guild_id is not None:
            clauses.append("guild_id=?")
            params.append(guild_id)
        if active_only:
            clauses.append("active=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self.fetchall(
            f"SELECT * FROM ranking_panels {where} ORDER BY created_at DESC", params
        )

    async def deactivate_ranking_panel(
        self, *, message_id: int | None = None, panel_id: int | None = None
    ) -> int:
        if message_id is not None:
            return await self.execute(
                "UPDATE ranking_panels SET active=0, updated_at=? WHERE message_id=?",
                (utils.now_ts(), message_id),
            )
        if panel_id is not None:
            return await self.execute(
                "UPDATE ranking_panels SET active=0, updated_at=? WHERE id=?",
                (utils.now_ts(), panel_id),
            )
        return 0

    async def update_ranking_panel_state(
        self, message_id: int, *, signature: str
    ) -> None:
        now = utils.now_ts()
        await self.execute(
            "UPDATE ranking_panels SET last_signature=?, last_updated_at=?, updated_at=? "
            "WHERE message_id=?",
            (signature, now, now, message_id),
        )

    async def guilds_with_ranking_panels(self) -> list[int]:
        rows = await self.fetchall(
            "SELECT DISTINCT guild_id FROM ranking_panels WHERE active=1"
        )
        return [int(r["guild_id"]) for r in rows]

    # ------------------------------------------------------------------
    # 監査ログ
    # ------------------------------------------------------------------
    async def add_audit_log(
        self,
        *,
        actor_id: int,
        action: str,
        guild_id: int | None = None,
        target_user_id: int | None = None,
        detail: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> str:
        op_id = operation_id or utils.new_operation_id()
        await self.execute(
            "INSERT INTO admin_audit_logs(operation_id, guild_id, actor_id, action, "
            "target_user_id, detail, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                op_id, guild_id, actor_id, action, target_user_id,
                utils.safe_json_dumps(detail or {}), utils.now_ts(),
            ),
        )
        return op_id

    async def list_audit_logs(
        self,
        *,
        guild_id: int | None = None,
        action: str | None = None,
        actor_id: int | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> tuple[list[sqlite3.Row], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if guild_id is not None:
            clauses.append("guild_id=?")
            params.append(guild_id)
        if action:
            clauses.append("action=?")
            params.append(action)
        if actor_id is not None:
            clauses.append("actor_id=?")
            params.append(actor_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        def _fn(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM admin_audit_logs {where}", tuple(params)
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM admin_audit_logs {where} ORDER BY created_at DESC, id DESC "
                f"LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return rows, int(total)

        return await self.run(_fn)

    # ------------------------------------------------------------------
    # Kyash アカウント
    # ------------------------------------------------------------------
    async def get_kyash_account(self) -> KyashAccountRecord:
        row = await self.fetchone("SELECT * FROM kyash_account WHERE id=1")
        return KyashAccountRecord.from_row(row)

    async def save_kyash_account(
        self,
        *,
        email: str | None,
        client_uuid: str | None,
        installation_uuid: str | None,
        access_token_enc: str | None,
        status: str,
        username: str | None = None,
        wallet_uuid: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO kyash_account(id, email, client_uuid, installation_uuid, access_token_enc, "
            "status, username, wallet_uuid, last_checked_at, last_error, created_at, updated_at) "
            "VALUES(1,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET email=excluded.email, client_uuid=excluded.client_uuid, "
            "installation_uuid=excluded.installation_uuid, access_token_enc=excluded.access_token_enc, "
            "status=excluded.status, username=excluded.username, wallet_uuid=excluded.wallet_uuid, "
            "last_checked_at=excluded.last_checked_at, last_error=excluded.last_error, "
            "updated_at=excluded.updated_at",
            (
                email, client_uuid, installation_uuid, access_token_enc, status, username,
                wallet_uuid, now, last_error, now, now,
            ),
        )

    async def update_kyash_status(
        self,
        status: str,
        *,
        last_error: str | None = None,
        wallet_uuid: str | None = None,
        username: str | None = None,
    ) -> None:
        now = utils.now_ts()
        sets = ["status=?", "last_checked_at=?", "updated_at=?", "last_error=?"]
        params: list[Any] = [
            status, now, now, utils.sanitize_for_log(last_error, limit=500) if last_error else None,
        ]
        if wallet_uuid is not None:
            sets.append("wallet_uuid=?")
            params.append(wallet_uuid)
        if username is not None:
            sets.append("username=?")
            params.append(username)
        await self.execute(
            f"UPDATE kyash_account SET {', '.join(sets)} WHERE id=1", params
        )

    async def clear_kyash_account(self) -> None:
        await self.execute("DELETE FROM kyash_account WHERE id=1")

    # ------------------------------------------------------------------
    # 通知キュー
    # ------------------------------------------------------------------
    async def enqueue_notification(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        guild_id: int | None = None,
        user_id: int | None = None,
        channel_id: int | None = None,
        transaction_id: str | None = None,
        delay: int = 30,
    ) -> int:
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO notification_queue(kind, guild_id, user_id, channel_id, "
                "transaction_id, payload, attempts, next_attempt_at, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    kind, guild_id, user_id, channel_id, transaction_id,
                    utils.safe_json_dumps(payload, limit=3500), 0, now + delay, "PENDING", now, now,
                ),
            )
            return int(cur.lastrowid)

        return await self.run(_fn, write=True)

    async def fetch_due_notifications(self, *, limit: int = 20) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT * FROM notification_queue WHERE status='PENDING' AND next_attempt_at<=? "
            "ORDER BY id ASC LIMIT ?",
            (utils.now_ts(), limit),
        )

    async def finish_notification(self, notification_id: int, *, status: str, error: str | None = None) -> None:
        await self.execute(
            "UPDATE notification_queue SET status=?, last_error=?, updated_at=? WHERE id=?",
            (status, utils.sanitize_for_log(error, limit=400) if error else None,
             utils.now_ts(), notification_id),
        )

    async def retry_notification(
        self, notification_id: int, *, attempts: int, next_attempt_at: int, error: str | None
    ) -> None:
        await self.execute(
            "UPDATE notification_queue SET attempts=?, next_attempt_at=?, last_error=?, "
            "updated_at=? WHERE id=?",
            (attempts, next_attempt_at,
             utils.sanitize_for_log(error, limit=400) if error else None,
             utils.now_ts(), notification_id),
        )

    async def count_pending_notifications(self) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS c FROM notification_queue WHERE status='PENDING'"
        )
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # 統計
    # ------------------------------------------------------------------
    async def get_statistics(self, guild_id: int | None = None) -> dict[str, Any]:
        """統計値を DB から集計する。"""
        scope = "WHERE guild_id=?" if guild_id is not None else ""
        params: tuple[Any, ...] = (guild_id,) if guild_id is not None else ()
        day_start = utils.jst_day_start()
        month_start = utils.jst_month_start()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            def one(sql: str, extra: tuple[Any, ...] = ()) -> sqlite3.Row:
                return conn.execute(sql, params + extra).fetchone()

            totals = one(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS success, "
                "SUM(CASE WHEN status IN ('FAILED','EXPIRED','CANCELLED') THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(received_amount,0) ELSE 0 END) AS sent, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(credited_amount,0) ELSE 0 END) AS credited, "
                "SUM(CASE WHEN status IN ('QUEUED','PROCESSING','RECEIVED','CREDITING') THEN 1 ELSE 0 END) AS in_progress, "
                "SUM(CASE WHEN status='MANUAL_REVIEW' THEN 1 ELSE 0 END) AS manual_review "
                f"FROM charge_transactions {scope}"
            )
            today = one(
                "SELECT COUNT(*) AS cnt, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(received_amount,0) ELSE 0 END) AS sent, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(credited_amount,0) ELSE 0 END) AS credited "
                f"FROM charge_transactions {scope} "
                f"{'AND' if scope else 'WHERE'} created_at>=?",
                (day_start,),
            )
            month = one(
                "SELECT COUNT(*) AS cnt, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(received_amount,0) ELSE 0 END) AS sent, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(credited_amount,0) ELSE 0 END) AS credited "
                f"FROM charge_transactions {scope} "
                f"{'AND' if scope else 'WHERE'} created_at>=?",
                (month_start,),
            )
            users = one(f"SELECT COUNT(*) AS c FROM balances {scope}")
            balance = one(f"SELECT COALESCE(SUM(balance),0) AS s FROM balances {scope}")
            return {
                "total": int(totals["total"] or 0),
                "success": int(totals["success"] or 0),
                "failed": int(totals["failed"] or 0),
                "sent": int(totals["sent"] or 0),
                "credited": int(totals["credited"] or 0),
                "in_progress": int(totals["in_progress"] or 0),
                "manual_review": int(totals["manual_review"] or 0),
                "today_count": int(today["cnt"] or 0),
                "today_sent": int(today["sent"] or 0),
                "today_credited": int(today["credited"] or 0),
                "month_count": int(month["cnt"] or 0),
                "month_sent": int(month["sent"] or 0),
                "month_credited": int(month["credited"] or 0),
                "users": int(users["c"] or 0),
                "total_balance": int(balance["s"] or 0),
            }

        return await self.run(_fn)

    # ------------------------------------------------------------------
    # 整合性チェック / メンテナンス
    # ------------------------------------------------------------------
    async def integrity_check(self) -> dict[str, Any]:
        """DB の整合性と業務的な矛盾を検出する (自動修復はしない)。"""

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            pragma = conn.execute("PRAGMA quick_check").fetchone()
            missing_history = conn.execute(
                # 付与種別 (CHARGE / PROXY_ACHIEVEMENT) を問わず履歴の有無を確認する
                "SELECT id FROM charge_transactions t WHERE t.status='COMPLETED' AND NOT EXISTS ("
                "SELECT 1 FROM balance_history h WHERE h.transaction_id=t.id) "
                "LIMIT 50"
            ).fetchall()
            orphan_queue = conn.execute(
                "SELECT q.transaction_id FROM charge_queue q JOIN charge_transactions t "
                "ON t.id=q.transaction_id WHERE t.status IN "
                "('COMPLETED','FAILED','CANCELLED','EXPIRED') LIMIT 50"
            ).fetchall()
            balance_mismatch = conn.execute(
                "SELECT b.guild_id, b.user_id, b.balance, "
                "COALESCE((SELECT SUM(h.change_amount) FROM balance_history h "
                "WHERE h.guild_id=b.guild_id AND h.user_id=b.user_id),0) AS history_sum "
                "FROM balances b WHERE b.balance <> COALESCE((SELECT SUM(h.change_amount) "
                "FROM balance_history h WHERE h.guild_id=b.guild_id AND h.user_id=b.user_id),0) "
                "LIMIT 50"
            ).fetchall()
            negative = conn.execute(
                "SELECT guild_id, user_id, balance FROM balances WHERE balance < 0 LIMIT 50"
            ).fetchall()
            return {
                "pragma": pragma[0] if pragma else "unknown",
                "completed_without_history": [r["id"] for r in missing_history],
                "orphan_queue": [r["transaction_id"] for r in orphan_queue],
                "balance_mismatch": [
                    {
                        "guild_id": int(r["guild_id"]),
                        "user_id": int(r["user_id"]),
                        "balance": int(r["balance"]),
                        "history_sum": int(r["history_sum"]),
                    }
                    for r in balance_mismatch
                ],
                "negative_balance": [
                    {"guild_id": int(r["guild_id"]), "user_id": int(r["user_id"]),
                     "balance": int(r["balance"])}
                    for r in negative
                ],
            }

        return await self.run(_fn)

    async def backup(self, destination: Path) -> Path:
        """SQLite のオンラインバックアップを実行する。"""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        def _fn(conn: sqlite3.Connection) -> Path:
            target = sqlite3.connect(str(destination))
            try:
                with target:
                    conn.backup(target)
            finally:
                target.close()
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            return destination

        return await self.run(_fn)

    async def delete_guild_data(self, guild_id: int) -> dict[str, int]:
        """サーバーのデータを削除する (危険操作。呼び出し側で2段階確認する)。"""

        def _fn(conn: sqlite3.Connection) -> dict[str, int]:
            counts: dict[str, int] = {}
            cur = conn.execute(
                "DELETE FROM charge_queue WHERE transaction_id IN "
                "(SELECT id FROM charge_transactions WHERE guild_id=?)",
                (guild_id,),
            )
            counts["charge_queue"] = cur.rowcount
            for table in (
                "balance_history", "charge_transactions", "balances", "users",
                "panels", "ranking_panels", "notification_queue", "guild_settings",
                "admin_audit_logs",
            ):
                cur = conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
                counts[table] = cur.rowcount
            return counts

        result = await self.run(_fn, write=True)
        self.invalidate_settings_cache(guild_id)
        return result

    async def vacuum(self) -> None:
        await self.run(lambda c: c.execute("VACUUM"))


# ---------------------------------------------------------------------------
# 前方互換 (将来バージョンで列が追加された場合の自動補完)
# ---------------------------------------------------------------------------
_FORWARD_COMPAT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("guild_settings", "guild_daily_limit", "INTEGER NOT NULL DEFAULT 0"),
    ("guild_settings", "ranking_hide_absent", "INTEGER NOT NULL DEFAULT 0"),
    ("charge_transactions", "wallet_before", "INTEGER"),
    ("charge_transactions", "source", "TEXT NOT NULL DEFAULT 'AUTOMATIC'"),
    ("charge_transactions", "achievement_channel_id", "INTEGER"),
    ("charge_transactions", "achievement_message_id", "INTEGER"),
    ("ranking_panels", "last_signature", "TEXT"),
    ("ranking_panels", "last_updated_at", "INTEGER"),
    ("admin_audit_logs", "operation_id", "TEXT"),
)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """列が存在しなければ追加する (テーブル名・列名は定数のみ)。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return
    if not rows:
        return
    existing = {r["name"] for r in rows}
    if column in existing:
        return
    logger.info("マイグレーション: %s.%s を追加します", table, column)
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
