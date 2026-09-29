"""SQLite 永続化層。

残高は「台帳(ledger)の合計」を正とし、users.balance はその写像として持つ。
両者がズレたら /mcd audit で検出できる。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import string
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

JST = timezone(timedelta(hours=9))

# 台帳の種別
K_CHARGE = "charge"            # Kyash チャージ
K_ORDER = "order"              # 注文の支払い (マイナス)
K_REFUND = "refund"            # 決済失敗の払い戻し
K_REFERRAL = "referral"        # 紹介報酬
K_PHOTO = "photo_bonus"        # 実績に画像を添えた報酬
K_BONUS = "charge_bonus"       # まとめてチャージした際のボーナス
K_ADJUST = "adjust"            # オーナーによる手動調整

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id              INTEGER PRIMARY KEY,
    balance              INTEGER NOT NULL DEFAULT 0,
    referral_code        TEXT UNIQUE,
    referred_by          INTEGER,
    referral_reward_paid INTEGER NOT NULL DEFAULT 0,
    terms_version        TEXT,
    terms_agreed_at      TEXT,
    total_orders         INTEGER NOT NULL DEFAULT 0,
    total_face           INTEGER NOT NULL DEFAULT 0,
    total_paid           INTEGER NOT NULL DEFAULT 0,
    banned               INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    amount        INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    ref           TEXT,
    memo          TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, created_at);

CREATE TABLE IF NOT EXISTS mcd_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL UNIQUE,
    refresh_token TEXT NOT NULL,
    card_id       TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_used_at  TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kyash_accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    label             TEXT NOT NULL UNIQUE,
    email             TEXT,
    access_token      TEXT NOT NULL,
    client_uuid       TEXT,
    installation_uuid TEXT,
    token_obtained_at TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1,
    fail_count        INTEGER NOT NULL DEFAULT 0,
    last_used_at      TEXT,
    last_error        TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id       TEXT    NOT NULL,
    user_id        INTEGER NOT NULL,
    mcd_account_id INTEGER,
    hex_sha256     TEXT    NOT NULL UNIQUE,
    raw_hex        TEXT,
    store_id       TEXT,
    store_name     TEXT,
    pickup         TEXT,
    face_amount    INTEGER NOT NULL,
    rate           INTEGER NOT NULL,
    paid_amount    INTEGER NOT NULL,
    item_count     INTEGER NOT NULL DEFAULT 0,
    receipt_number TEXT,
    short_code     TEXT,
    order_token    TEXT,
    group_name     TEXT,
    status         TEXT    NOT NULL,
    report_status  TEXT    NOT NULL DEFAULT 'none',
    error          TEXT,
    created_at     TEXT    NOT NULL,
    paid_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_report ON orders(report_status);

CREATE TABLE IF NOT EXISTS charges (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    kyash_account_id INTEGER,
    link_uuid        TEXT    NOT NULL UNIQUE,
    amount           INTEGER NOT NULL,
    sender_name      TEXT,
    sender_public_id TEXT,
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_charges_sender ON charges(sender_public_id);

CREATE TABLE IF NOT EXISTS reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    content           TEXT    NOT NULL,
    image_count       INTEGER NOT NULL DEFAULT 0,
    image_hashes      TEXT,
    status            TEXT    NOT NULL,
    approved_by       INTEGER,
    reject_reason     TEXT,
    source_channel_id INTEGER,
    source_message_id INTEGER,
    approval_message_id INTEGER,
    approval_channel_id INTEGER,
    posted_message_id INTEGER,
    photo_bonus_paid  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    decided_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);

CREATE TABLE IF NOT EXISTS image_hashes (
    phash      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    report_id  INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fraud_flags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    score      INTEGER NOT NULL,
    detail     TEXT,
    resolved   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fraud_user ON fraud_flags(user_id, resolved);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   TEXT,
    actor_id   INTEGER,
    event      TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event, created_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_presets (
    user_id    INTEGER NOT NULL,
    store_id   TEXT    NOT NULL,
    store_name TEXT,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, store_id)
);
"""


def now_jst() -> datetime:
    return datetime.now(JST)


def ts() -> str:
    return now_jst().isoformat(timespec="seconds")


