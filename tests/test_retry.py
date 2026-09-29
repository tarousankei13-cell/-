"""
再送の方針の検証

一番大事なのは「**送り直してはいけない要求を送り直さない**」こと。
注文の登録や支払いの確定を再送すると、二重注文・二重課金になる。
"""
import asyncio, os, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from core import retry
from core.retry import Idempotency as I

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    # 待ち時間を詰めてテストを速くする
    retry.BASE_DELAY = 0.001
    retry.MAX_DELAY = 0.01

    print("\n[1] 再送してよいもの")
    n = [0]
    async def flaky():
        n[0] += 1
        if n[0] < 3:
            raise RuntimeError("一時的")
        return "ok"
    check("SAFE は成功するまで送り直す",
          await retry.call(flaky, idempotency=I.SAFE) == "ok" and n[0] == 3, n[0])
    n[0] = 0
    check("KEYED も送り直す",
          await retry.call(flaky, idempotency=I.KEYED) == "ok" and n[0] == 3, n[0])

    print("\n[2] 再送してはいけないもの ★")
    n[0] = 0
    try:
        await retry.call(flaky, idempotency=I.ONCE)
        err = None
    except RuntimeError as e:
        err = str(e)
    check("ONCE は1回しか呼ばない ★", n[0] == 1, n[0])
    check("失敗はそのまま投げる", err == "一時的", err)
    n[0] = 0
    await retry.call(flaky, idempotency=I.ONCE, attempts=10) if False else None
    try:
        await retry.call(flaky, idempotency=I.ONCE, attempts=10)
    except RuntimeError:
        pass
    check("attempts を増やしても ONCE は1回のまま ★", n[0] == 1, n[0])

    print("\n[3] 待ち時間")
    check("回を追うごとに伸びる",
          retry.backoff_delay(1, jitter=0) < retry.backoff_delay(2, jitter=0)
          < retry.backoff_delay(3, jitter=0))
    check("上限を超えない",
          retry.backoff_delay(20, base=0.4, cap=6.0, jitter=0) == 6.0,
          retry.backoff_delay(20, base=0.4, cap=6.0, jitter=0))
    check("負にならない", all(retry.backoff_delay(i) >= 0 for i in range(1, 10)))

    print("\n[4] ばらつきがある（同時に再送して相手を叩かない）")
    xs = [retry.backoff_delay(3, base=1.0, cap=10.0) for _ in range(200)]
    check("値が散らばる", statistics.pstdev(xs) > 0.05, statistics.pstdev(xs))
    check("平均は元の待ち時間の近く", 3.0 < statistics.fmean(xs) < 5.0, statistics.fmean(xs))
    check("重なる値ばかりではない", len(set(round(x, 4) for x in xs)) > 150)

    print("\n[5] 諦める条件")
    class Fatal(Exception): pass
    n[0] = 0
    async def fatal():
        n[0] += 1
        raise Fatal("直らない")
    try:
        await retry.call(fatal, idempotency=I.SAFE, give_up_on=(Fatal,))
    except Fatal:
        pass
    check("諦める例外は送り直さない", n[0] == 1, n[0])
    n[0] = 0
    try:
        await retry.call(fatal, idempotency=I.SAFE, retry_on=(ValueError,))
    except Fatal:
        pass
    check("対象外の例外も送り直さない", n[0] == 1, n[0])

    print("\n[6] 最後まで失敗したら最後の例外を投げる")
    async def always():
        raise ValueError("ずっと失敗")
    try:
        await retry.call(always, idempotency=I.SAFE, attempts=3)
        got = None
    except ValueError as e:
        got = str(e)
    check("元の例外がそのまま出る", got == "ずっと失敗", got)

    print("\n[7] 成功したら送り直さない")
    n[0] = 0
    async def fine():
        n[0] += 1
        return 1
    await retry.call(fine, idempotency=I.SAFE, attempts=5)
    check("1回で済む", n[0] == 1, n[0])

    print("\n[8] クライアントの方針が正しく設定されている ★")
    import inspect
    from services.mcd import client as C
    src = inspect.getsource(C.McdClient)
    def region(name, size=1400):
        i = src.index(f"async def {name}")
        return src[i:i + size]
    check("注文の登録は ONCE ★", "Idempotency.ONCE" in region("store_order"))
    check("支払いの確定は ONCE ★", "Idempotency.ONCE" in region("authorise_order"))
    check("注文状態の確認は SAFE", "Idempotency.SAFE" in region("get_paid_order"))
    check("店舗情報の取得は SAFE", "Idempotency.SAFE" in region("fetch_store"))
    sig = inspect.signature(C.McdClient._qor_post)
    check("既定は安全側（ONCE）★",
          sig.parameters["idempotency"].default is I.ONCE,
          sig.parameters["idempotency"].default)

    print("\n[9] 決済の確定が再送されないことを実際に確かめる ★")
    calls = []
    class FakeHTTP:
        async def post(self, url, headers=None, content=None):
            calls.append(url)
            raise httpx.ConnectError("切断された")
        async def get(self, url, headers=None): raise httpx.ConnectError("x")
        async def aclose(self): pass
    from services.mcd.client import Fingerprint, McdClient, McdNetworkError
    c = McdClient(Fingerprint.generate())
    await c.aclose()
    c._client = FakeHTTP()
    c.tokens.root_paseto = "v2.local.x"
    c.tokens.root_exp = 9e18
    c.tokens.access_token = "a"
    c.tokens.access_exp = 9e18
    # 確定の直前にトークンを取り直す処理は、この検証では邪魔なので止める
    async def noop(*a, **k): pass
    c.ensure_auth = noop
    try:
        await c.authorise_order("group-f", "token")
    except Exception:
        pass
    check("支払いの確定は1回しか送られない ★", len(calls) == 1, len(calls))

    calls.clear()
    try:
        await c.get_paid_order("group-f", "token")
    except Exception:
        pass
    check("注文状態の確認は送り直す", len(calls) >= 2, len(calls))

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
