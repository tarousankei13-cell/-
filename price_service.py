"""LTC/JPY の価格取得。

この Bot は残高という「お金」を扱うため、価格は取れたら使うのではなく
**信用できるときだけ使う**。信用できない場合はチャージを拒否する (推測しない)。

多層の安全弁
    1. 取得元は CoinGecko の公開エンドポイント (``/api/v3/simple/price``) のみ。
       応答は型・範囲を厳格に検証し、想定外の形なら採用しない。
    2. 取得に失敗した場合は、管理者が設定した固定価格へフォールバックできる。
       フォールバックも無い場合は LTC チャージを拒否する。
    3. 直近の採用値から大きく跳ねた場合 (既定 35%) は採用せず Owner へ通知する。
       価格フィードの異常や汚染で、巨額の残高が付与されるのを防ぐ。
    4. 取得済みの価格は短時間キャッシュし、API を叩きすぎない。
       古すぎる価格 (既定 15 分) は使わない。

秘密情報は扱わないが、ネットワークエラーの本文はログに出す前に必ず
``utils.sanitize_for_log`` を通す。
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import requests

import config
import utils

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger("bot.price")


class PriceError(Exception):
    """価格を信用して使えないことを表す。"""

    def __init__(self, detail: str, *, code: str = config.ErrorCode.PRICE_UNAVAILABLE) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class PriceQuote:
    """採用した価格とその出自。"""

    price: Decimal          #: 1 LTC あたりの円
    source: str             #: COINGECKO / MANUAL
    fetched_at: int         #: 取得時刻 (UNIX 秒)
    stale: bool = False     #: キャッシュから返したか

    @property
    def age_seconds(self) -> int:
        return max(0, utils.now_ts() - self.fetched_at)

    def snapshot(self) -> dict[str, Any]:
        return {
            "price": str(self.price),
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


#: 手動価格・最終採用値を保存する system_settings のキー
KEY_MANUAL_PRICE = "ltc_manual_price"
KEY_MANUAL_PRICE_AT = "ltc_manual_price_at"
KEY_PRICE_SOURCE = "ltc_price_source"
KEY_LAST_GOOD_PRICE = "ltc_last_good_price"
KEY_LAST_GOOD_AT = "ltc_last_good_at"


class PriceService:
    """LTC/JPY 価格の取得と検証。"""

    def __init__(self, db: "Database", bot: Any = None) -> None:
        self.db = db
        self.bot = bot
        self._lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="price")
        self._cache: PriceQuote | None = None
        self._session: requests.Session | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------
    async def get_source(self) -> str:
        """現在の取得元 (既定は CoinGecko)。"""
        value = await self.db.get_system_value(KEY_PRICE_SOURCE)
        if value in (config.PRICE_SOURCE_COINGECKO, config.PRICE_SOURCE_MANUAL):
            return value
        return config.PRICE_SOURCE_COINGECKO

    async def set_source(self, source: str) -> None:
        if source not in (config.PRICE_SOURCE_COINGECKO, config.PRICE_SOURCE_MANUAL):
            raise PriceError(f"不明な取得元です: {source}")
        await self.db.set_system_value(KEY_PRICE_SOURCE, source)
        self._cache = None

    async def get_manual_price(self) -> tuple[Decimal | None, int]:
        """手動設定の価格と、その更新時刻を返す。"""
        raw = await self.db.get_system_value(KEY_MANUAL_PRICE)
        at = await self.db.get_system_value(KEY_MANUAL_PRICE_AT)
        price = utils.validate_price_jpy(raw) if raw else None
        return price, int(at or 0)

    async def set_manual_price(self, price: Decimal) -> None:
        await self.db.set_system_value(KEY_MANUAL_PRICE, utils.rate_to_db(price))
        await self.db.set_system_value(KEY_MANUAL_PRICE_AT, str(utils.now_ts()))
        self._cache = None

    async def _get_last_good(self) -> tuple[Decimal | None, int]:
        raw = await self.db.get_system_value(KEY_LAST_GOOD_PRICE)
        at = await self.db.get_system_value(KEY_LAST_GOOD_AT)
        price = utils.validate_price_jpy(raw) if raw else None
        return price, int(at or 0)

    async def _remember_good(self, price: Decimal) -> None:
        await self.db.set_system_value(KEY_LAST_GOOD_PRICE, utils.rate_to_db(price))
        await self.db.set_system_value(KEY_LAST_GOOD_AT, str(utils.now_ts()))

    # ------------------------------------------------------------------
    # 取得
    # ------------------------------------------------------------------
    async def get_price(self, *, force: bool = False) -> PriceQuote:
        """採用できる価格を返す。信用できない場合は PriceError。"""
        async with self._lock:
            now = utils.now_ts()
            if (
                not force
                and self._cache is not None
                and now - self._cache.fetched_at <= config.PRICE_CACHE_SECONDS
            ):
                return self._cache

            source = await self.get_source()
            if source == config.PRICE_SOURCE_MANUAL:
                quote = await self._manual_quote()
                self._cache = quote
                return quote

            try:
                price = await self._fetch_coingecko()
            except PriceError as exc:
                self._consecutive_failures += 1
                self._last_error = exc.detail
                logger.warning(
                    "LTC 価格の取得に失敗しました (%d回連続): %s",
                    self._consecutive_failures, utils.sanitize_for_log(exc.detail, limit=200),
                )
                quote = await self._fallback_quote()
                self._cache = quote
                return quote

            await self._guard_deviation(price)
            self._consecutive_failures = 0
            self._last_error = None
            await self._remember_good(price)
            quote = PriceQuote(
                price=price, source=config.PRICE_SOURCE_COINGECKO, fetched_at=utils.now_ts()
            )
            self._cache = quote
            return quote

    async def _manual_quote(self) -> PriceQuote:
        price, at = await self.get_manual_price()
        if price is None:
            raise PriceError(
                "手動価格が設定されていません (`/provider price` で設定してください)"
            )
        return PriceQuote(
            price=price, source=config.PRICE_SOURCE_MANUAL, fetched_at=at or utils.now_ts()
        )

    async def _fallback_quote(self) -> PriceQuote:
        """API 失敗時のフォールバック。手動価格があればそれを使う。"""
        price, at = await self.get_manual_price()
        if price is not None:
            logger.info("LTC 価格は手動設定値へフォールバックしました")
            return PriceQuote(
                price=price, source=config.PRICE_SOURCE_MANUAL,
                fetched_at=at or utils.now_ts(), stale=True,
            )
        # 直近の採用値が十分に新しければ再利用する
        last, last_at = await self._get_last_good()
        if last is not None and utils.now_ts() - last_at <= config.PRICE_MAX_AGE_SECONDS:
            logger.info("LTC 価格は直近の採用値を再利用しました (%d秒前)",
                        utils.now_ts() - last_at)
            return PriceQuote(
                price=last, source=config.PRICE_SOURCE_COINGECKO,
                fetched_at=last_at, stale=True,
            )
        raise PriceError("レートを取得できず、代替値もありません")

    async def _guard_deviation(self, price: Decimal) -> None:
        """直近の採用値から極端に離れた価格は採用しない。"""
        last, last_at = await self._get_last_good()
        if last is None or last <= 0:
            return
        if utils.now_ts() - last_at > config.PRICE_MAX_AGE_SECONDS * 8:
            return  # 古すぎる比較対象では判定しない
        guard = Decimal(config.PRICE_DEVIATION_GUARD_PERCENT)
        deviation = abs(price - last) / last * Decimal(100)
        if deviation <= guard:
            return
        detail = (
            f"LTC 価格が直近値から {deviation.quantize(Decimal('0.1'))}% 変動したため "
            f"採用しませんでした (直近 {utils.fmt_int(int(last))}円 → 取得 "
            f"{utils.fmt_int(int(price))}円)"
        )
        logger.error("%s", detail)
        if self.bot is not None:
            try:
                await self.bot.alert_owner(
                    f"**LTC 価格の異常を検出しました**\n{detail}\n"
                    "価格フィードを確認してください。必要なら "
                    "`/provider price_source manual` と `/provider price` で固定価格に"
                    "切り替えられます。"
                )
            except Exception:  # noqa: BLE001
                logger.exception("価格異常の通知に失敗しました")
        raise PriceError(detail)

    # ------------------------------------------------------------------
    # HTTP (CoinGecko)
    # ------------------------------------------------------------------
    def _get_session(self) -> requests.Session:
        if self._session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
            self._session = session
        return self._session

    async def _fetch_coingecko(self) -> Decimal:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(self._executor, self._request_coingecko)
        return self._parse_coingecko(payload)

    def _request_coingecko(self) -> Any:
        """CoinGecko へ 1 リクエストだけ行う (別スレッド)。"""
        params = {
            "ids": config.COINGECKO_LTC_ID,
            "vs_currencies": config.COINGECKO_VS_CURRENCY,
            "include_last_updated_at": "true",
        }
        try:
            response = self._get_session().get(
                config.COINGECKO_PRICE_URL,
                params=params,
                timeout=config.PRICE_HTTP_TIMEOUT,
            )
        except requests.Timeout as exc:
            raise PriceError(f"タイムアウト: {utils.safe_error_text(exc)}") from exc
        except requests.RequestException as exc:
            raise PriceError(f"通信エラー: {utils.safe_error_text(exc)}") from exc
        if response.status_code != 200:
            raise PriceError(f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise PriceError("JSON として解釈できない応答でした") from exc

    @staticmethod
    def _parse_coingecko(payload: Any) -> Decimal:
        """応答を厳格に検証して価格を取り出す。

        想定する形:
            ``{"litecoin": {"jpy": 12345.6, "last_updated_at": 1700000000}}``
        """
        if not isinstance(payload, dict):
            raise PriceError("応答の形式が想定と異なります (dict ではない)")
        asset = payload.get(config.COINGECKO_LTC_ID)
        if not isinstance(asset, dict):
            raise PriceError(
                f"応答に {config.COINGECKO_LTC_ID} が含まれていません"
            )
        raw = asset.get(config.COINGECKO_VS_CURRENCY)
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise PriceError(
                f"価格の型が想定と異なります ({type(raw).__name__})"
            )
        price = utils.validate_price_jpy(str(raw))
        if price is None:
            raise PriceError(f"価格が許容範囲外です ({utils.truncate(str(raw), 40)})")
        updated_at = asset.get("last_updated_at")
        if isinstance(updated_at, int) and not isinstance(updated_at, bool):
            age = utils.now_ts() - updated_at
            if age > config.PRICE_MAX_AGE_SECONDS:
                raise PriceError(f"価格が古すぎます ({age}秒前)")
        return price

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    async def status_snapshot(self) -> dict[str, Any]:
        """管理画面用の状態。"""
        source = await self.get_source()
        manual, manual_at = await self.get_manual_price()
        last, last_at = await self._get_last_good()
        cached = self._cache
        return {
            "source": source,
            "manual_price": str(manual) if manual is not None else None,
            "manual_updated_at": manual_at,
            "last_good_price": str(last) if last is not None else None,
            "last_good_at": last_at,
            "cached_price": str(cached.price) if cached else None,
            "cached_age": cached.age_seconds if cached else None,
            "cached_stale": bool(cached.stale) if cached else None,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
        }

    async def shutdown(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                logger.debug("価格取得セッションの終了に失敗しました", exc_info=True)
            self._session = None
        self._executor.shutdown(wait=False)