class InsufficientBalance(Exception):
    def __init__(self, balance: int, required: int):
        self.balance = balance
        self.required = required
        self.shortfall = required - balance
        super().__init__(f"残高不足: {balance} < {required}")


class DuplicateHex(Exception):
    pass


class DuplicateLink(Exception):
    pass


class Store:
    """スレッドセーフな SQLite ラッパー。

    discord.py のイベントループを塞がないよう、呼び出し側は
    ``asyncio.to_thread`` 経由で叩くこと。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """古い DB に足りない列を足す。"""
        wanted = {
            "orders": [("raw_hex", "TEXT")],
            "reports": [
                ("approval_message_id", "INTEGER"),
                ("approval_channel_id", "INTEGER"),
            ],
        }
        for table, columns in wanted.items():
            have = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, ddl in columns:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ バックアップ

    def backup_dir(self) -> Path:
        directory = self.path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _new_backup_path(self, prefix: str = "bot") -> Path:
        """まだ存在しないファイル名を作る。

        秒までの時刻だけだと同じ秒に2回取ったときに衝突し、
        復元元にしようとしたファイルを上書きしてしまう。
        """
        base = self.backup_dir()
        stamp = now_jst().strftime("%Y%m%d-%H%M%S")
        candidate = base / f"{prefix}-{stamp}.sqlite3"
        counter = 1
        while candidate.exists():
            candidate = base / f"{prefix}-{stamp}-{counter}.sqlite3"
            counter += 1
        return candidate

    def backup(self, keep: int = 14, prefix: str = "bot") -> Path:
        """SQLite の backup API で安全にコピーを取る。

        ファイルを直接コピーすると書き込み途中の状態を掴む恐れがあるため、
        接続経由でバックアップする。
        """
        target = self._new_backup_path(prefix)
        with self._lock:
            dest = sqlite3.connect(str(target))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        if prefix == "bot":
            self.prune_backups(keep)
        return target

    def list_backups(self) -> list[Path]:
        """新しい順。定期バックアップと復元直前の保存の両方を含む。"""
        files = list(self.backup_dir().glob("bot-*.sqlite3"))
        files += list(self.backup_dir().glob("pre-restore-*.sqlite3"))
        return sorted(files, key=lambda p: p.name, reverse=True)

    def prune_backups(self, keep: int) -> int:
        """定期バックアップだけを間引く。復元直前の保存は残す。"""
        files = sorted(
            self.backup_dir().glob("bot-*.sqlite3"), key=lambda p: p.name, reverse=True
        )
        removed = 0
        for old in files[max(0, keep):]:
            try:
                old.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def restore(self, backup_path: str | Path) -> None:
        """バックアップの内容を現在の DB に書き戻す。

        接続は張ったまま中身だけ差し替えるので、呼び出し後は
        キャッシュしている設定などを読み直すこと。
        """
        import shutil
        import uuid as _uuid

        source_path = Path(backup_path)
        if not source_path.exists():
            raise FileNotFoundError(str(source_path))

        # 復元元を先に退避する。この後に取る「復元前の保存」が
        # 万一同名になっても、元データを失わないようにするため。
        staged = self.backup_dir() / f".restoring-{_uuid.uuid4().hex[:8]}.sqlite3"
        shutil.copy2(source_path, staged)

        try:
            # 戻す前に今の状態も保存しておく（誤復元からの復帰用）
            self.backup(prefix="pre-restore")
            with self._lock:
                source = sqlite3.connect(str(staged))
                try:
                    source.backup(self._conn)
                finally:
                    source.close()
                self._conn.commit()
        finally:
            staged.unlink(missing_ok=True)

    @contextmanager
    def tx(self):
        """書き込みトランザクション。例外が出たら丸ごとロールバックする。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchall()

    def _q1(self, sql: str, args: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ kv

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self._q1("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_kv(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # --------------------------------------------------------------- users

    def _new_referral_code(self, conn: sqlite3.Connection) -> str:
        alphabet = string.ascii_uppercase + string.digits
        # 紛らわしい文字を除く
        alphabet = "".join(ch for ch in alphabet if ch not in "O0I1L")
        for _ in range(50):
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            if not conn.execute(
                "SELECT 1 FROM users WHERE referral_code=?", (code,)
            ).fetchone():
                return code
        raise RuntimeError("紹介コードを生成できませんでした")

    def ensure_user(self, user_id: int) -> sqlite3.Row:
        row = self._q1("SELECT * FROM users WHERE user_id=?", (user_id,))
        if row:
            return row
        with self.tx() as c:
            code = self._new_referral_code(c)
            c.execute(
                "INSERT OR IGNORE INTO users(user_id,referral_code,created_at) VALUES(?,?,?)",
                (user_id, code, ts()),
            )
        return self._q1("SELECT * FROM users WHERE user_id=?", (user_id,))

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return self._q1("SELECT * FROM users WHERE user_id=?", (user_id,))

    def user_by_referral_code(self, code: str) -> Optional[sqlite3.Row]:
        return self._q1("SELECT * FROM users WHERE referral_code=?", (code.strip().upper(),))

    def balance(self, user_id: int) -> int:
        row = self.get_user(user_id)
        return int(row["balance"]) if row else 0

    def agree_terms(self, user_id: int, version: str) -> None:
        self.ensure_user(user_id)
        with self.tx() as c:
            c.execute(
                "UPDATE users SET terms_version=?, terms_agreed_at=? WHERE user_id=?",
                (version, ts(), user_id),
            )

    def set_referrer(self, user_id: int, referrer_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
                (referrer_id, user_id),
            )

    def mark_referral_paid(self, invitee_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET referral_reward_paid=1 WHERE user_id=?", (invitee_id,)
            )

    def count_referred(self, user_id: int, only_paid: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM users WHERE referred_by=?"
        if only_paid:
            sql += " AND referral_reward_paid=1"
        return int(self._q(sql, (user_id,))[0]["n"])

    def set_banned(self, user_id: int, banned: bool) -> None:
        self.ensure_user(user_id)
        with self.tx() as c:
            c.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, user_id))

    # -------------------------------------------------------------- ledger

    def _apply(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        kind: str,
        amount: int,
        ref: str = "",
        memo: str = "",
        allow_negative: bool = False,
    ) -> int:
        """トランザクション内で残高を動かし、台帳に1行足す。戻り値は新残高。"""
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            code = self._new_referral_code(conn)
            conn.execute(
                "INSERT INTO users(user_id,referral_code,created_at) VALUES(?,?,?)",
                (user_id, code, ts()),
            )
            current = 0
        else:
            current = int(row["balance"])

        new_balance = current + amount
        if new_balance < 0 and not allow_negative:
            raise InsufficientBalance(current, -amount)

        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, user_id))
        conn.execute(
            "INSERT INTO ledger(user_id,kind,amount,balance_after,ref,memo,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, kind, amount, new_balance, ref, memo, ts()),
        )
        return new_balance

    def credit(self, user_id: int, kind: str, amount: int, ref: str = "", memo: str = "") -> int:
        if amount <= 0:
            raise ValueError("credit の amount は正の数であること")
        with self.tx() as c:
            return self._apply(c, user_id, kind, amount, ref, memo)

    def debit(self, user_id: int, kind: str, amount: int, ref: str = "", memo: str = "") -> int:
        if amount <= 0:
            raise ValueError("debit の amount は正の数であること")
        with self.tx() as c:
            return self._apply(c, user_id, kind, -amount, ref, memo)

    def adjust(self, user_id: int, amount: int, memo: str = "") -> int:
        """オーナーによる手動調整。マイナス残高も許可する。"""
        with self.tx() as c:
            return self._apply(c, user_id, K_ADJUST, amount, memo=memo, allow_negative=True)

    def ledger_of(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        return self._q(
            "SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )

    def audit_balances(self) -> list[dict]:
        """台帳の合計と users.balance がズレている利用者を返す。"""
        rows = self._q(
            "SELECT u.user_id, u.balance, COALESCE(SUM(l.amount),0) AS ledger_sum "
            "FROM users u LEFT JOIN ledger l ON l.user_id=u.user_id GROUP BY u.user_id"
        )
        return [
            {"user_id": r["user_id"], "balance": r["balance"], "ledger_sum": r["ledger_sum"]}
            for r in rows
            if int(r["balance"]) != int(r["ledger_sum"])
        ]

    # ------------------------------------------------------------ accounts

    def add_mcd_account(self, label: str, refresh_token: str, card_id: str = "") -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO mcd_accounts(label,refresh_token,card_id,created_at) VALUES(?,?,?,?)",
                (label, refresh_token, card_id, ts()),
            )
            return int(cur.lastrowid)

    def add_kyash_account(
        self,
        label: str,
        access_token: str,
        email: str = "",
        client_uuid: str = "",
        installation_uuid: str = "",
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO kyash_accounts"
                "(label,email,access_token,client_uuid,installation_uuid,token_obtained_at,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (label, email, access_token, client_uuid, installation_uuid, ts(), ts()),
            )
            return int(cur.lastrowid)

    def list_mcd_accounts(self, only_enabled: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mcd_accounts"
        if only_enabled:
            sql += " WHERE enabled=1"
        return self._q(sql + " ORDER BY id")

    def list_kyash_accounts(self, only_enabled: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM kyash_accounts"
        if only_enabled:
            sql += " WHERE enabled=1"
        return self._q(sql + " ORDER BY id")

    def update_mcd_account(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE mcd_accounts SET {cols} WHERE id=?", (*fields.values(), account_id)
            )

    def update_kyash_account(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE kyash_accounts SET {cols} WHERE id=?", (*fields.values(), account_id)
            )

    def delete_mcd_account(self, account_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM mcd_accounts WHERE id=?", (account_id,))

    def delete_kyash_account(self, account_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM kyash_accounts WHERE id=?", (account_id,))

    # -------------------------------------------------------------- orders

    def reserve_order(
        self,
        trace_id: str,
        user_id: int,
        hex_sha256: str,
        store_id: str,
        store_name: str,
        pickup: str,
        face_amount: int,
        rate: int,
        paid_amount: int,
        item_count: int,
        raw_hex: str = "",
    ) -> int:
        """決済「前」に枠を押さえる。同じ hex の二重決済はここで弾かれる。"""
        with self.tx() as c:
            dup = c.execute(
                "SELECT id FROM orders WHERE hex_sha256=?", (hex_sha256,)
            ).fetchone()
            if dup:
                raise DuplicateHex(f"この注文コードは既に使用されています (order #{dup['id']})")
            cur = c.execute(
                "INSERT INTO orders(trace_id,user_id,hex_sha256,raw_hex,store_id,store_name,"
                "pickup,face_amount,rate,paid_amount,item_count,status,report_status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending','none',?)",
                (
                    trace_id, user_id, hex_sha256, raw_hex, store_id, store_name, pickup,
                    face_amount, rate, paid_amount, item_count, ts(),
                ),
            )
            return int(cur.lastrowid)

    def finish_order_ok(
        self,
        order_id: int,
        mcd_account_id: int,
        receipt_number: str,
        short_code: str,
        order_token: str,
        group_name: str,
    ) -> None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"注文 #{order_id} が見つかりません")
            c.execute(
                "UPDATE orders SET status='paid', report_status='awaiting', raw_hex=NULL, mcd_account_id=?,"
                " receipt_number=?, short_code=?, order_token=?, group_name=?, paid_at=? "
                "WHERE id=?",
                (mcd_account_id, receipt_number, short_code, order_token, group_name, ts(), order_id),
            )
            c.execute(
                "UPDATE users SET total_orders=total_orders+1, total_face=total_face+?,"
                " total_paid=total_paid+? WHERE user_id=?",
                (int(row["face_amount"]), int(row["paid_amount"]), int(row["user_id"])),
            )

    def finish_order_failed(self, order_id: int, error: str, unknown: bool = False) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE orders SET status=?, error=? WHERE id=?",
                ("unknown" if unknown else "failed", error[:500], order_id),
            )

    def release_hex(self, order_id: int) -> None:
        """注文コードを再利用できる状態に戻す。

        まだ決済を試みていない (pending) 行はそのまま消す。
        既に失敗として記録済みの行は、履歴を残したいので消さず、
        hex_sha256 だけ一意な番兵に差し替えて UNIQUE 制約から外す。
        paid / unknown は二重決済になるので絶対に解放しない。
        """
        with self.tx() as c:
            row = c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                return
            if row["status"] == "pending":
                c.execute("DELETE FROM orders WHERE id=?", (order_id,))
            elif row["status"] == "failed":
                c.execute(
                    "UPDATE orders SET hex_sha256=?, raw_hex=NULL WHERE id=?",
                    (f"released:{order_id}", order_id),
                )

    def stale_pending_orders(self, older_than: datetime) -> list[sqlite3.Row]:
        return self._q(
            "SELECT * FROM orders WHERE status='pending' AND created_at < ?",
            (older_than.isoformat(timespec="seconds"),),
        )

    def get_order(self, order_id: int) -> Optional[sqlite3.Row]:
        return self._q1("SELECT * FROM orders WHERE id=?", (order_id,))

    def orders_of(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._q(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )

    def blocking_order(self, user_id: int) -> Optional[sqlite3.Row]:
        """感想が未完了で、次の注文をブロックしている注文を返す。"""
        return self._q1(
            "SELECT * FROM orders WHERE user_id=? AND status='paid' "
            "AND report_status IN ('awaiting','submitted','rejected') "
            "ORDER BY id ASC LIMIT 1",
            (user_id,),
        )

    def set_report_status(self, order_id: int, status: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE orders SET report_status=? WHERE id=?", (status, order_id))

    def count_orders_since(self, user_id: int, since: datetime) -> int:
        row = self._q1(
            "SELECT COUNT(*) AS n FROM orders WHERE user_id=? AND created_at>=?",
            (user_id, since.isoformat(timespec="seconds")),
        )
        return int(row["n"]) if row else 0

    def monthly_orders(self, user_id: int, year: int, month: int) -> list[sqlite3.Row]:
        prefix = f"{year:04d}-{month:02d}"
        return self._q(
            "SELECT * FROM orders WHERE user_id=? AND status='paid' AND paid_at LIKE ? "
            "ORDER BY id",
            (user_id, prefix + "%"),
        )

    # ------------------------------------------------------------- charges

    def record_charge(
        self,
        user_id: int,
        kyash_account_id: int,
        link_uuid: str,
        amount: int,
        sender_name: str,
        sender_public_id: str,
    ) -> tuple[int, int]:
        """チャージを記録し残高に反映する。戻り値は (charge_id, 新残高)。"""
        with self.tx() as c:
            dup = c.execute(
                "SELECT id FROM charges WHERE link_uuid=?", (link_uuid,)
            ).fetchone()
            if dup:
                raise DuplicateLink("このリンクは既に受け取り済みです")
            cur = c.execute(
                "INSERT INTO charges(user_id,kyash_account_id,link_uuid,amount,"
                "sender_name,sender_public_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, kyash_account_id, link_uuid, amount, sender_name, sender_public_id, ts()),
            )
            charge_id = int(cur.lastrowid)
            new_balance = self._apply(
                c, user_id, K_CHARGE, amount, ref=f"charge:{charge_id}",
                memo=f"Kyash {sender_name}",
            )
            return charge_id, new_balance

    def reserve_link(self, link_uuid: str) -> bool:
        """link_uuid を先に押さえる。既にあれば False。"""
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO charges(user_id,kyash_account_id,link_uuid,amount,created_at) "
                    "VALUES(0,0,?,0,?)",
                    (link_uuid, ts()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def fill_reserved_link(
        self,
        link_uuid: str,
        user_id: int,
        kyash_account_id: int,
        amount: int,
        sender_name: str,
        sender_public_id: str,
    ) -> tuple[int, int]:
        with self.tx() as c:
            row = c.execute("SELECT id FROM charges WHERE link_uuid=?", (link_uuid,)).fetchone()
            if row is None:
                raise DuplicateLink("予約されていないリンクです")
            charge_id = int(row["id"])
            c.execute(
                "UPDATE charges SET user_id=?, kyash_account_id=?, amount=?, sender_name=?,"
                " sender_public_id=? WHERE id=?",
                (user_id, kyash_account_id, amount, sender_name, sender_public_id, charge_id),
            )
            new_balance = self._apply(
                c, user_id, K_CHARGE, amount, ref=f"charge:{charge_id}",
                memo=f"Kyash {sender_name}",
            )
            return charge_id, new_balance

    def drop_reserved_link(self, link_uuid: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM charges WHERE link_uuid=? AND user_id=0", (link_uuid,))

    def drop_stale_reservations(self, older_than: datetime) -> int:
        """受け取りに失敗したまま残った予約行を消す。

        予約直後にプロセスが落ちると user_id=0 の行が残り、そのリンクが
        永久に使えなくなるため、一定時間たったものは掃除する。
        """
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM charges WHERE user_id=0 AND created_at < ?",
                (older_than.isoformat(timespec="seconds"),),
            )
            return int(cur.rowcount or 0)

    def users_sharing_sender(self, sender_public_id: str) -> list[int]:
        if not sender_public_id:
            return []
        rows = self._q(
            "SELECT DISTINCT user_id FROM charges WHERE sender_public_id=? AND user_id<>0",
            (sender_public_id,),
        )
        return [int(r["user_id"]) for r in rows]

    def senders_of_user(self, user_id: int) -> list[str]:
        rows = self._q(
            "SELECT DISTINCT sender_public_id FROM charges "
            "WHERE user_id=? AND sender_public_id IS NOT NULL AND sender_public_id<>''",
            (user_id,),
        )
        return [r["sender_public_id"] for r in rows]

    # ------------------------------------------------------------- reports

    def create_report(
        self,
        order_id: int,
        user_id: int,
        content: str,
        image_count: int,
        image_hashes: list[str],
        source_channel_id: int,
        source_message_id: int,
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO reports(order_id,user_id,content,image_count,image_hashes,status,"
                "source_channel_id,source_message_id,created_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
                (
                    order_id, user_id, content, image_count,
                    json.dumps(image_hashes, ensure_ascii=False),
                    source_channel_id, source_message_id, ts(),
                ),
            )
            c.execute("UPDATE orders SET report_status='submitted' WHERE id=?", (order_id,))
            return int(cur.lastrowid)

    def get_report(self, report_id: int) -> Optional[sqlite3.Row]:
        return self._q1("SELECT * FROM reports WHERE id=?", (report_id,))

    def pending_reports(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM reports WHERE status='pending' ORDER BY id")

    def decide_report(
        self, report_id: int, approved: bool, actor_id: int, reason: str = ""
    ) -> None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            c.execute(
                "UPDATE reports SET status=?, approved_by=?, reject_reason=?, decided_at=? WHERE id=?",
                ("approved" if approved else "rejected", actor_id, reason, ts(), report_id),
            )
            c.execute(
                "UPDATE orders SET report_status=? WHERE id=?",
                ("approved" if approved else "rejected", int(row["order_id"])),
            )

    def mark_photo_bonus_paid(self, report_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE reports SET photo_bonus_paid=1 WHERE id=?", (report_id,))

    def set_report_message(self, report_id: int, message_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE reports SET posted_message_id=? WHERE id=?", (message_id, report_id))

    def set_report_approval_message(
        self, report_id: int, channel_id: int, message_id: int
    ) -> None:
        """承認待ち投稿の場所を覚えておく。

        BOT が再起動するとメモリ上の画像が消えるため、承認時はここから
        添付を取り直す。
        """
        with self.tx() as c:
            c.execute(
                "UPDATE reports SET approval_channel_id=?, approval_message_id=? WHERE id=?",
                (channel_id, message_id, report_id),
            )

    def count_reports_since(self, user_id: int, since: datetime) -> int:
        row = self._q1(
            "SELECT COUNT(*) AS n FROM reports WHERE user_id=? AND created_at>=?",
            (user_id, since.isoformat(timespec="seconds")),
        )
        return int(row["n"]) if row else 0

    # -------------------------------------------------------- image hashes

    def seen_image_hash(self, phash: str) -> Optional[sqlite3.Row]:
        return self._q1("SELECT * FROM image_hashes WHERE phash=?", (phash,))

    def remember_image_hash(self, phash: str, user_id: int, report_id: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO image_hashes(phash,user_id,report_id,created_at) "
                "VALUES(?,?,?,?)",
                (phash, user_id, report_id, ts()),
            )

    def all_image_hashes(self) -> list[sqlite3.Row]:
        return self._q("SELECT phash, user_id FROM image_hashes")

    # ---------------------------------------------------------------- 不正

    def add_fraud_flag(self, user_id: int, kind: str, score: int, detail: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO fraud_flags(user_id,kind,score,detail,created_at) VALUES(?,?,?,?,?)",
                (user_id, kind, score, detail[:900], ts()),
            )
            return int(cur.lastrowid)

    def open_fraud_flags(self, user_id: Optional[int] = None) -> list[sqlite3.Row]:
        if user_id is None:
            return self._q("SELECT * FROM fraud_flags WHERE resolved=0 ORDER BY id DESC")
        return self._q(
            "SELECT * FROM fraud_flags WHERE resolved=0 AND user_id=? ORDER BY id DESC", (user_id,)
        )

    def resolve_fraud_flag(self, flag_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE fraud_flags SET resolved=1 WHERE id=?", (flag_id,))

    # ----------------------------------------------------------- audit log

    def audit(self, event: str, actor_id: int = 0, detail: Any = None, trace_id: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_log(trace_id,actor_id,event,detail,created_at) VALUES(?,?,?,?,?)",
                (
                    trace_id, actor_id, event,
                    json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None,
                    ts(),
                ),
            )

    def recent_audit(self, limit: int = 20, event_like: str = "") -> list[sqlite3.Row]:
        if event_like:
            return self._q(
                "SELECT * FROM audit_log WHERE event LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{event_like}%", limit),
            )
        return self._q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # ----------------------------------------------------------- 集計まわり

    def dashboard_totals(self) -> dict:
        held = self._q1("SELECT COALESCE(SUM(balance),0) AS v FROM users")["v"]
        users = self._q1("SELECT COUNT(*) AS v FROM users")["v"]
        today = now_jst().strftime("%Y-%m-%d")
        month = now_jst().strftime("%Y-%m")

        def agg(prefix: str) -> dict:
            row = self._q1(
                "SELECT COUNT(*) AS n, COALESCE(SUM(face_amount),0) AS face, "
                "COALESCE(SUM(paid_amount),0) AS paid FROM orders "
                "WHERE status='paid' AND paid_at LIKE ?",
                (prefix + "%",),
            )
            face, paid = int(row["face"]), int(row["paid"])
            return {"count": int(row["n"]), "face": face, "paid": paid, "burden": face - paid}

        return {
            "held_balance": int(held),
            "users": int(users),
            "today": agg(today),
            "month": agg(month),
            "pending_reports": len(self.pending_reports()),
            "open_flags": len(self.open_fraud_flags()),
        }

    def leaderboard(self, year: int, month: int, limit: int = 10) -> list[dict]:
        prefix = f"{year:04d}-{month:02d}"
        rows = self._q(
            "SELECT user_id, COUNT(*) AS n, COALESCE(SUM(paid_amount),0) AS paid, "
            "COALESCE(SUM(face_amount),0) AS face FROM orders "
            "WHERE status='paid' AND paid_at LIKE ? GROUP BY user_id ORDER BY n DESC, paid DESC "
            "LIMIT ?",
            (prefix + "%", limit),
        )
        return [dict(r) for r in rows]

    def active_user_ids(self) -> list[int]:
        return [int(r["user_id"]) for r in self._q("SELECT user_id FROM users WHERE banned=0")]

    def daily_summary(self, day: str) -> dict:
        """指定日(YYYY-MM-DD)の注文を集計する。"""
        row = self._q1(
            "SELECT COUNT(*) AS n, COALESCE(SUM(face_amount),0) AS face, "
            "COALESCE(SUM(paid_amount),0) AS paid FROM orders "
            "WHERE status='paid' AND paid_at LIKE ?",
            (day + "%",),
        )
        failed = self._q1(
            "SELECT COUNT(*) AS n FROM orders WHERE status IN ('failed','unknown') "
            "AND created_at LIKE ?",
            (day + "%",),
        )
        charged = self._q1(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM charges "
            "WHERE user_id<>0 AND created_at LIKE ?",
            (day + "%",),
        )
        face, paid = int(row["face"]), int(row["paid"])
        return {
            "day": day,
            "orders": int(row["n"]),
            "face": face,
            "paid": paid,
            "burden": face - paid,
            "failed": int(failed["n"]),
            "charges": int(charged["n"]),
            "charged_total": int(charged["total"]),
        }

    # -------------------------------------------- アカウント別の利用状況

    def account_usage(self, account_id: int, day: str) -> dict:
        """その日にそのアカウントで通した注文の件数と金額。"""
        row = self._q1(
            "SELECT COUNT(*) AS n, COALESCE(SUM(face_amount),0) AS face FROM orders "
            "WHERE status='paid' AND mcd_account_id=? AND paid_at LIKE ?",
            (account_id, day + "%"),
        )
        return {"count": int(row["n"]), "amount": int(row["face"])}

    # ------------------------------------------------------- 決済成否不明

    def unknown_orders(self) -> list[sqlite3.Row]:
        """課金されたか確認できていない注文。"""
        return self._q("SELECT * FROM orders WHERE status='unknown' ORDER BY id")

    def resolve_unknown(self, order_id: int, paid: bool, note: str = "") -> None:
        """照合の結果を反映する。paid=False なら失敗として扱う。"""
        with self.tx() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if row is None or row["status"] != "unknown":
                return
            if paid:
                c.execute(
                    "UPDATE orders SET status='paid', report_status='awaiting', "
                    "error=? WHERE id=?",
                    (note[:500], order_id),
                )
                c.execute(
                    "UPDATE users SET total_orders=total_orders+1, total_face=total_face+?,"
                    " total_paid=total_paid+? WHERE user_id=?",
                    (int(row["face_amount"]), int(row["paid_amount"]), int(row["user_id"])),
                )
            else:
                c.execute(
                    "UPDATE orders SET status='failed', error=? WHERE id=?",
                    (note[:500], order_id),
                )

    # -------------------------------------------------------- 店舗プリセット

    def remember_store(self, user_id: int, store_id: str, store_name: str) -> None:
        if not store_id:
            return
        with self.tx() as c:
            c.execute(
                "INSERT INTO store_presets(user_id,store_id,store_name,uses,created_at) "
                "VALUES(?,?,?,1,?) "
                "ON CONFLICT(user_id,store_id) DO UPDATE SET uses=uses+1, "
                "store_name=COALESCE(NULLIF(excluded.store_name,''), store_name)",
                (user_id, store_id, store_name, ts()),
            )

    def known_stores(self, user_id: int) -> list[sqlite3.Row]:
        return self._q(
            "SELECT * FROM store_presets WHERE user_id=? ORDER BY uses DESC, store_id",
            (user_id,),
        )

    def is_known_store(self, user_id: int, store_id: str) -> bool:
        return self._q1(
            "SELECT 1 FROM store_presets WHERE user_id=? AND store_id=?", (user_id, store_id)
        ) is not None

    # ----------------------------------------------------- 実績アーカイブ

    def search_reports(
        self,
        user_id: Optional[int] = None,
        store_id: str = "",
        keyword: str = "",
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT r.*, o.store_id AS o_store_id, o.store_name AS o_store_name, "
            "o.pickup AS o_pickup, o.paid_at AS o_paid_at "
            "FROM reports r LEFT JOIN orders o ON o.id = r.order_id "
            "WHERE r.status='approved'"
        )
        args: list[Any] = []
        if user_id:
            sql += " AND r.user_id=?"
            args.append(user_id)
        if store_id:
            sql += " AND o.store_id=?"
            args.append(store_id)
        if keyword:
            sql += " AND r.content LIKE ?"
            args.append(f"%{keyword}%")
        sql += " ORDER BY r.id DESC LIMIT ?"
        args.append(limit)
        return self._q(sql, args)

    # ------------------------------------------------- 長期未使用の残高

    def dormant_users(self, cutoff: datetime, min_balance: int) -> list[dict]:
        """最後の動きが cutoff より古く、残高が残っている利用者。"""
        rows = self._q(
            "SELECT u.user_id, u.balance, MAX(l.created_at) AS last_move "
            "FROM users u LEFT JOIN ledger l ON l.user_id = u.user_id "
            "WHERE u.balance >= ? AND u.banned = 0 "
            "GROUP BY u.user_id",
            (min_balance,),
        )
        limit = cutoff.isoformat(timespec="seconds")
        return [
            {
                "user_id": int(r["user_id"]),
                "balance": int(r["balance"]),
                "last_move": r["last_move"],
            }
            for r in rows
            if r["last_move"] and str(r["last_move"]) < limit
        ]
