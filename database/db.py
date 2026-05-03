import aiosqlite
import asyncio
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from config import Config


class Database:
    def __init__(self):
        self.path = Config.DB_PATH
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA cache_size=-8000")
        await self._create_tables()
        await self._run_migrations()
        await self._seed_defaults()
        await self._conn.commit()

    # ── Schema ────────────────────────────────────────────────────────────────

    async def _create_tables(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                balance         INTEGER NOT NULL DEFAULT 0,
                total_spent     INTEGER NOT NULL DEFAULT 0,
                is_banned       INTEGER NOT NULL DEFAULT 0,
                daily_last      TEXT,
                daily_streak    INTEGER NOT NULL DEFAULT 0,
                vip_tier        INTEGER NOT NULL DEFAULT 0,
                referral_code   TEXT UNIQUE,
                referred_by     INTEGER,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS categories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                emoji       TEXT NOT NULL DEFAULT '🛒',
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS products (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id     INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                name            TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                price           INTEGER NOT NULL,
                stock           INTEGER NOT NULL DEFAULT -1,
                image_url       TEXT NOT NULL DEFAULT '',
                is_available    INTEGER NOT NULL DEFAULT 1,
                is_digital      INTEGER NOT NULL DEFAULT 0,
                sold_count      INTEGER NOT NULL DEFAULT 0,
                tags            TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cart_items (
                user_id     INTEGER NOT NULL,
                product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity    INTEGER NOT NULL DEFAULT 1,
                added_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                recipient_id    INTEGER,
                total_price     INTEGER NOT NULL,
                original_price  INTEGER NOT NULL DEFAULT 0,
                coupon_id       INTEGER,
                status          TEXT NOT NULL DEFAULT 'pending',
                notes           TEXT NOT NULL DEFAULT '',
                is_gift         INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                product_id      INTEGER,
                product_name    TEXT NOT NULL,
                product_price   INTEGER NOT NULL,
                quantity        INTEGER NOT NULL,
                subtotal        INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment     TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL UNIQUE,
                subject     TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'open',
                priority    TEXT NOT NULL DEFAULT 'normal',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                closed_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                type        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- ── New tables ────────────────────────────────────────────────

            CREATE TABLE IF NOT EXISTS coupons (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                code            TEXT NOT NULL UNIQUE COLLATE NOCASE,
                discount_type   TEXT NOT NULL DEFAULT 'percent',
                discount_value  INTEGER NOT NULL,
                min_purchase    INTEGER NOT NULL DEFAULT 0,
                max_uses        INTEGER NOT NULL DEFAULT -1,
                uses_count      INTEGER NOT NULL DEFAULT 0,
                per_user_limit  INTEGER NOT NULL DEFAULT 1,
                expires_at      TEXT,
                is_active       INTEGER NOT NULL DEFAULT 1,
                created_by      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS coupon_uses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                coupon_id   INTEGER NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
                user_id     INTEGER NOT NULL,
                order_id    INTEGER,
                discount    INTEGER NOT NULL DEFAULT 0,
                used_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS digital_keys (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                key_value       TEXT NOT NULL,
                is_delivered    INTEGER NOT NULL DEFAULT 0,
                delivered_to    INTEGER,
                order_id        INTEGER,
                delivered_at    TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                user_id     INTEGER NOT NULL,
                product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                notified    INTEGER NOT NULL DEFAULT 0,
                added_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS flash_sales (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                discount_percent    INTEGER NOT NULL,
                original_price      INTEGER NOT NULL,
                sale_price          INTEGER NOT NULL,
                start_time          TEXT NOT NULL,
                end_time            TEXT NOT NULL,
                notify_channel      INTEGER,
                is_active           INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS rank_tiers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                emoji       TEXT NOT NULL DEFAULT '🏅',
                min_spent   INTEGER NOT NULL,
                role_id     INTEGER,
                color       INTEGER NOT NULL DEFAULT 0x5865F2,
                perks       TEXT NOT NULL DEFAULT '',
                daily_bonus INTEGER NOT NULL DEFAULT 0,
                sort_order  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS achievements (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                description     TEXT NOT NULL,
                icon            TEXT NOT NULL DEFAULT '🏆',
                reward          INTEGER NOT NULL DEFAULT 0,
                condition_type  TEXT NOT NULL,
                condition_value INTEGER NOT NULL,
                is_secret       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_achievements (
                user_id         INTEGER NOT NULL,
                achievement_id  INTEGER NOT NULL REFERENCES achievements(id),
                earned_at       TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, achievement_id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id     INTEGER NOT NULL,
                referred_id     INTEGER NOT NULL UNIQUE,
                reward_given    INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS shop_settings (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_products_category      ON products(category_id);
            CREATE INDEX IF NOT EXISTS idx_cart_user              ON cart_items(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_user            ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_order_items_order      ON order_items(order_id);
            CREATE INDEX IF NOT EXISTS idx_reviews_product        ON reviews(product_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_user      ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_digital_keys_product   ON digital_keys(product_id, is_delivered);
            CREATE INDEX IF NOT EXISTS idx_watchlist_product      ON watchlist(product_id, notified);
            CREATE INDEX IF NOT EXISTS idx_flash_sales_active     ON flash_sales(is_active, end_time);
            CREATE INDEX IF NOT EXISTS idx_coupon_uses_user       ON coupon_uses(coupon_id, user_id);
        """)

    async def _run_migrations(self):
        """Add columns to existing tables if they don't exist."""
        migrations = [
            ("users", "daily_streak INTEGER NOT NULL DEFAULT 0"),
            ("users", "vip_tier INTEGER NOT NULL DEFAULT 0"),
            ("users", "referral_code TEXT"),
            ("users", "referred_by INTEGER"),
            ("products", "is_digital INTEGER NOT NULL DEFAULT 0"),
            ("products", "tags TEXT NOT NULL DEFAULT ''"),
            ("orders", "recipient_id INTEGER"),
            ("orders", "original_price INTEGER NOT NULL DEFAULT 0"),
            ("orders", "coupon_id INTEGER"),
            ("orders", "is_gift INTEGER NOT NULL DEFAULT 0"),
            ("tickets", "priority TEXT NOT NULL DEFAULT 'normal'"),
        ]
        async with self._lock:
            for table, col_def in migrations:
                col_name = col_def.split()[0]
                try:
                    await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
                    await self._conn.commit()
                except Exception:
                    pass

    async def _seed_defaults(self):
        """Insert default rank tiers and achievements if empty."""
        count = (await self._fetch_one("SELECT COUNT(*) as c FROM rank_tiers"))["c"]
        if count == 0:
            default_ranks = [
                ("ノーマル",   "⚪", 0,       None, 0xAAAAAA, "通常メンバー",                  0,   0),
                ("ブロンズ",   "🥉", 5000,    None, 0xCD7F32, "デイリー+50pt",                 50,  1),
                ("シルバー",   "🥈", 20000,   None, 0xC0C0C0, "デイリー+150pt • 優先サポート", 150, 2),
                ("ゴールド",   "🥇", 50000,   None, 0xFFD700, "デイリー+300pt • 専用ロール",   300, 3),
                ("プラチナ",   "💎", 150000,  None, 0x00B4D8, "デイリー+500pt • VIP特典",      500, 4),
                ("ダイヤモンド","💠", 500000,  None, 0x5865F2, "デイリー+1000pt • 最高VIP",    1000, 5),
            ]
            async with self._lock:
                await self._conn.executemany(
                    "INSERT INTO rank_tiers (name, emoji, min_spent, role_id, color, perks, daily_bonus, sort_order) VALUES (?,?,?,?,?,?,?,?)",
                    default_ranks
                )

        ach_count = (await self._fetch_one("SELECT COUNT(*) as c FROM achievements"))["c"]
        if ach_count == 0:
            default_achievements = [
                ("初購入！",         "初めての購入をしました",                "🛒", 200,  "purchase_count", 1,      0),
                ("リピーター",       "合計5回購入しました",                   "🔄", 500,  "purchase_count", 5,      0),
                ("ショッパー",       "合計10回購入しました",                  "🛍️", 1000, "purchase_count", 10,     0),
                ("ヘビーユーザー",   "合計50回購入しました",                  "⚡", 3000, "purchase_count", 50,     0),
                ("常連客",           "合計100回購入しました",                 "👑", 8000, "purchase_count", 100,    0),
                ("太客",             "累計1,000pt使いました",                 "💸", 100,  "total_spent",    1000,   0),
                ("お金持ち",         "累計10,000pt使いました",                "💰", 500,  "total_spent",    10000,  0),
                ("大富豪",           "累計100,000pt使いました",               "🤑", 2000, "total_spent",    100000, 0),
                ("レビュアー",       "レビューを1件投稿しました",             "⭐", 100,  "review_count",   1,      0),
                ("批評家",           "レビューを10件投稿しました",            "📝", 500,  "review_count",   10,     0),
                ("デイリーマスター", "デイリーを7日連続で受け取りました",     "🔥", 700,  "daily_streak",   7,      0),
                ("ストリーク王",     "デイリーを30日連続で受け取りました",    "🔥", 3000, "daily_streak",   30,     0),
                ("友達紹介者",       "友達を紹介しました",                    "🤝", 500,  "referral_count", 1,      0),
                ("神様",             "全実績を解除しました（シークレット）",  "🌟", 5000, "all_achievements", 13,   1),
            ]
            async with self._lock:
                await self._conn.executemany(
                    "INSERT INTO achievements (name, description, icon, reward, condition_type, condition_value, is_secret) VALUES (?,?,?,?,?,?,?)",
                    default_achievements
                )
        await self._conn.commit()

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _fetch_one(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._lock:
            async with self._conn.execute(sql, params) as cur:
                return await cur.fetchone()

    async def _fetch_all(self, sql: str, params: tuple = ()) -> List[aiosqlite.Row]:
        async with self._lock:
            async with self._conn.execute(sql, params) as cur:
                return await cur.fetchall()

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        async with self._lock:
            async with self._conn.execute(sql, params) as cur:
                await self._conn.commit()
                return cur.lastrowid

    # ── users ─────────────────────────────────────────────────────────────────

    async def get_user(self, user_id: int) -> aiosqlite.Row:
        row = await self._fetch_one("SELECT * FROM users WHERE user_id=?", (user_id,))
        if row is None:
            import secrets
            code = secrets.token_urlsafe(6).upper()
            await self._execute(
                "INSERT OR IGNORE INTO users (user_id, balance, referral_code) VALUES (?, ?, ?)",
                (user_id, Config.STARTING_BALANCE, code)
            )
            await self.add_transaction(user_id, Config.STARTING_BALANCE, "welcome", "新規登録ボーナス")
            row = await self._fetch_one("SELECT * FROM users WHERE user_id=?", (user_id,))
        return row

    async def update_balance(self, user_id: int, amount: int) -> bool:
        await self.get_user(user_id)
        row = await self._fetch_one("SELECT balance FROM users WHERE user_id=?", (user_id,))
        if row["balance"] + amount < 0:
            return False
        await self._execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        return True

    async def get_balance_rank(self) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT user_id, balance, total_spent FROM users ORDER BY balance DESC LIMIT 10"
        )

    async def get_spending_rank(self) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT user_id, balance, total_spent FROM users ORDER BY total_spent DESC LIMIT 10"
        )

    async def ban_user(self, user_id: int, banned: bool = True):
        await self.get_user(user_id)
        await self._execute("UPDATE users SET is_banned=? WHERE user_id=?", (int(banned), user_id))

    async def claim_daily(self, user_id: int) -> Dict:
        user = await self.get_user(user_id)
        today = date.today().isoformat()
        if user["daily_last"] == today:
            return {"success": False}

        yesterday = (date.today().replace(day=date.today().day - 1)).isoformat() if date.today().day > 1 else None
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        streak = (user["daily_streak"] + 1) if user["daily_last"] == yesterday else 1

        tier = await self.get_user_rank_tier(user_id)
        base_reward = Config.DAILY_REWARD
        bonus = tier["daily_bonus"] if tier else 0
        streak_bonus = min(streak * 10, 200)
        total_reward = base_reward + bonus + streak_bonus

        await self._execute(
            "UPDATE users SET daily_last=?, daily_streak=?, balance=balance+? WHERE user_id=?",
            (today, streak, total_reward, user_id)
        )
        await self.add_transaction(user_id, total_reward, "daily", f"デイリーボーナス (ストリーク {streak}日)")
        return {"success": True, "reward": total_reward, "streak": streak, "bonus": bonus, "streak_bonus": streak_bonus}

    # ── transactions ──────────────────────────────────────────────────────────

    async def add_transaction(self, user_id: int, amount: int, tx_type: str, desc: str):
        await self._execute(
            "INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,?,?)",
            (user_id, amount, tx_type, desc)
        )

    async def get_transactions(self, user_id: int, limit: int = 10) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        )

    # ── categories ────────────────────────────────────────────────────────────

    async def get_categories(self) -> List[aiosqlite.Row]:
        return await self._fetch_all("SELECT * FROM categories ORDER BY sort_order, id")

    async def get_category(self, cat_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one("SELECT * FROM categories WHERE id=?", (cat_id,))

    async def add_category(self, name: str, description: str, emoji: str) -> int:
        return await self._execute(
            "INSERT INTO categories (name, description, emoji) VALUES (?,?,?)",
            (name, description, emoji)
        )

    async def update_category(self, cat_id: int, name: str, description: str, emoji: str):
        await self._execute(
            "UPDATE categories SET name=?, description=?, emoji=? WHERE id=?",
            (name, description, emoji, cat_id)
        )

    async def delete_category(self, cat_id: int):
        await self._execute("DELETE FROM categories WHERE id=?", (cat_id,))

    # ── products ──────────────────────────────────────────────────────────────

    async def get_products(self, category_id=None, available_only=True) -> List[aiosqlite.Row]:
        base = "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE 1=1"
        params = []
        if category_id:
            base += " AND p.category_id=?"
            params.append(category_id)
        if available_only:
            base += " AND p.is_available=1 AND (p.stock=-1 OR p.stock>0)"
        base += " ORDER BY p.sold_count DESC, p.id"
        return await self._fetch_all(base, tuple(params))

    async def get_product(self, product_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one(
            "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE p.id=?",
            (product_id,)
        )

    async def search_products(self, query: str) -> List[aiosqlite.Row]:
        q = f"%{query}%"
        return await self._fetch_all(
            "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE (p.name LIKE ? OR p.description LIKE ? OR p.tags LIKE ?) AND p.is_available=1 ORDER BY p.sold_count DESC LIMIT 25",
            (q, q, q)
        )

    async def get_products_by_tag(self, tag: str) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE p.tags LIKE ? AND p.is_available=1",
            (f"%{tag}%",)
        )

    async def add_product(self, category_id: int, name: str, description: str, price: int, stock: int, image_url: str, is_digital: int = 0, tags: str = "") -> int:
        return await self._execute(
            "INSERT INTO products (category_id, name, description, price, stock, image_url, is_digital, tags) VALUES (?,?,?,?,?,?,?,?)",
            (category_id, name, description, price, stock, image_url, is_digital, tags)
        )

    async def update_product(self, product_id: int, **kwargs):
        fields = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [datetime.now().isoformat(), product_id]
        await self._execute(f"UPDATE products SET {fields}, updated_at=? WHERE id=?", tuple(vals))

    async def update_stock(self, product_id: int, stock: int):
        await self._execute(
            "UPDATE products SET stock=?, updated_at=? WHERE id=?",
            (stock, datetime.now().isoformat(), product_id)
        )

    async def delete_product(self, product_id: int):
        await self._execute("DELETE FROM products WHERE id=?", (product_id,))

    async def decrement_stock(self, product_id: int, quantity: int) -> bool:
        row = await self._fetch_one("SELECT stock FROM products WHERE id=?", (product_id,))
        if row is None:
            return False
        if row["stock"] == -1:
            await self._execute(
                "UPDATE products SET sold_count=sold_count+?, updated_at=? WHERE id=?",
                (quantity, datetime.now().isoformat(), product_id)
            )
            return True
        if row["stock"] < quantity:
            return False
        await self._execute(
            "UPDATE products SET stock=stock-?, sold_count=sold_count+?, updated_at=? WHERE id=?",
            (quantity, quantity, datetime.now().isoformat(), product_id)
        )
        return True

    async def get_low_stock_products(self, threshold: int = 5) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT p.*, c.name as cat_name FROM products p JOIN categories c ON p.category_id=c.id WHERE p.stock != -1 AND p.stock <= ? AND p.is_available=1 ORDER BY p.stock",
            (threshold,)
        )

    # ── cart ──────────────────────────────────────────────────────────────────

    async def get_cart(self, user_id: int) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT ci.*, p.name, p.price, p.image_url, p.stock, p.is_available, p.is_digital, (p.stock=-1 OR p.stock>=ci.quantity) as in_stock FROM cart_items ci JOIN products p ON ci.product_id=p.id WHERE ci.user_id=? ORDER BY ci.added_at",
            (user_id,)
        )

    async def cart_total(self, user_id: int) -> int:
        row = await self._fetch_one(
            "SELECT COALESCE(SUM(ci.quantity * p.price),0) as total FROM cart_items ci JOIN products p ON ci.product_id=p.id WHERE ci.user_id=?",
            (user_id,)
        )
        return row["total"] if row else 0

    async def add_to_cart(self, user_id: int, product_id: int, quantity: int = 1) -> bool:
        cart = await self.get_cart(user_id)
        item_count = len(cart)
        existing = next((c for c in cart if c["product_id"] == product_id), None)
        if existing is None and item_count >= Config.MAX_CART_ITEMS:
            return False
        new_qty = quantity if existing is None else existing["quantity"] + quantity
        if new_qty > Config.MAX_CART_QUANTITY:
            return False
        await self._execute(
            "INSERT INTO cart_items (user_id, product_id, quantity) VALUES (?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=?",
            (user_id, product_id, new_qty, new_qty)
        )
        return True

    async def update_cart_quantity(self, user_id: int, product_id: int, quantity: int):
        if quantity <= 0:
            await self.remove_from_cart(user_id, product_id)
        else:
            await self._execute(
                "INSERT INTO cart_items (user_id, product_id, quantity) VALUES (?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=?",
                (user_id, product_id, quantity, quantity)
            )

    async def remove_from_cart(self, user_id: int, product_id: int):
        await self._execute("DELETE FROM cart_items WHERE user_id=? AND product_id=?", (user_id, product_id))

    async def clear_cart(self, user_id: int):
        await self._execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))

    # ── orders ────────────────────────────────────────────────────────────────

    async def create_order(self, user_id: int, items: List[Dict], total: int, notes: str,
                           original_price: int = 0, coupon_id: int = None,
                           recipient_id: int = None, is_gift: bool = False) -> int:
        order_id = await self._execute(
            "INSERT INTO orders (user_id, total_price, original_price, coupon_id, notes, recipient_id, is_gift) VALUES (?,?,?,?,?,?,?)",
            (user_id, total, original_price or total, coupon_id, notes, recipient_id, int(is_gift))
        )
        for item in items:
            await self._execute(
                "INSERT INTO order_items (order_id, product_id, product_name, product_price, quantity, subtotal) VALUES (?,?,?,?,?,?)",
                (order_id, item["product_id"], item["name"], item["price"], item["quantity"], item["price"] * item["quantity"])
            )
        await self._execute("UPDATE users SET total_spent=total_spent+? WHERE user_id=?", (total, user_id))
        return order_id

    async def get_order(self, order_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one("SELECT * FROM orders WHERE id=?", (order_id,))

    async def get_order_items(self, order_id: int) -> List[aiosqlite.Row]:
        return await self._fetch_all("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,))

    async def get_user_orders(self, user_id: int, limit: int = 20) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        )

    async def get_all_orders(self, status=None, limit: int = 50) -> List[aiosqlite.Row]:
        if status:
            return await self._fetch_all(
                "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            )
        return await self._fetch_all("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))

    async def update_order_status(self, order_id: int, status: str):
        await self._execute(
            "UPDATE orders SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now().isoformat(), order_id)
        )

    async def has_purchased(self, user_id: int, product_id: int) -> bool:
        row = await self._fetch_one(
            "SELECT 1 FROM orders o JOIN order_items oi ON o.id=oi.order_id WHERE o.user_id=? AND oi.product_id=? AND o.status='completed' LIMIT 1",
            (user_id, product_id)
        )
        return row is not None

    # ── coupons ───────────────────────────────────────────────────────────────

    async def create_coupon(self, code: str, discount_type: str, discount_value: int,
                            min_purchase: int, max_uses: int, per_user_limit: int,
                            expires_at: str, created_by: int) -> int:
        return await self._execute(
            "INSERT INTO coupons (code, discount_type, discount_value, min_purchase, max_uses, per_user_limit, expires_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
            (code.upper(), discount_type, discount_value, min_purchase, max_uses, per_user_limit, expires_at, created_by)
        )

    async def get_coupon(self, code: str) -> Optional[aiosqlite.Row]:
        return await self._fetch_one("SELECT * FROM coupons WHERE code=? COLLATE NOCASE", (code.upper(),))

    async def get_all_coupons(self) -> List[aiosqlite.Row]:
        return await self._fetch_all("SELECT * FROM coupons ORDER BY created_at DESC")

    async def validate_coupon(self, code: str, user_id: int, subtotal: int) -> Dict:
        coupon = await self.get_coupon(code)
        if not coupon:
            return {"valid": False, "reason": "クーポンコードが見つかりません。"}
        if not coupon["is_active"]:
            return {"valid": False, "reason": "このクーポンは無効です。"}
        if coupon["expires_at"] and coupon["expires_at"] < datetime.now().isoformat():
            return {"valid": False, "reason": "このクーポンの有効期限が切れています。"}
        if coupon["max_uses"] != -1 and coupon["uses_count"] >= coupon["max_uses"]:
            return {"valid": False, "reason": "このクーポンの使用回数が上限に達しています。"}
        if subtotal < coupon["min_purchase"]:
            return {"valid": False, "reason": f"最低購入金額 **{coupon['min_purchase']:,}** {Config.CURRENCY_NAME} に達していません。"}

        user_uses = await self._fetch_one(
            "SELECT COUNT(*) as c FROM coupon_uses WHERE coupon_id=? AND user_id=?",
            (coupon["id"], user_id)
        )
        if user_uses["c"] >= coupon["per_user_limit"]:
            return {"valid": False, "reason": "このクーポンは既に使用済みです。"}

        if coupon["discount_type"] == "percent":
            discount = int(subtotal * coupon["discount_value"] / 100)
        else:
            discount = min(coupon["discount_value"], subtotal)

        return {"valid": True, "coupon": coupon, "discount": discount, "final": subtotal - discount}

    async def use_coupon(self, coupon_id: int, user_id: int, order_id: int, discount: int):
        await self._execute(
            "INSERT INTO coupon_uses (coupon_id, user_id, order_id, discount) VALUES (?,?,?,?)",
            (coupon_id, user_id, order_id, discount)
        )
        await self._execute("UPDATE coupons SET uses_count=uses_count+1 WHERE id=?", (coupon_id,))

    async def deactivate_coupon(self, coupon_id: int):
        await self._execute("UPDATE coupons SET is_active=0 WHERE id=?", (coupon_id,))

    # ── digital keys ──────────────────────────────────────────────────────────

    async def add_digital_key(self, product_id: int, key_value: str) -> int:
        return await self._execute(
            "INSERT INTO digital_keys (product_id, key_value) VALUES (?,?)",
            (product_id, key_value)
        )

    async def add_digital_keys_bulk(self, product_id: int, keys: List[str]) -> int:
        count = 0
        for key in keys:
            key = key.strip()
            if key:
                await self.add_digital_key(product_id, key)
                count += 1
        return count

    async def get_available_key(self, product_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one(
            "SELECT * FROM digital_keys WHERE product_id=? AND is_delivered=0 ORDER BY id LIMIT 1",
            (product_id,)
        )

    async def deliver_key(self, key_id: int, user_id: int, order_id: int):
        await self._execute(
            "UPDATE digital_keys SET is_delivered=1, delivered_to=?, order_id=?, delivered_at=? WHERE id=?",
            (user_id, order_id, datetime.now().isoformat(), key_id)
        )

    async def get_key_stats(self, product_id: int) -> Dict:
        total = (await self._fetch_one("SELECT COUNT(*) as c FROM digital_keys WHERE product_id=?", (product_id,)))["c"]
        available = (await self._fetch_one("SELECT COUNT(*) as c FROM digital_keys WHERE product_id=? AND is_delivered=0", (product_id,)))["c"]
        return {"total": total, "available": available, "delivered": total - available}

    async def get_user_delivered_keys(self, user_id: int) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT dk.*, p.name as product_name FROM digital_keys dk JOIN products p ON dk.product_id=p.id WHERE dk.delivered_to=? ORDER BY dk.delivered_at DESC",
            (user_id,)
        )

    # ── watchlist ─────────────────────────────────────────────────────────────

    async def add_to_watchlist(self, user_id: int, product_id: int):
        await self._execute(
            "INSERT OR IGNORE INTO watchlist (user_id, product_id) VALUES (?,?)",
            (user_id, product_id)
        )

    async def remove_from_watchlist(self, user_id: int, product_id: int):
        await self._execute("DELETE FROM watchlist WHERE user_id=? AND product_id=?", (user_id, product_id))

    async def get_user_watchlist(self, user_id: int) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT w.*, p.name, p.price, p.stock FROM watchlist w JOIN products p ON w.product_id=p.id WHERE w.user_id=? ORDER BY w.added_at DESC",
            (user_id,)
        )

    async def get_product_watchers(self, product_id: int) -> List[int]:
        rows = await self._fetch_all(
            "SELECT user_id FROM watchlist WHERE product_id=? AND notified=0",
            (product_id,)
        )
        return [r["user_id"] for r in rows]

    async def mark_watchers_notified(self, product_id: int):
        await self._execute("UPDATE watchlist SET notified=1 WHERE product_id=?", (product_id,))

    async def reset_watcher_notify(self, product_id: int):
        await self._execute("UPDATE watchlist SET notified=0 WHERE product_id=?", (product_id,))

    # ── flash sales ───────────────────────────────────────────────────────────

    async def create_flash_sale(self, product_id: int, discount_percent: int,
                                 original_price: int, start_time: str, end_time: str,
                                 notify_channel: int = None) -> int:
        sale_price = int(original_price * (1 - discount_percent / 100))
        return await self._execute(
            "INSERT INTO flash_sales (product_id, discount_percent, original_price, sale_price, start_time, end_time, notify_channel) VALUES (?,?,?,?,?,?,?)",
            (product_id, discount_percent, original_price, sale_price, start_time, end_time, notify_channel)
        )

    async def get_active_flash_sales(self) -> List[aiosqlite.Row]:
        now = datetime.now().isoformat()
        return await self._fetch_all(
            "SELECT fs.*, p.name as product_name FROM flash_sales fs JOIN products p ON fs.product_id=p.id WHERE fs.is_active=1 AND fs.start_time<=? AND fs.end_time>?",
            (now, now)
        )

    async def get_product_flash_sale(self, product_id: int) -> Optional[aiosqlite.Row]:
        now = datetime.now().isoformat()
        return await self._fetch_one(
            "SELECT * FROM flash_sales WHERE product_id=? AND is_active=1 AND start_time<=? AND end_time>?",
            (product_id, now, now)
        )

    async def get_upcoming_flash_sales(self) -> List[aiosqlite.Row]:
        now = datetime.now().isoformat()
        return await self._fetch_all(
            "SELECT fs.*, p.name as product_name FROM flash_sales fs JOIN products p ON fs.product_id=p.id WHERE fs.is_active=1 AND fs.start_time>? ORDER BY fs.start_time",
            (now,)
        )

    async def end_flash_sale(self, sale_id: int):
        await self._execute("UPDATE flash_sales SET is_active=0 WHERE id=?", (sale_id,))

    async def get_expired_flash_sales(self) -> List[aiosqlite.Row]:
        now = datetime.now().isoformat()
        return await self._fetch_all(
            "SELECT fs.*, p.name as product_name FROM flash_sales fs JOIN products p ON fs.product_id=p.id WHERE fs.is_active=1 AND fs.end_time<=?",
            (now,)
        )

    # ── ranks ─────────────────────────────────────────────────────────────────

    async def get_rank_tiers(self) -> List[aiosqlite.Row]:
        return await self._fetch_all("SELECT * FROM rank_tiers ORDER BY sort_order")

    async def get_rank_tier(self, tier_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one("SELECT * FROM rank_tiers WHERE id=?", (tier_id,))

    async def get_user_rank_tier(self, user_id: int) -> Optional[aiosqlite.Row]:
        user = await self.get_user(user_id)
        spent = user["total_spent"]
        return await self._fetch_one(
            "SELECT * FROM rank_tiers WHERE min_spent<=? ORDER BY min_spent DESC LIMIT 1",
            (spent,)
        )

    async def get_next_rank_tier(self, user_id: int) -> Optional[aiosqlite.Row]:
        user = await self.get_user(user_id)
        spent = user["total_spent"]
        return await self._fetch_one(
            "SELECT * FROM rank_tiers WHERE min_spent>? ORDER BY min_spent LIMIT 1",
            (spent,)
        )

    async def update_user_vip_tier(self, user_id: int) -> Optional[aiosqlite.Row]:
        """Returns the new tier if it changed, else None."""
        user = await self.get_user(user_id)
        tier = await self.get_user_rank_tier(user_id)
        if not tier:
            return None
        new_tier_id = tier["id"]
        if user["vip_tier"] != new_tier_id:
            await self._execute("UPDATE users SET vip_tier=? WHERE user_id=?", (new_tier_id, user_id))
            return tier
        return None

    async def update_rank_tier(self, tier_id: int, **kwargs):
        fields = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [tier_id]
        await self._execute(f"UPDATE rank_tiers SET {fields} WHERE id=?", tuple(vals))

    # ── achievements ──────────────────────────────────────────────────────────

    async def get_achievements(self, include_secret: bool = False) -> List[aiosqlite.Row]:
        if include_secret:
            return await self._fetch_all("SELECT * FROM achievements ORDER BY id")
        return await self._fetch_all("SELECT * FROM achievements WHERE is_secret=0 ORDER BY id")

    async def get_user_achievements(self, user_id: int) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT a.*, ua.earned_at FROM achievements a JOIN user_achievements ua ON a.id=ua.achievement_id WHERE ua.user_id=? ORDER BY ua.earned_at DESC",
            (user_id,)
        )

    async def grant_achievement(self, user_id: int, achievement_id: int) -> bool:
        existing = await self._fetch_one(
            "SELECT 1 FROM user_achievements WHERE user_id=? AND achievement_id=?",
            (user_id, achievement_id)
        )
        if existing:
            return False
        await self._execute(
            "INSERT INTO user_achievements (user_id, achievement_id) VALUES (?,?)",
            (user_id, achievement_id)
        )
        return True

    async def check_and_grant_achievements(self, user_id: int) -> List[aiosqlite.Row]:
        """Check all achievements for a user and grant new ones. Returns list of newly earned."""
        user = await self.get_user(user_id)
        all_achievements = await self.get_achievements(include_secret=True)
        earned = await self.get_user_achievements(user_id)
        earned_ids = {e["id"] for e in earned}

        purchase_count = (await self._fetch_one("SELECT COUNT(*) as c FROM orders WHERE user_id=? AND status='completed'", (user_id,)))["c"]
        review_count = (await self._fetch_one("SELECT COUNT(*) as c FROM reviews WHERE user_id=?", (user_id,)))["c"]
        referral_count = (await self._fetch_one("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (user_id,)))["c"]
        total_ach = (await self._fetch_one("SELECT COUNT(*) as c FROM achievements WHERE is_secret=0", ()))["c"]

        new_achievements = []
        for ach in all_achievements:
            if ach["id"] in earned_ids:
                continue
            ct = ach["condition_type"]
            cv = ach["condition_value"]
            met = False
            if ct == "purchase_count" and purchase_count >= cv:
                met = True
            elif ct == "total_spent" and user["total_spent"] >= cv:
                met = True
            elif ct == "review_count" and review_count >= cv:
                met = True
            elif ct == "daily_streak" and user["daily_streak"] >= cv:
                met = True
            elif ct == "referral_count" and referral_count >= cv:
                met = True
            elif ct == "all_achievements" and len(earned_ids) >= total_ach - 1:
                met = True

            if met:
                granted = await self.grant_achievement(user_id, ach["id"])
                if granted:
                    if ach["reward"] > 0:
                        await self.update_balance(user_id, ach["reward"])
                        await self.add_transaction(user_id, ach["reward"], "achievement", f"実績解除: {ach['name']}")
                    new_achievements.append(ach)

        return new_achievements

    # ── referrals ─────────────────────────────────────────────────────────────

    async def get_user_by_referral_code(self, code: str) -> Optional[aiosqlite.Row]:
        return await self._fetch_one("SELECT * FROM users WHERE referral_code=?", (code.upper(),))

    async def process_referral(self, referrer_id: int, referred_id: int) -> bool:
        existing = await self._fetch_one("SELECT 1 FROM referrals WHERE referred_id=?", (referred_id,))
        if existing:
            return False
        await self._execute(
            "INSERT INTO referrals (referrer_id, referred_id, reward_given) VALUES (?,?,1)",
            (referrer_id, referred_id)
        )
        reward = Config.REFERRAL_REWARD
        await self.update_balance(referrer_id, reward)
        await self.add_transaction(referrer_id, reward, "referral", f"紹介ボーナス (ID:{referred_id})")
        await self._execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, referred_id))
        return True

    # ── reviews ───────────────────────────────────────────────────────────────

    async def add_review(self, user_id: int, product_id: int, rating: int, comment: str) -> bool:
        try:
            await self._execute(
                "INSERT INTO reviews (user_id, product_id, rating, comment) VALUES (?,?,?,?)",
                (user_id, product_id, rating, comment)
            )
            return True
        except Exception:
            return False

    async def get_product_reviews(self, product_id: int) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM reviews WHERE product_id=? ORDER BY created_at DESC",
            (product_id,)
        )

    async def get_product_rating(self, product_id: int) -> Dict:
        row = await self._fetch_one(
            "SELECT COUNT(*) as count, AVG(rating) as avg FROM reviews WHERE product_id=?",
            (product_id,)
        )
        return {"count": row["count"], "avg": round(row["avg"] or 0, 1)}

    async def get_user_review(self, user_id: int, product_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one(
            "SELECT * FROM reviews WHERE user_id=? AND product_id=?",
            (user_id, product_id)
        )

    async def delete_review(self, review_id: int):
        await self._execute("DELETE FROM reviews WHERE id=?", (review_id,))

    # ── tickets ───────────────────────────────────────────────────────────────

    async def create_ticket(self, user_id: int, channel_id: int, subject: str, priority: str = "normal") -> int:
        return await self._execute(
            "INSERT INTO tickets (user_id, channel_id, subject, priority) VALUES (?,?,?,?)",
            (user_id, channel_id, subject, priority)
        )

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one("SELECT * FROM tickets WHERE channel_id=?", (channel_id,))

    async def get_user_open_ticket(self, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one(
            "SELECT * FROM tickets WHERE user_id=? AND status='open'",
            (user_id,)
        )

    async def get_open_tickets(self) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT * FROM tickets WHERE status='open' ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at"
        )

    async def close_ticket(self, ticket_id: int):
        await self._execute(
            "UPDATE tickets SET status='closed', closed_at=? WHERE id=?",
            (datetime.now().isoformat(), ticket_id)
        )

    # ── stats ─────────────────────────────────────────────────────────────────

    async def get_shop_stats(self) -> Dict:
        total_products = (await self._fetch_one("SELECT COUNT(*) as c FROM products WHERE is_available=1"))["c"]
        total_orders   = (await self._fetch_one("SELECT COUNT(*) as c FROM orders"))["c"]
        total_revenue  = (await self._fetch_one("SELECT COALESCE(SUM(total_price),0) as s FROM orders WHERE status='completed'"))["s"]
        total_users    = (await self._fetch_one("SELECT COUNT(*) as c FROM users"))["c"]
        pending_orders = (await self._fetch_one("SELECT COUNT(*) as c FROM orders WHERE status='pending'"))["c"]
        today_revenue  = (await self._fetch_one("SELECT COALESCE(SUM(total_price),0) as s FROM orders WHERE status='completed' AND date(created_at)=date('now')"))["s"]
        today_orders   = (await self._fetch_one("SELECT COUNT(*) as c FROM orders WHERE date(created_at)=date('now')"))["c"]
        top_product    = await self._fetch_one("SELECT name, sold_count FROM products ORDER BY sold_count DESC LIMIT 1")
        total_coupons_used = (await self._fetch_one("SELECT COUNT(*) as c FROM coupon_uses"))["c"]
        active_tickets = (await self._fetch_one("SELECT COUNT(*) as c FROM tickets WHERE status='open'"))["c"]
        return {
            "total_products": total_products,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "total_users": total_users,
            "pending_orders": pending_orders,
            "today_revenue": today_revenue,
            "today_orders": today_orders,
            "top_product": dict(top_product) if top_product else None,
            "total_coupons_used": total_coupons_used,
            "active_tickets": active_tickets,
        }

    async def get_revenue_by_day(self, days: int = 7) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT date(created_at) as day, COUNT(*) as order_count, SUM(total_price) as revenue FROM orders WHERE status='completed' AND created_at >= datetime('now', ?) GROUP BY date(created_at) ORDER BY day",
            (f"-{days} days",)
        )

    async def close(self):
        if self._conn:
            await self._conn.close()
