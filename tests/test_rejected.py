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
    # ⚠️ 9030 は朝マックのセット。時間帯の外だと候補が全部消えて
    #    選択肢が1つも作られず、このテストは**実行した時刻によって
    #    落ちる**。実際に 05:25 に落ちた。時計を固定する。
    #    時刻で結果が変わるテストは、無いほうがましなので必ず固定する。
    #    config.now_jst() は毎回この環境変数を読むので、置くだけで効く。
    os.environ["BOT_FAKE_JST"] = "2026-10-06 08:00"

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

    test_debug_fails_scrub()
    await test_debug_fails_cmd()
    await test_bridge_commands()
    test_bridge_gaps()

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0




# ============================================================
# [8] 調査の窓口（/debug fails）
# ============================================================

def test_debug_fails_scrub():
    """応答を人に渡す前に、認証情報を伏せる。"""
    print("\n[ 応答を人に渡すときの伏せ字 ]")
    from services.mcd.errors import rejected_pairs, scrub

    body = (
        '{"error":"9030 > 9997925 > 3120 product not found",'
        '"token":"abcdef1234567890XYZ"}\n'
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CV\n"
        "password=hunter2hunter2\n"
    )
    out = scrub(body)

    check("トークンが残らない", "abcdef1234567890XYZ" not in out)
    check("JWTが残らない", "eyJhbGciOiJIUzI1NiJ9" not in out)
    check("パスワードが残らない", "hunter2hunter2" not in out)
    # ⚠️ 伏せすぎて原因が読めなくなっては意味がない
    check("原因の経路は残る ★", "9030 > 9997925 > 3120" in out)
    check("伏せた後でも経路を読める ★", rejected_pairs(out) == [("9997925", "3120")])
    check("入っていたことは分かる", "伏せました" in out)

    # 商品コードだけの応答は、何も伏せられない
    plain = "9030 > 9997925 > 3120 product not found"
    check("普通の応答はそのまま", scrub(plain) == plain)
    check("空でも落ちない", scrub(None) == "" and scrub("") == "")


async def test_debug_fails_cmd():
    """⚠️ 実際に `/debug fails` を呼んで、落ちずに応答することを確かめる。

    商品名を引く道具を try の中で作っていたため、注文コードが壊れていると
    未定義になり NameError で落ちていた。原因不明の注文ほど中身が読めないので、
    **一番必要な場面でだけ**窓口が使えなくなっていた。
    文面を眺めるのではなく、呼んで確かめる。
    """
    print("\n[ 読めない注文でも調べられる ★ ]")
    import cogs.admin as admin
    from _fake_discord import FakeClient, FakeInteraction, FakeUser
    from db.models import Order
    from db.session import session_scope

    async with session_scope() as s:
        # ① 注文コードが壊れている（中身を木にできない）
        s.add(Order(
            idempotency_key="k-broken", discord_id=1, state="failed",
            hex_payload="ZZZZ-読めない", store_id="10528", store_name="テスト店",
            list_price=500, subsidy_rate=0, user_amount=500, subsidy_amount=0,
            error="9030 > 9997925 > 3120 product not found",
        ))
        # ② 応答が長く、認証情報が混ざっている
        s.add(Order(
            idempotency_key="k-long", discord_id=1, state="failed",
            hex_payload="00", store_id="10528", store_name="テスト店",
            list_price=500, subsidy_rate=0, user_amount=500, subsidy_amount=0,
            error=("x" * 900 + '\n{"token":"abcdef1234567890SECRET"}'
                   + "\n9030 > 9997925 > 3120 product not found"),
        ))

    cog = admin.AdminCog(FakeClient())
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_fails.callback(cog, it, count=5)

    check("落ちずに応答する ★", bool(it.actions))
    body = it.text()
    check("原因の経路が読める ★", "9997925" in body and "3120" in body)
    check("相手が指した原因として出る ★", "断られました" in body)
    check("読めない注文もあきらめず出す ★", "読めませんでした" in body)

    f = next((kw["file"] for _, kw in it.actions if kw.get("file")), None)
    check("切れたら全文を添付する ★", f is not None)
    if f is not None:
        raw = f.fp.getvalue().decode("utf-8")
        check("添付に経路が入っている", "9030 > 9997925 > 3120" in raw)
        check("添付にトークンが残らない ★", "abcdef1234567890SECRET" not in raw)
        check("伏せたことが分かる", "伏せました" in raw)
        check("両方の注文が入っている", raw.count("=== ") >= 2)




# ============================================================
# [9] 選択枠の構造を教える窓口（/debug learn / bridges / forget）
# ============================================================

