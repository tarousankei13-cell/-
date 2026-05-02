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
        await self._create_tables()
        await self._conn.commit()

    async def _create_tables(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                balance     INTEGER NOT NULL DEFAULT 0,
                total_spent INTEGER NOT NULL DEFAULT 0,
                is_banned   INTEGER NOT NULL DEFAULT 0,
                daily_last  TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                name         TEXT NOT NULL,
                description  TEXT NOT NULL DEFAULT '',
                price        INTEGER NOT NULL,
                stock        INTEGER NOT NULL DEFAULT -1,
                image_url    TEXT NOT NULL DEFAULT '',
                is_available INTEGER NOT NULL DEFAULT 1,
                sold_count   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cart_items (
                user_id    INTEGER NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity   INTEGER NOT NULL DEFAULT 1,
                added_at   TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                total_price INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                notes       TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id      INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                product_id    INTEGER,
                product_name  TEXT NOT NULL,
                product_price INTEGER NOT NULL,
                quantity      INTEGER NOT NULL,
                subtotal      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                rating     INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment    TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, product_id)
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE,
                subject    TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                closed_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                type        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_products_category   ON products(category_id);
            CREATE INDEX IF NOT EXISTS idx_cart_user           ON cart_items(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_user         ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_order_items_order   ON order_items(order_id);
            CREATE INDEX IF NOT EXISTS idx_reviews_product     ON reviews(product_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_user   ON transactions(user_id);
        """)

    # ── helpers ──────────────────────────────────────────────────────────────

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

    # ── users ────────────────────────────────────────────────────────────────

    async def get_user(self, user_id: int) -> aiosqlite.Row:
        row = await self._fetch_one("SELECT * FROM users WHERE user_id=?", (user_id,))
        if row is None:
            await self._execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, Config.STARTING_BALANCE)
            )
            await self.add_transaction(user_id, Config.STARTING_BALANCE, "welcome", "新規登録ボーナス")
            row = await self._fetch_one("SELECT * FROM users WHERE user_id=?", (user_id,))
        return row

    async def update_balance(self, user_id: int, amount: int) -> bool:
        await self.get_user(user_id)
        row = await self._fetch_one("SELECT balance FROM users WHERE user_id=?", (user_id,))
        if row["balance"] + amount < 0:
            return False
        await self._execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (amount, user_id)
        )
        return True

    async def get_balance_rank(self) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT user_id, balance, total_spent FROM users ORDER BY balance DESC LIMIT 10"
        )

    async def ban_user(self, user_id: int, banned: bool = True):
        await self.get_user(user_id)
        await self._execute("UPDATE users SET is_banned=? WHERE user_id=?", (int(banned), user_id))

    async def claim_daily(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        today = date.today().isoformat()
        if user["daily_last"] == today:
            return False
        await self._execute(
            "UPDATE users SET daily_last=?, balance=balance+? WHERE user_id=?",
            (today, Config.DAILY_REWARD, user_id)
        )
        await self.add_transaction(user_id, Config.DAILY_REWARD, "daily", "デイリーボーナス")
        return True

    # ── transactions ─────────────────────────────────────────────────────────

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

    # ── categories ───────────────────────────────────────────────────────────

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

    # ── products ─────────────────────────────────────────────────────────────

    async def get_products(self, category_id: Optional[int] = None, available_only: bool = True) -> List[aiosqlite.Row]:
        if category_id:
            sql = "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE p.category_id=?"
            params: tuple = (category_id,)
            if available_only:
                sql += " AND p.is_available=1 AND (p.stock=-1 OR p.stock>0)"
        else:
            sql = "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE 1=1"
            params = ()
            if available_only:
                sql += " AND p.is_available=1 AND (p.stock=-1 OR p.stock>0)"
        sql += " ORDER BY p.sold_count DESC, p.id"
        return await self._fetch_all(sql, params)

    async def get_product(self, product_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one(
            "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE p.id=?",
            (product_id,)
        )

    async def search_products(self, query: str) -> List[aiosqlite.Row]:
        q = f"%{query}%"
        return await self._fetch_all(
            "SELECT p.*, c.name as cat_name, c.emoji as cat_emoji FROM products p JOIN categories c ON p.category_id=c.id WHERE (p.name LIKE ? OR p.description LIKE ?) AND p.is_available=1 ORDER BY p.sold_count DESC LIMIT 25",
            (q, q)
        )

    async def add_product(self, category_id: int, name: str, description: str, price: int, stock: int, image_url: str) -> int:
        return await self._execute(
            "INSERT INTO products (category_id, name, description, price, stock, image_url) VALUES (?,?,?,?,?,?)",
            (category_id, name, description, price, stock, image_url)
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
            return True
        if row["stock"] < quantity:
            return False
        await self._execute(
            "UPDATE products SET stock=stock-?, sold_count=sold_count+?, updated_at=? WHERE id=?",
            (quantity, quantity, datetime.now().isoformat(), product_id)
        )
        return True

    # ── cart ─────────────────────────────────────────────────────────────────

    async def get_cart(self, user_id: int) -> List[aiosqlite.Row]:
        return await self._fetch_all(
            "SELECT ci.*, p.name, p.price, p.image_url, p.stock, p.is_available, (p.stock=-1 OR p.stock>=ci.quantity) as in_stock FROM cart_items ci JOIN products p ON ci.product_id=p.id WHERE ci.user_id=? ORDER BY ci.added_at",
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
        item_count = sum(1 for _ in cart)
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

    # ── orders ───────────────────────────────────────────────────────────────

    async def create_order(self, user_id: int, items: List[Dict], total: int, notes: str) -> int:
        order_id = await self._execute(
            "INSERT INTO orders (user_id, total_price, notes) VALUES (?,?,?)",
            (user_id, total, notes)
        )
        for item in items:
            await self._execute(
                "INSERT INTO order_items (order_id, product_id, product_name, product_price, quantity, subtotal) VALUES (?,?,?,?,?,?)",
                (order_id, item["product_id"], item["name"], item["price"], item["quantity"], item["price"] * item["quantity"])
            )
        await self._execute(
            "UPDATE users SET total_spent=total_spent+? WHERE user_id=?",
            (total, user_id)
        )
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

    async def get_all_orders(self, status: Optional[str] = None, limit: int = 50) -> List[aiosqlite.Row]:
        if status:
            return await self._fetch_all(
                "SELECT o.*, u.balance FROM orders o LEFT JOIN users u ON o.user_id=u.user_id WHERE o.status=? ORDER BY o.created_at DESC LIMIT ?",
                (status, limit)
            )
        return await self._fetch_all(
            "SELECT o.* FROM orders o ORDER BY o.created_at DESC LIMIT ?",
            (limit,)
        )

    async def update_order_status(self, order_id: int, status: str):
        await self._execute(
            "UPDATE orders SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now().isoformat(), order_id)
        )

    # ── reviews ──────────────────────────────────────────────────────────────

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

    # ── tickets ──────────────────────────────────────────────────────────────

    async def create_ticket(self, user_id: int, channel_id: int, subject: str) -> int:
        return await self._execute(
            "INSERT INTO tickets (user_id, channel_id, subject) VALUES (?,?,?)",
            (user_id, channel_id, subject)
        )

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one("SELECT * FROM tickets WHERE channel_id=?", (channel_id,))

    async def get_user_open_ticket(self, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetch_one(
            "SELECT * FROM tickets WHERE user_id=? AND status='open'",
            (user_id,)
        )

    async def get_open_tickets(self) -> List[aiosqlite.Row]:
        return await self._fetch_all("SELECT * FROM tickets WHERE status='open' ORDER BY created_at")

    async def close_ticket(self, ticket_id: int):
        await self._execute(
            "UPDATE tickets SET status='closed', closed_at=? WHERE id=?",
            (datetime.now().isoformat(), ticket_id)
        )

    # ── stats ────────────────────────────────────────────────────────────────

    async def get_shop_stats(self) -> Dict:
        total_products = (await self._fetch_one("SELECT COUNT(*) as c FROM products WHERE is_available=1"))["c"]
        total_orders   = (await self._fetch_one("SELECT COUNT(*) as c FROM orders"))["c"]
        total_revenue  = (await self._fetch_one("SELECT COALESCE(SUM(total_price),0) as s FROM orders WHERE status='completed'"))["s"]
        total_users    = (await self._fetch_one("SELECT COUNT(*) as c FROM users"))["c"]
        pending_orders = (await self._fetch_one("SELECT COUNT(*) as c FROM orders WHERE status='pending'"))["c"]
        top_product    = await self._fetch_one("SELECT name, sold_count FROM products ORDER BY sold_count DESC LIMIT 1")
        return {
            "total_products": total_products,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "total_users": total_users,
            "pending_orders": pending_orders,
            "top_product": dict(top_product) if top_product else None,
        }

    async def close(self):
        if self._conn:
            await self._conn.close()
