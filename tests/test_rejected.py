"""
断られた組み合わせを、相手の応答から正しく覚えるか

⚠️ マクドナルドは応答の中で **どれが駄目かを正確に教えてくれる**。

      9030 > 9997925 > 3120 product not found
      └セット  └選択枠   └商品

   かつてはこれを読まず、注文全体から疑わしいものを絞り込んでいた。
   疑いが2つ以上あると「巻き添えを避ける」ため何も覚えず、
   同じ組み合わせで何度も失敗し続けていた。

⚠️ さらに、代表商品（referenceProduct）は「必ず残す」として
   候補の**先頭に押し戻して**いた。断られた商品が代表だった場合、
   学習しても先頭に出続け、選んだ人が必ず失敗していた。
   実際 9997925 の代表は コカ・コーラ M で、これが断られていた。
"""
import asyncio, json, os, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.crypto import init_cipher
from db.session import init_db, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


HERE = os.path.dirname(os.path.abspath(__file__))

# 実際に返ってきた応答（2026-10-05・１８号安中店）
REAL_BODY = (
    b"ErrorCode_ProductValidation\x12\x08products ErrorCode_ProductValidation*\x12"
    b"(type.googleapis.com/mcdord.ProductsError\x12\x08 3120 "
    b"'9030 > 9997925 > 3120 product not found\"Q"
    + "ただいまのお時間は選択した商品のお取り扱いがありません".encode()
    + "2<選択された商品はただいま販売していません".encode()
)


