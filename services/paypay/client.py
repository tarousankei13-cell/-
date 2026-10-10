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

# ⚠️ **もう動かない（受け取りようがない）状態。**
#    ここに**無い**ものは「まだ終わっていない」として扱う。
#
#    理由：PayPay は2024-02-28 から、受け取りリンクを**保留**にする
#    ことがある（送った側に警告を出し、「送る」か「キャンセル」を
#    選ばせる）。保留中のリンクはまだ生きているが、こちらからは
#    受け取れない。
#    https://paypay.ne.jp/notice/20240228/f-p2p-money-link/
#
#    以前は「PENDING でなければ受け取り・辞退・取り消し済み」と
#    決めつけていたため、**保留中の人に「このリンクはもう使えません」
#    と伝えていた**。お金はまだ送り主の手元にあるのに。
#
# ⚠️ 迷ったら「終わっていない」側に倒す。
#    ・終わったものを保留と誤れば → 無駄に数回見に行って期限切れ。害は小さい
#    ・保留を終わったと誤れば     → 届くはずのお金を「使えません」と断る。害が大きい
#
# ⚠️ この一覧は**実際に見た値で育てる**。保留の状態値が何かは
#    分かっていないので、推測で足さないこと（PayPayReceipt.link_status に
#    実際の値が残るので、それを見て判断する）。
TERMINAL_STATUSES = frozenset({
    # このBOTが以前から終了として扱っていた値（tests/test_paypay.py 由来）
    "SUCCESS", "FAILED", "REJECTED",
    # 同じ意味で来うる言い回し
    "COMPLETED", "COMPLETE", "RECEIVED", "ACCEPTED",
    "DECLINED", "CANCELED", "CANCELLED", "EXPIRED",
})


class PayPayError(Exception):
    """利用者にそのまま見せてよい、PayPay 側の失敗。"""


class PayPayLoginError(PayPayError):
    """ログインが切れている／できていない。"""


class LinkAlreadyUsed(PayPayError):
    """すでに受け取り・辞退・取り消し済みのリンク。"""


