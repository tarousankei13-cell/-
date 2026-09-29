"""
メニュー同期の検証

店舗ごとの同期は互いに関係が無いので同時に行う。
順番に待つと店舗が増えたぶんだけ時間がかかる。
ETag が効くので、変更が無ければ通信量はゼロで済む。
"""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/ms.db")
    from core import settings
    await settings.load_all()

    from db.models import StoreCache
    from services import tasks as jobs
    from services.mcd import accounts as A, stores as S

    STORES = [f"1000{i}" for i in range(12)]
    async with session_scope() as s:
        for sid in STORES:
            s.add(StoreCache(store_id=sid, group_name="group-f", store_name=f"店{sid}",
                             address="東京都", cat_root_url="https://x.invalid"))

    print("\n[1] 店舗ごとに同時に同期する ★")
    DELAY = 0.05
    live = {"now": 0, "max": 0}
    calls = []

    class FakeClient:
        async def aclose(self): pass

    class FakeHandle:
        account_id, label, card_id = 1, "t", "c"
        client = FakeClient()
        async def aclose(self): pass

    async def fake_resolve(client, store_id, force=False):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(DELAY)
        live["now"] -= 1
        calls.append(store_id)
        return S.StoreInfo(store_id=store_id, group="group-f", name=f"店{store_id}",
                           address="", latitude=0, longitude=0,
                           cat_root_url="https://x.invalid", ord_root_url="",
                           delivery_methods={"takeOut": True})

    async def fake_sync(client, store_id, *, store=None, force=False):
        await asyncio.sleep(DELAY)
        from services.mcd.menu import MenuDiff
        return MenuDiff()

    async def fake_pick(exclude=None): return FakeHandle()

    real = (A.pick_account, S.resolve_store, S.sync_menu, S.active_store_ids)
    A.pick_account = fake_pick
    jobs.mcd_accounts.pick_account = fake_pick
    S.resolve_store = fake_resolve
    S.sync_menu = fake_sync
    jobs.mcd_stores.resolve_store = fake_resolve
    jobs.mcd_stores.sync_menu = fake_sync

    t = time.perf_counter()
    out = await jobs.sync_all_menus()
    elapsed = time.perf_counter() - t

    check("全店舗を同期する", len(out) == len(STORES), len(out))
    check("同時に動いている ★", live["max"] > 1, live["max"])
    check(f"同時数の上限を守る（{config.MENU_SYNC_CONCURRENCY}）",
          live["max"] <= config.MENU_SYNC_CONCURRENCY, live["max"])
    serial = len(STORES) * DELAY * 2
    check("順番に待つより速い ★", elapsed < serial * 0.6,
          f"実測{elapsed:.2f}秒 / 直列なら{serial:.2f}秒")

    print("\n[2] 1店舗が失敗しても他は続く ★")
    async def flaky_resolve(client, store_id, force=False):
        if store_id == "10003":
            raise RuntimeError("この店舗だけ落ちている")
        return await fake_resolve(client, store_id, force)
    S.resolve_store = flaky_resolve
    jobs.mcd_stores.resolve_store = flaky_resolve
    out = await jobs.sync_all_menus()
    check("失敗した店舗以外は同期される ★", len(out) == len(STORES) - 1, len(out))
    check("失敗した店舗は結果に入らない", "10003" not in out)

    print("\n[3] アカウントが取れなければ何もしない")
    async def no_account(exclude=None):
        raise RuntimeError("アカウントが無い")
    jobs.mcd_accounts.pick_account = no_account
    out = await jobs.sync_all_menus()
    check("例外を外に出さない", isinstance(out, dict), type(out).__name__)
    jobs.mcd_accounts.pick_account = fake_pick

    print("\n[4] 対象が無ければ通信しない")
    calls.clear()
    async def none_active(days=None): return []
    jobs.mcd_stores.active_store_ids = none_active
    out = await jobs.sync_all_menus()
    check("空を返す", out == {}, out)
    check("取りに行かない", calls == [], calls)
    jobs.mcd_stores.active_store_ids = real[3]

    A.pick_account, S.resolve_store, S.sync_menu, S.active_store_ids = real

    print("\n[5] 照合の間隔を設定で変えられる")
    from services.mcd import store_sync
    await settings.set_value("store_sitemap_check_minutes", 180, updated_by=1)
    check("設定が読める",
          settings.get("store_sitemap_check_minutes") == 180,
          settings.get("store_sitemap_check_minutes"))
    import inspect
    src = inspect.getsource(store_sync.sync)
    check("同期処理が設定を見る", "store_sitemap_check_minutes" in src)

    print("\n[6] ETagで変更が無ければ取り直さない")
    src = inspect.getsource(S.sync_menu)
    check("ETagを送る", "etag=etag" in src)
    check("304なら解析しない", "raw is None" in src and "return MenuDiff()" in src)
    check("商品が空ならETagを無視する", "has_products" in src)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
