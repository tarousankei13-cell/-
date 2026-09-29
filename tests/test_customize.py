"""
具材の調整（ピクルス抜き・氷抜きなど）の検証

カタログの composition には、具材ごとに「最小・最大・既定の数量」が
書かれている。最小が既定より小さければ外せる（＝抜き）、
最大が既定より大きければ増やせる。

実データでの例:
  ハンバーガー  マスタード / ケチャップ / オニオン / ピクルス （どれも抜ける）
  コカ・コーラ  氷 （抜ける）
  マックフライポテト  調整できる具材なし
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.mcd.menu import (
    ParsedMenu, Product, Slot, customization_note, parse_menu,
)
from services.mcd.protocol import OrderItem, build_hex, decode_hex

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


def build_raw():
    """実データと同じ形の小さなカタログ。"""
    def comp(code, dq=1, mn=0, mx=1):
        return {"productCode": code, "defaultQuantity": dq,
                "minQuantity": mn, "maxQuantity": mx,
                "prePrice": {"valid": True, "price": 0}}
    products = {
        "1010": {  # ハンバーガー
            "productCode": "1010", "productClass": "PRODUCT",
            "priceList": [{"priceCode": "TAKEOUT", "price": 170}],
            "composition": [
                comp("99901031"),                 # マスタード（抜ける）
                comp("99901030"),                 # ケチャップ（抜ける）
                comp("99901032"),                 # ピクルス（抜ける）
                comp("99901099", mn=1, mx=1),     # バンズ（固定）
                comp("99801019", dq=1, mn=0, mx=2),  # スライスチーズ（抜ける・増やせる）
                comp("99900001"),                 # 名前の無い具材
            ],
            "choices": [], "canAdds": [],
        },
        "3120": {  # コカ・コーラ
            "productCode": "3120", "productClass": "PRODUCT",
            "priceList": [{"priceCode": "TAKEOUT", "price": 120}],
            "composition": [comp("99903001")],    # 氷（抜ける）
            "choices": [], "canAdds": [],
        },
        "2020": {  # ポテト（調整なし）
            "productCode": "2020", "productClass": "PRODUCT",
            "priceList": [{"priceCode": "TAKEOUT", "price": 330}],
            "composition": [], "choices": [], "canAdds": [],
        },
    }
    names = {
        "1010": "ハンバーガー", "3120": "コカ・コーラ", "2020": "マックフライポテト®",
        "99901031": "マスタード", "99901030": "ケチャップ", "99901032": "ピクルス",
        "99901099": "バンズ", "99801019": "スライスチーズ", "99903001": "氷",
        # 99900001 はわざと名前を入れない
    }
    return {
        "products": products,
        "groupMenu": {"products": {c: {"productCode": c, "tName": {"ja": n}}
                                   for c, n in names.items()}},
        "collections": [],
        "sizeVariants": {},
        "limitedAbility": {},
    }


class FakeCart:
    def __init__(self, menu):
        self.menu = menu
        self.pickup = "takeOut"


def main():
    m = parse_menu("10571", build_raw())

    print("\n[1] 調整できる具材を拾う")
    burger = m.products["1010"]
    names = [s.name for s in burger.customizations()]
    check("ハンバーガーの具材が並ぶ",
          set(names) == {"マスタード", "ケチャップ", "ピクルス", "スライスチーズ"}, names)
    check("固定の具材（バンズ）は出さない", "バンズ" not in names, names)
    check("名前の分からない具材は出さない", len(burger.customizations()) == 4, names)
    check("コーラは氷だけ", [s.name for s in m.products["3120"].customizations()] == ["氷"])
    check("ポテトは調整なし", m.products["2020"].customizations() == [])

    print("\n[2] 抜ける／増やせるの判定")
    by = {s.name: s for s in burger.customizations()}
    check("ピクルスは抜ける", by["ピクルス"].removable)
    check("ピクルスは増やせない", not by["ピクルス"].increasable)
    check("スライスチーズは抜けて増やせる",
          by["スライスチーズ"].removable and by["スライスチーズ"].increasable)
    fixed = next(s for s in burger.slots_of("composition") if s.code == "99901099")
    check("バンズは抜けない", not fixed.removable and not fixed.increasable)

    print("\n[3] 注文の中身に数量が入る")
    from ui.menu_flows import build_order_item
    cart = FakeCart(m)
    item = build_order_item(cart, burger, {}, {"99901032": 0})
    qty = {c.product_code: c.quantity for c in item.components}
    check("ピクルスが0になる", qty.get("99901032") == 0, qty)
    check("他の具材は既定のまま", qty.get("99901031") == 1 and qty.get("99901030") == 1, qty)
    check("固定の具材も残る", qty.get("99901099") == 1, qty)

    print("\n[4] カタログの範囲を超えた指定は丸める")
    item = build_order_item(cart, burger, {}, {"99901099": 0, "99801019": 99})
    qty = {c.product_code: c.quantity for c in item.components}
    check("抜けない具材を0にしようとしても1のまま", qty.get("99901099") == 1, qty)
    check("上限を超える増量は上限で止まる", qty.get("99801019") == 2, qty)

    print("\n[5] 氷抜き")
    cola = m.products["3120"]
    item = build_order_item(cart, cola, {}, {"99903001": 0})
    check("氷が0になる",
          {c.product_code: c.quantity for c in item.components}.get("99903001") == 0)

    print("\n[6] 表示に「抜き」が出る")
    item = build_order_item(cart, burger, {}, {"99901032": 0, "99901031": 0})
    note = customization_note(m, item)
    check("抜いた具材が並ぶ", "ピクルス" in note and "マスタード" in note, note)
    check("「抜き」と書く", note.endswith("抜き）"), note)
    item = build_order_item(cart, burger, {}, {"99801019": 2})
    check("増量も出る", "スライスチーズ×2" in customization_note(m, item),
          customization_note(m, item))
    item = build_order_item(cart, burger, {}, {})
    check("調整していなければ何も出さない", customization_note(m, item) == "",
          customization_note(m, item))

    print("\n[7] 注文コードに正しく載る")
    item = build_order_item(cart, burger, {}, {"99901032": 0})
    hexstr = build_hex("10571", [item], "takeOut")
    decoded = decode_hex(hexstr)
    top = decoded.items[0]
    got = {c.product_code: c.quantity for c in top.components}
    check("読み直してもピクルスが0", got.get("99901032") == 0, got)
    check("他の具材は1のまま", got.get("99901031") == 1, got)
    check("商品コードは変わらない", top.product_code == "1010", top.product_code)

    print("\n[8] 保存して読み戻しても消えない")
    import json
    from services.mcd.menu import Slot as S
    raw = json.loads(json.dumps([s.to_dict() for s in burger.slots]))
    back = [S.from_dict(d) for d in raw]
    check("具材名が残る",
          [b.name for b in back if b.code == "99901032"] == ["ピクルス"])
    check("数量の範囲が残る",
          [(b.min_quantity, b.default_quantity, b.max_quantity)
           for b in back if b.code == "99801019"] == [(0, 1, 2)])
    old_shape = {"kind": "composition", "code": "99901032", "min_quantity": 0,
                 "max_quantity": 1, "default_product": "", "reference_product": "",
                 "extra_price": 0, "cost_inclusive": True}
    check("列が増える前の保存内容も読める", S.from_dict(old_shape).code == "99901032")

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