class LinkOnHold(PayPayError):
    """まだ受け取れないが、**終わってもいない**リンク。

    PayPay が送金を保留している場合など。送った側が PayPay アプリで
    「送る」を押すと受け取れるようになる。

    ⚠️ LinkAlreadyUsed と**必ず区別する**。混ぜると、まだ生きている
       お金を「使えません」と断ってしまう。
    """

    def __init__(self, message: str, status: str = "") -> None:
        super().__init__(message)
        self.status = status


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

    @property
    def terminal(self) -> bool:
        """もう受け取りようがないか。

        ⚠️ **分からない状態は終わっていない扱い。** 保留かもしれない。
        """
        return (self.status or "").upper() in TERMINAL_STATUSES

    @property
    def on_hold(self) -> bool:
        """まだ受け取れないが、終わってもいない（保留など）。"""
        return not self.receivable and not self.terminal


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
        self._client = build_async_client(
            timeout=timeout, proxy=proxy, service="paypay",
        )

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
        # portal 側はヘッダがまったく別なので、差し替えられるようにする
        headers = kw.pop("headers_override", None) or self._headers()
        try:
            r = await self._client.request(method, url, headers=headers, **kw)
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

    def _portal_headers(self, *, json_api: bool = True) -> dict[str, str]:
        """
        portal（www.paypay.ne.jp）側のヘッダ。

        ⚠️ app4 側とは別物。あちらはアプリとして、こちらは
           **アプリ内ブラウザ** として振る舞う必要がある。
        """
        ua = (
            "Mozilla/5.0 (Linux; Android 10; SCV38 Build/QP1A.190711.020; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
            f"Chrome/132.0.6834.163 Mobile Safari/537.36 jp.pay2.app.android/{APP_VERSION}"
        )
        h = {
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Host": "www.paypay.ne.jp",
            "User-Agent": ua,
            "X-Requested-With": "jp.ne.paypay.android.app",
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
        }
        if json_api:
            h |= {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Client-Id": CLIENT_ID,
                "Client-OS-Type": "ANDROID",
                "Client-OS-Version": "29.0.0",
                "Client-Type": "PAYPAYAPP",
                "Client-Version": APP_VERSION,
                "Origin": PORTAL,
                "Referer": (
                    f"{PORTAL}/portal/oauth2/sign-in"
                    f"?client_id={CLIENT_ID}&mode=landing"
                ),
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        else:
            h |= {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "is-emulator": "false",
            }
        return h

    async def start_login(self, phone: str, password: str) -> dict:
        """
        ログインを始める。

        ⚠️ **手順を飛ばせない。** 相手は www.paypay.ne.jp 側で
           Cookie を積み上げながら進める作りになっている。

             ① app4  /bff/v2/oauth2/par            … 要求を登録し requestUri をもらう
             ② portal /portal/api/v2/oauth2/authorize
             ③ portal /portal/oauth2/sign-in
             ④ portal /portal/api/v2/oauth2/par/check
             ⑤ portal /portal/api/v2/oauth2/sign-in/password … ここで資格情報

           いきなり ⑤ や ① に資格情報を送っても通らない。

        返り値:
          {"done": True, "session": ...}  端末が登録済みで、SMSなしで入れた
          {"done": False}                 SMSでURLが届くので login_confirm へ
        """
        phone = (phone or "").replace("-", "").strip()
        if not phone or not password:
            raise PayPayLoginError("電話番号とパスワードを入力してください")

        self._verifier, challenge = _pkce()
        state = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()

        # ① 要求の登録（アプリとして）
        par = await self._request(
            "POST", f"{APP}/bff/v2/oauth2/par",
            data={
                "clientId": CLIENT_ID,
                "clientAppVersion": APP_VERSION,
                "clientOsVersion": "29.0.0",
                "clientOsType": "ANDROID",
                "redirectUri": REDIRECT_URI,
                "responseType": "code",
                "state": state,
                "codeChallenge": challenge,
                "codeChallengeMethod": "S256",
                "scope": "REGULAR",
                "tokenVersion": "v2",
                "prompt": "",
                "uiLocales": "ja",
            },
            params={"payPayLang": "ja"},
        )
        request_uri = str((par.get("payload") or {}).get("requestUri") or "")
        if not request_uri:
            raise PayPayLoginError("ログインの準備に失敗しました")

        # ②〜④ ブラウザとして画面をたどる（Cookie を積む）
        browser = self._portal_headers(json_api=False)
        try:
            await self._client.get(
                f"{PORTAL}/portal/api/v2/oauth2/authorize",
                headers=browser,
                params={"client_id": CLIENT_ID, "request_uri": request_uri},
                follow_redirects=False,
            )
            r = await self._client.get(
                f"{PORTAL}/portal/oauth2/sign-in", headers=browser,
                params={"client_id": CLIENT_ID, "mode": "landing"},
            )
            if r.status_code >= 400:
                raise PayPayLoginError("サインイン画面を開けませんでした")
            await self._request(
                "GET", f"{PORTAL}/portal/api/v2/oauth2/par/check",
                headers_override=self._portal_headers(),
            )
        except httpx.HTTPError as e:
            raise PayPayError(f"PayPayに接続できませんでした: {e}") from e

        # ⑤ 資格情報
        signin = await self._request(
            "POST", f"{PORTAL}/portal/api/v2/oauth2/sign-in/password",
            json={"username": phone, "password": password},
            headers_override=self._portal_headers(),
        )

        # 端末が登録済みなら、ここで認可コードがそのまま返る（SMSなし）
        redirect = str((signin.get("payload") or {}).get("redirectUrl") or "")
        m = re.search(r"code=([^&\s]+)", redirect)
        if m:
            session = await self._exchange(m.group(1))
            return {"done": True, "session": session}
        return {"done": False}

    async def _exchange(self, code: str) -> PayPaySession:
        """認可コードをトークンに換える。"""
        data = await self._request(
            "POST", f"{APP}/bff/v2/oauth2/token",
            data={
                "clientId": CLIENT_ID,
                "redirectUri": REDIRECT_URI,
                "code": code,
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
            headers_override=self._portal_headers(),
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
        return await self._exchange(m.group(1))

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
            # ⚠️ 終わったものと保留を分ける。保留を「使えません」と
            #    断ると、届くはずのお金を取りこぼす。
            if info.terminal:
                raise LinkAlreadyUsed(
                    "このリンクはすでに受け取り・辞退・取り消しのいずれかが済んでいます。"
                )
            raise LinkOnHold(
                "このリンクはまだ受け取れる状態になっていません。", info.status
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
