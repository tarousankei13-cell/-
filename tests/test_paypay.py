"""
PayPay でのチャージ

⚠️ お金が動く経路なので、Kyash と同じ厳しさで見る。
   ・同じリンクを2回使えないこと
   ・残高の差分が合わなければ記帳しないこと
   ・受け取れない状態のリンクを弾くこと
"""
import asyncio, os, sys, tempfile, uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/pp.db")
    from core import settings, ledger as L, users as user_repo
    from db.models import PayPayAccount, PayPayReceipt, utcnow
    await settings.load_all()

    from services.paypay import client as C
    from services.paypay import accounts as PA
    from services.paypay import charge as PC

    async def balance(uid):
        async with session_scope() as s:
            return await L.user_balance(s, uid)

    print("── ① リンクの読み取り ──")
    check("URLから確認コードを取り出す ★",
          C.normalize_link("https://pay.paypay.ne.jp/abcd1234") == "abcd1234")
    check("コードだけでも通る", C.normalize_link("abcd1234") == "abcd1234")
    check("クエリを落とす",
          C.normalize_link("https://pay.paypay.ne.jp/abcd1234?x=1") == "abcd1234")
    check("前後の空白を落とす", C.normalize_link("  abcd1234  ") == "abcd1234")
    check("空は空", C.normalize_link("") == "" and C.normalize_link(None) == "")

    print("\n── ② 結果コードの判定 ──")
    C.PayPayClient._check({"header": {"resultCode": "S0000"}})
    check("S0000 は通す", True)
    C.PayPayClient._check({})
    check("header が無くても落ちない", True)
    for code, want, why in (
        ("S0001", C.PayPayLoginError, "ログイン切れ"),
        ("E9999", C.PayPayError, "その他の失敗"),
    ):
        try:
            C.PayPayClient._check({"header": {"resultCode": code}})
            check(f"{why} を弾く ★", False)
        except want:
            check(f"{why} を弾く ★", True)
    try:
        C.PayPayClient._check({
            "header": {"resultCode": "E1"},
            "error": {"displayErrorResponse": {
                "description": "しばらく時間をおいて、再度お試しください"}},
        })
        check("混雑を見分ける ★", False)
    except C.PayPayError as e:
        check("混雑を見分ける ★", "混み合って" in str(e), e)

    print("\n── ③ 受け取れる状態か ──")
    def info(status="PENDING", **kw):
        return C.LinkInfo(
            amount=kw.get("amount", 1000), order_id="o1", message_id="m1",
            chat_room_id="c1", status=status,
            has_password=kw.get("has_password", False), sender_name="送った人",
        )
    check("PENDING は受け取れる ★", info().receivable)
    for st in ("SUCCESS", "REJECTED", "FAILED"):
        check(f"{st} は受け取れない ★", not info(st).receivable)

    print("\n── ④ 口座の選び方 ──")
    try:
        await PA.pick_account(100)
        check("口座が無ければ断る ★", False)
    except C.PayPayError as e:
        check("口座が無ければ断る ★", "ありません" in str(e), e)

    from core.crypto import get_cipher
    cipher = get_cipher()
    async with session_scope() as s:
        s.add(PayPayAccount(
            id=1, label="本口座", phone_enc=cipher.encrypt("09000000000"),
            access_token_enc=cipher.encrypt("tok"), device_uuid="dev",
            client_uuid="cli", token_obtained_at=utcnow(),
        ))
        s.add(PayPayAccount(
            id=2, label="未ログイン", phone_enc=cipher.encrypt("09011111111"),
        ))
    handle = await PA.pick_account(100)
    check("トークンのある口座を選ぶ ★", handle.account_id == 1, handle.account_id)
    await handle.aclose()

    async with session_scope() as s:
        (await s.get(PayPayAccount, 1)).monthly_cap = 500
    try:
        await PA.pick_account(1000)
        check("月間上限を超える口座は選ばない ★", False)
    except C.PayPayError:
        check("月間上限を超える口座は選ばない ★", True)
    async with session_scope() as s:
        (await s.get(PayPayAccount, 1)).monthly_cap = None

    acc = None
    async with session_scope() as s:
        acc = await s.get(PayPayAccount, 1)
        s.expunge(acc)
    check("トークンの残りは90日基準 ★",
          89 <= (PA.token_days_left(acc) or 0) <= 90, PA.token_days_left(acc))

    print("\n── ⑤ チャージを通す ──")
    UID = 4001
    await user_repo.get_or_create(UID)

    class FakeClient:
        def __init__(self, amount=1000, status="PENDING", has_password=False,
                     delta=None):
            self.amount = amount; self.status = status
            self.has_password = has_password
            self.wallet = 5000
            self.delta = amount if delta is None else delta
            self.received = 0
        async def link_check(self, url):
            return C.LinkInfo(
                amount=self.amount, order_id=f"order-{url}", message_id="m",
                chat_room_id="c", status=self.status,
                has_password=self.has_password, sender_name="送った人",
            )
        async def link_receive(self, url, info=None, passcode=None):
            if info and not info.receivable:
                raise C.LinkAlreadyUsed("済み")
            self.received += 1
            self.wallet += self.delta
            return {}
        async def get_balance(self):
            return C.Balance(all_balance=self.wallet)
        async def aclose(self): pass

    CUR = {}
    async def fake_pick(amount):
        return PA.PayPayHandle(1, "本口座", CUR["client"])
    PA.pick_account = fake_pick
    PC.paypay_accounts.pick_account = fake_pick

    CUR["client"] = FakeClient(1000)
    before = await balance(UID)
    r = await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link001")
    check("チャージできた ★", r.amount == 1000, r.amount)
    check("残高が増えた ★", await balance(UID) == before + 1000)
    check("送金者名を残す", r.sender_name == "送った人")
    check("受け取りは1回だけ呼ばれる ★", CUR["client"].received == 1)

    print("\n── ⑥ 同じリンクは2回使えない ──")
    try:
        await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link001")
        check("2回目は断る ★", False)
    except PC.ChargeError as e:
        check("2回目は断る ★", "すでに使用" in str(e), e)
    check("残高は増えない ★", await balance(UID) == before + 1000)

    print("\n── ⑦ 受け取れない状態のリンク ──")
    CUR["client"] = FakeClient(1000, status="SUCCESS")
    try:
        await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link002")
        check("受け取り済みは断る ★", False)
    except PC.ChargeError as e:
        check("受け取り済みは断る ★", "済んで" in str(e), e)
    check("受け取りを呼んでいない ★", CUR["client"].received == 0)

    print("\n── ⑧ 金額の上下限 ──")
    await settings.set_value("charge_min", 100)
    await settings.set_value("charge_max", 50000)
    CUR["client"] = FakeClient(50)
    try:
        await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link003")
        check("下限より少なければ断る ★", False)
    except PC.ChargeError as e:
        check("下限より少なければ断る ★", "100" in str(e), e)
    CUR["client"] = FakeClient(99999)
    try:
        await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link004")
        check("上限を超えたら断る ★", False)
    except PC.ChargeError as e:
        check("上限を超えたら断る ★", "50,000" in str(e), e)

    print("\n── ⑨ パスコード付きのリンク ──")
    CUR["client"] = FakeClient(1000, has_password=True)
    try:
        await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link005")
        check("パスコード無しは断る ★", False)
    except PC.ChargeError as e:
        check("パスコード無しは断る ★", "パスコード" in str(e), e)
    before = await balance(UID)
    await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link005", passcode="1234")
    check("パスコードがあれば通る ★", await balance(UID) == before + 1000)

    print("\n── ⑩ 残高の差分が合わないとき ──")
    CUR["client"] = FakeClient(1000, delta=500)   # 1000円のはずが500円しか増えない
    before = await balance(UID)
    try:
        await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link006")
        check("差分が合わなければ記帳しない ★", False)
    except PC.ChargeError as e:
        check("差分が合わなければ記帳しない ★", "確認中" in str(e), e)
    check("残高は増えない ★", await balance(UID) == before)
    async with session_scope() as s:
        from sqlalchemy import select
        rows = (await s.execute(
            select(PayPayReceipt).where(PayPayReceipt.status == "MANUAL_REVIEW")
        )).scalars().all()
    check("人の確認が要る印を付ける ★", len(rows) == 1, len(rows))

    print("\n── ⑪ チャージ率がきく ──")
    await settings.set_value("charge_rate", 120)
    CUR["client"] = FakeClient(1000)
    before = await balance(UID)
    r = await PC.charge_from_link(UID, "https://pay.paypay.ne.jp/link007")
    check("送金額はそのまま記録 ★", r.amount == 1000, r.amount)
    check("残高には1,200入る ★", r.credited == 1200 and
          await balance(UID) == before + 1200, (r.credited, await balance(UID) - before))
    from core import limits
    # ⚠️ PayPay のぶんも累計に入ること（初回チャージ条件で使う）。
    #    ここを見落とすと、PayPayで入れた人が永久に条件を満たせない。
    total = await limits.charged_total(UID)
    check("PayPayのチャージも累計に入る ★", total > 0, total)
    check("数えるのは送金額（率を掛ける前）★", total % 1000 == 0, total)
    await settings.set_value("charge_rate", 100)

    print("\n── ⑫ 画面での振り分け ──")
    from ui.flows import detect_method, charge_methods
    check("PayPayのリンクを見分ける ★",
          detect_method("https://pay.paypay.ne.jp/abc") == "paypay")
    check("Kyashのリンクを見分ける ★",
          detect_method("https://kyash.me/payments/x") == "kyash")
    check("分からないものは空 ★（間違った相手に聞かない）",
          detect_method("ただの文字") == "")
    check("既定は両方使える", charge_methods() == ["kyash", "paypay"])
    await settings.set_value("charge_methods", "paypay")
    check("PayPayだけにもできる", charge_methods() == ["paypay"])
    await settings.set_value("charge_methods", "kyash")
    check("Kyashだけにもできる", charge_methods() == ["kyash"])
    await settings.set_value("charge_methods", "both")

    print("\n── ⑬ ログインの手順を飛ばしていないか ★ ──")
    # ⚠️ 相手は www.paypay.ne.jp 側で Cookie を積み上げながら進む。
    #    いきなり資格情報を送っても通らない。順番を固定する。
    calls = []

    class RecordingClient(C.PayPayClient):
        async def _request(self, method, url, **kw):
            calls.append((method, url.split("?")[0]))
            if url.endswith("/par") or "oauth2/par" in url and "check" not in url:
                return {"header": {"resultCode": "S0000"},
                        "payload": {"requestUri": "urn:req:1"}}
            if "par/check" in url:
                return {"header": {"resultCode": "S0000"}}
            if "sign-in/password" in url:
                return {"header": {"resultCode": "S0000"}, "payload": {}}
            return {"header": {"resultCode": "S0000"}, "payload": {}}

    class FakeHTTP:
        def __init__(self): self.gets = []
        async def get(self, url, **kw):
            self.gets.append(url)
            class R: status_code = 200
            return R()
        async def request(self, *a, **kw): raise AssertionError("使わない")
        async def aclose(self): pass

    cl = RecordingClient()
    cl._client = FakeHTTP()
    got = await cl.start_login("090-1234-5678", "pw")
    paths = [u for _, u in calls]
    check("① まず par で要求を登録する ★",
          paths and paths[0].endswith("/bff/v2/oauth2/par"), paths)
    check("② authorize と sign-in 画面をたどる ★",
          any("oauth2/authorize" in u for u in cl._client.gets)
          and any("portal/oauth2/sign-in" in u for u in cl._client.gets),
          cl._client.gets)
    check("③ par/check を通す ★",
          any("par/check" in u for u in paths), paths)
    check("④ 最後に資格情報を送る ★",
          paths[-1].endswith("/sign-in/password"), paths)
    check("資格情報を par へ送っていない ★（順番の取り違え）",
          not any(u.endswith("/bff/v2/oauth2/par") for u in paths[1:]), paths)
    check("端末が未登録ならSMS待ちになる ★", got == {"done": False}, got)

    class RegisteredClient(RecordingClient):
        async def _request(self, method, url, **kw):
            if "sign-in/password" in url:
                calls.append((method, url.split("?")[0]))
                return {"header": {"resultCode": "S0000"},
                        "payload": {"redirectUrl":
                                    "paypay://oauth2/callback?code=AUTHCODE&state=x"}}
            if "oauth2/token" in url:
                calls.append((method, url.split("?")[0]))
                return {"header": {"resultCode": "S0000"},
                        "payload": {"accessToken": "AT", "refreshToken": "RT"}}
            return await super()._request(method, url, **kw)

    calls.clear()
    cl2 = RegisteredClient()
    cl2._client = FakeHTTP()
    got = await cl2.start_login("09012345678", "pw")
    check("登録済み端末ならSMSなしで入れる ★", got.get("done") is True, got)
    check("そのままトークンを受け取る ★",
          got["session"].access_token == "AT"
          and got["session"].refresh_token == "RT",
          got.get("session"))
    check("トークン交換まで進む ★",
          any("oauth2/token" in u for _, u in calls), [u for _, u in calls])

    from services import tasks as jobs
    async with session_scope() as s:
        from datetime import timedelta
        a = await s.get(PayPayAccount, 1)
        a.token_obtained_at = utcnow() - timedelta(days=85)
    warns = await jobs.kyash_token_warnings()
    check("PayPayの期限も出る ★", any("PayPay" in w for w in warns), warns)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
