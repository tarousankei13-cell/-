"""Database layer for McDonald's Concierge Bot.

Write operations are serialized through asyncio.Lock.
Critical balance operations use BEGIN IMMEDIATE for atomicity.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiosqlite

from models import (
    DEFAULT_SETTINGS,
    DepositStatus,
    OrderStatus,
    TransactionType,
    can_transition,
)

logger = logging.getLogger("bot.db")
SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: str = "concierge.db"):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self._init_settings()
        logger.info("Database initialized: %s (schema v%d)", self.path, SCHEMA_VERSION)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def _create_tables(self) -> None:
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_before INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                operator_id INTEGER,
                order_id INTEGER,
                deposit_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                store_id TEXT NOT NULL DEFAULT '',
                store_name TEXT NOT NULL DEFAULT '',
                pickup_method TEXT NOT NULL DEFAULT '',
                total_amount INTEGER NOT NULL DEFAULT 0,
                user_amount INTEGER NOT NULL DEFAULT 0,
                subsidy_amount INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                receipt_number TEXT NOT NULL DEFAULT '',
                order_token TEXT NOT NULL DEFAULT '',
                order_group TEXT NOT NULL DEFAULT '',
                hex_data TEXT NOT NULL DEFAULT '',
                products_json TEXT NOT NULL DEFAULT '[]',
                error_info TEXT NOT NULL DEFAULT '',
                achievement_posted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_id INTEGER,
                reject_reason TEXT NOT NULL DEFAULT '',
                message_id INTEGER,
                channel_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                processed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status);
        """)
        cursor = await self._db.execute("SELECT version FROM schema_version LIMIT 1")
        if await cursor.fetchone() is None:
            await self._db.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )

    async def _init_settings(self) -> None:
        for key, default in DEFAULT_SETTINGS.items():
            await self._db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, default),
            )

    # ── Settings ──────────────────────────────────────────────

    async def get_setting(self, key: str) -> str:
        cursor = await self._db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    async def set_setting(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

    async def get_all_settings(self) -> dict[str, str]:
        cursor = await self._db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        result = dict(DEFAULT_SETTINGS)
        for r in rows:
            result[r["key"]] = r["value"]
        return result

    # ── Users ─────────────────────────────────────────────────

    async def ensure_user(self, user_id: int) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )

    async def get_balance(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        cursor = await self._db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["balance"] if row else 0

    async def get_all_balances(self, limit: int = 20) -> list[tuple[int, int]]:
        cursor = await self._db.execute(
            "SELECT user_id, balance FROM users WHERE balance > 0 "
            "ORDER BY balance DESC LIMIT ?",
            (limit,),
        )
        return [(r["user_id"], r["balance"]) for r in await cursor.fetchall()]

    async def get_user_count(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) AS c FROM users")
        return (await cursor.fetchone())["c"]

    # ── Balance operations (write-locked, transactional) ──────

    async def add_balance(
        self,
        user_id: int,
        amount: int,
        tx_type: TransactionType,
        reason: str = "",
        operator_id: int | None = None,
        order_id: int | None = None,
        deposit_id: int | None = None,
    ) -> int:
        if amount <= 0:
            raise ValueError("金額は正の値である必要があります。")
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                before = (await cursor.fetchone())["balance"]
                after = before + amount
                await self._db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (after, user_id),
                )
                await self._db.execute(
                    "INSERT INTO transactions "
                    "(user_id, type, amount, balance_before, balance_after, "
                    "reason, operator_id, order_id, deposit_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, tx_type.value, amount, before, after,
                        reason, operator_id, order_id, deposit_id,
                    ),
                )
                await self._db.execute("COMMIT")
                return after
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def deduct_balance(
        self,
        user_id: int,
        amount: int,
        tx_type: TransactionType,
        reason: str = "",
        operator_id: int | None = None,
        order_id: int | None = None,
    ) -> int:
        if amount <= 0:
            raise ValueError("金額は正の値である必要があります。")
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                before = (await cursor.fetchone())["balance"]
                if before < amount:
                    await self._db.execute("ROLLBACK")
                    raise ValueError(
                        f"残高不足です（残高: ¥{before:,} / 必要: ¥{amount:,}）"
                    )
                after = before - amount
                await self._db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (after, user_id),
                )
                await self._db.execute(
                    "INSERT INTO transactions "
                    "(user_id, type, amount, balance_before, balance_after, "
                    "reason, operator_id, order_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, tx_type.value, -amount, before, after,
                        reason, operator_id, order_id,
                    ),
                )
                await self._db.execute("COMMIT")
                return after
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def set_balance(
        self,
        user_id: int,
        new_balance: int,
        reason: str = "",
        operator_id: int | None = None,
    ) -> int:
        if new_balance < 0:
            raise ValueError("残高を負の値に設定することはできません。")
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                before = (await cursor.fetchone())["balance"]
                diff = new_balance - before
                await self._db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (new_balance, user_id),
                )
                await self._db.execute(
                    "INSERT INTO transactions "
                    "(user_id, type, amount, balance_before, balance_after, "
                    "reason, operator_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, TransactionType.ADMIN_SET.value, diff,
                        before, new_balance, reason, operator_id,
                    ),
                )
                await self._db.execute("COMMIT")
                return new_balance
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ── Transactions ──────────────────────────────────────────

    async def get_transactions(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM transactions WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_transaction_count(self, user_id: int) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE user_id = ?",
            (user_id,),
        )
        return (await cursor.fetchone())["c"]

    # ── Orders ────────────────────────────────────────────────

    async def create_order_with_payment(
        self,
        user_id: int,
        store_id: str,
        store_name: str,
        pickup_method: str,
        total_amount: int,
        user_amount: int,
        subsidy_amount: int,
        hex_data: str,
        products_json: str,
    ) -> int:
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT COUNT(*) AS c FROM orders "
                    "WHERE user_id = ? AND status IN ('pending', 'processing')",
                    (user_id,),
                )
                if (await cursor.fetchone())["c"] > 0:
                    await self._db.execute("ROLLBACK")
                    raise ValueError("既に処理中の注文があります。")

                cursor = await self._db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                balance = (await cursor.fetchone())["balance"]
                if balance < user_amount:
                    await self._db.execute("ROLLBACK")
                    raise ValueError(
                        f"残高不足です（残高: ¥{balance:,} / 必要: ¥{user_amount:,}）"
                    )

                new_balance = balance - user_amount
                await self._db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (new_balance, user_id),
                )

                cursor = await self._db.execute(
                    "INSERT INTO orders "
                    "(user_id, store_id, store_name, pickup_method, "
                    "total_amount, user_amount, subsidy_amount, "
                    "status, hex_data, products_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        user_id, store_id, store_name, pickup_method,
                        total_amount, user_amount, subsidy_amount,
                        hex_data, products_json,
                    ),
                )
                order_id = cursor.lastrowid

                await self._db.execute(
                    "INSERT INTO transactions "
                    "(user_id, type, amount, balance_before, balance_after, "
                    "reason, order_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, TransactionType.ORDER_PAYMENT.value,
                        -user_amount, balance, new_balance,
                        f"注文 #{order_id}", order_id,
                    ),
                )
                await self._db.execute("COMMIT")
                return order_id
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def update_order_status(
        self, order_id: int, new_status: OrderStatus, **kwargs: Any
    ) -> bool:
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT status FROM orders WHERE id = ?", (order_id,)
                )
                row = await cursor.fetchone()
                if row is None:
                    await self._db.execute("ROLLBACK")
                    return False
                current = OrderStatus(row["status"])
                if not can_transition(current, new_status):
                    await self._db.execute("ROLLBACK")
                    raise ValueError(
                        f"無効な状態遷移: {current.display} → {new_status.display}"
                    )
                sets = ["status = ?"]
                params: list[Any] = [new_status.value]
                if new_status == OrderStatus.COMPLETED:
                    sets.append("completed_at = datetime('now')")
                for col in ("receipt_number", "order_token", "order_group", "error_info"):
                    if col in kwargs:
                        sets.append(f"{col} = ?")
                        params.append(kwargs[col])
                params.append(order_id)
                await self._db.execute(
                    f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", params
                )
                await self._db.execute("COMMIT")
                return True
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def refund_order(
        self, order_id: int, operator_id: int | None = None
    ) -> int:
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                )
                order = await cursor.fetchone()
                if order is None:
                    await self._db.execute("ROLLBACK")
                    raise ValueError("注文が見つかりません。")
                current = OrderStatus(order["status"])
                if not can_transition(current, OrderStatus.REFUNDED):
                    await self._db.execute("ROLLBACK")
                    raise ValueError(
                        f"この注文は返金できません（現在: {current.display}）"
                    )
                user_id = order["user_id"]
                amount = order["user_amount"]
                await self._db.execute(
                    "UPDATE orders SET status = 'refunded' WHERE id = ?",
                    (order_id,),
                )
                cursor = await self._db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                before = (await cursor.fetchone())["balance"]
                after = before + amount
                await self._db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (after, user_id),
                )
                await self._db.execute(
                    "INSERT INTO transactions "
                    "(user_id, type, amount, balance_before, balance_after, "
                    "reason, operator_id, order_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, TransactionType.REFUND.value, amount,
                        before, after, f"注文 #{order_id} 返金",
                        operator_id, order_id,
                    ),
                )
                await self._db.execute("COMMIT")
                return amount
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def get_order(self, order_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_user_orders(
        self, user_id: int, limit: int = 5, offset: int = 0
    ) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM orders WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_user_order_count(self, user_id: int) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE user_id = ?", (user_id,)
        )
        return (await cursor.fetchone())["c"]

    async def search_orders(
        self,
        user_id: int | None = None,
        status: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        conds: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            conds.append("user_id = ?")
            params.append(user_id)
        if status is not None:
            conds.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        params.append(limit)
        cursor = await self._db.execute(
            f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ?",
            params,
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def set_achievement_posted(self, order_id: int) -> None:
        async with self._write_lock:
            await self._db.execute(
                "UPDATE orders SET achievement_posted = 1 WHERE id = ?",
                (order_id,),
            )

    async def get_completed_order_count(self) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status = 'completed'"
        )
        return (await cursor.fetchone())["c"]

    # ── Deposits ──────────────────────────────────────────────

    async def create_deposit(self, user_id: int, amount: int) -> int:
        async with self._write_lock:
            await self.ensure_user(user_id)
            cursor = await self._db.execute(
                "INSERT INTO deposits (user_id, amount) VALUES (?, ?)",
                (user_id, amount),
            )
            return cursor.lastrowid

    async def approve_deposit(
        self, deposit_id: int, admin_id: int
    ) -> tuple[int, int, int]:
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT * FROM deposits WHERE id = ?", (deposit_id,)
                )
                dep = await cursor.fetchone()
                if dep is None:
                    await self._db.execute("ROLLBACK")
                    raise ValueError("入金申請が見つかりません。")
                if dep["status"] != DepositStatus.PENDING.value:
                    await self._db.execute("ROLLBACK")
                    raise ValueError("この入金申請は既に処理済みです。")
                user_id = dep["user_id"]
                amount = dep["amount"]
                await self._db.execute(
                    "UPDATE deposits SET status = ?, admin_id = ?, "
                    "processed_at = datetime('now') WHERE id = ?",
                    (DepositStatus.APPROVED.value, admin_id, deposit_id),
                )
                await self.ensure_user(user_id)
                cursor = await self._db.execute(
                    "SELECT balance FROM users WHERE user_id = ?", (user_id,)
                )
                before = (await cursor.fetchone())["balance"]
                after = before + amount
                await self._db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (after, user_id),
                )
                await self._db.execute(
                    "INSERT INTO transactions "
                    "(user_id, type, amount, balance_before, balance_after, "
                    "reason, operator_id, deposit_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, TransactionType.DEPOSIT.value, amount,
                        before, after, f"入金 #{deposit_id}",
                        admin_id, deposit_id,
                    ),
                )
                await self._db.execute("COMMIT")
                return user_id, amount, after
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def reject_deposit(
        self, deposit_id: int, admin_id: int, reason: str = ""
    ) -> tuple[int, int]:
        async with self._write_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT * FROM deposits WHERE id = ?", (deposit_id,)
                )
                dep = await cursor.fetchone()
                if dep is None:
                    await self._db.execute("ROLLBACK")
                    raise ValueError("入金申請が見つかりません。")
                if dep["status"] != DepositStatus.PENDING.value:
                    await self._db.execute("ROLLBACK")
                    raise ValueError("この入金申請は既に処理済みです。")
                await self._db.execute(
                    "UPDATE deposits SET status = ?, admin_id = ?, "
                    "reject_reason = ?, processed_at = datetime('now') WHERE id = ?",
                    (DepositStatus.REJECTED.value, admin_id, reason, deposit_id),
                )
                await self._db.execute("COMMIT")
                return dep["user_id"], dep["amount"]
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def get_deposit(self, deposit_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM deposits WHERE id = ?", (deposit_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_pending_deposits(self, limit: int = 20) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM deposits WHERE status = 'pending' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def update_deposit_message(
        self, deposit_id: int, message_id: int, channel_id: int
    ) -> None:
        await self._db.execute(
            "UPDATE deposits SET message_id = ?, channel_id = ? WHERE id = ?",
            (message_id, channel_id, deposit_id),
        )

    # ── Panels ────────────────────────────────────────────────

    async def save_panel(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                "DELETE FROM panels WHERE guild_id = ?", (guild_id,)
            )
            await self._db.execute(
                "INSERT INTO panels (guild_id, channel_id, message_id) "
                "VALUES (?, ?, ?)",
                (guild_id, channel_id, message_id),
            )

    async def get_panel(self, guild_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM panels WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_panel(self, guild_id: int) -> None:
        async with self._write_lock:
            await self._db.execute(
                "DELETE FROM panels WHERE guild_id = ?", (guild_id,)
            )

    async def get_all_panels(self) -> list[dict]:
        cursor = await self._db.execute("SELECT * FROM panels")
        return [dict(r) for r in await cursor.fetchall()]

    # ── Stats ─────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, int]:
        s: dict[str, int] = {}
        for label, sql in (
            ("user_count", "SELECT COUNT(*) AS c FROM users"),
            ("total_orders", "SELECT COUNT(*) AS c FROM orders"),
            ("completed_orders",
             "SELECT COUNT(*) AS c FROM orders WHERE status='completed'"),
            ("total_revenue",
             "SELECT COALESCE(SUM(user_amount),0) AS c FROM orders WHERE status='completed'"),
            ("total_subsidy",
             "SELECT COALESCE(SUM(subsidy_amount),0) AS c FROM orders WHERE status='completed'"),
            ("total_balance",
             "SELECT COALESCE(SUM(balance),0) AS c FROM users"),
            ("pending_deposits",
             "SELECT COUNT(*) AS c FROM deposits WHERE status='pending'"),
            ("review_orders",
             "SELECT COUNT(*) AS c FROM orders WHERE status='manual_review'"),
        ):
            cursor = await self._db.execute(sql)
            s[label] = (await cursor.fetchone())["c"]
        return s

    # ── Export ─────────────────────────────────────────────────

    async def export_orders(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT id, user_id, store_id, store_name, pickup_method, "
            "total_amount, user_amount, subsidy_amount, status, "
            "receipt_number, created_at, completed_at FROM orders ORDER BY id"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def export_transactions(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT id, user_id, type, amount, balance_before, balance_after, "
            "reason, operator_id, order_id, deposit_id, created_at "
            "FROM transactions ORDER BY id"
        )
        return [dict(r) for r in await cursor.fetchall()]
