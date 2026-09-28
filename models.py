"""Data models, enums, and constants for McDonald's Concierge Bot."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
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
}


class DepositStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


DEFAULT_SETTINGS: dict[str, str] = {
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

PRODUCT_NAMES: dict[str, str] = {}


def resolve_product_name(product_id: str) -> str:
    return PRODUCT_NAMES.get(product_id, product_id)


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
