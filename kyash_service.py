"""添付 Kyash モジュール (Kyasher 1.5.0) の安全な抽象化レイヤ。

添付モジュールは同期 (requests) 実装のため、専用の単一ワーカースレッドで実行し、
Discord のイベントループをブロックしない。受取用アカウントは1つだけなので、
すべての Kyash 通信を ``asyncio.Lock`` + 単一スレッドで直列化する (並列受取の禁止)。

上流実装の既知の不具合と、本レイヤでの対処
------------------------------------------
``vendor/Kyasher/main.py`` を解析した結果、以下の不具合が存在する。いずれも
上流コードは書き換えず (監査可能性のため無変更で同梱)、本レイヤで回避する。

1. ``get_wallet()`` (line 147): NamedTuple ``Wallet`` のフィールドは ``uuid`` だが
   ``wallet_uuid=`` で生成しており、常に ``TypeError`` になる。
   → まず本来の呼び出しを試み、``TypeError`` の場合のみ、モジュールと同一の
     エンドポイント/ヘッダ (``/v1/me/primary_wallet``) で取得し直す。
2. ``link_check()`` (lines 263/282): 未定義属性 ``self.link_uuid`` を参照し、
   さらに NamedTuple ``LinkInfo`` の生成キーワードが全て不一致。加えて金額に
   カンマが含まれると ``int()`` が失敗する。bare ``except`` で握り潰されるため
   常に「処理済みのリンク」エラーになる。
   → モジュール自身が ``link_recieve()`` で使っているスクレイピング手順
     (lines 300-310) と ``/v1/links/{uuid}`` 参照を、同じ手順で再実装する。
3. ``__init__`` UUIDログイン経路 (line 73): ``headers["X-Auth"]`` に引数の
   ``access_token`` (=None) を代入しており、取得したトークンがヘッダへ入らない。
   → 生成直後に ``headers["X-Auth"]`` を実トークンで補正する。
4. ``send_to_link()``: 未使用 (本Botは請求リンクへ送金しないため呼び出さない)。
5. すべての ``requests`` 呼び出しに timeout 指定がない。
   → ``socket.setdefaulttimeout()`` で下限を強制し、さらに ``asyncio.wait_for``
     で待ち時間を制限する (無限待機の禁止)。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import config
import utils

logger = logging.getLogger(config.LOGGER_KYASH)

# 同梱した添付モジュールを import できるようにする
if str(config.VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(config.VENDOR_DIR))

import requests  # noqa: E402  (Kyasher の依存)
from bs4 import BeautifulSoup  # noqa: E402

from Kyasher import Kyash, KyashError, KyashLoginError, NetWorkError  # noqa: E402
import Kyasher as kyasher_pkg  # noqa: E402

KYASH_MODULE_VERSION: str = getattr(kyasher_pkg, "__version__", "unknown")

#: 添付モジュールが使用しているエンドポイント (URLは変更しない)
_WALLET_URL = "https://api.kyash.me/v1/me/primary_wallet"
_LINK_META_URL = "https://api.kyash.me/v1/links/{uuid}"
_PAYMENT_PAGE_PREFIX = "https://kyash.me/payments/"


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------
class KyashServiceError(Exception):
    """Kyash 連携の基底例外。"""

    error_code: str = config.ErrorCode.UNKNOWN_ERROR


class KyashUnavailable(KyashServiceError):
    """受取用アカウントが未登録・利用不可。"""

    error_code = config.ErrorCode.KYASH_UNAVAILABLE


class KyashAuthError(KyashServiceError):
    """認証エラー (セッション失効・再ログインが必要)。"""

    error_code = config.ErrorCode.KYASH_AUTH_ERROR


class KyashTimeoutError(KyashServiceError):
    """タイムアウト (結果が不明な可能性がある)。"""

    error_code = config.ErrorCode.KYASH_TIMEOUT


class KyashNetworkError(KyashServiceError):
    """ネットワーク障害。"""

    error_code = config.ErrorCode.KYASH_NETWORK_ERROR


class KyashRejectedError(KyashServiceError):
    """Kyash 側が処理を拒否した (業務的な失敗)。"""

    error_code = config.ErrorCode.KYASH_REJECTED


class KyashProtocolError(KyashServiceError):
    """想定外のレスポンス形式。"""

    error_code = config.ErrorCode.UNKNOWN_ERROR


class LinkInvalidError(KyashServiceError):
    """送金リンクとして利用できない。"""

    error_code = config.ErrorCode.INVALID_LINK


class LinkIsClaimError(KyashServiceError):
    """請求リンクであり受け取れない。"""

    error_code = config.ErrorCode.LINK_IS_CLAIM


class WalletLimitError(KyashServiceError):
    """受取用アカウントの残高がしきい値を超えており、これ以上受け取れない。"""

    error_code = config.ErrorCode.WALLET_LIMIT


class NoCapacityError(KyashServiceError):
    """受け取れる受取用アカウントが1つも無い。

    複数登録した全アカウントがしきい値に達している、またはログインできて
    いない状態。``WalletLimitError`` と分けることで、利用者向けの案内を
    「一時的に受け付けられない」に寄せられる。
    """

    error_code = config.ErrorCode.NO_KYASH_CAPACITY


# ---------------------------------------------------------------------------
# データモデル
# ---------------------------------------------------------------------------
@dataclass
class AccountSlot:
    """受取用 Kyash アカウント1件ぶんの状態。

    v4 から受取アカウントを複数登録できるようにしたため、従来
    ``KyashService`` が1組だけ持っていた状態をこの単位へ切り出した。
    ``client`` はログイン済みのときだけ入る。
    """

    id: int
    label: str
    client: Kyash | None = None
    status: str = config.KyashAccountStatus.UNCONFIGURED
    email: str | None = None
    username: str | None = None
    wallet_uuid: str | None = None
    wallet_balance: int | None = None
    threshold: int = 0
    priority: int = 0
    enabled: bool = True
    last_error: str | None = None
    last_checked_at: int | None = None
    token_issued_at: int | None = None

    @property
    def active(self) -> bool:
        """受取に使える状態か。"""
        return (
            self.enabled
            and self.client is not None
            and self.status == config.KyashAccountStatus.ACTIVE
        )

    @property
    def token_expires_at(self) -> int | None:
        if not self.token_issued_at:
            return None
        return self.token_issued_at + config.KYASH_TOKEN_LIFETIME_DAYS * 86400

    @property
    def token_days_left(self) -> float | None:
        expires = self.token_expires_at
        return None if expires is None else (expires - utils.now_ts()) / 86400

    @property
    def token_expiring_soon(self) -> bool:
        days = self.token_days_left
        return days is not None and days <= config.KYASH_TOKEN_WARN_DAYS

    @property
    def limit_reached(self) -> bool:
        """しきい値に達していて、もう受け取れないか。"""
        if self.threshold <= 0 or self.wallet_balance is None:
            return False
        return self.wallet_balance >= self.threshold

    def headroom(self) -> int | None:
        """しきい値までの余裕額 (未設定なら None = 無制限)。"""
        if self.threshold <= 0 or self.wallet_balance is None:
            return None
        return max(0, self.threshold - self.wallet_balance)

    def has_capacity(self, amount: int) -> bool:
        """その額を受け取ってもしきい値を超えないか。"""
        if self.threshold <= 0 or self.wallet_balance is None:
            return True
        return self.wallet_balance + max(0, amount) <= self.threshold

    def snapshot(self) -> dict[str, Any]:
        """管理者表示用 (秘密情報は含めない)。"""
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "enabled": self.enabled,
            "priority": self.priority,
            "logged_in": self.client is not None,
            "username": self.username,
            "wallet_uuid": utils.mask_identifier(self.wallet_uuid, keep=8),
            "wallet_balance": self.wallet_balance,
            "threshold": self.threshold,
            "headroom": self.headroom(),
            "limit_reached": self.limit_reached,
            "token_issued_at": self.token_issued_at,
            "token_expires_at": self.token_expires_at,
            "token_days_left": self.token_days_left,
            "token_expiring_soon": self.token_expiring_soon,
            "last_error": self.last_error,
            "last_checked_at": self.last_checked_at,
        }



@dataclass(slots=True)
class LinkInfo:
    """送金リンクの検証結果。完全なURLは保持しない。"""

    amount: int
    uuid: str
    send_to_me: bool
    public_id: str | None = None
    sender_name: str | None = None


@dataclass(slots=True)
class WalletInfo:
    uuid: str | None
    all_balance: int
    money: int
    value: int
    point: int


@dataclass(slots=True)
class ProfileInfo:
    username: str | None
    is_kyc: bool


class Verdict:
    """受取確認の結論。"""

    CONFIRMED = "CONFIRMED"          # 実際に受取済みであることを確認
    NO_EVIDENCE = "NO_EVIDENCE"      # 確認できる情報は取得できたが受取の痕跡がない
    UNAVAILABLE = "UNAVAILABLE"      # 確認情報自体が取得できない (判定不能)


@dataclass(slots=True)
class VerificationResult:
    verdict: str
    detail: str = ""
    wallet_after: int | None = None


@dataclass(slots=True)
class PendingLogin:
    """OTP 入力待ちのログインセッション (メモリ上のみ)。"""

    client: Kyash
    email: str
    #: ログイン先の受取用アカウント ID
    account_id: int = 0
    created_at: int = field(default_factory=utils.now_ts)

    @property
    def expired(self) -> bool:
        return utils.now_ts() - self.created_at > config.KYASH_LOGIN_PENDING_TTL


# ---------------------------------------------------------------------------
# 例外分類
# ---------------------------------------------------------------------------
_AUTH_HINTS = ("auth", "unauthorized", "token", "認証", "ログイン", "セッション", "期限")


def classify_exception(exc: BaseException) -> KyashServiceError:
    """添付モジュール / requests が投げた例外を、本レイヤの例外へ分類する。"""
    if isinstance(exc, KyashServiceError):
        return exc
    if isinstance(exc, KyashLoginError):
        return KyashAuthError(str(exc))
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, socket.timeout,
                        requests.exceptions.Timeout)):
        return KyashTimeoutError("通信がタイムアウトしました")
    if isinstance(exc, NetWorkError):
        return KyashNetworkError(str(exc))
    if isinstance(exc, KyashError):
        message = str(exc)
        lowered = message.lower()
        if any(hint in lowered for hint in _AUTH_HINTS):
            return KyashAuthError(message)
        return KyashRejectedError(message)
    if isinstance(exc, requests.exceptions.RequestException):
        return KyashNetworkError(utils.safe_error_text(exc))
    if isinstance(exc, OSError):
        return KyashNetworkError(utils.safe_error_text(exc))
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError, IndexError)):
        # レスポンス形式の想定違い (JSON でない / キー欠落など)
        return KyashProtocolError(utils.safe_error_text(exc))
    return KyashProtocolError(utils.safe_error_text(exc))


# ---------------------------------------------------------------------------
# 同期ヘルパ (すべて専用スレッド上で実行される)
# ---------------------------------------------------------------------------

def _check_api_payload(payload: Any, *, context: str) -> dict[str, Any]:
    """Kyash API の JSON レスポンスを検証する (モジュールと同じ ``code`` 判定)。"""
    if not isinstance(payload, dict):
        raise KyashProtocolError(f"{context}: 想定外のレスポンス形式")
    code = payload.get("code")
    if code != 200:
        message = ""
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        raise KyashRejectedError(f"{context}: {message or f'code={code}'}")
    return payload


def _wallet_from_payload(payload: dict[str, Any]) -> WalletInfo:
    """``/v1/me/primary_wallet`` のレスポンスを WalletInfo へ変換する。

    上流 ``get_wallet()`` と同じキー構成を参照する。
    """
    data = payload["result"]["data"]
    balance = data["balance"]
    breakdown = balance.get("amountBreakdown", {})
    return WalletInfo(
        uuid=data.get("uuid"),
        all_balance=int(balance["amount"]),
        money=int(breakdown.get("kyashMoney") or 0),
        value=int(breakdown.get("kyashValue") or 0),
        point=int((data.get("pointBalance") or {}).get("availableAmount") or 0),
    )


def _sync_get_wallet(client: Kyash) -> WalletInfo:
    """残高照会。上流の NamedTuple 生成バグ (line 147) を回避する。"""
    try:
        wallet = client.get_wallet()
    except TypeError:
        logger.debug("get_wallet() の上流バグを検出したため互換経路で取得します")
    else:
        return WalletInfo(
            uuid=getattr(wallet, "uuid", None),
            all_balance=int(wallet.all_balance),
            money=int(wallet.money),
            value=int(wallet.value),
            point=int(wallet.point),
        )
    response = requests.get(
        _WALLET_URL,
        headers=client.headers,
        proxies=client.proxy,
        timeout=config.KYASH_SOCKET_TIMEOUT,
    )
    payload = _check_api_payload(response.json(), context="wallet")
    return _wallet_from_payload(payload)


def _sync_get_profile(client: Kyash) -> ProfileInfo:
    profile = client.get_profile()
    return ProfileInfo(username=profile.username, is_kyc=bool(profile.is_kyc))


def _sync_get_history(client: Kyash, limit: int) -> list[Any]:
    history = client.get_history(limit=limit)
    timelines = history.timelines
    return list(timelines) if isinstance(timelines, (list, tuple)) else []


def _attr_text(node: Any, name: str) -> str:
    """BeautifulSoup の属性値を文字列として取り出す。

    複数値属性ではリストが返ることがあるため、その場合は先頭要素を使う。
    """
    value = node.get(name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value)


def _scrape_link_page(client: Kyash, url: str) -> tuple[int, str, bool]:
    """送金 / 請求リンクのページから (金額, リンクUUID, 受取リンクか) を取得する。

    添付モジュール ``link_recieve()`` (lines 300-310) と同一の手順。
    上流と違い、金額のカンマを除去してから整数化する。
    """
    if _PAYMENT_PAGE_PREFIX not in url:
        url = _PAYMENT_PAGE_PREFIX + url
    response = requests.get(url, proxies=client.proxy, timeout=config.KYASH_SOCKET_TIMEOUT)
    if response.status_code >= 500:
        raise KyashNetworkError(f"リンクページの取得に失敗しました (HTTP {response.status_code})")
    soup = BeautifulSoup(response.text, "html.parser")

    send_amount = soup.find(class_="amountText text_send")
    send_button = soup.find(class_="btn_send")
    if send_amount is not None and send_button is not None:
        raw_uuid = _attr_text(send_button, "data-href-app")
        link_uuid = raw_uuid.replace("kyash://claim/", "").strip()
        amount = utils.parse_money_text(send_amount.text)
        send_to_me = True
    else:
        request_amount = soup.find(class_="amountText text_request")
        request_button = soup.find(class_="btn_request")
        if request_amount is None or request_button is None:
            raise LinkInvalidError("受取可能な送金リンクではありません (処理済みの可能性があります)")
        raw_uuid = _attr_text(request_button, "data-href-app")
        link_uuid = raw_uuid.replace("kyash://request/u/", "").strip()
        amount = utils.parse_money_text(request_amount.text)
        send_to_me = False

    if not link_uuid:
        raise LinkInvalidError("リンクの識別子を取得できませんでした")
    if amount is None or amount <= 0:
        raise LinkInvalidError("リンクの金額を取得できませんでした")
    return amount, link_uuid, send_to_me


def _sync_link_meta(client: Kyash, link_uuid: str) -> dict[str, Any] | None:
    """``/v1/links/{uuid}`` を参照する (補助情報。失敗しても致命的にしない)。"""
    try:
        response = requests.get(
            _LINK_META_URL.format(uuid=link_uuid),
            headers=client.headers,
            proxies=client.proxy,
            timeout=config.KYASH_SOCKET_TIMEOUT,
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - 補助情報のため失敗を吸収する
        logger.debug("リンク情報の取得に失敗しました: %s", utils.safe_error_text(exc))
        return None
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return None
    return payload


def _sync_link_check(client: Kyash, url: str) -> LinkInfo:
    """リンク検証 (上流 ``link_check()`` の代替実装)。"""
    amount, link_uuid, send_to_me = _scrape_link_page(client, url)
    public_id: str | None = None
    sender_name: str | None = None
    payload = _sync_link_meta(client, link_uuid)
    if payload:
        target = ((payload.get("result") or {}).get("data") or {}).get("target") or {}
        public_id = target.get("publicId")
        sender_name = target.get("userName")
    return LinkInfo(
        amount=amount,
        uuid=link_uuid,
        send_to_me=send_to_me,
        public_id=public_id,
        sender_name=sender_name,
    )


@dataclass(slots=True)
class ClaimLink:
    """Bot が発行した請求リンク。

    利用者が支払う先なので、URL 自体に金銭的価値はない
    (これを知っても Bot へ送る操作しかできない)。利用者の送金リンクとは
    性質が異なるため、再表示とキャンセルのために URL を保持してよい。
    """

    url: str
    link_id: str        #: URL の末尾 (kyash.me/payments/<link_id>)
    link_uuid: str      #: 履歴との突合に使う識別子
    amount: int
    account_id: int = 0  #: 発行した受取用アカウント


def _sync_create_claim_link(client: Kyash, amount: int, message: str) -> tuple[str, dict[str, Any]]:
    """請求リンクを発行する (添付モジュール ``create_link(is_claim=True)``)。"""
    result = client.create_link(amount=int(amount), message=message, is_claim=True)
    url = getattr(result, "link", None)
    if not isinstance(url, str) or not url:
        raise KyashServiceError("請求リンクの URL を取得できませんでした")
    raw = getattr(result, "raw", None)
    return url, raw if isinstance(raw, dict) else {}


def _sync_cancel_link(client: Kyash, link_uuid: str) -> dict[str, Any]:
    """発行済みリンクを無効化する (添付モジュール ``link_cancel``)。"""
    result = client.link_cancel(link_uuid=link_uuid)
    return result if isinstance(result, dict) else {}


def _sync_link_receive(client: Kyash, link_uuid: str) -> dict[str, Any]:
    """送金リンクの受取 (link_uuid 指定でスクレイピングを省略する)。"""
    result = client.link_recieve(link_uuid=link_uuid)
    if not isinstance(result, dict):
        raise KyashProtocolError("受取レスポンスの形式が想定外です")
    if result.get("code") != 200:
        raise KyashRejectedError("受取が完了しませんでした")
    return result


# ---------------------------------------------------------------------------
# サービス
# ---------------------------------------------------------------------------
class KyashService:
    """受取用 Kyash アカウントを扱うサービス。

    * すべての通信を単一スレッド + Lock で直列化する
    * 認証情報はログにも DB にも平文で残さない (トークンは暗号化して保存)
    * パスワードは保存しない (再ログイン時に管理者が都度入力する)
    """

    def __init__(self, db: Any, cipher: utils.TokenCipher) -> None:
        self._db = db
        self._cipher = cipher
        # 受取 (link_receive) は必ず1件ずつ直列化する。
        # 参照系 (リンク検証・残高照会・履歴) は別系統にして、
        # 遅いページ取得が受取キュー全体を止めないようにする。
        self._receive_lock = asyncio.Lock()
        self._receive_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kyash-recv")
        self._read_lock = asyncio.Lock()
        self._read_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kyash-read")
        self._pending_logins: dict[int, PendingLogin] = {}
        #: 受取用アカウント {id: AccountSlot}。優先度順に使う。
        self._slots: dict[int, AccountSlot] = {}
        # 添付モジュールは timeout を指定しないため、socket 側で上限を強制する
        if socket.getdefaulttimeout() is None:
            socket.setdefaulttimeout(config.KYASH_SOCKET_TIMEOUT)
            logger.info("socket のデフォルトタイムアウトを %s 秒に設定しました",
                        config.KYASH_SOCKET_TIMEOUT)

    # ------------------------------------------------------------------
    # アカウント枠
    # ------------------------------------------------------------------
    def slots(self) -> list[AccountSlot]:
        """全アカウントを優先度順で返す。"""
        return sorted(self._slots.values(), key=lambda s: (s.priority, s.id))

    def usable_slots(self) -> list[AccountSlot]:
        """受取に使えるアカウントを優先度順で返す。"""
        return [slot for slot in self.slots() if slot.active]

    def get_slot(self, account_id: int | None) -> AccountSlot | None:
        """ID でアカウントを引く。

        ``None`` および未知の ID では、v3 までの単一アカウント時代に作られた
        取引を扱えるよう、最も古いアカウントへフォールバックする。
        """
        if account_id is not None:
            slot = self._slots.get(int(account_id))
            if slot is not None:
                return slot
        ordered = sorted(self._slots.values(), key=lambda s: s.id)
        return ordered[0] if ordered else None

    def find_slot_by_label(self, label: str) -> AccountSlot | None:
        target = label.strip().lower()
        for slot in self._slots.values():
            if slot.label.lower() == target:
                return slot
        return None

    @property
    def primary(self) -> AccountSlot | None:
        """代表のアカウント (使えるものを優先し、無ければ先頭)。"""
        usable = self.usable_slots()
        if usable:
            return usable[0]
        ordered = self.slots()
        return ordered[0] if ordered else None

    def pick_slot(self, amount: int = 0) -> AccountSlot:
        """その額を受け取れるアカウントを選ぶ。

        優先度の小さい順に見て、しきい値に余裕がある最初のアカウントを返す。

        Raises:
            NoCapacityError: 受け取れるアカウントが1つも無い場合。
        """
        usable = self.usable_slots()
        if not usable:
            raise NoCapacityError("利用できる受取用アカウントがありません")
        for slot in usable:
            if slot.has_capacity(amount):
                return slot
        raise NoCapacityError(
            f"全 {len(usable)} アカウントが残高しきい値に達しています (要求 {amount})"
        )

    # ------------------------------------------------------------------
    # 状態 (代表アカウント / 全体集約)
    # ------------------------------------------------------------------
    @property
    def status(self) -> str:
        slot = self.primary
        return slot.status if slot else config.KyashAccountStatus.UNCONFIGURED

    @property
    def is_usable(self) -> bool:
        """新規チャージを受け付けてよい状態か (どれか1つでも使えればよい)。"""
        return bool(self.usable_slots())

    @property
    def token_issued_at(self) -> int | None:
        slot = self.primary
        return slot.token_issued_at if slot else None

    @property
    def token_expires_at(self) -> int | None:
        """アクセストークンの推定失効時刻 (代表アカウント)。"""
        slot = self.primary
        return slot.token_expires_at if slot else None

    @property
    def token_days_left(self) -> float | None:
        """トークンの残り日数。複数ある場合は**最も近い失効**を返す。"""
        values = [
            slot.token_days_left for slot in self.slots()
            if slot.token_days_left is not None and slot.enabled
        ]
        return min(values) if values else None

    @property
    def token_expiring_soon(self) -> bool:
        """どれか1つでも失効が近いか。"""
        days = self.token_days_left
        return days is not None and days <= config.KYASH_TOKEN_WARN_DAYS

    @property
    def expiring_slots(self) -> list[AccountSlot]:
        """失効が近いアカウント (警告に使う)。"""
        return [slot for slot in self.slots() if slot.enabled and slot.token_expiring_soon]

    @property
    def wallet_threshold(self) -> int:
        """代表アカウントの残高しきい値 (0=無効)。"""
        slot = self.primary
        return slot.threshold if slot else 0

    @property
    def wallet_limit_reached(self) -> bool:
        """**すべての**アカウントがしきい値に達しているか。

        1つでも余裕があればチャージを止めない。
        """
        usable = self.usable_slots()
        if not usable:
            return False
        return all(slot.limit_reached for slot in usable)

    def wallet_headroom(self) -> int | None:
        """しきい値までの余裕額の合計 (すべて無制限なら None)。"""
        usable = self.usable_slots()
        values = [slot.headroom() for slot in usable]
        if not values or any(v is None for v in values):
            return None
        return sum(v for v in values if v is not None)

    async def set_wallet_threshold(self, threshold: int, *, account_id: int | None = None) -> None:
        """残高しきい値を設定して永続化する。"""
        slot = self.get_slot(account_id)
        if slot is None:
            raise KyashServiceError("受取用アカウントが登録されていません")
        slot.threshold = max(0, int(threshold))
        await self._db.set_kyash_account_options(slot.id, threshold=slot.threshold)

    def check_wallet_capacity(self, amount: int) -> None:
        """受け取り予定額を扱えるアカウントがあるか確認する。

        Raises:
            WalletLimitError: 使えるアカウントはあるが、どれも余裕がない場合。
            NoCapacityError: 使えるアカウントが1つも無い場合。
        """
        usable = self.usable_slots()
        if not usable:
            return  # ログイン状態の判定は preflight 側で行う
        if any(slot.has_capacity(amount) for slot in usable):
            return
        raise WalletLimitError(
            f"受取用アカウント {len(usable)} 件すべてが残高しきい値に達します (要求 {amount})"
        )

    @property
    def last_error(self) -> str | None:
        """直近のエラー (どれかのアカウントで出ていれば最初のもの)。"""
        for slot in self.slots():
            if slot.last_error:
                return slot.last_error
        return None

    @property
    def last_checked_at(self) -> int | None:
        values = [s.last_checked_at for s in self.slots() if s.last_checked_at]
        return max(values) if values else None

    @property
    def username(self) -> str | None:
        slot = self.primary
        return slot.username if slot else None

    @property
    def wallet_uuid(self) -> str | None:
        slot = self.primary
        return slot.wallet_uuid if slot else None

    @property
    def last_wallet_balance(self) -> int | None:
        """使えるアカウントの残高合計 (受取余力の目安)。"""
        values = [s.wallet_balance for s in self.usable_slots() if s.wallet_balance is not None]
        return sum(values) if values else None

    def status_snapshot(self) -> dict[str, Any]:
        """管理者表示用のスナップショット (秘密情報は含めない)。

        複数アカウントに対応したため、全体の要約と各アカウントの明細を返す。
        既存の表示が壊れないよう、従来のキーも維持する。
        """
        slots = self.slots()
        usable = self.usable_slots()
        return {
            "status": self.status,
            "logged_in": bool(usable),
            "username": self.username,
            "wallet_uuid": utils.mask_identifier(self.wallet_uuid, keep=8),
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
            "module_version": KYASH_MODULE_VERSION,
            "pending_logins": len(self._pending_logins),
            "token_issued_at": self.token_issued_at,
            "token_expires_at": self.token_expires_at,
            "token_days_left": self.token_days_left,
            "token_expiring_soon": self.token_expiring_soon,
            "wallet_threshold": self.wallet_threshold,
            "wallet_balance": self.last_wallet_balance,
            "wallet_headroom": self.wallet_headroom(),
            # v4: 複数アカウントの明細
            "account_count": len(slots),
            "usable_count": len(usable),
            "accounts": [slot.snapshot() for slot in slots],
        }

    # ------------------------------------------------------------------
    # 実行基盤
    # ------------------------------------------------------------------
    async def _call(self, fn: Callable[[], Any], *, context: str,
                    budget: float | None = None, exclusive: bool = False) -> Any:
        """同期処理を専用スレッドで直列実行する (無限待機を禁止)。

        Args:
            exclusive: True の場合は受取系の専用ロック/スレッドを使う
                (受取・ログインなどクライアント状態を変える操作)。
                False の場合は参照系の系統を使い、受取キューをブロックしない。
        """
        loop = asyncio.get_running_loop()
        lock = self._receive_lock if exclusive else self._read_lock
        executor = self._receive_executor if exclusive else self._read_executor
        async with lock:
            future = loop.run_in_executor(executor, fn)
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=budget or config.KYASH_CALL_BUDGET
                )
            except asyncio.TimeoutError as exc:
                logger.error("Kyash 通信がタイムアウトしました (%s)", context)
                raise KyashTimeoutError(f"{context} がタイムアウトしました") from exc
            except BaseException as exc:
                raise classify_exception(exc) from exc

    def _require_client(self, account_id: int | None = None) -> Kyash:
        """参照系で使うクライアントを取り出す。

        ``account_id`` を省略した場合は使えるアカウントの先頭を使う
        (リンクページの取得のように、どのアカウントでも同じ結果になる操作用)。
        """
        slot = self.get_slot(account_id) if account_id is not None else self.primary
        if slot is None or slot.client is None:
            raise KyashUnavailable("受取用Kyashアカウントが登録されていません")
        return slot.client

    def _require_slot(self, account_id: int | None) -> AccountSlot:
        slot = self.get_slot(account_id)
        if slot is None or slot.client is None:
            raise KyashUnavailable("受取用Kyashアカウントが登録されていません")
        return slot

    @staticmethod
    def _fix_auth_header(client: Kyash) -> None:
        """上流 ``__init__`` (line 73) のヘッダ設定漏れを補正する。"""
        token = getattr(client, "access_token", None)
        if token and client.headers.get("X-Auth") != token:
            client.headers["X-Auth"] = token
            logger.debug("X-Auth ヘッダを補正しました")

    async def _set_slot_status(
        self,
        slot: AccountSlot,
        status: str,
        *,
        error: str | None = None,
        persist: bool = True,
        wallet_uuid: str | None = None,
        username: str | None = None,
        wallet_balance: int | None = None,
    ) -> None:
        """アカウント1件の状態を更新して永続化する。"""
        slot.status = status
        slot.last_error = error
        slot.last_checked_at = utils.now_ts()
        if wallet_uuid:
            slot.wallet_uuid = wallet_uuid
        if username:
            slot.username = username
        if wallet_balance is not None:
            slot.wallet_balance = wallet_balance
        if persist:
            try:
                await self._db.update_kyash_account_status(
                    slot.id, status, last_error=error, wallet_uuid=wallet_uuid,
                    username=username, wallet_balance=wallet_balance,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Kyash 状態の保存に失敗しました (%s): %s",
                             slot.label, utils.safe_error_text(exc))

    # ------------------------------------------------------------------
    # ログイン
    # ------------------------------------------------------------------
    async def load_accounts(self) -> str:
        """DB からアカウント一覧を読み込み、保存済みトークンで復元する (起動時)。

        v3 までの単一アカウントは ``migrate_kyash_account`` で取り込む。
        1つでも復元できれば、そのアカウントでチャージを受け付けられる。

        Returns:
            代表アカウントの状態 (アカウントが無ければ UNCONFIGURED)。
        """
        await self._db.migrate_kyash_account()
        rows = await self._db.list_kyash_accounts()
        self._slots = {}
        for row in rows:
            slot = AccountSlot(
                id=int(row["id"]),
                label=str(row["label"]),
                status=str(row["status"]),
                email=row["email"],
                username=row["username"],
                wallet_uuid=row["wallet_uuid"],
                wallet_balance=row["wallet_balance"],
                threshold=int(row["threshold"] or 0),
                priority=int(row["priority"] or 0),
                enabled=bool(row["enabled"]),
                token_issued_at=row["token_issued_at"],
                last_checked_at=row["last_checked_at"],
                last_error=row["last_error"],
            )
            self._slots[slot.id] = slot
            await self._restore_slot(slot, row["access_token_enc"])
        usable = len(self.usable_slots())
        if self._slots:
            logger.info("受取用アカウントを %d 件読み込みました (利用可 %d 件)",
                        len(self._slots), usable)
        return self.status

    async def _restore_slot(self, slot: AccountSlot, token_enc: str | None) -> None:
        """1件ぶんのセッションを保存済みトークンから復元する。"""
        if not token_enc:
            slot.status = config.KyashAccountStatus.UNCONFIGURED
            return
        if not self._cipher.available:
            slot.status = config.KyashAccountStatus.ERROR
            slot.last_error = "トークンの復号に必要な cryptography が利用できません"
            return
        try:
            token = self._cipher.decrypt(token_enc)
        except Exception as exc:  # noqa: BLE001
            logger.error("アクセストークンの復号に失敗しました (%s): %s",
                         slot.label, utils.safe_error_text(exc))
            await self._set_slot_status(
                slot, config.KyashAccountStatus.AUTH_REQUIRED,
                error="保存済みトークンを復号できません",
            )
            return

        def _build() -> Kyash:
            return Kyash(access_token=token)

        try:
            client = await self._call(_build, context="セッション復元", exclusive=True)
        except KyashServiceError as exc:
            await self._set_slot_status(
                slot, config.KyashAccountStatus.ERROR, error=str(exc)
            )
            return
        slot.client = client
        self._fix_auth_header(client)
        logger.info("保存済みトークンでセッションを復元しました (%s)", slot.label)
        await self.health_check(slot.id)

    async def restore_from_db(self) -> str:
        """起動時の復元 (``load_accounts`` の別名。既存の呼び出し名を維持する)。"""
        return await self.load_accounts()

    async def add_account(self, label: str, *, threshold: int = 0, priority: int = 0) -> int:
        """アカウント枠を追加する (ログインは /kyash login で行う)。"""
        name = label.strip()[:40]
        if not name:
            raise KyashServiceError("アカウント名を指定してください")
        account_id = await self._db.add_kyash_account(
            label=name, threshold=threshold, priority=priority
        )
        self._slots[account_id] = AccountSlot(
            id=account_id, label=name, threshold=threshold, priority=priority
        )
        logger.info("受取用アカウント枠を追加しました (%s / id=%s)", name, account_id)
        return account_id

    async def remove_account(self, account_id: int) -> bool:
        """アカウントを削除する (取引の記録は残る)。"""
        slot = self._slots.pop(int(account_id), None)
        removed = await self._db.delete_kyash_account(int(account_id))
        if slot is not None:
            logger.info("受取用アカウントを削除しました (%s)", slot.label)
        return removed

    async def set_account_options(
        self,
        account_id: int,
        *,
        threshold: int | None = None,
        priority: int | None = None,
        enabled: bool | None = None,
    ) -> bool:
        slot = self.get_slot(account_id)
        if slot is None:
            raise KyashServiceError("アカウントが見つかりません")
        updated = await self._db.set_kyash_account_options(
            slot.id, threshold=threshold, priority=priority, enabled=enabled
        )
        if threshold is not None:
            slot.threshold = max(0, int(threshold))
        if priority is not None:
            slot.priority = int(priority)
        if enabled is not None:
            slot.enabled = bool(enabled)
        return updated

    async def begin_login(
        self, actor_id: int, email: str, password: str, *, account_id: int | None = None
    ) -> bool:
        """メールアドレス + パスワードでログインを開始する。

        Returns:
            True なら OTP 入力が必要、False なら登録済み UUID によりログイン完了。

        保存済みの client_uuid / installation_uuid があればそれを利用し、
        Kyash から OTP を要求されずにログインできる場合がある。
        """
        slot = self.get_slot(account_id)
        if slot is None:
            # まだ枠が無い場合は既定の枠を作る (初回ログインを簡単にする)
            new_id = await self.add_account("main")
            slot = self._slots[new_id]
        row = await self._db.get_kyash_account_row(slot.id)
        reuse_uuid = bool(
            row is not None
            and row["client_uuid"]
            and row["installation_uuid"]
            and row["email"]
            and str(row["email"]).strip().lower() == email.strip().lower()
        )
        client_uuid = row["client_uuid"] if reuse_uuid and row is not None else None
        installation_uuid = (
            row["installation_uuid"] if reuse_uuid and row is not None else None
        )

        def _build() -> Kyash:
            if client_uuid and installation_uuid:
                return Kyash(email, password, client_uuid, installation_uuid)
            return Kyash(email, password)

        client = await self._call(
            _build, context="ログイン開始", budget=config.KYASH_CALL_BUDGET, exclusive=True
        )
        # 認証情報をメモリに残さない
        try:
            client.password = None
        except Exception:  # noqa: BLE001  pragma: no cover
            pass

        if getattr(client, "access_token", None):
            # UUID 再利用によりトークンを取得済み (OTP 不要)
            self._fix_auth_header(client)
            await self._finalize_login(slot, client, email=email)
            return False

        self._pending_logins[actor_id] = PendingLogin(
            client=client, email=email, account_id=slot.id
        )
        logger.info("OTP 認証待ちのログインセッションを作成しました (actor=%s / %s)",
                    actor_id, slot.label)
        return True

    def has_pending_login(self, actor_id: int) -> bool:
        pending = self._pending_logins.get(actor_id)
        if pending is None:
            return False
        if pending.expired:
            self._pending_logins.pop(actor_id, None)
            return False
        return True

    def cleanup_pending_logins(self) -> int:
        """期限切れのログイン待ちセッションを破棄する。"""
        expired = [key for key, value in self._pending_logins.items() if value.expired]
        for key in expired:
            self._pending_logins.pop(key, None)
        return len(expired)

    async def complete_login(self, actor_id: int, otp: str) -> None:
        """OTP を検証してログインを完了する。

        OTP はログにも DB にも残さず、処理後に参照を破棄する。
        """
        pending = self._pending_logins.get(actor_id)
        if pending is None or pending.expired:
            self._pending_logins.pop(actor_id, None)
            raise KyashAuthError("ログインセッションの有効期限が切れました。もう一度やり直してください。")
        client = pending.client
        email = pending.email
        slot = self.get_slot(pending.account_id)
        if slot is None:
            raise KyashServiceError("ログイン先のアカウントが見つかりません")

        def _verify() -> None:
            client.login(otp)

        try:
            await self._call(_verify, context="OTP検証", exclusive=True)
        finally:
            # 成否に関わらず OTP を保持しない
            otp = ""  # noqa: F841
        self._pending_logins.pop(actor_id, None)
        self._fix_auth_header(client)
        await self._finalize_login(slot, client, email=email)

    async def _finalize_login(
        self, slot: AccountSlot, client: Kyash, *, email: str
    ) -> None:
        """ログイン完了後の検証とトークン保存。"""
        slot.client = client

        def _profile() -> ProfileInfo:
            return _sync_get_profile(client)

        try:
            profile = await self._call(_profile, context="プロフィール確認")
        except KyashServiceError as exc:
            slot.client = None
            await self._set_slot_status(
                slot, config.KyashAccountStatus.ERROR, error=str(exc), persist=False
            )
            raise

        token = getattr(client, "access_token", None)
        if not token:
            slot.client = None
            raise KyashAuthError("アクセストークンを取得できませんでした")
        token_enc = self._cipher.encrypt(token) if self._cipher.available else None
        if token_enc is None:
            logger.warning("暗号化が利用できないためトークンを保存しません (再起動時に再ログインが必要)")

        wallet_uuid: str | None = None
        wallet_balance: int | None = None
        try:
            wallet = await self.get_wallet(slot.id)
            wallet_uuid = wallet.uuid
            wallet_balance = wallet.all_balance
        except KyashServiceError as exc:
            logger.warning("ログイン直後の残高照会に失敗しました: %s", exc)

        issued_at = utils.now_ts()
        await self._db.save_kyash_session(
            slot.id,
            email=email,
            client_uuid=getattr(client, "client_uuid", None),
            installation_uuid=getattr(client, "installation_uuid", None),
            access_token_enc=token_enc,
            status=config.KyashAccountStatus.ACTIVE,
            username=profile.username,
            wallet_uuid=wallet_uuid,
            token_issued_at=issued_at,
        )
        slot.token_issued_at = issued_at
        slot.username = profile.username
        slot.wallet_uuid = wallet_uuid
        slot.wallet_balance = wallet_balance
        slot.email = email
        slot.status = config.KyashAccountStatus.ACTIVE
        slot.last_error = None
        slot.last_checked_at = utils.now_ts()
        # メールアドレスもクライアント側には不要になるため破棄する
        try:
            client.email = None
        except Exception:  # noqa: BLE001  pragma: no cover
            pass
        logger.info("Kyash アカウントのログインが完了しました (%s / user=%s)",
                    slot.label, utils.mask_identifier(profile.username, keep=3))

    async def logout(self, account_id: int | None = None) -> None:
        """セッションを破棄し、保存済み認証情報を削除する。

        ``account_id`` を省略した場合は**すべての**アカウントをログアウトする。
        """
        targets = (
            [slot for slot in self.slots()] if account_id is None
            else [s for s in (self.get_slot(account_id),) if s is not None]
        )
        self._pending_logins.clear()
        for slot in targets:
            slot.client = None
            slot.username = None
            slot.wallet_uuid = None
            slot.wallet_balance = None
            slot.token_issued_at = None
            slot.status = config.KyashAccountStatus.UNCONFIGURED
            slot.last_error = None
            await self._db.clear_kyash_session(slot.id)
            logger.info("Kyash アカウントをログアウトしました (%s)", slot.label)

    async def deactivate(self, reason: str, *, account_id: int | None = None) -> None:
        """セッションを利用不可としてマークする (認証情報は保持)。"""
        slot = self.get_slot(account_id)
        if slot is None:
            return
        await self._set_slot_status(
            slot, config.KyashAccountStatus.AUTH_REQUIRED, error=reason
        )

    # ------------------------------------------------------------------
    # 参照系
    # ------------------------------------------------------------------
    async def get_wallet(self, account_id: int | None = None) -> WalletInfo:
        """残高を照会する。取得した残高はそのアカウントの状態へ反映する。"""
        if account_id is not None:
            slot = self._require_slot(account_id)
        else:
            candidate = self.primary
            if candidate is None or candidate.client is None:
                raise KyashUnavailable("受取用Kyashアカウントが登録されていません")
            slot = candidate
        client = slot.client
        assert client is not None
        wallet: WalletInfo = await self._call(
            lambda: _sync_get_wallet(client), context="残高照会"
        )
        slot.wallet_balance = wallet.all_balance
        if wallet.uuid:
            slot.wallet_uuid = wallet.uuid
        return wallet

    async def get_history(
        self, limit: int = config.KYASH_HISTORY_LIMIT, *, account_id: int | None = None
    ) -> list[Any]:
        client = self._require_client(account_id)
        return await self._call(lambda: _sync_get_history(client, limit), context="履歴取得")

    async def get_profile(self, account_id: int | None = None) -> ProfileInfo:
        client = self._require_client(account_id)
        return await self._call(lambda: _sync_get_profile(client), context="プロフィール取得")

    async def health_check(self, account_id: int | None = None) -> str:
        """セッションの健康確認。状態を更新して返す。

        ``account_id`` を省略した場合は**すべての**アカウントを確認する。
        """
        if account_id is None:
            for each in self.slots():
                if each.client is not None:
                    await self._health_check_slot(each)
            return self.status
        target = self.get_slot(account_id)
        if target is None:
            return config.KyashAccountStatus.UNCONFIGURED
        return await self._health_check_slot(target)

    async def _health_check_slot(self, slot: AccountSlot) -> str:
        if slot.client is None:
            slot.status = config.KyashAccountStatus.UNCONFIGURED
            return slot.status
        try:
            profile = await self.get_profile(slot.id)
            wallet = await self.get_wallet(slot.id)
        except KyashAuthError as exc:
            logger.error("Kyash セッションが無効です (%s): %s", slot.label, exc)
            await self._set_slot_status(
                slot, config.KyashAccountStatus.AUTH_REQUIRED, error=str(exc)
            )
            return slot.status
        except KyashServiceError as exc:
            logger.warning("Kyash 健康確認に失敗しました (%s): %s", slot.label, exc)
            await self._set_slot_status(
                slot, config.KyashAccountStatus.ERROR, error=str(exc)
            )
            return slot.status
        await self._set_slot_status(
            slot,
            config.KyashAccountStatus.ACTIVE,
            error=None,
            wallet_uuid=wallet.uuid,
            username=profile.username,
            wallet_balance=wallet.all_balance,
        )
        return slot.status

    # ------------------------------------------------------------------
    # リンク検証 / 受取
    # ------------------------------------------------------------------
    async def link_check(
        self, canonical_url: str, *, account_id: int | None = None
    ) -> LinkInfo:
        """送金リンクを検証する。

        Raises:
            LinkInvalidError: 受取できないリンク (処理済み・不正など)。
            LinkIsClaimError: 請求リンク。
        """
        client = self._require_client(account_id)
        info: LinkInfo = await self._call(
            lambda: _sync_link_check(client, canonical_url), context="リンク検証"
        )
        if not info.send_to_me:
            raise LinkIsClaimError("請求リンクは受け取れません")
        return info

    async def create_claim_link(
        self, amount: int, *, message: str | None = None, account_id: int | None = None
    ) -> ClaimLink:
        """Bot 名義の請求リンクを発行する。

        利用者に「この金額を支払ってください」と提示するためのリンク。
        金額は Bot が指定するので、送金リンク方式のような金額不一致は起きない。

        履歴との突合に使う ``link_uuid`` は、発行した URL をページ解析して
        取得する (``create_link`` の応答構造には依存しない)。

        Raises:
            KyashServiceError: 発行または識別子の取得に失敗した場合。
        """
        # 余裕のあるアカウントを選ぶ (指定があればそれを使う)
        slot = self._require_slot(account_id) if account_id is not None else self.pick_slot(amount)
        client = slot.client
        assert client is not None
        text = message if message is not None else config.CLAIM_LINK_MESSAGE
        url, _raw = await self._call(
            lambda: _sync_create_claim_link(client, amount, text),
            context="請求リンク発行", exclusive=True,
        )
        try:
            link_id = utils.normalize_kyash_link(url)[1]
        except utils.LinkParseError as exc:
            raise KyashServiceError(f"発行された請求リンクを解釈できません: {exc}") from exc
        info: LinkInfo = await self._call(
            lambda: _sync_link_check(client, url), context="請求リンク確認"
        )
        if info.send_to_me:
            # 送金リンクが返ってきた = is_claim が効いていない。受け取り側の
            # 取り違えを防ぐため、ここで明確に失敗させる。
            raise KyashServiceError("請求リンクではなく送金リンクが発行されました")
        if info.amount != int(amount):
            raise KyashServiceError(
                f"発行された請求リンクの金額が一致しません (要求 {amount} / 実際 {info.amount})"
            )
        logger.info("請求リンクを発行しました amount=%s link=%s account=%s",
                    amount, utils.mask_identifier(link_id), slot.label)
        return ClaimLink(
            url=url, link_id=link_id, link_uuid=info.uuid,
            amount=int(amount), account_id=slot.id,
        )

    async def cancel_link(self, link_uuid: str, *, account_id: int | None = None) -> bool:
        """発行済みリンクを無効化する (失敗しても致命的にしない)。

        発行したアカウントでないと無効化できないため、``account_id`` には
        そのリンクを発行したアカウントを渡す。
        """
        slot = self.get_slot(account_id)
        client = slot.client if slot is not None else None
        if client is None:
            return False
        try:
            await self._call(
                lambda: _sync_cancel_link(client, link_uuid),
                context="リンク無効化", exclusive=True,
            )
        except KyashServiceError as exc:
            logger.info("リンクの無効化に失敗しました (無視します): %s", exc)
            return False
        return True

    async def find_payment(
        self, link_uuid: str, *, limit: int | None = None, account_id: int | None = None
    ) -> bool:
        """履歴にその請求リンクの支払いがあるかを確認する。"""
        timelines = await self.get_history(
            limit or config.KYASH_HISTORY_LIMIT, account_id=account_id
        )
        return utils.json_contains_text(timelines, link_uuid)

    async def link_receive(
        self, link_uuid: str, *, amount: int = 0, account_id: int | None = None
    ) -> tuple[dict[str, Any], AccountSlot]:
        """送金リンクを受け取る。

        受取先のアカウントは、しきい値に余裕があるものから自動で選ぶ。
        呼び出し側は戻り値の ``AccountSlot`` を取引へ記録し、以後の確認
        (``verify_receipt``) を**同じアカウント**に対して行う必要がある。

        戻り値だけで成功と判断してはならない
        (``verify_receipt`` による実状態の確認と組み合わせる)。

        Raises:
            NoCapacityError: 受け取れるアカウントが無い場合。
        """
        slot = (
            self._require_slot(account_id) if account_id is not None
            else self.pick_slot(amount)
        )
        client = slot.client
        assert client is not None
        result = await self._call(
            lambda: _sync_link_receive(client, link_uuid),
            context="リンク受取", exclusive=True,
        )
        return result, slot

    async def verify_receipt(
        self,
        *,
        link_uuid: str,
        amount: int,
        wallet_before: int | None,
        allow_wallet_delta: bool = True,
        account_id: int | None = None,
    ) -> VerificationResult:
        """実際に受け取れたかを履歴・残高から確認する。

        添付モジュールで利用できる情報 (履歴 / Wallet) のみを使い、
        スキーマに依存しない形で突合する。

        Args:
            allow_wallet_delta: 履歴に痕跡が無いとき、受取用アカウントの
                残高増加を根拠にしてよいか。

                送金リンク方式では Bot 自身が受取を実行した直後に確認するため、
                残高の増加はその受取によるものと見なせる。

                一方、**請求リンク方式では False を渡すこと**。支払いは
                利用者が任意のタイミングで行い、複数の請求リンクが同時に
                未払いで残り得るため、他人の支払いによる残高増加を
                自分の支払いと誤認して残高を発行してしまう。
                (この場合は履歴に ``link_uuid`` が現れるまで待つ)
        """
        history_error: str | None = None
        wallet_error: str | None = None

        try:
            timelines = await self.get_history(
                config.KYASH_HISTORY_LIMIT, account_id=account_id
            )
            if utils.json_contains_text(timelines, link_uuid):
                return VerificationResult(
                    verdict=Verdict.CONFIRMED, detail="履歴にリンク識別子を確認"
                )
        except KyashServiceError as exc:
            history_error = str(exc)

        wallet_after: int | None = None
        try:
            wallet = await self.get_wallet(account_id)
            wallet_after = wallet.all_balance
        except KyashServiceError as exc:
            wallet_error = str(exc)

        if not allow_wallet_delta:
            # 残高差分を根拠にしない方式。履歴に現れていない = まだ未払い。
            if history_error is None:
                return VerificationResult(
                    verdict=Verdict.NO_EVIDENCE,
                    detail="履歴に支払いの痕跡がありません",
                    wallet_after=wallet_after,
                )
            return VerificationResult(
                verdict=Verdict.UNAVAILABLE,
                detail=f"履歴を取得できません: {history_error}",
                wallet_after=wallet_after,
            )

        if wallet_before is not None and wallet_after is not None:
            delta = wallet_after - wallet_before
            if delta >= amount:
                return VerificationResult(
                    verdict=Verdict.CONFIRMED,
                    detail=f"残高が{delta}増加 (期待{amount}以上)",
                    wallet_after=wallet_after,
                )
            if history_error is None:
                return VerificationResult(
                    verdict=Verdict.NO_EVIDENCE,
                    detail=f"履歴に痕跡なし / 残高増加{delta} (期待{amount})",
                    wallet_after=wallet_after,
                )

        if history_error is None and wallet_before is None and wallet_after is not None:
            # 受取前の残高が不明 → 履歴だけで判断できない
            return VerificationResult(
                verdict=Verdict.UNAVAILABLE,
                detail="受取前残高が不明のため残高差分で判定できません",
                wallet_after=wallet_after,
            )

        detail_parts = [p for p in (history_error, wallet_error) if p]
        return VerificationResult(
            verdict=Verdict.UNAVAILABLE,
            detail="確認情報を取得できません: " + (" / ".join(detail_parts) or "不明"),
            wallet_after=wallet_after,
        )

    # ------------------------------------------------------------------
    # 終了処理
    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        """セッション情報をメモリから破棄し、スレッドプールを停止する。"""
        self._pending_logins.clear()
        for slot in self._slots.values():
            slot.client = None
        self._receive_executor.shutdown(wait=False, cancel_futures=True)
        self._read_executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Kyash サービスを停止しました")
