"""
複数個必須の選択枠

チキンマックナゲット15ピースのソース枠（7251）は **3個必須**
（min=max=default=3）。1個しか送っていなかったため注文できなかった。

ここでは
  ・必要な個数がそのまま送られること
  ・種類を分けて選べること（実際のアプリと同じ）
  ・足りない分は選んだものを増やして埋めること
  ・枠 / 中間ノード / 入れ子 の3つの形を取り違えないこと
を確かめる。
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))


def tree(item):
    """(商品コード, 個数, [中身]) に畳む。"""
    return (
        str(item.product_code), item.quantity,
        [tree(c) for c in (item.components or [])],
    )


def find(item, code):
    if str(item.product_code) == code:
        return item
    for c in item.components or []:
        got = find(c, code)
        if got is not None:
            return got
    return None


def main():
    from services.mcd.menu import Slot, parse_menu
    from services.mcd import slot_rules, slot_bridge
    from ui.menu_flows import build_order_item, picked_codes, spread_quantity

    print("── Slot.need / Slot.multi ──")
    one = Slot(kind="choices", code="9987009")
    three = Slot(kind="choices", code="7251",
                 min_quantity=3, max_quantity=3, default_quantity=3)
    check("ふつうの枠は1個", one.need == 1 and not one.multi, one.need)
    check("3個必須の枠は3個", three.need == 3 and three.multi, three.need)
    check("既定0でも1個に丸める",
          Slot(kind="choices", code="x", default_quantity=0).need == 1)
    check("min だけ大きい枠も拾う",
          Slot(kind="choices", code="x", min_quantity=2).need == 2)

    print("\n── picked_codes ──")
    check("空は空", picked_codes(None) == [] and picked_codes("") == [])
    check("1個", picked_codes("6048") == ["6048"])
    check("複数", picked_codes("6048,6049,6050") == ["6048", "6049", "6050"])
    check("余分なコンマを落とす", picked_codes("6048,,6049") == ["6048", "6049"])

    print("\n── spread_quantity ──")
    check("1種類で3個 → その1種類を3個",
          spread_quantity(["6048"], 3) == [("6048", 3)])
    check("3種類で3個 → 1個ずつ",
          spread_quantity(["6048", "6049", "6050"], 3)
          == [("6048", 1), ("6049", 1), ("6050", 1)])
    check("2種類で3個 → 2個と1個",
          spread_quantity(["6048", "6049"], 3) == [("6048", 2), ("6049", 1)])
    check("空なら何も作らない", spread_quantity([], 3) == [])
    check("重複は1つにまとめる",
          spread_quantity(["6048", "6048"], 3) == [("6048", 3)])
    check("選んだ数が必要数を超えても全部送る",
          spread_quantity(["a", "b", "c", "d"], 3)
          == [("a", 1), ("b", 1), ("c", 1), ("d", 1)])
    check("合計が必要数に一致する（3種類）",
          sum(n for _, n in spread_quantity(["a", "b", "c"], 3)) == 3)
    check("合計が必要数に一致する（1種類）",
          sum(n for _, n in spread_quantity(["a"], 3)) == 3)

    print("\n── 実データ：チキンマックナゲット15ピース ──")
    menu = parse_menu("13934", json.load(open(f"{HERE}/m13934.json")))
    nug = menu.products["1670"]
    sauce = [s for s in nug.slots_of("choices") if s.code == "7251"]
    check("ソース枠がある", len(sauce) == 1)
    check("ソース枠は3個必須", sauce and sauce[0].need == 3, sauce and sauce[0].need)

    class Cart:
        pass
    cart = Cart(); cart.menu = menu; cart.pickup = "takeOut"

    it = build_order_item(cart, nug, {"7251": "6048"})
    slot = find(it, "7251")
    check("枠そのものが3個で送られる", slot is not None and slot.quantity == 3,
          slot and slot.quantity)
    check("1種類だけ選んだらソースも3個",
          slot and [tree(c) for c in slot.components] == [("6048", 3, [])],
          slot and [tree(c) for c in slot.components])

    it3 = build_order_item(cart, nug, {"7251": "6048,6049,6050"})
    slot3 = find(it3, "7251")
    check("3種類選んだら1個ずつ",
          slot3 and [tree(c) for c in slot3.components]
          == [("6048", 1, []), ("6049", 1, []), ("6050", 1, [])],
          slot3 and [tree(c) for c in slot3.components])
    check("3種類でも枠は3個", slot3 and slot3.quantity == 3)
    check("ソースの合計は必ず3個",
          slot3 and sum(c.quantity for c in slot3.components) == 3)

    print("\n── 実データ：ポテナゲ（入れ子の枠）──")
    pot = menu.products["9094"]
    nested = menu.nested_choices(pot)
    check("ナゲットのソース枠を入れ子として見つける",
          [s.code for _c, s in nested] == ["7251"], [s.code for _c, s in nested])
    pit = build_order_item(cart, pot, {"1610/7251": "6048"})
    inner = find(pit, "7251")
    check("入れ子でもソース枠が送られる", inner is not None)
    check("入れ子のソースも個数どおり",
          inner and sum(c.quantity for c in inner.components) == inner.quantity,
          inner and (inner.quantity, [tree(c) for c in inner.components]))

    print("\n── choices_of：3つの形を取り違えない ──")
    check("枠 → 商品（ナゲット単品）",
          slot_rules.choices_of(it) == [("7251", "6048")],
          slot_rules.choices_of(it))
    check("枠 → 商品（3種類）",
          slot_rules.choices_of(it3)
          == [("7251", "6048"), ("7251", "6049"), ("7251", "6050")],
          slot_rules.choices_of(it3))
    got = slot_rules.choices_of(pit)
    check("構成品 → 枠 → 商品（ポテナゲ）は枠を返す",
          ("7251", "6048") in got, got)
    check("構成品のコードを枠として誤報告しない",
          not any(s == "1610" for s, _ in got), got)

    bridged = None
    for p in menu.products.values():
        if any(s.code == "9997918" for s in p.slots_of("choices")):
            picks = {s.code: (s.default_product or s.reference_product)
                     for s in p.slots_of("choices")}
            bridged = build_order_item(cart, p, picks)
            break
    check("中間ノードのあるセットを見つけた", bridged is not None)
    if bridged is not None:
        pairs = dict(slot_rules.choices_of(bridged))
        check("枠 → 中間 → 商品 は外側の枠を返す",
              "9997918" in pairs and pairs["9997918"] not in
              set(slot_bridge.all_known().values()), pairs)
        check("中間ノードを枠として報告しない",
              not any(s in set(slot_bridge.all_known().values())
                      for s in pairs), pairs)

    print("\n── 具材の調整は選択枠ではない ──")
    from services.mcd.protocol import OrderItem
    plain = OrderItem(product_code="1", quantity=1, components=[
        OrderItem(product_code="9901", quantity=0),      # ピクルス抜き
    ])
    check("中身のない節は枠として数えない", slot_rules.choices_of(plain) == [],
          slot_rules.choices_of(plain))

    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
