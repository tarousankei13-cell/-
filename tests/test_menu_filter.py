"""
注文できない商品を表示しないことの検証

実データで見つかった問題:
  ・`checkoutable: []` の商品を「制限なし」と読んで、いつでも表示していた
    （この店舗では扱っていない、という意味だった）
  ・時間帯が終わったカテゴリ（夜の「朝マック」）をそのまま出していた
"""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from services.mcd import availability as av
from services.mcd.menu import parse_menu

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

DATE = "2026-10-03"


def catalog():
    def prod(code):
        return {"productCode": code, "productClass": "PRODUCT",
                "priceList": [{"priceCode": "TAKEOUT", "price": 100}],
                "composition": [], "choices": [], "canAdds": []}
    products = {c: prod(c) for c in
                ["1010", "2010", "3120", "9229", "7777", "5050"]}
    names = {"1010": "ハンバーガー", "2010": "マックフライポテト®",
             "3120": "コカ・コーラ", "9229": "ひるまック 募金付きセット",
             "7777": "登録の無い商品", "5050": "朝マックの商品"}
    ability = {
        "1010": [{"start": 620, "end": 1430}],     # 昼以降
        "2010": [{"start": 360, "end": 1430}],     # 終日
        "3120": [{"start": 360, "end": 1430}],     # 終日
        "9229": [],                                 # ★扱っていない
        "5050": [{"start": 360, "end": 620}],      # 朝のみ
        # 7777 はわざと登録しない
    }
    return {
        "products": products,
        "groupMenu": {"products": {c: {"productCode": c, "tName": {"ja": n}}
                                   for c, n in names.items()}},
        "collections": [
            {"id": "1", "tName": {"ja": "バーガー"}, "productCodes": ["1010", "9229"]},
            {"id": "6", "tName": {"ja": "朝マック"}, "productCodes": ["5050", "2010"]},
            {"id": "7", "tName": {"ja": "夜マック"}, "productCodes": ["1010"]},
            {"id": "4", "tName": {"ja": "ドリンク"}, "productCodes": ["3120"]},
        ],
        "sizeVariants": {},
        "limitedAbility": {
            DATE: {"ability": {
                c: {"productCode": c, "visible": [{"start": 360, "end": 1430}],
                    "checkoutable": w}
                for c, w in ability.items()
            }}
        },
    }


