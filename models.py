"""Data models, enums, and constants for McDonald's Concierge Bot."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class EmbedColor:
    PRIMARY = 0xD4A017
    SUCCESS = 0x2ECC71
    ERROR = 0xE74C3C
    WARNING = 0xF39C12
    INFO = 0x3498DB
    DARK = 0x2C2F33


class OrderStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"

    @property
    def display(self) -> str:
        return _STATUS_DISPLAY[self.value]

    @property
    def emoji(self) -> str:
        return _STATUS_EMOJI[self.value]


_STATUS_DISPLAY = {
    "pending": "処理待ち",
    "processing": "処理中",
    "completed": "完了",
    "failed": "失敗",
    "manual_review": "要確認",
    "refunded": "返金済",
    "cancelled": "キャンセル",
}

_STATUS_EMOJI = {
    "pending": "⏳",
    "processing": "⚙",
    "completed": "✅",
    "failed": "❌",
    "manual_review": "🔍",
    "refunded": "↩",
    "cancelled": "✕",
}


VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {
        OrderStatus.COMPLETED,
        OrderStatus.FAILED,
        OrderStatus.MANUAL_REVIEW,
    },
    OrderStatus.COMPLETED: {OrderStatus.REFUNDED},
    OrderStatus.FAILED: {OrderStatus.PROCESSING},
    OrderStatus.MANUAL_REVIEW: {
        OrderStatus.COMPLETED,
        OrderStatus.REFUNDED,
        OrderStatus.FAILED,
    },
    OrderStatus.REFUNDED: set(),
    OrderStatus.CANCELLED: set(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in VALID_TRANSITIONS.get(current, set())


class TransactionType(Enum):
    DEPOSIT = "deposit"
    ORDER_PAYMENT = "order_payment"
    REFUND = "refund"
    ADMIN_ADD = "admin_add"
    ADMIN_REMOVE = "admin_remove"
    ADMIN_SET = "admin_set"
    POINT_REDEEM = "point_redeem"

    @property
    def display(self) -> str:
        return _TX_DISPLAY[self.value]


_TX_DISPLAY = {
    "deposit": "入金",
    "order_payment": "注文支払",
    "refund": "返金",
    "admin_add": "管理者加算",
    "admin_remove": "管理者減算",
    "admin_set": "管理者設定",
    "point_redeem": "ポイント交換",
}


class DepositStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


DEFAULT_SETTINGS: dict[str, str] = {
    # 基本
    "user_rate": "60",
    "min_order_amount": "400",
    "maintenance": "0",
    "accepting_orders": "1",
    "achievement_channel_id": "",
    "admin_log_channel_id": "",
    "deposit_instruction": (
        "PayPay または銀行振込にて入金をお願いします。\n"
        "入金完了後、入金申請を行ってください。"
    ),
    # 上限・制限
    "max_order_amount": "0",      # 0 = 無制限
    "min_deposit_amount": "100",
    "max_deposit_amount": "0",    # 0 = 無制限
    "daily_order_limit": "0",     # 0 = 無制限
    "order_cooldown": "30",       # 秒
    "deposit_cooldown": "60",     # 秒
    "hex_reuse_check": "1",
    # ランク・ポイント
    "rank_enabled": "1",
    "point_rate": "0",            # 還元率% (0 = 無効)
    # キャンペーン
    "campaign_rate": "",          # 空 = なし
    "campaign_end": "",           # ISO8601 UTC
    # 通知
    "dm_notify": "1",
    # バックアップ
    "backup_enabled": "1",
    "backup_interval_hours": "6",
    "backup_keep": "14",
    # 日次レポート
    "report_enabled": "0",
    "report_channel_id": "",
    "report_hour": "9",
    "report_last_date": "",
    # リトライ
    "retry_max_attempts": "3",
    # タイムゾーン
    "tz_offset": "9",             # JST
}


@dataclass
class DecodedOrderInfo:
    store_id: str = ""
    store_name: str = ""
    pickup_method: str = ""
    total_amount: int = 0
    short_order_code: str = ""
    products: list["ProductInfo"] = field(default_factory=list)
    raw_hex: str = ""


@dataclass
class ProductInfo:
    product_id: str = ""
    display_name: str = ""
    addons: list["ProductInfo"] = field(default_factory=list)


MAX_HEX_LENGTH = 10_000


# ── ランク制度 ─────────────────────────────────────────────


@dataclass(frozen=True)
class Rank:
    threshold: int
    name: str
    emoji: str
    bonus: int  # 追加割引ポイント（負担率から引く）


RANKS: tuple[Rank, ...] = (
    Rank(0, "ブロンズ", "🥉", 0),
    Rank(10, "シルバー", "🥈", 2),
    Rank(30, "ゴールド", "🥇", 4),
    Rank(60, "プラチナ", "💎", 6),
    Rank(100, "ダイヤモンド", "👑", 8),
)


def resolve_rank(completed_orders: int) -> Rank:
    current = RANKS[0]
    for r in RANKS:
        if completed_orders >= r.threshold:
            current = r
    return current


def next_rank(completed_orders: int) -> Rank | None:
    for r in RANKS:
        if completed_orders < r.threshold:
            return r
    return None


@dataclass
class RateBreakdown:
    base: int = 60
    vip: bool = False
    campaign: bool = False
    rank: Rank | None = None
    rank_bonus: int = 0
    coupon_code: str = ""
    coupon_bonus: int = 0
    final: int = 60

    @property
    def discount(self) -> int:
        return 100 - self.final


MIN_RATE = 1
MAX_RATE = 100


def clamp_rate(rate: int) -> int:
    return max(MIN_RATE, min(MAX_RATE, rate))


# ── ユーティリティ ─────────────────────────────────────────


def validate_hex(hex_str: str) -> tuple[bool, str]:
    s = hex_str.strip()
    if not s:
        return False, "Hexデータが空です。"
    if len(s) > MAX_HEX_LENGTH:
        return False, f"Hexデータが長すぎます（上限: {MAX_HEX_LENGTH:,}文字）"
    if len(s) % 2 != 0:
        return False, "Hexデータの長さが奇数です。"
    try:
        bytes.fromhex(s)
    except ValueError:
        return False, "不正なHex形式です。16進数文字列を入力してください。"
    return True, ""


def calculate_user_amount(total: int, rate_percent: int) -> int:
    return math.ceil(total * rate_percent / 100)


def hex_digest(hex_str: str) -> str:
    return hashlib.sha256(hex_str.strip().lower().encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now(offset_hours: int = 9) -> datetime:
    return utc_now() + timedelta(hours=offset_hours)


def day_start_utc(offset_hours: int = 9) -> str:
    """ローカル日付の0時をUTCのISO文字列(SQLite形式)で返す。"""
    now_local = local_now(offset_hours)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local - timedelta(hours=offset_hours)
    return start_utc.strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
