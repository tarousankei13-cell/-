"""
マクドナルド モバイルオーダー API クライアント（非同期）

HATTIMCD は requests ベースの同期ライブラリで、discord.py から直接呼ぶと
注文中の数〜十数秒 BOT 全体が固まる。そのため httpx で書き直した。

エンドポイントの根拠は docs/01 §2〜§6。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from core.http import build_async_client
from core.telemetry import current as correlation_id
from services.mcd.protocol import (
    OrderResponse, build_authorise_body, build_get_paid_body,
    parse_order_response, pb_str, proto_parse, varint_encode,
)

log = logging.getLogger("bot.mcd")

OAUTH2_CLIENT_ID = "164d4a98beb14f13ce02a6fb62c7712c"
VERITRANS_TOKEN_API_KEY = "9d179769-488a-4769-b84a-e725f233257b"

AUTH_BASE = "https://authorization-vmob-prod-jpe.vmobapps.com"
CON_BASE = "https://con-japan-east-prod.vmobapps.com"
USER_API = "https://user-api.dir.prod.mop.mcd.qorcommerce.com"
PAY_API = "https://pay.dir.prod.mop.mcd.qorcommerce.com"
COUPON_API = "https://user-coupon.dir.prod.mop.mcd.qorcommerce.com"
DATA_CAT = "https://data.cat.{group}.prod.mop.mcd.qorcommerce.com"
ORD = "https://ord.{group}.prod.mop.mcd.qorcommerce.com"
TID = "https://tid.ord.{group}.prod.mop.mcd.qorcommerce.com"

GROUPS = ["group-j", "group-i", "group-h", "group-g", "group-f", "group-e"]


class McdError(Exception):
    pass


class McdAuthError(McdError):
    pass


class McdOrderError(McdError):
    pass


class McdNetworkError(McdError):
    pass


@dataclass
class Fingerprint:
    """
    端末の識別情報。

    HATTIMCD は全アカウントで同じ値をハードコードしていた（docs/01 §7）。
    複数アカウントを同じ端末・同じ座標から使うのは不自然なので、
    アカウントごとに生成して固定する。
    """
    device_uid: str
    wmop_device_id: str
    fb_instance_id: str
    latitude: float
    longitude: float

    @classmethod
    def generate(cls, latitude: float = 35.681236, longitude: float = 139.767125) -> "Fingerprint":
        return cls(
            device_uid=str(uuid.uuid4()).upper(),
            wmop_device_id=str(uuid.uuid4()).upper(),
            fb_instance_id=uuid.uuid4().hex.upper(),
            latitude=latitude,
            longitude=longitude,
        )


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    root_paseto: str = ""
    access_exp: float = 0.0
    root_exp: float = 0.0

    def access_valid(self, margin: float = 60.0) -> bool:
        return bool(self.access_token) and time.time() + margin < self.access_exp

    def root_valid(self, margin: float = 60.0) -> bool:
        return bool(self.root_paseto) and time.time() + margin < self.root_exp


def jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return time.time() + 1800  # 読めなければ30分後とみなす


def vmob_headers(fp: Fingerprint) -> dict[str, str]:
    return {
        "User-Agent": "McDonaldsJapan-Prod/5.5.30 (iPhone; iOS 26.3.1; Scale/3.00)",
        "x-vmob-device_os_version": "26.3.1",
        "x-vmob-location_accuracy": "6.076432",
        "x-vmob-device_network_type": "wifi",
        "x-vmob-location_longitude": f"{fp.longitude}",
        "x-vmob-location_latitude": f"{fp.latitude}",
        "x-vmob-device": "iPhone",
        "x-vmob-application_version": "1657",
        "x-vmob-uid": fp.device_uid,
        "x-vmob-beacons": "",
        "x-vmob-device_timezone_id": "Asia/Tokyo",
        "x-vmob-device_utc_offset": "+09:00",
        "x-vmob-device_screen_resolution": "2532x1170",
        "x-vmob-mobile_operator": "--",
        "Accept-Language": "ja-JP",
        "x-vmob-device_type": "i_p",
        "x-vmob-sdk_version": "5.11.1.593",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-vmob-cost-center": "McD-Japan",
        "x-vmob-authorization": "77c0e8d4-19ef-4e39-8348-859c5d60aebc",
    }


def qor_headers(fp: Fingerprint) -> dict[str, str]:
    return {
        "x-wmop-deviceid": fp.wmop_device_id,
        "x-fb-instance-id": fp.fb_instance_id,
        "x-wmop-app-version": "5.5.30",
        "x-wmop-app-build": "1657",
        "x-wmob-bundle-id": "jp.mcdonalds.coupon",
        "x-wmop-bundle-id": "jp.mcdonalds.coupon",
        "x-wmop-platform-version": "26.3.1",
        "x-wmop-platform": "iOS",
        "Accept": "*/*",
        "Accept-Language": "ja",
        "Cache-Control": "no-cache",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "McDonaldsJapan-Prod/1657 CFNetwork/3860.400.51 Darwin/25.3.0",
        "Connection": "keep-alive",
    }


# トークンが更新されたときに呼ばれる。DBへ即保存するために使う。
TokenSaver = Callable[[TokenSet], Awaitable[None]]


class McdClient:
    """
    1アカウント分のクライアント。

    ⚠️ refresh_token は更新のたびにローテーションする。
       取得したら on_tokens_updated で即座にDBへ保存すること。
       保存に失敗するとそのアカウントは二度とログインできなくなる。
    """

    def __init__(
        self,
        fingerprint: Fingerprint,
        tokens: TokenSet | None = None,
        *,
        proxy: str | None = None,
        timeout: float = 20.0,
        on_tokens_updated: TokenSaver | None = None,
    ) -> None:
        self.fp = fingerprint
        self.tokens = tokens or TokenSet()
        self._on_tokens_updated = on_tokens_updated
        self._client = build_async_client(
            timeout=timeout, proxy=proxy,
            max_connections=8, max_keepalive=4,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "McdClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _save_tokens(self) -> None:
        if self._on_tokens_updated:
            await self._on_tokens_updated(self.tokens)

    # -- 認証 -------------------------------------------------

    async def login(self, email: str, password: str) -> str:
        """メールとパスワードでログインし、MFAトークンを返す。"""
        try:
            r = await self._client.post(
                f"{CON_BASE}/v3/logins",
                headers=vmob_headers(self.fp),
                json={
                    "username": email, "password": password,
                    "returnConsumerInfo": False, "returnCrossReferences": False,
                    "grant_type": "password",
                },
            )
        except httpx.HTTPError as e:
            raise McdNetworkError(f"ログインに接続できませんでした: {e}") from e
        if r.status_code not in (200, 202):
            raise McdAuthError(
                "メールアドレスまたはパスワードが違います"
                if r.status_code in (400, 401, 403)
                else f"ログインに失敗しました (HTTP {r.status_code})"
            )
        d = r.json()
        token = d.get("jwtMfaToken") or d.get("jwtAccessToken", "")
        if not token:
            raise McdAuthError("認証コードの送信に失敗しました")
        return token

    async def login_with_mfa(self, mfa_token: str, otp: str) -> TokenSet:
        """SMS/メールで届いた6桁を入力してログインを完了する。"""
        try:
            r = await self._client.post(
                f"{CON_BASE}/v3/loginwithmfa",
                headers={**vmob_headers(self.fp), "Authorization": f"Bearer {mfa_token}"},
                json={"Otp": otp, "returnConsumerInfo": True, "returnCrossReferences": True},
            )
        except httpx.HTTPError as e:
            raise McdNetworkError(f"認証に接続できませんでした: {e}") from e
        if r.status_code != 200:
            raise McdAuthError("認証コードが正しくありません")
        d = r.json()
        access = d.get("jwtAccessToken", "")
        refresh = d.get("jwtRefreshToken", "")
        if not refresh:
            raise McdAuthError("認証には成功しましたが、トークンを取得できませんでした")
        self.tokens = TokenSet(
            access_token=access, refresh_token=refresh,
            access_exp=jwt_exp(access) if access else 0.0,
        )
        await self._save_tokens()
        return self.tokens

    async def refresh_access_token(self) -> None:
        if not self.tokens.refresh_token:
            raise McdAuthError("リフレッシュトークンがありません。再ログインが必要です")
        try:
            r = await self._client.get(
                f"{AUTH_BASE}/Authorization/AccessToken",
                headers={**vmob_headers(self.fp), "refreshToken": self.tokens.refresh_token},
            )
        except httpx.HTTPError as e:
            raise McdNetworkError(f"トークン更新に接続できませんでした: {e}") from e
        if r.status_code != 200:
            raise McdAuthError(
                f"トークンの更新に失敗しました (HTTP {r.status_code})。再ログインが必要です"
            )
        d = r.json()
        if new := d.get("jwtAccessToken"):
            self.tokens.access_token = new
            self.tokens.access_exp = jwt_exp(new)
        # ⚠️ リフレッシュトークンもローテーションする。必ず保存する。
        if new := d.get("jwtRefreshToken"):
            self.tokens.refresh_token = new
        await self._save_tokens()

    async def _get_oauth2_code(self) -> str:
        """有効期限30秒の認可コードを取得する。"""
        try:
            r = await self._client.post(
                f"{AUTH_BASE}///oauth2/code",
                headers={
                    **vmob_headers(self.fp),
                    "Authorization": f"Bearer {self.tokens.access_token}",
                },
                json={
                    "client_id": OAUTH2_CLIENT_ID, "scope": "openid email profile",
                    "code_challenge": "", "response_type": "code",
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as e:
            raise McdNetworkError(f"認可コードの取得に接続できませんでした: {e}") from e
        if r.status_code != 200:
            raise McdAuthError(f"認可コードの取得に失敗しました (HTTP {r.status_code})")
        code = r.json().get("code", "")
        if not code:
            raise McdAuthError("認可コードが空で返されました")
        return code

    async def refresh_root_paseto(self) -> None:
        code = await self._get_oauth2_code()
        blob = code.encode("utf-8")
        body = b"\x0a" + varint_encode(len(blob)) + blob
        try:
            r = await self._client.post(
                f"{USER_API}/app/mcduser.AuthorizationService/VerifyPlexureAuthorizationCode",
                headers=qor_headers(self.fp), content=body,
            )
        except httpx.HTTPError as e:
            raise McdNetworkError(f"PASETOの取得に接続できませんでした: {e}") from e
        if r.status_code != 200:
            raise McdAuthError(f"PASETOの取得に失敗しました (HTTP {r.status_code})")
        fields = proto_parse(r.content, strict=False)
        raw = next((v for v in fields.get(1, []) if isinstance(v, bytes)), None)
        if not raw:
            raise McdAuthError("PASETOが空で返されました")
        paseto = raw.decode("utf-8", errors="replace")
        if not paseto.startswith("v2.local."):
            raise McdAuthError("PASETOの形式が想定と異なります")
        self.tokens.root_paseto = paseto
        self.tokens.root_exp = time.time() + 3600
        await self._save_tokens()

    async def ensure_auth(self, *, force: bool = False) -> None:
        """
        必要なときだけトークンを更新する。

        HATTIMCD は毎回すべて取り直していたため、1注文で無駄な往復が
        8回以上あった。ここでは期限を見て必要な分だけ更新する。
        """
        if force or not self.tokens.access_valid():
            await self.refresh_access_token()
        if force or not self.tokens.root_valid():
            await self.refresh_root_paseto()

    def _auth_headers(self) -> dict[str, str]:
        return {**qor_headers(self.fp), "Authorization": f"Bearer {self.tokens.root_paseto}"}

    async def _qor_post(self, url: str, body: bytes, *, retry_on_401: bool = True) -> httpx.Response:
        try:
            r = await self._client.post(url, headers=self._auth_headers(), content=body)
        except httpx.HTTPError as e:
            raise McdNetworkError(f"通信に失敗しました: {e}") from e
        if r.status_code in (401, 403) and retry_on_401:
            log.info("認証切れを検出したため再取得します")
            await self.ensure_auth(force=True)
            return await self._qor_post(url, body, retry_on_401=False)
        return r

    # -- 店舗・メニュー（認証不要） ---------------------------

    async def fetch_store(
        self, store_id: str, group: str | None = None, etag: str | None = None
    ) -> tuple[dict | None, str, str]:
        """
        店舗情報を取得する。(データ, group, ETag) を返す。

        前回のETagを渡すと、内容が変わっていなければデータは None で返る
        （サーバーが 304 を返し、通信量がゼロで済む）。
        """
        headers = {"If-None-Match": etag} if etag else {}
        for g in ([group] if group else GROUPS):
            url = f"{DATA_CAT.format(group=g)}/{store_id}.json"
            try:
                r = await self._client.get(url, headers=headers)
            except httpx.HTTPError:
                continue
            if r.status_code == 304:
                return None, g, etag or ""
            if r.status_code == 200:
                return r.json(), g, r.headers.get("etag", "")
        raise McdError(f"店舗 {store_id} が見つかりません")

    async def fetch_menu(
        self, store_id: str, cat_root_url: str, etag: str | None = None
    ) -> tuple[dict | None, str]:
        """
        メニューカタログ（約1MB）を取得する。(データ, ETag) を返す。

        前回のETagを渡すと、変更が無ければ 304 が返りデータは None になる。
        高頻度で同期しても通信量がほとんど増えないのはこのため。
        """
        headers = {"If-None-Match": etag} if etag else {}
        try:
            r = await self._client.get(
                f"{cat_root_url}/{store_id}/menu.json", headers=headers, timeout=60.0
            )
        except httpx.HTTPError as e:
            raise McdNetworkError(f"メニューの取得に失敗しました: {e}") from e
        if r.status_code == 304:
            return None, etag or ""
        if r.status_code != 200:
            raise McdError(f"メニューを取得できませんでした (HTTP {r.status_code})")
        return r.json(), r.headers.get("etag", "")

    # -- カード -----------------------------------------------

    async def get_cards(self) -> list[dict]:
        await self.ensure_auth()
        r = await self._qor_post(f"{PAY_API}/app/mcdpay.CardService/GetCardInfos", b"")
        if r.status_code != 200:
            raise McdError(f"カード一覧を取得できませんでした (HTTP {r.status_code})")
        cards = []
        for raw in proto_parse(r.content, strict=False).get(1, []):
            if not isinstance(raw, bytes):
                continue
            f = proto_parse(raw, strict=False)

            def s(n: int) -> str:
                return f[n][0].decode("utf-8", "replace") if n in f and f[n] else ""

            cards.append({"card_id": s(1), "masked": s(2), "expiry": s(3), "name": s(4)})
        return cards

    # -- 注文 -------------------------------------------------

    async def get_pos_paseto(self, group: str) -> str:
        r = await self._qor_post(
            f"{TID.format(group=group)}/app/mcdtid.UserPosIdService/GetUserPosIdToken", b""
        )
        if r.status_code != 200:
            raise McdAuthError(f"POSトークンを取得できませんでした (HTTP {r.status_code})")
        fields = proto_parse(r.content, strict=False)
        raw = next((v for v in fields.get(1, []) if isinstance(v, bytes)), None)
        if not raw:
            raise McdAuthError("POSトークンが空で返されました")
        paseto = raw.decode("utf-8", errors="replace")
        if not paseto.startswith("v2.local."):
            raise McdAuthError("POSトークンの形式が想定と異なります")
        return paseto

    async def store_order(self, group: str, body: bytes) -> OrderResponse:
        """
        注文を登録する。

        ⚠️ 冪等ではない可能性があるため、同じ group への再試行はしない。
        """
        r = await self._qor_post(
            f"{ORD.format(group=group)}/app/mcdord.UserOrderService/StoreOrder", body
        )
        if r.status_code == 200 and r.content:
            return parse_order_response(r.content)
        raise McdOrderError(
            f"注文の登録に失敗しました (HTTP {r.status_code}): "
            f"{r.content.decode('utf-8', 'replace')[:200]}"
        )

    async def authorise_order(self, group: str, order_token: str) -> OrderResponse:
        """
        支払いを確定する。

        ⚠️ この呼び出しは絶対にリトライしないこと（二重課金になる）。
           失敗したら get_paid_order で実際の状態を確認する。
        """
        await self.ensure_auth(force=True)  # 確定直前に必ず新しいPASETOで
        r = await self._qor_post(
            f"{ORD.format(group=group)}/app/mcdord.UserOrderService/AuthoriseOrder",
            build_authorise_body(order_token),
            retry_on_401=False,   # ★リトライ禁止
        )
        if r.status_code != 200 or not r.content:
            raise McdOrderError(
                f"支払いの確定に失敗しました (HTTP {r.status_code}): "
                f"{r.content.decode('utf-8', 'replace')[:200]}"
            )
        return parse_order_response(r.content)

    async def get_paid_order(self, group: str, order_token: str) -> OrderResponse:
        """決済済みの注文を確認する。読み取り専用なので何度でも呼べる。"""
        r = await self._qor_post(
            f"{ORD.format(group=group)}/app/mcdord.UserOrderService/GetPaidOrder",
            build_get_paid_body(order_token),
        )
        if r.status_code != 200 or not r.content:
            raise McdOrderError(f"注文を確認できませんでした (HTTP {r.status_code})")
        return parse_order_response(r.content)

    async def get_buzzer_number(self, group: str, order_token: str) -> int | None:
        """呼び出し番号。出来上がり通知に使う。"""
        try:
            r = await self._qor_post(
                f"{ORD.format(group=group)}/app/mcdord.UserOrderService/GetOrderBuzzerNotification",
                pb_str(1, order_token),
            )
        except McdError:
            return None
        if r.status_code != 200 or not r.content:
            return None
        f = proto_parse(r.content, strict=False)
        for vals in f.values():
            for v in vals:
                if isinstance(v, int):
                    return v
        return None

    # -- 生存確認 ---------------------------------------------

    async def healthcheck(self) -> bool:
        """アカウントがまだ使えるかを軽い呼び出しで確認する。"""
        try:
            await self.ensure_auth()
            return bool(self.tokens.root_paseto)
        except McdError:
            return False