async def test_bridge_commands():
    """⚠️ これが無いと、構造を教える手段が存在しない。

    学習は注文の流れの中だけで動いていた。そこを通るには金額・残高・
    店舗の確認をすべて越える必要があり、**構造だけ教えたい場合に
    使えなかった**。カタログにも公式アプリにも無い値なので
    （docs/04）、通った注文コードを貼る以外に入手経路が無い。
    """
    print("\n[ 構造を教える窓口 ]")
    import cogs.admin as admin
    from _fake_discord import FakeClient, FakeInteraction, FakeUser
    from services.mcd import slot_bridge
    from services.mcd.protocol import OrderItem, build_hex

    slot_bridge.forget_all()
    cog = admin.AdminCog(FakeClient())

    def hexfor(slot, bridge, leaf="3604", top="9052"):
        return build_hex("13934", [OrderItem(product_code=top, quantity=1, amount=500,
            components=[OrderItem(product_code=slot, quantity=1, has_flag=True,
                components=[OrderItem(product_code=bridge, quantity=1, has_flag=True,
                    components=[OrderItem(product_code=leaf, quantity=1)])])])], "takeOut")

    # ① 知らない枠をおぼえる
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_learn.callback(cog, it, code=hexfor("9997929", "9997926"))
    check("落ちずに応答する", bool(it.actions))
    check("おぼえたと言う ★", "おぼえました" in it.text())
    check("実際に覚えている ★", slot_bridge.bridge_for("9997929") == "9997926")

    # ② 同じものをもう一度 → 既知として扱う
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_learn.callback(cog, it, code=hexfor("9997929", "9997926"))
    check("2回目は既知と言う", "既に知っています" in it.text())

    # ③ ⚠️ 違う値が来たら「書き換えた」と必ず知らせる。
    #    黙って上書きすると、通っていたものが通らなくなった理由が追えない。
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_learn.callback(cog, it, code=hexfor("9997929", "9997999"))
    check("書き換えを知らせる ★", "書き換えました" in it.text())
    check("前の値も見せる ★", "9997926" in it.text())

    # ④ 中間ノードが無いコードでは、何も学ばず、そう言う
    flat = build_hex("13934", [OrderItem(product_code="1010", quantity=1, amount=200)],
                     "takeOut")
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_learn.callback(cog, it, code=flat)
    check("学ぶものが無いと言う", "ありませんでした" in it.text())

    # ⑤ 壊れたコードで落ちない
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_learn.callback(cog, it, code="ZZ-読めない")
    check("壊れたコードでも落ちない ★", bool(it.actions))
    check("読めないと言う", "読み取れませんでした" in it.text())

    # ⑥ 一覧
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_bridges.callback(cog, it, store="13934")
    txt = it.text()
    check("一覧が出る", bool(it.actions))
    check("中間ありを出す", "9997918" in txt)
    check("中間不要と確認済みを別に出す ★", "直結" in txt)
    check("まだ分からない枠を出す", "まだ分からない枠" in txt)
    # ⚠️ 「不明」と「不要と確認済み」を混ぜない
    check("確認済みの枠を不明側に出さない ★",
          "9987009" not in txt.split("まだ分からない枠")[-1])
    check("全部壊れていると誤解させない ★", "全部が壊れているわけではありません" in txt)

    # ⑦ おぼえた分は消せる
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_forget.callback(cog, it, slot="9997929")
    check("消せる", "消しました" in it.text())
    check("実際に消えている ★", slot_bridge.bridge_for("9997929") == "")

    # ⑧ ⚠️ 実物で確認済みのものは消せない（事故で動かなくなるため）
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_forget.callback(cog, it, slot="9997918")
    check("確認済みは消せない ★", "消せません" in it.text())
    check("消されていない ★", slot_bridge.bridge_for("9997918") == "9997914")

    # ⑨ 知らない枠を消そうとしても落ちない
    it = FakeInteraction(FakeUser(1))
    await admin.AdminCog.debug_forget.callback(cog, it, slot="0000")
    check("知らない枠でも落ちない", bool(it.actions))

    slot_bridge.forget_all()


def test_bridge_gaps():
    """⚠️ 一覧は「不明」だけを出す。確認済みを混ぜてはいけない。"""
    print("\n[ 分からない枠の数え方 ★ ]")
    import json
    from services.mcd import slot_bridge
    from services.mcd.menu import parse_menu

    slot_bridge.forget_all()
    m = parse_menu("13934", json.load(open(os.path.join(HERE, "m13934.json"))))
    codes = [c for c, _ in m.bridge_gaps()]

    for c in slot_bridge.KNOWN:
        check(f"中間ありの {c} は不明に出ない", c not in codes)
    # 9987009 は37商品が使う枠。中間不要と実物で確認済み。
    for c in slot_bridge.NO_BRIDGE:
        check(f"中間不要と確認済みの {c} は不明に出ない ★", c not in codes)

    check("それでも不明は残る（0件にはならない）", len(codes) > 0, codes[:3])
    check("状況を3つに分けられる ★",
          slot_bridge.status_of("9997918") == "bridge"
          and slot_bridge.status_of("9987009") == "direct"
          and slot_bridge.status_of("0000") == "unknown")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
