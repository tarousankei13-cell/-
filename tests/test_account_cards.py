"""決済カードの世話と、まとめ登録

⚠️ **カードが無いアカウントは注文に選ばれない**（pick_account が外す）。
   気づかないと、使えるアカウントが静かに減り、同時注文がさばけなくなる。
   だから「登録したら自動で見つける」「一覧で欠けを出す」の2本立てにする。

⚠️ カード番号そのものは**扱わない**。公式のAPIにカードを登録する呼び出しは
   無く（RPC 29個すべて確認）、3-Dセキュアの本人確認を通す別画面で行う
   作りになっている。こちらが受け取るのはカードの識別子だけ。
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.crypto import get_cipher, init_cipher
from db.models import McdAccount
from db.session import close_db, init_db, session_scope

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/card.db")
    c = get_cipher()
    async with session_scope() as s:
        for i, card in ((1, "card1"), (2, ""), (3, "")):
            s.add(McdAccount(
                id=i, label=f"acc{i}", email_enc=c.encrypt(f"a{i}@x"),
                refresh_token_enc=c.encrypt("rt"), card_id=card,
                device_uid="d", wmop_device_id="w", fb_instance_id="f",
                home_lat=35.0, home_lng=139.0, status="ACTIVE"))

    from services.mcd import accounts as A

    print("\n[1] どれにカードが無いか分かる ★")
    rows = await A.card_overview()
    check("全部出る", len(rows) == 3, len(rows))
    missing = [r["id"] for r in rows if not r["has_card"]]
    check("カード無しを見つけられる ★", missing == [2, 3], missing)

    print("\n[2] 1枚だけなら自動で設定する ★")
    async def one_card(_id):
        return [{"card_id": "newcard", "masked": "**** 1234"}]
    A.fetch_cards = one_card
    done, n, name = await A.auto_pick_card(2)
    check("設定される ★", done is True and n == 1, (done, n))
    check("名前も返す", "1234" in name, name)
    async with session_scope() as s:
        check("本当に保存される ★", (await s.get(McdAccount, 2)).card_id == "newcard")

    print("\n[3] ⚠️ 2枚以上なら自動で決めない ★")
    # どれで決済するかはお金の話。勝手に選んでよいものではない。
    async def two_cards(_id):
        return [{"card_id": "a", "masked": "A"}, {"card_id": "b", "masked": "B"}]
    A.fetch_cards = two_cards
    done, n, _ = await A.auto_pick_card(3)
    check("設定しない ★", done is False, done)
    check("枚数は伝える", n == 2, n)
    async with session_scope() as s:
        check("勝手に入れない ★", (await s.get(McdAccount, 3)).card_id == "")

    print("\n[4] ⚠️ すでに設定済みなら触らない ★")
    # 取り直すたびに上書きすると、せっかく選んだものが消える。
    A.fetch_cards = one_card
    done, n, _ = await A.auto_pick_card(1)
    check("触らない ★", done is False and n == -1, (done, n))
    async with session_scope() as s:
        check("元のままである ★", (await s.get(McdAccount, 1)).card_id == "card1")

    print("\n[5] 0枚でも落ちない")
    async def no_card(_id):
        return []
    A.fetch_cards = no_card
    done, n, _ = await A.auto_pick_card(3)
    check("設定しない", done is False and n == 0, (done, n))

    print("\n[6] 取得に失敗しても落ちない ★")
    # ⚠️ ログインできないアカウントもある。ここで例外を出すと
    #    一括処理が丸ごと止まる。
    async def boom(_id):
        raise RuntimeError("ログインできません")
    A.fetch_cards = boom
    done, n, _ = await A.auto_pick_card(3)
    check("落ちずに False を返す ★", done is False and n == 0, (done, n))

    print("\n[7] まとめ登録の読み取り ★")
    import cogs.account as AC
    got = AC._parse_bulk(
        "a@x.com,pass1\nb@x.com:pass2\nc@x.com\tpass3\nd@x.com pass4")
    check("4つの区切り文字に対応 ★", len(got) == 4, got)
    check("メールとパスワードが分かれる",
          got[0] == ("a@x.com", "pass1", "a"), got[0])
    # ⚠️ パスワードに区切り文字が入ることがある。最初の1つだけで切る。
    got = AC._parse_bulk("e@x.com,p:a:ss")
    check("パスワード内の記号が壊れない ★", got[0][1] == "p:a:ss", got)
    check("同じメールは1件だけ ★",
          len(AC._parse_bulk("a@x.com,p1\na@x.com,p2")) == 1)
    check("おかしな行は飛ばす",
          AC._parse_bulk("# コメント\n\nでたらめ\nメールなし,p") == [])
    check("空なら空", AC._parse_bulk("") == [])

    print("\n[8] メールをそのまま出さない ★")
    check("伏せ字になる ★", AC._mask_mail("tarousan@example.com") == "ta***@example.com",
          AC._mask_mail("tarousan@example.com"))
    check("短い名前でも漏らさない", AC._mask_mail("a@x.jp") == "a***@x.jp",
          AC._mask_mail("a@x.jp"))
    check("＠が無くても落ちない", "***" in AC._mask_mail("こわれた"))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
