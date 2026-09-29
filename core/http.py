"""
HTTPクライアントの生成をここに集約する

通信の設定（計測・タイムアウト・圧縮・接続の使い回し）を
一箇所で決められるようにするための入り口。

BOT内で `httpx.AsyncClient(...)` を直接作らず、必ずここを通すこと。
そうしておけば、通信の方針を変えるときに全箇所へ反映できる。
"""

from __future__ import annotations

import logging

import httpx

from core.telemetry import MeasuredTransport

log = logging.getLogger("bot.http")

# 既定のタイムアウト（秒）
#   接続だけ短くしておくのが要点。応答しない相手に20秒待つと、
#   利用者は「ボタンが効かない」と感じる。
#   読み取りは注文処理に時間がかかることがあるので長めに取る。
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


def build_async_client(
    *,
    timeout: httpx.Timeout | float | None = None,
    proxy: str | None = None,
    max_connections: int = 16,
    max_keepalive: int = 8,
    follow_redirects: bool = True,
    headers: dict[str, str] | None = None,
    measured: bool = True,
) -> httpx.AsyncClient:
    """
    計測付きの AsyncClient を作る。

    measured=False にすると計測を外せる（計測そのものの検証用）。
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    elif isinstance(timeout, (int, float)):
        timeout = httpx.Timeout(timeout)

    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive,
    )

    kwargs: dict = {
        "timeout": timeout,
        "limits": limits,
        "follow_redirects": follow_redirects,
    }
    if proxy:
        kwargs["proxy"] = proxy
    if headers:
        kwargs["headers"] = headers

    if measured:
        # 通信を計測できるよう transport を包む。
        # AsyncHTTPTransport には limits/proxy を渡す必要がある。
        inner = httpx.AsyncHTTPTransport(
            limits=limits, proxy=proxy, retries=0,
        )
        kwargs["transport"] = MeasuredTransport(inner)

    return httpx.AsyncClient(**kwargs)
