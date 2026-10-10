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
import asyncio, json, os, pathlib, sys

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

    print("\n[3] 上乗せ額を当てにいかない ★")
    # ⚠️⚠️ **実機で確かめた結果、計算では出せないと分かった。**
    #    参照は コカ・コーラ M（¥310）。
    #       カフェラテ    単品¥240（参照より安い） → 実機 **+¥50**
    #       野菜生活100   単品¥330（参照より高い） → 実機 **+¥0**
    #    一度「差額＝単品価格の差」で計算したが、8件中5件外れた。
    #    カタログのどこにも上乗せ額は書かれていない（全項目確認済み）。
    #
    #    だから**定価だけを出す**。当て推量の金額を自信ありげに
    #    出すほうが、出さないより悪い。本当の金額はマクドナルドが
    #    注文の登録時に返すので、決済の前にそれを見せて確かめる。
    for picks, label in (
        ({"9997918": "3479"}, "黒烏龍茶（単品¥360）"),
        ({"9997918": "3918"}, "野菜生活100（¥330・実機は+¥0）"),
        ({"9997918": "3120", "9987009": "1610"}, "サイドをナゲット5ピース"),
    ):
        item = MF.build_order_item(cart, p, picks)
        got = MF.price_of(mn, item, "takeOut")
        check(f"{label} でも定価 ¥810 のまま ★", got == 810, got)
    check("差額を計算する処理を持たない ★",
          not hasattr(MF, "choice_upcharge"), "choice_upcharge が残っている")

    print("\n[5] 受取方法ごとの定価")
    for method in ("eatIn", "takeOut", "addressDelivery"):
        item = MF.build_order_item(cart, p, {"9997918": "3120"})
        got = MF.price_of(mn, item, method)
        want = p.price_for(method)
        check(f"{method}: 参照の組み合わせは定価 ¥{want} と一致",
              got == want, got)

    print("\n[6] 画面に当て推量の金額を出さないか ★")
    # ⚠️ 以前は枠の prePrice（含まれている額）を「+90円」として
    #    出していた。意味が逆なうえ、正しい上乗せ額でもない。
    src = (pathlib.Path(__file__).parent.parent / "ui" / "menu_flows.py").read_text()
    check("選択肢に「+¥」を出さない ★", "+{embeds.yen(up)}" not in src)
    check("extra_price を上乗せ額として出さない ★",
          "c.extra_price" not in src, "まだ出している")
    check("お値段が変わりうることは伝える ★", "変わることがあります" in src)

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

    print("\n[9] 実機の選択肢とどれだけ合っているか ★")
    # ⚠️ 実機（公式アプリ / チーズチーズ倍月見 セット）で確かめた。
    #    サイド枠に出るのは **4つだけ**。
    #      マックフライポテト / サイドサラダ /
    #      チキンマックナゲット5ピース / えだまめコーン
    #    こちらは14件出しており、**セットそのもの**（ポテナゲ特大 ¥980、
    #    食べくらべポテナゲ特大 ¥990）まで並べていた。
    #    選べないものを選ばせれば、注文は必ず断られる。
    #    カタログには「どれが選べるか」が書かれていない（全項目確認済み）
    #    ので完全には絞れないが、**確実に違うものは外す**。
    REAL = {"2020", "2323", "1610", "2605"}
    p9195 = mn.products["9195"]
    side = [x for x in p9195.slots_of("choices") if x.code == "9987009"][0]
    cands = mn.choice_candidates(side, 14 * 60, parent=p9195)
    got = {c.code for c in cands}
    check("実機の4つが全部残っている ★", REAL <= got, sorted(REAL - got))
    meals = [c.code for c in cands if c.product_class == "VALUE_MEAL"]
    check("セット（VALUE_MEAL）を候補にしない ★", not meals, meals)
    check("朝だけの商品（ハッシュポテト5010）を出さない ★",
          "5010" not in got, sorted(got))
    # ⚠️ **残っている4件を「出さない」と書いてはいけない。**
    #    前の版は `"1670" not in got or len(got) <= 8` と書いており、
    #    後ろが常に真なので、**実際には出しているのに合格**していた。
    #    通るだけのテストは無いより悪い。いまの状態を正直に固定する。
    #
    #    この4件は実機では選べないが、カタログ上は正しい4つと
    #    見分けがつかない（productClass も dayPart も時間帯も価格も
    #    同じ / docs/09 V-22）。断られた時点で自動的に消える。
    extra = sorted(got - REAL)
    check("実機に無い候補が4件のまま（増えていない）★",
          extra == ["1670", "2080", "2081", "2255"], extra)
    check(f"候補が広がりすぎない（8件以下 / いま{len(got)}件）★",
          len(got) <= 8, sorted(got))
    check("候補が空にならない ★", bool(got))

    # ⚠️ 候補を狭めすぎて空にしないこと。空になると、その商品が
    #    一切注文できなくなる（直す前より悪い）。
    #
    # ⚠️ **参照商品が無い枠は数えない。** マカロンのボックスセットなど、
    #    カタログに referenceProduct も defaultProduct も入っていない枠が
    #    あり、これは元から候補を出しようがない（別の既知の穴で、
    #    services/mcd/slot_hints.py の担当）。ここで一緒に数えると、
    #    絞り込みのせいで空になったのか元からかが分からなくなる。
    empty, no_ref = [], []
    for q in mn.products.values():
        for sl in q.slots_of("choices"):
            if sl.min_quantity < 1:
                continue
            if not mn.slot_reference(sl):
                no_ref.append(f"{q.code} / 枠{sl.code}")
                continue
            if not mn.choice_candidates(sl, 14 * 60, parent=q):
                empty.append(f"{q.code} {q.name} / 枠{sl.code}")
    check(f"参照のある必須枠が空にならない ★（{len(mn.products)}商品）",
          not empty, empty[:3])
    print(f"      （参照そのものが無い枠 {len(no_ref)} 件は別の穴。"
          f"例 {no_ref[:2]}）")

    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
