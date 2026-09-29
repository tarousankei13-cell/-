"""
外形監視

マクドナルド側が落ちていることに、利用者からの報告で気付くのでは遅い。
軽い読み取りを定期的に投げて、応答しなくなったらすぐ管理者へ知らせる。

  ・使うのは**認証のいらない読み取り**だけ（店舗情報の取得）
  ・注文には一切影響しない
  ・状態が変わったときだけ通知する（落ちた・戻った）
    毎回通知すると、本当の異常が埋もれてしまう
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import config
from core import breaker
from core.http import catalog_session

log = logging.getLogger("bot.monitor")

# 監視に使う店舗。実在していて、どの時間でも取得できるもの。
PROBE_STORE = "13934"   # 南砂町店

UP = "up"
DOWN = "down"
UNKNOWN = "unknown"


@dataclass
class Health:
    """1つの配信元の様子。"""
    name: str
    state: str = UNKNOWN
    latency_ms: float = 0.0
    last_ok: float = 0.0
    last_error: str = ""
    consecutive_failures: int = 0
    checked_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state == UP


@dataclass
class Report:
    """1回ぶんの結果。"""
    results: list[Health] = field(default_factory=list)
    became_down: list[Health] = field(default_factory=list)
    became_up: list[Health] = field(default_factory=list)

    @property
    def all_down(self) -> bool:
        return bool(self.results) and all(not h.ok for h in self.results)

    @property
    def any_change(self) -> bool:
        return bool(self.became_down or self.became_up)

    @property
    def healthy(self) -> int:
        return sum(1 for h in self.results if h.ok)


_state: dict[str, Health] = {}
# 何回続けて失敗したら「落ちた」と判断するか。
# 1回の失敗で騒ぐと、一時的な乱れのたびに通知が飛ぶ。
FAILURES_TO_REPORT = 2


def snapshot() -> list[Health]:
    return sorted(_state.values(), key=lambda h: h.name)


def reset() -> None:
    _state.clear()


async def _probe(client, group: str) -> Health:
    h = _state.get(group)
    if h is None:
        h = _state[group] = Health(name=group)

    url = (
        f"https://data.cat.{group}.prod.mop.mcd.qorcommerce.com/{PROBE_STORE}.json"
    )
    start = time.perf_counter()
    try:
        r = await client.get(url, timeout=10.0)
        ms = (time.perf_counter() - start) * 1000
        # 404 でも「サーバーは動いている」と分かるので、生存としては合格。
        # その店舗がそのグループに無いだけ。
        alive = r.status_code < 500
        error = "" if alive else f"HTTP {r.status_code}"
    except Exception as e:
        ms = (time.perf_counter() - start) * 1000
        alive, error = False, type(e).__name__

    h.latency_ms = ms
    h.checked_at = time.time()
    if alive:
        h.consecutive_failures = 0
        h.last_ok = h.checked_at
        h.last_error = ""
        h.state = UP
    else:
        h.consecutive_failures += 1
        h.last_error = error
        if h.consecutive_failures >= FAILURES_TO_REPORT:
            h.state = DOWN
    return h


async def check() -> Report:
    """
    全ての配信元を確かめる。

    落ちた・戻ったの変化だけを Report に入れて返す。
    呼び出し側は変化があったときだけ通知すること。
    """
    report = Report()
    before = {g: _state[g].state for g in _state}

    async with catalog_session() as client:
        results = await asyncio.gather(
            *[_probe(client, g) for g in config.MCD_GROUPS]
        )

    for h in results:
        report.results.append(h)
        was = before.get(h.name, UNKNOWN)
        if h.state == DOWN and was != DOWN:
            report.became_down.append(h)
        elif h.state == UP and was == DOWN:
            report.became_up.append(h)
            # 復帰したら、止めていた経路も戻す
            breaker.groups.get(h.name).reset()

    if report.became_down:
        log.error(
            "応答しなくなりました: %s",
            ", ".join(f"{h.name}({h.last_error})" for h in report.became_down),
        )
    if report.became_up:
        log.info("復帰しました: %s", ", ".join(h.name for h in report.became_up))
    return report


def format_report(r: Report) -> str:
    """管理者への通知文。変化が無ければ空。"""
    if not r.any_change:
        return ""
    lines = []
    if r.became_down:
        lines.append("**応答しなくなりました**")
        for h in r.became_down:
            lines.append(f"　{h.name}　`{h.last_error}`")
    if r.became_up:
        lines.append("**復帰しました**")
        for h in r.became_up:
            lines.append(f"　{h.name}　{h.latency_ms:.0f}ms")
    lines.append(f"\n正常 {r.healthy} / {len(r.results)} 件")
    if r.all_down:
        lines.append(
            "\n⚠️ **すべての配信元が応答していません。**\n"
            "マクドナルド側の障害か、この端末の回線に問題があります。"
        )
    return "\n".join(lines)
