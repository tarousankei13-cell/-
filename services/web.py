"""
注文番号ページ

店頭で注文番号を見せるためだけの、小さなサイト。
BOT と同じプロセスで動く（aiohttp は discord.py が持っているので
追加のインストールは要らない）。

公開のしかた:
  1. `/config web` で有効にする
  2. 前段の nginx などから、このポートへ回す
  3. `/config web base_url https://example.com/order` で公開URLを教える
     （DMに出すリンクを組み立てるのに使う）

⚠️ URLには推測できない合い言葉（view_token）を入れる。
   注文番号そのものをURLにしてはいけない。4桁しかないので、
   順に試すだけで他人の注文が覗けてしまう。

⚠️ ここに出すのは「店頭で番号を見せる」ために要る情報だけ。
   いくら払ったか、誰が頼んだかは出さない。
"""

from __future__ import annotations

import html
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from aiohttp import web

import config
from core import settings
from db.models import Order, as_utc
from db.session import session_scope
from services import receipt_page
from services.mcd.protocol import PICKUP_LABEL
from sqlalchemy import select

log = logging.getLogger("bot.web")

TOKEN_BYTES = 16        # 128ビット。総当たりは現実的でない


def new_view_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def page_url(token: str | None) -> str:
    """
    公開URL。設定が無ければ空文字（呼び出し側はリンクを出さない）。
    """
    if not token:
        return ""
    if not should_start():
        return ""
    # 公開URLは環境変数でも渡せる（ホスティングのサイトURLをそのまま入れる）
    base = (
        os.environ.get("WEB_BASE_URL", "").strip()
        or str(settings.get("web_base_url", config.WEB_BASE_URL) or "").strip()
    )
    if not base:
        return ""
    return f"{base.rstrip('/')}/{token}"


def _expired(created: datetime | None) -> bool:
    """期限切れか。注文番号は当日しか使わないので、長くは残さない。"""
    if created is None:
        return True
    hours = int(settings.get("receipt_page_hours", config.RECEIPT_PAGE_HOURS))
    if hours <= 0:
        return False        # 0以下なら期限なし
    return datetime.now(timezone.utc) - created > timedelta(hours=hours)


# ============================================================
#  見た目
# ============================================================

# ============================================================
#  経路
# ============================================================

def _shell(title: str, body: str) -> web.Response:
    """receipt_page が組んだHTMLを、そのまま返す。"""
    return web.Response(
        text=body, content_type="text/html", charset="utf-8",
        headers=dict(receipt_page.HEADERS),
    )


def _not_found() -> web.Response:
    return _shell("見つかりません", receipt_page.not_found_html())


def render_order(
    *, receipt_number: str, store_name: str, store_id: str,
    pickup_label: str, created: datetime | None,
) -> web.Response:
    return _shell(
        f"ご注文番号 {receipt_number}",
        receipt_page.order_html(
            receipt_number=receipt_number, store_name=store_name,
            store_id=store_id, pickup_label=pickup_label, created=created,
        ),
    )


async def handle_order(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    # 合い言葉の形だけ先に見る。DBを引くまでもない文字列を弾く。
    if not token or len(token) > 64:
        return _not_found()

    async with session_scope() as s:
        row = (
            await s.execute(select(Order).where(Order.view_token == token))
        ).scalars().first()
        if row is None or not row.receipt_number:
            return _not_found()
        created = as_utc(row.created_at)
        if _expired(created):
            return _not_found()
        return render_order(
            receipt_number=row.receipt_number,
            store_name=row.store_name or "",
            store_id=row.store_id or "",
            pickup_label=PICKUP_LABEL.get(row.pickup_method or "", ""),
            created=created,
        )


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", handle_health)
    # 前段で /order/ を剥がす置き方と、剥がさない置き方の両方に備える
    app.router.add_get("/{token}", handle_order)
    app.router.add_get("/order/{token}", handle_order)
    return app


# ============================================================
#  起動と停止
# ============================================================

_runner: web.AppRunner | None = None


def _from_env() -> tuple[str, int] | None:
    """
    ホスティングサービスが指定してくる待ち受け先。

    多くのサービス（Render / Railway / Fly / puratya など）は、
    起動のたびにポートを決めて環境変数 PORT で渡してくる。
    そこを使わないとサイトが開けないので、**設定より環境変数を優先**する。

    ⚠️ HOST は 0.0.0.0 でなければ外から届かない。
       127.0.0.1 や localhost にすると、立ち上がっているのに
       「サイトが開けません」になる。
    """
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        log.warning("環境変数 PORT の値が数字ではありません: %r", raw)
        return None
    host = os.environ.get("HOST", "").strip() or "0.0.0.0"
    return host, port


def should_start() -> bool:
    """
    立ち上げるかどうか。

    PORT が渡されている＝ホスティング側がHTTPサーバを待っている、
    ということなので、設定を待たずに立ち上げる。
    そうしないと、置いただけでは「サイトが開けません」になる。
    """
    if _from_env() is not None:
        return True
    return bool(settings.get("web_enabled", config.WEB_ENABLED))


async def start() -> str:
    """
    サイトを立ち上げる。立ち上げたURLを返す（無効なら空文字）。

    ⚠️ ここで失敗しても BOT は動く。注文番号は DM の控えにも入っている。
    """
    global _runner
    if _runner is not None:
        return ""
    if not should_start():
        return ""

    env = _from_env()
    if env is not None:
        host, port = env
        log.info("待ち受け先を環境変数から読みました（HOST=%s PORT=%s）", host, port)
    else:
        host = str(settings.get("web_host", config.WEB_HOST))
        port = int(settings.get("web_port", config.WEB_PORT))
    try:
        _runner = web.AppRunner(build_app(), access_log=None)
        await _runner.setup()
        site = web.TCPSite(_runner, host, port)
        await site.start()
    except Exception:
        log.exception("注文番号ページを立ち上げられませんでした（%s:%s）", host, port)
        _runner = None
        return ""

    log.info("注文番号ページを公開しました: http://%s:%s", host, port)
    return f"http://{host}:{port}"


async def stop() -> None:
    global _runner
    if _runner is None:
        return
    try:
        await _runner.cleanup()
    except Exception:
        log.exception("注文番号ページの停止に失敗しました")
    finally:
        _runner = None


def running() -> bool:
    return _runner is not None
