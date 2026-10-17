"""認証コードを、あとからまとめて入れる

⚠️ ここが狂うと「お金の入ったアカウントを取り違える」ので、次を固定する。

   ① 待っていない番号にコードを当てない（別口座に入れない）
   ② 預かったクライアントは、**どの道を通っても必ず閉じる**
      （完了・間違い3回・やめる・時間切れ）
   ③ 正しかった分だけ登録され、間違えた分は残って再試行できる
   ④ メールとパスワードを二度打たせない（預かりから続けられる）
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
from services.mcd.client import LoginResult, McdError, TokenSet

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class FakeValue:
    def __init__(self, v): self.value = v
    def __str__(self): return self.value


class FakeMcd:
    """マクドナルドの偽物。認証コードの要否と、コードの正解を指定できる。"""

    def __init__(self, need_otp=True, good_code="123456", cards=None):
        self.need_otp = need_otp
        self.good_code = good_code
        self.tokens = TokenSet()
        self.cards = cards if cards is not None else [
            {"card_id": "card-1", "masked": "**** 1234"},
        ]
        self.closed = False
        self.mfa_calls = []

    async def login(self, email, password):
        if not self.need_otp:
            self.tokens = TokenSet(access_token="a", refresh_token="r")
            return LoginResult(needs_otp=False, tokens=self.tokens)
        return LoginResult(needs_otp=True, mfa_token=f"mfa-{email}")

    async def login_with_mfa(self, mfa, otp):
        self.mfa_calls.append((mfa, otp))
        if otp != self.good_code:
            raise McdError("認証コードが違います")
        self.tokens = TokenSet(access_token="a", refresh_token="r")
        return self.tokens

    async def get_cards(self): return self.cards
    async def aclose(self): self.closed = True


async def main() -> int:
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/otp.db")
    await settings.load_all()

    import cogs.account as acc
    owner = FakeUser(1, "オーナー")
    dclient = FakeClient()

    # ============================================================
    print("\n[1] コードの読み取り ★")
    P = acc._parse_otp_batch
    got, bad = P("1,111111\n2,222222", [0, 1])
    check("番号を添えた形を読む ★", got == {0: "111111", 1: "222222"}, (got, bad))
    got, bad = P("111111\n222222", [0, 1])
    check("コードだけ並べたら表示順に当てる ★",
          got == {0: "111111", 1: "222222"}, (got, bad))
    got, bad = P("2,222222\n111111", [0, 1])
    check("混ざっても番号付きを優先し、残りを空きへ詰める ★",
          got == {0: "111111", 1: "222222"}, (got, bad))
    got, bad = P("1:111111\n2 222222\n3=333333", [0, 1, 2])
    check("コロン・スペース・イコールでも読む",
          got == {0: "111111", 1: "222222", 2: "333333"}, (got, bad))
    got, bad = P("# めも\n\n1,111111", [0, 1])
    check("#と空行は飛ばす", got == {0: "111111"} and not bad, (got, bad))
    got, bad = P("1,abcd\n2,12\n", [0, 1])
    check("数字でない・桁が足りないものは断る ★", not got and len(bad) == 2, (got, bad))

    # ⚠️ ここが一番危ない。待っていない番号に当てると、別の口座の
    #    コードを使ってしまう。
    got, bad = P("9,111111", [0, 1])
    check("待っていない番号には当てない ★", not got and bad, (got, bad))
    got, bad = P("1,111111\n2,222222", [0])       # 2番はもう済んでいる
    check("済んだ分には当てない ★", got == {0: "111111"} and bad, (got, bad))
    got, bad = P("111111\n222222\n333333", [0, 1])
    check("多すぎるコードは余らせる ★",
          len(got) == 2 and len(bad) == 1, (got, bad))

    # ============================================================
    print("\n[2] まとめ登録で、コードが必要な分を預かる ★")
    made: list[FakeMcd] = []

    def factory(fp, *a, **kw):
        # 1件目はコード不要、2・3件目はコードが必要
        c = FakeMcd(need_otp=len(made) >= 1)
        made.append(c)
        return c

    acc.McdClient = factory
    bulk = acc.McdBulkModal()
    bulk.lines = FakeValue(
        "no-otp@example.com,pw\n"
        "need1@example.com,pw\n"
        "need2@example.com,pw\n"
    )
    itx = FakeInteraction(owner, dclient)
    await bulk.on_submit(itx)

    check("3件ぶん試した", len(made) == 3, len(made))
    check("コード不要の分は閉じている", made[0].closed)
    # ⚠️ ここが本題。閉じてしまうとログインの続きができず、
    #    メールとパスワードを打ち直させることになる。
    check("コードが必要な分は閉じずに預かる ★",
          not made[1].closed and not made[2].closed,
          (made[1].closed, made[2].closed))
    check("コードだけで済むと伝える ★", "コードだけ" in itx.text(), itx.text()[:200])
    check("1件ずつやり直せとは言わない ★",
          "1件ずつ登録してください" not in itx.text())

    view = None
    for _, kw in reversed(itx.actions):
        if isinstance(kw.get("view"), acc.OtpBatchView):
            view = kw["view"]; break
    check("まとめ入力の画面が出る ★", view is not None)
    check("預かりは2件", view is not None and len(view.pending) == 2,
          len(view.pending) if view else "無し")

    # ============================================================
    print("\n[3] コードをまとめて入れると登録される ★")
    modal = acc.OtpBatchModal(view)
    modal.codes = FakeValue("1,123456\n2,123456")
    itx3 = FakeInteraction(owner, dclient)
    await modal.on_submit(itx3)

    async with session_scope() as s:
        rows = (await s.execute(select(McdAccount))).scalars().all()
    labels = {r.label for r in rows}
    check("2件とも保存される ★",
          {"need1", "need2"} <= labels, sorted(labels))
    check("預かったクライアントを閉じる ★",
          made[1].closed and made[2].closed)
    check("待ちが残っていない ★", not view.open_idx(), view.open_idx())
    saved = [r for r in rows if r.label == "need1"]
    check("リフレッシュトークンが入る ★", saved and bool(saved[0].refresh_token_enc))
    check("カードが1枚なら自動で設定する ★",
          saved and saved[0].card_id == "card-1",
          saved[0].card_id if saved else "無し")
    check("コードは預かったmfa_tokenで確認している ★",
          made[1].mfa_calls and made[1].mfa_calls[0][0] == "mfa-need1@example.com",
          made[1].mfa_calls)

    # ============================================================
    print("\n[4] 間違えた分だけ残り、3回でやめる ★")
    made2: list[FakeMcd] = []

    def factory2(fp, *a, **kw):
        c = FakeMcd(need_otp=True)
        made2.append(c)
        return c

    acc.McdClient = factory2
    bulk2 = acc.McdBulkModal()
    bulk2.lines = FakeValue("a1@example.com,pw\na2@example.com,pw\n")
    itx4 = FakeInteraction(owner, dclient)
    await bulk2.on_submit(itx4)
    view2 = None
    for _, kw in reversed(itx4.actions):
        if isinstance(kw.get("view"), acc.OtpBatchView):
            view2 = kw["view"]; break
    check("2件を預かった", view2 is not None and len(view2.pending) == 2)

    # 1件目は正解、2件目は間違い
    m = acc.OtpBatchModal(view2)
    m.codes = FakeValue("1,123456\n2,999999")
    itx5 = FakeInteraction(owner, dclient)
    await m.on_submit(itx5)
    check("正しい分は登録される ★", view2.pending[0].done)
    check("間違えた分は残る ★", not view2.pending[1].done)
    check("残り回数を伝える ★", "残り" in view2.pending[1].note, view2.pending[1].note)
    check("正しい分のクライアントは閉じる ★", made2[0].closed)
    check("間違えた分はまだ閉じない（やり直せる）★", not made2[1].closed)

    # あと2回間違える → 諦めて閉じる
    for _ in range(2):
        mm = acc.OtpBatchModal(view2)
        mm.codes = FakeValue("2,999999")
        await mm.on_submit(FakeInteraction(owner, dclient))
    check("3回間違えたらやめる ★", view2.pending[1].done)
    check("やめた分も必ず閉じる ★", made2[1].closed)

    # ============================================================
    print("\n[5] やめる・時間切れでも必ず閉じる ★")
    made3: list[FakeMcd] = []

    def factory3(fp, *a, **kw):
        c = FakeMcd(need_otp=True)
        made3.append(c)
        return c

    acc.McdClient = factory3
    b3 = acc.McdBulkModal()
    b3.lines = FakeValue("c1@example.com,pw\nc2@example.com,pw\n")
    itx6 = FakeInteraction(owner, dclient)
    await b3.on_submit(itx6)
    v3 = None
    for _, kw in reversed(itx6.actions):
        if isinstance(kw.get("view"), acc.OtpBatchView):
            v3 = kw["view"]; break
    itx7 = FakeInteraction(owner, dclient)
    await v3.cancel.callback(itx7)
    check("やめたら全部閉じる ★", all(c.closed for c in made3),
          [c.closed for c in made3])
    check("やめたと伝える", "やめ" in itx7.text(), itx7.text()[:120])

    made4: list[FakeMcd] = []

    def factory4(fp, *a, **kw):
        c = FakeMcd(need_otp=True)
        made4.append(c)
        return c

    acc.McdClient = factory4
    b4 = acc.McdBulkModal()
    b4.lines = FakeValue("d1@example.com,pw\n")
    itx8 = FakeInteraction(owner, dclient)
    await b4.on_submit(itx8)
    v4 = None
    for _, kw in reversed(itx8.actions):
        if isinstance(kw.get("view"), acc.OtpBatchView):
            v4 = kw["view"]; break
    await v4.on_timeout()
    check("時間切れでも閉じる ★", all(c.closed for c in made4),
          [c.closed for c in made4])
    # ⚠️ 1件ぶんの待ち時間も、まとめと同じだけ待てること
    check("待ち時間が十分ある ★", acc.OTP_WAIT_SECONDS >= 600,
          acc.OTP_WAIT_SECONDS)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
