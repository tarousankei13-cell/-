"""
商品の詳細画面と、受取方法を最後に聞く流れの検証

実物のカタログ（tests/m13934.json・247商品）をそのまま使い、
実際のマクドナルドのアプリと同じ情報が出ているかを確かめる。
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


def text_of(embed) -> str:
    return (
        (embed.title or "") + (embed.description or "")
        + "".join(f"{f.name}{f.value}" for f in embed.fields)
        + (embed.footer.text or "" if embed.footer else "")
    )


async def main():
    # ⚠️ 時刻を固定する。実データの商品には販売時間帯があり
    #    （ハンバーガーは 10:20〜23:50）、朝に回すとカートが正しく
    #    止めてしまう。固定しないと「毎朝だけ落ちるテスト」になる。
    import config as _cfg
    from datetime import datetime as _dt

    _cfg.now_jst = lambda: _dt(2026, 10, 3, 12, 0, tzinfo=_cfg.JST)

    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/pd.db")
    from core import settings, users as user_repo
    await settings.load_all()

    from services.mcd.menu import parse_menu, Display, KIND_LABEL
    from ui.menu_flows import (
        CartView, CustomizeView, OptionView, PickupView,
        ProductDetailView, ProductView, _add_to_cart, cart_lines, reprice,
    )

    raw = json.load(open(os.path.join(HERE, "m13934.json")))
    menu = parse_menu("13934", raw)
    user = FakeUser(4001)
    await user_repo.get_or_create(user.id)
    client = FakeClient()

    def new_cart(**kw):
        return CartView(
            user.id, "order", "13934", "南砂町店",
            {"takeOut": True, "eatIn": True}, menu, **kw
        )

    # ========================================================
    print("\n[1] 実物のカタログから情報を取りこぼしていないか")
    total = len(menu.products)
    with_img = sum(1 for p in menu.products.values() if p.display.image_url)
    with_desc = sum(1 for p in menu.products.values() if p.display.description)
    check(f"全{total}商品に画像がある", with_img == total, f"{with_img}/{total}")
    check("半数以上に説明文がある", with_desc > total // 2, f"{with_desc}/{total}")

    # 公式のアレルギー・栄養ページは単品にだけ用意されている。
    # セットは detailUrl.valid が false なので、リンクを作ってはいけない。
    singles = [p for p in menu.products.values() if p.product_class == "PRODUCT"]
    linked = [p for p in singles if p.display.detail_url]
    meals = [p for p in menu.products.values() if p.product_class == "VALUE_MEAL"]
    check("単品にはほぼ公式ページがある", len(linked) > len(singles) * 0.9,
          f"{len(linked)}/{len(singles)}")
    check("セットには無いので作らない ★",
          not any(p.display.detail_url for p in meals),
          [p.name for p in meals if p.display.detail_url][:3])
    check("リンクはすべて公式ドメイン ★",
          all(p.display.detail_url.startswith("https://www.mcdonalds.co.jp/")
              for p in linked),
          [p.display.detail_url for p in linked[:2]])
    check("期間限定を見分けられる",
          any(p.display.limited for p in menu.products.values()))
    check("種別（バーガー/サイド/ドリンク）が入る",
          len({p.display.kind for p in menu.products.values()}) >= 3,
          {p.display.kind for p in menu.products.values()})

    tsukimi = menu.products["1567"]
    check("商品名が正しい", tsukimi.name == "チーズ月見", tsukimi.name)
    check("説明文が本物", "チェダーチーズ" in tsukimi.display.description,
          tsukimi.display.description[:60])
    check("画像が公式のもの",
          tsukimi.display.image_url.startswith("https://www.mcdonalds.co.jp/"),
          tsukimi.display.image_url)

    # ========================================================
    print("\n[2] 商品の詳細画面に、アプリと同じ情報が並ぶ")
    cart = new_cart()
    view = ProductDetailView(cart, tsukimi)
    e = view.build_embed()
    body = text_of(e)
    check("商品名", "チーズ月見" in body, body[:80])
    check("説明文", "チェダーチーズ" in body, body[:200])
    check("画像を出す", e.thumbnail is not None and e.thumbnail.url, e.thumbnail)
    check("期間限定だと分かる", "期間限定" in body, body[:80])
    check("販売期間を出す", "2026/08/18" in body, body[:400])
    check("価格", "¥" in body, body[:200])
    check("注意書き", "スライスチーズ" in body or "価格が異なります" in body, body[:600])
    check("変更できる具材を挙げる", "変更できるもの" in body, body[:400])

    labels = [getattr(c, "label", "") for c in view.children]
    check("個数ボタンが3つ", sum("個 追加" in l for l in labels) == 3, labels)
    check("カスタマイズがある", any("カスタマイズ" in l for l in labels), labels)
    check("商品一覧へ戻れる", any("商品一覧へ" in l for l in labels), labels)
    check("カートへ行ける", any("カートを確認" in l for l in labels), labels)
    check("公式のアレルギー情報へ飛べる",
          any(getattr(c, "url", None) for c in view.children), labels)

    # ========================================================
    print("\n[3] 価格は実データどおりに出す")
    # ⚠️ 実データ（247商品）では店内とお持ち帰りが**全部同額**だった。
    #    値段が違うのはデリバリーだけ（202/247商品）。
    #    「店内は10%・持ち帰りは8%」という思い込みで2行出すと、
    #    同じ金額が2行並んで読みにくいだけになる。
    diff = [p for p in menu.products.values()
            if p.price_for("eatIn") != p.price_for("takeOut")]
    check("実データでは店内とお持ち帰りが同額 ★", not diff,
          [(p.name, p.price_eatin, p.price_takeout) for p in diff[:3]])
    deli = [p for p in menu.products.values()
            if p.price_for("addressDelivery") != p.price_for("takeOut")]
    check("デリバリーだけ値段が違う", len(deli) > 100, len(deli))

    same = next(p for p in menu.products.values()
                if p.price_for("eatIn") == p.price_for("takeOut"))
    t2 = text_of(ProductDetailView(cart, same).build_embed())
    check("同額なら1行にまとめる ★", "店内・お持ち帰り" in t2, t2[:220])
    check("同じ金額を2行並べない ★", t2.count(f"¥{same.price_for('eatIn'):,}") == 1,
          t2[:220])

    # 店舗や時期で変わりうるので、違うときは両方出せること自体は確かめる
    from services.mcd.menu import Product as P
    split = P(code="zz", name="値段が違う商品", price_eatin=520, price_takeout=510)
    t3 = text_of(ProductDetailView(cart, split).build_embed())
    check("違えば両方出す", "¥520" in t3 and "¥510" in t3, t3[:250])

    # ========================================================
    print("\n[4] 単品でも具材を変えられる（以前は入口が無かった）")
    burger = menu.products["1010"]      # ハンバーガー（選択枠なし）
    check("選択枠を持たない商品", not burger.slots_of("choices"))
    check("でも具材は変えられる", len(burger.customizations()) > 0,
          [s.name for s in burger.customizations()])

    dv = ProductDetailView(cart, burger)
    cz = next((c for c in dv.children
               if "カスタマイズ" in getattr(c, "label", "")), None)
    check("詳細画面にカスタマイズがある ★", cz is not None,
          [getattr(c, "label", "") for c in dv.children])

    itx = FakeInteraction(user, client)
    await cz.callback(itx)
    cv = itx.last_view()
    check("調整画面が開く", isinstance(cv, CustomizeView), type(cv).__name__)
    names = [s.name for s in cv.slots]
    check("ピクルスを抜ける", "ピクルス" in names, names)

    keep = next(c for c in cv.children if hasattr(c, "options"))
    keep._values = [o.value for o in keep.options if o.label != "ピクルス"]
    itx2 = FakeInteraction(user, client)
    await keep.callback(itx2)
    okbtn = next(c for c in itx2.last_view().children
                 if "この内容で追加" in getattr(c, "label", ""))
    itx3 = FakeInteraction(user, client)
    await okbtn.callback(itx3)
    check("カートに入る", len(cart.items) == 1, len(cart.items))
    qty = {c.product_code: c.quantity for c in cart.items[0].components}
    pickle_code = next(s.code for s in burger.customizations() if s.name == "ピクルス")
    check("注文に「ピクルス抜き」が入る ★", qty.get(pickle_code) == 0, qty)
    check("利用者にも伝わる", "ピクルス抜き" in itx3.text(), itx3.text()[:150])

    # ========================================================
    print("\n[5] まとめて追加できる")
    cart2 = new_cart()
    dv2 = ProductDetailView(cart2, burger)
    add3 = next(c for c in dv2.children if "3個 追加" in getattr(c, "label", ""))
    itx4 = FakeInteraction(user, client)
    await add3.callback(itx4)
    check("3個入る", len(cart2.items) == 3, len(cart2.items))
    lines = cart_lines(menu, cart2.items, "takeOut")
    check("カートでは「×3」とまとめる ★", len(lines) == 1 and "×3" in lines[0], lines)
    check("金額も3個ぶん",
          f"¥{burger.price_for('takeOut') * 3:,}" in lines[0], lines)
    check("伝える文にも個数が出る", "×3" in itx4.text(), itx4.text()[:120])

    # ========================================================
    print("\n[6] セットは中身を選んでから")
    meal = next(p for p in menu.products.values()
                if p.product_class == "VALUE_MEAL" and p.slots_of("choices"))
    cart3 = new_cart()
    dv3 = ProductDetailView(cart3, meal)
    l3 = [getattr(c, "label", "") for c in dv3.children]
    check("「中身を選ぶ」になる", any("中身を選ぶ" in l for l in l3), l3)
    check("個数ボタンは出さない", not any("個 追加" in l for l in l3), l3)
    e3 = dv3.build_embed()
    check("セットの内容を見せる", "セットの内容" in text_of(e3), text_of(e3)[:300])
    check("リンクが無い商品にボタンを作らない ★",
          not any(getattr(c, "url", None) for c in dv3.children)
          if not meal.display.detail_url else True,
          meal.display.detail_url)

    itx5 = FakeInteraction(user, client)
    await next(c for c in dv3.children if "中身を選ぶ" in getattr(c, "label", "")).callback(itx5)
    check("選択枠の画面へ", isinstance(itx5.last_view(), OptionView),
          type(itx5.last_view()).__name__)

    # ========================================================
    print("\n[7] 受取方法は最後に聞く ★")
    cart4 = new_cart(pickup="takeOut")
    check("カートに受取方法の選択肢は無い ★",
          not any(hasattr(c, "options") for c in cart4.children),
          [type(c).__name__ for c in cart4.children])
    ce = await cart4.build_embed()
    check("カートに「未選択」の警告を出さない", "未選択" not in text_of(ce), text_of(ce))
    check("あとで聞くと伝える", "お受け取り" in text_of(ce) or not cart4.items,
          text_of(ce)[:200])

    await _add_to_cart(cart4, burger, {})
    itx6 = FakeInteraction(user, client)
    await cart4._on_go(itx6)
    pv = itx6.last_view()
    check("受取方法の画面が出る ★", isinstance(pv, PickupView), type(pv).__name__)
    pl = [getattr(c, "label", "") for c in pv.children]
    check("店内でお召し上がり", any("店内でお召し上がり" in l for l in pl), pl)
    check("お持ち帰り", any("お持ち帰り" in l for l in pl), pl)
    check("カートに戻れる", any("カートに戻る" in l for l in pl), pl)
    pe = pv.build_embed()
    check("何を頼むのか見える", "ハンバーガー" in text_of(pe), text_of(pe)[:200])
    check("店舗も見える", "南砂町店" in text_of(pe), text_of(pe)[:200])

    # ========================================================
    print("\n[8] 選んだ受取方法で金額が入り直る ★")
    # 受取方法はカートを作る時点では決まっていないので、
    # 最後に選んだところで金額を入れ直す必要がある。
    # 実データの店内/持ち帰りは同額なので、差の出るデリバリー価格で確かめる。
    pricey = next(p for p in menu.products.values()
                  if p.price_for("addressDelivery") != p.price_for("takeOut"))
    cart5 = new_cart()
    await _add_to_cart(cart5, pricey, {})
    check("カートに入れた時点では持ち帰りの金額",
          cart5.items[0].amount == pricey.price_for("takeOut"),
          cart5.items[0].amount)
    reprice(menu, cart5.items, "addressDelivery")
    check("受取方法を変えると金額が入り直る ★",
          cart5.items[0].amount == pricey.price_for("addressDelivery"),
          (cart5.items[0].amount, pricey.price_for("addressDelivery")))
    reprice(menu, cart5.items, "takeOut")
    check("戻せば元の金額 ★",
          cart5.items[0].amount == pricey.price_for("takeOut"),
          cart5.items[0].amount)
    check("合計も受取方法ごとに出る",
          cart5.total("addressDelivery") != cart5.total("takeOut"),
          (cart5.total("addressDelivery"), cart5.total("takeOut")))

    # ========================================================
    print("\n[9] 受取方法を押すと注文コードができる ★")
    cart6 = new_cart()
    await _add_to_cart(cart6, burger, {})
    cart6.purpose = "hex"
    itx7 = FakeInteraction(user, client)
    await cart6._on_go(itx7)
    pv6 = itx7.last_view()
    eat = next(c for c in pv6.children if "店内でお召し上がり" in getattr(c, "label", ""))
    itx8 = FakeInteraction(user, client)
    await eat.callback(itx8)
    out = itx8.text()
    check("注文コードが出る", "注文コード" in out, out[:150])
    check("選んだ受取方法が反映される ★", "店内でお召し上がり" in out, out[:300])

    async with session_scope() as s:
        from db.models import User
        row = await s.get(User, user.id)
    check("次回のために覚える ★", row.last_pickup == "eatIn", row.last_pickup)

    # ========================================================
    print("\n[10] 表示名が実物のアプリと同じ ★")
    from services.mcd.protocol import PICKUP_LABEL
    check("店内でお召し上がり", PICKUP_LABEL["eatIn"] == "店内でお召し上がり",
          PICKUP_LABEL["eatIn"])
    check("お持ち帰り", PICKUP_LABEL["takeOut"] == "お持ち帰り", PICKUP_LABEL["takeOut"])
    check("設定と食い違わない ★",
          all(config.PICKUP_METHODS[k]["label"] == v for k, v in PICKUP_LABEL.items()),
          PICKUP_LABEL)

    # ========================================================
    print("\n[11] 値段を ¥0 と出さない（お金の事故を防ぐ）")
    from services.mcd.menu import Product
    p0 = Product(code="x", name="片方だけ", price_takeout=170)
    check("店内価格が無ければ持ち帰り価格で代える ★", p0.price_for("eatIn") == 170,
          p0.price_for("eatIn"))
    zero = [p.name for p in menu.products.values()
            if p.price_for("eatIn") <= 0 or p.price_for("takeOut") <= 0]
    check("実データに ¥0 の商品が無い", not zero, zero[:5])

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