def store_raw(hiru=False):
    return {
        "store": {"id": "13934", "mopEnabled": True, "foeStatus": "NORMAL",
                  "deliveryMethod": {"takeOut": {"isSupported": True}}},
        "mopDaypartAbilityLists": {DATE: {"daypartAbilities": [
            {"daypart": "DAYPART_BREAKFAST", "checkoutable": [{"start": 360, "end": 620}]},
            {"daypart": "DAYPART_REGULAR", "checkoutable": [{"start": 620, "end": 1430}]},
            {"daypart": "DAYPART_HIRU_MAC",
             "checkoutable": [{"start": 620, "end": 1020}] if hiru else []},
            {"daypart": "DAYPART_YORU_MAC", "checkoutable": [{"start": 1020, "end": 1430}]},
        ]}},
    }


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/mf.db")

    m = parse_menu("13934", catalog())

    print("\n[1] 時間帯の読み分け ★")
    check("終日の商品は None ではない", m.products["2010"].time_windows is not None)
    check("扱っていない商品は空 ★", m.products["9229"].time_windows == [],
          m.products["9229"].time_windows)
    check("登録の無い商品は None ★", m.products["7777"].time_windows is None,
          m.products["7777"].time_windows)

    print("\n[2] 扱っていない商品は常に出さない ★")
    p = m.products["9229"]
    check("扱っていないと判定する ★", p.never_orderable)
    for minutes in (0, 410, 720, 1300, 1439):
        check(f"{minutes//60}時台でも注文不可 ★", not p.is_orderable_at(minutes))

    print("\n[3] 登録の無い商品は止めない")
    p = m.products["7777"]
    check("いつでも注文できる扱い", p.is_orderable_at(410) and p.is_orderable_at(1300))
    check("扱っていない扱いにはしない", not p.never_orderable)

    print("\n[4] 一覧に出てこない ★")
    for minutes, label in [(410, "朝"), (720, "昼"), (1300, "夜")]:
        shown = [p.code for p in m.visible_products("1", minutes)]
        check(f"{label}: 扱っていない商品が出ない ★", "9229" not in shown, shown)
    check("昼はハンバーガーが出る", "1010" in [p.code for p in m.visible_products("1", 720)])
    check("朝はハンバーガーが出ない", "1010" not in [p.code for p in m.visible_products("1", 410)])

    print("\n[5] 時間帯が終わったカテゴリを出さない ★")
    raw = store_raw()
    for minutes, label, want in [
        (410, "朝6:50", {"バーガー", "朝マック", "ドリンク"}),
        (720, "昼12:00", {"バーガー", "ドリンク"}),
        (1300, "夜21:40", {"バーガー", "夜マック", "ドリンク"}),
    ]:
        active = av.active_dayparts(raw, DATE, minutes)
        shown = {
            c.name for c in m.collections
            if av.collection_available(c.name, active)
        }
        check(f"{label}: {sorted(want)} ★", shown == want, sorted(shown))

    print("\n[6] 扱っていない時間帯のカテゴリは常に出さない ★")
    raw_no_hiru = store_raw(hiru=False)
    for minutes in (410, 720, 1300):
        active = av.active_dayparts(raw_no_hiru, DATE, minutes)
        check(f"{minutes//60}時: ヒルマックを出さない ★",
              not av.collection_available("ヒルマック", active), sorted(active))
    raw_hiru = store_raw(hiru=True)
    active = av.active_dayparts(raw_hiru, DATE, 720)
    check("扱う店舗では昼に出す", av.collection_available("ヒルマック", active),
          sorted(active))

    print("\n[7] 時間帯に結びつかないカテゴリは常に出す")
    for name in ("バーガー", "ドリンク", "おすすめ", "サイドメニュー"):
        check(f"{name} は常に出す", av.collection_available(name, set()))
        check(f"{name} は時間帯があっても出す",
              av.collection_available(name, {"DAYPART_REGULAR"}))

    print("\n[8] 時間帯が分からなければ止めない")
    check("情報が無ければ朝マックも出す", av.collection_available("朝マック", set()))

    print("\n[9] 保存して読み戻しても区別が残る ★")
    from db.models import MenuProduct, StoreCache, StoreDaypart
    async with session_scope() as s:
        s.add(StoreCache(store_id="13934", group_name="group-f", store_name="テスト店",
                         address="東京都", cat_root_url="x"))
        for p in m.products.values():
            s.add(MenuProduct(
                store_id="13934", product_code=p.code, name_ja=p.name,
                product_class=p.product_class,
                structure=p.structure_json(),
                time_windows=(None if p.time_windows is None
                              else json.dumps(p.time_windows)),
            ))
        for a in store_raw()["mopDaypartAbilityLists"][DATE]["daypartAbilities"]:
            s.add(StoreDaypart(store_id="13934", daypart=a["daypart"], date=DATE,
                               visible="[]",
                               checkoutable=json.dumps(a["checkoutable"])))
    from services.mcd import stores as S
    back = await S.load_menu("13934")
    check("扱っていない商品は空のまま ★", back.products["9229"].time_windows == [],
          back.products["9229"].time_windows)
    check("登録の無い商品は None のまま ★", back.products["7777"].time_windows is None,
          back.products["7777"].time_windows)
    check("読み戻しても出てこない ★",
          not back.products["9229"].is_orderable_at(720))

    print("\n[10] 保存済みの情報から時間帯を引ける")
    active = await av.active_dayparts_for("13934", minutes=410, date_key=DATE)
    check("朝は BREAKFAST", "DAYPART_BREAKFAST" in active, sorted(active))
    active = await av.active_dayparts_for("13934", minutes=1300, date_key=DATE)
    check("夜は YORU_MAC", "DAYPART_YORU_MAC" in active, sorted(active))
    check("夜に BREAKFAST は入らない", "DAYPART_BREAKFAST" not in active, sorted(active))
    active = await av.active_dayparts_for("99999", minutes=720, date_key=DATE)
    check("知らない店舗では空", active == set(), active)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
