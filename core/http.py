"""
HTTPクライアントの生成をここに集約する

通信の設定（計測・タイムアウト・圧縮・接続の使い回し）を
一箇所で決められるようにするための入り口。

BOT内で `httpx.AsyncClient(...)` を直接作らず、必ずここを通すこと。
そうしておけば、通信の方針を変えるときに全箇所へ反映できる。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx

from core.telemetry import MeasuredTransport

log = logging.getLogger("bot.http")

# 既定のタイムアウト（秒）
#   接続だけ短くしておくのが要点。応答しない相手に20秒待つと、
#   利用者は「ボタンが効かない」と感じる。
#   読み取りは注文処理に時間がかかることがあるので長めに取る。
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)

# 共通で付けるヘッダ
#   圧縮を明示する。メニューは1件1MB近くあるので、
#   指定しておかないと無駄に帯域を使う。
DEFAULT_HEADERS = {
    "Accept-Encoding": "br, gzip, deflate",
}

# HTTP/2 を使うか。
#   マクドナルドの配信元は HTTP/2 に対応している。
#   1本の接続に複数の要求を流せるので、店舗同期のように
#   一度にたくさん取りに行く場面で効く（実測 2.0秒 → 1.3秒）。
#   h2 が入っていない環境では自動的に HTTP/1.1 に落ちる。
try:
    import h2  # noqa: F401

    HTTP2_AVAILABLE = True
except ImportError:  # pragma: no cover - 依存を入れていない環境向け
    HTTP2_AVAILABLE = False
    log.info("h2 が入っていないため HTTP/1.1 で通信します")


def build_async_client(
    *,
    timeout: httpx.Timeout | float | None = None,
    proxy: str | None = None,
    service: str | None = None,
    max_connections: int = 16,
    max_keepalive: int = 8,
    follow_redirects: bool = True,
    headers: dict[str, str] | None = None,
    measured: bool = True,
    http2: bool | None = None,
) -> httpx.AsyncClient:
    """
    計測付きの AsyncClient を作る。

    measured=False にすると計測を外せる（計測そのものの検証用）。

    ⚠️ **外へ出る通信は必ず service を渡すこと。**
       proxy を省いたとき、service に応じた設定（/proxy set）を
       自動で引く。渡し忘れると、その通信だけプロキシを通らず
       素のIPで出てしまう。
       proxy を明示したときは、そちらが優先される（口座ごとの指定）。
    """
    if not proxy and service:
        from core import proxy as proxy_mod

        proxy = proxy_mod.resolve(service)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    elif isinstance(timeout, (int, float)):
        timeout = httpx.Timeout(timeout)

    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive,
    )

    use_http2 = HTTP2_AVAILABLE if http2 is None else (http2 and HTTP2_AVAILABLE)

    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)

    kwargs: dict = {
        "timeout": timeout,
        "limits": limits,
        "follow_redirects": follow_redirects,
        "headers": merged,
        "http2": use_http2,
    }
    if proxy:
        kwargs["proxy"] = proxy

    if measured:
        # 通信を計測できるよう transport を包む。
        # AsyncHTTPTransport には limits/proxy/http2 を渡す必要がある。
        inner = httpx.AsyncHTTPTransport(
            limits=limits, proxy=proxy, retries=0, http2=use_http2,
        )
        kwargs["transport"] = MeasuredTransport(inner)

    return httpx.AsyncClient(**kwargs)


# ------------------------------------------------------------
#  カタログ用の共用クライアント
# ------------------------------------------------------------
# 店舗やメニューの取得は認証がいらず、どのアカウントからでも同じ結果になる。
# 毎回クライアントを作るとそのたびにTLSの handshake が起きるため、
# 1本を使い回して接続を温かいまま保つ。
_catalog: httpx.AsyncClient | None = None

# どのプロキシで作ったか。設定が変わったら作り直すために覚えておく。
#   ⚠️ 使い回しているので、設定し直しても勝手には切り替わらない。
#      ここで見張らないと「変えたのに前の出口から出続ける」ことになる。
_catalog_proxy: str | None = None


def catalog_client() -> httpx.AsyncClient:
    """店舗・メニューの取得に使う共用クライアント。"""
    global _catalog, _catalog_proxy
    from core import proxy as proxy_mod

    want = proxy_mod.resolve("mcd")
    if _catalog is not None and not _catalog.is_closed and want != _catalog_proxy:
        # 設定が変わった。古い接続は捨てる。
        old, _catalog = _catalog, None
        import asyncio

        try:
            asyncio.get_running_loop().create_task(old.aclose())
        except RuntimeError:          # ループ外から呼ばれたとき
            pass
        log.info("プロキシの設定が変わったため、カタログ用の接続を作り直します")

    if _catalog is None or _catalog.is_closed:
        _catalog = build_async_client(
            max_connections=32, max_keepalive=16, proxy=want,
        )
        _catalog_proxy = want
        log.info("カタログ用の接続を用意しました（HTTP/2 %s・プロキシ %s）",
                 "あり" if HTTP2_AVAILABLE else "なし",
                 proxy_mod.mask(want))
    return _catalog


@asynccontextmanager
async def catalog_session():
    """
    共用の接続を借りる。

    `async with` を抜けても**閉じない**のが普通のクライアントとの違い。
    次に使うときに接続が温かいまま残るようにしておく。
    """
    yield catalog_client()


async def close_shared() -> None:
    """終了時に共用の接続を閉じる。"""
    global _catalog, _catalog_proxy
    if _catalog is not None and not _catalog.is_closed:
        await _catalog.aclose()
    _catalog = None
    _catalog_proxy = None
