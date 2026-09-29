"""McDonald's API adapter.

Hex decoding is always available (pure protobuf parsing).
External ordering requires MCD_REFRESH_TOKEN and the HATTIMCD module.
Store name lookup works without authentication (aiohttp).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from models import DecodedOrderInfo, ProductInfo

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
    return ProductInfo(product_id=product_id, display_name="", addons=addons)


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

_SENSITIVE_WORDS = (
    "bearer", "token", "paseto", "authorization",
    "password", "secret", "key", "cookie",
)

_RETRYABLE_HINTS = (
    "connection refused", "connection reset", "connection aborted",
    "name or service not known", "temporary failure in name resolution",
    "cannot connect", "server disconnected", "bad gateway",
    "service unavailable", "502", "503", "504",
)

_TIMEOUT_HINTS = ("timeout", "timed out")


def _sanitize(message: str) -> str:
    low = message.lower()
    if any(w in low for w in _SENSITIVE_WORDS):
        return "外部API処理エラー"
    return message


def _classify(message: str) -> str:
    """'timeout' | 'retryable' | 'fatal' を返す。"""
    low = message.lower()
    if any(w in low for w in _TIMEOUT_HINTS):
        return "timeout"
    if any(w in low for w in _RETRYABLE_HINTS):
        return "retryable"
    return "fatal"


# ── Async adapter ──────────────────────────────────────────


class MCDAdapter:
    def __init__(self) -> None:
        self._enabled = False
        self._mcd: object | None = None
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._store_cache: dict[str, str] = {}
        self.max_attempts = 3

    async def initialize(self, refresh_token: str = "") -> None:
        refresh_token = refresh_token.strip()
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

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers=_STORE_HEADERS,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_store_name(self, store_id: str) -> str:
        if not store_id:
            return ""
        if store_id in self._store_cache:
            return self._store_cache[store_id]
        session = await self._get_session()
        for group in _DATA_GROUPS:
            url = (
                f"https://data.cat.{group}.prod.mop.mcd.qorcommerce.com"
                f"/{store_id}.json"
            )
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        name = (payload or {}).get("store", {}).get("name", "")
                        if name:
                            self._store_cache[store_id] = name
                            return name
            except Exception:
                continue
        return ""

    async def decode_hex(self, hex_str: str) -> DecodedOrderInfo:
        info = await asyncio.to_thread(decode_hex_sync, hex_str)
        if info.store_id:
            try:
                info.store_name = await self.fetch_store_name(info.store_id)
            except Exception:
                pass
        return info

    async def execute_order(
        self, hex_str: str, max_attempts: int | None = None
    ) -> dict:
        """注文を実行する。

        一時的なネットワーク障害のみ指数バックオフで自動リトライする。
        タイムアウトは二重決済を避けるためリトライせず要確認扱いにする。
        """
        if not self._enabled or self._mcd is None:
            raise RuntimeError("外部注文APIが設定されていません。")

        attempts = max_attempts if max_attempts is not None else self.max_attempts
        attempts = max(1, min(5, attempts))

        async with self._lock:
            last_error = "不明なエラー"
            for attempt in range(1, attempts + 1):
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
                        "attempts": attempt,
                    }
                except Exception as exc:
                    raw = str(exc)
                    kind = _classify(raw)
                    last_error = _sanitize(raw)[:300]

                    if kind == "timeout":
                        return {
                            "success": False,
                            "error": last_error,
                            "unknown_state": True,
                            "attempts": attempt,
                        }
                    if kind == "retryable" and attempt < attempts:
                        delay = 2 ** (attempt - 1)
                        logger.warning(
                            "Order attempt %d/%d failed (retrying in %ds)",
                            attempt, attempts, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    return {
                        "success": False,
                        "error": last_error,
                        "unknown_state": False,
                        "attempts": attempt,
                    }

            return {
                "success": False,
                "error": last_error,
                "unknown_state": False,
                "attempts": attempts,
            }
