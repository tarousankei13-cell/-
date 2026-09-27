"""共通ユーティリティ。

- 秘密情報のマスキング
- 送金リンクの正規化 / ハッシュ化 (完全なURLはDBにもログにも残さない)
- Decimal による金額計算 (float 禁止)
- レート制限
- アクセストークンの暗号化 (DBバックアップ経由の漏洩対策)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import config

logger = logging.getLogger(config.LOGGER_BOT)

JST = timezone(timedelta(hours=9))

#: kyash.me の送金 / 請求リンク
_LINK_RE = re.compile(r"kyash\.me/payments/([A-Za-z0-9_\-]{3,128})", re.IGNORECASE)
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,128}$")
#: ログマスキング用。ホスト名が分かれて出力されるケース
#: (requests 例外の "url: /payments/xxxx" など) も捕捉する。
_PAYMENT_PATH_RE = re.compile(r"payments/([A-Za-z0-9_\-]{3,128})", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")

# ---------------------------------------------------------------------------
# 時刻
# ---------------------------------------------------------------------------

def now_ts() -> int:
    """現在時刻の UNIX 秒 (UTC基準の絶対時刻)。"""
    return int(time.time())


def format_jst(ts: int | None, *, with_seconds: bool = False) -> str:
    """UNIX秒を JST の表示文字列へ変換する。"""
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(ts, JST)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def jst_day_start(ts: int | None = None) -> int:
    """JST における「その日の 00:00」の UNIX 秒を返す (日次上限の集計境界)。"""
    dt = datetime.fromtimestamp(ts if ts is not None else now_ts(), JST)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def jst_month_start(ts: int | None = None) -> int:
    """JST における「その月の1日 00:00」の UNIX 秒を返す。"""
    dt = datetime.fromtimestamp(ts if ts is not None else now_ts(), JST)
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def discord_ts(ts: int | None, style: str = "f") -> str:
    """Discord のタイムスタンプ記法を返す。

    ``<t:1234567890:f>`` 形式で、閲覧者のローカル時刻で表示される。
    利用者向けの表示に使う (管理者向けは JST 固定の ``format_jst`` を使う)。

    Args:
        style: ``f``=日時 / ``F``=曜日つき日時 / ``R``=相対 / ``t``=時刻 / ``d``=日付
    """
    if not ts:
        return "-"
    return f"<t:{int(ts)}:{style}>"


def jst_week_start(ts: int | None = None) -> int:
    """その週の月曜 00:00 (JST) の UNIX 秒。"""
    moment = datetime.fromtimestamp(ts or now_ts(), JST)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=start.weekday())
    return int(start.timestamp())


def period_bounds(period: str, ts: int | None = None) -> tuple[int, int, str]:
    """締めた期間の (開始, 終了, 期間キー) を返す。

    ``ts`` が属する期間ではなく、**その1つ前の完了した期間**を返す。
    ランキング報酬は「終わった期間」を表彰するため。

    Returns:
        ``(start, end, period_key)``。end は期間の直後 (半開区間)。
        period_key は ``2026-W39`` / ``2026-09`` の形式。
    """
    moment = ts or now_ts()
    if period == config.RankingPeriod.WEEKLY:
        this_start = jst_week_start(moment)
        start = jst_week_start(this_start - 1)
        end = this_start
        iso = datetime.fromtimestamp(start, JST).isocalendar()
        return start, end, f"{iso[0]}-W{iso[1]:02d}"
    if period == config.RankingPeriod.MONTHLY:
        this_start = jst_month_start(moment)
        start = jst_month_start(this_start - 1)
        end = this_start
        stamp = datetime.fromtimestamp(start, JST)
        return start, end, f"{stamp.year}-{stamp.month:02d}"
    raise ValueError(f"不明な期間です: {period}")


def current_period_bounds(period: str, ts: int | None = None) -> tuple[int, int, str]:
    """進行中の期間の (開始, 現在, 期間キー)。手動配布の確認用。"""
    moment = ts or now_ts()
    if period == config.RankingPeriod.WEEKLY:
        start = jst_week_start(moment)
        iso = datetime.fromtimestamp(start, JST).isocalendar()
        return start, moment, f"{iso[0]}-W{iso[1]:02d}"
    if period == config.RankingPeriod.MONTHLY:
        start = jst_month_start(moment)
        stamp = datetime.fromtimestamp(start, JST)
        return start, moment, f"{stamp.year}-{stamp.month:02d}"
    raise ValueError(f"不明な期間です: {period}")


def progress_bar(current: int, target: int, width: int = config.GOAL_BAR_WIDTH) -> str:
    """進捗バーを文字で描く (端末・スマホどちらでも崩れない文字を使う)。"""
    if target <= 0:
        return "─" * width
    ratio = min(1.0, max(0.0, current / target))
    filled = int(ratio * width)
    if 0 < ratio < 1 and filled == 0:
        filled = 1           # わずかでも進んでいれば1つ塗る
    if ratio < 1 and filled == width:
        filled = width - 1   # 未達で満タンに見せない
    return "█" * filled + "░" * (width - filled)


def format_duration(seconds: int | float) -> str:
    """秒数を「1d 02:03:04」形式へ整形する。"""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------------------
# 表示整形
# ---------------------------------------------------------------------------

def fmt_int(value: int | None) -> str:
    """3桁区切りの整数表示。"""
    if value is None:
        return "-"
    return f"{int(value):,}"


def fmt_yen(value: int | None) -> str:
    """円表記。"""
    if value is None:
        return "-"
    return f"{int(value):,}円"


def fmt_rate(rate: Decimal | str | None) -> str:
    """チャージ率表示 (130 → "130%", 130.5 → "130.5%")。"""
    if rate is None:
        return "-"
    dec = to_decimal(rate)
    if dec is None:
        return "-"
    normalized = dec.normalize()
    text = format(normalized, "f")
    return f"{text}%"


def truncate(text: str, limit: int) -> str:
    """Discord の文字数制限向けに安全に切り詰める。"""
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


# ---------------------------------------------------------------------------
# 金額 / Decimal
# ---------------------------------------------------------------------------

def to_decimal(value: Decimal | str | int | float | None) -> Decimal | None:
    """Decimal へ安全に変換する (float 由来の誤差を避けるため str 経由)。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_user_amount(raw: str) -> int | None:
    """利用者が入力した金額文字列を整数 (円) へ変換する。

    全角数字・カンマ・「円」等を許容し、それ以外の文字が含まれる入力や
    0以下・桁数過大な入力は ``None`` を返して拒否する。
    """
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    text = text.replace(",", "").replace("_", "").replace(" ", "")
    for suffix in ("円", "yen", "JPY", "jpy", "¥"):
        text = text.replace(suffix, "")
    if not text or len(text) > config.AMOUNT_INPUT_MAX_LEN:
        return None
    if not text.isdigit():  # 負号・小数点・指数表記・記号はすべて拒否
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def parse_money_text(raw: str | None) -> int | None:
    """Kyash のページ等から取得した金額表記 ("¥1,000") を整数へ変換する。"""
    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw))
    digits = "".join(_DIGITS_RE.findall(text))
    if not digits or len(digits) > 12:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def calc_credited_amount(received_amount: int, charge_rate: Decimal | str) -> int:
    """受取額とチャージ率から付与する内部残高を算出する (四捨五入)。

    例: 101円 × 130% = 131.3 → 131
    """
    rate = to_decimal(charge_rate)
    if rate is None:
        raise ValueError("charge_rate が不正です")
    amount = Decimal(int(received_amount)) * rate / Decimal(100)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate_charge_rate(raw: str) -> Decimal | None:
    """チャージ率入力を検証し、妥当な範囲の Decimal を返す。"""
    dec = to_decimal(unicodedata.normalize("NFKC", str(raw)).strip().rstrip("%"))
    if dec is None or not dec.is_finite():
        return None
    if dec < config.MIN_CHARGE_RATE or dec > config.MAX_CHARGE_RATE:
        return None
    # 小数第2位までに丸める (無意味な精度を持たせない)
    return dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()


