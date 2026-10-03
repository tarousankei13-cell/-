"""
チャージ率と、注文の入口の条件

お金が動くところなので、
  ・いくら送って、いくら残高に入るか
  ・いくらから注文できるか
  ・はじめての注文の前にいくらチャージが要るか
を、端まで確かめる。

⚠️ 運営がいくら持ち出しているかを、利用者に見せていないことも確かめる。
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
    await init_db(f"sqlite+aiosqlite:///{tmp}/lim.db")
    from core import settings, limits, users as user_repo
    from db.models import KyashAccount, KyashReceipt, User, utcnow
    await settings.load_all()

    async def set_rate(r): await settings.set_value("charge_rate", r)

    async def add_receipt(uid, amount, status="CREDITED"):
        async with session_scope() as s:
            s.add(KyashReceipt(
                id=str(uuid.uuid4()), link_uuid=str(uuid.uuid4()),
                kyash_account_id=1, discord_id=uid, amount=amount,
                sender_name="テスト", status=status, raw_link="x",
            ))

    async def set_orders(uid, n):
        async with session_scope() as s:
            (await s.get(User, uid)).total_orders = n

    async with session_scope() as s:
        s.add(KyashAccount(id=1, label="t", email_enc=b"x",
                           token_obtained_at=utcnow()))
    for uid in range(6100, 6120):
        await user_repo.get_or_create(uid)

    print("── ① チャージ率の計算 ──")
    await set_rate(100)
    check("100%は送った額のまま", limits.credited_for(1000) == 1000)
    check("100%は上乗せ0", limits.bonus_for(1000) == 0)
    await set_rate(120)
    check("120%で1,000→1,200 ★", limits.credited_for(1000) == 1200,
          limits.credited_for(1000))
    check("上乗せは200", limits.bonus_for(1000) == 200)
    check("端数は切り捨て（333→399）★", limits.credited_for(333) == 399,
          limits.credited_for(333))
    check("1円でも落ちない", limits.credited_for(1) == 1)
    check("0円は0円", limits.credited_for(0) == 0)
    check("負の数でも落ちない", limits.credited_for(-100) == 0)
    await set_rate(150)
    check("150%で1,000→1,500", limits.credited_for(1000) == 1500)
    await set_rate(90)
    check("100%未満も設定できる（900）", limits.credited_for(1000) == 900)
    await set_rate(1000)
    check("上限1000%でも落ちない", limits.credited_for(1000) == 10000)
    await settings.set_value("charge_rate", 0)
    check("0を入れても1%として扱う（0除算や全没収を防ぐ）★",
          limits.charge_rate() == 1, limits.charge_rate())
    await set_rate(100)

    print("\n── ② 累計チャージ額の数え方 ──")
    A = 6100
    check("まだ0円", await limits.charged_total(A) == 0)
    await add_receipt(A, 500)
    await add_receipt(A, 500)
    check("500円×2回で1,000円 ★", await limits.charged_total(A) == 1000,
          await limits.charged_total(A))
    await add_receipt(A, 9999, status="FAILED")
    check("失敗した受取は数えない ★", await limits.charged_total(A) == 1000,
          await limits.charged_total(A))
    await add_receipt(6101, 777)
    check("人を取り違えない ★", await limits.charged_total(A) == 1000)
    await set_rate(200)
    check("チャージ率を上げても、数えるのは送った額 ★",
          await limits.charged_total(A) == 1000, await limits.charged_total(A))
    await set_rate(100)

    print("\n── ③ 最低注文額 ──")
    await settings.set_value("order_min", 0)
    await limits.check_order(A, 1)
    check("0なら1円でも通る", True)
    await settings.set_value("order_min", 500)
    try:
        await limits.check_order(A, 499)
        check("499円を弾く ★", False)
    except limits.LimitError as e:
        check("499円を弾く ★", "¥500" in str(e), e)
        check("いまの金額も伝える", "¥499" in str(e), e)
    await limits.check_order(A, 500)
    check("500円ちょうどは通る ★", True)
    await limits.check_order(A, 10000)
    check("それ以上も通る", True)

    print("\n── ④ はじめての注文の条件 ──")
    await settings.set_value("order_min", 0)
    await settings.set_value("first_charge_gate", False)
    B = 6102
    await limits.check_order(B, 1000)
    check("無効なら素通り ★", True)

    await settings.set_value("first_charge_gate", True)
    await settings.set_value("first_charge_min", 1000)
    try:
        await limits.check_order(B, 1000)
        check("チャージ0円なら弾く ★", False)
    except limits.LimitError as e:
        check("チャージ0円なら弾く ★", "¥1,000" in str(e), e)
        check("あといくら必要かを伝える ★", "あと" in str(e), e)
        check("2回目以降は不要と伝える", "2回目以降" in str(e), e)
    await add_receipt(B, 999)
    try:
        await limits.check_order(B, 1000)
        check("999円では足りない ★", False)
    except limits.LimitError as e:
        check("999円では足りない ★", "¥1" in str(e), e)
    await add_receipt(B, 1)
    await limits.check_order(B, 1000)
    check("累計1,000円ちょうどで通る ★", True)

    C = 6103
    await set_orders(C, 1)
    await limits.check_order(C, 1000)
    check("2回目以降はチャージ0でも通る ★", True)

    print("\n── ⑤ お知らせの文言 ──")
    D = 6104
    note = await limits.first_order_notice(D)
    check("未達の人には出る", "¥1,000" in note, note)
    check("あといくらかを伝える", "あと" in note, note)
    await add_receipt(D, 1000)
    check("達した人には出さない ★", await limits.first_order_notice(D) == "")
    E_ = 6105
    await set_orders(E_, 3)
    check("注文済みの人には出さない ★", await limits.first_order_notice(E_) == "")
    await settings.set_value("first_charge_gate", False)
    check("無効なら出さない", await limits.first_order_notice(6106) == "")

    print("\n── ⑥ 条件は順番どおりに見る ──")
    await settings.set_value("order_min", 500)
    await settings.set_value("first_charge_gate", True)
    F = 6107
    try:
        await limits.check_order(F, 100)
        check("金額が足りなければ、まず金額を伝える ★", False)
    except limits.LimitError as e:
        check("金額が足りなければ、まず金額を伝える ★",
              "ご注文は" in str(e) and "チャージ" not in str(e), e)
    try:
        await limits.check_order(F, 1000)
        check("金額が足りていれば、次にチャージを見る ★", False)
    except limits.LimitError as e:
        check("金額が足りていれば、次にチャージを見る ★", "チャージ" in str(e), e)
    await settings.set_value("order_min", 0)
    await settings.set_value("first_charge_gate", False)

    print("\n── ⑦ 利用者に見える画面に、負担が出ていないか ──")
    from ui import embeds
    from core.subsidy import Quote
    await settings.set_value("subsidy_rate", 40.0)
    q = Quote(list_price=590, user_amount=354, subsidy_amount=236,
              subsidy_rate=40.0, source="既定", capped=False)

    def body(e):
        out = (e.title or "") + (e.description or "")
        out += "".join(f.name + f.value for f in e.fields)
        out += (e.footer.text or "") if e.footer else ""
        return out

    screens = {
        "注文パネル": embeds.order_panel("both"),
        "注文内容の確認": embeds.order_preview(
            store_name="テスト店", store_id="13934",
            item_lines=["ハンバーガー ×1"], quote=q, balance=5000,
            pickup_label="お持ち帰り",
        ),
        "完了DM": embeds.dm_complete(
            receipt_number="123", store_name="テスト店", store_id="13934",
            pickup_label="お持ち帰り", list_price=590, user_amount=354,
            subsidy_rate=40.0, balance=1000, total_orders=1,
        ),
        "残高増減パネル": embeds.balance_change(
            display_name=None, anon_code="U-TEST", amount=-354, balance=646,
            reason="ご注文",
            fields=["anon_code", "list_price", "subsidy_rate",
                    "user_amount", "daily_count"],
            total_orders=1,
        ),
        "チャージパネル": embeds.charge_panel(),
    }
    for name, e in screens.items():
        t = body(e)
        check(f"{name}に「負担」が無い ★", "負担" not in t, t[:160])
        check(f"{name}に「% OFF」が無い ★", "OFF" not in t, t[:160])

    # 定価とお支払い額は出す（ご希望どおり、負担率の言及だけ消す）
    pv = body(screens["注文内容の確認"])
    check("注文確認にお支払い額は出る", "¥354" in pv, pv[:200])
    dm = body(screens["完了DM"])
    check("完了DMに定価が出る ★", "¥590" in dm, dm[:200])
    check("完了DMにお支払い額が出る ★", "¥354" in dm, dm[:200])
    check("残高パネルに負担率の項目が無い ★",
          not any("負担" in f.name for f in screens["残高増減パネル"].fields),
          [f.name for f in screens["残高増減パネル"].fields])

    print("\n── ⑧ チャージ画面 ──")
    await set_rate(120)
    cp = embeds.charge_panel()
    cbody = (cp.description or "") + "".join(f.name + f.value for f in cp.fields)
    check("増量中と出る ★", "120%" in cbody and "¥1,200" in cbody, cbody[:200])
    await set_rate(100)
    cp = embeds.charge_panel()
    cbody = (cp.description or "") + "".join(f.name + f.value for f in cp.fields)
    check("100%なら増量の案内は出さない ★", "増量" not in cbody, cbody[:200])
    await settings.set_value("first_charge_gate", True)
    cp = embeds.charge_panel()
    cbody = "".join(f.name + f.value for f in cp.fields)
    check("初回条件が出る ★", "はじめて" in cbody and "¥1,000" in cbody, cbody[:300])
    await settings.set_value("first_charge_gate", False)

    print("\n── ⑨ 実際のチャージ経路で率が効くか ──")
    from services.kyash import charge as kyash_charge
    from services.kyash import accounts as kyash_accounts
    from core import ledger as L

    class FakeInfo:
        def __init__(self, amount):
            self.amount = amount; self.uuid = str(uuid.uuid4())
            self.sender_name = "送金太郎"; self.send_to_me = True

    class FakeClient:
        def __init__(self, amount):
            self.amount = amount; self.wallet = 0
        async def link_check(self, url): return FakeInfo(self.amount)
        async def link_receive(self, uid): self.wallet += self.amount
        async def get_wallet(self):
            return type("W", (), {"all_balance": self.wallet})()
        async def aclose(self): pass

    class FakeHandle:
        def __init__(self, c): self.account_id = 1; self.client = c
        async def aclose(self): pass

    CUR = {}
    async def fake_pick(amount): return FakeHandle(CUR["client"])
    async def fake_record(*a, **k): pass
    kyash_accounts.pick_account = fake_pick
    kyash_accounts.record_received = fake_record
    kyash_charge.kyash_accounts = kyash_accounts

    G = 6110
    await set_rate(120)
    CUR["client"] = FakeClient(1000)
    r = await kyash_charge.charge_from_link(G, "https://kyash.me/payments/a1")
    check("送金額はそのまま記録 ★", r.amount == 1000, r.amount)
    check("残高には1,200入る ★", r.credited == 1200, r.credited)
    check("上乗せ200を持っている", r.bonus == 200, r.bonus)
    check("率も持っている", r.rate == 120, r.rate)
    async with session_scope() as s:
        check("元帳の残高も1,200 ★", await L.user_balance(s, G) == 1200,
              await L.user_balance(s, G))
    check("累計チャージは送金額の1,000 ★", await limits.charged_total(G) == 1000,
          await limits.charged_total(G))

    await set_rate(100)
    CUR["client"] = FakeClient(1000)
    r = await kyash_charge.charge_from_link(G, "https://kyash.me/payments/a2")
    check("100%に戻すと上乗せ無し ★", r.credited == 1000 and r.bonus == 0,
          (r.credited, r.bonus))
    async with session_scope() as s:
        check("残高は2,200", await L.user_balance(s, G) == 2200,
              await L.user_balance(s, G))

    await set_rate(150)
    CUR["client"] = FakeClient(333)
    r = await kyash_charge.charge_from_link(G, "https://kyash.me/payments/a3")
    check("端数は切り捨て（333×150%＝499）★", r.credited == 499, r.credited)
    await set_rate(100)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
