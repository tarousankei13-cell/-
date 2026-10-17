"""ファイルでのまとめ登録と、下見（ドライラン）

⚠️ ここが狂うと「登録したつもりで入っていない」ので、次を固定する。

   ① 捨てた行は**行番号つきで**必ず報告する（黙って飛ばさない）
   ② 下見では **1件も登録しない**
   ③ すでに登録済みのものは、ログインを試さず飛ばす（二重登録しない）
   ④ ファイルは中身を見て断る（拡張子・大きさ・文字コード）
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select

from _fake_discord import FakeClient, FakeInteraction, FakeUser
from core import settings
from core.crypto import init_cipher
from db.models import McdAccount
from db.session import close_db, init_db, session_scope
from services.mcd.client import LoginResult, TokenSet

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class FakeValue:
    def __init__(self, v): self.value = v
    def __str__(self): return self.value


class FakeAttachment:
    def __init__(self, data: bytes, filename="accounts.txt", size=None):
        self.data = data
        self.filename = filename
        self.size = len(data) if size is None else size
    async def read(self): return self.data


class FakeMcd:
    """コードを求めずにすぐ登録できる偽物。"""
    def __init__(self):
        self.tokens = TokenSet()
        self.closed = False
    async def login(self, email, password):
        self.tokens = TokenSet(access_token="a", refresh_token="r")
        return LoginResult(needs_otp=False, tokens=self.tokens)
    async def get_cards(self):
        return [{"card_id": "c1", "masked": "**** 1111"}]
    async def aclose(self): self.closed = True


async def count_accounts() -> int:
    async with session_scope() as s:
        return len((await s.execute(select(McdAccount))).scalars().all())


async def main() -> int:
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/bulk.db")
    await settings.load_all()

    import cogs.account as acc
    from cogs.account import AccountCog

    owner = FakeUser(1, "オーナー")
    dclient = FakeClient()
    cog = AccountCog(dclient)

    # ============================================================
    print("\n[1] 読み取りレポート ★")
    r = acc._parse_bulk_report(
        "a@example.com,pw1\n"
        "# これはメモ\n"
        "\n"
        "こわれた行\n"
        "b@example.com:pw2\n"
        "a@example.com,pw3\n"
    )
    check("読めた行を採る", [e[0] for e in r.entries] == ["a@example.com", "b@example.com"],
          [e[0] for e in r.entries])
    check("読めない行を行番号つきで返す ★", r.bad == [(4, "こわれた行")], r.bad)
    check("重複を行番号つきで返す ★", r.dup == [(6, "a@example.com")], r.dup)
    check("#と空行は読めない行に数えない ★", len(r.bad) == 1, r.bad)
    check("飛ばした合計が出る", r.total_skipped == 2, r.total_skipped)
    # ⚠️ 従来の入口を壊していないこと
    check("_parse_bulk は今までどおり ★",
          acc._parse_bulk("a@b.c,pw") == [("a@b.c", "pw", "a")],
          acc._parse_bulk("a@b.c,pw"))

    # ============================================================
    print("\n[2] ファイルの読み込み ★")
    txt = "x@example.com,pw\ny@example.com,pw\n"
    got = await acc._read_bulk_file(FakeAttachment(txt.encode("utf-8")))
    check(".txt を読む", got == txt, repr(got))
    got = await acc._read_bulk_file(
        FakeAttachment(txt.encode("utf-8"), filename="a.csv"))
    check(".csv も読む", got == txt)
    got = await acc._read_bulk_file(
        FakeAttachment(txt.encode("utf-8"), filename="a.exe"))
    check("別の拡張子は断る ★", got is None, repr(got))
    got = await acc._read_bulk_file(
        FakeAttachment(txt.encode("utf-8"), size=99_000_000))
    check("大きすぎるものは読まない ★", got is None, repr(got))
    # ⚠️ Excel から出すと cp932 になることがある。UTF-8 だけだと止まる。
    jp = "日本語@example.com,ぱすわーど\n"
    got = await acc._read_bulk_file(FakeAttachment(jp.encode("cp932")))
    check("cp932 でも読める ★", got == jp, repr(got))
    got = await acc._read_bulk_file(FakeAttachment(txt.encode("utf-8-sig")))
    check("BOM付きでも読める ★", got == txt, repr(got))

    # ============================================================
    print("\n[3] 下見では1件も登録しない ★")
    made: list[FakeMcd] = []
    acc.McdClient = lambda fp, *a, **kw: (made.append(FakeMcd()) or made[-1])

    before = await count_accounts()
    data = ("d1@example.com,pw\n"
            "d2@example.com,pw\n"
            "こわれ\n"
            "d1@example.com,pw\n").encode("utf-8")
    itx = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(cog, itx, file=FakeAttachment(data), dry_run=True)
    check("登録していない ★", await count_accounts() == before,
          (before, await count_accounts()))
    check("ログインも試さない ★", not made, len(made))
    check("下見だと分かる ★", "登録はしていません" in itx.text(), itx.text()[:160])
    check("登録できる件数が出る", "2件" in itx.text(), itx.text()[:300])
    check("読めない行を知らせる ★", "読めなかった行" in itx.text(), itx.text()[:400])
    check("重複を知らせる ★", "重複" in itx.text(), itx.text()[:400])

    # ============================================================
    print("\n[4] ファイルで実際に登録する ★")
    made.clear()
    itx2 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(
        cog, itx2, file=FakeAttachment(b"f1@example.com,pw\nf2@example.com,pw\n"))
    check("2件登録される ★", await count_accounts() == before + 2,
          await count_accounts())
    check("2件ぶんログインした", len(made) == 2, len(made))
    check("接続を閉じる", all(m.closed for m in made))

    # ============================================================
    print("\n[5] すでに登録済みは飛ばす ★")
    # ⚠️ 二重に登録すると、どちらが使われるか分からなくなる。
    made.clear()
    n_before = await count_accounts()
    itx3 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(
        cog, itx3, file=FakeAttachment(b"f1@example.com,pw\nf3@example.com,pw\n"))
    check("増えたのは1件だけ ★", await count_accounts() == n_before + 1,
          (n_before, await count_accounts()))
    check("登録済みの分はログインを試さない ★", len(made) == 1, len(made))
    check("飛ばしたことを知らせる ★", "すでに登録済み" in itx3.text(),
          itx3.text()[:400])

    # 下見でも「登録済み」が見える
    itx4 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(
        cog, itx4, file=FakeAttachment(b"f1@example.com,pw\n"), dry_run=True)
    check("下見でも登録済みが分かる ★", "すでに登録済み" in itx4.text(),
          itx4.text()[:300])

    # ============================================================
    print("\n[6] 上限と断り方 ★")
    check("ファイルのほうが多く入る ★",
          acc.BULK_MAX_FILE > acc.BULK_MAX_PASTE,
          (acc.BULK_MAX_FILE, acc.BULK_MAX_PASTE))
    many = "\n".join(f"m{i}@example.com,pw" for i in range(acc.BULK_MAX_FILE + 5))
    made.clear()
    n5 = await count_accounts()
    itx5 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(
        cog, itx5, file=FakeAttachment(many.encode("utf-8")))
    check("上限を超えたら登録しない ★", await count_accounts() == n5,
          await count_accounts())
    check("上限を超えたらログインも試さない ★", not made, len(made))
    check("上限を伝える ★", str(acc.BULK_MAX_FILE) in itx5.text(), itx5.text()[:200])

    # 読める行が無いファイル
    itx6 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(
        cog, itx6, file=FakeAttachment(b"\xe5\xa3\x8a\xe3\x82\x8c\n"))
    check("読める行が無ければ断る ★", "ありませんでした" in itx6.text(),
          itx6.text()[:200])

    # 下見なのにファイルが無い
    itx7 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(cog, itx7, file=None, dry_run=True)
    check("下見でファイル無しは案内する ★", "添付" in itx7.text(), itx7.text()[:200])

    # 何も付けなければ今までどおり貼り付け窓
    itx8 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(cog, itx8)
    check("何も付けなければ貼り付け窓 ★",
          isinstance(itx8.last_modal(), acc.McdBulkModal),
          type(itx8.last_modal()).__name__)

    # ============================================================
    print("\n[7] 失敗した分だけやり直す ★")
    from services.mcd.client import McdError

    BAD = {"r2@example.com"}

    class MaybeFail(FakeMcd):
        def __init__(self, email_box):
            super().__init__()
            self.email_box = email_box
        async def login(self, email, password):
            self.email_box.append(email)
            if email in BAD:
                raise McdError("一時的な通信エラー")
            return await FakeMcd.login(self, email, password)

    tried: list[str] = []
    made2: list[MaybeFail] = []

    def f2(fp, *a, **kw):
        c = MaybeFail(tried)
        made2.append(c)
        return c

    acc.McdClient = f2
    n7 = await count_accounts()
    itx9 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(
        cog, itx9,
        file=FakeAttachment(b"r1@example.com,pw\nr2@example.com,pw\n"))
    check("成功した分だけ入る ★", await count_accounts() == n7 + 1,
          await count_accounts())
    retry = None
    for _, kw in reversed(itx9.actions):
        if isinstance(kw.get("view"), acc.BulkRetryView):
            retry = kw["view"]; break
    check("やり直しの画面が出る ★", retry is not None)
    check("失敗した1件を控えている ★",
          retry is not None and [e[0] for e in retry.entries] == ["r2@example.com"],
          [e[0] for e in retry.entries] if retry else "無し")
    # ⚠️ パスワードも控えてあること（打ち直させない）
    check("パスワードも控えている ★",
          retry is not None and retry.entries[0][1] == "pw",
          retry.entries[0] if retry else "無し")

    # 今度は通る状態にして、やり直す
    BAD.clear()
    tried.clear()
    n8 = await count_accounts()
    itx10 = FakeInteraction(owner, dclient)
    await retry.retry.callback(itx10)
    check("やり直しで入る ★", await count_accounts() == n8 + 1,
          await count_accounts())
    check("やり直したのは失敗した分だけ ★", tried == ["r2@example.com"], tried)

    # ⚠️ 二度押しで二重に登録しない
    tried.clear()
    n9 = await count_accounts()
    itx11 = FakeInteraction(owner, dclient)
    await retry.retry.callback(itx11)
    check("二度押ししても増えない ★", await count_accounts() == n9,
          await count_accounts())
    check("二度押しではログインを試さない ★", not tried, tried)
    check("やり直す分が無いと伝える", "ありません" in itx11.text(),
          itx11.text()[:120])

    # ⚠️ 時間切れでパスワードを捨てる
    v = acc.BulkRetryView(owner.id, [("z@example.com", "pw", "z")])
    await v.on_timeout()
    check("時間切れでパスワードを捨てる ★", v.entries == [], v.entries)

    # ============================================================
    print("\n[8] 省略が起きたら全文をファイルで渡す ★")
    # ⚠️ 画面の一覧は10件で切るので、それ以上はファイルが無いと
    #    「どれが入らなかったか」が分からなくなる。
    acc.McdClient = lambda fp, *a, **kw: FakeMcd()
    many2 = "\n".join(f"big{i}@example.com,pw" for i in range(12))
    itx12 = FakeInteraction(owner, dclient)
    await cog.mcd_bulk.callback(
        cog, itx12, file=FakeAttachment(many2.encode("utf-8")))
    files = [kw.get("file") for _, kw in itx12.actions if kw.get("file")]
    check("全文のファイルが付く ★", bool(files), len(files))
    check("全文だと分かる案内が出る ★", "添付のファイル" in itx12.text(),
          itx12.text()[-200:])
    body = acc._bulk_report_text(
        [(1, "a", "**** 1", 1)], [("bad@example.com", "だめ")],
        ["dup@example.com"], acc._parse_bulk_report("こわれ\n"))
    check("全文に失敗の理由が入る ★", "bad@example.com" in body and "だめ" in body)
    check("全文に登録済みが入る ★", "dup@example.com" in body)
    check("全文に読めない行が入る ★", "こわれ" in body, body[-120:])

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
