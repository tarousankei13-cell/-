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
    from ui.menu_flows import CartView, CategoryView, CustomizeView, ProductView

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
    check("1回でカートに入る ★", len(cart.items) == 1,
          [i.product_code for i in cart.items])
    check("具材の画面を勝手に挟まない ★",
          not isinstance(itx2.last_view(), CustomizeView),
          type(itx2.last_view()).__name__)
    check("追加できたと伝える", "追加しました" in itx2.text(), itx2.text()[:100])

    print("\n[2] 調整できることは伝える")
    check("抜ける具材があると案内する",
          "ピクルス" in itx2.text() and "抜く" in itx2.text(), itx2.text()[:200])

    print("\n[3] 必要な人だけ調整できる ★")
    view = itx2.last_view()
    labels = [getattr(c, "label", "") for c in view.children]
    cz = next((c for c in view.children if "具材を変える" in getattr(c, "label", "")), None)
    check("カートから入れる ★", cz is not None, labels)
    itx3 = FakeInteraction(user, client)
    await cz.callback(itx3)
    check("調整の画面が出る", isinstance(itx3.last_view(), CustomizeView),
          type(itx3.last_view()).__name__)
    check("いったんカートから出る", len(cart.items) == 0, len(cart.items))

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
    ov = itx7.last_view()
    check("選択枠の画面が出る", hasattr(ov, "picks"), type(ov).__name__)
    add = next(c for c in ov.children if "カートに追加" in getattr(c, "label", ""))
    itx8 = FakeInteraction(user, client)
    await add.callback(itx8)
    check("そのまま入る ★", len(cart2.items) == 1, len(cart2.items))

    print("\n[5] どの画面からも戻れる ★")
    for name, view in [("商品一覧", pv), ("選択枠", ov), ("調整", cv)]:
        labels = [getattr(c, "label", "") for c in view.children]
        check(f"{name}に戻るボタンがある ★", any("戻る" in l for l in labels), labels)

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

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
