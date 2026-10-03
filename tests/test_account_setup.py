"""アカウント登録の検証 — メール/パスワード → OTP → カード選択 の流れ"""
import asyncio, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fake_discord import FakeInteraction, FakeUser, FakeClient
from core.crypto import init_cipher, get_cipher
from db.session import init_db, session_scope, close_db
from core import settings
from db.models import KyashAccount, McdAccount
from services.mcd.client import LoginResult, McdAuthError, TokenSet
from services.kyash.client import KyashError, KyashSession, Profile, Wallet

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

def set_selected(sel, values):
    """Select の選択値を差し込む（values は読み取り専用のため内部に入れる）"""
    for attr in ("_values", "_selected_values"):
        try:
            object.__setattr__(sel, attr, list(values))
        except Exception:
            pass
    assert list(sel.values) == list(values), f"選択値を差し込めませんでした: {sel.values}"


class FakeValue:
    def __init__(self, v): self.value = v
    def __str__(self): return self.value

class FakeMcd:
    def __init__(self, login_error=None, mfa_error=None, cards=None, need_otp=True):
        self.login_error = login_error; self.mfa_error = mfa_error
        self.need_otp = need_otp
        self.tokens = TokenSet()
        self.cards = cards if cards is not None else [
            {"card_id": "card-1", "masked": "**** 1234", "expiry": "12/28", "name": "TARO"},
            {"card_id": "card-2", "masked": "**** 5678", "expiry": "03/27", "name": "TARO"},
        ]
        self.closed = False
    async def login(self, email, password):
        if self.login_error: raise self.login_error
        if not self.need_otp:
            # マクドナルドが認証コードを求めてこなかった場合
            self.tokens = TokenSet(access_token="a", refresh_token="r")
            return LoginResult(needs_otp=False, tokens=self.tokens)
        return LoginResult(needs_otp=True, mfa_token="mfa-token")
    async def login_with_mfa(self, mfa, otp):
        if self.mfa_error: raise self.mfa_error
        self.tokens = TokenSet(access_token="a", refresh_token="r")
        return self.tokens
    async def get_cards(self): return self.cards
    async def aclose(self): self.closed = True

class FakeKyashClient:
    def __init__(self, need_otp=True, otp_error=None):
        self.need_otp = need_otp; self.otp_error = otp_error
        self.session = KyashSession(access_token="", client_uuid="CU", installation_uuid="IU")
        self.closed = False
    async def start_login(self, email, password):
        if not self.need_otp: self.session.access_token = "tok"
        return self.need_otp
    async def verify_otp(self, otp, email=None):
        if self.otp_error: raise self.otp_error
        self.session.access_token = "tok"
        return self.session
    async def get_profile(self): return Profile(username="u", is_kyc=True)
    async def get_wallet(self): return Wallet(uuid="w", all_balance=3000)
    async def aclose(self): self.closed = True

