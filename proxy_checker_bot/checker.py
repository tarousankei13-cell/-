"""生ソケットだけで動く HTTP / HTTPS プロキシチェッカー。

requests / aiohttp / PySocks のようなプロキシ用ライブラリは一切使わず、
asyncio の TCP ストリームへ HTTP を直接書き込んで判定する。

判定は2通りを自動で使い分ける:

  [1] HTTP中継 (絶対URI)      GET http://ip-api.com/json/ HTTP/1.1
        いわゆる普通の HTTP プロキシ。これが通ればそのまま結果を読む。

  [2] HTTPSトンネル (CONNECT)  CONNECT ipwho.is:443 HTTP/1.1
        [1] を拒否する「HTTPS専用プロキシ」向けのフォールバック。
        トンネルを張ったあと標準ライブラリの ssl で TLS を張って読む。

どちらも応答時間を測り、返ってきた JSON から出口IPと国名を取り出す。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# 定数
# --------------------------------------------------------------------------- #

#: [1] HTTP中継で使う判定先 (ip-api の無料枠は http のみ)
DEFAULT_JUDGE_URL = "http://ip-api.com/json/?fields=status,message,country,countryCode,query"

#: [2] CONNECT フォールバックで使う判定先 (https)
DEFAULT_HTTPS_JUDGE_URL = "https://ipwho.is/"

#: HTTPS (CONNECT) 対応判定でトンネル先に使うホスト
HTTPS_TEST_HOST = "example.com"
HTTPS_TEST_PORT = 443

#: レスポンス本文の読み取り上限
MAX_BODY_BYTES = 64 * 1024
_STREAM_LIMIT = 256 * 1024

#: 「平文HTTPの中継はできない」系のステータス。CONNECT へ切り替える合図。
_PLAIN_REFUSED_STATUS = frozenset({400, 403, 405, 501})

#: CONNECT + TLS には StreamWriter.start_tls (Python 3.11以降) が必要
_START_TLS_SUPPORTED = hasattr(asyncio.StreamWriter, "start_tls")

_USER_AGENT = "Mozilla/5.0 (compatible; SimpleProxyChecker/1.0)"

METHOD_HTTP = "http"
METHOD_CONNECT = "connect"

_IP_KEYS = ("query", "ip", "origin", "ipAddress", "client_ip", "clientIp", "address")
_COUNTRY_KEYS = ("country", "country_name", "countryName")
_COUNTRY_CODE_KEYS = ("countryCode", "country_code", "countryCodeIso")

_HOST_PART = r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"

# ip:port / http://ip:port / user:pass@ip:port
_PATTERN_PLAIN = re.compile(
    rf"^(?:(?P<scheme>https?)://)?"
    rf"(?:(?P<user>[^\s:@/]+):(?P<password>[^\s:@/]*)@)?"
    rf"(?P<host>{_HOST_PART}):(?P<port>\d{{1,5}})$"
)
# ip:port:user:pass
_PATTERN_QUAD = re.compile(
    rf"^(?:https?://)?(?P<host>{_HOST_PART}):(?P<port>\d{{1,5}})"
    rf":(?P<user>[^\s:]+):(?P<password>[^\s:]*)$"
)
_SPLIT_PATTERN = re.compile(r"[\s,;|]+")
_BARE_IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

_ssl_context: Optional[ssl.SSLContext] = None


# --------------------------------------------------------------------------- #
# データ構造
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProxyTarget:
    """検査対象のプロキシ1台。"""

    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def authenticated(self) -> bool:
        return bool(self.username)

    def __str__(self) -> str:  # pragma: no cover - 表示用
        return self.address


@dataclass
class ProxyResult:
    """1台ぶんの判定結果。"""

    target: ProxyTarget
    ok: bool
    latency_ms: Optional[float] = None
    connect_ms: Optional[float] = None
    exit_ip: Optional[str] = None
    country: Optional[str] = None
    country_code: Optional[str] = None
    https: Optional[bool] = None
    method: Optional[str] = None  # METHOD_HTTP / METHOD_CONNECT
    status: Optional[int] = None
    error: Optional[str] = None
    attempts: int = 1

    @property
    def address(self) -> str:
        return self.target.address


@dataclass(frozen=True)
class Judge:
    """判定用エンドポイントを分解したもの。"""

    url: str  # 絶対URI (HTTP中継でそのまま送る)
    host: str
    port: int
    host_header: str
    path: str  # トンネル内で送る origin-form
    secure: bool


# --------------------------------------------------------------------------- #
# 入力のパース
# --------------------------------------------------------------------------- #


def _build_judge(url: str, *, scheme: str, default_port: int) -> Judge:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("判定用URLが空です")
    parts = urlsplit(raw if "://" in raw else f"{scheme}://{raw}")
    if parts.scheme != scheme:
        raise ValueError(f"判定用URLは {scheme}:// で指定してください")
    if not parts.hostname:
        raise ValueError("判定用URLのホスト名を解釈できませんでした")

    port = parts.port or default_port
    path = f"{parts.path or '/'}{f'?{parts.query}' if parts.query else ''}"
    host_header = parts.hostname if port == default_port else f"{parts.hostname}:{port}"
    return Judge(
        url=f"{scheme}://{host_header}{path}",
        host=parts.hostname,
        port=port,
        host_header=host_header,
        path=path,
        secure=scheme == "https",
    )


def parse_judge(url: str) -> Judge:
    """HTTP中継で使う判定用URL (http:// のみ)。不正なら ValueError。"""
    return _build_judge(url, scheme="http", default_port=80)


def parse_https_judge(url: str) -> Judge:
    """CONNECT フォールバックで使う判定用URL (https:// のみ)。不正なら ValueError。"""
    return _build_judge(url, scheme="https", default_port=443)


def parse_proxy(token: str) -> Optional[ProxyTarget]:
    """1トークンを ProxyTarget に変換する。解釈できなければ None。"""
    text = (token or "").strip().strip("<>\"'").rstrip("/")
    if not text:
        return None

    match = _PATTERN_PLAIN.match(text) or _PATTERN_QUAD.match(text)
    if not match:
        return None

    try:
        port = int(match.group("port"))
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None

    groups = match.groupdict()
    user = groups.get("user") or None
    password = groups.get("password") if user else None
    return ProxyTarget(host=groups["host"], port=port, username=user, password=password)


def parse_proxy_list(text: str) -> tuple[list[ProxyTarget], list[str]]:
    """貼り付けられたテキストからプロキシ一覧を作る。

    改行 / カンマ / 空白 / セミコロン / パイプ 区切りに対応し、重複は除去する。
    戻り値は (解釈できた一覧, 解釈できなかったトークン一覧)。
    """
    targets: list[ProxyTarget] = []
    invalid: list[str] = []
    seen: set[tuple[str, int, Optional[str]]] = set()

    for token in _SPLIT_PATTERN.split((text or "").strip()):
        if not token:
            continue
        target = parse_proxy(token)
        if target is None:
            if len(invalid) < 20:
                invalid.append(token[:40])
            continue
        key = (target.host.lower(), target.port, target.username)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)

    return targets, invalid


