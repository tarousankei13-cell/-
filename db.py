"""Database layer for McDonald's Concierge Bot.

Write operations are serialized through asyncio.Lock.
Critical balance operations use BEGIN IMMEDIATE for atomicity.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from models import (
    DEFAULT_SETTINGS,
    DecodedOrderInfo,
    DepositStatus,
    OrderStatus,
    Rank,
    RateBreakdown,
    TransactionType,
    can_transition,
    clamp_rate,
    day_start_utc,
    hex_digest,
    parse_iso,
    resolve_rank,
    utc_now,
)

logger = logging.getLogger("bot.db")
SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: str = "concierge.db"):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()
        self._settings_cache: Optional[dict[str, str]] = None
        self._balance_cache: dict[int, int] = {}

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self._migrate()
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
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                hex_data TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                bonus INTEGER NOT NULL,
                uses_left INTEGER NOT NULL DEFAULT -1,
                per_user INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT NOT NULL DEFAULT '',
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS coupon_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                order_id INTEGER,
                used_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS product_names (
                product_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS unknown_products (
                product_id TEXT PRIMARY KEY,
                seen_count INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status);
            CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id);
            CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
            CREATE INDEX IF NOT EXISTS idx_coupon_uses_code ON coupon_uses(code, user_id);
        """)
        cursor = await self._db.execute("SELECT version FROM schema_version LIMIT 1")
        if await cursor.fetchone() is None:
            await self._db.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )

    # ── マイグレーション ──────────────────────────────────────

    async def _columns(self, table: str) -> set[str]:
        cursor = await self._db.execute(f"PRAGMA table_info({table})")
        return {r["name"] for r in await cursor.fetchall()}

    async def _add_column(self, table: str, name: str, ddl: str) -> None:
        if name not in await self._columns(table):
            await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            logger.info("Migration: added %s.%s", table, name)

    async def _migrate(self) -> None:
        cursor = await self._db.execute("SELECT version FROM schema_version LIMIT 1")
        row = await cursor.fetchone()
        current = row["version"] if row else SCHEMA_VERSION

        # v1 → v2: ランク/ポイント/VIP/BL/クーポン/Hex重複/リトライ
        await self._add_column("users", "points", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column("users", "custom_rate", "INTEGER")
        await self._add_column("users", "blacklisted", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column("users", "blacklist_reason", "TEXT NOT NULL DEFAULT ''")
        await self._add_column("users", "active_coupon", "TEXT NOT NULL DEFAULT ''")
        await self._add_column("users", "notify_dm", "INTEGER NOT NULL DEFAULT 1")

        await self._add_column("orders", "hex_hash", "TEXT NOT NULL DEFAULT ''")
        await self._add_column("orders", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column("orders", "rate_used", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column("orders", "coupon_code", "TEXT NOT NULL DEFAULT ''")
        await self._add_column("orders", "points_earned", "INTEGER NOT NULL DEFAULT 0")

        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_hexhash ON orders(hex_hash)"
        )

        if current != SCHEMA_VERSION:
            await self._db.execute(
                "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
            )
            logger.info("Schema migrated: v%s → v%s", current, SCHEMA_VERSION)

    async def _init_settings(self) -> None:
        for key, default in DEFAULT_SETTINGS.items():
            await self._db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, default),
            )
        self._settings_cache = None

    # ── Settings (キャッシュ付き) ─────────────────────────────

    async def _load_settings(self) -> dict[str, str]:
        if self._settings_cache is None:
            cursor = await self._db.execute("SELECT key, value FROM settings")
            rows = await cursor.fetchall()
            cache = dict(DEFAULT_SETTINGS)
            for r in rows:
                cache[r["key"]] = r["value"]
            self._settings_cache = cache
        return self._settings_cache

    async def get_setting(self, key: str) -> str:
        cache = await self._load_settings()
        return cache.get(key, DEFAULT_SETTINGS.get(key, ""))

    async def get_int_setting(self, key: str, default: int = 0) -> int:
        try:
            return int(await self.get_setting(key))
        except (ValueError, TypeError):
            return default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            if self._settings_cache is not None:
                self._settings_cache[key] = value

    async def get_all_settings(self) -> dict[str, str]:
        return dict(await self._load_settings())

    # ── Users ─────────────────────────────────────────────────

    async def ensure_user(self, user_id: int) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )

    async def get_user(self, user_id: int) -> dict:
        await self.ensure_user(user_id)
        cursor = await self._db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def get_balance(self, user_id: int) -> int:
        cached = self._balance_cache.get(user_id)
        if cached is not None:
            return cached
        await self.ensure_user(user_id)
        cursor = await self._db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        balance = row["balance"] if row else 0
        self._balance_cache[user_id] = balance
        return balance

    def _invalidate_balance(self, user_id: int) -> None:
        self._balance_cache.pop(user_id, None)

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

    # ── Blacklist ─────────────────────────────────────────────

    async def set_blacklist(
        self, user_id: int, blocked: bool, reason: str = ""
    ) -> None:
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute(
                "UPDATE users SET blacklisted = ?, blacklist_reason = ? "
                "WHERE user_id = ?",
                (1 if blocked else 0, reason, user_id),
            )

    async def is_blacklisted(self, user_id: int) -> tuple[bool, str]:
        cursor = await self._db.execute(
            "SELECT blacklisted, blacklist_reason FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False, ""
        return bool(row["blacklisted"]), row["blacklist_reason"]

    async def set_notify(self, user_id: int, enabled: bool) -> None:
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute(
                "UPDATE users SET notify_dm = ? WHERE user_id = ?",
                (1 if enabled else 0, user_id),
            )

    async def get_blacklist(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT user_id, blacklist_reason FROM users WHERE blacklisted = 1"
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── VIP rate ──────────────────────────────────────────────

    async def set_custom_rate(self, user_id: int, rate: int | None) -> None:
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute(
                "UPDATE users SET custom_rate = ? WHERE user_id = ?",
                (rate, user_id),
            )

    async def get_vip_users(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT user_id, custom_rate FROM users WHERE custom_rate IS NOT NULL"
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Points ────────────────────────────────────────────────

    async def get_points(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        cursor = await self._db.execute(
            "SELECT points FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["points"] if row else 0

    async def add_points(self, user_id: int, amount: int) -> int:
        if amount <= 0:
            return await self.get_points(user_id)
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute(
                "UPDATE users SET points = points + ? WHERE user_id = ?",
                (amount, user_id),
            )
            cursor = await self._db.execute(
                "SELECT points FROM users WHERE user_id = ?", (user_id,)
            )
            return (await cursor.fetchone())["points"]

    async def redeem_points(self, user_id: int, amount: int) -> tuple[int, int]:
        """ポイントを残高に交換。(残ポイント, 新残高) を返す。"""
        if amount <= 0:
            raise ValueError("交換ポイントは1以上で指定してください。")
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._db.execute(
                    "SELECT points, balance FROM users WHERE user_id = ?", (user_id,)
                )
                row = await cursor.fetchone()
                points, balance = row["points"], row["balance"]
                if points < amount:
                    await self._db.execute("ROLLBACK")
                    raise ValueError(
                        f"ポイントが不足しています（保有: {points:,}pt）"
                    )
                new_points = points - amount
                new_balance = balance + amount
                await self._db.execute(
                    "UPDATE users SET points = ?, balance = ? WHERE user_id = ?",
                    (new_points, new_balance, user_id),
                )
                await self._db.execute(
                    "INSERT INTO transactions "
                    "(user_id, type, amount, balance_before, balance_after, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user_id, TransactionType.POINT_REDEEM.value, amount,
                        balance, new_balance, f"{amount:,}pt を残高に交換",
                    ),
                )
                await self._db.execute("COMMIT")
                self._balance_cache[user_id] = new_balance
                return new_points, new_balance
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                self._invalidate_balance(user_id)
                raise

    # ── 負担率の解決 ───────────────────────────────────────────

    async def resolve_rate(self, user_id: int) -> RateBreakdown:
        settings = await self._load_settings()
        user = await self.get_user(user_id)

        base = clamp_rate(int(settings.get("user_rate", "60") or 60))
        br = RateBreakdown(base=base, final=base)

        # VIP個別レート（基準を上書き）
        if user.get("custom_rate") is not None:
            br.base = clamp_rate(int(user["custom_rate"]))
            br.vip = True
            br.final = br.base

        # キャンペーン（基準を上書き。VIPの方が有利なら据え置き）
        camp_rate = settings.get("campaign_rate", "")
        camp_end = parse_iso(settings.get("campaign_end", ""))
        if camp_rate and (camp_end is None or camp_end > utc_now()):
            try:
                cr = clamp_rate(int(camp_rate))
                if cr < br.final:
                    br.final = cr
                    br.campaign = True
            except ValueError:
                pass

        # ランク割引
        if settings.get("rank_enabled", "1") == "1":
            completed = await self.get_user_completed_count(user_id)
            rank = resolve_rank(completed)
            br.rank = rank
            br.rank_bonus = rank.bonus
            br.final -= rank.bonus

        # クーポン
        code = (user.get("active_coupon") or "").strip()
        if code:
            coupon = await self.get_coupon(code)
            if coupon and await self.is_coupon_usable(code, user_id):
                br.coupon_code = coupon["code"]
                br.coupon_bonus = coupon["bonus"]
                br.final -= coupon["bonus"]

        br.final = clamp_rate(br.final)
        return br

    # ── Coupons ───────────────────────────────────────────────

    async def create_coupon(
        self,
        code: str,
        bonus: int,
        uses: int = -1,
        per_user: int = 1,
        expires_at: str = "",
        created_by: int | None = None,
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO coupons "
                "(code, bonus, uses_left, per_user, expires_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (code.upper(), bonus, uses, per_user, expires_at, created_by),
            )

    async def get_coupon(self, code: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM coupons WHERE code = ?", (code.upper(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_coupons(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM coupons ORDER BY created_at DESC LIMIT 50"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_coupon(self, code: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM coupons WHERE code = ?", (code.upper(),)
            )
            return cursor.rowcount > 0

    async def is_coupon_usable(self, code: str, user_id: int) -> bool:
        coupon = await self.get_coupon(code)
        if not coupon:
            return False
        if coupon["uses_left"] == 0:
            return False
        exp = parse_iso(coupon["expires_at"])
        if exp is not None and exp <= utc_now():
            return False
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM coupon_uses WHERE code = ? AND user_id = ?",
            (coupon["code"], user_id),
        )
        used = (await cursor.fetchone())["c"]
        if coupon["per_user"] > 0 and used >= coupon["per_user"]:
            return False
        return True

    async def set_active_coupon(self, user_id: int, code: str) -> None:
        async with self._write_lock:
            await self.ensure_user(user_id)
            await self._db.execute(
                "UPDATE users SET active_coupon = ? WHERE user_id = ?",
                (code.upper(), user_id),
            )

    async def consume_coupon(
        self, code: str, user_id: int, order_id: int
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO coupon_uses (code, user_id, order_id) VALUES (?, ?, ?)",
                (code.upper(), user_id, order_id),
            )
            await self._db.execute(
                "UPDATE coupons SET uses_left = uses_left - 1 "
                "WHERE code = ? AND uses_left > 0",
                (code.upper(),),
            )
            await self._db.execute(
                "UPDATE users SET active_coupon = '' WHERE user_id = ?", (user_id,)
            )

    # ── Favorites ─────────────────────────────────────────────

    async def add_favorite(self, user_id: int, name: str, hex_data: str) -> int:
        async with self._write_lock:
            await self.ensure_user(user_id)
            cursor = await self._db.execute(
                "INSERT INTO favorites (user_id, name, hex_data) VALUES (?, ?, ?)",
                (user_id, name[:60], hex_data),
            )
            return cursor.lastrowid

    async def get_favorites(self, user_id: int, limit: int = 25) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM favorites WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_favorite(self, fav_id: int, user_id: int) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM favorites WHERE id = ? AND user_id = ?",
            (fav_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_favorite(self, fav_id: int, user_id: int) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM favorites WHERE id = ? AND user_id = ?",
                (fav_id, user_id),
            )
            return cursor.rowcount > 0

    # ── Product names ─────────────────────────────────────────

    async def set_product_name(self, product_id: str, name: str) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO product_names (product_id, name, updated_at) "
                "VALUES (?, ?, datetime('now'))",
                (product_id, name),
            )
            await self._db.execute(
                "DELETE FROM unknown_products WHERE product_id = ?", (product_id,)
            )

    async def get_product_name(self, product_id: str) -> str:
        cursor = await self._db.execute(
            "SELECT name FROM product_names WHERE product_id = ?", (product_id,)
        )
        row = await cursor.fetchone()
        return row["name"] if row else ""

    async def list_product_names(self, limit: int = 100) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM product_names ORDER BY product_id LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_product_name(self, product_id: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM product_names WHERE product_id = ?", (product_id,)
            )
            return cursor.rowcount > 0

    async def _record_unknown_product(self, product_id: str) -> None:
        await self._db.execute(
            "INSERT INTO unknown_products (product_id) VALUES (?) "
            "ON CONFLICT(product_id) DO UPDATE SET "
            "seen_count = seen_count + 1, last_seen = datetime('now')",
            (product_id,),
        )

    async def list_unknown_products(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM unknown_products ORDER BY seen_count DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def resolve_product_names(self, decoded: DecodedOrderInfo) -> None:
        """DecodedOrderInfo の商品名を辞書から解決する（未登録は記録）。"""
        async def fill(product) -> None:
            if product.product_id:
                name = await self.get_product_name(product.product_id)
                if name:
                    product.display_name = name
                else:
                    product.display_name = product.product_id
                    await self._record_unknown_product(product.product_id)
            for addon in product.addons:
                await fill(addon)

        for p in decoded.products:
            await fill(p)

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
                self._balance_cache[user_id] = after
                return after
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                self._invalidate_balance(user_id)
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
                self._balance_cache[user_id] = after
                return after
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                self._invalidate_balance(user_id)
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
                self._balance_cache[user_id] = new_balance
                return new_balance
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                self._invalidate_balance(user_id)
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

    async def find_reused_hex(self, hex_data: str) -> dict | None:
        """同一Hexで有効な注文が既にあるか確認。"""
        digest = hex_digest(hex_data)
        cursor = await self._db.execute(
            "SELECT id, user_id, status, created_at FROM orders "
            "WHERE hex_hash = ? AND status NOT IN ('failed', 'cancelled') "
            "ORDER BY id DESC LIMIT 1",
            (digest,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def count_orders_today(self, user_id: int, tz_offset: int = 9) -> int:
        cutoff = day_start_utc(tz_offset)
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM orders "
            "WHERE user_id = ? AND created_at >= ? "
            "AND status NOT IN ('failed', 'cancelled')",
            (user_id, cutoff),
        )
        return (await cursor.fetchone())["c"]

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
        rate_used: int = 0,
        coupon_code: str = "",
    ) -> int:
        digest = hex_digest(hex_data)
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
                    "status, hex_data, products_json, hex_hash, "
                    "rate_used, coupon_code) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (
                        user_id, store_id, store_name, pickup_method,
                        total_amount, user_amount, subsidy_amount,
                        hex_data, products_json, digest,
                        rate_used, coupon_code,
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
                self._balance_cache[user_id] = new_balance
                return order_id
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                self._invalidate_balance(user_id)
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
                for col in (
                    "receipt_number", "order_token", "order_group",
                    "error_info", "retry_count", "points_earned",
                ):
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
            user_id = None
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
                self._balance_cache[user_id] = after
                return amount
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                if user_id is not None:
                    self._invalidate_balance(user_id)
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

    async def get_user_completed_count(self, user_id: int) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM orders "
            "WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        )
        return (await cursor.fetchone())["c"]

    async def get_user_savings(self, user_id: int) -> int:
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(subsidy_amount), 0) AS c FROM orders "
            "WHERE user_id = ? AND status = 'completed'",
            (user_id,),
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

    async def reset_achievement_posted(self, order_id: int) -> None:
        async with self._write_lock:
            await self._db.execute(
                "UPDATE orders SET achievement_posted = 0 WHERE id = ?",
                (order_id,),
            )

    async def get_completed_order_count(self) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status = 'completed'"
        )
        return (await cursor.fetchone())["c"]

    async def get_max_order_id(self) -> int:
        cursor = await self._db.execute("SELECT COALESCE(MAX(id), 0) AS c FROM orders")
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
            user_id = None
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
                self._balance_cache[user_id] = after
                return user_id, amount, after
            except Exception:
                try:
                    await self._db.execute("ROLLBACK")
                except Exception:
                    pass
                if user_id is not None:
                    self._invalidate_balance(user_id)
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
            "ORDER BY id ASC LIMIT ?",
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
            ("failed_orders",
             "SELECT COUNT(*) AS c FROM orders WHERE status='failed'"),
            ("blacklisted",
             "SELECT COUNT(*) AS c FROM users WHERE blacklisted=1"),
            ("total_points",
             "SELECT COALESCE(SUM(points),0) AS c FROM users"),
        ):
            cursor = await self._db.execute(sql)
            s[label] = (await cursor.fetchone())["c"]
        return s

    async def get_daily_stats(
        self, days: int = 7, tz_offset: int = 9
    ) -> list[dict]:
        modifier = f"+{int(tz_offset)} hours"
        cursor = await self._db.execute(
            "SELECT date(created_at, ?) AS d, COUNT(*) AS cnt, "
            "COALESCE(SUM(user_amount),0) AS revenue, "
            "COALESCE(SUM(subsidy_amount),0) AS subsidy "
            "FROM orders WHERE status='completed' "
            "AND created_at >= datetime('now', ?) "
            "GROUP BY d ORDER BY d",
            (modifier, f"-{int(days)} days"),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_hourly_distribution(self, tz_offset: int = 9) -> list[dict]:
        modifier = f"+{int(tz_offset)} hours"
        cursor = await self._db.execute(
            "SELECT strftime('%H', created_at, ?) AS h, COUNT(*) AS cnt "
            "FROM orders WHERE status='completed' GROUP BY h ORDER BY h",
            (modifier,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_top_stores(self, limit: int = 10) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT COALESCE(NULLIF(store_name,''), store_id) AS store, "
            "COUNT(*) AS cnt FROM orders WHERE status='completed' "
            "GROUP BY store ORDER BY cnt DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Backup ────────────────────────────────────────────────

    async def backup_to(self, dest_path: str) -> int:
        """VACUUM INTO で一貫性のあるバックアップを作成。バイト数を返す。"""
        async with self._write_lock:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            await self._db.execute("VACUUM INTO ?", (dest_path,))
        return os.path.getsize(dest_path)

    # ── Export ─────────────────────────────────────────────────

    async def export_orders(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT id, user_id, store_id, store_name, pickup_method, "
            "total_amount, user_amount, subsidy_amount, status, "
            "receipt_number, rate_used, coupon_code, points_earned, "
            "created_at, completed_at FROM orders ORDER BY id"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def export_transactions(self) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT id, user_id, type, amount, balance_before, balance_after, "
            "reason, operator_id, order_id, deposit_id, created_at "
            "FROM transactions ORDER BY id"
        )
        return [dict(r) for r in await cursor.fetchall()]
