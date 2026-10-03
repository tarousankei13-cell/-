"""
注文番号を、別置きのページへ送る

BOT とページを別の場所で動かすときに使う。
ページ側はデータベースを共有せず、ここから送られた内容だけを持つ。

⚠️ 送れなくても注文は成立している。ここで例外を外へ出さない。
   ページが落ちていても、注文番号は DM の控えに入っている。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import config
from core import settings
from core.http import build_async_client

log = logging.getLogger("bot.web_push")

TIMEOUT = 5.0       # 注文の裏で走るので、長く待たない


def secret_digest(secret: str) -> str:
    """
    合い言葉を、そのまま送らずに指紋にして送る。

    ⚠️ HTTPヘッダは ASCII しか運べない。日本語の合い言葉を
       そのまま入れると、送る側で UnicodeEncodeError、
       受ける側で hmac.compare_digest が TypeError になる。
       指紋（16進）にすれば、どんな文字でも使える。

    合い言葉そのものが回線や中継のログに残らない利点もある。
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def configured() -> bool:
    return bool(push_url()) and bool(push_secret())


def push_url() -> str:
    return str(settings.get("web_push_url", config.WEB_PUSH_URL) or "").strip()


def push_secret() -> str:
    return str(settings.get("web_push_secret", config.WEB_PUSH_SECRET) or "").strip()


def page_link(token: str | None) -> str:
    """
    別置きページの公開URL。設定が無ければ空文字。

    登録先（/api/receipts）から、見てもらうURLを組み立てる。
    """
    if not token or not configured():
        return ""
    base = str(settings.get("web_base_url", config.WEB_BASE_URL) or "").strip()
    if not base:
        # 公開URLの指定が無ければ、登録先から推測する
        base = push_url().rsplit("/api/", 1)[0].rstrip("/") + "/order"
    return f"{base.rstrip('/')}/{token}"


async def send(
    *, token: str, receipt_number: str, store_name: str = "",
    store_id: str = "", pickup_label: str = "", created: datetime | None = None,
) -> bool:
    """
    ページへ1件登録する。送れたら True。

    送れなくても呼び出し側は何もしなくてよい。
    """
    if not configured() or not token or not receipt_number:
        return False

    payload = {
        "token": token,
        "receipt_number": receipt_number,
        "store_name": store_name,
        "store_id": store_id,
        "pickup_label": pickup_label,
        "created_at": (created or config.now_jst()).isoformat(),
    }
    try:
        client = build_async_client(timeout=TIMEOUT)
        try:
            r = await client.post(
                push_url(),
                json=payload,
                headers={"X-Push-Secret": secret_digest(push_secret())},
            )
        finally:
            await client.aclose()
    except Exception as e:
        # 相手が落ちている・URLが間違っている・回線が切れた
        log.warning("注文番号をページへ送れませんでした: %s", e)
        return False

    if r.status_code == 403:
        log.error(
            "ページ側に断られました。BOT とページの合い言葉"
            "（PUSH_SECRET）が一致しているか確認してください。"
        )
        return False
    if r.status_code >= 400:
        log.warning("ページが受け取りませんでした（HTTP %s）", r.status_code)
        return False
    return True


async def check() -> tuple[bool, str]:
    """
    つながるか確かめる。(つながったか, 説明) を返す。
    管理コマンドから呼ぶ。
    """
    url = push_url()
    if not url:
        return False, "送り先が設定されていません。"
    if not push_secret():
        return False, "合い言葉が設定されていません。"

    health = url.rsplit("/api/", 1)[0].rstrip("/") + "/healthz"
    try:
        client = build_async_client(timeout=TIMEOUT)
        try:
            r = await client.get(health)
        finally:
            await client.aclose()
    except Exception as e:
        return False, f"つながりませんでした: {e}"

    if r.status_code != 200:
        return False, f"応答がおかしいです（HTTP {r.status_code}）。URLをご確認ください。"
    try:
        ready = bool(r.json().get("ready"))
    except Exception:
        return True, "つながりましたが、応答の形が想定と違います。"
    if not ready:
        return False, "ページ側に PUSH_SECRET が設定されていません。"
    return True, "つながりました。"