# --------------------------------------------------------------------------- #
# HTTP を手書きする部分
# --------------------------------------------------------------------------- #


def _ssl_context_for_tunnel() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        context = ssl.create_default_context()
        try:
            context.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:  # pragma: no cover - 環境依存
            pass
        _ssl_context = context
    return _ssl_context


def _proxy_auth_header(target: ProxyTarget) -> str:
    if not target.username:
        return ""
    raw = f"{target.username}:{target.password or ''}".encode("utf-8")
    token = base64.b64encode(raw).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n"


def _common_headers() -> str:
    return (
        f"User-Agent: {_USER_AGENT}\r\n"
        "Accept: application/json, text/plain, */*\r\n"
        "Accept-Encoding: identity\r\n"
        "Cache-Control: no-cache\r\n"
    )


def _build_get_request(target: ProxyTarget, judge: Judge) -> bytes:
    """HTTPプロキシ向けの絶対URI形式リクエスト。"""
    head = (
        f"GET {judge.url} HTTP/1.1\r\n"
        f"Host: {judge.host_header}\r\n"
        f"{_common_headers()}"
        "Proxy-Connection: close\r\n"
        "Connection: close\r\n"
    )
    return (head + _proxy_auth_header(target) + "\r\n").encode("latin-1")


def _build_tunnel_request(judge: Judge) -> bytes:
    """CONNECT で張ったトンネルの中へ送る通常のリクエスト。"""
    head = (
        f"GET {judge.path} HTTP/1.1\r\n"
        f"Host: {judge.host_header}\r\n"
        f"{_common_headers()}"
        "Connection: close\r\n"
    )
    return (head + "\r\n").encode("latin-1")


def _build_connect_request(target: ProxyTarget, host: str, port: int) -> bytes:
    head = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"User-Agent: {_USER_AGENT}\r\n"
        "Proxy-Connection: keep-alive\r\n"
    )
    return (head + _proxy_auth_header(target) + "\r\n").encode("latin-1")