async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/acc.db")
    await settings.load_all()

    import cogs.account as acc
    client = FakeClient()
    owner = FakeUser(1, "オーナー")

    print("\n[1] マクドナルド: 認証情報を入れたあと")
    mcd = FakeMcd()
    acc.McdClient = lambda fp, *a, **kw: mcd
    modal = acc.McdCredModal()
    modal.label, modal.email, modal.password = FakeValue("メイン"), FakeValue("a@b.c"), FakeValue("pw")
    itx = FakeInteraction(owner, client)
    await modal.on_submit(itx)
    check("すぐ応答する（3秒制限の回避）", itx.kinds[0] == "send_message", itx.kinds)
    check("認証コードの案内が出る", "認証コード" in itx.text(), itx.text()[:80])
    view = itx.last_view()
    check("「認証コードを入力」ボタンが出る", view is not None and len(view.children) == 1)

    print("\n[1.5] マクドナルド: 認証コードを求められなかったとき ★")
    # ⚠️ 以前は jwtMfaToken と jwtAccessToken を区別しておらず、
    #    求められていないのに必ず6桁を聞いていた。届かないコードは
    #    入力できないので、そこで進めなくなっていた。
    mcd_no_otp = FakeMcd(need_otp=False)
    acc.McdClient = lambda fp, *a, **kw: mcd_no_otp
    modal15 = acc.McdCredModal()
    modal15.label, modal15.email, modal15.password = (
        FakeValue("コード不要"), FakeValue("x@y.z"), FakeValue("pw"))
    itx15 = FakeInteraction(owner, client)
    await modal15.on_submit(itx15)
    check("認証コードを聞かない ★", "認証コードを送信" not in itx15.text(), itx15.text()[:120])
    check("そのまま登録が終わる ★", "登録しました" in itx15.text(), itx15.text()[:120])
    check("入力ボタンを出さない ★", itx15.last_view() is None, itx15.last_view())
    check("次の手順を案内する", "/mcd card" in itx15.text(), itx15.text()[:200])
    check("接続を閉じる", mcd_no_otp.closed)
    async with session_scope() as s:
        from sqlalchemy import select as _sel
        rows = (await s.execute(_sel(McdAccount))).scalars().all()
    saved = [r for r in rows if r.label == "コード不要"]
    check("アカウントが保存される ★", len(saved) == 1, [r.label for r in rows])
    if saved:
        check("リフレッシュトークンが入る ★", bool(saved[0].refresh_token_enc))

    acc.McdClient = lambda fp, *a, **kw: mcd
    print("\n[2] マクドナルド: 認証コードを入れる")
    otp_modal = acc.McdOtpModal(view)
    otp_modal.otp = FakeValue("123456")
    itx2 = FakeInteraction(owner, client)
    await otp_modal.on_submit(itx2)
    async with session_scope() as s:
        from sqlalchemy import select
        all_rows = (await s.execute(select(McdAccount))).scalars().all()
    # [1.5] でも1件保存しているので、こちらの表示名で絞る
    rows = [r for r in all_rows if r.label == "メイン"]
    check("アカウントが保存される", len(rows) == 1, [r.label for r in all_rows])
    if rows:
        a = rows[0]
        cipher = get_cipher()
        check("表示名が保存される", a.label == "メイン", a.label)
        check("メールが暗号化されている", cipher.decrypt(a.email_enc) == "a@b.c")
        check("リフレッシュトークンが暗号化されている", cipher.decrypt(a.refresh_token_enc) == "r")
        check("端末情報が個別に生成される", a.device_uid and a.wmop_device_id and a.fb_instance_id)
        check("位置情報が設定される", a.home_lat and a.home_lng)
    check("カード選択が出る", "カードを選んで" in itx2.text(), itx2.text()[:80])
    card_view = itx2.last_view()
    check("カードが2枚とも選べる",
          card_view and len(card_view.children[0].options) == 2,
          [o.label for o in card_view.children[0].options] if card_view else None)

    print("\n[3] マクドナルド: カードを選ぶ")
    set_selected(card_view._sel, ["card-2"])
    itx3 = FakeInteraction(owner, client)
    await card_view._on_pick(itx3)
    async with session_scope() as s:
        a = await s.get(McdAccount, rows[0].id)
    check("選んだカードが保存される", a.card_id == "card-2", a.card_id)
    check("完了を伝える", "使用できます" in itx3.text(), itx3.text()[:80])

    print("\n[4] マクドナルド: ログイン失敗")
    acc.McdClient = lambda fp, *a, **kw: FakeMcd(
        login_error=McdAuthError("メールアドレスまたはパスワードが違います"))
    modal = acc.McdCredModal()
    modal.label, modal.email, modal.password = FakeValue(""), FakeValue("x@y.z"), FakeValue("bad")
    itx = FakeInteraction(owner, client)
    await modal.on_submit(itx)
    check("失敗を伝える", "違います" in itx.text(), itx.text()[:80])

    print("\n[5] マクドナルド: 認証コードを3回間違える")
    mcd = FakeMcd(mfa_error=McdAuthError("認証コードが正しくありません"))
    acc.McdClient = lambda fp, *a, **kw: mcd
    modal = acc.McdCredModal()
    modal.label, modal.email, modal.password = FakeValue(""), FakeValue("a@b.c"), FakeValue("pw")
    itx = FakeInteraction(owner, client)
    await modal.on_submit(itx)
    v = itx.last_view()
    for i in range(3):
        m = acc.McdOtpModal(v); m.otp = FakeValue("000000")
        it = FakeInteraction(owner, client)
        await m.on_submit(it)
        last = it.text()
    check("3回目でやり直しを促す", "最初から" in last, last[:80])
    check("接続を閉じている", mcd.closed)

    print("\n[6] Kyash: 認証コードありの登録")
    ky = FakeKyashClient(need_otp=True)
    acc.KyashClient = lambda *a, **kw: ky
    kmodal = acc.KyashCredModal()
    kmodal.label, kmodal.email, kmodal.password = FakeValue("K1"), FakeValue("k@b.c"), FakeValue("pw")
    itx = FakeInteraction(owner, client)
    await kmodal.on_submit(itx)
    check("認証コードの案内が出る", "認証コード" in itx.text(), itx.text()[:80])
    kview = itx.last_view()
    kotp = acc.KyashOtpModal(kview); kotp.otp = FakeValue("123456")
    itx2 = FakeInteraction(owner, client)
    await kotp.on_submit(itx2)
    async with session_scope() as s:
        from sqlalchemy import select
        krows = (await s.execute(select(KyashAccount))).scalars().all()
    check("Kyashアカウントが保存される", len(krows) == 1, len(krows))
    if krows:
        k = krows[0]
        cipher = get_cipher()
        check("UUIDが2つとも保存される（次回OTP不要）",
              k.client_uuid == "CU" and k.installation_uuid == "IU",
              (k.client_uuid, k.installation_uuid))
        check("アクセストークンが暗号化されている", cipher.decrypt(k.access_token_enc) == "tok")
        check("パスワードも保存される（再ログイン用）", cipher.decrypt(k.password_enc) == "pw")
        check("本人確認の状態を記録", k.is_kyc is True)
        check("残高を記録", k.last_balance == 3000, k.last_balance)
        check("取得時刻を記録（1ヶ月で失効するため）", k.token_obtained_at is not None)
    check("次回はOTP不要と伝える", "認証コードなし" in itx2.text(), itx2.text()[:100])

    print("\n[7] Kyash: 認証コード不要で登録できる場合")
    ky2 = FakeKyashClient(need_otp=False)
    acc.KyashClient = lambda *a, **kw: ky2
    kmodal = acc.KyashCredModal()
    kmodal.label, kmodal.email, kmodal.password = FakeValue("K2"), FakeValue("k2@b.c"), FakeValue("pw")
    itx = FakeInteraction(owner, client)
    await kmodal.on_submit(itx)
    check("そのまま登録が完了する", "登録しました" in itx.text(), itx.text()[:80])
    async with session_scope() as s:
        from sqlalchemy import select, func
        n = await s.scalar(select(func.count()).select_from(KyashAccount))
    check("2件目が保存される", n == 2, n)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
