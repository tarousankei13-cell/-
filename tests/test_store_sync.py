"""
店舗一覧の定期同期の検証

新店舗の追加・閉店・店名変更・モバイルオーダー可否の切り替えが
自動で反映されるか、通信をモックして確かめる。
"""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
import config
from services.mcd import store_index, store_sync

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


# ---- 偽の通信 ------------------------------------------------

class FakeResp:
    def __init__(self, status, body="", etag="", text=""):
        self.status_code = status
        self._body = body
        self.text = text or (json.dumps(body) if body else "")
        self.headers = {"etag": etag} if etag else {}
    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")


class World:
    """マクドナルド側の状態。テストから書き換えて変化を起こす。"""
    def __init__(self):
        # 店舗ID → (グループ, 店名, 住所, mop, etag)
        self.stores = {
            "01003": ("group-h", "西町店", "北海道札幌市西区", True, "v1"),
            "13934": ("group-f", "南砂町店", "東京都江東区", True, "v1"),
            "43518": ("group-i", "熊本下通店", "熊本県熊本市", True, "v1"),
        }
        self.sitemap_extra = []   # サイトマップにだけ載せるID
        self.requests = []        # 何を何回取りに行ったか

    def sitemap(self):
        ids = list(self.stores) + self.sitemap_extra
        return "".join(f"<loc>https://map.mcdonalds.co.jp/map/{i}</loc>" for i in ids)


class FakeClient:
    def __init__(self, world): self.w = world
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, headers=None, timeout=None):
        headers = headers or {}
        self.w.requests.append(url)
        if "sitemap" in url:
            return FakeResp(200, text=self.w.sitemap())
        # https://data.cat.<group>.../<id>.json
        group = url.split("data.cat.")[1].split(".")[0]
        sid = url.rsplit("/", 1)[1].removesuffix(".json")
        row = self.w.stores.get(sid)
        if row is None or row[0] != group:
            return FakeResp(404)
        g, name, addr, mop, etag = row
        if headers.get("If-None-Match") == etag:
            return FakeResp(304)
        return FakeResp(200, {"store": {
            "name": name, "address": addr, "mopEnabled": mop}}, etag=etag)


def install(world, tmp: Path):
    store_index.SYNCED_PATH = tmp / "stores.json"
    store_index.BUNDLED_PATH = tmp / "bundled.json"
    # 通信は core.http.build_async_client で作るので、そこを差し替える
    store_sync.build_async_client = lambda **kw: FakeClient(world)
    store_sync.SITEMAP_URLS = ["https://map.mcdonalds.co.jp/sitemap.xml"]


