"""
画面が作れること（全商品・全画面）

⚠️ Discord のビューは、**部品を置けないと作る時点で例外になる**。
   例外が出ると応答を返せず、利用者には
   「BOTは時間内に応答しませんでした」としか見えない。
   何が悪いのか誰にも分からないので、ここで必ず捕まえる。

⚠️ **行の決まり**
     ・1画面は5行（row 0〜4）
     ・Select は1つで1行を丸ごと使う（幅5）
     ・Button は幅1。1行に5個まで
   「Select がある行に Button を置く」と ValueError になる。
   実際、確定ボタンを row=3 に決め打ちしていたため、
   枠が4つあるセット（ハッピーセットなど）**42商品**が
   注文できなくなっていた。

⚠️ 選んだ**あと**にも作り直されるので、両方を試すこと。
   サイズ選択が増えて行数が変わり、選ぶまで落ちない商品があった。
"""
import asyncio, json, os, sys, traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord

from services.mcd.menu import parse_menu

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


HERE = os.path.dirname(os.path.abspath(__file__))
pm = parse_menu("13934", json.load(open(os.path.join(HERE, "m13934.json"))))

TIMES = {"朝 8:00": 8 * 60, "昼 13:00": 13 * 60, "夜 20:00": 20 * 60}


class FakeCart:
    """CartView の代わり。画面を作るのに要るものだけ持つ。"""

    def __init__(self):
        self.menu = pm
        self.pickup = "takeOut"
        self.owner_id = 1
        self.store_id = "13934"
        self.store_name = "テスト店"
        self.purpose = "order"
        self.items = []
        self.active_dayparts = set()
        self.supported = {"takeOut": True, "eatIn": True}
        self.selected = None

    def popular_products(self, limit=20):
        from ui.menu_flows import CartView
        return CartView.popular_products(self, limit)


def rows_of(view) -> dict[int, int]:
    """行ごとの使用幅。Select は5、Button は1。"""
    out = {}
    for item in view.children:
        r = item.row if item.row is not None else 0
        w = 5 if isinstance(item, discord.ui.Select) else 1
        out[r] = out.get(r, 0) + w
    return out


