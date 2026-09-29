"""メニュー解析の検証 — 南砂町店(13934)の実データ247商品で確認"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.mcd.menu import (
    parse_menu, parse_dayparts, supported_pickup_methods, diff_menus, minutes_of,
)
from datetime import datetime

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))
menu_raw = json.load(open(f"{HERE}/m13934.json"))
store_raw = json.load(open(f"{HERE}/s13934.json"))
m = parse_menu("13934", menu_raw)

print("\n[1] 商品の読み込み")
check(f"247商品（実際{len(m.products)}）", len(m.products) == 247, len(m.products))
unnamed = [p.code for p in m.products.values() if p.name.startswith("商品 ")]
check(f"全商品に名前がある（無名 {len(unnamed)}件）", not unnamed, unnamed[:5])
check("カテゴリ10件", len(m.collections) == 10, len(m.collections))

print("\n[2] 実HEXの商品と一致するか")
p = m.products["9180"]
check("9180 = 月見バーガー セット", p.name == "月見バーガー セット", p.name)
check("VALUE_MEAL", p.product_class == "VALUE_MEAL", p.product_class)
check("セット価格 800円 (prePrice)", p.pre_price == 800, p.pre_price)
check("2020 = マックフライポテト® M", m.products["2020"].name == "マックフライポテト® M", m.products["2020"].name)
check("3120 = コカ・コーラ M", m.products["3120"].name == "コカ・コーラ M", m.products["3120"].name)

print("\n[3] 金額の再計算がHEXと一致するか（改ざん検知の要）")
burger = p.price_eatin
extras = sum(s.extra_price for s in p.slots_of("choices"))
check(f"340 + 370 + 90 = 800（実際 {burger}+{extras}={burger+extras}）", burger + extras == 800, f"{burger}+{extras}")
check("price_for(eatIn) が 800", p.price_for("eatIn") == 800, p.price_for("eatIn"))
check("選択枠が2つ", len(p.slots_of("choices")) == 2, len(p.slots_of("choices")))
check("サイド枠の既定が 2020", p.slots_of("choices")[0].default_product == "2020")

print("\n[4] 受取方法で価格が変わるか")
single = m.products["2110"]  # ホットアップルパイ
check(f"{single.name} 店内={single.price_eatin} 持帰={single.price_takeout} 配達={single.price_other}",
      single.price_other > single.price_eatin, f"{single.price_eatin}/{single.price_other}")
check("price_for(addressDelivery) が配達価格", single.price_for("addressDelivery") == single.price_other)

print("\n[5] 時間帯による販売可否")
with_windows = [p for p in m.products.values() if p.time_windows]
check(f"時間帯が定義された商品 {len(with_windows)}件", len(with_windows) > 0)
if with_windows:
    t = with_windows[0]
    w = t.time_windows[0]
    check(f"{t.name}: {w['start']//60:02d}:{w['start']%60:02d}〜 注文可",
          t.is_orderable_at(w["start"] + 1))
    check("時間外は注文不可", not t.is_orderable_at(w["start"] - 10) or w["start"] == 0)
breakfast = [p for p in m.products.values() if p.day_part == "BREAKFAST_MENU"]
check(f"朝マック限定商品 {len(breakfast)}件", len(breakfast) > 0)

print("\n[6] 店舗の時間帯定義")
dp = parse_dayparts(store_raw)
names = {d.name for d in dp}
check("朝マック/レギュラー/ヒルマック/夜マックを取得",
      {"DAYPART_BREAKFAST","DAYPART_REGULAR","DAYPART_HIRU_MAC","DAYPART_YORU_MAC"} <= names, names)
yoru = next(d for d in dp if d.name == "DAYPART_YORU_MAC")
check("夜マックは17:00に有効", yoru.active_at(17*60))
check("夜マックは12:00には無効", not yoru.active_at(12*60))

print("\n[7] 店舗が対応する受取方法")
sup = supported_pickup_methods(store_raw)
check("テイクアウト対応", sup["takeOut"])
check("店内カウンター対応", sup["eatIn"])
check("席まで対応", sup["tableDelivery"])
check("駐車場は非対応（選択肢に出さない）", not sup["curbsidePickUp"], sup)
check("mcDelivery→addressDelivery の対応付け", sup["addressDelivery"] is True, sup)

print("\n[8] 差分検出（新商品・値上げの通知）")
old = [("9180","月見バーガー セット",340), ("2110","ホットアップルパイ",120), ("0000","終売品",100)]
d = diff_menus(old, m)
check(f"新商品 {len(d.added)}件", len(d.added) == 245, len(d.added))
check("終売を検出", ("0000","終売品") in d.removed)
check("値上げを検出", any(c[0]=="2110" and c[2]==120 for c in d.price_changed), d.price_changed[:3])

print("\n[9] カテゴリからの商品一覧")
burgers = next(c for c in m.collections if c.name == "バーガー")
lst = m.visible_products(burgers.id)
check(f"バーガー {len(lst)}件", len(lst) > 20, len(lst))
check("全部に名前がある", all(not p.name.startswith("商品 ") for p in lst))

print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
sys.exit(1 if fail else 0)
