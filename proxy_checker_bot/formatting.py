"""見た目まわりの小さな整形ヘルパー。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from checker import METHOD_CONNECT, ProxyResult

# 応答速度のバッジ (速い順)
_LATENCY_BADGES: tuple[tuple[float, str], ...] = (
    (500.0, "🟢"),
    (1500.0, "🟡"),
    (3000.0, "🟠"),
)
_SLOW_BADGE = "🔴"
_DEAD_BADGE = "⚫"

_BAR_FILLED = "▰"
_BAR_EMPTY = "▱"


def flag_emoji(country_code: Optional[str]) -> str:
    """ISO 3166-1 alpha-2 を国旗絵文字にする。"""
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🏴"
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in code)


def latency_badge(latency_ms: Optional[float]) -> str:
    if latency_ms is None:
        return _DEAD_BADGE
    for threshold, badge in _LATENCY_BADGES:
        if latency_ms < threshold:
            return badge
    return _SLOW_BADGE


def progress_bar(done: int, total: int, width: int = 12) -> str:
    ratio = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    return f"{_BAR_FILLED * filled}{_BAR_EMPTY * (width - filled)} **{ratio * 100:.0f}%**"


def format_latency(latency_ms: Optional[float]) -> str:
    if latency_ms is None:
        return "―"
    if latency_ms >= 1000:
        return f"{latency_ms / 1000:.2f}s"
    return f"{latency_ms:.0f}ms"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}分{rest:.0f}秒"


def clip(text: str, limit: int) -> str:
    """埋め込みの文字数上限に合わせて安全に切る。"""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 4)].rstrip() + " …"


def location_text(result: ProxyResult) -> str:
    """国旗 + 国名 (不明なら控えめに)。"""
    if result.country or result.country_code:
        name = result.country or result.country_code or ""
        code = f" ({result.country_code})" if result.country_code and result.country else ""
        return f"{flag_emoji(result.country_code)} {name}{code}".strip()
    return "🏴 国不明"


def result_line(result: ProxyResult, index: int) -> str:
    """結果1件を2行で表す。"""
    head = f"{latency_badge(result.latency_ms if result.ok else None)} `{index:>2}.` `{result.address}`"

    if not result.ok:
        return f"{head}\n　└ ❌ {result.error or '判定失敗'}"

    details = [f"⚡ {format_latency(result.latency_ms)}", location_text(result)]
    if result.exit_ip:
        details.append(f"出口 `{result.exit_ip}`")
    if result.method == METHOD_CONNECT:
        details.append("🚇 CONNECT専用")
    elif result.https:
        details.append("🔒 HTTPS可")
    if result.target.authenticated:
        details.append("🔑 認証付き")
    return f"{head}\n　└ " + " ・ ".join(details)


def alive_list_text(results: Sequence[ProxyResult]) -> str:
    """他のツールへそのまま貼れる ip:port 一覧。"""
    return "\n".join(r.address for r in results if r.ok) + "\n"


def report_text(results: Sequence[ProxyResult], *, timeout: float, concurrency: int) -> str:
    """全件の詳細レポート (等幅前提のテキスト)。"""
    alive = [r for r in results if r.ok]
    total = len(results)
    rate = (len(alive) / total * 100) if total else 0.0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# プロキシチェック結果",
        f"# 実行日時   : {stamp}",
        f"# 件数       : 生存 {len(alive)} / {total} (成功率 {rate:.1f}%)",
        f"# 設定       : タイムアウト {timeout:.1f}秒 / 同時実行 {concurrency}台",
        "#",
        f"# {'STATUS':<6} {'ADDRESS':<24} {'LATENCY':>8} {'METHOD':<8} {'HTTPS':<6} "
        f"{'EXIT_IP':<16} DETAIL",
    ]

    ordered = sorted(alive, key=lambda r: r.latency_ms or 0.0)
    ordered += [r for r in results if not r.ok]

    for result in ordered:
        method = {METHOD_CONNECT: "CONNECT"}.get(result.method or "", "HTTP")
        if result.ok:
            https = {True: "yes", False: "no", None: "-"}[result.https]
            country = result.country or "-"
            code = f" ({result.country_code})" if result.country_code else ""
            detail = f"{country}{code}"
            latency = f"{result.latency_ms:.0f}ms" if result.latency_ms else "-"
            status = "OK"
            exit_ip = result.exit_ip or "-"
        else:
            https, detail, latency, status, exit_ip = "-", result.error or "判定失敗", "-", "NG", "-"
            method = "-"
        lines.append(
            f"  {status:<6} {result.address:<24} {latency:>8} {method:<8} {https:<6} "
            f"{exit_ip:<16} {detail}"
        )

    return "\n".join(lines) + "\n"
