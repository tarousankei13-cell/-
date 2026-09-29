"""
名前解決の控えの検証

一番大事なのは「**DNSが引けなくなっても動き続ける**」こと。
相手のサーバーは生きているのに、名前が引けないだけで止まっては困る。
"""
import asyncio, os, socket, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import dns

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

FAKE = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]


async def main():
    dns.uninstall()
    dns.clear()
    for k in dns._stats:
        dns._stats[k] = 0

    calls = []
    def fake_resolve(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        if host == "落ちている.example":
            raise socket.gaierror("引けません")
        return FAKE

    print("\n[1] 控えが効く")
    dns.install()
    dns._original = fake_resolve          # 実際のDNSは使わない
    socket.getaddrinfo("example.test", 443)
    socket.getaddrinfo("example.test", 443)
    socket.getaddrinfo("example.test", 443)
    check("2回目以降は引きに行かない", len(calls) == 1, calls)
    check("同じ結果を返す", socket.getaddrinfo("example.test", 443) == FAKE)
    check("控えで済んだ回数を数える", dns.stats()["hit"] >= 3, dns.stats())

    print("\n[2] 引数が違えば別扱い")
    calls.clear()
    socket.getaddrinfo("example.test", 80)
    check("ポートが違えば引き直す", len(calls) == 1, calls)

    print("\n[3] DNSが引けなくなっても前の結果で動く ★")
    calls.clear()
    socket.getaddrinfo("あとで落ちる.test", 443)
    # 以降は必ず失敗するようにする
    def always_fail(*a, **k):
        calls.append("失敗")
        raise socket.gaierror("引けません")
    dns._original = always_fail
    dns.TTL_backup = dns.TTL
    # 期限を切らせる
    key = ("あとで落ちる.test", 443, 0, 0, 0, 0)
    exp, val = dns._cache[key]
    dns._cache[key] = (0.0, val)
    got = socket.getaddrinfo("あとで落ちる.test", 443)
    check("期限切れでも前回の結果を返す ★", got == FAKE, got)
    check("引き直そうとはする", "失敗" in calls, calls)
    check("その回数を数える", dns.stats()["stale"] >= 1, dns.stats())

    print("\n[4] 一度も引けていない相手は素直に失敗する")
    try:
        socket.getaddrinfo("知らない.test", 443)
        err = None
    except socket.gaierror as e:
        err = str(e)
    check("例外になる", err is not None, err)
    check("失敗の回数を数える", dns.stats()["fail"] >= 1, dns.stats())

    print("\n[5] 控えが無限に増えない")
    dns._original = fake_resolve
    dns.clear()
    for i in range(dns.MAX_ENTRIES + 30):
        socket.getaddrinfo(f"h{i}.test", 443)
    check("上限で止まる", dns.stats()["entries"] <= dns.MAX_ENTRIES, dns.stats())

    print("\n[6] 元に戻せる")
    dns._original = fake_resolve
    dns.uninstall()
    check("標準の関数に戻る", socket.getaddrinfo is not dns._cached_getaddrinfo)
    check("控えも消える", dns.stats()["entries"] == 0, dns.stats())
    check("二重に戻しても落ちない", dns.uninstall() is None)

    print("\n[7] 二重に有効にしない")
    check("1回目は有効になる", dns.install() is True)
    check("2回目は何もしない", dns.install() is False)
    dns.uninstall()

    print("\n[8] 先読みする相手")
    hosts = dns.known_hosts()
    check("配信元が全グループぶん入る",
          sum(1 for h in hosts if h.startswith("data.cat.")) == 6,
          [h for h in hosts if h.startswith("data.cat.")])
    check("注文の送り先も入る", any(h.startswith("ord.") for h in hosts))
    check("Kyashも入る", any("kyash" in h for h in hosts))
    check("重複が無い", len(hosts) == len(set(hosts)))

    print("\n[9] 実際に先読みできる")
    n = await dns.prewarm()
    check("引けた相手がある", n > 0, n)
    check("控えに入る", dns.stats()["entries"] > 0, dns.stats())
    import time
    t = time.perf_counter()
    socket.getaddrinfo("api.kyash.me", 443, 0, socket.SOCK_STREAM)
    elapsed = (time.perf_counter() - t) * 1000
    check("2回目は速い（1ms未満）", elapsed < 1.0, f"{elapsed:.2f}ms")
    dns.uninstall()

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