def rate_to_db(rate: Decimal) -> str:
    """チャージ率を DB 保存用の文字列へ変換する。"""
    return format(rate.normalize(), "f")


# ---------------------------------------------------------------------------
# 暗号資産 (LTC) の数量と価格
# ---------------------------------------------------------------------------

_LTC_QUANT: Final[Decimal] = Decimal(1).scaleb(-config.LTC_DECIMALS)


def fmt_asset(amount: Decimal | str | None, *, unit: str = "LTC") -> str:
    """暗号資産の数量を表示用に整える (末尾のゼロを落とす)。"""
    dec = to_decimal(amount)
    if dec is None:
        return "-"
    text = format(quantize_asset(dec).normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    text = text or "0"
    return f"{text} {unit}" if unit else text


def quantize_asset(amount: Decimal) -> Decimal:
    """LTC の最小単位 (1e-8) へ切り捨てる。

    切り上げると利用者に「送れない額」を要求してしまうため、
    請求額の算出では常に切り上げ (``asset_amount_for``) を使い、
    ここでは表示・保存用の丸めだけを行う。
    """
    return amount.quantize(_LTC_QUANT, rounding=ROUND_DOWN)


def asset_amount_for(jpy_amount: int, price_jpy: Decimal) -> Decimal:
    """円建ての金額を、その時の単価で暗号資産の数量へ換算する。

    利用者が送る額が不足しないよう **切り上げ** る。1 litoshi 未満の
    不足で承認できなくなるのを防ぐため。
    """
    if price_jpy <= 0:
        raise ValueError("価格が不正です")
    raw = Decimal(int(jpy_amount)) / price_jpy
    return raw.quantize(_LTC_QUANT, rounding=ROUND_UP)


def jpy_value_of(asset_amount: Decimal, price_jpy: Decimal) -> int:
    """暗号資産の数量を円換算する (四捨五入)。"""
    value = asset_amount * price_jpy
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_asset_amount(raw: str) -> Decimal | None:
    """利用者が入力した LTC 数量を検証する。

    全角・カンマ・単位表記を許容し、負数・ゼロ・桁あふれは拒否する。
    """
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    # カンマは小数点の代わりに使われることがあり (0,1 = 0.1)、桁区切りと
    # 区別できない。金額を取り違えると実害が出るため、含む入力は拒否する。
    if "," in text or "_" in text:
        return None
    for suffix in ("ltc", "LTC", "Ltc"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    if not text or not re.fullmatch(r"\d{0,12}(?:\.\d{0,12})?", text):
        return None
    dec = to_decimal(text)
    if dec is None or not dec.is_finite() or dec <= 0:
        return None
    quantized = quantize_asset(dec)
    if quantized <= 0:
        return None
    return quantized


def validate_price_jpy(raw: str) -> Decimal | None:
    """1 LTC あたりの円価格を検証する (手動設定・API 応答の共通検証)。"""
    dec = to_decimal(unicodedata.normalize("NFKC", str(raw)).strip().replace(",", ""))
    if dec is None or not dec.is_finite() or dec <= 0:
        return None
    if dec < Decimal(config.PRICE_MIN_JPY) or dec > Decimal(config.PRICE_MAX_JPY):
        return None
    return dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()


_TXID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{%d}$" % config.LTC_TXID_LENGTH)


def normalize_txid(raw: str) -> str | None:
    """Litecoin の txid を正規化する (64桁の16進数のみ)。

    エクスプローラの URL を貼られた場合も末尾の txid を取り出す。
    """
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if "/" in text:                       # URL で貼られた場合
        text = text.rstrip("/").rsplit("/", 1)[-1]
    text = text.split("?", 1)[0].split("#", 1)[0].strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text if _TXID_RE.fullmatch(text) else None


_PAYPAY_REF_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Za-z_-]{6,64}$")


def normalize_payment_ref(raw: str) -> str | None:
    """PayPay の取引ID を正規化する。

    表示形式が将来変わっても壊れないよう、英数と ``-`` ``_`` のみに
    絞った緩い検証にとどめる (二重申請の判定には正規化後の値を使う)。
    """
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    text = text.replace(" ", "").replace("\u3000", "")
    if text.lower().startswith("id:"):
        text = text[3:]
    text = text.strip().upper()
    return text if _PAYPAY_REF_RE.fullmatch(text) else None


def proof_hash(provider: str, proof_ref: str) -> str:
    """証拠 (取引ID / txid) のハッシュ。二重申請の判定に使う。"""
    return hashlib.sha256(f"proof:v1:{provider}:{proof_ref}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 送金リンク
# ---------------------------------------------------------------------------

#: Kyash のリンク URL 前置詞
KYASH_LINK_PREFIX: Final[str] = "https://kyash.me/payments/"


class LinkParseError(ValueError):
    """送金リンクとして解釈できない入力。"""


def normalize_kyash_link(raw: str) -> tuple[str, str]:
    """利用者入力から Kyash 送金リンクを正規化する。

    Returns:
        (canonical_url, link_id)

    Raises:
        LinkParseError: リンクとして解釈できない場合。

    ``https://kyash.me/payments/<id>`` 形式に正規化する。クエリ・フラグメント・
    前後の文章・全角文字の差異で同じリンクが別物として扱われないようにする。
    添付モジュールは ``https://kyash.me/payments/`` が含まれない場合に
    プレフィックスを自動で付与するため、この正規化結果はそのまま渡せる。
    """
    if raw is None:
        raise LinkParseError("入力が空です")
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    # ゼロ幅文字・制御文字の除去
    text = "".join(ch for ch in text if ch.isprintable())
    match = _LINK_RE.search(text)
    if match:
        link_id = match.group(1)
    else:
        candidate = text.split()[0] if text.split() else ""
        candidate = candidate.split("?")[0].split("#")[0].rstrip("/")
        if "/" in candidate:
            candidate = candidate.rsplit("/", 1)[-1]
        if not _BARE_ID_RE.match(candidate):
            raise LinkParseError("Kyash の送金リンクを検出できません")
        link_id = candidate
    return f"{KYASH_LINK_PREFIX}{link_id}", link_id


def kyash_link_url(link_id: str) -> str:
    """リンク識別子から Kyash のリンク URL を組み立てる。

    Bot が発行した請求リンクを再表示するために使う。
    利用者の送金リンクには使わない (完全なURLを保存しないため組み立てられない)。
    """
    return f"{KYASH_LINK_PREFIX}{link_id}"


def link_hash(link_id: str) -> str:
    """送金リンク識別子のハッシュ (二重送信判定用)。完全なURLは保存しない。"""
    return hashlib.sha256(f"kyashlink:v1:{link_id}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 秘密情報マスキング
# ---------------------------------------------------------------------------

#: 値を1つだけマスクする鍵名 (JSON のフィールドなど)
_SECRET_KEY_HINTS = (
    "token", "password", "passwd", "secret", "otp", "verificationcode",
    "refreshtoken", "card", "pan", "cvv",
)

#: ヘッダ名。値はスペースを含み得るため **行末までまとめて** マスクする
#: (``Authorization: Bearer <token>`` の token 部分を残さないため)
_SECRET_HEADER_HINTS = ("authorization", "cookie", "set-cookie", "x-auth", "x-api-key")

#: JWT / ドット区切りトークン風の文字列
_JWT_RE = re.compile(r"\b[A-Za-z0-9_\-]{6,}(?:\.[A-Za-z0-9_\-]{4,}){2,}")


def mask_secret(value: Any, *, keep: int = 2) -> str:
    """秘密情報を "ab…yz (len=40)" 形式にマスクする。"""
    if value is None:
        return "<none>"
    text = str(value)
    if not text:
        return "<empty>"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]} (len={len(text)})"


def mask_identifier(value: Any, *, keep: int = 6) -> str:
    """リンクUUID等の識別子を部分表示する (ログ・管理画面向け)。"""
    if value is None:
        return "-"
    text = str(value)
    if len(text) <= keep:
        return text
    return f"{text[:keep]}…"


def sanitize_for_log(text: Any, *, limit: int = 500) -> str:
    """ログ出力前に秘密情報らしき文字列をマスキングする。

    - Kyash 送金リンクは ID 部分をマスク
    - ``token=...`` / ``"password": "..."`` 等のパターンをマスク
    - Bearer / X-Auth ヘッダ値をマスク
    """
    if text is None:
        return ""
    s = str(text)
    s = _PAYMENT_PATH_RE.sub(lambda m: f"payments/{mask_identifier(m.group(1), keep=4)}", s)
    # 1) 認証スキーム付きの値は、スキームごと行末までマスクする。
    #    先にここで潰さないと、後続の鍵名マスクが "Bearer" だけを消して
    #    トークン本体を残してしまう。
    s = re.sub(
        r"\b(Bearer|Basic|Token|Digest)\s+\S+", r"\1 ***", s, flags=re.IGNORECASE
    )
    # 2) ヘッダ名は値にスペースを含み得るため行末までマスクする
    for hint in _SECRET_HEADER_HINTS:
        s = re.sub(
            rf'((?:"|\')?{re.escape(hint)}(?:"|\')?\s*[:=]\s*)[^\r\n]+',
            lambda m: m.group(1) + "***",
            s,
            flags=re.IGNORECASE,
        )
    # 3) JSON のフィールドなどは値を1つだけマスクする
    for hint in _SECRET_KEY_HINTS:
        s = re.sub(
            rf'((?:"|\')?{re.escape(hint)}(?:"|\')?\s*[:=]\s*)(?:"|\')?([^\s,;"\'}}\)]+)',
            lambda m: m.group(1) + "***",
            s,
            flags=re.IGNORECASE,
        )
    # 4) JWT 風 (ドット区切り) とトークン風の長い連続文字列
    s = _JWT_RE.sub("***", s)
    s = re.sub(r"\b[A-Za-z0-9_\-]{40,}\b", "***", s)
    return truncate(s, limit)


def safe_error_text(exc: BaseException, *, limit: int = 400) -> str:
    """例外を管理者向けに安全な文字列へ変換する (秘密情報をマスク)。"""
    return f"{type(exc).__name__}: {sanitize_for_log(exc, limit=limit)}"


# ---------------------------------------------------------------------------
# ID 生成
# ---------------------------------------------------------------------------

def new_transaction_id() -> str:
    """取引ID (TX-XXXXXXXX)。衝突時は DB の PRIMARY KEY で検出する。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 紛らわしい文字を除外
    return "TX-" + "".join(secrets.choice(alphabet) for _ in range(8))


def new_operation_id() -> str:
    """監査ログ用の操作ID。"""
    return "OP-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))


# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------

class RateLimiter:
    """メモリ上のスライディングウィンドウ方式レート制限。"""

    def __init__(self, limit: int, window: int) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._last_prune = time.monotonic()

    def check(self, key: str) -> bool:
        """制限内なら True を返して1回分を消費する。"""
        now = time.monotonic()
        self._maybe_prune(now)
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> int:
        """次に実行可能になるまでの秒数 (概算)。"""
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        return max(0, int(self.window - (time.monotonic() - bucket[0])) + 1)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _maybe_prune(self, now: float) -> None:
        """古いエントリを定期的に破棄してメモリ肥大を防ぐ。"""
        if now - self._last_prune < 300:
            return
        self._last_prune = now
        for key in list(self._hits.keys()):
            bucket = self._hits[key]
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if not bucket:
                del self._hits[key]


class KeyedLocks:
    """キーごとの asyncio.Lock を必要な間だけ保持するヘルパ。"""

    def __init__(self) -> None:
        import asyncio

        self._asyncio = asyncio
        self._locks: dict[str, Any] = {}
        self._waiters: dict[str, int] = {}

    def get(self, key: str):
        lock = self._locks.get(key)
        if lock is None:
            lock = self._asyncio.Lock()
            self._locks[key] = lock
        return lock

    class _Ctx:
        def __init__(self, parent: "KeyedLocks", key: str) -> None:
            self._parent = parent
            self._key = key
            self._lock = parent.get(key)

        async def __aenter__(self):
            self._parent._waiters[self._key] = self._parent._waiters.get(self._key, 0) + 1
            await self._lock.acquire()
            return self._lock

        async def __aexit__(self, *exc_info):
            self._lock.release()
            remaining = self._parent._waiters.get(self._key, 1) - 1
            if remaining <= 0:
                self._parent._waiters.pop(self._key, None)
                self._parent._locks.pop(self._key, None)
            else:
                self._parent._waiters[self._key] = remaining
            return False

    def acquire(self, key: str) -> "KeyedLocks._Ctx":
        return KeyedLocks._Ctx(self, key)


# ---------------------------------------------------------------------------
# アクセストークン暗号化
# ---------------------------------------------------------------------------

class TokenCipher:
    """Kyash アクセストークンを暗号化して DB に保存するためのラッパ。

    鍵は ``data/secret.key`` (0600) に保存する。DB バックアップが流出しても
    トークン単体では復号できないようにするのが目的。
    """

    def __init__(self, key_path: Path) -> None:
        self._key_path = key_path
        self._fernet = None
        self._init_error: str | None = None
        try:
            from cryptography.fernet import Fernet  # type: ignore

            key = self._load_or_create_key(Fernet)
            self._fernet = Fernet(key)
        # cryptography のネイティブ拡張が壊れている場合 pyo3 の PanicException
        # (BaseException 派生) が飛ぶことがあるため、ここでは BaseException を捕捉して
        # 「トークンを保存しない」degraded モードへ落とす (Bot 全体は停止させない)。
        except BaseException as exc:  # noqa: BLE001  pragma: no cover - 依存欠如時のみ
            self._init_error = safe_error_text(exc)
            logger.error("トークン暗号化を初期化できませんでした: %s", self._init_error)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def _load_or_create_key(self, fernet_cls) -> bytes:
        path = self._key_path
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                return data
        key = fernet_cls.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先に 0600 で作成してから書き込む
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("新しい暗号化キーを作成しました: %s", path)
        return key

    def encrypt(self, plaintext: str) -> str:
        if not self._fernet:
            raise RuntimeError("暗号化が利用できません (cryptography 未インストール)")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not self._fernet:
            raise RuntimeError("暗号化が利用できません (cryptography 未インストール)")
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# レシート (チャージの控え) の署名
# ---------------------------------------------------------------------------

class ReceiptSigner:
    """チャージの控えに署名して、後から真正性を確認できるようにする。

    利用者が「たしかにこの取引で残高を受け取った」と示せる文字列を作る。
    署名鍵はサーバー側だけが持つため、利用者は控えを**偽造できない**。

    形式: ``R1.<payload>.<signature>``

    * ``payload`` … 取引ID・金額・時刻を並べた文字列を base32 化したもの
    * ``signature`` … payload の HMAC-SHA256 の先頭を base32 化したもの

    鍵はトークン暗号鍵とは別に保存する。控えの検証と、Kyash トークンの
    復号を、同じ鍵に頼らせないため。
    """

    #: 署名の長さ (base32 の文字数)。総当たりに対して十分な強度を確保する。
    SIGNATURE_CHARS = 26

    def __init__(self, key_path: Path) -> None:
        self._key_path = key_path
        self._key: bytes | None = None

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        path = self._key_path
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                self._key = data
                return data
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("レシート署名の鍵を作成しました: %s", path)
        self._key = key
        return key

    @staticmethod
    def _canonical(
        *, guild_id: int, user_id: int, tx_id: str, received: int, credited: int,
        completed_at: int,
    ) -> str:
        """署名の対象になる文字列 (順序と区切りを固定する)。"""
        return "|".join((
            config.RECEIPT_VERSION, str(int(guild_id)), str(int(user_id)),
            str(tx_id), str(int(received)), str(int(credited)), str(int(completed_at)),
        ))

    def issue(
        self, *, guild_id: int, user_id: int, tx_id: str, received: int,
        credited: int, completed_at: int,
    ) -> str:
        """控えの文字列を発行する。"""
        canonical = self._canonical(
            guild_id=guild_id, user_id=user_id, tx_id=tx_id, received=received,
            credited=credited, completed_at=completed_at,
        )
        payload = base64.b32encode(canonical.encode("utf-8")).decode("ascii").rstrip("=")
        signature = self._sign(payload)
        return f"{config.RECEIPT_VERSION}.{payload}.{signature}"

    def _sign(self, payload: str) -> str:
        digest = hmac.new(
            self._load_key(), payload.encode("ascii"), hashlib.sha256
        ).digest()
        return base64.b32encode(digest).decode("ascii").rstrip("=")[: self.SIGNATURE_CHARS]

    def verify(self, code: str) -> dict[str, Any] | None:
        """控えを検証して中身を返す (壊れている・署名が合わなければ None)。

        入力は利用者が貼り付けたものなので、どんな文字列でも例外を投げない。
        """
        text = re.sub(r"\s+", "", str(code or ""))
        parts = text.split(".")
        if len(parts) != 3:
            return None
        version, payload, signature = parts
        if version != config.RECEIPT_VERSION:
            return None
        if not re.fullmatch(r"[A-Z2-7]+", payload or "") or not signature:
            return None
        expected = self._sign(payload)
        # 比較は定数時間で行う (署名の総当たりに時間差の手がかりを与えない)
        if not hmac.compare_digest(expected, signature):
            return None
        try:
            padding = "=" * (-len(payload) % 8)
            canonical = base64.b32decode(payload + padding).decode("utf-8")
        except Exception:  # noqa: BLE001 - 壊れた入力でも落とさない
            return None
        fields = canonical.split("|")
        if len(fields) != 7 or fields[0] != config.RECEIPT_VERSION:
            return None
        try:
            return {
                "version": fields[0],
                "guild_id": int(fields[1]),
                "user_id": int(fields[2]),
                "tx_id": fields[3],
                "received": int(fields[4]),
                "credited": int(fields[5]),
                "completed_at": int(fields[6]),
            }
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# JSON 探索 (Kyash 履歴の突合)
# ---------------------------------------------------------------------------

def json_contains_text(obj: Any, needle: str) -> bool:
    """ネストした dict / list の文字列値に needle が含まれるか判定する。

    Kyash 履歴 (timeline) のスキーマは添付モジュールが素通しするだけで
    確定していないため、識別子の突合はスキーマ非依存で行う。
    """
    if not needle:
        return False
    target = needle.lower()
    stack: list[Any] = [obj]
    seen = 0
    while stack and seen < 50_000:
        current = stack.pop()
        seen += 1
        if isinstance(current, str):
            if target in current.lower():
                return True
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, (list, tuple, set)):
            stack.extend(current)
    return False


def safe_json_dumps(obj: Any, *, limit: int = 4000) -> str:
    """監査ログ用の JSON 文字列 (秘密情報はマスク済みの値のみ渡すこと)。"""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return truncate(text, limit)


#: Discord の表示名に使えない (使わせたくない) 文字
#: 見た目を偽装したり、埋め込みを壊したりするものだけを落とす。
_DISPLAY_NAME_BAN_RE = re.compile(r"[`@#\\\r\n\t\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_display_name(raw: Any, *, limit: int) -> str:
    """利用者が入力したロール名・ニックネームを安全な形に整える。

    Discord のメンション記法・コードブロック記法と、表示順を偽装できる
    制御文字を落とす。空白の連続は1つにまとめ、前後の空白は削る。
    残らなかった場合は空文字を返し、呼び出し側で入力エラーにする。
    """
    if raw is None:
        return ""
    text = _DISPLAY_NAME_BAN_RE.sub("", str(raw))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_color(raw: Any) -> int | None:
    """``#FF00AA`` 形式の色指定を整数へ変換する (不正なら None)。"""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if 0 <= raw <= 0xFFFFFF else None
    text = str(raw).strip().lstrip("#").lstrip("0x").lstrip("0X")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return int(text, 16)


def channel_name_from(raw: Any, *, limit: int = 90) -> str:
    """チャンネル名に使える文字列へ変換する。

    Discord はチャンネル名の大文字・空白を自動で変換するが、変換後の名前が
    こちらの想定と食い違うと後片付けで取り違える恐れがある。そのため
    あらかじめ小文字・ハイフン区切りへ正規化しておく。
    """
    text = str(raw or "").lower()
    text = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff\uff66-\uff9f-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:limit]


