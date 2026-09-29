"""
通信の記録と計測

目的は2つ。

  1. **相関ID** — 1回の注文に紐づく全リクエストへ同じIDを振る。
     ログを `注文 a1b2c3` で絞れば、その注文で何が起きたかだけを
     一続きで追える。障害調査の時間が桁で変わる。

  2. **計測** — 相手先ごとの応答時間と失敗率を残す。
     「最近遅い」を感覚ではなく数字で判断できるようにする。

⚠️ 相関IDは**こちらのログにだけ**使う。マクドナルドやKyashへ送る
   リクエストには載せない。こちら独自のヘッダを付けると、
   通常のアプリと違う通信として識別されうるため。
"""

from __future__ import annotations

import functools
import logging
import statistics
import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

log = logging.getLogger("bot.telemetry")

# いま処理している作業のID。注文・チャージ・同期ごとに振る。
_correlation: ContextVar[str] = ContextVar("correlation_id", default="")

# 直近この件数ぶんの応答時間から中央値などを出す。
# 増やすほど精度は上がるがメモリを食う。1GBのVPSを想定して控えめに。
WINDOW = 300


def new_id(prefix: str = "") -> str:
    """短い相関IDを作る。ログで目視できる長さにしておく。"""
    tail = uuid.uuid4().hex[:8]
    return f"{prefix}-{tail}" if prefix else tail


def set_correlation(value: str):
    """相関IDを設定し、元に戻すためのトークンを返す。"""
    return _correlation.set(value)


def reset_correlation(token) -> None:
    _correlation.reset(token)


def current() -> str:
    return _correlation.get()


class Correlation:
    """
    `async with Correlation("注文"):` の形で使う。

    中で行った通信のログには、すべて同じIDが付く。
    """

    def __init__(self, prefix: str = "", value: str = "") -> None:
        self.value = value or new_id(prefix)
        self._token = None

    def __enter__(self) -> str:
        self._token = set_correlation(self.value)
        return self.value

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            reset_correlation(self._token)

    async def __aenter__(self) -> str:
        return self.__enter__()

    async def __aexit__(self, *exc) -> None:
        self.__exit__(*exc)


def traced(prefix: str):
    """
    関数の実行中だけ相関IDを振るデコレータ。

        @traced("注文")
        async def run_order(...): ...

    途中で return しても例外で抜けても必ず元に戻るので、
    出口が複数ある関数でも安全に使える。
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            with Correlation(prefix):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


class CorrelationFilter(logging.Filter):
    """全てのログ行に相関IDを載せる。無いときは空欄にする。"""

    def filter(self, record: logging.LogRecord) -> bool:
        value = current()
        record.corr = f"[{value}] " if value else ""
        return True


# ============================================================
#  計測
# ============================================================

@dataclass
class HostStats:
    """1つの相手先ぶんの記録。"""
    host: str
    total: int = 0
    failures: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    durations: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))
    statuses: dict[int, int] = field(default_factory=dict)

    @property
    def failure_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0

    def percentile(self, q: float) -> float | None:
        """応答時間のパーセンタイル（ミリ秒）。記録が少なければ None。"""
        if len(self.durations) < 2:
            return self.durations[0] if self.durations else None
        ordered = sorted(self.durations)
        # statistics.quantiles は分割数が要るので、素朴に位置で取る
        idx = min(int(q * len(ordered)), len(ordered) - 1)
        return ordered[idx]

    @property
    def p50(self) -> float | None:
        return self.percentile(0.50)

    @property
    def p95(self) -> float | None:
        return self.percentile(0.95)

    @property
    def average(self) -> float | None:
        return statistics.fmean(self.durations) if self.durations else None


class Metrics:
    """相手先ごとの記録をまとめて持つ。"""

    def __init__(self) -> None:
        self._hosts: dict[str, HostStats] = {}
        self.started_at = time.time()

    def record(
        self, host: str, *, ms: float, status: int | None = None, error: str = ""
    ) -> None:
        stats = self._hosts.get(host)
        if stats is None:
            stats = self._hosts[host] = HostStats(host=host)
        stats.total += 1
        stats.durations.append(ms)
        if status is not None:
            stats.statuses[status] = stats.statuses.get(status, 0) + 1
        # 4xx は「相手は動いているがこちらの要求が通らなかった」なので
        # 通信の失敗には数えない。5xx と例外だけを失敗とする。
        if error or (status is not None and status >= 500):
            stats.failures += 1
            stats.last_error = error or f"HTTP {status}"
            stats.last_error_at = time.time()

    def snapshot(self) -> list[HostStats]:
        """通信の多い順に返す。"""
        return sorted(self._hosts.values(), key=lambda s: s.total, reverse=True)

    def get(self, host: str) -> HostStats | None:
        return self._hosts.get(host)

    def reset(self) -> None:
        self._hosts.clear()
        self.started_at = time.time()

    @property
    def total_requests(self) -> int:
        return sum(s.total for s in self._hosts.values())

    @property
    def total_failures(self) -> int:
        return sum(s.failures for s in self._hosts.values())


metrics = Metrics()


def short_host(url: str) -> str:
    """
    記録用の相手先名。

    店舗ごとにホスト名が変わるわけではないが、グループ名が入るので
    `data.cat.group-f...` のような長い名前になる。
    見やすいよう短くまとめる。
    """
    host = urlparse(str(url)).hostname or "?"
    for prefix, label in (
        ("data.cat.", "data.cat"),
        ("ord.", "ord"),
        ("tid.ord.", "tid"),
    ):
        if host.startswith(prefix):
            # グループ名だけ残す（data.cat/group-f）
            part = host[len(prefix):].split(".")[0]
            return f"{label}/{part}"
    # 先頭2つのラベルだけ残す（user-api.dir... → user-api）
    return host.split(".")[0] if host.count(".") >= 2 else host


class MeasuredTransport(httpx.AsyncBaseTransport):
    """
    httpx の通信を横から計測する。

    クライアントの使い方を変えずに差し込めるよう、transport を包む形にした。
    例外で終わった場合も必ず記録する。
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = short_host(request.url)
        start = time.perf_counter()
        try:
            response = await self._inner.handle_async_request(request)
        except Exception as e:
            ms = (time.perf_counter() - start) * 1000
            metrics.record(host, ms=ms, error=type(e).__name__)
            log.debug("%s %s → %s (%.0fms)", request.method, host, type(e).__name__, ms)
            raise
        ms = (time.perf_counter() - start) * 1000
        metrics.record(host, ms=ms, status=response.status_code)
        if ms > 3000:
            log.info("応答が遅いです: %s %s %.1f秒", request.method, host, ms / 1000)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
