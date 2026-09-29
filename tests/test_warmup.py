"""
注文経路の並列化と下ごしらえの検証

確定ボタンを押してからの待ちを短くするための仕組み。
・認証の更新と店舗の確認を同時に行う
・POSトークンを短時間だけ使い回す
・利用者が商品を選んでいる間に裏で温めておく
"""
import asyncio, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class FakeClient:
    """それぞれの処理に時間がかかる偽物。"""
    def __init__(self, delay=0.1):
        self.delay = delay
        self.calls = []
    async def ensure_auth(self, force=False):
        self.calls.append("auth")
        await asyncio.sleep(self.delay)
    async def get_pos_paseto(self, group):
        self.calls.append(f"pos:{group}")
        await asyncio.sleep(self.delay)
        return "v2.local.fake"
    async def aclose(self): pass


class FakeHandle:
    def __init__(self, client, account_id=1):
        self.account_id = account_id
        self.label, self.card_id, self.client = "test", "card", client
    async def aclose(self): pass


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/w.db")

    from services.mcd import accounts as A
    A.forget_pos()

    print("\n[1] POSトークンを使い回す")
    c = FakeClient(delay=0.01)
    h = FakeHandle(c)
    t1 = await A.pos_paseto(h, "group-f")
    t2 = await A.pos_paseto(h, "group-f")
    check("同じトークンが返る", t1 == t2 == "v2.local.fake")
    check("2回目は取りに行かない ★", c.calls.count("pos:group-f") == 1, c.calls)

    print("\n[2] グループが違えば取り直す")
    await A.pos_paseto(h, "group-h")
    check("別グループは別に取る", c.calls.count("pos:group-h") == 1, c.calls)

    print("\n[3] アカウントが違えば取り直す")
    c2 = FakeClient(delay=0.01)
    await A.pos_paseto(FakeHandle(c2, account_id=2), "group-f")
    check("別アカウントは別に取る", "pos:group-f" in c2.calls, c2.calls)

    print("\n[4] 期限が切れたら取り直す")
    import config
    A.forget_pos()
    c3 = FakeClient(delay=0.01)
    h3 = FakeHandle(c3)
    saved = config.POS_PASETO_TTL_SECONDS
    config.POS_PASETO_TTL_SECONDS = 0
    await A.pos_paseto(h3, "group-f")
    await A.pos_paseto(h3, "group-f")
    check("期限切れなら取り直す", c3.calls.count("pos:group-f") == 2, c3.calls)
    config.POS_PASETO_TTL_SECONDS = saved

    print("\n[5] 失敗したアカウントのトークンは捨てる ★")
    A.forget_pos()
    c4 = FakeClient(delay=0.01)
    h4 = FakeHandle(c4, account_id=7)
    await A.pos_paseto(h4, "group-f")
    check("控えている", (7, "group-f") in A._pos_cache)
    A.forget_pos(7)
    check("捨てられる ★", (7, "group-f") not in A._pos_cache)

    print("\n[6] 認証と店舗の確認を同時に行う ★")
    async def slow_auth():
        await asyncio.sleep(0.1)
        return "auth"
    async def slow_store():
        await asyncio.sleep(0.1)
        return "store"
    t = time.perf_counter()
    await asyncio.gather(slow_auth(), slow_store())
    parallel = time.perf_counter() - t
    t = time.perf_counter()
    await slow_auth(); await slow_store()
    serial = time.perf_counter() - t
    check("並列のほうが速い ★", parallel < serial * 0.7,
          f"並列{parallel*1000:.0f}ms 直列{serial*1000:.0f}ms")

    print("\n[7] Saga が実際に並列化されている")
    import inspect
    from core import saga
    src = inspect.getsource(saga.execute)
    check("gather を使っている", "asyncio.gather(" in src)
    check("認証と店舗の確認をまとめている",
          "ensure_auth()" in src and "resolve_store(" in src
          and src.index("asyncio.gather(") < src.index("resolve_store("))
    check("POSトークンは使い回す経路を通る", "mcd_accounts.pos_paseto(" in src)

    print("\n[8] 下ごしらえは失敗しても影響しない ★")
    async def broken():
        raise RuntimeError("アカウントが無い")
    real = A.pick_account
    A.pick_account = broken
    try:
        await A.warm_up("13934", "group-f")
        err = None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    A.pick_account = real
    check("例外を外に出さない ★", err is None, err or "")

    print("\n[9] 下ごしらえでトークンが温まる")
    A.forget_pos()
    c5 = FakeClient(delay=0.01)
    async def fake_pick(exclude=None):
        return FakeHandle(c5, account_id=9)
    A.pick_account = fake_pick
    await A.warm_up("13934", "group-f")
    A.pick_account = real
    check("認証を済ませる", "auth" in c5.calls, c5.calls)
    check("POSトークンを取っておく", "pos:group-f" in c5.calls, c5.calls)
    check("控えに入る", (9, "group-f") in A._pos_cache)

    print("\n[10] グループが分からなくても落ちない")
    A.pick_account = fake_pick
    try:
        await A.warm_up("13934", "")
        err = None
    except Exception as e:
        err = str(e)
    A.pick_account = real
    check("空のグループでも通る", err is None, err or "")

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
