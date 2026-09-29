"""
相関IDと通信計測の検証

1回の注文に紐づくログを絞り込めること、相手先ごとの応答時間と
失敗率が正しく貯まることを確かめる。
"""
import asyncio, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import telemetry as T

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    print("\n[1] 相関ID")
    check("最初は空", T.current() == "")
    with T.Correlation("注文") as cid:
        check("中では設定される", T.current() == cid, T.current())
        check("見分けやすい形", cid.startswith("注文-") and len(cid) < 20, cid)
    check("抜けると元に戻る", T.current() == "")

    print("\n[2] 入れ子でも壊れない")
    with T.Correlation("外"):
        outer = T.current()
        with T.Correlation("内"):
            check("内側が優先される", T.current() != outer)
        check("内側を抜けると外側に戻る", T.current() == outer)

    print("\n[3] 例外で抜けても戻る")
    @T.traced("注文")
    async def boom():
        raise RuntimeError("失敗")
    try:
        await boom()
    except RuntimeError:
        pass
    check("相関IDが残らない", T.current() == "")

    print("\n[4] 並行して動いても混ざらない")
    seen = {}
    @T.traced("注文")
    async def work(key):
        seen[key] = T.current()
        await asyncio.sleep(0.01)
        return T.current() == seen[key]
    results = await asyncio.gather(*[work(i) for i in range(5)])
    check("それぞれ自分のIDを保つ", all(results))
    check("IDが全部ちがう", len(set(seen.values())) == 5, seen)

    print("\n[5] ログに載る")
    records = []
    class Catch(logging.Handler):
        def emit(self, record): records.append(self.format(record))
    h = Catch()
    h.setFormatter(logging.Formatter("%(corr)s%(message)s"))
    h.addFilter(T.CorrelationFilter())
    lg = logging.getLogger("test.corr")
    lg.addHandler(h); lg.setLevel(logging.INFO); lg.propagate = False
    lg.info("相関なし")
    with T.Correlation("注文", value="注文-abc123"):
        lg.info("相関あり")
    check("相関IDが付く", any("[注文-abc123] 相関あり" == r for r in records), records)
    check("無いときは何も付けない", any(r == "相関なし" for r in records), records)

    print("\n[6] 計測")
    T.metrics.reset()
    for ms, status in [(100, 200), (200, 200), (300, 200), (50, 500), (80, 404)]:
        T.metrics.record("data.cat/group-f", ms=ms, status=status)
    T.metrics.record("ord/group-f", ms=900, error="ConnectTimeout")
    st = T.metrics.get("data.cat/group-f")
    check("件数を数える", st.total == 5, st.total)
    check("5xxは失敗に数える", st.failures == 1, st.failures)
    check("4xxは失敗に数えない（相手は動いている）",
          st.statuses.get(404) == 1 and st.failures == 1, st.statuses)
    check("中央値が出る", 100 <= st.p50 <= 300, st.p50)
    check("95%値が出る", st.p95 >= st.p50, (st.p50, st.p95))
    other = T.metrics.get("ord/group-f")
    check("例外も記録する", other.failures == 1 and "Timeout" in other.last_error,
          other.last_error)
    check("全体の件数", T.metrics.total_requests == 6, T.metrics.total_requests)

    print("\n[7] 相手先の名前を短くまとめる")
    for url, want in [
        ("https://data.cat.group-f.prod.mop.mcd.qorcommerce.com/13934.json", "data.cat/group-f"),
        ("https://ord.group-j.prod.mop.mcd.qorcommerce.com/app/x", "ord/group-j"),
        ("https://user-api.dir.prod.mop.mcd.qorcommerce.com/x", "user-api"),
        ("https://api.kyash.me/v1/me", "api"),
    ]:
        got = T.short_host(url)
        check(f"{want} にまとまる", got == want, got)

    print("\n[8] 計測しながら通信できる")
    from core.http import build_async_client
    T.metrics.reset()
    async with build_async_client(timeout=20.0) as c:
        try:
            r = await c.get("https://data.cat.group-f.prod.mop.mcd.qorcommerce.com/13934.json")
            status = r.status_code
        except Exception as e:
            status = str(e)
    check("通信できる", status == 200, status)
    check("記録が残る", T.metrics.total_requests == 1, T.metrics.total_requests)
    st = T.metrics.get("data.cat/group-f")
    check("応答時間が入る", st is not None and st.p50 and st.p50 > 0, st.p50 if st else None)

    print("\n[9] 記録は上限を超えて増えない")
    T.metrics.reset()
    for i in range(T.WINDOW + 200):
        T.metrics.record("x", ms=i, status=200)
    st = T.metrics.get("x")
    check("応答時間の保持は上限まで", len(st.durations) == T.WINDOW, len(st.durations))
    check("件数の合計は正しい", st.total == T.WINDOW + 200, st.total)

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
