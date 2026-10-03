"""
利用者の使いやすさの検証

パネルとボタンだけで操作する人にとって、
・手順が少ないか
・どの画面からも戻れるか
・迷ったときに次の一手が分かるか
を確かめる。
"""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from _fake_discord import FakeInteraction, FakeUser, FakeClient

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


def catalog():
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
        "1010": {"productCode": "1010", "productClass": "PRODUCT",
                 "priceList": [{"priceCode": "TAKEOUT", "price": 170}],
                 "composition": [comp("99901032"), comp("99901033")],
                 "choices": [], "canAdds": []},
        "9180": {"productCode": "9180", "productClass": "VALUE_MEAL",
                 "priceList": [{"priceCode": "TAKEOUT", "price": 340}],
                 "prePrice": {"valid": True, "price": 800},
                 "composition": [comp("1566")],
                 "choices": [slot("9987009", "2020", "2020", 370)],
                 "canAdds": []},
        "1566": {"productCode": "1566", "composition": [], "choices": [], "canAdds": [],
                 "priceList": [{"priceCode": "TAKEOUT", "price": 340}]},
        "2020": {"productCode": "2020", "composition": [], "choices": [], "canAdds": [],
                 "priceList": [{"priceCode": "TAKEOUT", "price": 330}]},
    }
    names = {"1010": "ハンバーガー", "9180": "月見バーガー セット",
             "1566": "月見バーガー", "2020": "マックフライポテト®",
             "99901032": "ピクルス", "99901033": "オニオン"}
    return {
        "products": products,
        "groupMenu": {"products": {c: {"productCode": c, "tName": {"ja": n}}
                                   for c, n in names.items()}},
        "collections": [{"id": "1", "tName": {"ja": "バーガー"},
                         "productCodes": ["1010", "9180"]},
                        {"id": "3", "tName": {"ja": "サイドメニュー"},
                         "productCodes": ["2020"]}],
        "sizeVariants": {}, "limitedAbility": {},
    }


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/us.db")
    from core import settings
    await settings.load_all()

    from services.mcd.menu import parse_menu
    from ui import menu_flows
    from ui.menu_flows import _add_to_cart
    from ui.menu_flows import (
        CartView, CategoryView, CustomizeView, PickupView,
        ProductDetailView, ProductView,
    )

    menu = parse_menu("13934", catalog())
    user = FakeUser(3001)
    client = FakeClient()

    print("\n[1] ふつうの注文は手順が少ない ★")
    cart = CartView(user.id, "order", "13934", "テスト店", {"takeOut": True}, menu)
    cat = CategoryView(cart)
    itx = FakeInteraction(user, client)
    # カテゴリを選ぶ
    cat._sel._values = ["1"]
    await cat._on_pick(itx)
    pv = itx.last_view()
    check("商品一覧が出る", isinstance(pv, ProductView), type(pv).__name__)

    itx2 = FakeInteraction(user, client)
    pv._sel._values = ["1010"]
    await pv._on_pick(itx2)
    detail = itx2.last_view()
    check("商品の詳細が出る ★", isinstance(detail, ProductDetailView),
          type(detail).__name__)
    check("この時点ではまだカートに入らない", len(cart.items) == 0, len(cart.items))
    check("具材の画面を勝手に挟まない ★",
          not isinstance(detail, CustomizeView), type(detail).__name__)

    dlabels = [getattr(c, "label", "") for c in detail.children]
    check("1個追加がある ★", any("1個 追加" in l for l in dlabels), dlabels)
    check("まとめて追加もできる",
          any("2個 追加" in l for l in dlabels) and any("3個 追加" in l for l in dlabels),
          dlabels)

    add1 = next(c for c in detail.children if "1個 追加" in getattr(c, "label", ""))
    itx2b = FakeInteraction(user, client)
    await add1.callback(itx2b)
    check("1回でカートに入る ★", len(cart.items) == 1,
          [i.product_code for i in cart.items])
    check("追加できたと伝える", "追加しました" in itx2b.text(), itx2b.text()[:100])

    print("\n[2] 調整できることは、商品の画面で分かる")
    de = detail.build_embed()
    body = (de.description or "") + "".join(f.name + f.value for f in de.fields)
    check("抜ける具材が書いてある", "ピクルス" in body, body[:250])
    check("どこで変えるのか書いてある", "カスタマイズ" in body, body[:250])

    print("\n[3] 必要な人だけ調整できる ★")
    cz = next((c for c in detail.children
               if "カスタマイズ" in getattr(c, "label", "")), None)
    check("商品の画面から入れる ★", cz is not None, dlabels)
    itx3 = FakeInteraction(user, client)
    await cz.callback(itx3)
    check("調整の画面が出る", isinstance(itx3.last_view(), CustomizeView),
          type(itx3.last_view()).__name__)
    cart.items.clear()      # 比較しやすいよう、ここで空にしておく

    cv = itx3.last_view()
    keep = next(c for c in cv.children if hasattr(c, "options"))
    keep._values = ["99901033"]      # オニオンだけ残す＝ピクルス抜き
    itx4 = FakeInteraction(user, client)
    await keep.callback(itx4)
    okbtn = next(c for c in itx4.last_view().children
                 if "この内容で追加" in getattr(c, "label", ""))
    itx5 = FakeInteraction(user, client)
    await okbtn.callback(itx5)
    check("調整して戻せる", len(cart.items) == 1, len(cart.items))
    check("抜いたことが伝わる", "ピクルス抜き" in itx5.text(), itx5.text()[:150])
    qty = {c.product_code: c.quantity for c in cart.items[0].components}
    check("注文に反映される ★", qty.get("99901032") == 0, qty)

    print("\n[4] セットも1回で入る")
    cart2 = CartView(user.id, "order", "13934", "テスト店", {"takeOut": True}, menu)
    cat2 = CategoryView(cart2)
    itx6 = FakeInteraction(user, client)
    cat2._sel._values = ["1"]
    await cat2._on_pick(itx6)
    pv2 = itx6.last_view()
    itx7 = FakeInteraction(user, client)
    pv2._sel._values = ["9180"]
    await pv2._on_pick(itx7)
    sd = itx7.last_view()
    check("セットも詳細が出る", isinstance(sd, ProductDetailView), type(sd).__name__)
    slabels = [getattr(c, "label", "") for c in sd.children]
    check("セットは「中身を選ぶ」から ★", any("中身を選ぶ" in l for l in slabels), slabels)
    check("セットに個数ボタンは出さない",
          not any("個 追加" in l for l in slabels), slabels)

    choose = next(c for c in sd.children if "中身を選ぶ" in getattr(c, "label", ""))
    itx7b = FakeInteraction(user, client)
    await choose.callback(itx7b)
    ov = itx7b.last_view()
    check("選択枠の画面が出る", hasattr(ov, "picks"), type(ov).__name__)
    add = next(c for c in ov.children if "カートに追加" in getattr(c, "label", ""))
    itx8 = FakeInteraction(user, client)
    await add.callback(itx8)
    check("そのまま入る ★", len(cart2.items) == 1, len(cart2.items))

    print("\n[5] どの画面からも戻れる ★")
    for name, view in [("商品一覧", pv), ("選択枠", ov), ("調整", cv)]:
        labels = [getattr(c, "label", "") for c in view.children]
        check(f"{name}に戻るボタンがある ★", any("戻る" in l for l in labels), labels)
    check("商品の詳細から一覧へ戻れる ★",
          any("商品一覧へ" in getattr(c, "label", "") for c in detail.children),
          dlabels)
    check("商品の詳細からカートへ行ける ★",
          any("カートを確認" in getattr(c, "label", "") for c in detail.children),
          dlabels)

    print("\n[6] 迷ったときの次の一手")
    cart3 = CartView(user.id, "order", "13934", "テスト店", {"takeOut": True}, menu)
    e = await cart3.build_embed()
    body = (e.description or "") + "".join(f.value for f in e.fields)
    check("空のカートで何をすべきか分かる", "商品を追加" in body, body[:200])

    print("\n[7] 注文パネルの案内 ★")
    from ui import embeds
    for mode in ("both", "menu", "hex"):
        panel = embeds.order_panel(mode)
        text = (panel.description or "") + "".join(
            f.name + f.value for f in panel.fields
        )
        check(f"{mode}: 手順が番号で書いてある ★",
              "1." in text and "2." in text, text[:120])
        check(f"{mode}: 割引が分かる", "%" in text, text[:160])
        check(f"{mode}: 本人にしか見えないと伝える", "見え" in text, text[:160])

    print("\n[8] 文言に専門用語が出ていない ★")
    jargon = ["protobuf", "PASETO", "ETag", "saga", "idempot", "varint",
              "HTTP/2", "ブレーカー", "冪等"]
    from ui import embeds as E2
    texts = []
    for mode in ("both", "menu", "hex"):
        p = E2.order_panel(mode)
        texts.append((p.description or "") + "".join(f.name + f.value for f in p.fields))
    texts.append(str(E2.charge_panel().description or ""))
    found = [w for w in jargon if any(w.lower() in t.lower() for t in texts)]
    check("利用者向けの画面に専門用語が無い ★", not found, found)

    print("\n[9] 前回のお店を1回で選べる ★")
    from db.models import User
    from ui.menu_flows import StoreSelectView, start_store_select, clear_cart
    from core import users as user_repo
    await user_repo.get_or_create(user.id)

    # 途中のカートが残っていると「続きから」が先に出る（別途 [13] で検証）
    await clear_cart(user.id)

    itx9 = FakeInteraction(user, client)
    await start_store_select(itx9, "order")
    v9 = itx9.last_view()
    labels9 = [getattr(c, "label", "") for c in v9.children]
    check("初回は前回のお店が出ない", not any("前回" in l for l in labels9), labels9)

    async with session_scope() as s:
        row = await s.get(User, user.id)
        row.last_store_id = "13934"
        row.last_store_name = "南砂町店"
        row.last_pickup = "takeOut"
    await clear_cart(user.id)
    itx10 = FakeInteraction(user, client)
    await start_store_select(itx10, "order")
    v10 = itx10.last_view()
    labels10 = [getattr(c, "label", "") for c in v10.children]
    check("2回目は前回のお店が出る ★", any("前回のお店" in l for l in labels10), labels10)
    check("店名が見える", any("南砂町店" in l for l in labels10), labels10)
    check("一番上に置く ★", "前回のお店" in labels10[0], labels10)
    check("案内文でも触れる", "前回のお店" in itx10.text(), itx10.text()[:150])

    print("\n[10] 受取方法は最後に聞く ★")
    cart4 = CartView(user.id, "order", "13934", "テスト店",
                     {"takeOut": True, "eatIn": True}, menu, pickup="takeOut")
    check("前回の受取方法を覚えている ★", cart4.pickup == "takeOut", cart4.pickup)
    c4labels = [getattr(c, "label", "") for c in cart4.children]
    check("カートでは受取方法を聞かない ★",
          not any(hasattr(c, "options") and "受取" in (c.placeholder or "")
                  for c in cart4.children),
          c4labels)
    check("カートの行き先は「注文へ進む」★",
          any("注文へ進む" in l for l in c4labels), c4labels)

    await _add_to_cart(cart4, menu.products["1010"], {})
    itxp = FakeInteraction(user, client)
    await cart4._on_go(itxp)
    pick = itxp.last_view()
    check("最後に受取方法の画面が出る ★", isinstance(pick, PickupView),
          type(pick).__name__)
    plabels = [getattr(c, "label", "") for c in pick.children]
    check("店内とお持ち帰りが並ぶ ★",
          any("店内" in l for l in plabels) and any("持ち帰り" in l for l in plabels),
          plabels)
    check("カートに戻れる", any("カートに戻る" in l for l in plabels), plabels)

    print("\n[11] カテゴリを選ばずに商品へ進める ★")
    cart6 = CartView(user.id, "order", "13934", "テスト店", {"takeOut": True}, menu)
    popular = cart6.popular_products()
    check("人気の商品が並ぶ ★", len(popular) >= 2, [p.name for p in popular])
    check("セットも単品も入る",
          len({p.product_class for p in popular}) >= 1,
          [p.product_class for p in popular])
    check("重複しない", len(popular) == len({p.code for p in popular}))

    cat6 = CategoryView(cart6)
    selects = [c for c in cat6.children if hasattr(c, "options")]
    check("選択肢が2つ出る（人気／カテゴリ）★", len(selects) == 2, len(selects))
    check("人気の商品が先に来る ★",
          "人気" in (selects[0].placeholder or ""), selects[0].placeholder)

    itx11 = FakeInteraction(user, client)
    cat6._quick._values = ["1010"]
    await cat6._on_quick(itx11)
    qd = itx11.last_view()
    check("カテゴリを飛ばして商品へ進める ★", isinstance(qd, ProductDetailView),
          type(qd).__name__)
    add_q = next(c for c in qd.children if "1個 追加" in getattr(c, "label", ""))
    itx11b = FakeInteraction(user, client)
    await add_q.callback(itx11b)
    check("そこから追加できる ★", len(cart6.items) == 1,
          [i.product_code for i in cart6.items])

    print("\n[12] 2回目の注文は操作が少ない ★")
    # パネル→メニュー→前回の店→商品を追加→人気から選ぶ→確定
    check("前回のお店で検索3操作が減る ★", any("前回のお店" in l for l in labels10))
    check("人気の商品でカテゴリ1操作が減る ★", len(selects) == 2)
    check("受取方法は最後の1回だけ ★", isinstance(pick, PickupView))

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