def main():
    from ui import menu_flows as mf

    print(f"商品数 {len(pm.products)}")

    # ========================================================
    print("\n[ 選択枠の画面（セットの中身を選ぶところ）]")
    # ========================================================
    broken, broken_after = {}, {}
    checked = 0
    for mins in TIMES.values():
        for code, p in pm.products.items():
            if not pm.orderable(p, mins):
                continue
            checked += 1
            cart = FakeCart()
            try:
                v = mf.OptionView(cart, p, p.slots_of("choices"))
            except Exception as e:
                broken[(code, p.name)] = f"{type(e).__name__}: {e}"
                continue
            # 選んだあとの作り直し（ここで落ちる商品があった）
            try:
                for c in v.choices:
                    cands = v._candidates(c)
                    if cands:
                        v.picks[v._key_of(c)] = cands[0].code
                v._build()
            except Exception as e:
                broken_after[(code, p.name)] = f"{type(e).__name__}: {e}"
                continue
            # 行の決まりを守れているか
            over = {r: w for r, w in rows_of(v).items() if w > 5}
            if over:
                broken_after[(code, p.name)] = f"行があふれています {over}"

    check(f"開いた時点で落ちる商品がない ★（{checked}通り確認）", not broken,
          "\n".join(f"      {c} {n}: {e}" for (c, n), e in list(broken.items())[:10]))
    check("選んだあとに落ちる商品がない ★", not broken_after,
          "\n".join(f"      {c} {n}: {e}"
                    for (c, n), e in list(broken_after.items())[:10]))

    # ========================================================
    print("\n[ 行の使い方が決まりを守れているか ]")
    # ========================================================
    # 枠が多い商品（ハッピーセット）で、実際の並びを確かめる
    happy = pm.products["9005"]
    v = mf.OptionView(FakeCart(), happy, happy.slots_of("choices"))
    used = rows_of(v)
    sel_rows = {i.row for i in v.children if isinstance(i, discord.ui.Select)}
    btn_rows = {i.row for i in v.children if isinstance(i, discord.ui.Button)}
    print(f"      ハッピーセット: 選択枠 {len(sel_rows)} 行 / 行ごとの幅 {used}")
    check("どの行も幅5を超えない ★", all(w <= 5 for w in used.values()), used)
    check("ボタンと選択枠が同じ行に無い ★", not (sel_rows & btn_rows),
          (sel_rows, btn_rows))
    check("行は0〜4に収まる ★", all(0 <= r <= 4 for r in used), sorted(used))
    check("カートに追加のボタンがある ★",
          any(getattr(i, "label", "") == "カートに追加" for i in v.children))
    check("戻るボタンがある", any(getattr(i, "label", "") == "戻る"
                            for i in v.children))

    # ========================================================
    print("\n[ 商品詳細・具材の調整・一覧 ]")
    # ========================================================
    detail, custom = {}, {}
    for mins in TIMES.values():
        for code, p in pm.products.items():
            if not pm.orderable(p, mins):
                continue
            cart = FakeCart()
            try:
                dv = mf.ProductDetailView(cart, p)
                over = {r: w for r, w in rows_of(dv).items() if w > 5}
                if over:
                    detail[(code, p.name)] = f"行があふれています {over}"
            except Exception as e:
                detail[(code, p.name)] = f"{type(e).__name__}: {e}"
            if p.customizations():
                try:
                    cv = mf.CustomizeView(cart, p, {})
                    cv._build()
                    over = {r: w for r, w in rows_of(cv).items() if w > 5}
                    if over:
                        custom[(code, p.name)] = f"行があふれています {over}"
                except Exception as e:
                    custom[(code, p.name)] = f"{type(e).__name__}: {e}"

    check("商品詳細がすべての商品で作れる ★", not detail,
          "\n".join(f"      {c} {n}: {e}" for (c, n), e in list(detail.items())[:8]))
    check("具材の調整がすべての商品で作れる ★", not custom,
          "\n".join(f"      {c} {n}: {e}" for (c, n), e in list(custom.items())[:8]))

    cart = FakeCart()
    lists = {}
    try:
        cv = mf.CategoryView(cart)
        over = {r: w for r, w in rows_of(cv).items() if w > 5}
        if over:
            lists["カテゴリ一覧"] = f"行があふれています {over}"
    except Exception as e:
        lists["カテゴリ一覧"] = f"{type(e).__name__}: {e}"
    for col in pm.collections:
        try:
            pv = mf.ProductView(cart, col.id, 0)
            over = {r: w for r, w in rows_of(pv).items() if w > 5}
            if over:
                lists[f"商品一覧（{col.name}）"] = f"行があふれています {over}"
        except Exception as e:
            lists[f"商品一覧（{col.name}）"] = f"{type(e).__name__}: {e}"
    check("カテゴリ・商品一覧がすべて作れる ★", not lists,
          "\n".join(f"      {k}: {v}" for k, v in list(lists.items())[:8]))

    # ========================================================
    print("\n[ Discord の部品数の上限 ]")
    # ========================================================
    too_many, too_many_opts = {}, {}
    for mins in TIMES.values():
        for code, p in pm.products.items():
            if not pm.orderable(p, mins):
                continue
            try:
                v = mf.OptionView(FakeCart(), p, p.slots_of("choices"))
            except Exception:
                continue
            if len(v.children) > 25:
                too_many[(code, p.name)] = len(v.children)
            for item in v.children:
                if isinstance(item, discord.ui.Select) and len(item.options) > 25:
                    too_many_opts[(code, p.name)] = len(item.options)
    check("1画面の部品が25個を超えない ★", not too_many, too_many)
    check("選択肢が25個を超えない ★", not too_many_opts, too_many_opts)

    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
