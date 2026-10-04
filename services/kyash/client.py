"""
Kyash API クライアント（非同期）

Kyasher (https://github.com/taka-4602/Kyasher) を httpx へ移植したもの。
仕様の根拠は docs/03。

移植にあたって直した点:
  - link_check が未定義の self.link_uuid を参照しており、有効なリンクでも
    必ず「処理済み」として失敗していた（元実装のバグ）
  - 例外を握りつぶす bare except をやめ、原因が分かるメッセージにした
  - 同期 requests → 非同期 httpx（BOTが固まらないように）

用途は「利用者からの送金リンクを受け取る」こと。決済には使わない。
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from dataclasses import dataclass

import httpx

from core.http import build_async_client
from bs4 import BeautifulSoup

log = logging.getLogger("bot.kyash")

API = "https://api.kyash.me"
LINK_PREFIX = "https://kyash.me/payments/"
CLIENT_VERSION = "11.8.1"


class KyashError(Exception):
    pass


class KyashLoginError(KyashError):
    pass


class KyashNetworkError(KyashError):
    pass


class LinkAlreadyUsed(KyashError):
    """すでに受け取り済み・キャンセル済みのリンク。"""


@dataclass
class Profile:
    username: str = ""
    icon: str = ""
    last_name: str = ""
    first_name: str = ""
    phone: str = ""
    is_kyc: bool = False


@dataclass
class Wallet:
    uuid: str = ""
    all_balance: int = 0
    money: int = 0      # 出金可能なキャッシュマネー
    value: int = 0      # 出金不可のキャッシュバリュー
    point: int = 0


@dataclass
class LinkInfo:
    amount: int
    uuid: str
    send_to_me: bool           # True=送金リンク（受け取れる）/ False=請求リンク
    public_id: str = ""
    sender_name: str = ""


@dataclass
class KyashSession:
    access_token: str = ""
    refresh_token: str = ""
    client_uuid: str = ""
    installation_uuid: str = ""


class KyashClient:
    def __init__(
        self,
        session: KyashSession | None = None,
        *,
        proxy: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        s = session or KyashSession()
        self.session = KyashSession(
            access_token=s.access_token,
            refresh_token=s.refresh_token,
            client_uuid=s.client_uuid or str(uuid.uuid4()).upper(),
            installation_uuid=s.installation_uuid or str(uuid.uuid4()).upper(),
        )
        self._email = ""
        self._client = build_async_client(
            timeout=timeout, proxy=proxy, service="kyash",
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KyashClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- 共通 -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        h = {
            "Host": "api.kyash.me",
            "Content-Type": "application/json",
            "X-Kyash-Client-Id": self.session.client_uuid,
            "Accept": "application/json",
            "X-Kyash-Device-Language": "ja",
            "X-Kyash-Client-Version": CLIENT_VERSION,
            "X-Kyash-Device-Info": "iPhone 8, Version:16.7.5",
            "Accept-Language": "ja-jp",
            "X-Kyash-Date": str(round(datetime.datetime.now(jst).timestamp())),
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "Kyash/2 CFNetwork/1240.0.4 Darwin/20.6.0",
            "X-Kyash-Installation-Id": self.session.installation_uuid,
            "X-Kyash-Os": "iOS",
            "Connection": "keep-alive",
        }
        if self.session.access_token:
            h["X-Auth"] = self.session.access_token
        return h

    def _require_login(self) -> None:
        if not self.session.access_token:
            raise KyashLoginError("Kyashにログインしていません")

    async def _request(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self._client.request(method, f"{API}{path}", headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise KyashNetworkError(f"Kyashに接続できませんでした: {e}") from e
        try:
            data = r.json()
        except ValueError as e:
            raise KyashError(f"Kyashの応答を解釈できませんでした (HTTP {r.status_code})") from e
        if data.get("code") != 200:
            msg = ((data.get("error") or {}).get("message")) or f"HTTP {r.status_code}"
            raise KyashError(msg)
        return data

    @staticmethod
    def _normalize_link(url: str) -> str:
        url = url.strip()
        if LINK_PREFIX in url:
            return url
        # 「リンクのIDだけ」を貼られた場合にも対応する
        return LINK_PREFIX + url.rstrip("/").split("/")[-1]

    # -- ログイン ---------------------------------------------

    async def start_login(self, email: str, password: str) -> bool:
        """
        ログインを開始する。

        保存済みの client_uuid / installation_uuid があれば、この時点で
        トークンが返ってきて OTP は不要になる。
        返り値 True = OTP が必要 / False = ログイン完了。
        """
        self._email = email
        data = await self._request("POST", "/v2/login", json={"email": email, "password": password})
        token = ((data.get("result") or {}).get("data") or {}).get("token")
        if token:
            self.session.access_token = token
            self.session.refresh_token = (
                ((data.get("result") or {}).get("data") or {}).get("refreshToken") or ""
            )
            return False
        return True

    async def verify_otp(self, otp: str, email: str | None = None) -> KyashSession:
        """SMSで届いた6桁を入力してログインを完了する。"""
        payload = {"verificationCode": otp, "email": email or self._email}
        if not payload["email"]:
            raise KyashLoginError("メールアドレスが指定されていません")
        data = await self._request("POST", "/v2/login/mobile/verify", json=payload)
        d = (data.get("result") or {}).get("data") or {}
        if not d.get("token"):
            raise KyashLoginError("認証コードが正しくありません")
        self.session.access_token = d["token"]
        self.session.refresh_token = d.get("refreshToken") or ""
        # ⚠️ client_uuid と installation_uuid は2つで1セット。
        #    両方保存しておくと次回から OTP が不要になる。
        return self.session

    # -- 情報取得 ---------------------------------------------

    async def get_profile(self) -> Profile:
        self._require_login()
        data = await self._request("GET", "/v1/me")
        d = (data.get("result") or {}).get("data") or {}
        return Profile(
            username=d.get("userName") or "",
            icon=d.get("icon") or "",
            last_name=d.get("lastName") or "",
            first_name=d.get("firstName") or "",
            phone=d.get("phoneNumber") or "",
            is_kyc=bool(d.get("isKyc") or d.get("kycStatus") == "APPROVED"),
        )

    async def get_wallet(self) -> Wallet:
        self._require_login()
        data = await self._request("GET", "/v1/me/primary_wallet")
        d = (data.get("result") or {}).get("data") or {}
        bal = d.get("balance") or {}
        bd = bal.get("amountBreakdown") or {}
        return Wallet(
            uuid=d.get("uuid") or "",
            all_balance=int(bal.get("amount") or 0),
            money=int(bd.get("kyashMoney") or 0),
            value=int(bd.get("kyashValue") or 0),
            point=int((d.get("pointBalance") or {}).get("availableAmount") or 0),
        )

    async def get_history(self, wallet_uuid: str | None = None, limit: int = 10) -> list[dict]:
        self._require_login()
        if not wallet_uuid:
            wallet_uuid = (await self.get_wallet()).uuid
        data = await self._request(
            "GET", f"/v1/me/wallets/{wallet_uuid}/timeline", params={"limit": limit}
        )
        d = (data.get("result") or {}).get("data") or {}
        return d.get("timelines") or []

    # -- 送金リンク -------------------------------------------

    async def _scrape_link(self, url: str) -> tuple[int, str, bool]:
        """
        リンクのHTMLから (金額, リンクUUID, 受け取りリンクか) を取り出す。

        Kyash はリンクのID部分ではなく内部UUIDを使うため、HTMLから拾う必要がある。
        """
        try:
            r = await self._client.get(url)
        except httpx.HTTPError as e:
            raise KyashNetworkError(f"リンクを開けませんでした: {e}") from e
        if r.status_code >= 400:
            raise LinkAlreadyUsed("このリンクは無効か、すでに使用されています")

        soup = BeautifulSoup(r.text, "html.parser")

        send_amount = soup.find(class_="amountText text_send")
        send_btn = soup.find(class_="btn_send")
        if send_amount is not None and send_btn is not None:
            href = send_btn.get("data-href-app") or ""
            return (
                _parse_yen(send_amount.get_text()),
                href.replace("kyash://claim/", "").strip(),
                True,
            )

        req_amount = soup.find(class_="amountText text_request")
        req_btn = soup.find(class_="btn_request")
        if req_amount is not None and req_btn is not None:
            href = req_btn.get("data-href-app") or ""
            return (
                _parse_yen(req_amount.get_text()),
                href.replace("kyash://request/u/", "").strip(),
                False,
            )

        raise LinkAlreadyUsed(
            "リンクの内容を読み取れませんでした。"
            "すでに使用済みか、Kyash側の画面が変わった可能性があります"
        )

    async def link_check(self, url: str) -> LinkInfo:
        """
        リンクを確認する。受け取りは行わない。

        ⚠️ 元実装は未定義の self.link_uuid を参照していたため、有効なリンクでも
           必ず失敗していた。ここではスクレイプで得た uuid を使う。
        """
        self._require_login()
        url = self._normalize_link(url)
        amount, link_uuid, send_to_me = await self._scrape_link(url)
        if not link_uuid:
            raise LinkAlreadyUsed("リンクのIDを取得できませんでした")

        public_id = sender_name = ""
        try:
            data = await self._request("GET", f"/v1/links/{link_uuid}")
            target = ((data.get("result") or {}).get("data") or {}).get("target") or {}
            public_id = target.get("publicId") or ""
            sender_name = target.get("userName") or ""
        except KyashError as e:
            # 詳細が取れなくても、金額とUUIDが分かれば受け取りはできる
            log.info("リンクの詳細を取得できませんでした（続行します）: %s", e)

        return LinkInfo(
            amount=amount, uuid=link_uuid, send_to_me=send_to_me,
            public_id=public_id, sender_name=sender_name,
        )

    async def link_receive(self, link_uuid: str) -> dict:
        """
        送金リンクを受け取る。★ここで実際にお金が動く。

        成功したら必ず残高を検証してから記帳すること（docs/03 §3.1）。
        """
        self._require_login()
        if not link_uuid:
            raise KyashError("リンクIDが指定されていません")
        return await self._request("PUT", f"/v1/links/{link_uuid}/receive")

    async def link_cancel(self, link_uuid: str) -> dict:
        self._require_login()
        return await self._request("DELETE", f"/v1/links/{link_uuid}")

    async def create_link(self, amount: int, message: str = "", is_claim: bool = False) -> str:
        """送金／請求リンクを作る。返り値はURL。"""
        self._require_login()
        payload = {
            "amount": int(amount),
            "message": message,
            "type": "REQUEST" if is_claim else "SEND",
        }
        data = await self._request("POST", "/v1/me/links", json=payload)
        d = (data.get("result") or {}).get("data") or {}
        return d.get("url") or d.get("link") or ""

    async def healthcheck(self) -> bool:
        try:
            await self.get_wallet()
            return True
        except KyashError:
            return False


def _parse_yen(text: str) -> int:
    """「¥1,000」のような表記から数値を取り出す。"""
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0
