"""
注文経路の総点検

実データ（247商品・81セット）を全部通して、
「表示したのに注文できない」経路が残っていないかを確かめる。
"""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from _fake_discord import FakeInteraction, FakeUser, FakeClient

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))
HOURS = [6*60, 8*60, 10*60+30, 12*60, 15*60, 18*60, 22*60]


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/au.db")
    from core import settings, users as user_repo
    await settings.load_all()

    from services.mcd.menu import parse_menu
    from services.mcd.protocol import build_hex, decode_hex
    from services.mcd import slot_rules
    import pathlib
    slot_rules.STORE_PATH = pathlib.Path(tmp) / "sr.json"
    slot_rules.load()
    from ui.menu_flows import (
        CartView, OptionView, ProductDetailView, build_order_item, price_of,
    )

    menu = parse_menu("13934", json.load(open(os.path.join(HERE, "m13934.json"))))
    user = FakeUser(8001)
    await user_repo.get_or_create(user.id)
    client = FakeClient()

    def cart():
        return CartView(user.id, "order", "13934", "南砂町店",
                        {"takeOut": True, "eatIn": True}, menu)

    # ========================================================
    print("\n[1] 一覧に出る商品は、すべて注文を組み立てられる ★")
    # ⚠️ 「表示したのに注文できない」が一番つらい。全時間帯で確かめる。
    problems = []
    for minutes in HOURS:
        for col in menu.collections:
            for p in menu.visible_products(col.id, minutes):
                missing = menu.unfillable_slots(p, minutes)
                if missing:
                    problems.append((f"{minutes//60}時", p.name, "埋まらない枠"))
    check("埋められない枠を持つ商品が一覧に出ない ★", not problems,
          problems[:4])

    print("\n[2] 一覧に出る商品から hex を作れる ★")
    broke = []
    for minutes in HOURS:
        for col in menu.collections:
            for p in menu.visible_products(col.id, minutes):
                picks = {}
                for slot in p.slots_of("choices"):
                    cands = menu.choice_candidates(slot, minutes)
                    if cands:
                        picks[slot.code] = cands[0].code
                C = type("C", (), {"pickup": "takeOut", "menu": menu})
                try:
                    item = build_order_item(C, p, picks)
                    d = decode_hex(build_hex("13934", [item], "takeOut"))
                    if d.items[0].product_code != p.code:
                        broke.append((p.name, "商品コードが違う"))
                except Exception as e:
                    broke.append((p.name, str(e)[:50]))
    check("全部 hex にできる ★", not broke, broke[:4])

    print("\n[3] 必須の枠が空のまま注文できない ★")
    meal = next(p for p in menu.products.values()
                if p.product_class == "VALUE_MEAL" and p.slots_of("choices")
                and all(not s.default_product for s in p.slots_of("choices")[:1]))
    c3 = cart()
    ov = OptionView(c3, meal, meal.slots_of("choices"))
    ov.picks = {}                       # わざと空にする
    itx = FakeInteraction(user, client)
    await ov._on_ok(itx)
    check("カートに入らない ★", len(c3.items) == 0, len(c3.items))
    check("理由を伝える ★", "選ばれていません" in itx.text(), itx.text()[:140])

    print("\n[4] 埋められないセットは詳細画面でも押せない ★")
    # ⚠️ 実データ側は（ハッピーセットの手がかりを入れたことで）
    #    埋まらない枠が0件になった。前提が無くなったので、
    #    **埋まらない枠をわざと作って** 画面の動きを確かめる。
    # ハッピーセット8商品は手がかりの表で解消した。残るのは
    #   ・マカロンのボックス（枠の中身がカタログに一切無い）
    #   ・エンプロイミール（従業員用。そもそも売らない）
    left = {p.code for p in menu.products.values() if menu.unfillable_slots(p, 12*60)}
    check("ハッピーセットは埋まるようになった ★",
          not (left & {"9005", "9006", "9007", "9008",
                       "9058", "9059", "9064", "9138"}), sorted(left))
    check("残っているのはマカロンと従業員用だけ ★",
          left == {"9122", "9125"} | set(menu.staff_only), sorted(left))
    check("従業員用は注文できない ★",
          all(not menu.orderable(menu.products[c], 12*60) for c in menu.staff_only),
          sorted(menu.staff_only))

    import copy
    from services.mcd.menu import Slot
    blocked = copy.deepcopy(meal)
    blocked.slots.append(Slot(
        kind="choices", code="9999999",      # どこにも載っていない枠
        min_quantity=1, max_quantity=1, default_quantity=1,
    ))
    check("作った枠は埋められない（検証の前提）",
          [s.code for s in menu.unfillable_slots(blocked, 12*60)] == ["9999999"],
          [s.code for s in menu.unfillable_slots(blocked, 12*60)])
    if blocked:
        dv = ProductDetailView(cart(), blocked)
        btn = [c for c in dv.children if not hasattr(c, "options")]
        choose = next((c for c in btn if "中身" in getattr(c, "label", "")), None)
        check("ボタンが押せない ★", choose is not None and choose.disabled, 
              [(getattr(c,'label',''), getattr(c,'disabled',None)) for c in btn])
        body = "".join(f.name + f.value for f in dv.build_embed().fields)
        check("理由が書いてある ★", "承れません" in body, body[:200])

    print("\n[5] カートの中身が揃っているか、確定前に見る ★")
    c5 = cart()
    item = build_order_item(type("C", (), {"pickup": "takeOut", "menu": menu}), meal, {})
    c5.items.append(item)               # 枠が空のまま入れる
    bad = c5._incomplete_items(12*60)
    check("揃っていない商品を見つける ★", bad, bad)
    itx5 = FakeInteraction(user, client)
    await c5._on_go(itx5)
    check("受取方法の画面へ進ませない ★", "そろっていない" in itx5.text(),
          itx5.text()[:140])

    print("\n[6] カートの表示金額と、送る金額が一致する ★")
    # ⚠️ ここがずれると、見せた額と請求額が食い違う
    from ui.menu_flows import reprice
    mismatches = []
    for pickup in ("eatIn", "takeOut"):
        c6 = cart()
        for code in ("1010", "1020", "9180"):
            p6 = menu.products[code]
            c6.items.append(build_order_item(
                type("C", (), {"pickup": pickup, "menu": menu}), p6,
                {s.code: (menu.choice_candidates(s, 12*60) or [p6])[0].code
                 for s in p6.slots_of("choices")}))
        reprice(menu, c6.items, pickup)
        shown = c6.total(pickup)
        sent = decode_hex(build_hex("13934", c6.items, pickup)).total_amount
        if shown != sent:
            mismatches.append((pickup, shown, sent))
    check("表示と送信が一致 ★", not mismatches, mismatches)

    print("\n[7] 断られた組み合わせが候補から消える ★")
    meal7 = menu.products["9180"]
    slot7 = meal7.slots_of("choices")[0]
    before = len(menu.choice_candidates(slot7, 12*60))
    victim = menu.choice_candidates(slot7, 12*60)[-1]
    slot_rules.reject("13934", slot7.code, victim.code)
    after = menu.choice_candidates(slot7, 12*60)
    check("1件だけ減る ★", len(after) == before - 1, (before, len(after)))
    check("消えたのは断られたもの ★", victim.code not in [q.code for q in after])
    check("既定の商品は残る ★",
          any(q.code == slot7.reference_product for q in after))

    print("\n[8] 全部断られても、既定だけは残る ★")
    # ⚠️ 候補が0になると注文を組み立てられなくなる
    for q in list(menu.choice_candidates(slot7, 12*60)):
        slot_rules.reject("13934", slot7.code, q.code)
    left = menu.choice_candidates(slot7, 12*60)
    check("候補が空にならない ★", left, [q.name for q in left])
    slot_rules.forget("13934")

    print("\n[9] 時間帯をまたいでも矛盾しない ★")
    odd = []
    for minutes in HOURS:
        for col in menu.collections:
            for p in menu.visible_products(col.id, minutes):
                if not p.is_orderable_at(minutes):
                    odd.append((minutes // 60, p.name))
                for slot in p.slots_of("choices"):
                    for q in menu.choice_candidates(slot, minutes):
                        if not q.is_orderable_at(minutes):
                            odd.append((minutes // 60, f"{p.name}の{q.name}"))
    check("時間外のものが候補に混ざらない ★", not odd, odd[:4])

    print("\n[10] ナゲットのソース枠 ★")
    # ⚠️ 利用者から「食べくらべポテナゲ特大が注文できない」と報告。
    #    原因はナゲットが持つソース枠(7251)を一切扱っていなかったこと。
    nugget = menu.products["1610"]
    sauce_slot = next(s for s in nugget.slots_of("choices") if s.code == "7251")
    check("ナゲットにソース枠がある（前提）", sauce_slot.min_quantity >= 1,
          sauce_slot.min_quantity)
    check("枠に参照が書かれていない（前提）★", not sauce_slot.reference_product,
          sauce_slot.reference_product)
    check("他の商品の同じ枠から参照を引ける ★",
          menu.slot_reference(sauce_slot) == "6048",
          menu.slot_reference(sauce_slot))
    # ⚠️ 同じ名前で products にもあるが**別物**。
    #      5502 バーベキューソース price=50 … 単品で買うソース
    #      6048 バーベキューソース price=0  … ナゲットに付ける無料のソース
    #    読み替えると、無料のはずのソースを50円で注文してしまう。
    check("単品のソース(5502)に読み替えない ★",
          menu.slot_reference(sauce_slot) != "5502")
    sauces = menu.choice_candidates(sauce_slot, 12*60, parent=nugget)
    check("ソース3種が候補に出る ★", len(sauces) == 3, [q.name for q in sauces])
    check("食べ物が混ざらない ★", all("ソース" in q.name for q in sauces),
          [q.name for q in sauces])
    check("無料の選択肢専用の商品である ★",
          all(q.code in menu.extras for q in sauces), [q.code for q in sauces])
    check("単品のナゲットが注文できる ★", menu.orderable(nugget, 12*60))

    print("\n[11] ポテナゲ（構成品がソース枠を持つ）★")
    for code in ("9222", "9221", "9094", "9099"):
        p11 = menu.products.get(code)
        if p11 is None:
            continue
        nested = menu.nested_choices(p11)
        check(f"{p11.name[:18]} の入れ子の枠を見つける ★", len(nested) == 1, nested)
        picks = {
            f"{c}/{sl.code}": menu.choice_candidates(
                sl, 12*60, parent=menu.products[c])[0].code
            for c, sl in nested
        }
        C11 = type("C", (), {"pickup": "takeOut", "menu": menu})
        item = build_order_item(C11, p11, picks)
        # セット → 構成品 → 枠 → ソース の4段になる
        def depth(n):
            return 1 + max([depth(c) for c in n.components], default=0)
        d11 = decode_hex(build_hex("13934", [item], "takeOut")).items[0]
        check(f"{p11.name[:18]} のソースが注文に入る ★", depth(d11) == 4, depth(d11))
        leaves = [x.product_code for x in d11.walk()]
        check(f"{p11.name[:18]} にソースの商品コードが入る ★",
              any(c in ("6048", "6049", "6059") for c in leaves), leaves)

    print("\n[12] ソースを選ばないと追加できない ★")
    c12 = cart()
    pote = menu.products["9222"]
    ov12 = OptionView(c12, pote, pote.slots_of("choices"))
    check("ソース枠が画面に出る ★", any(sl.code == "7251" for sl in ov12.choices),
          [sl.code for sl in ov12.choices])
    ov12.picks = {}
    itx12 = FakeInteraction(user, client)
    await ov12._on_ok(itx12)
    check("選ばないとカートに入らない ★", len(c12.items) == 0, len(c12.items))
    check("理由を伝える ★", "選ばれていません" in itx12.text(), itx12.text()[:140])

    print("\n[12.5] 選択肢専用の商品 ★")
    # ⚠️ products に無く groupMenu にだけある商品（6xxx番台）。
    #    同じ名前で products にもあるが**別物**。
    #      5502 バーベキューソース price=50 … 単品で買うソース
    #      6048 バーベキューソース price=0  … ナゲットに付ける無料のソース
    check("選択肢専用の商品を拾っている ★", len(menu.extras) > 0, len(menu.extras))
    check("材料（氷・ピクルス）が混ざらない ★",
          not any(c.startswith("999") for c in menu.extras), 
          [c for c in menu.extras if c.startswith("999")])
    check("社内用の符号【CLR】が混ざらない ★",
          not any(e.name.startswith("【") for e in menu.extras.values()),
          [e.name for e in menu.extras.values() if e.name.startswith("【")])
    check("単品で買える商品は入らない ★",
          not (set(menu.extras) & set(menu.products)),
          set(menu.extras) & set(menu.products))

    for code, label, expect in (
        ("2080", "シャカチキ", "シーズニング"),
        ("2323", "サイドサラダ", "ドレッシング"),
    ):
        p125 = menu.products[code]
        slots = p125.slots_of("choices")
        cands = menu.choice_candidates(slots[0], 12*60, parent=p125)
        check(f"{label}の枠が解ける ★", cands, [q.name for q in cands])
        check(f"{label}に{expect}が出る ★",
              any(expect in q.name for q in cands), [q.name for q in cands])
        check(f"{label}が注文できる ★", menu.orderable(p125, 12*60))

    print("\n[13] 画面に枠コードを出さない ★")
    # スクリーンショットで「選択枠 9987009」と出ていた
    meal13 = menu.products["9180"]
    ov13 = OptionView(cart(), meal13, meal13.slots_of("choices"))
    body13 = "".join(f.name for f in ov13.build_embed().fields)
    check("見出しに枠コードが出ない ★",
          not any(sl.code in body13 for sl in ov13.choices), body13)
    check("人が読める見出しになっている ★",
          "ドリンク" in body13 or "サイド" in body13, body13)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