async def main():
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/rj.db")
    from core import settings
    from services.mcd import errors as E
    from services.mcd import slot_rules, stores as mcd_stores
    from services.mcd.menu import parse_menu
    from ui import flows
    await settings.load_all()

    # この試験の間だけ、学習結果を別のファイルに逃がす
    tmp = tempfile.mkdtemp()
    slot_rules.STORE_PATH = __import__("pathlib").Path(tmp) / "slot_rules.json"
    slot_rules._rejected.clear(); slot_rules._confirmed.clear()
    slot_rules._loaded = True

    pm = parse_menu("13934", json.load(open(os.path.join(HERE, "m13934.json"))))
    async def fake_load(_sid):
        return pm
    mcd_stores.load_menu = fake_load

    # ========================================================
    print("\n[ 応答から『どれが駄目か』を読み取る ]")
    # ========================================================
    info = E.parse(400, REAL_BODY)
    check("種類を判別できる", info.kind == E.PRODUCT_TIME, info.kind)
    check("断られた組み合わせを取り出せる ★",
          info.rejected == [("9997925", "3120")], info.rejected)
    check("日本語の文言も取れる",
          "お取り扱いがありません" in info.message, info.message)

    # 経路が無い応答でも落ちない
    plain = E.parse(400, b"ErrorCode_Payment card declined")
    check("経路が無ければ空のまま ★", plain.rejected == [], plain.rejected)
    check("文字列でも読める",
          E.rejected_pairs("9003 > 1110 product not found") == [("9003", "1110")])
    check("経路が1段だけなら返さない ★",
          E.rejected_pairs("3120 product not found") == [])
    check("複数の経路を全部拾う",
          len(E.rejected_pairs("9030 > 9997925 > 3120 ... 9005 > 9987017 > 3315")) == 2)

    # ========================================================
    print("\n[ 1回の失敗で、正確に1つだけ覚える ]")
    # ========================================================
    product = pm.products["9030"]
    slot = [s for s in product.slots if s.code == "9997925"][0]

    before = [p.code for p in pm.choice_candidates(slot, 8 * 60, product)]
    check("はじめは コカ・コーラM が候補にある", "3120" in before)
    check("しかも先頭にいる（代表商品のため）★", before[0] == "3120", before[:3])

    result = types.SimpleNamespace(error_info=info, store_id="13934")
    decoded = types.SimpleNamespace(items=[], store_id="13934")
    learned = await flows.learn_rejected_choices(decoded, result)
    check("覚えた商品名を返す ★", learned == ["コカ・コーラ M"], learned)

    after = [p.code for p in pm.choice_candidates(slot, 8 * 60, product)]
    check("次からは候補に出ない ★", "3120" not in after, after[:3])
    check("代表商品でも押し戻さない ★", after[0] != "3120", after[0])
    check("他のドリンクは巻き添えにならない ★",
          len(after) == len(before) - 1, (len(before), len(after)))
    check("通ると分かっているものは残る ★", "3170" in after)

    # 2回目は何も覚えない（同じことを二度記録しない）
    again = await flows.learn_rejected_choices(decoded, result)
    check("同じ失敗を二度覚えない", again == [], again)

    # ========================================================
    print("\n[ 巻き添えを出さないこと ]")
    # ========================================================
    # 経路の末尾がメニューに無い商品なら、覚えない
    body = b"ErrorCode_ProductValidation 9030 > 9997925 > 999999 product not found"
    r2 = types.SimpleNamespace(error_info=E.parse(400, body), store_id="13934")
    check("知らない商品コードは覚えない ★",
          await flows.learn_rejected_choices(decoded, r2) == [])

    # 決済エラーでは何も覚えない
    pay = types.SimpleNamespace(
        error_info=E.parse(402, b"ErrorCode_Payment 9030 > 9997925 > 3170"),
        store_id="13934",
    )
    check("決済の失敗では覚えない ★",
          await flows.learn_rejected_choices(decoded, pay) == [])
    check("スプライトは巻き添えになっていない ★",
          slot_rules.allowed("13934", "9997925", "3170"))

    # ========================================================
    print("\n[ ドリンクが選びやすくなっているか ]")
    # ========================================================
    from services.mcd import drinks

    cands = pm.choice_candidates(slot, 8 * 60, product)
    check("ドリンクの枠だと判定できる ★", drinks.is_drink_slot(cands))
    groups = [drinks.group_of(p.name)[0] for p in cands]
    check("種類ごとにまとまっている ★", groups == sorted(groups), groups)
    names = [p.name for p in cands]
    check("炭酸が先頭にかたまる ★",
          all("コカ・コーラ" in n or "スプライト" in n or "ファンタ" in n
              for n in names[:4]), names[:4])
    for name, want in (
        ("コカ・コーラ M", "炭酸"), ("ミニッツメイド® オレンジ(S)", "ジュース"),
        ("アイスカフェラテ S", "コーヒー"), ("ホットティー(レモン) S", "お茶"),
        ("ミルク", "乳製品"), ("謎の飲み物", "その他"),
    ):
        check(f"「{name}」→ {want}", drinks.group_of(name)[2] == want,
              drinks.group_of(name))
    check("絵文字が付く", all(drinks.emoji_of(n) for n in names))
    # ドリンクでない枠は、まとめない
    side = [s for s in product.slots if s.code == "9987010"][0]
    check("サイドの枠はドリンク扱いしない ★",
          not drinks.is_drink_slot(pm.choice_candidates(side, 8 * 60, product)))

    # ========================================================
    print("\n[ 画面に出せる形になっているか ]")
    # ========================================================
    import discord
    from ui import menu_flows as mf

    class FakeCart:
        menu = pm; pickup = "takeOut"; owner_id = 1; active_dayparts = set()

    v = mf.OptionView(FakeCart(), product, product.slots_of("choices"))
    sels = [i for i in v.children if isinstance(i, discord.ui.Select)]
    check("選択肢が作れる", bool(sels))
    drink_sel = None
    for sel in sels:
        if any(getattr(o, "emoji", None) for o in sel.options):
            drink_sel = sel
            break
    check("ドリンクに絵文字が付いている ★", drink_sel is not None)
    if drink_sel:
        check("種類の名前も出ている ★",
              all(o.description for o in drink_sel.options),
              [o.description for o in drink_sel.options[:3]])
        check("選択肢は25個まで", len(drink_sel.options) <= 25)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
