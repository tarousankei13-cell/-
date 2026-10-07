"""鍵が合わないときに、きちんと説明して止まるか

⚠️ これは起こりうる。バックアップを `data/encryption_key.txt` 無しで
   戻した場合、`ENCRYPTION_KEY` を書き換えた場合、鍵を作り直した場合。
   バックアップの案内文自身が「鍵も必要です」と言っている状況。

⚠️ そのとき、生の例外で落ちてはいけない。
   ・管理者のコマンド → 何が起きたか説明して止まる
   ・注文やチャージ   → そのアカウントを使えないものとして扱い、
                        注文の途中で理由不明の失敗にしない
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import crypto
from core.crypto import CryptoError, init_cipher
from db.models import KyashAccount, McdAccount, McdToken, utcnow
from db.session import close_db, init_db, session_scope

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


KEY_A = "dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0"
KEY_B = "YW5vdGhlci1rZXktMzJieXRlcy1mb3ItdGVzdC05OTk5"


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher(KEY_A)
    await init_db(f"sqlite+aiosqlite:///{tmp}/k.db")

    cipher = crypto.get_cipher()
    blob = cipher.encrypt("ひみつの値")

    print("\n[1] 読める・読めないを区別できるか")
    check("正しい鍵なら読める", crypto.try_decrypt(blob) == "ひみつの値")
    check("正しい鍵では『読めない』にならない", crypto.is_unreadable(blob) is False)
    check("空は読めないではない（中身が無いだけ）★",
          crypto.is_unreadable(None) is False and crypto.is_unreadable(b"") is False)
    check("空は None を返す", crypto.try_decrypt(None) is None)

    # 鍵を入れ替える＝バックアップを鍵なしで戻した状態
    init_cipher(KEY_B)
    print("\n[2] 鍵を入れ替えたあと")
    check("読めないと分かる ★", crypto.is_unreadable(blob) is True)
    check("落ちずに None を返す ★", crypto.try_decrypt(blob) is None)
    try:
        crypto.get_cipher().decrypt(blob)
        check("生の decrypt は例外を出す（説明用）", False, "出なかった")
    except CryptoError as e:
        check("生の decrypt は例外を出す（説明用）", "ENCRYPTION_KEY" in str(e), str(e))

    print("\n[3] 注文の経路が、鍵違いで落ちないか ★")
    # ⚠️ ここで落ちると、注文の途中で理由の分からない失敗になる。
    init_cipher(KEY_A)
    async with session_scope() as s:
        s.add(McdAccount(id=1, label="mcd-1", email_enc=b"x",
                         refresh_token_enc=crypto.get_cipher().encrypt("rt"),
                         card_id="c", device_uid="d", wmop_device_id="w",
                         fb_instance_id="f", home_lat=35.0, home_lng=139.0))
        s.add(McdToken(mcd_account_id=1,
                       access_token_enc=crypto.get_cipher().encrypt("at"),
                       root_paseto_enc=crypto.get_cipher().encrypt("rp")))
        s.add(KyashAccount(id=1, label="k", email_enc=b"x",
                           access_token_enc=crypto.get_cipher().encrypt("kt"),
                           token_obtained_at=utcnow()))

    from services.mcd import accounts as mcd_accounts
    async with session_scope() as s:
        t = await mcd_accounts._load_tokens(s, 1)
    check("正しい鍵ならトークンを読める", t.refresh_token == "rt" and t.access_token == "at")

    init_cipher(KEY_B)
    try:
        async with session_scope() as s:
            t2 = await mcd_accounts._load_tokens(s, 1)
        check("鍵違いでも落ちない ★", True)
        check("読めなかったものは空になる ★",
              t2.refresh_token == "" and t2.access_token == "", t2)
    except Exception as e:
        check("鍵違いでも落ちない ★", False, f"{type(e).__name__}: {e}")

    from services.kyash import accounts as ky_accounts
    async with session_scope() as s:
        acc = await s.get(KyashAccount, 1)
        try:
            c = ky_accounts.build_client(acc)
            check("Kyash も鍵違いで落ちない ★", True)
            check("読めなければ空のトークンになる ★", c.session.access_token == "")
            await c.aclose()
        except Exception as e:
            check("Kyash も鍵違いで落ちない ★", False, f"{type(e).__name__}: {e}")

    print("\n[4] 管理者コマンドが説明して止まるか ★")
    import discord
    from _fake_discord import FakeClient, FakeInteraction, FakeUser
    import cogs.account as account_cog

    cog = account_cog.AccountCog(FakeClient())
    it = FakeInteraction(FakeUser(1))
    try:
        await account_cog.AccountCog.kyash_relogin.callback(cog, it, account_id=1)
        check("落ちずに応答する ★", bool(it.actions))
        txt = it.text()
        check("鍵のことを説明する ★", "鍵" in txt and "読み取れません" in txt, txt[:120])
        check("どう直すかを書いてある ★",
              "encryption_key" in txt or "登録し直" in txt, txt[:160])
    except Exception as e:
        check("落ちずに応答する ★", False, f"{type(e).__name__}: {e}")

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
