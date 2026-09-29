"""
名前解決の先読みと控え

接続のたびにDNSを引くと、毎回20〜60ミリ秒かかる。
店舗同期のように一度に何百件も取りに行く場面では無視できない。

さらに大きいのは**DNSが引けなくなったとき**。
相手のサーバーは生きているのに、名前が引けないだけで
BOT全体が止まってしまう。前回引けた結果を覚えておけば、
その間も動き続けられる。

そこで `socket.getaddrinfo` に控えを挟む。

  ・成功した結果を一定時間おぼえる
  ・引けなかったときは、期限切れでも**前回の結果を使う**
  ・起動時に、使う相手をまとめて引いておく

⚠️ 標準ライブラリの関数を差し替えるため、影響範囲を絞ってある。
   控えの件数には上限を設け、`uninstall()` で元に戻せる。
"""

from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger("bot.dns")

# 控えを持つ時間（秒）
TTL = 300.0
# 控えの上限。BOTが使う相手は十数件なので、これで十分すぎる。
MAX_ENTRIES = 128

_original = None
_lock = threading.Lock()
# キー → (期限, 結果)
_cache: dict[tuple, tuple[float, list]] = {}
_stats = {"hit": 0, "miss": 0, "stale": 0, "fail": 0}


def known_hosts() -> list[str]:
    """先に引いておく相手。"""
    import config

    hosts = [
        "authorization-vmob-prod-jpe.vmobapps.com",
        "con-japan-east-prod.vmobapps.com",
        "user-api.dir.prod.mop.mcd.qorcommerce.com",
        "pay.dir.prod.mop.mcd.qorcommerce.com",
        "api.kyash.me",
        "kyash.me",
    ]
    for group in config.MCD_GROUPS:
        hosts.append(f"data.cat.{group}.prod.mop.mcd.qorcommerce.com")
        hosts.append(f"ord.{group}.prod.mop.mcd.qorcommerce.com")
    return hosts


def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    key = (host, port, family, type, proto, flags)
    now = time.monotonic()

    with _lock:
        entry = _cache.get(key)
    if entry and entry[0] > now:
        _stats["hit"] += 1
        return entry[1]

    try:
        result = _original(host, port, family, type, proto, flags)
    except socket.gaierror:
        # 引けなかった。期限切れでも前の結果があるなら、それを使う。
        # 相手は生きているのに名前が引けないだけ、ということがあるため。
        if entry:
            _stats["stale"] += 1
            log.warning("%s の名前を引けないため、前回の結果を使います", host)
            return entry[1]
        _stats["fail"] += 1
        raise

    _stats["miss"] += 1
    with _lock:
        if len(_cache) >= MAX_ENTRIES:
            # 期限の早いものから捨てる
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (now + TTL, result)
    return result


def install() -> bool:
    """控えを有効にする。すでに有効なら何もしない。"""
    global _original
    if _original is not None:
        return False
    _original = socket.getaddrinfo
    socket.getaddrinfo = _cached_getaddrinfo
    log.info("名前解決の控えを有効にしました（%.0f秒）", TTL)
    return True


def uninstall() -> None:
    """元に戻す。"""
    global _original
    if _original is None:
        return
    socket.getaddrinfo = _original
    _original = None
    clear()


def clear() -> None:
    with _lock:
        _cache.clear()


def stats() -> dict:
    with _lock:
        return {**_stats, "entries": len(_cache)}


async def prewarm() -> int:
    """
    使う相手をまとめて引いておく。

    起動時に一度呼ぶ。最初の注文で名前解決を待たずに済む。
    引けなかった相手があっても止めない（そのときに引き直せばよい）。
    """
    import asyncio

    install()
    hosts = known_hosts()

    async def one(host: str) -> bool:
        try:
            await asyncio.to_thread(
                socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM
            )
            return True
        except Exception as e:
            log.info("%s の名前を引けませんでした: %s", host, e)
            return False

    results = await asyncio.gather(*[one(h) for h in hosts])
    done = sum(1 for r in results if r)
    log.info("名前解決を先に済ませました: %d / %d 件", done, len(hosts))
    return done
