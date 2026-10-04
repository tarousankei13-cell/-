"""
PayPay クライアント

⚠️ **モバイルアプリ側のAPI（app4.paypay.ne.jp）を使う。**
   Web側（www.paypay.ne.jp/app/...）にも同じような口があるが、

     Web側   … トークンが2時間で切れ、更新手段が無い。
                送金リンクの作成は2024年3月に廃止され 404 になる。
     アプリ側 … トークンは **90日**。更新トークンもある。
                送金リンクの作成も生きている。

   なので必ずこちらを使うこと。
   （解析元: https://github.com/taka-4602/PayPaython-mobile）

⚠️ ログインは2段階。
     ① login()          … 電話番号とパスワードを送ると、SMSで
                           **URL** が届く（数字のOTPではない）
     ② login_confirm()  … そのURLを渡すとトークンが手に入る
   いちど通れば device_uuid を保存しておくことで、次からは
   SMSなしで入り直せる。

⚠️ **日本からしかアクセスできない。** 海外からはプロキシが要る。
⚠️ ログイン3回失敗でアカウントが一時ロックされる。
   セッションを作りすぎると凍結の恐れがある。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import re
import uuid
from dataclasses import dataclass

import httpx

from core.http import build_async_client

log = logging.getLogger("bot.paypay")

APP = "https://app4.paypay.ne.jp"
PORTAL = "https://www.paypay.ne.jp"
LINK_PREFIX = "https://pay.paypay.ne.jp/"
CLIENT_ID = "pay2-mobile-app-client"
REDIRECT_URI = "paypay://oauth2/callback"
APP_VERSION = "5.57.0"

# 受け取れる状態
PENDING = "PENDING"


class PayPayError(Exception):
    """利用者にそのまま見せてよい、PayPay 側の失敗。"""


class PayPayLoginError(PayPayError):
    """ログインが切れている／できていない。"""


class LinkAlreadyUsed(PayPayError):
    """すでに受け取り・辞退・取り消し済みのリンク。"""


@dataclass
class PayPaySession:
    access_token: str = ""
    refresh_token: str = ""
    device_uuid: str = ""
    client_uuid: str = ""


@dataclass
class LinkInfo:
    amount: int
    order_id: str
    message_id: str
    chat_room_id: str
    status: str
    has_password: bool
    sender_name: str = ""
    raw: dict | None = None

    @property
    def receivable(self) -> bool:
        return self.status == PENDING


@dataclass
class Balance:
    all_balance: int = 0
    usable_balance: int = 0
    money: int = 0
    money_light: int = 0
    points: int = 0


def _pkce() -> tuple[str, str]:
    """PKCE の検証子と挑戦値。アプリと同じ S256 方式。"""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _vec(a: tuple[float, float], b: tuple[float, float],
         c: tuple[float, float]) -> str:
    """
    端末の傾きや加速度のふり。

    ⚠️ アプリは毎回この値を送っている。固定値を送り続けると
       「人が持っていない端末」として目立つので、毎回ばらす。
    """
    return ",".join(
        f"{random.uniform(*r):.6f}" for r in (a, b, c)
    )


def normalize_link(url: str) -> str:
    """送金リンクから確認コードだけを取り出す。"""
    raw = (url or "").strip()
    for prefix in (LINK_PREFIX, "http://pay.paypay.ne.jp/"):
        if raw.lower().startswith(prefix.lower()):
            raw = raw[len(prefix):]
            break
    return raw.strip().strip("/").split("?")[0]


class PayPayClient:
    def __init__(
        self, session: PayPaySession | None = None, *,
        proxy: str | None = None, timeout: float = 20.0,
    ) -> None:
        s = session or PayPaySession()
        self.session = PayPaySession(
            access_token=s.access_token,
            refresh_token=s.refresh_token,
            device_uuid=s.device_uuid or str(uuid.uuid4()),
            client_uuid=s.client_uuid or str(uuid.uuid4()),
        )
        self._verifier = ""
        self._client = build_async_client(timeout=timeout, proxy=proxy)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PayPayClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- 共通 -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "*/*",
            "Accept-Charset": "UTF-8",
            "Client-Mode": "NORMAL",
            "Client-OS-Release-Version": "10",
            "Client-OS-Type": "ANDROID",
            "Client-OS-Version": "29.0.0",
            "Client-Type": "PAYPAYAPP",
            "Client-UUID": self.session.client_uuid,
            "Client-Version": APP_VERSION,
            "Device-Acceleration": _vec((-0.2, 0.2), (-0.2, 0.2), (9.6, 9.9)),
            "Device-Acceleration-2": _vec((-0.2, 0.2), (-0.2, 0.2), (9.6, 9.9)),
            "Device-Brand-Name": "KDDI",
            "Device-Hardware-Name": "qcom",
            "Device-In-Call": "false",
            "Device-Lock-App-Setting": "false",
            "Device-Lock-Type": "NONE",
            "Device-Manufacturer-Name": "samsung",
            "Device-Name": "SCV38",
            "Device-Orientation": _vec((2.2, 2.6), (-0.2, -0.05), (-0.05, 0.1)),
            "Device-Orientation-2": _vec((2.0, 2.6), (-0.2, -0.05), (-0.05, 0.2)),
            "Device-Rotation": _vec((-0.8, -0.6), (-0.1, 0.1), (-0.1, 0.1)),
            "Device-Rotation-2": _vec((-0.8, -0.6), (-0.1, 0.1), (-0.1, 0.1)),
            "Device-UUID": self.session.device_uuid,
            "Is-Emulator": "false",
            "Network-Status": "WIFI",
            "System-Locale": "ja",
            "Timezone": "Asia/Tokyo",
            "User-Agent": f"PaypayApp/{APP_VERSION} Android10",
        }
        if self.session.access_token:
            h["Authorization"] = f"Bearer {self.session.access_token}"
            h["Content-Type"] = "application/json"
        return h

    def _require_login(self) -> None:
        if not self.session.access_token:
            raise PayPayLoginError("PayPayにログインしていません")

    async def _request(self, method: str, url: str, **kw) -> dict:
        try:
            r = await self._client.request(
                method, url, headers=self._headers(), **kw
            )
        except httpx.HTTPError as e:
            raise PayPayError(f"PayPayに接続できませんでした: {e}") from e
        try:
            data = r.json()
        except ValueError:
            raise PayPayError(
                f"PayPayの応答を読めませんでした（HTTP {r.status_code}）"
            ) from None
        self._check(data)
        return data

    @staticmethod
    def _check(data: dict) -> None:
        """
        応答の結果コードを見る。

        ⚠️ HTTPが200でも中身が失敗のことがある。必ずここを通すこと。
        """
        header = data.get("header") or {}
        code = header.get("resultCode")
        if not code or code in ("S0000", "S4002"):
            return
        if code == "S0001":
            raise PayPayLoginError("PayPayのログインが切れています。登録し直してください。")
        message = ""
        try:
            message = data["error"]["displayErrorResponse"]["description"]
        except (KeyError, TypeError):
            message = header.get("resultMessage") or ""
        if "しばらく時間をおいて" in message:
            raise PayPayError("PayPay側が混み合っています。少し時間をおいてください。")
        raise PayPayError(message or f"PayPayに断られました（{code}）")

    # -- ログイン ---------------------------------------------

    async def start_login(self, phone: str, password: str) -> str:
        """
        ログインを始める。SMSで **URL** が届くので、それを
        login_confirm に渡すこと（数字のOTPではない）。
        """
        phone = (phone or "").replace("-", "").strip()
        if not phone or not password:
            raise PayPayLoginError("電話番号とパスワードを入力してください")

        self._verifier, challenge = _pkce()
        payload = {
            "clientId": CLIENT_ID,
            "clientAppVersion": APP_VERSION,
            "clientOsVersion": "29.0.0",
            "clientOsType": "ANDROID",
            "redirectUri": REDIRECT_URI,
            "responseType": "code",
            "state": base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode(),
            "codeChallenge": challenge,
            "codeChallengeMethod": "S256",
            "scope": "REGULAR",
            "tokenVersion": "v2",
            "prompt": "",
            "uiLocales": "ja",
            "username": phone,
            "password": password,
        }
        data = await self._request(
            "POST", f"{APP}/bff/v2/oauth2/par",
            data=payload, params={"payPayLang": "ja"},
        )
        return str((data.get("payload") or {}).get("requestUri") or "")

    async def login_confirm(self, url_or_code: str) -> PayPaySession:
        """SMSで届いたURLを渡してトークンを受け取る。"""
        code = (url_or_code or "").strip()
        m = re.search(r"[?&]id=([^&\s]+)", code)
        if m:
            code = m.group(1)
        code = code.rsplit("/", 1)[-1] if "://" in code else code
        if not code:
            raise PayPayLoginError("SMSに届いたURLを貼り付けてください")

        verify = await self._request(
            "POST",
            f"{PORTAL}/portal/api/v2/oauth2/extension/sign-in/2fa/otl/verify",
            json={"code": code},
        )
        redirect = ""
        payload = verify.get("payload") or {}
        for key in ("redirectUri", "redirect_uri", "uri", "url"):
            if payload.get(key):
                redirect = str(payload[key])
                break
        m = re.search(r"code=([^&\s]+)", redirect)
        if not m:
            raise PayPayLoginError(
                "ログインの確認に失敗しました。URLをもう一度ご確認ください。"
            )

        data = await self._request(
            "POST", f"{APP}/bff/v2/oauth2/token",
            data={
                "clientId": CLIENT_ID,
                "redirectUri": REDIRECT_URI,
                "code": m.group(1),
                "codeVerifier": self._verifier,
            },
            params={"payPayLang": "ja"},
        )
        got = data.get("payload") or {}
        self.session.access_token = str(got.get("accessToken") or "")
        self.session.refresh_token = str(got.get("refreshToken") or "")
        if not self.session.access_token:
            raise PayPayLoginError("トークンを受け取れませんでした")
        return self.session

    # -- 残高・履歴 -------------------------------------------

    async def get_balance(self) -> Balance:
        self._require_login()
        data = await self._request(
            "GET", f"{APP}/bff/v1/getBalanceInfo",
            params={
                "includePending": "true", "noCache": "true",
                "includeKycInfo": "true", "payPayLang": "ja",
            },
        )
        p = data.get("payload") or {}
        detail = p.get("walletDetail") or {}
        summary = p.get("walletSummary") or {}

        def num(d: dict, *keys) -> int:
            for k in keys:
                d = (d or {}).get(k) or {}
            return int(d) if isinstance(d, int) else 0

        return Balance(
            all_balance=int(
                ((summary.get("allTotalBalanceInfo") or {}).get("balance")) or 0
            ),
            usable_balance=int(
                ((summary.get("usableBalanceInfoWithoutCashback") or {})
                 .get("balance")) or 0
            ),
            money=int(((detail.get("emoneyBalanceInfo") or {}).get("balance")) or 0),
            money_light=int(
                ((detail.get("prepaidBalanceInfo") or {}).get("balance")) or 0
            ),
            points=int(
                ((detail.get("cashBackBalanceInfo") or {}).get("balance")) or 0
            ),
        )

    async def get_history(self, size: int = 20) -> list[dict]:
        self._require_login()
        data = await self._request(
            "GET", f"{APP}/bff/v4/getPaymentHistory",
            params={"pageSize": str(size), "payPayLang": "ja"},
        )
        p = data.get("payload") or {}
        for key in ("paymentHistoryList", "historyList", "transactions"):
            if isinstance(p.get(key), list):
                return p[key]
        return []

    # -- 送金リンク -------------------------------------------

    async def link_check(self, url: str) -> LinkInfo:
        """
        リンクの中身を見る。

        ⚠️ 受け取りはせず、読むだけ。金額が分かってから口座を選ぶ。
        """
        self._require_login()
        code = normalize_link(url)
        if not code:
            raise PayPayError("送金リンクを正しく貼り付けてください")
        data = await self._request(
            "GET", f"{APP}/bff/v2/getP2PLinkInfo",
            params={"verificationCode": code, "payPayLang": "ja"},
        )
        p = data.get("payload") or {}
        pending = p.get("pendingP2PInfo") or {}
        message = p.get("message") or {}
        return LinkInfo(
            amount=int(pending.get("amount") or 0),
            order_id=str(pending.get("orderId") or ""),
            message_id=str(message.get("messageId") or ""),
            chat_room_id=str(message.get("chatRoomId") or ""),
            status=str(p.get("orderStatus") or ""),
            has_password=bool(pending.get("isSetPasscode")),
            sender_name=str((p.get("sender") or {}).get("displayName") or ""),
            raw=data,
        )

    async def link_receive(
        self, url: str, info: LinkInfo | None = None, passcode: str | None = None
    ) -> dict:
        """
        送金リンクを受け取る。**ここでお金が動く。**

        ⚠️ 受け取れる状態かを必ず先に見る。すでに受け取り済みの
           リンクを送ると、相手は断るが、こちらが二重に記帳する
           危険が残るため。
        """
        self._require_login()
        code = normalize_link(url)
        info = info or await self.link_check(code)
        if info.status and info.status != PENDING:
            raise LinkAlreadyUsed(
                "このリンクはすでに受け取り・辞退・取り消しのいずれかが済んでいます。"
            )
        if info.has_password and not passcode:
            raise PayPayError("このリンクにはパスコードが設定されています。")
        if passcode and (not passcode.isdigit() or len(passcode) != 4):
            raise PayPayError("パスコードは4桁の数字で入力してください。")

        payload = {
            "requestId": str(uuid.uuid4()),
            "orderId": info.order_id,
            "verificationCode": code,
            "passcode": passcode if info.has_password else None,
            "senderMessageId": info.message_id,
            "senderChannelUrl": info.chat_room_id,
        }
        return await self._request(
            "POST", f"{APP}/bff/v2/acceptP2PSendMoneyLink", json=payload,
            params={
                "payPayLang": "ja",
                "appContext": "P2PMoneyTransferDetailScreen_linkReceiver",
            },
        )

    async def create_link(self, amount: int, passcode: str | None = None) -> str:
        """
        送金リンクを作る（返金に使う）。**ここでお金が出ていく。**

        ⚠️ Web側のAPIでは2024年3月に廃止されたが、
           アプリ側のこの口は生きている。
        """
        self._require_login()
        payload = {
            "requestId": str(uuid.uuid4()),
            "amount": int(amount),
            "socketConnection": "P2P",
            "theme": "default-sendmoney",
            "source": "sendmoney_home_sns",
            "ackPhoneCallDetected": False,
        }
        if passcode:
            payload["passcode"] = passcode
        data = await self._request(
            "POST", f"{APP}/bff/v2/executeP2PSendMoneyLink", json=payload,
            params={"payPayLang": "ja"},
        )
        return str((data.get("payload") or {}).get("link") or "")

    # -- 生存確認 ---------------------------------------------

    async def healthcheck(self) -> bool:
        try:
            await self.get_balance()
            return True
        except PayPayError:
            return False
