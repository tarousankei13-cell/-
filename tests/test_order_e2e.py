"""注文Sagaの検証 — マクドナルドAPIをモックして、金銭の流れを端から端まで確かめる"""
import asyncio, sys, os, tempfile, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from core import ledger as L, saga, settings, users as user_repo
from core.subsidy import Quote, calculate
from services.mcd import accounts as mcd_accounts
from services.mcd.client import McdOrderError, McdNetworkError, OrderResponse
from services.mcd import stores as mcd_stores
from services.mcd.protocol import DecodedOrder, OrderItem
from db.models import Order, User

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

# ---- モック --------------------------------------------------

class FakeClient:
    """マクドナルドAPIの偽物。どこで失敗させるかを指定できる。"""
    def __init__(self, fail_at=None, error=McdOrderError("テスト用の失敗"),
                 total_amount=0):
        self.fail_at = fail_at
        self.error = error
        self.authorise_calls = 0
        # マクドナルドが言う金額。0 なら「言ってこなかった」扱い
        self.total_amount = total_amount

    async def ensure_auth(self, force=False): self._maybe("auth")
    async def get_pos_paseto(self, group): self._maybe("pos"); return "v2.local.fake"
    async def store_order(self, group, body):
        self._maybe("store")
        return OrderResponse(order_code="OC123", order_token="TOKEN123",
                             total_amount=self.total_amount)
    async def authorise_order(self, group, token, pickup_method="takeOut"):
        self.authorise_calls += 1
        self._maybe("authorise")
        return OrderResponse(order_code="OC123", display_order_number="7161")
    async def get_paid_order(self, group, token):
        self._maybe("paid")
        return OrderResponse(order_code="OC123", display_order_number="7161")
    async def fetch_store(self, store_id, group=None, etag=None):
        return {"store": {"name": "テスト店", "api": {
            "catRootUrl": "https://example.invalid",
            "ordRootUrl": "https://ord.group-f.prod.mop.mcd.qorcommerce.com"},
            "deliveryMethod": {"takeOut": {"isSupported": True}}}}, "group-f", "etag-1"
    async def fetch_menu(self, store_id, cat_root_url, etag=None):
        return None, etag or ""
    async def aclose(self): pass
    def _maybe(self, step):
        if self.fail_at == step: raise self.error

class FakeHandle:
    def __init__(self, client): self.account_id = 1; self.label="test"; self.card_id="card"; self.client = client
    async def aclose(self): pass

CURRENT = {"client": None}
async def fake_pick(exclude=None): return FakeHandle(CURRENT["client"])
async def fake_open(account_id): return FakeHandle(CURRENT["client"])
async def fake_success(aid): pass
async def fake_failure(aid, err, *, fatal=False): return "QUARANTINED" if fatal else "ACTIVE"
async def fake_fresh(client, store_id): pass

def install_mocks():
    mcd_accounts.pick_account = fake_pick
    mcd_accounts.open_account = fake_open
    mcd_accounts.report_success = fake_success
    mcd_accounts.report_failure = fake_failure
    saga.mcd_accounts = mcd_accounts
    mcd_stores.ensure_menu_fresh = fake_fresh

DECODED = DecodedOrder(
    store_id="13934", pickup_method="eatIn",
    items=[OrderItem("9180", amount=800, components=[OrderItem("9987009", components=[OrderItem("2020")])])],
    raw_hex="0a0531333933",
)

def make_quote(price=800, rate=40.0):
    u, sub = calculate(price, rate)
    return Quote(list_price=price, subsidy_rate=rate, user_amount=u, subsidy_amount=sub, source="test")

async def new_order(uid, quote):
    return await saga.create_order(
        discord_id=uid, decoded=DECODED, quote=quote, pickup_method="eatIn",
        store_name="テスト店", group="group-f", idempotency_key=str(uuid.uuid4()),
    )

# ---- テスト --------------------------------------------------

