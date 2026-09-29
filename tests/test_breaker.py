"""
サーキットブレーカーの検証

壊れている相手に送り続けると全員がタイムアウトを待たされる。
一定回数失敗したらしばらく使わず、時間が経ったら様子見で戻す。
"""
import asyncio, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import breaker as B
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    print("\n[1] 失敗が続くと止める")
    b = B.Breaker("group-f", failures_to_open=3, cooldown=0.2, successes_to_close=2)
    check("はじめは通す", b.allows() and b.state == B.CLOSED)
    b.record_failure("timeout"); b.record_failure("timeout")
    check("2回では止めない", b.allows() and b.state == B.CLOSED, b.state)
    b.record_failure("timeout")
    check("3回で止まる", b.state == B.OPEN, b.state)
    check("通さなくなる", not b.allows())
    check("理由が残る", b.last_error == "timeout", b.last_error)
    check("止めた回数を数える", b.total_blocked >= 1, b.total_blocked)

    print("\n[2] 途中で成功したら数え直す")
    b2 = B.Breaker("x", failures_to_open=3)
    b2.record_failure(); b2.record_failure(); b2.record_success()
    check("連続の数がリセットされる", b2.consecutive_failures == 0)
    b2.record_failure(); b2.record_failure()
    check("2回失敗しても止まらない", b2.state == B.CLOSED, b2.state)

    print("\n[3] 時間が経てば様子見に移る")
    time.sleep(0.25)
    check("冷却後は通す", b.allows())
    check("様子見になる", b.state == B.HALF, b.state)

    print("\n[4] 様子見で失敗したらまた止める")
    b.record_failure("まだ駄目")
    check("止まる", b.state == B.OPEN, b.state)
    check("冷却がやり直しになる", b.retry_after > 0.1, b.retry_after)

    print("\n[5] 様子見で通れば元に戻る")
    time.sleep(0.25)
    b.allows()
    b.record_success()
    check("1回では戻さない", b.state == B.HALF, b.state)
    b.record_success()
    check("2回通れば元に戻る", b.state == B.CLOSED, b.state)
    check("また通せる", b.allows())

    print("\n[6] 例外で伝える")
    b3 = B.Breaker("y", failures_to_open=1, cooldown=10)
    b3.record_failure("落ちている")
    try:
        b3.check()
        err = None
    except B.CircuitOpen as e:
        err = e
    check("CircuitOpen を投げる", err is not None)
    check("あと何秒かが分かる", err and err.retry_after > 5, err.retry_after if err else None)
    check("文面に相手の名前が入る", err and "y" in str(err), str(err) if err else "")

    print("\n[7] 名前ごとに独立している")
    r = B.Registry()
    for _ in range(B.FAILURES_TO_OPEN):
        r.record_failure("group-a", "x")
    check("止めた相手は通さない", not r.allows("group-a"))
    check("別の相手は通す", r.allows("group-b"))
    check("正常な相手を一覧できる", "group-b" in r.healthy(), r.healthy())
    check("止めている相手を一覧できる",
          [b.name for b in r.blocked()] == ["group-a"], [b.name for b in r.blocked()])
    r.reset_all()
    check("まとめて戻せる", r.allows("group-a"))

    print("\n[8] 壊れた配信元を飛ばして別を試す ★")
    import httpx
    from services.mcd.client import Fingerprint, McdClient
    B.groups.reset_all()
    tried = []
    class FakeHTTP:
        async def get(self, url, headers=None):
            g = url.split("data.cat.")[1].split(".")[0]
            tried.append(g)
            if g != "group-f":
                raise httpx.ConnectError("落ちている")
            return httpx.Response(200, json={"store": {"name": "テスト店"}},
                                  request=httpx.Request("GET", url))
        async def aclose(self): pass
    c = McdClient(Fingerprint.generate())
    await c.aclose()
    c._client = FakeHTTP()
    data, group, _ = await c.fetch_store("13934")
    check("生きている配信元を見つける", group == "group-f", group)
    first = len(tried)
    tried.clear()
    # 壊れた配信元を規定回数まで失敗させる
    for _ in range(B.FAILURES_TO_OPEN):
        try:
            await c.fetch_store("99999")
        except Exception:
            pass
    tried.clear()
    await c.fetch_store("13934")
    check("止めた配信元にはもう送らない ★",
          all(g == "group-f" for g in tried), tried)
    check("試す数が減る", len(tried) < first, (len(tried), first))

    print("\n[9] 全部止まったら分かる文面を出す")
    B.groups.reset_all()
    class AllDown:
        async def get(self, url, headers=None):
            raise httpx.ConnectError("全部落ちている")
        async def aclose(self): pass
    c._client = AllDown()
    for _ in range(B.FAILURES_TO_OPEN):
        for _g in range(6):
            try:
                await c.fetch_store("13934")
            except Exception:
                pass
    try:
        await c.fetch_store("13934")
        msg = ""
    except Exception as e:
        msg = str(e)
    check("接続できない旨を伝える", "接続できません" in msg or "見つかりません" in msg, msg)

    print("\n[10] 使えないアカウントを避けて選ぶ ★")
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/br.db")
    from db.models import McdAccount
    from services.mcd import accounts as A
    B.accounts.reset_all()
    async with session_scope() as s:
        for i in (1, 2):
            s.add(McdAccount(
                id=i, label=f"acc{i}", email_enc=b"x", status="ACTIVE", card_id="c",
                device_uid="d", wmop_device_id="w", fb_instance_id="f",
                home_lat=35.0, home_lng=139.0,
            ))
    for _ in range(B.FAILURES_TO_OPEN):
        B.accounts.record_failure("mcd:1", "決済に失敗")
    h = await A.pick_account()
    check("止めたアカウントは選ばない ★", h.account_id == 2, h.account_id)
    await h.aclose()
    for _ in range(B.FAILURES_TO_OPEN):
        B.accounts.record_failure("mcd:2", "決済に失敗")
    h = await A.pick_account()
    check("全部止まっていても、諦めずに一番近いものを試す",
          h.account_id in (1, 2), h.account_id)
    await h.aclose()

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
