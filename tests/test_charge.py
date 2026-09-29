"""チャージ処理の検証 — Kyashをモックして、お金の流れを確かめる"""
import asyncio, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher, get_cipher
from db.session import init_db, session_scope, close_db
from core import ledger as L, settings, users as user_repo
from services.kyash import accounts as kyash_accounts, charge as kyash_charge
from services.kyash.client import KyashError, LinkAlreadyUsed, LinkInfo, Wallet
from services.kyash.charge import ChargeError
from db.models import KyashAccount, KyashReceipt, utcnow

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

class FakeKyash:
    """Kyashの偽物。残高の増え方や失敗の起こし方を指定できる。"""
    def __init__(self, amount=1000, send_to_me=True, balance=5000,
                 delta=None, check_error=None, receive_error=None):
        self.amount = amount; self.send_to_me = send_to_me
        self.balance = balance
        self.delta = amount if delta is None else delta
        self.check_error = check_error; self.receive_error = receive_error
        self.received = []
    async def link_check(self, url):
        if self.check_error: raise self.check_error
        return LinkInfo(amount=self.amount, uuid=f"uuid-{url[-6:]}",
                        send_to_me=self.send_to_me, sender_name="送金太郎")
    async def link_receive(self, link_uuid):
        if self.receive_error: raise self.receive_error
        self.received.append(link_uuid)
        self.balance += self.delta
        return {}
    async def get_wallet(self):
        return Wallet(uuid="w", all_balance=self.balance)
    async def aclose(self): pass

class FakeHandle:
    def __init__(self, client): self.account_id = 1; self.label = "test"; self.client = client
    async def aclose(self): pass

CUR = {"client": None}
async def fake_pick(amount): return FakeHandle(CUR["client"])
async def fake_record(aid, amount, balance=None): pass
async def fake_fail(aid, err): pass

async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/c.db")
    await settings.load_all()
    kyash_accounts.pick_account = fake_pick
    kyash_accounts.record_received = fake_record
    kyash_accounts.report_failure = fake_fail
    kyash_charge.kyash_accounts = kyash_accounts

    async with session_scope() as s:
        s.add(KyashAccount(id=1, label="t", email_enc=b"x", token_obtained_at=utcnow()))

    UID = 8001
    await user_repo.get_or_create(UID)

    print("\n[1] 正常なチャージ")
    CUR["client"] = FakeKyash(amount=1000)
    r = await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/aaa111")
    check("¥1,000 を受け取った", r.amount == 1000, r.amount)
    check("残高に反映された", r.balance == 1000, r.balance)
    check("送金者名を記録", r.sender_name == "送金太郎", r.sender_name)
    async with session_scope() as s:
        check("残高は1000円", await L.user_balance(s, UID) == 1000)

    print("\n[2] ★同じリンクは2回使えない")
    CUR["client"] = FakeKyash(amount=1000)
    try:
        await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/aaa111")
        check("2回目を拒否", False, "通ってしまった")
    except ChargeError as e:
        check("2回目を拒否", "使用されています" in str(e), str(e))
    async with session_scope() as s:
        check("残高が二重に増えていない", await L.user_balance(s, UID) == 1000,
              await L.user_balance(s, UID))

    print("\n[3] 請求リンクは受け取らない")
    CUR["client"] = FakeKyash(amount=500, send_to_me=False)
    try:
        await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/bbb222")
        check("請求リンクを拒否", False)
    except ChargeError as e:
        check("請求リンクを拒否", "請求リンク" in str(e), str(e))

    print("\n[4] 金額の範囲外")
    CUR["client"] = FakeKyash(amount=50)
    try:
        await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/ccc333")
        check("下限未満を拒否", False)
    except ChargeError as e:
        check("下限未満を拒否", "以上" in str(e), str(e))
    CUR["client"] = FakeKyash(amount=999999)
    try:
        await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/ddd444")
        check("上限超えを拒否", False)
    except ChargeError as e:
        check("上限超えを拒否", "まで" in str(e), str(e))

    print("\n[5] 使用済みリンク")
    CUR["client"] = FakeKyash(check_error=LinkAlreadyUsed("処理済みです"))
    try:
        await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/eee555")
        check("使用済みを拒否", False)
    except ChargeError as e:
        check("使用済みを拒否", "使用できません" in str(e), str(e))

    print("\n[6] ★残高の差分が合わない（記帳せず要確認へ）")
    before = None
    async with session_scope() as s:
        before = await L.user_balance(s, UID)
    CUR["client"] = FakeKyash(amount=1000, delta=300)   # 1000円のはずが300円しか増えない
    try:
        await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/fff666")
        check("不一致を検出して中断", False, "通ってしまった")
    except ChargeError as e:
        check("不一致を検出して中断", "確認中" in str(e), str(e))
    async with session_scope() as s:
        check("残高を増やしていない", await L.user_balance(s, UID) == before,
              await L.user_balance(s, UID))
        from sqlalchemy import select
        rows = (await s.execute(
            select(KyashReceipt).where(KyashReceipt.status == "MANUAL_REVIEW")
        )).scalars().all()
        check("要確認として記録される", len(rows) == 1, len(rows))

    print("\n[7] 受け取りが失敗した場合")
    async with session_scope() as s:
        before = await L.user_balance(s, UID)
    CUR["client"] = FakeKyash(amount=1000, receive_error=KyashError("受け取りに失敗"))
    try:
        await kyash_charge.charge_from_link(UID, "https://kyash.me/payments/ggg777")
        check("失敗として扱う", False)
    except ChargeError as e:
        check("失敗として扱う", "失敗" in str(e), str(e))
    async with session_scope() as s:
        check("残高は変わらない", await L.user_balance(s, UID) == before)

    print("\n[8] 同時に同じリンクを使おうとした場合")
    CUR["client"] = FakeKyash(amount=1000)
    results = await asyncio.gather(*[
        kyash_charge.charge_from_link(UID, "https://kyash.me/payments/hhh888")
        for _ in range(5)
    ], return_exceptions=True)
    succeeded = [r for r in results if not isinstance(r, Exception)]
    check(f"5回同時でも1回だけ成功（実際{len(succeeded)}回）", len(succeeded) == 1,
          [type(r).__name__ for r in results])

    print("\n[9] 元帳の整合性")
    async with session_scope() as s:
        rep = await L.verify_integrity(s)
        check(f"全{rep.checked}取引で貸借一致", not rep.broken, rep.broken)
        check("マイナス残高なし", not rep.negative, rep.negative)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
