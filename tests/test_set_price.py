"""セットの金額（選んだ中身で変わる差額）

⚠️⚠️ **セットの prePrice は「参照商品を選んだときの値段」。**
   実データ（13934 / 9241 ビッグマック® セット）で検算できる。

       セット prePrice 810
         = 350（バーガー分 priceList TAKEOUT）
         + 370（サイド枠 9987009 の prePrice ＝ ポテトM の単品価格）
         +  90（ドリンク枠 9997918 の prePrice）

   ¥810 は「ポテトM＋コーラM」の値段であって、別のものを選べば
   差額が乗る。

⚠️ **この差額を見積りに入れていなかった。** そのため既定以外の
   ドリンクやサイドを選んだセットは、マクドナルドの言う金額と
   こちらの見積りが必ず食い違い、決済の直前で中止されていた。
   実データで **417通り** が該当した。利用者からは
   「セットが頼めない」ように見える。

⚠️ ここで固定するのは次の3点。
   ① 参照の組み合わせなら差額0（合計はセット価格ちょうど）
   ② 高いものを選んだら差額が乗る
   ③ 安いものを選んでも**引かない**（refundThreshold=0 ＝ 返金の仕組みが無い）
"""
import asyncio, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.mcd import menu as M
import ui.menu_flows as MF

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeCart:
    def __init__(self, menu, pickup="takeOut"):
        self.menu = menu
        self.pickup = pickup
        self.owner_id = 1


def main() -> int:
    mn = M.parse_menu("13934", json.load(open(os.path.join(HERE, "m13934.json"))))
    cart = FakeCart(mn)
    p = mn.products["9241"]          # ビッグマック® セット

    print("\n[1] カタログの足し算が合っているか ★")
    base = p.price_for("takeOut")
    check("セット定価 ¥810", base == 810, base)
    parts = {s.code: s.extra_price for s in p.slots_of("choices")}
    check("サイド枠に含まれる額 ¥370", parts.get("9987009") == 370, parts)
    check("ドリンク枠に含まれる額 ¥90", parts.get("9997918") == 90, parts)
    check("バーガー分 + 枠の合計 = セット定価 ★",
          p.price_takeout + sum(parts.values()) == base,
          f"{p.price_takeout}+{sum(parts.values())}")

    print("\n[2] 参照の組み合わせなら差額が出ない ★")
    # ⚠️ ここが崩れると、**普通のセットまで注文できなくなる**。
    item = MF.build_order_item(cart, p, {"9997918": "3120"})   # ポテトM＋コーラM
    got = MF.price_of(mn, item, "takeOut")
    check("ポテトM＋コーラM は ¥810 のまま ★", got == 810, got)

    print("\n[3] 高いものを選んだら差額が乗る ★")
    for picks, want, label in (
        ({"9997918": "3479"}, 810 + 50,  "黒烏龍茶（単品¥360 / 参照¥310）"),
        ({"9997918": "3918"}, 810 + 20,  "野菜生活100 M（¥330）"),
        ({"9997918": "3120", "9987009": "1670"}, 810 + 410,
         "サイドをナゲット15ピース（¥780 / 参照のポテトM¥370）"),
        ({"9997918": "3479", "9987009": "9099"}, 810 + 50 + 610,
         "ポテナゲ特大（¥980）＋黒烏龍茶"),
    ):
        item = MF.build_order_item(cart, p, picks)
        got = MF.price_of(mn, item, "takeOut")
        check(f"{label} → ¥{want}", got == want, got)

    print("\n[4] 安いものを選んでも引かない ★")
    # ⚠️ カタログの refundThreshold は 0。返金の仕組みが無いので、
    #    引くと取りはぐれ（運営の持ち出し）になる。
    item = MF.build_order_item(cart, p, {"9997918": "3501"})   # コーヒーS ¥140
    got = MF.price_of(mn, item, "takeOut")
    check("コーヒーS（¥140）でも ¥810 のまま ★", got == 810, got)

    print("\n[5] 受取方法で差額も変わるか")
    for method in ("eatIn", "takeOut", "addressDelivery"):
        item = MF.build_order_item(cart, p, {"9997918": "3120"})
        got = MF.price_of(mn, item, method)
        want = p.price_for(method)
        check(f"{method}: 参照の組み合わせは定価 ¥{want} と一致",
              got == want, got)

    print("\n[6] 画面の表示が差額になっているか ★")
    # ⚠️ 以前は枠の prePrice（含まれている額）を「+90円」と出していた。
    #    コカ・コーラMを選んだだけで「+90円」と出ており、意味が逆。
    view_cls = None
    for nm in dir(MF):
        o = getattr(MF, nm)
        if isinstance(o, type) and hasattr(o, "_slot_extra"):
            view_cls = o
            break
    check("差額を出す処理がある ★", view_cls is not None, "見つからない")
    if view_cls is not None:
        v = view_cls.__new__(view_cls)
        v.cart = cart
        v.product = p
        v.choices = list(p.slots_of("choices"))
        v.picks = {}
        drink = [s for s in v.choices if s.code == "9997918"][0]
        side = [s for s in v.choices if s.code == "9987009"][0]
        check("コーラMは「+0円」（含まれている90円を出さない）★",
              v._slot_extra(drink, ["3120"]) == 0, v._slot_extra(drink, ["3120"]))
        check("黒烏龍茶は「+50円」★",
              v._slot_extra(drink, ["3479"]) == 50, v._slot_extra(drink, ["3479"]))
        check("ナゲット15ピースは「+410円」★",
              v._slot_extra(side, ["1670"]) == 410, v._slot_extra(side, ["1670"]))
        check("未選択なら0（落ちない）", v._slot_extra(drink, []) == 0)
        check("知らないコードでも落ちない", v._slot_extra(drink, ["ないコード"]) == 0)

    print("\n[7] 入れ子の枠でも落ちないか")
    # ポテナゲ（中のナゲットにソース枠がある）
    nested = [q for q in mn.products.values()
              if mn.nested_choices(q) and q.slots_of("choices")]
    check(f"入れ子の枠を持つ商品がある（{len(nested)}件）", bool(nested))
    bad = []
    for q in nested[:10]:
        try:
            it = MF.build_order_item(cart, q, {})
            MF.price_of(mn, it, "takeOut")
        except Exception as e:
            bad.append(f"{q.code} {type(e).__name__}: {e}")
    check("入れ子の枠でも金額を出せる ★", not bad, bad[:2])

    print("\n[8] 全セットで金額を出しても落ちないか ★")
    bad = []
    for q in mn.products.values():
        if not q.slots_of("choices"):
            continue
        try:
            it = MF.build_order_item(cart, q, {})
            v = MF.price_of(mn, it, "takeOut")
            if v < q.price_for("takeOut"):
                bad.append(f"{q.code} {q.name} が定価より安い")
        except Exception as e:
            bad.append(f"{q.code} {type(e).__name__}: {e}")
    check("選択枠を持つ全商品で落ちない・定価を下回らない ★", not bad, bad[:3])

    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