#: 利用者へそのまま見せてよいリンクの形式
#: 見せたリンクは押されるため、http(s) 以外のスキームは受け付けない。
_SAFE_LINK_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::\d+)?(?:/[^\s<>\"']*)?$")


# ---------------------------------------------------------------------------
# Discord の絵文字
# ---------------------------------------------------------------------------
# ボタンや選択メニューに渡せるのは **Unicode の絵文字** だけ。
# ただの記号や文字を渡すと Discord は 400 Invalid Form Body を返し、
# そのメッセージ全体が送信できなくなる。利用者からは
# 「アプリケーションは応答しませんでした」と見え、ボタンが押せなくなる。
#
# 例: Litecoin の "Ł" (U+0141) は絵文字ではないため、方式選択メニューが
#     まるごと送れず、チャージボタンが反応しなくなっていた。

#: Unicode 絵文字として扱われる符号位置の範囲
_EMOJI_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE),          # © ®
    (0x203C, 0x203C), (0x2049, 0x2049),          # ‼ ⁉
    (0x2122, 0x2122), (0x2139, 0x2139),          # ™ ℹ
    (0x2194, 0x21AA),                            # ↔ 〜 ↪
    (0x231A, 0x231B), (0x2328, 0x2328),          # ⌚ ⌛ ⌨
    (0x23CF, 0x23FA),                            # ⏏ 〜 ⏺
    (0x24C2, 0x24C2),                            # Ⓜ
    (0x25AA, 0x25AB), (0x25B6, 0x25B6),          # ▪ ▫ ▶
    (0x25C0, 0x25C0), (0x25FB, 0x25FE),          # ◀ ◻ 〜 ◾
    (0x2600, 0x27BF),                            # ☀ 〜 ➿
    (0x2934, 0x2935), (0x2B00, 0x2BFF),          # ⤴ ⤵ ⬅ 〜 ⭐
    (0x3030, 0x3030), (0x303D, 0x303D),          # 〰 〽
    (0x3297, 0x3297), (0x3299, 0x3299),          # ㊗ ㊙
    (0x1F000, 0x1FAFF),                          # 🀄 〜 🫿 (大半の絵文字)
)

