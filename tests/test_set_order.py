"""
セット注文の組み立ての検証

実物の注文コード（docs/09）とバイト単位で突き合わせる。
ここが違うとマクドナルド側に弾かれ、セットが一切注文できない。

実物の構造:
    9180 月見バーガー セット          金額800
      9987009 サイド枠 → 2020
      9997918 ドリンク枠 → 9997914 → 3120
                            ↑ カタログに載っていない中間ノード
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.mcd import slot_bridge as SB
from services.mcd.menu import parse_menu
from services.mcd.protocol import build_hex, build_item, decode_hex

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

# docs/09 で検証済みの実物
REAL = ("0a0531333933343a020a00424e124c120439313830180120a006"
        "2a17080112073939383730303918012a0812043230323018012a26"
        "080112073939393739313818012a1708011207393939373931341801"
        "2a081204333132301801")

HERE = os.path.dirname(os.path.abspath(__file__))


def build_catalog():
    """実物と同じ構造の、小さなカタログ。"""
    def slot(code, default="", ref="", pre=0):
        return {"productCode": code, "defaultProduct": default,
                "referenceProduct": ref, "defaultQuantity": 1,
                "minQuantity": 1, "maxQuantity": 1,
                "prePrice": {"valid": True, "price": pre}}
    def comp(code):
        return {"productCode": code, "defaultQuantity": 1,
                "minQuantity": 0, "maxQuantity": 1,
                "prePrice": {"valid": True, "price": 0}}
    products = {
        "9180": {"productCode": "9180", "productClass": "VALUE_MEAL",
                 "priceList": [{"priceCode": "EATIN", "price": 340}],
                 "prePrice": {"valid": True, "price": 800},
                 "composition": [comp("1566")],
                 "choices": [slot("9987009", "2020", "2020", 370),
                             slot("9997918", "", "3120", 90)],
                 "canAdds": []},
        "1566": {"productCode": "1566", "composition": [], "choices": [], "canAdds": [],
                 "priceList": [{"priceCode": "EATIN", "price": 340}]},
        "2020": {"productCode": "2020", "composition": [], "choices": [], "canAdds": [],
                 "priceList": [{"priceCode": "EATIN", "price": 330}]},
        "3120": {"productCode": "3120", "composition": [comp("99903001")],
                 "choices": [], "canAdds": [],
                 "priceList": [{"priceCode": "EATIN", "price": 120}]},
        "3170": {"productCode": "3170", "composition": [], "choices": [], "canAdds": [],
                 "priceList": [{"priceCode": "EATIN", "price": 120}]},
    }
    names = {"9180": "月見バーガー セット", "1566": "月見バーガー",
             "2020": "マックフライポテト®", "3120": "コカ・コーラ",
             "3170": "スプライト", "99903001": "氷"}
    return {
        "products": products,
        "groupMenu": {"products": {c: {"productCode": c, "tName": {"ja": n}}
                                   for c, n in names.items()}},
        "collections": [{"id": "4", "tName": {"ja": "ドリンク"},
                         "productCodes": ["3120", "3170"]},
                        {"id": "3", "tName": {"ja": "サイドメニュー"},
                         "productCodes": ["2020"]}],
        "sizeVariants": {}, "limitedAbility": {},
    }


class Cart:
    def __init__(self, menu, pickup="eatIn"):
        self.menu = menu
        self.pickup = pickup


def shape(it):
    """構造を比べやすい形に。"""
    return (it.product_code, it.quantity,
            [shape(c) for c in it.components])


def main():
    import tempfile
    from pathlib import Path as _P

    # 検証でプロジェクトの data/ を汚さないよう、控えの保存先を移す
    SB.STORE_PATH = _P(tempfile.mkdtemp()) / "slot_bridge.json"
    SB._loaded = True

    from ui.menu_flows import build_order_item

    menu = parse_menu("13934", build_catalog())
    cart = Cart(menu)
    real = decode_hex(REAL).items[0]

    print("\n[1] 実物と同じ構造になる ★")
    item = build_order_item(cart, menu.products["9180"], {"9997918": "3120"})
    check("構造が一致する ★", shape(item) == shape(real),
          f"\n   BOT : {shape(item)}\n   実物: {shape(real)}")
    check("金額が一致する", item.amount == real.amount, (item.amount, real.amount))

    print("\n[2] バイト列まで一致する ★")
    mine = build_item(item, top_level=True).hex()
    theirs = build_item(real, top_level=True).hex()
    check("商品部分が完全一致 ★", mine == theirs,
          f"\n   BOT : {mine[:90]}\n   実物: {theirs[:90]}")

    print("\n[3] 既定のままの具材は送らない ★")
    codes = [c.product_code for c in item.components]
    check("セットの中のバーガーを送らない ★", "1566" not in codes, codes)
    check("選択枠だけが並ぶ", codes == ["9987009", "9997918"], codes)

    print("\n[4] 抜いた具材だけは送る")
    cola = menu.products["3120"]
    plain = build_order_item(cart, cola, {})
    check("何も指定しなければ空", plain.components == [],
          [c.product_code for c in plain.components])
    no_ice = build_order_item(cart, cola, {}, {"99903001": 0})
    check("氷抜きは送る",
          [(c.product_code, c.quantity) for c in no_ice.components] == [("99903001", 0)],
          [(c.product_code, c.quantity) for c in no_ice.components])

    print("\n[5] 中間ノードの扱い")
    side = next(c for c in item.components if c.product_code == "9987009")
    drink = next(c for c in item.components if c.product_code == "9997918")
    check("サイド枠は中間を挟まない",
          [c.product_code for c in side.components] == ["2020"],
          [c.product_code for c in side.components])
    check("ドリンク枠は中間を挟む ★",
          [c.product_code for c in drink.components] == ["9997914"],
          [c.product_code for c in drink.components])
    check("中間の下に商品が入る",
          [c.product_code for c in drink.components[0].components] == ["3120"])

    print("\n[6] 別のドリンクを選んでも形は同じ")
    other = build_order_item(cart, menu.products["9180"], {"9997918": "3170"})
    d2 = next(c for c in other.components if c.product_code == "9997918")
    check("中間は変わらない", d2.components[0].product_code == "9997914")
    check("選んだ商品が入る", d2.components[0].components[0].product_code == "3170")

    print("\n[7] フラグの規則 ★")
    def flags(it, depth=0, top=True):
        out = [(it.product_code, bool(it.components) and not top)]
        for c in it.components:
            out += flags(c, depth + 1, top=False)
        return out
    mine_f = flags(item)
    real_f = [("9180", False), ("9987009", True), ("2020", False),
              ("9997918", True), ("9997914", True), ("3120", False)]
    check("子を持つ中間だけにフラグが付く ★", mine_f == real_f, mine_f)

    print("\n[8] 貼られた注文コードから中間ノードを学ぶ ★")
    saved_known = dict(SB.KNOWN)
    SB.forget_all(); SB.KNOWN.clear()
    check("学ぶ前は知らない", SB.bridge_for("9997918") == "")
    n = SB.learn_from_order(decode_hex(REAL).items)
    check("1件だけ学ぶ ★", n == 1, n)
    check("正しい対応をおぼえる ★", SB.bridge_for("9997918") == "9997914",
          SB.bridge_for("9997918"))
    check("商品→枠は学ばない（カタログにあるので不要）",
          SB.bridge_for("9180") == "", SB.bridge_for("9180"))
    check("同じものを2度は学ばない", SB.learn_from_order(decode_hex(REAL).items) == 0)

    # ------------------------------------------------------------
    print("\n[ 実物で確かめていない選択枠の洗い出し ]")
    # ------------------------------------------------------------
    # ⚠️ 中間ノードはカタログに載っておらず、**実物の注文コードからしか
    #    分からない**。分からない枠は「中間なし」として送っているので、
    #    本当は中間が要る枠だと注文が通らない。
    #    どの枠が未検証かを、いつでも数えられるようにしておく。
    import json as _json
    from collections import defaultdict

    from services.mcd.menu import parse_menu as _parse

    _pm = _parse("13934", _json.load(open(os.path.join(HERE, "m13934.json"))))
    # 実物の注文コードから確認できている枠
    VERIFIED = {
        "9987009", "9987010", "9987017", "9997008", "9997028",  # 中間なし
        "9997918", "9997925",                                   # 中間あり
    }
    users = defaultdict(set)
    for _mins in (8 * 60, 13 * 60, 20 * 60):
        for _c, _p in _pm.products.items():
            if _p.product_class != "VALUE_MEAL" or not _pm.orderable(_p, _mins):
                continue
            for _s in _p.slots_of("choices"):
                users[_s.code].add(_c)
    unknown = sorted(c for c in users if c not in VERIFIED)
    risky = sorted({c for u in unknown for c in users[u]})
    print(f"      未検証の枠 {len(unknown)} 種類 / 影響する商品 {len(risky)} 件")
    for u in unknown:
        print(f"        枠{u} … {len(users[u])} 商品")
    check("検証済みの枠は実在する ★",
          all(v in users or v in SB.KNOWN for v in VERIFIED),
          [v for v in VERIFIED if v not in users and v not in SB.KNOWN])
    # ⚠️ ここは「0件であるべき」ではない。実物をいただくまでは残る。
    #    数が**増えていないこと**を見るための記録。
    check("未検証の枠が想定どおり（増えていない）★", len(unknown) <= 6, unknown)

    print("\n[9] 中間を知らない枠でも注文を止めない ★")
    SB.forget_all(); SB.KNOWN.clear()
    fallback = build_order_item(cart, menu.products["9180"], {"9997918": "3120"})
    d3 = next(c for c in fallback.components if c.product_code == "9997918")
    check("枠の直下に商品を置く ★",
          [c.product_code for c in d3.components] == ["3120"],
          [c.product_code for c in d3.components])
    check("注文コードは作れる", bool(build_hex("13934", [fallback], "eatIn")))
    SB.KNOWN.update(saved_known)

    print("\n[10] 読み直しても形が崩れない")
    SB.KNOWN.update({"9997918": "9997914"})
    again = decode_hex(build_hex("13934", [item], "eatIn")).items[0]
    check("往復しても同じ", shape(again) == shape(real), shape(again))
    check("金額も残る", again.amount == 800, again.amount)

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
