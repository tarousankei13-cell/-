"""カート組み立ての検証 — 実メニューから注文コードを作り、正しく読み戻せるか"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.mcd.menu import parse_menu
from services.mcd.protocol import build_hex, decode_hex, OrderItem

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))
menu = parse_menu("13934", json.load(open(f"{HERE}/m13934.json")))

class FakeCart:
    """CartView のうち build_order_item が使う部分だけを真似たもの"""
    def __init__(self, pickup="eatIn"):
        self.pickup = pickup
        self.menu = menu
        self.items = []
        self.store_id = "13934"
        self.purpose = "order"
        self.owner_id = 1

from ui.menu_flows import build_order_item

print("\n[1] セット商品（月見バーガー セット）を組み立てる")
cart = FakeCart("eatIn")
p = menu.products["9180"]
item = build_order_item(cart, p, {})          # 既定の選択で組む
check("商品コード 9180", item.product_code == "9180", item.product_code)
check("金額 800円（セットのprePrice）", item.amount == 800, item.amount)
codes = [x.product_code for x in item.walk()]
check("固定構成 1566 が入る", "1566" in codes, codes)
check("サイド枠 9987009 が入る", "9987009" in codes, codes)
check("既定のポテトM(2020)が入る", "2020" in codes, codes)

print("\n[2] 選択枠を変えられる")
choices = p.slots_of("choices")
side = choices[0]
# ポテトL(2050)へ差し替え
item2 = build_order_item(cart, p, {side.code: "2050"})
codes2 = [x.product_code for x in item2.walk()]
check("差し替えた 2050 が入る", "2050" in codes2, codes2)
check("既定の 2020 は入らない", "2020" not in codes2, codes2)

print("\n[3] 受取方法で価格が変わる")
cart_takeout = FakeCart("takeOut")
single = menu.products["2110"]               # ホットアップルパイ
i_eat = build_order_item(FakeCart("eatIn"), single, {})
i_take = build_order_item(cart_takeout, single, {})
i_dlv = build_order_item(FakeCart("addressDelivery"), single, {})
check(f"店内 {i_eat.amount} / 持帰 {i_take.amount} / 配達 {i_dlv.amount}",
      i_dlv.amount > i_eat.amount, f"{i_eat.amount}/{i_dlv.amount}")

print("\n[4] 注文コードを作って読み戻す")
cart.items = [item, build_order_item(cart, menu.products["3110"], {})]
total = sum(i.amount for i in cart.items)
hex_str = build_hex(cart.store_id, cart.items, "eatIn")
d = decode_hex(hex_str)
check("店舗ID 13934", d.store_id == "13934")
check("受取方法 eatIn", d.pickup_method == "eatIn", d.pickup_method)
check(f"商品2点 / 合計{total}円", len(d.items) == 2 and d.total_amount == total,
      f"{len(d.items)}点 {d.total_amount}円")
check("1品目の構成が保たれる",
      [x.product_code for x in d.items[0].walk()] == [x.product_code for x in item.walk()])

print("\n[5] 商品名がすべて引ける（Discordの表示用）")
names = []
for it in d.items:
    for leaf in it.walk():
        p2 = menu.products.get(leaf.product_code)
        if p2: names.append(p2.name)
check(f"{len(names)}件の名前を解決", len(names) >= 2, names)
check("「M」だけの名前が無い（サイズ親を辿れている）",
      all(n not in ("M", "S", "L") for n in names), names)

print("\n[6] 全カテゴリの商品が組み立てられる")
errors = []
built = 0
for col in menu.collections:
    for p3 in menu.visible_products(col.id)[:8]:
        if p3.product_class not in ("PRODUCT", "VALUE_MEAL"): continue
        try:
            it = build_order_item(cart, p3, {})
            h = build_hex("13934", [it], "takeOut")
            dd = decode_hex(h)
            assert dd.items[0].product_code == p3.code
            built += 1
        except Exception as e:
            errors.append(f"{p3.code} {p3.name}: {e}")
check(f"{built}品をコード化して読み戻せた", not errors, errors[:3])

print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
sys.exit(1 if fail else 0)
