"""
数量のまとめ方と、選択枠の中間ノード

実機で見つかった2件を固定する。

  ① 同じ商品を3つ並べて送ったら、1個ぶんしか注文されなかった
     （¥300の商品を3つで、相手の金額は¥300）
  ② 朝マックのセットが通らなかった
     ドリンク枠が通常セット（9997918）と別コード（9997925）で、
     中間ノードが付いていなかった
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


def shape(item):
    return (str(item.product_code), item.quantity,
            [shape(c) for c in (item.components or [])])


def main():
    from services.mcd.protocol import (
        OrderItem, build_hex, decode_hex, merge_items,
    )
    from services.mcd.menu import parse_menu
    from services.mcd import slot_bridge
    from ui.menu_flows import build_order_item

    print("── ① 同じ商品は数量にまとめる ──")
    a = OrderItem(product_code="2160", quantity=1, amount=300)
    got = merge_items([a, a, a])
    check("3つ並べたら1つ×3になる ★", [shape(x) for x in got] == [("2160", 3, [])],
          [shape(x) for x in got])
    check("1つなら何も変えない", [shape(x) for x in merge_items([a])] == [("2160", 1, [])])
    check("空でも落ちない", merge_items([]) == [])

    b = OrderItem(product_code="1010", quantity=1, amount=170)
    c = OrderItem(product_code="1010", quantity=1, amount=170,
                  components=[OrderItem(product_code="9901", quantity=0)])
    got = merge_items([b, c, b])
    check("中身が違えばまとめない ★（ピクルス抜きは別の品）",
          [shape(x) for x in got]
          == [("1010", 2, []), ("1010", 1, [("9901", 0, [])])],
          [shape(x) for x in got])

    d = OrderItem(product_code="2160", quantity=2, amount=300)
    check("すでに数量があるものも足せる",
          [shape(x) for x in merge_items([a, d])] == [("2160", 3, [])],
          [shape(x) for x in merge_items([a, d])])
    check("順番は保つ",
          [x.product_code for x in merge_items([b, a, b])] == ["1010", "2160"])

    print("\n── 送るところで必ずまとまる ──")
    dec = decode_hex(build_hex("10528", [a, a, a], "takeOut"))
    check("注文コードに3つ並ばない ★", len(dec.items) == 1, len(dec.items))
    check("数量が3になっている ★", dec.items[0].quantity == 3, dec.items[0].quantity)

    from services.mcd.protocol import build_store_order_body, DecodedOrder
    body = build_store_order_body(
        DecodedOrder(store_id="10528", pickup_method="takeOut", items=[a, a, a]),
        pos_paseto="",
    )
    check("注文の送信本体でもまとまる ★", body.hex().count("120432313630") == 1,
          body.hex().count("120432313630"))

    print("\n── ② 選択枠の中間ノード ──")
    menu = parse_menu("13934", json.load(open(f"{HERE}/m13934.json")))
    known = slot_bridge.all_known()
    check("実データで分かっているのは2本", sorted(known) == ["9997918", "9997925"],
          known)

    def slot_of(code, slot_code):
        p = menu.products[code]
        return p, next(s for s in p.slots_of("choices") if s.code == slot_code)

    p, drink = slot_of("9075", "9997918")
    check("通常セットのドリンク枠（実データ）", menu.bridge_for(drink) == "9997914")

    p2, drink2 = slot_of("9030", "9997925")
    check("朝マックのドリンク枠（実データ）★", menu.bridge_for(drink2) == "9997922",
          menu.bridge_for(drink2))
    check("値に規則は無い（推測で埋めない）★",
          menu.bridge_for(drink) != menu.bridge_for(drink2),
          (menu.bridge_for(drink), menu.bridge_for(drink2)))
    p3, side = slot_of("9030", "9987010")
    check("サイド枠には付けない ★（中間ノードの要らない形）",
          menu.bridge_for(side) == "", menu.bridge_for(side))

    print("\n── 朝マックのセットを組み立てる ──")
    class Cart:
        pass
    cart = Cart(); cart.menu = menu; cart.pickup = "takeOut"

    for code, name in (("9030", "メガマフィン セット"), ("9029", "月見マフィン セット")):
        prod = menu.products[code]
        picks = {s.code: (s.default_product or menu.slot_reference(s))
                 for s in prod.slots_of("choices")}
        item = build_order_item(cart, prod, picks)
        tree = shape(item)
        drinks = [c for c in item.components if c.product_code == "9997925"]
        check(f"{name}: ドリンク枠がある", len(drinks) == 1, tree)
        if drinks:
            inner = drinks[0].components
            check(f"{name}: 中間ノードが入っている ★",
                  len(inner) == 1 and inner[0].product_code == "9997922",
                  shape(drinks[0]))
            check(f"{name}: その下に商品がある ★",
                  bool(inner and inner[0].components), shape(drinks[0]))
        sides = [c for c in item.components if c.product_code == "9987010"]
        check(f"{name}: サイド枠は中間ノード無しで商品が直下 ★",
              bool(sides) and bool(sides[0].components)
              and not sides[0].components[0].components, shape(sides[0]) if sides else None)

    print("\n── 通常セットを壊していないか ──")
    prod = menu.products["9075"]
    picks = {s.code: (s.default_product or menu.slot_reference(s))
             for s in prod.slots_of("choices")}
    item = build_order_item(cart, prod, picks)
    drink = next(c for c in item.components if c.product_code == "9997918")
    check("通常セットのドリンクはこれまでどおり",
          shape(drink)[2][0][0] == "9997914", shape(drink))

    print("\n── ③ 実物の注文コードと突き合わせる ★ ──")
    # mcdon.asia で公式画面から取った本物。これと1バイトでも違えば、
    # こちらの組み立て方が間違っている。
    from services.mcd.protocol import (
        build_item, decode_hex, pb_msg, proto_parse,
    )

    REAL_SET = (
        "0a0531333933341a6b2a690a2168747470733a2f2f6d63646f6e2e617369612f6d6f70"
        "2f31333933342f61757468122168747470733a2f2f6d63646f6e2e617369612f6d6f70"
        "2f31333933342f617574681a2168747470733a2f2f6d63646f6e2e617369612f6d6f70"
        "2f31333933342f617574683a020a00424e124c120439303330180120a8052a17080112"
        "073939383730313018012a0812043530313018012a260801120739393937393235180"
        "12a17080112073939393739323218012a081204333137301801"
    )
    REAL_X3 = (
        "0a0531333933341a6b2a690a2168747470733a2f2f6d63646f6e2e617369612f6d6f70"
        "2f31333933342f61757468122168747470733a2f2f6d63646f6e2e617369612f6d6f70"
        "2f31333933342f617574681a2168747470733a2f2f6d63646f6e2e617369612f6d6f70"
        "2f31333933342f617574683a020a00420d120b120432303831180320b602"
    )

    def items_hex(h):
        raw = bytes.fromhex(h)
        return next(v for v in proto_parse(raw).get(8, []) if isinstance(v, bytes)).hex()

    for label, h in (("朝マックのセット", REAL_SET), ("同じ商品×3", REAL_X3)):
        dec = decode_hex(h)
        mine = b"".join(
            pb_msg(2, build_item(i, top_level=True)) for i in dec.items
        ).hex()
        check(f"{label}: 読んで組み直すと1バイトも変わらない ★",
              mine == items_hex(h), (mine[:80], items_hex(h)[:80]))

    real_set = decode_hex(REAL_SET).items[0]
    check("実物の構造: セット → 枠 → 中間 → 商品 ★",
          shape(real_set) == ("9030", 1, [
              ("9987010", 1, [("5010", 1, [])]),
              ("9997925", 1, [("9997922", 1, [("3170", 1, [])])]),
          ]), shape(real_set))

    prod = menu.products["9030"]
    mine = build_order_item(cart, prod, {"9987010": "5010", "9997925": "3170"})
    check("カートから組み立てた形が実物と同じ ★", shape(mine) == shape(real_set),
          (shape(mine), shape(real_set)))
    check("金額も同じ（¥680）★", mine.amount == real_set.amount == 680,
          (mine.amount, real_set.amount))

    real_x3 = decode_hex(REAL_X3).items[0]
    check("実物も数量で表す（×3）★", shape(real_x3) == ("2081", 3, []),
          shape(real_x3))
    check("金額欄は **単価** ★（合計ではない）",
          real_x3.amount == menu.products["2081"].price_for("takeOut") == 310,
          (real_x3.amount, menu.products["2081"].price_for("takeOut")))

    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