async def main():
    tmp = Path(tempfile.mkdtemp())
    w = World()
    install(w, tmp)
    # 同梱の一覧は空（初回は同期で作られる）
    store_index.BUNDLED_PATH.write_text("{}", encoding="utf-8")

    print("\n[1] 初回の同期")
    r = await store_sync.sync()
    check("3店舗を取り込む", r.total == 3, r.total)
    check("すべて新規として記録される", len(r.added) == 3, list(r.added))
    check("インデックスに読み込まれる", store_index.count() == 3, store_index.count())
    check("店名で検索できる", [e.store_id for e in store_index.search("熊本")] == ["43518"])
    check("グループも記録される",
          json.loads(store_index.SYNCED_PATH.read_text())["stores"]["43518"]["g"] == "group-i")

    print("\n[2] 変化が無いときは304で済む")
    w.requests.clear()
    r = await store_sync.sync()
    check("追加も削除も無い", not r.changed, store_sync.format_report(r))
    check("店舗数は変わらない", r.total == 3, r.total)
    check("1店舗につき1回しか取りに行かない",
          len([u for u in w.requests if "data.cat" in u]) == 3,
          len([u for u in w.requests if "data.cat" in u]))

    print("\n[3] 新店舗が開店した")
    w.stores["27818"] = ("group-j", "梅田堂島店", "大阪府大阪市北区", True, "v1")
    _force_sitemap_check()
    r = await store_sync.sync()
    check("新店舗が追加される", r.added.get("27818") == "梅田堂島店", r.added)
    check("合計4店舗になる", r.total == 4, r.total)
    check("すぐ検索できる", [e.store_id for e in store_index.search("梅田")] == ["27818"])
    check("通知文に載る", "梅田堂島店" in store_sync.format_report(r))

    print("\n[4] 店名が変わった")
    w.stores["13934"] = ("group-f", "南砂町SUNAMO店", "東京都江東区", True, "v2")
    r = await store_sync.sync(batch=10)
    check("改名が記録される",
          any(i == "13934" and o == "南砂町店" and n == "南砂町SUNAMO店"
              for i, o, n in r.renamed), r.renamed)
    check("新しい名前で検索できる",
          [e.store_id for e in store_index.search("SUNAMO")] == ["13934"])
    check("通知文に載る", "南砂町SUNAMO店" in store_sync.format_report(r))

    print("\n[5] モバイルオーダーを停止した")
    w.stores["01003"] = ("group-h", "西町店", "北海道札幌市西区", False, "v3")
    r = await store_sync.sync(batch=10)
    check("停止が記録される", r.mop_off.get("01003") == "西町店", r.mop_off)
    check("通知文に載る", "停止した店舗" in store_sync.format_report(r))

    print("\n[6] 閉店した（サイトマップから消えた）")
    w.stores.pop("43518")
    _force_sitemap_check()
    r = await store_sync.sync()
    check("一覧から外れる", r.removed.get("43518") == "熊本下通店", r.removed)
    check("検索に出なくなる", store_index.search("熊本下通") == [])
    check("合計3店舗になる", r.total == 3, r.total)

    print("\n[7] 配信元に無いIDは間隔をあけて再挑戦する")
    w.sitemap_extra = ["99999"]
    _force_sitemap_check()
    r = await store_sync.sync()
    meta = json.loads(store_index.SYNCED_PATH.read_text())["meta"]
    check("控えに記録される", "99999" in (meta.get("unresolved") or {}), meta.get("unresolved"))
    check("一覧には入らない", store_index.get("99999") is None)
    _force_sitemap_check()
    w.requests.clear()
    await store_sync.sync()
    check("次回は取りに行かない",
          not [u for u in w.requests if "99999" in u],
          [u for u in w.requests if "99999" in u])

    print("\n[8] 通信が切れても一覧を壊さない")
    class Broken(FakeClient):
        async def get(self, url, headers=None, timeout=None):
            if "sitemap" in url: raise RuntimeError("接続できません")
            return await super().get(url, headers, timeout)
    store_sync.build_async_client = lambda **kw: Broken(w)
    _force_sitemap_check()
    r = await store_sync.sync()
    check("エラーを記録する", bool(r.error), r.error)
    check("店舗は消えない", r.total == 3, r.total)
    check("検索は続けられる", len(store_index.search("梅田")) == 1)
    store_sync.build_async_client = lambda **kw: FakeClient(w)

    print("\n[9] 一覧が壊れていたら同梱の一覧から作り直す")
    store_index.SYNCED_PATH.write_text("これはJSONではない", encoding="utf-8")
    store_index.BUNDLED_PATH.write_text(json.dumps(
        {"13934": {"n": "南砂町店", "a": "東京都", "g": "group-f", "mop": True}}
    ), encoding="utf-8")
    _force_sitemap_check(missing_ok=True)
    r = await store_sync.sync()
    check("作り直せる", r.total >= 1, r.total)
    check("検索できる状態に戻る", store_index.available())

    print("\n[10] ゼロ埋めの店舗ID")
    check("01003 で引ける", store_index.get("01003") is not None)
    check("先頭の0を省いた 1003 でも引ける", store_index.get("1003") is not None)

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


def _force_sitemap_check(missing_ok=False):
    """照合の間隔を待たずに、次回の同期でサイトマップを見させる。"""
    p = store_index.SYNCED_PATH
    if not p.exists():
        if missing_ok: return
        raise AssertionError("同期済みの一覧がありません")
    try:
        d = json.loads(p.read_text())
    except ValueError:
        if missing_ok: return
        raise
    d.setdefault("meta", {})["sitemap_at"] = 0
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
