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


class ShopError(DatabaseError):
    """ショップ購入・返金が行えない (利用者向けエラーコードを持つ)。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


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
    max_balance: int = config.DEFAULT_MAX_BALANCE
    manual_review_allow_new: bool = False
    balance_log_channel_id: int | None = None
    balance_log_scope: str = config.DEFAULT_BALANCE_LOG_SCOPE
    summary_channel_id: int | None = None
    summary_enabled: bool = False
    shop_enabled: bool = True
    panel_title: str | None = None
    panel_description: str | None = None
    accent_color: int | None = None
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
            max_balance=row["max_balance"] or 0,
            manual_review_allow_new=bool(row["manual_review_allow_new"]),
            balance_log_channel_id=row["balance_log_channel_id"],
            balance_log_scope=row["balance_log_scope"] or config.DEFAULT_BALANCE_LOG_SCOPE,
            summary_channel_id=row["summary_channel_id"],
            summary_enabled=bool(row["summary_enabled"]),
            shop_enabled=bool(row["shop_enabled"]),
            panel_title=row["panel_title"],
            panel_description=row["panel_description"],
            accent_color=row["accent_color"],
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
    token_issued_at: int | None = None
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
            token_issued_at=row["token_issued_at"],
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
        note       TEXT,
        expires_at INTEGER
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
        max_balance            INTEGER NOT NULL DEFAULT 0,
        manual_review_allow_new INTEGER NOT NULL DEFAULT 0,
        balance_log_channel_id INTEGER,
        balance_log_scope      TEXT    NOT NULL DEFAULT 'MANUAL',
        summary_channel_id     INTEGER,
        summary_enabled        INTEGER NOT NULL DEFAULT 0,
        shop_enabled           INTEGER NOT NULL DEFAULT 1,
        panel_title            TEXT,
        panel_description      TEXT,
        accent_color           INTEGER,
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
        refunded_at            INTEGER,
        refund_operation_id    TEXT,
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
        undo_of        INTEGER REFERENCES balance_history(id),
        created_at     INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_guild_user ON balance_history(guild_id, user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_history_type ON balance_history(guild_id, type, created_at DESC)",
    # 同じ残高操作を二重に取り消せないようにする
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_history_undo ON balance_history(undo_of) WHERE undo_of IS NOT NULL",
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
        ranking_type    TEXT    NOT NULL DEFAULT 'BALANCE',
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
        token_issued_at   INTEGER,
        created_at        INTEGER NOT NULL DEFAULT 0,
        updated_at        INTEGER NOT NULL DEFAULT 0
    )
    """,
    # -- ロール別チャージ率 (VIP等) --
    """
    CREATE TABLE IF NOT EXISTS guild_role_rates (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    INTEGER NOT NULL,
        role_id     INTEGER NOT NULL,
        charge_rate TEXT    NOT NULL,
        priority    INTEGER NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL DEFAULT 0,
        updated_at  INTEGER NOT NULL DEFAULT 0,
        UNIQUE (guild_id, role_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_role_rates_guild ON guild_role_rates(guild_id, priority DESC)",
    # -- ショップ (内部残高でロールを購入) --
    """
    CREATE TABLE IF NOT EXISTS shop_items (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id       INTEGER NOT NULL,
        role_id        INTEGER NOT NULL,
        name           TEXT    NOT NULL,
        description    TEXT,
        price          INTEGER NOT NULL,
        duration_days  INTEGER NOT NULL DEFAULT 0,
        stock          INTEGER NOT NULL DEFAULT -1,
        purchase_limit INTEGER NOT NULL DEFAULT 0,
        sort_order     INTEGER NOT NULL DEFAULT 0,
        active         INTEGER NOT NULL DEFAULT 1,
        created_by     INTEGER,
        created_at     INTEGER NOT NULL DEFAULT 0,
        updated_at     INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shop_items_guild ON shop_items(guild_id, active, sort_order)",
    """
    CREATE TABLE IF NOT EXISTS shop_purchases (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        item_id       INTEGER NOT NULL REFERENCES shop_items(id),
        item_name     TEXT,
        role_id       INTEGER NOT NULL,
        price         INTEGER NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'PENDING',
        expires_at    INTEGER,
        refunded_at   INTEGER,
        refund_reason TEXT,
        operator_id   INTEGER,
        achievement_channel_id INTEGER,
        achievement_message_id INTEGER,
        created_at    INTEGER NOT NULL DEFAULT 0,
        updated_at    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_purchases_guild_user ON shop_purchases(guild_id, user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_expiry ON shop_purchases(status, expires_at)",
    # -- 招待キャンペーン --
    """
    CREATE TABLE IF NOT EXISTS invite_campaigns (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id             INTEGER NOT NULL,
        name                 TEXT    NOT NULL,
        status               TEXT    NOT NULL DEFAULT 'ACTIVE',
        inviter_reward       INTEGER NOT NULL,
        invited_reward       INTEGER NOT NULL DEFAULT 0,
        min_account_age_days INTEGER NOT NULL DEFAULT 7,
        daily_limit          INTEGER NOT NULL DEFAULT 5,
        total_limit          INTEGER NOT NULL DEFAULT 50,
        require_charge       INTEGER NOT NULL DEFAULT 1,
        require_days         INTEGER NOT NULL DEFAULT 0,
        require_review       INTEGER NOT NULL DEFAULT 0,
        starts_at            INTEGER,
        ends_at              INTEGER,
        created_by           INTEGER,
        created_at           INTEGER NOT NULL DEFAULT 0,
        updated_at           INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_campaign_guild ON invite_campaigns(guild_id, status)",
    """
    CREATE TABLE IF NOT EXISTS invite_codes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        code       TEXT    NOT NULL,
        url        TEXT,
        uses       INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        UNIQUE (guild_id, user_id),
        UNIQUE (code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invite_records (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id  INTEGER REFERENCES invite_campaigns(id),
        guild_id     INTEGER NOT NULL,
        inviter_id   INTEGER,
        invited_id   INTEGER NOT NULL,
        code         TEXT,
        status       TEXT    NOT NULL DEFAULT 'PENDING',
        reason       TEXT,
        note         TEXT,
        reward_inviter INTEGER NOT NULL DEFAULT 0,
        reward_invited INTEGER NOT NULL DEFAULT 0,
        joined_at    INTEGER NOT NULL DEFAULT 0,
        confirmed_at INTEGER,
        reviewed_by  INTEGER,
        created_at   INTEGER NOT NULL DEFAULT 0,
        updated_at   INTEGER NOT NULL DEFAULT 0,
        UNIQUE (guild_id, invited_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_invite_records_inviter ON invite_records(guild_id, inviter_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_invite_records_status ON invite_records(guild_id, status, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS invite_blacklist (
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        reason     TEXT,
        actor_id   INTEGER,
        created_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    # -- 参加履歴 (退出→再入場による報酬の周回を防ぐ) --
    """
    CREATE TABLE IF NOT EXISTS guild_member_history (
        guild_id        INTEGER NOT NULL,
        user_id         INTEGER NOT NULL,
        first_joined_at INTEGER NOT NULL DEFAULT 0,
        last_joined_at  INTEGER NOT NULL DEFAULT 0,
        join_count      INTEGER NOT NULL DEFAULT 0,
        leave_count     INTEGER NOT NULL DEFAULT 0,
        updated_at      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
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
            # 1) テーブルを作成する (既存 DB では何も起きない)
            tables = [st for st in _SCHEMA_STATEMENTS if "CREATE TABLE" in st]
            indexes = [st for st in _SCHEMA_STATEMENTS if "CREATE TABLE" not in st]
            for statement in tables:
                conn.execute(statement)
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key='schema_version'"
            ).fetchone()
            current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
            if current > config.SCHEMA_VERSION:
                raise DatabaseError(
                    f"DBのスキーマバージョン({current})がBot({config.SCHEMA_VERSION})より新しいため起動できません"
                )
            # 2) 旧バージョンの DB に不足している列を追加する。
            #    インデックスより先に実行しないと、新しい列を参照する
            #    インデックスの作成に失敗する。
            for table, column, ddl in _FORWARD_COMPAT_COLUMNS:
                _ensure_column(conn, table, column, ddl)
            # 3) インデックスを作成する
            for statement in indexes:
                conn.execute(statement)
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
        "max_balance", "manual_review_allow_new", "balance_log_channel_id",
        "balance_log_scope", "summary_channel_id", "summary_enabled", "shop_enabled",
        "panel_title", "panel_description", "accent_color",
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

    async def count_active_transactions(
        self, guild_id: int, user_id: int, *, exclude_manual_review: bool = False
    ) -> int:
        """アクティブな Transaction 件数。

        Args:
            exclude_manual_review: True の場合 MANUAL_REVIEW を除外する
                (管理者確認待ちで利用者が無期限にロックされるのを避ける設定用)。
        """
        statuses = tuple(
            st for st in config.ACTIVE_STATUSES
            if not (exclude_manual_review and st == config.TxStatus.MANUAL_REVIEW)
        )
        placeholders = ",".join("?" for _ in statuses)
        row = await self.fetchone(
            f"SELECT COUNT(*) AS c FROM charge_transactions WHERE guild_id=? AND user_id=? "
            f"AND status IN ({placeholders})",
            (guild_id, user_id, *statuses),
        )
        return int(row["c"]) if row else 0

    async def list_stale_manual_reviews(self, threshold: int) -> list[sqlite3.Row]:
        """一定時間解決されていない MANUAL_REVIEW を返す (再通知用)。"""
        return await self.fetchall(
            "SELECT * FROM charge_transactions WHERE status=? AND updated_at < ? "
            "ORDER BY updated_at ASC LIMIT 50",
            (config.TxStatus.MANUAL_REVIEW, threshold),
        )

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

    async def get_user_spend_total(self, guild_id: int, user_id: int) -> int:
        """その利用者が内部残高から使った累計 (ショップ購入など)。"""
        row = await self.fetchone(
            "SELECT COALESCE(SUM(-change_amount),0) AS s FROM balance_history "
            "WHERE guild_id=? AND user_id=? AND change_amount < 0",
            (guild_id, user_id),
        )
        return int(row["s"] or 0) if row else 0

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
            return _lastrowid(cur)

        return await self.run(_fn, write=True)

    async def list_panels(
        self,
        guild_id: int | None = None,
        *,
        active_only: bool = True,
        panel_type: str | None = None,
    ) -> list[sqlite3.Row]:
        """パネル一覧を返す。``panel_type`` で種類を絞り込む。"""
        clauses: list[str] = []
        params: list[Any] = []
        if guild_id is not None:
            clauses.append("guild_id=?")
            params.append(guild_id)
        if panel_type is not None:
            clauses.append("panel_type=?")
            params.append(panel_type)
        if active_only:
            clauses.append("active=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self.fetchall(
            f"SELECT * FROM panels {where} ORDER BY created_at DESC", params
        )

    async def list_panel_guilds(self, panel_type: str) -> list[int]:
        """指定種類のパネルを持つサーバーIDを返す。"""
        rows = await self.fetchall(
            "SELECT DISTINCT guild_id FROM panels WHERE panel_type=? AND active=1", (panel_type,)
        )
        return [int(r["guild_id"]) for r in rows]

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
            ``"CHARGE"`` / ``"SHOP"`` / ``"INVITE"`` / ``"ADMIN"`` / ``"RANKING"`` /
            ``None``。メッセージ削除イベントごとに
            書き込みを行わないよう、まず読み取りだけで判定するために使う。
        """
        row = await self.fetchone(
            "SELECT panel_type AS kind FROM panels WHERE message_id=? "
            "UNION ALL SELECT 'RANKING' AS kind FROM ranking_panels WHERE message_id=? LIMIT 1",
            (message_id, message_id),
        )
        return str(row["kind"]) if row else None

    async def add_ranking_panel(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        ranking_type: str = config.RankingType.BALANCE,
    ) -> int:
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO ranking_panels(guild_id, channel_id, message_id, active, "
                "ranking_type, created_at, updated_at) VALUES(?,?,?,1,?,?,?)",
                (guild_id, channel_id, message_id, ranking_type, now, now),
            )
            return _lastrowid(cur)

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
        token_issued_at: int | None = None,
    ) -> None:
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO kyash_account(id, email, client_uuid, installation_uuid, access_token_enc, "
            "status, username, wallet_uuid, last_checked_at, last_error, token_issued_at, "
            "created_at, updated_at) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET email=excluded.email, client_uuid=excluded.client_uuid, "
            "installation_uuid=excluded.installation_uuid, access_token_enc=excluded.access_token_enc, "
            "status=excluded.status, username=excluded.username, wallet_uuid=excluded.wallet_uuid, "
            "last_checked_at=excluded.last_checked_at, last_error=excluded.last_error, "
            "token_issued_at=excluded.token_issued_at, updated_at=excluded.updated_at",
            (
                email, client_uuid, installation_uuid, access_token_enc, status, username,
                wallet_uuid, now, last_error, token_issued_at or now, now, now,
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
            return _lastrowid(cur)

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

    # ==================================================================
    # 期限付きサーバー許可
    # ==================================================================
    async def set_guild_permission_expiry(self, guild_id: int, expires_at: int | None) -> None:
        """許可の有効期限を設定する (期限切れで自動的に DENIED へ)。"""
        await self.execute(
            "UPDATE allowed_guilds SET expires_at=? WHERE guild_id=?", (expires_at, guild_id)
        )
        self.invalidate_guild_cache(guild_id)

    async def expire_guild_permissions(self) -> list[int]:
        """期限を過ぎた許可を失効させ、対象サーバーIDを返す。"""
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> list[int]:
            rows = conn.execute(
                "SELECT guild_id FROM allowed_guilds WHERE status='ALLOWED' "
                "AND expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE allowed_guilds SET status='DENIED', note=? WHERE guild_id=?",
                    ("期限切れによる自動失効", int(row["guild_id"])),
                )
            return [int(r["guild_id"]) for r in rows]

        expired = await self.run(_fn, write=True)
        for guild_id in expired:
            self.invalidate_guild_cache(guild_id)
        return expired

    # ==================================================================
    # ロール別チャージ率
    # ==================================================================
    async def set_role_rate(
        self, guild_id: int, role_id: int, charge_rate: Decimal, priority: int
    ) -> None:
        """ロールに紐づくチャージ率を登録・更新する。"""
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO guild_role_rates(guild_id, role_id, charge_rate, priority, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET charge_rate=excluded.charge_rate, "
            "priority=excluded.priority, updated_at=excluded.updated_at",
            (guild_id, role_id, utils.rate_to_db(charge_rate), priority, now, now),
        )

    async def remove_role_rate(self, guild_id: int, role_id: int) -> int:
        return await self.execute(
            "DELETE FROM guild_role_rates WHERE guild_id=? AND role_id=?", (guild_id, role_id)
        )

    async def list_role_rates(self, guild_id: int) -> list[sqlite3.Row]:
        """優先度の高い順に返す (同一優先度ならチャージ率の高い順)。"""
        return await self.fetchall(
            "SELECT * FROM guild_role_rates WHERE guild_id=? "
            "ORDER BY priority DESC, CAST(charge_rate AS REAL) DESC, role_id ASC",
            (guild_id,),
        )

    # ==================================================================
    # ショップ
    # ==================================================================
    async def add_shop_item(
        self,
        *,
        guild_id: int,
        role_id: int,
        name: str,
        price: int,
        duration_days: int = 0,
        stock: int = -1,
        purchase_limit: int = 0,
        description: str | None = None,
        sort_order: int = 0,
        created_by: int | None = None,
    ) -> int:
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO shop_items(guild_id, role_id, name, description, price, "
                "duration_days, stock, purchase_limit, sort_order, active, created_by, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (guild_id, role_id, name, description, price, duration_days, stock,
                 purchase_limit, sort_order, created_by, now, now),
            )
            return _lastrowid(cur)

        return await self.run(_fn, write=True)

    SHOP_UPDATABLE: frozenset[str] = frozenset({
        "role_id", "name", "description", "price", "duration_days", "stock",
        "purchase_limit", "sort_order", "active",
    })

    async def update_shop_item(self, item_id: int, guild_id: int, **values: Any) -> int:
        invalid = set(values) - self.SHOP_UPDATABLE
        if invalid:
            raise DatabaseError(f"更新できない商品項目です: {sorted(invalid)}")
        if not values:
            return 0
        assignments = ", ".join(f"{name}=?" for name in values)
        params = list(values.values()) + [utils.now_ts(), item_id, guild_id]
        return await self.execute(
            f"UPDATE shop_items SET {assignments}, updated_at=? WHERE id=? AND guild_id=?", params
        )

    async def get_shop_item(self, item_id: int, guild_id: int | None = None) -> sqlite3.Row | None:
        if guild_id is None:
            return await self.fetchone("SELECT * FROM shop_items WHERE id=?", (item_id,))
        return await self.fetchone(
            "SELECT * FROM shop_items WHERE id=? AND guild_id=?", (item_id, guild_id)
        )

    async def list_shop_items(
        self, guild_id: int, *, active_only: bool = True
    ) -> list[sqlite3.Row]:
        where = "WHERE guild_id=?" + (" AND active=1" if active_only else "")
        return await self.fetchall(
            f"SELECT * FROM shop_items {where} ORDER BY sort_order ASC, price ASC, id ASC",
            (guild_id,),
        )

    async def count_user_purchases(
        self, guild_id: int, user_id: int, item_id: int
    ) -> int:
        """購入上限の判定に使う件数 (返金・失敗は除外)。"""
        row = await self.fetchone(
            "SELECT COUNT(*) AS c FROM shop_purchases WHERE guild_id=? AND user_id=? "
            "AND item_id=? AND status IN (?,?,?)",
            (guild_id, user_id, item_id, config.PurchaseStatus.PENDING,
             config.PurchaseStatus.ACTIVE, config.PurchaseStatus.EXPIRED),
        )
        return int(row["c"]) if row else 0

    async def purchase_shop_item(
        self, *, guild_id: int, user_id: int, item_id: int
    ) -> dict[str, Any]:
        """内部残高で商品を購入する (在庫・上限・残高の確認まで単一トランザクション)。

        残高の引き落としと購入記録・在庫減算を同時にコミットするため、
        二重購入・二重課金が発生しない。ロール付与は購入確定後に行い、
        失敗した場合は ``refund_purchase`` で返金する。

        Raises:
            ShopError: 購入できない理由 (エラーコード付き)。
        """
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            item = conn.execute(
                "SELECT * FROM shop_items WHERE id=? AND guild_id=?", (item_id, guild_id)
            ).fetchone()
            if item is None or not item["active"]:
                raise ShopError(config.ErrorCode.SHOP_ITEM_UNAVAILABLE, "商品が見つかりません")
            stock = int(item["stock"])
            if stock == 0:
                raise ShopError(config.ErrorCode.SHOP_OUT_OF_STOCK, "在庫切れです")
            limit = int(item["purchase_limit"])
            if limit > 0:
                owned = conn.execute(
                    "SELECT COUNT(*) AS c FROM shop_purchases WHERE guild_id=? AND user_id=? "
                    "AND item_id=? AND status IN (?,?,?)",
                    (guild_id, user_id, item_id, config.PurchaseStatus.PENDING,
                     config.PurchaseStatus.ACTIVE, config.PurchaseStatus.EXPIRED),
                ).fetchone()
                if int(owned["c"]) >= limit:
                    raise ShopError(config.ErrorCode.SHOP_LIMIT_REACHED, "購入上限に達しています")
            price = int(item["price"])
            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            before = int(bal_row["balance"]) if bal_row else 0
            if before < price:
                raise ShopError(
                    config.ErrorCode.INSUFFICIENT_BALANCE,
                    f"残高不足 (所持 {before} / 必要 {price})",
                )
            after = before - price
            duration = int(item["duration_days"])
            expires_at = now + duration * 86400 if duration > 0 else None

            cur = conn.execute(
                "INSERT INTO shop_purchases(guild_id, user_id, item_id, item_name, role_id, "
                "price, status, expires_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (guild_id, user_id, item_id, item["name"], int(item["role_id"]), price,
                 config.PurchaseStatus.PENDING, expires_at, now, now),
            )
            purchase_id = _lastrowid(cur)
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
                (guild_id, user_id, -price, before, after, config.BalanceChangeType.SPEND,
                 f"SHOP-{purchase_id}", None,
                 utils.truncate(f"ショップ購入: {item['name']}", 500), now),
            )
            if stock > 0:
                conn.execute(
                    "UPDATE shop_items SET stock=stock-1, updated_at=? WHERE id=? AND stock>0",
                    (now, item_id),
                )
            return {
                "purchase_id": purchase_id,
                "item_id": item_id,
                "item_name": item["name"],
                "role_id": int(item["role_id"]),
                "price": price,
                "balance_before": before,
                "balance_after": after,
                "expires_at": expires_at,
                "duration_days": duration,
            }

        return await self.run(_fn, write=True)

    async def activate_purchase(self, purchase_id: int) -> None:
        """ロール付与に成功した購入を有効化する。"""
        await self.execute(
            "UPDATE shop_purchases SET status=?, updated_at=? WHERE id=? AND status=?",
            (config.PurchaseStatus.ACTIVE, utils.now_ts(), purchase_id,
             config.PurchaseStatus.PENDING),
        )

    async def refund_purchase(
        self,
        purchase_id: int,
        *,
        operator_id: int | None,
        reason: str,
        status: str = config.PurchaseStatus.REFUNDED,
    ) -> dict[str, Any]:
        """購入を返金する (残高返却・在庫復元・状態更新を単一トランザクションで)。"""
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM shop_purchases WHERE id=?", (purchase_id,)
            ).fetchone()
            if row is None:
                raise DatabaseError(f"購入記録が見つかりません: {purchase_id}")
            if row["status"] not in (config.PurchaseStatus.PENDING,
                                     config.PurchaseStatus.ACTIVE,
                                     config.PurchaseStatus.EXPIRED):
                raise ShopError(
                    config.ErrorCode.SHOP_ITEM_UNAVAILABLE,
                    f"この購入は返金できません (状態: {row['status']})",
                )
            guild_id = int(row["guild_id"])
            user_id = int(row["user_id"])
            price = int(row["price"])
            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            before = int(bal_row["balance"]) if bal_row else 0
            after = before + price
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
                (guild_id, user_id, price, before, after,
                 config.BalanceChangeType.SPEND_REFUND, f"SHOP-{purchase_id}", operator_id,
                 utils.truncate(reason, 500), now),
            )
            conn.execute(
                "UPDATE shop_purchases SET status=?, refunded_at=?, refund_reason=?, "
                "operator_id=?, updated_at=? WHERE id=?",
                (status, now, utils.truncate(reason, 500), operator_id, now, purchase_id),
            )
            # 在庫を戻す (無制限の場合は何もしない)
            conn.execute(
                "UPDATE shop_items SET stock=stock+1, updated_at=? WHERE id=? AND stock>=0",
                (now, int(row["item_id"])),
            )
            return {
                "guild_id": guild_id, "user_id": user_id, "role_id": int(row["role_id"]),
                "price": price, "balance_before": before, "balance_after": after,
                "item_name": row["item_name"],
            }

        return await self.run(_fn, write=True)

    async def set_purchase_achievement(
        self, purchase_id: int, *, channel_id: int, message_id: int
    ) -> None:
        """購入実績のメッセージIDを保存する (返金・期限切れ時に更新するため)。"""
        await self.execute(
            "UPDATE shop_purchases SET achievement_channel_id=?, achievement_message_id=?, "
            "updated_at=? WHERE id=?",
            (channel_id, message_id, utils.now_ts(), purchase_id),
        )

    async def get_purchase(self, purchase_id: int) -> sqlite3.Row | None:
        return await self.fetchone("SELECT * FROM shop_purchases WHERE id=?", (purchase_id,))

    async def list_user_purchases(
        self, guild_id: int, user_id: int, *, limit: int = 15
    ) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT * FROM shop_purchases WHERE guild_id=? AND user_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (guild_id, user_id, limit),
        )

    async def list_purchases(
        self, guild_id: int, *, status: str | None = None, offset: int = 0, limit: int = 10
    ) -> tuple[list[sqlite3.Row], int]:
        clauses = ["guild_id=?"]
        params: list[Any] = [guild_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses)

        def _fn(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM shop_purchases {where}", tuple(params)
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM shop_purchases {where} ORDER BY created_at DESC, id DESC "
                f"LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return rows, int(total)

        return await self.run(_fn)

    async def list_expired_purchases(self, now: int | None = None) -> list[sqlite3.Row]:
        """期限切れでロールを剥奪すべき購入を返す。"""
        return await self.fetchall(
            "SELECT * FROM shop_purchases WHERE status=? AND expires_at IS NOT NULL "
            "AND expires_at < ? ORDER BY expires_at ASC LIMIT 100",
            (config.PurchaseStatus.ACTIVE, now or utils.now_ts()),
        )

    async def mark_purchase_expired(self, purchase_id: int) -> None:
        await self.execute(
            "UPDATE shop_purchases SET status=?, updated_at=? WHERE id=? AND status=?",
            (config.PurchaseStatus.EXPIRED, utils.now_ts(), purchase_id,
             config.PurchaseStatus.ACTIVE),
        )

    async def list_active_purchases_for_role(
        self, guild_id: int, user_id: int, role_id: int
    ) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT * FROM shop_purchases WHERE guild_id=? AND user_id=? AND role_id=? "
            "AND status IN (?,?)",
            (guild_id, user_id, role_id, config.PurchaseStatus.PENDING,
             config.PurchaseStatus.ACTIVE),
        )

    # ==================================================================
    # 招待キャンペーン
    # ==================================================================
    async def create_campaign(
        self,
        *,
        guild_id: int,
        name: str,
        inviter_reward: int,
        invited_reward: int,
        min_account_age_days: int,
        daily_limit: int,
        total_limit: int,
        require_charge: bool,
        require_days: int,
        require_review: bool,
        starts_at: int | None,
        ends_at: int | None,
        created_by: int,
    ) -> int:
        """キャンペーンを作成する (既存のACTIVEは自動的に終了させる)。"""
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> int:
            conn.execute(
                "UPDATE invite_campaigns SET status=?, updated_at=? WHERE guild_id=? AND status=?",
                (config.CampaignStatus.ENDED, now, guild_id, config.CampaignStatus.ACTIVE),
            )
            cur = conn.execute(
                "INSERT INTO invite_campaigns(guild_id, name, status, inviter_reward, "
                "invited_reward, min_account_age_days, daily_limit, total_limit, require_charge, "
                "require_days, require_review, starts_at, ends_at, created_by, created_at, "
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (guild_id, name, config.CampaignStatus.ACTIVE, inviter_reward, invited_reward,
                 min_account_age_days, daily_limit, total_limit, 1 if require_charge else 0,
                 require_days, 1 if require_review else 0, starts_at, ends_at, created_by,
                 now, now),
            )
            return _lastrowid(cur)

        return await self.run(_fn, write=True)

    async def get_active_campaign(self, guild_id: int) -> sqlite3.Row | None:
        """開催中のキャンペーンを返す (期間外なら None)。"""
        now = utils.now_ts()
        return await self.fetchone(
            "SELECT * FROM invite_campaigns WHERE guild_id=? AND status=? "
            "AND (starts_at IS NULL OR starts_at <= ?) AND (ends_at IS NULL OR ends_at > ?) "
            "ORDER BY id DESC LIMIT 1",
            (guild_id, config.CampaignStatus.ACTIVE, now, now),
        )

    async def get_campaign(self, campaign_id: int) -> sqlite3.Row | None:
        return await self.fetchone("SELECT * FROM invite_campaigns WHERE id=?", (campaign_id,))

    async def list_campaigns(self, guild_id: int, *, limit: int = 10) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT * FROM invite_campaigns WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )

    CAMPAIGN_UPDATABLE: frozenset[str] = frozenset({
        "name", "inviter_reward", "invited_reward", "min_account_age_days", "daily_limit",
        "total_limit", "require_charge", "require_days", "require_review", "starts_at",
        "ends_at", "status",
    })

    async def update_campaign(self, campaign_id: int, **values: Any) -> int:
        invalid = set(values) - self.CAMPAIGN_UPDATABLE
        if invalid:
            raise DatabaseError(f"更新できないキャンペーン項目です: {sorted(invalid)}")
        if not values:
            return 0
        assignments = ", ".join(f"{name}=?" for name in values)
        params = list(values.values()) + [utils.now_ts(), campaign_id]
        return await self.execute(
            f"UPDATE invite_campaigns SET {assignments}, updated_at=? WHERE id=?", params
        )

    async def end_campaign(self, guild_id: int) -> int:
        return await self.execute(
            "UPDATE invite_campaigns SET status=?, updated_at=? WHERE guild_id=? AND status=?",
            (config.CampaignStatus.ENDED, utils.now_ts(), guild_id, config.CampaignStatus.ACTIVE),
        )

    # ------------------------------------------------------------------
    # 個人専用招待コード
    # ------------------------------------------------------------------
    async def save_invite_code(
        self, guild_id: int, user_id: int, code: str, url: str
    ) -> None:
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO invite_codes(guild_id, user_id, code, url, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET code=excluded.code, url=excluded.url, "
            "updated_at=excluded.updated_at",
            (guild_id, user_id, code, url, now, now),
        )

    async def get_invite_code_for_user(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        return await self.fetchone(
            "SELECT * FROM invite_codes WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )

    async def get_invite_code_owner(self, guild_id: int, code: str) -> int | None:
        row = await self.fetchone(
            "SELECT user_id FROM invite_codes WHERE guild_id=? AND code=?", (guild_id, code)
        )
        return int(row["user_id"]) if row else None

    async def delete_invite_code(self, guild_id: int, user_id: int) -> int:
        return await self.execute(
            "DELETE FROM invite_codes WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )

    async def increment_invite_code_uses(self, guild_id: int, code: str) -> None:
        await self.execute(
            "UPDATE invite_codes SET uses=uses+1, updated_at=? WHERE guild_id=? AND code=?",
            (utils.now_ts(), guild_id, code),
        )

    # ------------------------------------------------------------------
    # 招待記録
    # ------------------------------------------------------------------
    async def record_invite(
        self,
        *,
        guild_id: int,
        campaign_id: int | None,
        inviter_id: int | None,
        invited_id: int,
        code: str | None,
        status: str,
        reason: str | None,
    ) -> tuple[int, bool]:
        """招待を記録する。

        Returns:
            ``(record_id, created)``。同一サーバーで既に被招待者として記録されている
            場合は既存の record_id と ``created=False`` を返し、新しい記録は作らない
            (退出→再入場による報酬の周回を防ぐ)。
        """
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> tuple[int, bool]:
            existing = conn.execute(
                "SELECT id FROM invite_records WHERE guild_id=? AND invited_id=?",
                (guild_id, invited_id),
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), False
            cur = conn.execute(
                "INSERT INTO invite_records(campaign_id, guild_id, inviter_id, invited_id, code, "
                "status, reason, joined_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (campaign_id, guild_id, inviter_id, invited_id, code, status,
                 reason, now, now, now),
            )
            return _lastrowid(cur), True

        return await self.run(_fn, write=True)

    async def get_invite_record(self, record_id: int) -> sqlite3.Row | None:
        return await self.fetchone("SELECT * FROM invite_records WHERE id=?", (record_id,))

    async def get_invite_record_for_invited(
        self, guild_id: int, invited_id: int
    ) -> sqlite3.Row | None:
        return await self.fetchone(
            "SELECT * FROM invite_records WHERE guild_id=? AND invited_id=?",
            (guild_id, invited_id),
        )

    async def set_invite_status(
        self, record_id: int, status: str, *, reason: str | None = None,
        reviewed_by: int | None = None,
    ) -> int:
        return await self.execute(
            "UPDATE invite_records SET status=?, reason=COALESCE(?, reason), reviewed_by=?, "
            "updated_at=? WHERE id=?",
            (status, reason, reviewed_by, utils.now_ts(), record_id),
        )

    async def count_invites(
        self,
        guild_id: int,
        inviter_id: int,
        *,
        since: int | None = None,
        statuses: Sequence[str] = (config.InviteStatus.PENDING, config.InviteStatus.CONFIRMED),
    ) -> int:
        """招待者の招待件数 (上限判定用。保留も消費扱いにして水増しを防ぐ)。"""
        placeholders = ",".join("?" for _ in statuses)
        sql = (
            f"SELECT COUNT(*) AS c FROM invite_records WHERE guild_id=? AND inviter_id=? "
            f"AND status IN ({placeholders})"
        )
        params: list[Any] = [guild_id, inviter_id, *statuses]
        if since is not None:
            sql += " AND joined_at >= ?"
            params.append(since)
        row = await self.fetchone(sql, params)
        return int(row["c"]) if row else 0

    async def list_invite_records(
        self,
        guild_id: int,
        *,
        status: str | None = None,
        inviter_id: int | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> tuple[list[sqlite3.Row], int]:
        clauses = ["guild_id=?"]
        params: list[Any] = [guild_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        if inviter_id is not None:
            clauses.append("inviter_id=?")
            params.append(inviter_id)
        where = "WHERE " + " AND ".join(clauses)

        def _fn(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM invite_records {where}", tuple(params)
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM invite_records {where} ORDER BY created_at DESC, id DESC "
                f"LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return rows, int(total)

        return await self.run(_fn)

    async def get_invite_summary(self, guild_id: int, user_id: int) -> dict[str, int]:
        """招待者向けの集計 (確定 / 保留 / 要確認 / 無効 / 獲得報酬)。"""
        rows = await self.fetchall(
            "SELECT status, COUNT(*) AS c, COALESCE(SUM(reward_inviter),0) AS reward "
            "FROM invite_records WHERE guild_id=? AND inviter_id=? GROUP BY status",
            (guild_id, user_id),
        )
        result = {"confirmed": 0, "pending": 0, "hold": 0, "rejected": 0, "reward": 0}
        mapping = {
            config.InviteStatus.CONFIRMED: "confirmed",
            config.InviteStatus.PENDING: "pending",
            config.InviteStatus.HOLD: "hold",
            config.InviteStatus.REJECTED: "rejected",
        }
        for row in rows:
            key = mapping.get(str(row["status"]))
            if key:
                result[key] = int(row["c"])
            result["reward"] += int(row["reward"] or 0)
        return result

    async def confirm_invite_and_reward(self, record_id: int) -> dict[str, Any]:
        """招待を確定し、招待者・被招待者へ報酬を付与する (冪等)。

        報酬付与は ``balance_history`` の UNIQUE 制約 (transaction_id, type) で
        二重付与を防ぐ。すでに確定済みの場合は残高を変更しない。
        """
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            record = conn.execute(
                "SELECT * FROM invite_records WHERE id=?", (record_id,)
            ).fetchone()
            if record is None:
                raise DatabaseError(f"招待記録が見つかりません: {record_id}")
            if record["status"] == config.InviteStatus.CONFIRMED:
                return {
                    "already_confirmed": True,
                    "guild_id": int(record["guild_id"]),
                    "inviter_id": record["inviter_id"],
                    "invited_id": int(record["invited_id"]),
                    "reward_inviter": int(record["reward_inviter"] or 0),
                    "reward_invited": int(record["reward_invited"] or 0),
                }
            if record["status"] not in (config.InviteStatus.PENDING, config.InviteStatus.HOLD):
                raise DatabaseError(
                    f"確定できない状態です: {record['status']}"
                )
            campaign = conn.execute(
                "SELECT * FROM invite_campaigns WHERE id=?", (record["campaign_id"],)
            ).fetchone()
            if campaign is None:
                raise DatabaseError("キャンペーンが見つかりません")
            guild_id = int(record["guild_id"])
            inviter_id = record["inviter_id"]
            invited_id = int(record["invited_id"])
            inviter_reward = int(campaign["inviter_reward"] or 0)
            invited_reward = int(campaign["invited_reward"] or 0)

            granted: dict[str, int] = {"inviter": 0, "invited": 0}
            targets = []
            if inviter_id and inviter_reward > 0:
                targets.append(("inviter", int(inviter_id), inviter_reward, f"INV-{record_id}-I"))
            if invited_reward > 0:
                targets.append(("invited", invited_id, invited_reward, f"INV-{record_id}-V"))

            for key, user_id, amount, tx_key in targets:
                dup = conn.execute(
                    "SELECT id FROM balance_history WHERE transaction_id=? AND type=?",
                    (tx_key, config.BalanceChangeType.INVITE_REWARD),
                ).fetchone()
                if dup is not None:
                    continue  # 既に付与済み
                bal_row = conn.execute(
                    "SELECT balance FROM balances WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()
                before = int(bal_row["balance"]) if bal_row else 0
                after = before + amount
                conn.execute(
                    "INSERT INTO users(guild_id, user_id, created_at, updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(guild_id, user_id) DO NOTHING",
                    (guild_id, user_id, now, now),
                )
                conn.execute(
                    "INSERT INTO balances(guild_id, user_id, balance, updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                    "balance=excluded.balance, updated_at=excluded.updated_at",
                    (guild_id, user_id, after, now),
                )
                conn.execute(
                    "INSERT INTO balance_history(guild_id, user_id, change_amount, "
                    "balance_before, balance_after, type, transaction_id, operator_id, reason, "
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (guild_id, user_id, amount, before, after,
                     config.BalanceChangeType.INVITE_REWARD, tx_key, None,
                     utils.truncate(f"招待キャンペーン報酬 ({campaign['name']})", 500), now),
                )
                granted[key] = amount

            conn.execute(
                "UPDATE invite_records SET status=?, confirmed_at=?, reward_inviter=?, "
                "reward_invited=?, updated_at=? WHERE id=?",
                (config.InviteStatus.CONFIRMED, now, granted["inviter"], granted["invited"],
                 now, record_id),
            )
            return {
                "already_confirmed": False,
                "guild_id": guild_id,
                "inviter_id": int(inviter_id) if inviter_id else None,
                "invited_id": invited_id,
                "reward_inviter": granted["inviter"],
                "reward_invited": granted["invited"],
                "campaign_name": campaign["name"],
            }

        return await self.run(_fn, write=True)

    async def list_pending_invites_for_user(
        self, guild_id: int, invited_id: int
    ) -> sqlite3.Row | None:
        """チャージ完了時に確定判定する対象を取得する。"""
        return await self.fetchone(
            "SELECT * FROM invite_records WHERE guild_id=? AND invited_id=? AND status=?",
            (guild_id, invited_id, config.InviteStatus.PENDING),
        )

    async def list_invites_awaiting_days(self, guild_id: int | None = None) -> list[sqlite3.Row]:
        """滞在日数の条件を待っている招待を返す。"""
        sql = (
            "SELECT r.*, c.require_days, c.require_charge FROM invite_records r "
            "JOIN invite_campaigns c ON c.id = r.campaign_id "
            "WHERE r.status=? AND c.status=? AND c.require_charge=0 AND c.require_days > 0"
        )
        params: list[Any] = [config.InviteStatus.PENDING, config.CampaignStatus.ACTIVE]
        if guild_id is not None:
            sql += " AND r.guild_id=?"
            params.append(guild_id)
        return await self.fetchall(sql + " LIMIT 200", params)

    async def count_recent_invites_by_inviter(
        self, guild_id: int, inviter_id: int, since: int
    ) -> int:
        """短時間の大量参加を検知するための件数。"""
        row = await self.fetchone(
            "SELECT COUNT(*) AS c FROM invite_records WHERE guild_id=? AND inviter_id=? "
            "AND joined_at >= ?",
            (guild_id, inviter_id, since),
        )
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # 招待ブラックリスト
    # ------------------------------------------------------------------
    async def add_invite_blacklist(
        self, guild_id: int, user_id: int, reason: str, actor_id: int
    ) -> None:
        await self.execute(
            "INSERT INTO invite_blacklist(guild_id, user_id, reason, actor_id, created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "reason=excluded.reason, actor_id=excluded.actor_id",
            (guild_id, user_id, utils.truncate(reason, 300), actor_id, utils.now_ts()),
        )

    async def remove_invite_blacklist(self, guild_id: int, user_id: int) -> int:
        return await self.execute(
            "DELETE FROM invite_blacklist WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )

    async def is_invite_blacklisted(self, guild_id: int, user_id: int) -> bool:
        row = await self.fetchone(
            "SELECT 1 AS x FROM invite_blacklist WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        return row is not None

    async def list_invite_blacklist(self, guild_id: int) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT * FROM invite_blacklist WHERE guild_id=? ORDER BY created_at DESC LIMIT 50",
            (guild_id,),
        )

    # ==================================================================
    # 参加履歴 (退出→再入場による報酬の周回を防ぐ)
    # ==================================================================
    async def record_member_join(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """参加を記録し、再入場かどうかを返す。"""
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM guild_member_history WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO guild_member_history(guild_id, user_id, first_joined_at, "
                    "last_joined_at, join_count, leave_count, updated_at) VALUES(?,?,?,?,1,0,?)",
                    (guild_id, user_id, now, now, now),
                )
                return {"rejoin": False, "join_count": 1, "leave_count": 0,
                        "first_joined_at": now}
            conn.execute(
                "UPDATE guild_member_history SET last_joined_at=?, join_count=join_count+1, "
                "updated_at=? WHERE guild_id=? AND user_id=?",
                (now, now, guild_id, user_id),
            )
            return {
                "rejoin": True,
                "join_count": int(row["join_count"]) + 1,
                "leave_count": int(row["leave_count"]),
                "first_joined_at": int(row["first_joined_at"]),
            }

        return await self.run(_fn, write=True)

    async def record_member_leave(self, guild_id: int, user_id: int) -> None:
        now = utils.now_ts()
        await self.execute(
            "INSERT INTO guild_member_history(guild_id, user_id, first_joined_at, last_joined_at, "
            "join_count, leave_count, updated_at) VALUES(?,?,?,?,0,1,?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET leave_count=leave_count+1, "
            "updated_at=excluded.updated_at",
            (guild_id, user_id, now, now, now),
        )

    async def get_member_history(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        return await self.fetchone(
            "SELECT * FROM guild_member_history WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )

    # ==================================================================
    # 残高の詳細操作 (管理者向け)
    # ==================================================================
    async def move_balance(
        self,
        *,
        guild_id: int,
        from_user_id: int,
        to_user_id: int,
        amount: int,
        operator_id: int,
        reason: str,
    ) -> dict[str, Any]:
        """残高を利用者間で付け替える (誤付与の是正など)。

        出金側・入金側の残高更新と履歴2件を単一トランザクションで処理し、
        同一の操作IDで紐付ける。
        """
        if amount <= 0:
            raise DatabaseError("付け替え額は1以上で指定してください")
        if from_user_id == to_user_id:
            raise DatabaseError("同一ユーザー間では付け替えできません")
        now = utils.now_ts()
        move_id = utils.new_operation_id()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            src = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?",
                (guild_id, from_user_id),
            ).fetchone()
            src_before = int(src["balance"]) if src else 0
            if src_before < amount:
                raise DatabaseError(
                    f"出金側の残高が不足しています (所持 {src_before} / 必要 {amount})"
                )
            dst = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?",
                (guild_id, to_user_id),
            ).fetchone()
            dst_before = int(dst["balance"]) if dst else 0
            src_after = src_before - amount
            dst_after = dst_before + amount

            for user_id, before, after, change, change_type in (
                (from_user_id, src_before, src_after, -amount,
                 config.BalanceChangeType.ADMIN_MOVE_OUT),
                (to_user_id, dst_before, dst_after, amount,
                 config.BalanceChangeType.ADMIN_MOVE_IN),
            ):
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
                    "INSERT INTO balance_history(guild_id, user_id, change_amount, "
                    "balance_before, balance_after, type, transaction_id, operator_id, reason, "
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (guild_id, user_id, change, before, after, change_type, move_id,
                     operator_id, utils.truncate(reason, 500), now),
                )
            return {
                "operation_id": move_id,
                "amount": amount,
                "from_before": src_before, "from_after": src_after,
                "to_before": dst_before, "to_after": dst_after,
            }

        return await self.run(_fn, write=True)

    async def undo_balance_history(
        self, history_id: int, *, guild_id: int, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """残高変更履歴1件を逆仕訳で取り消す (元の履歴は書き換えない)。

        ``undo_of`` の UNIQUE 制約により、同じ履歴を二重に取り消せない。
        """
        now = utils.now_ts()
        blocked = {
            config.BalanceChangeType.UNDO,
            config.BalanceChangeType.REVERSAL,
            config.BalanceChangeType.RECONCILE,
        }

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM balance_history WHERE id=? AND guild_id=?", (history_id, guild_id)
            ).fetchone()
            if row is None:
                raise DatabaseError(f"履歴が見つかりません: {history_id}")
            if row["type"] in blocked:
                raise DatabaseError(
                    f"この種別は取り消せません: {row['type']}"
                )
            already = conn.execute(
                "SELECT id FROM balance_history WHERE undo_of=?", (history_id,)
            ).fetchone()
            if already is not None:
                raise AlreadyCredited(
                    f"この履歴は既に取り消されています (履歴ID {already['id']})"
                )
            user_id = int(row["user_id"])
            original_change = int(row["change_amount"])
            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            before = int(bal_row["balance"]) if bal_row else 0
            # 0未満にはしない (実際に適用された差分を履歴へ残す)
            after = max(0, before - original_change)
            change = after - before
            conn.execute(
                "INSERT INTO balances(guild_id, user_id, balance, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET balance=excluded.balance, "
                "updated_at=excluded.updated_at",
                (guild_id, user_id, after, now),
            )
            cur = conn.execute(
                "INSERT INTO balance_history(guild_id, user_id, change_amount, balance_before, "
                "balance_after, type, transaction_id, operator_id, reason, undo_of, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (guild_id, user_id, change, before, after, config.BalanceChangeType.UNDO,
                 row["transaction_id"], operator_id,
                 utils.truncate(f"操作取消 (履歴ID {history_id}): {reason}", 500),
                 history_id, now),
            )
            return {
                "undo_history_id": _lastrowid(cur),
                "user_id": user_id,
                "original_type": row["type"],
                "original_change": original_change,
                "applied_change": change,
                "balance_before": before,
                "balance_after": after,
            }

        return await self.run(_fn, write=True)

    async def audit_balance(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """残高と履歴合計を突合する (修復はしない)。"""

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            actual = int(bal_row["balance"]) if bal_row else 0
            sum_row = conn.execute(
                "SELECT COALESCE(SUM(change_amount),0) AS s, COUNT(*) AS c FROM balance_history "
                "WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            expected = int(sum_row["s"])
            breakdown = conn.execute(
                "SELECT type, COUNT(*) AS c, COALESCE(SUM(change_amount),0) AS total "
                "FROM balance_history WHERE guild_id=? AND user_id=? GROUP BY type "
                "ORDER BY total DESC",
                (guild_id, user_id),
            ).fetchall()
            return {
                "actual": actual,
                "expected": expected,
                "diff": actual - expected,
                "entries": int(sum_row["c"]),
                "breakdown": [
                    {"type": str(r["type"]), "count": int(r["c"]), "total": int(r["total"])}
                    for r in breakdown
                ],
            }

        return await self.run(_fn)

    async def repair_balance(
        self, *, guild_id: int, user_id: int, operator_id: int, reason: str, mode: str = "history"
    ) -> dict[str, Any]:
        """残高と履歴合計の不一致を修復する。

        Args:
            mode: ``"history"`` は履歴側に差分行 (RECONCILE) を追加して現在残高へ合わせる
                  (残高は変更しない)。``"balance"`` は残高を履歴合計へ合わせ、
                  経済的な変動を伴わない修復として記録する (change_amount=0)。
        """
        if mode not in ("history", "balance"):
            raise DatabaseError("mode は history または balance を指定してください")
        now = utils.now_ts()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            actual = int(bal_row["balance"]) if bal_row else 0
            sum_row = conn.execute(
                "SELECT COALESCE(SUM(change_amount),0) AS s FROM balance_history "
                "WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            expected = int(sum_row["s"])
            diff = actual - expected
            if diff == 0:
                return {"repaired": False, "actual": actual, "expected": expected, "diff": 0}

            if mode == "history":
                change, before, after = diff, expected, actual
                note = f"履歴へ差分を追記して現在残高に整合 (差分 {diff})"
            else:
                conn.execute(
                    "INSERT INTO balances(guild_id, user_id, balance, updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                    "balance=excluded.balance, updated_at=excluded.updated_at",
                    (guild_id, user_id, expected, now),
                )
                # 残高を履歴合計へ戻す修復であり、経済的な増減は発生しない
                change, before, after = 0, actual, expected
                note = f"残高を履歴合計へ修復 ({actual} → {expected})"
            conn.execute(
                "INSERT INTO balance_history(guild_id, user_id, change_amount, balance_before, "
                "balance_after, type, transaction_id, operator_id, reason, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (guild_id, user_id, change, before, after, config.BalanceChangeType.RECONCILE,
                 None, operator_id, utils.truncate(f"{note}: {reason}", 500), now),
            )
            return {
                "repaired": True, "mode": mode, "actual": actual, "expected": expected,
                "diff": diff,
            }

        return await self.run(_fn, write=True)

    async def get_balance_history_entry(
        self, history_id: int, guild_id: int | None = None
    ) -> sqlite3.Row | None:
        if guild_id is None:
            return await self.fetchone("SELECT * FROM balance_history WHERE id=?", (history_id,))
        return await self.fetchone(
            "SELECT * FROM balance_history WHERE id=? AND guild_id=?", (history_id, guild_id)
        )

    async def list_balance_history_filtered(
        self,
        guild_id: int,
        *,
        user_id: int | None = None,
        types: Sequence[str] | None = None,
        operator_id: int | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> tuple[list[sqlite3.Row], int]:
        """残高台帳の検索 (種別・操作者・期間で絞り込み)。"""
        clauses = ["guild_id=?"]
        params: list[Any] = [guild_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"type IN ({placeholders})")
            params.extend(types)
        if operator_id is not None:
            clauses.append("operator_id=?")
            params.append(operator_id)
        if date_from is not None:
            clauses.append("created_at>=?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("created_at<?")
            params.append(date_to)
        where = "WHERE " + " AND ".join(clauses)

        def _fn(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM balance_history {where}", tuple(params)
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM balance_history {where} ORDER BY created_at DESC, id DESC "
                f"LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return rows, int(total)

        return await self.run(_fn)

    async def list_guild_balances(
        self, guild_id: int, *, include_zero: bool = True, limit: int = 5000
    ) -> list[sqlite3.Row]:
        """CSV 出力・分布分析用に残高一覧を取得する。"""
        where = "WHERE b.guild_id=?" + ("" if include_zero else " AND b.balance > 0")
        return await self.fetchall(
            "SELECT b.user_id, b.balance, b.updated_at, COALESCE(u.frozen,0) AS frozen "
            f"FROM balances b LEFT JOIN users u ON u.guild_id=b.guild_id AND u.user_id=b.user_id "
            f"{where} ORDER BY b.balance DESC, b.user_id ASC LIMIT ?",
            (guild_id, limit),
        )

    async def refund_transaction(
        self, tx_id: str, *, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """完了済みチャージを取り消し、付与した残高を逆仕訳で回収する。"""
        now = utils.now_ts()
        operation_id = utils.new_operation_id()

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM charge_transactions WHERE id=?", (tx_id,)
            ).fetchone()
            if row is None:
                raise DatabaseError(f"取引が見つかりません: {tx_id}")
            if row["status"] != config.TxStatus.COMPLETED:
                raise DatabaseError(
                    f"完了していない取引は取消できません (状態: {row['status']})"
                )
            if row["refunded_at"]:
                raise AlreadyCredited(f"この取引は既に取消済みです ({tx_id})")
            guild_id = int(row["guild_id"])
            user_id = int(row["user_id"])
            credited = int(row["credited_amount"] or 0)
            bal_row = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            before = int(bal_row["balance"]) if bal_row else 0
            after = max(0, before - credited)
            change = after - before
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
                (guild_id, user_id, change, before, after, config.BalanceChangeType.REVERSAL,
                 tx_id, operator_id, utils.truncate(f"チャージ取消: {reason}", 500), now),
            )
            conn.execute(
                "UPDATE charge_transactions SET refunded_at=?, refund_operation_id=?, "
                "updated_at=? WHERE id=?",
                (now, operation_id, now, tx_id),
            )
            return {
                "operation_id": operation_id, "guild_id": guild_id, "user_id": user_id,
                "credited_amount": credited, "applied_change": change,
                "balance_before": before, "balance_after": after,
            }

        return await self.run(_fn, write=True)

    # ==================================================================
    # ランキング (期間別 / 招待)
    # ==================================================================
    async def get_charge_ranking(
        self, guild_id: int, *, since: int, limit: int
    ) -> list[sqlite3.Row]:
        """期間内のチャージ獲得残高ランキング (取消済みは除外)。"""
        return await self.fetchall(
            "SELECT t.user_id, SUM(t.credited_amount) AS total FROM charge_transactions t "
            "LEFT JOIN users u ON u.guild_id=t.guild_id AND u.user_id=t.user_id "
            "WHERE t.guild_id=? AND t.status=? AND t.refunded_at IS NULL "
            "AND COALESCE(t.completed_at, t.updated_at) >= ? AND COALESCE(u.frozen,0)=0 "
            "GROUP BY t.user_id HAVING total > 0 ORDER BY total DESC, t.user_id ASC LIMIT ?",
            (guild_id, config.TxStatus.COMPLETED, since, max(1, int(limit))),
        )

    async def get_charge_rank(
        self, guild_id: int, user_id: int, *, since: int
    ) -> tuple[int | None, int, int]:
        """期間内チャージランキングでの (順位, 合計, 対象人数)。"""

        def _fn(conn: sqlite3.Connection) -> tuple[int | None, int, int]:
            rows = conn.execute(
                "SELECT t.user_id, SUM(t.credited_amount) AS total FROM charge_transactions t "
                "LEFT JOIN users u ON u.guild_id=t.guild_id AND u.user_id=t.user_id "
                "WHERE t.guild_id=? AND t.status=? AND t.refunded_at IS NULL "
                "AND COALESCE(t.completed_at, t.updated_at) >= ? AND COALESCE(u.frozen,0)=0 "
                "GROUP BY t.user_id HAVING total > 0",
                (guild_id, config.TxStatus.COMPLETED, since),
            ).fetchall()
            totals = {int(r["user_id"]): int(r["total"]) for r in rows}
            mine = totals.get(user_id)
            if mine is None:
                return None, 0, len(totals)
            higher = sum(
                1 for uid, total in totals.items()
                if total > mine or (total == mine and uid < user_id)
            )
            return higher + 1, mine, len(totals)

        return await self.run(_fn)

    async def get_invite_ranking(self, guild_id: int, *, limit: int) -> list[sqlite3.Row]:
        """確定した招待数のランキング。"""
        return await self.fetchall(
            "SELECT inviter_id AS user_id, COUNT(*) AS total FROM invite_records "
            "WHERE guild_id=? AND status=? AND inviter_id IS NOT NULL "
            "GROUP BY inviter_id ORDER BY total DESC, inviter_id ASC LIMIT ?",
            (guild_id, config.InviteStatus.CONFIRMED, max(1, int(limit))),
        )

    async def get_invite_rank(
        self, guild_id: int, user_id: int
    ) -> tuple[int | None, int, int]:
        def _fn(conn: sqlite3.Connection) -> tuple[int | None, int, int]:
            rows = conn.execute(
                "SELECT inviter_id, COUNT(*) AS total FROM invite_records "
                "WHERE guild_id=? AND status=? AND inviter_id IS NOT NULL GROUP BY inviter_id",
                (guild_id, config.InviteStatus.CONFIRMED),
            ).fetchall()
            totals = {int(r["inviter_id"]): int(r["total"]) for r in rows}
            mine = totals.get(user_id)
            if mine is None:
                return None, 0, len(totals)
            higher = sum(
                1 for uid, total in totals.items()
                if total > mine or (total == mine and uid < user_id)
            )
            return higher + 1, mine, len(totals)

        return await self.run(_fn)

    # ==================================================================
    # 日次サマリ / 失敗内訳
    # ==================================================================
    async def get_period_summary(
        self, guild_id: int, *, start: int, end: int
    ) -> dict[str, Any]:
        """期間内のチャージ実績を集計する (日次サマリ用)。"""

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            base = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS success, "
                "SUM(CASE WHEN status IN ('FAILED','EXPIRED','CANCELLED') THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN status='MANUAL_REVIEW' THEN 1 ELSE 0 END) AS review, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(received_amount,0) ELSE 0 END) AS received, "
                "SUM(CASE WHEN status='COMPLETED' THEN COALESCE(credited_amount,0) ELSE 0 END) AS credited, "
                "COUNT(DISTINCT user_id) AS users "
                "FROM charge_transactions WHERE guild_id=? AND created_at>=? AND created_at<?",
                (guild_id, start, end),
            ).fetchone()
            errors = conn.execute(
                "SELECT error_code, COUNT(*) AS c FROM charge_transactions "
                "WHERE guild_id=? AND created_at>=? AND created_at<? AND error_code IS NOT NULL "
                "GROUP BY error_code ORDER BY c DESC LIMIT 8",
                (guild_id, start, end),
            ).fetchall()
            top = conn.execute(
                "SELECT user_id, SUM(credited_amount) AS total FROM charge_transactions "
                "WHERE guild_id=? AND status='COMPLETED' AND refunded_at IS NULL "
                "AND created_at>=? AND created_at<? GROUP BY user_id "
                "ORDER BY total DESC, user_id ASC LIMIT 5",
                (guild_id, start, end),
            ).fetchall()
            spend = conn.execute(
                "SELECT COALESCE(SUM(-change_amount),0) AS s, COUNT(*) AS c FROM balance_history "
                "WHERE guild_id=? AND type=? AND created_at>=? AND created_at<?",
                (guild_id, config.BalanceChangeType.SPEND, start, end),
            ).fetchone()
            invites = conn.execute(
                "SELECT COUNT(*) AS c FROM invite_records WHERE guild_id=? AND status=? "
                "AND confirmed_at>=? AND confirmed_at<?",
                (guild_id, config.InviteStatus.CONFIRMED, start, end),
            ).fetchone()
            return {
                "total": int(base["total"] or 0),
                "success": int(base["success"] or 0),
                "failed": int(base["failed"] or 0),
                "review": int(base["review"] or 0),
                "received": int(base["received"] or 0),
                "credited": int(base["credited"] or 0),
                "users": int(base["users"] or 0),
                "errors": [(str(r["error_code"]), int(r["c"])) for r in errors],
                "top": [(int(r["user_id"]), int(r["total"])) for r in top],
                "spend_total": int(spend["s"] or 0),
                "spend_count": int(spend["c"] or 0),
                "invites_confirmed": int(invites["c"] or 0),
            }

        return await self.run(_fn)

    async def get_balance_distribution(self, guild_id: int) -> dict[str, Any]:
        """残高の分布 (管理者向け分析)。"""

        def _fn(conn: sqlite3.Connection) -> dict[str, Any]:
            rows = conn.execute(
                "SELECT balance FROM balances WHERE guild_id=? ORDER BY balance DESC",
                (guild_id,),
            ).fetchall()
            values = [int(r["balance"]) for r in rows]
            total = sum(values)
            count = len(values)
            zero = sum(1 for v in values if v == 0)
            median = 0
            if count:
                mid = count // 2
                median = values[mid] if count % 2 else (values[mid - 1] + values[mid]) // 2
            top_count = max(1, count // 10) if count else 0
            top_sum = sum(values[:top_count])
            issued = conn.execute(
                "SELECT COALESCE(SUM(change_amount),0) AS s FROM balance_history "
                "WHERE guild_id=? AND change_amount > 0",
                (guild_id,),
            ).fetchone()
            spent = conn.execute(
                "SELECT COALESCE(SUM(-change_amount),0) AS s FROM balance_history "
                "WHERE guild_id=? AND change_amount < 0",
                (guild_id,),
            ).fetchone()
            return {
                "count": count,
                "total": total,
                "zero": zero,
                "median": median,
                "max": values[0] if values else 0,
                "top10_share": (top_sum / total * 100) if total else 0.0,
                "issued": int(issued["s"] or 0),
                "spent": int(spent["s"] or 0),
            }

        return await self.run(_fn)

    async def get_global_overview(self) -> list[sqlite3.Row]:
        """全サーバー横断の状況 (Bot Owner 向け)。"""
        return await self.fetchall(
            "SELECT g.guild_id, "
            "(SELECT COUNT(*) FROM charge_transactions t WHERE t.guild_id=g.guild_id "
            " AND t.status='COMPLETED') AS charges, "
            "(SELECT COALESCE(SUM(t.received_amount),0) FROM charge_transactions t "
            " WHERE t.guild_id=g.guild_id AND t.status='COMPLETED') AS received, "
            "(SELECT COALESCE(SUM(b.balance),0) FROM balances b WHERE b.guild_id=g.guild_id) AS balance, "
            "(SELECT COUNT(*) FROM charge_transactions t WHERE t.guild_id=g.guild_id "
            " AND t.status='MANUAL_REVIEW') AS review, "
            "(SELECT COUNT(*) FROM charge_transactions t WHERE t.guild_id=g.guild_id "
            " AND t.status IN ('FAILED','EXPIRED')) AS failed, "
            "(SELECT MAX(t.created_at) FROM charge_transactions t WHERE t.guild_id=g.guild_id) AS last_activity, "
            "g.status, g.expires_at "
            "FROM allowed_guilds g ORDER BY received DESC LIMIT 50"
        )

    async def vacuum(self) -> None:
        await self.run(lambda c: c.execute("VACUUM"))


# ---------------------------------------------------------------------------
# 前方互換 (将来バージョンで列が追加された場合の自動補完)
# ---------------------------------------------------------------------------
_FORWARD_COMPAT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # v2 で追加
    ("guild_settings", "max_balance", "INTEGER NOT NULL DEFAULT 0"),
    ("guild_settings", "manual_review_allow_new", "INTEGER NOT NULL DEFAULT 0"),
    ("guild_settings", "balance_log_channel_id", "INTEGER"),
    ("guild_settings", "balance_log_scope", "TEXT NOT NULL DEFAULT 'MANUAL'"),
    ("guild_settings", "summary_channel_id", "INTEGER"),
    ("guild_settings", "summary_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("guild_settings", "shop_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("guild_settings", "panel_title", "TEXT"),
    ("guild_settings", "panel_description", "TEXT"),
    ("guild_settings", "accent_color", "INTEGER"),
    ("allowed_guilds", "expires_at", "INTEGER"),
    ("charge_transactions", "refunded_at", "INTEGER"),
    ("charge_transactions", "refund_operation_id", "TEXT"),
    ("balance_history", "undo_of", "INTEGER"),
    ("ranking_panels", "ranking_type", "TEXT NOT NULL DEFAULT 'BALANCE'"),
    ("kyash_account", "token_issued_at", "INTEGER"),
    ("shop_purchases", "achievement_channel_id", "INTEGER"),
    ("shop_purchases", "achievement_message_id", "INTEGER"),
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


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    """INSERT 直後の rowid を返す。

    ``sqlite3.Cursor.lastrowid`` は理論上 None を返し得るため、
    暗黙に ``int(None)`` で落ちるのではなく明示的なエラーにする。
    """
    rowid = cursor.lastrowid
    if rowid is None:
        raise DatabaseError("INSERT 後の rowid を取得できませんでした")
    return int(rowid)


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
