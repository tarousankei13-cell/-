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
    def __init__(self, fail_at=None, error=McdOrderError("テスト用の失敗")):
        self.fail_at = fail_at
        self.error = error
        self.authorise_calls = 0

    async def ensure_auth(self, force=False): self._maybe("auth")
    async def get_pos_paseto(self, group): self._maybe("pos"); return "v2.local.fake"
    async def store_order(self, group, body):
        self._maybe("store")
        return OrderResponse(order_code="OC123", order_token="TOKEN123")
    async def authorise_order(self, group, token):
        self.authorise_calls += 1
        self._maybe("authorise")
        return OrderResponse(order_code="OC123", display_order_number="7161")
    async def get_paid_order(self, group, token):
        self._maybe("paid")
        return OrderResponse(order_code="OC123", display_order_number="7161")
    async def fetch_store(self, store_id, group=None):
        return {"store": {"name": "テスト店", "api": {
            "catRootUrl": "https://example.invalid",
            "ordRootUrl": "https://ord.group-f.prod.mop.mcd.qorcommerce.com"},
            "deliveryMethod": {"takeOut": {"isSupported": True}}}}, "group-f"
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
async def fake_failure(aid, err): return "ACTIVE"
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
    client.authorise_order = lambda g, t: _raise_after_auth(client)
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
    client.authorise_order = lambda g, t: _raise_after_auth(client)
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
    client.authorise_order = lambda g, t: _raise_after_auth(client)
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