async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/e2e.db")
    await settings.load_all()
    install_mocks()

    # 注文は mcd_accounts を参照するので、テスト用の行を1件入れておく
    from db.models import McdAccount
    async with session_scope() as sess:
        sess.add(McdAccount(
            id=1, label="test", email_enc=b"x",
            device_uid="d", wmop_device_id="w", fb_instance_id="f",
            home_lat=35.0, home_lng=139.0,
        ))

    print("\n[1] 正常系（定価800 / 負担40% → 利用者480）")
    q = make_quote()
    check(f"利用者 {q.user_amount}円 / 負担 {q.subsidy_amount}円", q.user_amount == 480 and q.subsidy_amount == 320)
    uid = 1001
    await user_repo.get_or_create(uid)
    async with user_scope(uid) as s: await L.charge(s, uid, 2000, receipt_id="r1")
    CURRENT["client"] = FakeClient()
    oid = await new_order(uid, q)
    r = await saga.execute(oid)
    check("注文が成立した", r.succeeded, r.state)
    check("注文番号 7161 を取得", r.receipt_number == "7161", r.receipt_number)
    async with session_scope() as s:
        check("残高 2000-480=1520", await L.user_balance(s, uid) == 1520, await L.user_balance(s, uid))
        check("ホールドが残っていない", await L.held_balance(s, uid) == 0)
        check("settlement に定価800", await L.balance(s, L.SETTLEMENT) == 800, await L.balance(s, L.SETTLEMENT))
        check("subsidy_pool -320", await L.balance(s, L.SUBSIDY_POOL) == -320)

    print("\n[2] 残高不足")
    uid2 = 1002
    await user_repo.get_or_create(uid2)
    async with user_scope(uid2) as s: await L.charge(s, uid2, 100, receipt_id="r2")
    CURRENT["client"] = FakeClient()
    r = await saga.execute(await new_order(uid2, make_quote()))
    check("注文されない", not r.succeeded and r.state == saga.REFUNDED, r.state)
    async with session_scope() as s:
        check("残高が減っていない", await L.user_balance(s, uid2) == 100)

    print("\n[3] 決済前の失敗 → 自動返金")
    uid3 = 1003
    await user_repo.get_or_create(uid3)
    async with user_scope(uid3) as s: await L.charge(s, uid3, 2000, receipt_id="r3")
    CURRENT["client"] = FakeClient(fail_at="store")
    r = await saga.execute(await new_order(uid3, make_quote()))
    check("返金された", r.state == saga.REFUNDED, r.state)
    async with session_scope() as s:
        check("残高が全額戻っている", await L.user_balance(s, uid3) == 2000, await L.user_balance(s, uid3))
        check("ホールドが解放されている", await L.held_balance(s, uid3) == 0)

    print("\n[4] ★決済後の失敗 → 自動返金せず要確認へ")
    uid4 = 1004
    await user_repo.get_or_create(uid4)
    async with user_scope(uid4) as s: await L.charge(s, uid4, 2000, receipt_id="r4")
    client = FakeClient(fail_at="paid")
    client.authorise_order = lambda g, t, pm="takeOut": _raise_after_auth(client)
    CURRENT["client"] = client
    oid4 = await new_order(uid4, make_quote())
    r = await saga.execute(oid4)
    check("要確認になった（自動返金していない）", r.needs_review, r.state)
    async with session_scope() as s:
        check("ホールドされたまま（勝手に戻さない）", await L.held_balance(s, uid4) == 480, await L.held_balance(s, uid4))

    print("\n[5] 要確認を管理者が「成立」で処理")
    await saga.resolve_review(oid4, refund=False)
    async with session_scope() as s:
        check("ホールドが確定された", await L.held_balance(s, uid4) == 0)
        o = await s.get(Order, oid4)
        check("状態が COMPLETED", o.state == saga.COMPLETED, o.state)

    print("\n[6] 要確認を管理者が「返金」で処理")
    uid5 = 1005
    await user_repo.get_or_create(uid5)
    async with user_scope(uid5) as s: await L.charge(s, uid5, 2000, receipt_id="r5")
    # 決済も確認も通らない状況（成否が分からない）→ 要確認になるはず
    client = FakeClient(fail_at="paid")
    client.authorise_order = lambda g, t, pm="takeOut": _raise_after_auth(client)
    CURRENT["client"] = client
    oid5 = await new_order(uid5, make_quote())
    r5 = await saga.execute(oid5)
    check("成否不明なので要確認になる", r5.needs_review, r5.state)
    await saga.resolve_review(oid5, refund=True)
    async with session_scope() as s:
        check("全額返金された", await L.user_balance(s, uid5) == 2000, await L.user_balance(s, uid5))

    print("\n[6b] ★応答だけ失われたケース（決済は成立していた）")
    uid5b = 10051
    await user_repo.get_or_create(uid5b)
    async with user_scope(uid5b) as s: await L.charge(s, uid5b, 2000, receipt_id="r5b")
    client = FakeClient()                       # GetPaidOrder は成功する
    client.authorise_order = lambda g, t, pm="takeOut": _raise_after_auth(client)
    CURRENT["client"] = client
    oid5b = await new_order(uid5b, make_quote())
    r5b = await saga.execute(oid5b)
    check("確認して成立と判断し、完了させた", r5b.succeeded, r5b.state)
    check("注文番号も取り戻した", r5b.receipt_number == "7161", r5b.receipt_number)
    async with session_scope() as s:
        check("二重に引かれていない", await L.user_balance(s, uid5b) == 1520, await L.user_balance(s, uid5b))

    print("\n[7] ★冪等性（ボタン連打で二重注文しない）")
    uid6 = 1006
    await user_repo.get_or_create(uid6)
    async with user_scope(uid6) as s: await L.charge(s, uid6, 5000, receipt_id="r6")
    CURRENT["client"] = FakeClient()
    key = str(uuid.uuid4())
    q = make_quote()
    ids = [await saga.create_order(
        discord_id=uid6, decoded=DECODED, quote=q, pickup_method="eatIn",
        store_name="テスト店", group="group-f", idempotency_key=key) for _ in range(5)]
    check("5回押しても注文は1件", len(set(ids)) == 1, ids)
    await saga.execute(ids[0])
    async with session_scope() as s:
        from sqlalchemy import func, select
        cnt = await s.scalar(select(func.count()).select_from(Order).where(Order.discord_id == uid6))
        check("DB上も1件だけ", cnt == 1, cnt)
        check("1回分しか引かれていない", await L.user_balance(s, uid6) == 4520, await L.user_balance(s, uid6))

    print("\n[8] ★AuthoriseOrder を二度呼ばない")
    uid7 = 1007
    await user_repo.get_or_create(uid7)
    async with user_scope(uid7) as s: await L.charge(s, uid7, 2000, receipt_id="r7")
    c = FakeClient()
    CURRENT["client"] = c
    await saga.execute(await new_order(uid7, make_quote()))
    check(f"AuthoriseOrder の呼び出しは1回（実際{c.authorise_calls}回）", c.authorise_calls == 1, c.authorise_calls)

    print("\n[9] ★クラッシュからの復旧（StoreOrder直後に落ちた想定）")
    uid8 = 1008
    await user_repo.get_or_create(uid8)
    async with user_scope(uid8) as s: await L.charge(s, uid8, 2000, receipt_id="r8")
    CURRENT["client"] = FakeClient()
    oid8 = await new_order(uid8, make_quote())
    # 途中まで進めた状態を再現する
    async with user_scope(uid8) as s:
        tx = await L.hold(s, uid8, 480, order_id=oid8)
        o = await s.get(Order, oid8)
        o.hold_tx_id = tx; o.state = saga.MCD_STORED
        o.order_token = "TOKEN123"; o.mcd_account_id = 1
    results = await saga.recover_pending()
    check(f"{len(results)}件を復旧した", len(results) >= 1)
    async with session_scope() as s:
        o = await s.get(Order, oid8)
        check("復旧して完了状態になった", o.state in (saga.CAPTURED, saga.COMPLETED), o.state)
        check("注文番号を取り戻した", o.receipt_number == "7161", o.receipt_number)
        check("残高が正しく確定", await L.user_balance(s, uid8) == 1520, await L.user_balance(s, uid8))

    print("\n[9b] ★復旧を繰り返しても壊れないか")
    async with session_scope() as s:
        u8 = await s.get(User, uid8)
        orders_before = u8.total_orders
        bal_before = await L.user_balance(s, uid8)
    again = await saga.recover_pending()
    check(f"2回目の復旧では何も拾わない（{len(again)}件）", len(again) == 0, [r.state for r in again])
    async with session_scope() as s:
        u8 = await s.get(User, uid8)
        check(f"利用回数が二重に増えない（{orders_before}回のまま）",
              u8.total_orders == orders_before, u8.total_orders)
        check("残高も変わらない", await L.user_balance(s, uid8) == bal_before)

    print("\n[9c] 利用回数のカウント")
    async with session_scope() as s:
        u1 = await s.get(User, 1001)
        check(f"正常注文1件で利用回数1（実際{u1.total_orders}）", u1.total_orders == 1, u1.total_orders)

    print("\n[9d] ★値段が変わっていたら、決済せずに止める")
    # ⚠️ カタログは最大 MENU_STALE_MINUTES 分古いことがある。
    #    値上げに気づかずに進むと、利用者からは古い安い金額を取り、
    #    カードには新しい高い金額が請求される。差額は管理者が被る。
    #    StoreOrder は課金前なので、ここで止めれば1円も動かない。
    uid9 = 1009
    await user_repo.get_or_create(uid9)
    async with user_scope(uid9) as s:
        await L.charge(s, uid9, 5000, receipt_id="r9")
    async with session_scope() as s:
        bal9 = await L.user_balance(s, uid9)

    CURRENT["client"] = FakeClient(total_amount=850)      # 見積り800 → 実際850
    oid9 = await new_order(uid9, make_quote())
    r9 = await saga.execute(oid9)
    check("注文は成立しない ★", not r9.succeeded, r9.state)
    check("返金された（課金前なので全額戻る）★", r9.state == saga.REFUNDED, r9.state)
    async with session_scope() as s:
        now9 = await L.user_balance(s, uid9)
    check("残高が元どおり ★", now9 == bal9, (bal9, now9))
    check("決済を呼んでいない ★", CURRENT["client"].authorise_calls == 0,
          CURRENT["client"].authorise_calls)
    check("食い違いを記録している ★", r9.price_changed == (800, 850), r9.price_changed)
    msg = r9.user_message
    check("利用者に金額の変化を伝える ★", "¥800" in msg and "¥850" in msg, msg[:160])
    check("支払いが無いことを伝える ★", "発生していません" in msg, msg[:200])
    check("「時間外」とは言わない ★", "お時間" not in msg, msg[:200])

    print("\n[9d-2] 本人が承知すれば、その金額で通る ★")
    # ⚠️⚠️ **セットの上乗せ額は計算で出せない**（docs/09 V-23）。
    #    カフェラテは参照より安いのに +¥50、野菜生活100 は高いのに
    #    +¥0 で、カタログのどこにも書かれていない。
    #    当てにいって中止していると、既定の組み合わせしか注文できない。
    #    マクドナルドが言う本当の金額を見せて、本人に決めてもらう。
    uid9b = 1092
    await user_repo.get_or_create(uid9b)
    async with user_scope(uid9b) as s:
        await L.charge(s, uid9b, 5000, receipt_id="r9b")
    async with session_scope() as s:
        bal9b = await L.user_balance(s, uid9b)

    asked = []
    async def say_yes(expected, actual):
        asked.append((expected, actual))
        return True

    CURRENT["client"] = FakeClient(total_amount=1210)   # 見積り800 → 実際1210
    oid9b = await new_order(uid9b, make_quote())        # 補助率40%
    r9b = await saga.execute(oid9b, None, say_yes)
    check("本人に確かめている ★", asked == [(800, 1210)], asked)
    check("注文が成立する ★", r9b.succeeded, (r9b.state, r9b.error))
    async with session_scope() as s:
        o = await s.get(Order, oid9b)
        check("定価を直している ★", o.list_price == 1210, o.list_price)
        # 補助率40% → 利用者60%。1210 の 60% = 726
        want_user, want_sub = calculate(1210, 40.0)
        check(f"利用者の負担を計算し直している ★（¥{want_user}）",
              o.user_amount == want_user, o.user_amount)
        check(f"管理者の負担も計算し直している ★（¥{want_sub}）",
              o.subsidy_amount == want_sub, o.subsidy_amount)
        now9b = await L.user_balance(s, uid9b)
        check("本当の金額どおり引かれている ★", now9b == bal9b - want_user,
              (bal9b, now9b, want_user))
        check("ホールドが残っていない ★", await L.held_balance(s, uid9b) == 0)

    print("\n[9d-3] 断れば、1円も動かない ★")
    uid9c = 1093
    await user_repo.get_or_create(uid9c)
    async with user_scope(uid9c) as s:
        await L.charge(s, uid9c, 5000, receipt_id="r9c")
    async with session_scope() as s:
        bal9c = await L.user_balance(s, uid9c)

    async def say_no(expected, actual):
        return False

    CURRENT["client"] = FakeClient(total_amount=1210)
    r9c = await saga.execute(await new_order(uid9c, make_quote()), None, say_no)
    check("注文は成立しない ★", not r9c.succeeded, r9c.state)
    check("決済を呼んでいない ★", CURRENT["client"].authorise_calls == 0)
    async with session_scope() as s:
        check("残高が元どおり ★", await L.user_balance(s, uid9c) == bal9c)

    print("\n[9d-4] 残高が足りなければ進めない ★")
    # ⚠️ 本人が「はい」と言っても、払えないものは通せない。
    uid9d = 1094
    await user_repo.get_or_create(uid9d)
    async with user_scope(uid9d) as s:
        await L.charge(s, uid9d, 500, receipt_id="r9d")   # 800の60%=480 は足りる
    CURRENT["client"] = FakeClient(total_amount=5000)     # 5000の60%=3000 は足りない
    r9d = await saga.execute(await new_order(uid9d, make_quote()), None, say_yes)
    check("注文は成立しない ★", not r9d.succeeded, r9d.state)
    check("決済を呼んでいない ★", CURRENT["client"].authorise_calls == 0)
    async with session_scope() as s:
        check("残高が元どおり ★", await L.user_balance(s, uid9d) == 500,
              await L.user_balance(s, uid9d))

    print("\n[9d-5] 安くなったぶんは戻る ★")
    uid9e = 1095
    await user_repo.get_or_create(uid9e)
    async with user_scope(uid9e) as s:
        await L.charge(s, uid9e, 5000, receipt_id="r9e")
    async with session_scope() as s:
        bal9e = await L.user_balance(s, uid9e)
    CURRENT["client"] = FakeClient(total_amount=600)      # 見積り800 → 実際600
    r9e = await saga.execute(await new_order(uid9e, make_quote()), None, say_yes)
    check("注文が成立する ★", r9e.succeeded, (r9e.state, r9e.error))
    want_user, _ = calculate(600, 40.0)
    async with session_scope() as s:
        check(f"安いほうで引かれている ★（¥{want_user}）",
              await L.user_balance(s, uid9e) == bal9e - want_user,
              await L.user_balance(s, uid9e))
        check("ホールドが残っていない ★", await L.held_balance(s, uid9e) == 0)

    print("\n[9d-6] 確かめる手段が無ければ、今までどおり中止 ★")
    # ⚠️ 復旧処理など、本人に聞けない場面で勝手に高い金額を通さない。
    uid9f = 1096
    await user_repo.get_or_create(uid9f)
    async with user_scope(uid9f) as s:
        await L.charge(s, uid9f, 5000, receipt_id="r9f")
    CURRENT["client"] = FakeClient(total_amount=1210)
    r9f = await saga.execute(await new_order(uid9f, make_quote()))   # 第3引数なし
    check("中止する ★", not r9f.succeeded, r9f.state)
    check("決済を呼んでいない ★", CURRENT["client"].authorise_calls == 0)

    print("\n[9e] 金額が一致していれば、そのまま通る ★")
    uid10 = 1010
    await user_repo.get_or_create(uid10)
    async with user_scope(uid10) as s:
        await L.charge(s, uid10, 5000, receipt_id="r10")
    CURRENT["client"] = FakeClient(total_amount=800)      # 見積りと一致
    r10 = await saga.execute(await new_order(uid10, make_quote()))
    check("注文が成立する ★", r10.succeeded, (r10.state, r10.error))
    check("食い違いの記録は無い", r10.price_changed is None, r10.price_changed)

    print("\n[9f] 金額を言ってこない相手でも止めない ★")
    # 応答に金額が入らない実装・将来の仕様変更で注文が全部止まると困る
    uid11 = 1011
    await user_repo.get_or_create(uid11)
    async with user_scope(uid11) as s:
        await L.charge(s, uid11, 5000, receipt_id="r11")
    CURRENT["client"] = FakeClient(total_amount=0)
    r11 = await saga.execute(await new_order(uid11, make_quote()))
    check("金額不明でも注文は通る ★", r11.succeeded, (r11.state, r11.error))

    print("\n[10] 元帳の最終整合性")
    async with session_scope() as s:
        rep = await L.verify_integrity(s)
        check(f"全{rep.checked}取引で貸借一致", not rep.broken, rep.broken)
        check("マイナス残高なし", not rep.negative, rep.negative)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

async def _raise_after_auth(client):
    """AuthoriseOrder は成功するが、その後で失敗させる"""
    client.authorise_calls += 1
    from services.mcd.client import OrderResponse
    raise McdNetworkError("決済後に通信が切れた")

sys.exit(asyncio.run(main()))