#: 単体では絵文字にならないが、絵文字の一部として現れる符号位置
_EMOJI_JOINERS: Final[frozenset[int]] = frozenset({
    0xFE0F,  # 異体字セレクタ (絵文字表示)
    0xFE0E,  # 異体字セレクタ (文字表示)
    0x200D,  # ゼロ幅接合子 (👨\u200d👩 のような合成)
    0x20E3,  # 囲み記号 (1️⃣ の ⃣)
})

#: カスタム絵文字 (<:name:id> / <a:name:id>)
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")


def is_discord_emoji(value: Any) -> bool:
    """Discord のボタン・選択メニューに渡せる絵文字かどうか。

    受け付けるのは Unicode 絵文字 (国旗・キーキャップ・合成絵文字を含む) と
    カスタム絵文字の記法のみ。判定に迷う値は **False** を返す
    (送信できないものを送るより、絵文字を出さない方が害が小さい)。
    """
    if not isinstance(value, str) or not value:
        return False
    if _CUSTOM_EMOJI_RE.match(value):
        return True
    if len(value) > 32:
        return False
    codepoints = [ord(char) for char in value]
    if all(cp in _EMOJI_JOINERS for cp in codepoints):
        return False  # 接合子だけでは絵文字にならない
    for cp in codepoints:
        if cp in _EMOJI_JOINERS:
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF:
            continue  # 国旗 (地域指示記号)
        if cp in (0x23, 0x2A) or 0x30 <= cp <= 0x39:
            continue  # キーキャップの土台 (# * 0-9)
        if not any(low <= cp <= high for low, high in _EMOJI_RANGES):
            return False
    return True


