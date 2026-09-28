"""McDonald's API adapter.

Hex decoding is always available (pure protobuf parsing).
External ordering requires MCD_REFRESH_TOKEN and the HATTIMCD module.
Store name lookup works without authentication.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import requests

from models import DecodedOrderInfo, ProductInfo, resolve_product_name

logger = logging.getLogger("bot.mcd")

# ── Protobuf utilities (from HATTIMCD) ─────────────────────


def _varint_decode(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, pos


def _proto_parse(data: bytes) -> dict[int, list]:
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _varint_decode(data, pos)
        except Exception:
            break
        fn, wt = tag >> 3, tag & 0x7
        if wt == 0:
            v, pos = _varint_decode(data, pos)
            fields.setdefault(fn, []).append(v)
        elif wt == 2:
            length, pos = _varint_decode(data, pos)
            fields.setdefault(fn, []).append(data[pos : pos + length])
            pos += length
        elif wt == 5:
            fields.setdefault(fn, []).append(data[pos : pos + 4])
            pos += 4
        elif wt == 1:
            fields.setdefault(fn, []).append(data[pos : pos + 8])
            pos += 8
        else:
            break
    return fields


def _try_str(b: object) -> str:
    if isinstance(b, bytes):
        try:
            return b.decode("utf-8")
        except Exception:
            return b.hex()
    return str(b)


def _detect_pickup_method(hex_str: str) -> str:
    try:
        data = bytes.fromhex(hex_str.strip())
        top = _proto_parse(data)
        f7_list = top.get(7, [])
        if not f7_list:
            return "テイクアウト"
        f7_raw = f7_list[0]
        if not isinstance(f7_raw, bytes) or len(f7_raw) == 0:
            return "テイクアウト"
        inner = _proto_parse(f7_raw)
        if 2 in inner:
            return "デリバリー"
        if 1 in inner:
            return "イートイン"
        return "テイクアウト"
    except Exception:
        return "テイクアウト"


def _parse_product_proto(raw: bytes) -> ProductInfo:
    f = _proto_parse(raw)
    product_id = ""
    for v in f.get(2, []):
        s = _try_str(v)
        if s and not s.startswith("0x"):
            product_id = s
    addons = [
        _parse_product_proto(r) for r in f.get(5, []) if isinstance(r, bytes)
    ]
    display = resolve_product_name(product_id) if product_id else ""
    return ProductInfo(product_id=product_id, display_name=display, addons=addons)


# ── Hex decoder (no network needed) ────────────────────────


def decode_hex_sync(hex_str: str) -> DecodedOrderInfo:
    hex_str = hex_str.strip()
    data = bytes.fromhex(hex_str)
    top = _proto_parse(data)

    store_id = ""
    for v in top.get(1, []):
        store_id = _try_str(v)

    short_code = ""
    amount = 0
    products: list[ProductInfo] = []

    for f8_raw in top.get(8, []):
        if not isinstance(f8_raw, bytes):
            continue
        for f2_raw in _proto_parse(f8_raw).get(2, []):
            if not isinstance(f2_raw, bytes):
                continue
            f2 = _proto_parse(f2_raw)
            for v in f2.get(2, []):
                short_code = _try_str(v)
            for v in f2.get(4, []):
                if isinstance(v, int):
                    amount = v
            for pr in f2.get(5, []):
                if isinstance(pr, bytes):
                    p = _parse_product_proto(pr)
                    if p.product_id:
                        products.append(p)

    pickup_method = _detect_pickup_method(hex_str)

    return DecodedOrderInfo(
        store_id=store_id,
        store_name="",
        pickup_method=pickup_method,
        total_amount=amount,
        short_order_code=short_code,
        products=products,
        raw_hex=hex_str,
    )


# ── Store name fetcher (no auth needed) ────────────────────

_DATA_GROUPS = ["group-h", "group-g", "group-f", "group-e"]
_STORE_HEADERS = {
    "User-Agent": "McDonaldsJapan-Prod/5.5.30 (iPhone; iOS 26.3.1; Scale/3.00)",
    "Accept": "application/json",
}


def _fetch_store_name_sync(store_id: str, timeout: int = 10) -> str:
    for group in _DATA_GROUPS:
        url = (
            f"https://data.cat.{group}.prod.mop.mcd.qorcommerce.com"
            f"/{store_id}.json"
        )
        try:
            resp = requests.get(url, headers=_STORE_HEADERS, timeout=timeout)
            if resp.status_code == 200:
                name = resp.json().get("store", {}).get("name", "")
                if name:
                    return name
        except Exception:
            continue
    return ""


# ── Async adapter ──────────────────────────────────────────


class MCDAdapter:
    def __init__(self) -> None:
        self._enabled = False
        self._mcd: object | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        refresh_token = os.getenv("MCD_REFRESH_TOKEN", "").strip()
        if not refresh_token:
            logger.info("MCD adapter: no credentials configured")
            return
        try:
            import importlib
            mod = importlib.import_module("HATTIMCD.main")
            MCD = getattr(mod, "MCD")
            TokenSet = getattr(mod, "TokenSet")
            self._mcd = MCD(tokens=TokenSet(refresh_token=refresh_token))
            self._enabled = True
            logger.info("MCD adapter: credentials loaded, external ordering enabled")
        except ImportError:
            logger.warning("HATTIMCD module not found; external ordering disabled")
        except Exception as exc:
            logger.error("MCD adapter init error: %s", type(exc).__name__)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def decode_hex(self, hex_str: str) -> DecodedOrderInfo:
        info = await asyncio.to_thread(decode_hex_sync, hex_str)
        if info.store_id:
            try:
                name = await asyncio.to_thread(
                    _fetch_store_name_sync, info.store_id
                )
                info.store_name = name
            except Exception:
                pass
        return info

    async def execute_order(self, hex_str: str) -> dict:
        if not self._enabled or self._mcd is None:
            raise RuntimeError("外部注文APIが設定されていません。")
        async with self._lock:
            try:
                result = await asyncio.to_thread(
                    self._mcd.pay_from_hex, hex_str  # type: ignore[union-attr]
                )
                return {
                    "success": True,
                    "receipt_number": getattr(result, "receipt_number", "") or "",
                    "order_token": getattr(result, "order_token", "") or "",
                    "order_group": getattr(result, "group", "") or "",
                    "store_name": getattr(result, "store_name", "") or "",
                }
            except Exception as exc:
                err = str(exc)
                sensitive_words = (
                    "bearer", "token", "paseto", "authorization",
                    "password", "secret", "key",
                )
                if any(w in err.lower() for w in sensitive_words):
                    err = "外部API処理エラー"
                is_timeout = any(
                    w in err.lower() for w in ("timeout", "timed out", "connect")
                )
                return {
                    "success": False,
                    "error": err[:300],
                    "unknown_state": is_timeout,
                }
