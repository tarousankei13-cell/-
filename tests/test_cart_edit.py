"""
カートの編集と「続きから」の検証

・途中のカートを15分以内なら拾い直せるか
・1品ずつ削除・個数の増減ができるか（表示と実体がずれないか）
・セットの中身が何の枠か分かるか
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


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/ce.db")
    from core import settings, users as user_repo
    await settings.load_all()

    from services.mcd.menu import parse_menu
    from services.mcd.protocol import OrderItem
    from ui.menu_flows import (
        CartView, CustomizeView, ResumeCartView, SavedCart,
        _add_to_cart, cart_lines, clear_cart, group_items, load_cart, save_cart,
        start_store_select,
    )

    menu = parse_menu("13934", json.load(open(os.path.join(HERE, "m13934.json"))))
    user = FakeUser(6001)
    await user_repo.get_or_create(user.id)
    client = FakeClient()
    burger = menu.products["1010"]          # ハンバーガー（具材あり）
    cheese = menu.products["1020"]          # チーズバーガー

    def new_cart(**kw):
        return CartView(user.id, "order", "13934", "南砂町店",
                        {"takeOut": True, "eatIn": True}, menu, **kw)

    # ========================================================
    print("\n[1] 保存したカートを読み戻せる ★")
    await clear_cart(user.id)
    check("空なら None", await load_cart(user.id) is None)

    await save_cart(user.id, purpose="order", store_id="13934", pickup=None,
                    items=[OrderItem(product_code="1010", amount=190)])
    saved = await load_cart(user.id)
    check("読み戻せる", saved is not None and len(saved.items) == 1, saved)
    check("お店も覚えている", saved.store_id == "13934", saved.store_id)
    check("直後なら拾える ★", saved.resumable, saved.age_minutes)

    print("\n[2] 拾える条件（中身・お店・新しさ）")
    check(f"{config.CART_RESUME_MINUTES}分を過ぎたら拾わない ★",
          not SavedCart("order", "13934", None, saved.items,
                        config.CART_RESUME_MINUTES + 0.1).resumable)
    check("ちょうど15分なら拾える",
          SavedCart("order", "13934", None, saved.items,
                    float(config.CART_RESUME_MINUTES)).resumable)
    check("中身が空なら拾わない",
          not SavedCart("order", "13934", None, [], 1.0).resumable)
    check("お店が無ければ拾わない",
          not SavedCart("order", "", None, saved.items, 1.0).resumable)

    print("\n[3] 注文を始めると「続きから」を聞く ★")
    itx = FakeInteraction(user, client)
    await start_store_select(itx, "order")
    v = itx.last_view()
    check("続きから聞く画面が出る ★", isinstance(v, ResumeCartView), type(v).__name__)
    labels = [getattr(c, "label", "") for c in v.children]
    check("「続きから」がある", any("続きから" in l for l in labels), labels)
    check("「最初から」も選べる ★", any("最初から" in l for l in labels), labels)
    body = itx.text()
    check("お店の名前が出る", "13934" in body or "南砂" in body, body[:150])

    print("\n[4] 用途が違えば拾わない（注文 と コード作成）★")
    itx2 = FakeInteraction(user, client)
    await start_store_select(itx2, "hex")
    check("別の用途では聞かない ★",
          not isinstance(itx2.last_view(), ResumeCartView),
          type(itx2.last_view()).__name__)

    print("\n[5] 「最初から」を押したらカートを捨てる ★")
    fresh = next(c for c in v.children if "最初から" in getattr(c, "label", ""))
    itx3 = FakeInteraction(user, client)
    await fresh.callback(itx3)
    check("保存してあったカートが消える ★", await load_cart(user.id) is None)

    # ========================================================
    print("\n[6] 同じ内容のまとめ方が、表示と中身で一致する ★")
    cart = new_cart()
    for _ in range(3):
        await _add_to_cart(cart, burger, {})
    await _add_to_cart(cart, cheese, {})
    groups = group_items(cart.items)
    check("2行にまとまる", len(groups) == 2, [(g.item.product_code, g.count) for g in groups])
    check("1行目が3個", groups[0].count == 3, groups[0].count)
    check("位置も持っている ★", groups[0].indexes == [0, 1, 2], groups[0].indexes)
    lines = cart_lines(menu, cart.items)
    check("表示も2行", len([l for l in lines if l.startswith("**")]) == 2, lines)
    check("×3 と出る", "×3" in lines[0], lines[0])

    print("\n[7] 1個ずつ減らせる ★")
    cart.selected = 0
    cart._build()
    itx4 = FakeInteraction(user, client)
    await cart._on_minus(itx4)
    check("3個 → 2個 ★", group_items(cart.items)[0].count == 2,
          [g.count for g in group_items(cart.items)])
    check("他の商品は残る", len(cart.items) == 3, len(cart.items))

    print("\n[8] 1個ずつ増やせる ★")
    itx5 = FakeInteraction(user, client)
    await cart._on_plus(itx5)
    check("2個 → 3個 ★", group_items(cart.items)[0].count == 3,
          [g.count for g in group_items(cart.items)])
    a, b = cart.items[0], cart.items[1]
    check("別の実体として増える ★", a is not b)
    check("中身は同じ", a.to_dict() == b.to_dict())

    print("\n[9] 選んだ商品だけまとめて削除できる ★")
    itx6 = FakeInteraction(user, client)
    await cart._on_drop(itx6)
    rest = group_items(cart.items)
    check("3個まとめて消える ★", len(cart.items) == 1, len(cart.items))
    check("消えたのは選んだ行だけ ★", rest[0].item.product_code == "1020",
          rest[0].item.product_code)
    check("選択は外れる", cart.selected is None, cart.selected)

    print("\n[10] 選んでいないときは押せない ★")
    cart._build()
    disabled = {
        getattr(c, "label", ""): getattr(c, "disabled", False)
        for c in cart.children if not hasattr(c, "options")
    }
    check("減らすが押せない ★", disabled.get("1個 減らす") is True, disabled)
    check("増やすが押せない ★", disabled.get("1個 増やす") is True, disabled)
    check("削除が押せない ★", disabled.get("この商品を削除") is True, disabled)

    print("\n[11] 選ぶと押せるようになる")
    cart.selected = 0
    cart._build()
    d2 = {getattr(c, "label", ""): getattr(c, "disabled", False)
          for c in cart.children if not hasattr(c, "options")}
    check("押せるようになる", d2.get("この商品を削除") is False, d2)
    czb = [l for l in d2 if "具材を変える" in l]
    check("選んだ商品の具材を変えられる ★", czb, list(d2))

    print("\n[12] 具材を変えるとき、他の個数を巻き添えにしない ★")
    cart2 = new_cart()
    for _ in range(3):
        await _add_to_cart(cart2, burger, {})
    cart2.selected = 0
    itx7 = FakeInteraction(user, client)
    await cart2._on_customize_selected(itx7)
    check("調整画面が出る", isinstance(itx7.last_view(), CustomizeView),
          type(itx7.last_view()).__name__)
    check("取り出すのは1個だけ ★", len(cart2.items) == 2, len(cart2.items))

    print("\n[13] セットの中身が何の枠か分かる ★")
    meal = next(p for p in menu.products.values()
                if p.product_class == "VALUE_MEAL" and p.slots_of("choices"))
    cart3 = new_cart()
    await _add_to_cart(cart3, meal, {})
    mlines = cart_lines(menu, cart3.items)
    subs = [l for l in mlines if l.startswith("　└")]
    check("中身が並ぶ", subs, mlines)
    check("枠の名前が付く ★", any("：" in l for l in subs), subs)

    print("\n[14] 上限を超えて入らない ★")
    cart4 = new_cart()
    for _ in range(config.CART_MAX_ITEMS):
        await _add_to_cart(cart4, burger, {})
    check(f"{config.CART_MAX_ITEMS} 点まで入る", len(cart4.items) == config.CART_MAX_ITEMS)
    added = await _add_to_cart(cart4, burger, {})
    check("それ以上は入らない ★", added is False and len(cart4.items) == config.CART_MAX_ITEMS,
          len(cart4.items))
    cart4.selected = 0
    itx8 = FakeInteraction(user, client)
    await cart4._on_plus(itx8)
    check("増やすボタンでも超えない ★", len(cart4.items) == config.CART_MAX_ITEMS,
          len(cart4.items))
    check("理由を伝える", f"{config.CART_MAX_ITEMS} 点" in itx8.text(),
          itx8.text()[:100])

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