def _parse_status_line(line: str) -> int:
    parts = line.strip().split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("HTTPステータス行を解釈できませんでした")
    return int(parts[1])


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    """Content-Length / chunked / EOF のいずれにも対応して本文を読む。"""
    try:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            body = bytearray()
            while len(body) < MAX_BODY_BYTES:
                size_line = await reader.readuntil(b"\r\n")
                size_text = size_line.strip().split(b";")[0]
                try:
                    size = int(size_text, 16)
                except ValueError:
                    break
                if size == 0:
                    break
                body += await reader.readexactly(size)
                await reader.readexactly(2)  # チャンク末尾の CRLF
            return bytes(body[:MAX_BODY_BYTES])

        length = headers.get("content-length", "")
        if length.isdigit():
            return await reader.readexactly(min(int(length), MAX_BODY_BYTES))

        body = bytearray()
        while len(body) < MAX_BODY_BYTES:
            chunk = await reader.read(4096)
            if not chunk:
                break
            body += chunk
        return bytes(body)
    except asyncio.IncompleteReadError as exc:
        return bytes(exc.partial)


async def _read_response(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = _parse_status_line(lines[0])

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()

    return status, headers, await _read_body(reader, headers)


def _first_value(data: dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.split(",")[0].strip()
    return None


def _extract_geo(body: bytes) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """本文から (出口IP, 国名, 国コード) を取り出す。判定APIの形式差を吸収する。"""
    text = body.decode("utf-8", errors="replace").strip()

    data: Optional[dict[str, Any]] = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except (ValueError, TypeError):
        data = None

    if data is not None:
        return (
            _first_value(data, _IP_KEYS),
            _first_value(data, _COUNTRY_KEYS),
            _first_value(data, _COUNTRY_CODE_KEYS),
        )

    match = _BARE_IP_PATTERN.search(text)
    return (match.group(0) if match else None), None, None


def _status_reason(status: int) -> str:
    return {
        400: "不正な要求と判断された (400)",
        403: "アクセス拒否 (403)",
        405: "HTTP中継に非対応 (405)",
        407: "プロキシ認証が必要 (407)",
        429: "レート制限 (429)",
        501: "HTTP中継に非対応 (501)",
        502: "上位への接続に失敗 (502)",
        503: "プロキシが過負荷 (503)",
    }.get(status, f"HTTP {status}")


def _describe_error(exc: BaseException) -> str:
    """例外を短い日本語に落とす。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "タイムアウト"
    if isinstance(exc, ConnectionRefusedError):
        return "接続拒否"
    if isinstance(exc, ConnectionResetError):
        return "接続リセット"
    if isinstance(exc, socket.gaierror):
        return "名前解決に失敗"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "TLS証明書の検証に失敗"
    if isinstance(exc, ssl.SSLError):
        return "TLSハンドシェイクに失敗"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "応答が途切れた"
    if isinstance(exc, asyncio.LimitOverrunError):
        return "応答が大きすぎる"
    if isinstance(exc, ValueError):
        return "HTTP応答が不正"
    if isinstance(exc, OSError):
        return f"ネットワークエラー ({exc.strerror or exc.errno})"
    return f"{type(exc).__name__}"


async def _close(writer: Optional[asyncio.StreamWriter]) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 - 後片付けの失敗は無視して良い
        pass


# --------------------------------------------------------------------------- #
# 判定本体
# --------------------------------------------------------------------------- #


def _success(
    target: ProxyTarget,
    *,
    body: bytes,
    latency_ms: float,
    connect_ms: float,
    method: str,
) -> ProxyResult:
    exit_ip, country, country_code = _extract_geo(body)
    return ProxyResult(
        target=target,
        ok=True,
        latency_ms=latency_ms,
        connect_ms=connect_ms,
        exit_ip=exit_ip,
        country=country,
        country_code=country_code,
        method=method,
        status=200,
        https=True if method == METHOD_CONNECT else None,
    )


async def _attempt_http(target: ProxyTarget, judge: Judge) -> ProxyResult:
    """[1] 絶対URI の GET を投げる。ネットワーク例外は呼び出し側へ。"""
    started = time.perf_counter()
    writer: Optional[asyncio.StreamWriter] = None
    try:
        reader, writer = await asyncio.open_connection(
            target.host, target.port, limit=_STREAM_LIMIT
        )
        connect_ms = (time.perf_counter() - started) * 1000

        writer.write(_build_get_request(target, judge))
        await writer.drain()
        status, _headers, body = await _read_response(reader)
        latency_ms = (time.perf_counter() - started) * 1000
    finally:
        await _close(writer)

    if status == 200:
        return _success(
            target, body=body, latency_ms=latency_ms, connect_ms=connect_ms, method=METHOD_HTTP
        )
    return ProxyResult(
        target=target,
        ok=False,
        connect_ms=connect_ms,
        latency_ms=latency_ms,
        status=status,
        method=METHOD_HTTP,
        error=_status_reason(status),
    )


async def _attempt_connect(target: ProxyTarget, judge: Judge) -> ProxyResult:
    """[2] CONNECT でトンネルを張り、TLS 越しに判定先を読む。"""
    started = time.perf_counter()
    writer: Optional[asyncio.StreamWriter] = None
    try:
        reader, writer = await asyncio.open_connection(
            target.host, target.port, limit=_STREAM_LIMIT
        )
        connect_ms = (time.perf_counter() - started) * 1000

        writer.write(_build_connect_request(target, judge.host, judge.port))
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        status = _parse_status_line(head.decode("latin-1").split("\r\n")[0])
        if not 200 <= status < 300:
            return ProxyResult(
                target=target,
                ok=False,
                connect_ms=connect_ms,
                status=status,
                method=METHOD_CONNECT,
                error=f"CONNECT拒否 ({status})",
                https=False,
            )

        await writer.start_tls(_ssl_context_for_tunnel(), server_hostname=judge.host)
        writer.write(_build_tunnel_request(judge))
        await writer.drain()
        status, _headers, body = await _read_response(reader)
        latency_ms = (time.perf_counter() - started) * 1000
    finally:
        await _close(writer)

    if status == 200:
        return _success(
            target, body=body, latency_ms=latency_ms, connect_ms=connect_ms, method=METHOD_CONNECT
        )
    return ProxyResult(
        target=target,
        ok=False,
        connect_ms=connect_ms,
        latency_ms=latency_ms,
        status=status,
        method=METHOD_CONNECT,
        https=True,  # トンネル自体は張れている
        error=_status_reason(status),
    )


async def _connect_test(target: ProxyTarget, timeout: float) -> bool:
    """CONNECT メソッドで HTTPS トンネルが張れるかだけ確認する。"""
    writer: Optional[asyncio.StreamWriter] = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.host, target.port, limit=_STREAM_LIMIT),
            timeout=timeout,
        )
        writer.write(_build_connect_request(target, HTTPS_TEST_HOST, HTTPS_TEST_PORT))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=timeout)
        return 200 <= _parse_status_line(line.decode("latin-1")) < 300
    except Exception:  # noqa: BLE001 - 失敗は「非対応」として扱う
        return False
    finally:
        await _close(writer)


async def check_proxy(
    target: ProxyTarget,
    *,
    judge: Judge,
    https_judge: Optional[Judge] = None,
    timeout: float = 8.0,
    https_check: bool = True,
    retries: int = 0,
) -> ProxyResult:
    """1台を判定する。例外は投げず、必ず ProxyResult を返す。

    まず HTTP中継 (絶対URI) を試し、「中継は無理」と言われたら
    CONNECT + TLS へ切り替える。HTTPS専用プロキシもこれで判定できる。
    """
    attempts = max(1, retries + 1)
    fallback_ready = https_judge is not None and _START_TLS_SUPPORTED
    last_result: Optional[ProxyResult] = None
    last_error = "不明なエラー"

    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.wait_for(_attempt_http(target, judge), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - すべて結果として持ち帰る
            last_error = _describe_error(exc)
            last_result = None
            continue

        result.attempts = attempt
        if result.ok:
            if https_check:
                result.https = await _connect_test(target, timeout)
            return result

        # HTTP中継を拒否するプロキシ (HTTPS専用) は CONNECT で再挑戦する
        if fallback_ready and result.status in _PLAIN_REFUSED_STATUS:
            try:
                tunnel = await asyncio.wait_for(
                    _attempt_connect(target, https_judge), timeout=timeout  # type: ignore[arg-type]
                )
            except Exception as exc:  # noqa: BLE001
                tunnel = ProxyResult(
                    target=target, ok=False, method=METHOD_CONNECT, error=_describe_error(exc)
                )
            tunnel.attempts = attempt
            if tunnel.ok:
                return tunnel
            result.error = f"{result.error} / CONNECT: {tunnel.error}"

        last_result, last_error = result, result.error or last_error

    if last_result is not None:
        last_result.attempts = attempts
        return last_result
    return ProxyResult(target=target, ok=False, error=last_error, attempts=attempts)


async def check_many(
    targets: list[ProxyTarget],
    *,
    judge: Judge,
    https_judge: Optional[Judge] = None,
    timeout: float = 8.0,
    concurrency: int = 50,
    https_check: bool = True,
    retries: int = 0,
    on_result: Optional[Callable[[ProxyResult], None]] = None,
) -> list[ProxyResult]:
    """複数台を同時実行数の上限付きで判定する。戻り値は入力順。"""
    if not targets:
        return []

    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[Optional[ProxyResult]] = [None] * len(targets)

    async def worker(index: int, target: ProxyTarget) -> None:
        async with semaphore:
            result = await check_proxy(
                target,
                judge=judge,
                https_judge=https_judge,
                timeout=timeout,
                https_check=https_check,
                retries=retries,
            )
        results[index] = result
        if on_result is not None:
            try:
                on_result(result)
            except Exception:  # noqa: BLE001 - 進捗表示の失敗で検査は止めない
                pass

    await asyncio.gather(*(worker(i, t) for i, t in enumerate(targets)))
    return [r for r in results if r is not None]