def safe_emoji(value: Any) -> str | None:
    """絵文字として使える値だけを返す (使えなければ None)。

    Discord のコンポーネントへ渡す直前に通す。設定ミスや将来の書き換えで
    絵文字でない文字が混ざっても、**絵文字が出ないだけ**で済み、
    ボタンや選択メニューが送れなくなることはない。
    """
    if is_discord_emoji(value):
        return str(value)
    if value:
        logger.warning(
            "絵文字として使えない値のため表示を省略します: %r", str(value)[:32]
        )
    return None


def is_safe_link(url: Any) -> bool:
    """利用者へ表示してよいリンクか判定する。

    ``https://`` のみを許可する。``javascript:`` のような危険なスキームや、
    空白・山括弧を含む値 (Embed を壊す・別のリンクに見せかける) を弾く。
    """
    text = str(url or "").strip()
    if not text or len(text) > 500:
        return False
    return bool(_SAFE_LINK_RE.match(text))


def load_json_dict(text: Any) -> dict[str, Any]:
    """DB に保存した JSON 文字列を辞書として読み戻す (壊れていても落ちない)。

    商品の追加設定 (payload) のように「無ければ既定値で動く」データに使う。
    dict 以外が入っていた場合も空の辞書として扱う。
    """
    if not text:
        return {}
    if isinstance(text, dict):
        return dict(text)
    try:
        value = json.loads(str(text))
    except (TypeError, ValueError):
        logger.warning("JSON として読めない設定値を無視しました: %s", truncate(str(text), 120))
        return {}
    return value if isinstance(value, dict) else {}


def chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """シーケンスを size 件ずつに分割する。"""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
