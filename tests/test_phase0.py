"""Phase 0 の検証: DBスキーマ・暗号化・複式元帳"""
import asyncio, sys, tempfile, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.session import init_db, session_scope, user_scope, close_db
from core.locks import LockError
from core.crypto import Cipher, CryptoError
from core import ledger as L

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name} {extra}")

async def main():
    tmp = tempfile.mkdtemp()
    await init_db(f"sqlite+aiosqlite:///{tmp}/t.db")

    print("\n[1] 暗号化")
    c = Cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    for s in ["refresh_token_abc", "パスワード漢字", "", "x"*5000]:
        check(f"往復 ({len(s)}文字)", c.decrypt(c.encrypt(s)) == s)
    check("None は None のまま", c.encrypt(None) is None and c.decrypt(None) is None)
    check("毎回異なる暗号文 (nonce)", c.encrypt("same") != c.encrypt("same"))
    try:
        Cipher("別の鍵ですよこちらは").decrypt(c.encrypt("secret")); check("鍵違いを検出", False)
    except CryptoError: check("鍵違いを検出", True)

    print("\n[2] 元帳 — 基本")
    U = 12345
    async with user_scope(U) as s:
        await L.charge(s, U, 1000, receipt_id="r1")
    async with session_scope() as s:
        check("チャージ後の残高 1000", await L.user_balance(s, U) == 1000)

    print("\n[3] 元帳 — 貸借不一致は拒否")
    async with session_scope() as s:
        try:
            await L.post(s, [L.Entry("a", 100), L.Entry("b", -50)], kind="bad")
            check("不一致を拒否", False)
        except L.LedgerError: check("不一致を拒否", True)
        try:
            await L.post(s, [L.Entry("a", 0), L.Entry("b", 0)], kind="bad")
            check("金額0を拒否", False)
        except L.LedgerError: check("金額0を拒否", True)

    print("\n[4] 元帳 — ホールドと確定 (定価590 / 負担40%)")
    async with user_scope(U) as s:
        await L.hold(s, U, 354, order_id="o1")
    async with session_scope() as s:
        check("利用可能 646", await L.user_balance(s, U) == 646, await L.user_balance(s, U))
        check("ホールド 354", await L.held_balance(s, U) == 354)
    async with user_scope(U) as s:
        await L.capture(s, U, 354, 236, order_id="o1")
    async with session_scope() as s:
        check("確定後ホールド 0", await L.held_balance(s, U) == 0)
        check("settlement 590 (=354+236)", await L.balance(s, L.SETTLEMENT) == 590)
        check("subsidy_pool -236", await L.balance(s, L.SUBSIDY_POOL) == -236)

    print("\n[5] 元帳 — 解放")
    async with user_scope(U) as s:
        await L.hold(s, U, 100, order_id="o2")
    async with user_scope(U) as s:
        await L.release(s, U, 100, order_id="o2")
    async with session_scope() as s:
        check("解放で残高が戻る 646", await L.user_balance(s, U) == 646)

    print("\n[6] 元帳 — 残高不足は拒否")
    async with user_scope(U) as s:
        try:
            await L.hold(s, U, 999999, order_id="o3"); check("残高不足を拒否", False)
        except L.InsufficientBalance as e:
            check("残高不足を拒否", True); check("必要額と現在額を通知", e.required==999999 and e.available==646)

    print("\n[7] 元帳 — 同時実行10件でマイナスにならない")
    V = 999
    async with user_scope(V) as s:
        await L.charge(s, V, 1000, receipt_id="r2")
    async def try_hold(i):
        try:
            async with user_scope(V) as s:
                await L.hold(s, V, 300, order_id=f"c{i}")
            return True
        except Exception:
            return False
    res = await asyncio.gather(*[try_hold(i) for i in range(10)])
    async with session_scope() as s:
        bal, held = await L.user_balance(s, V), await L.held_balance(s, V)
    check(f"3件だけ成功 (実際{sum(res)}件) / 残高{bal} ホールド{held}", sum(res) == 3 and bal == 100 and held == 900, f"bal={bal} held={held}")

    print("\n[7b] ロックを取らずに記帳したら止まるか")
    async with session_scope() as s:
        try:
            await L.hold(s, U, 10, order_id="nolock"); check("ロック外の記帳を拒否", False)
        except LockError: check("ロック外の記帳を拒否", True)

    print("\n[8] 整合性チェック")
    async with session_scope() as s:
        r = await L.verify_integrity(s)
        check(f"全{r.checked}取引で貸借一致", not r.broken, r.broken)
        check("マイナス残高なし", not r.negative, r.negative)
        check("未使用残高総額が正", await L.outstanding_user_balance(s) >= 0)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
