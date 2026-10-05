"""
人数が増えたときに壊れないか

⚠️ 利用者が少ないうちは何も起きないが、増えた瞬間に
   **突然使えなくなる**たぐいの不具合をここで捕まえる。
   実際に匿名コードの衝突で「その人だけBOTが使えない」状態が起きていた。

⚠️ 定期処理が人数に比例して重くなっていないかも見る。
   10分おきに走るものが人数ぶんのSQLを投げると、
   利用者が増えたときにBOT全体が遅くなる。
"""
import asyncio, math, os, random, sys, tempfile, uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import func, select

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/sc.db")
    from core import users as ur
    from db.models import Order, User

    # ========================================================
    print("\n[ 匿名コードが衝突しないか ]")
    # ========================================================
    space = 16 ** ur.CODE_LENGTH
    check(f"取りうる値が十分ある（{space:,}通り）★", space >= 16 ** 6, space)
    # 4桁だと300人で5割衝突する。そこに戻っていないことを見張る。
    p300 = 1 - math.exp(-300 * 299 / (2 * space))
    check("300人でも衝突がまれ（1%未満）★", p300 < 0.01, f"{p300*100:.1f}%")

    check("同じ人はいつも同じコード ★", ur.anon_code(12345) == ur.anon_code(12345))
    check("違う人は違うコード", ur.anon_code(1) != ur.anon_code(2))

    N = 3000
    random.seed(20261005)
    ids = [random.randint(10 ** 17, 10 ** 18) for _ in range(N)]
    async with session_scope() as s:
        for uid in ids:
            await ur.ensure_user(s, uid)
    async with session_scope() as s:
        rows = (await s.execute(select(func.count()).select_from(User))).scalar()
        uniq = (await s.execute(
            select(func.count(func.distinct(User.anon_code)))
        )).scalar()
    check(f"{N}人すべて登録できる ★", rows == N, rows)
    check("コードが全員ちがう ★", uniq == N, (uniq, N))

    print("\n[ わざと衝突させても登録できるか ]")
    a, b = 777001, 777002
    async with session_scope() as s:
        await ur.ensure_user(s, a)
        # a のコードを「b が取るはずのコード」に書き換えて、ぶつける
        want_b = ur.anon_code(b)
        await s.execute(
            User.__table__.update()
            .where(User.discord_id == a).values(anon_code=want_b)
        )
        await s.flush()
        made = await ur.ensure_user(s, b)
    check("ぶつかっても登録できる ★", made is not None)
    check("別のコードが割り当てられる ★", made.anon_code != want_b, made.anon_code)
    check("同じ人なら2回目も同じコード ★",
          (await ur.get_or_create(b)).anon_code == made.anon_code)

    # ========================================================
    print("\n[ 招待コードが衝突しないか ]")
    # ========================================================
    import math as _m

    from core import invite as inv
    ispace = len(inv.ALPHABET) ** inv.CODE_LENGTH
    check(f"取りうる値が十分ある（{ispace:,}通り）★", ispace > 10 ** 8, ispace)
    p5000 = 1 - _m.exp(-5000 * 4999 / (2 * ispace))
    check("5000人でも衝突は数%以内 ★", p5000 < 0.05, f"{p5000*100:.2f}%")
    check("塩なしなら今までと同じ値 ★",
          inv.code_for(42) == inv.code_for(42, salt=0))
    check("塩を足せば別の値 ★", inv.code_for(42) != inv.code_for(42, salt=1))

    # 2人とも登録したうえで、わざとぶつける
    await ur.get_or_create(880001)
    await ur.get_or_create(880002)
    want = inv.code_for(880002)
    async with session_scope() as s:
        await s.execute(
            User.__table__.update()
            .where(User.discord_id == 880001).values(invite_code=want)
        )
    got = await inv.ensure_code(880002)
    check("ぶつかっても受け取れる ★", bool(got))
    check("別の値が割り当てられる ★", got != want, got)
    async with session_scope() as s:
        stored = (await s.execute(
            select(User.invite_code).where(User.discord_id == 880002)
        )).scalar()
    check("返した値とDBが一致する ★", got == stored, (got, stored))
    check("そのコードで本人を引ける ★", await inv.find_inviter(got) == 880002)
    check("2回目も同じ値（配ったリンクが死なない）★",
          await inv.ensure_code(880002) == got)

    # ========================================================
    print("\n[ 定期処理が人数に比例して重くならないか ]")
    # ========================================================
    from core import ledger as L
    from core import settings
    from core.locks import lock_user
    from services import outreach
    await settings.load_all()
    await settings.set_value("nudge_enabled", True)
    await settings.set_value("nudge_idle_days", 14)
    await settings.set_value("nudge_idle_min_balance", 300)

    old = datetime.now(timezone.utc) - timedelta(days=60)
    counts = {}
    for size in (200, 1500):
        await close_db()
        await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/s{size}.db")
        await settings.load_all()
        await settings.set_value("nudge_enabled", True)
        await settings.set_value("nudge_idle_days", 14)
        await settings.set_value("nudge_idle_min_balance", 300)

        async with session_scope() as s:
            for i in range(size):
                await ur.ensure_user(s, 900_000 + i)
                s.add(Order(
                    id=str(uuid.uuid4()), idempotency_key=str(uuid.uuid4()),
                    discord_id=900_000 + i, state="COMPLETED", hex_payload="00",
                    store_id="1", items_json="[]", list_price=500,
                    subsidy_rate=40, user_amount=300, subsidy_amount=200,
                    created_at=old,
                ))
        # 残高があるのは5人だけ（＝ほとんどが対象外になる形）
        for i in range(5):
            async with lock_user(900_000 + i):
                async with session_scope() as s:
                    await L.adjust(s, 900_000 + i, 1000, memo="テスト")

        from sqlalchemy import event
        import db.session as ds
        n = [0]
        @event.listens_for(ds._engine.sync_engine, "before_cursor_execute")
        def _count(*a, **kw): n[0] += 1

        got = await outreach.idle_balance_targets(20)
        counts[size] = n[0]
        check(f"{size}人で対象を正しく絞れる", len(got) == 5, len(got))

    check(f"SQLの回数が人数で変わらない ★（200人 {counts[200]} 回 / "
          f"1500人 {counts[1500]} 回）",
          counts[1500] <= counts[200] + 2, counts)
    check("1回あたりのSQLが十分少ない ★", counts[1500] <= 10, counts[1500])

    print("\n[ まとめて残高を引く ]")
    async with session_scope() as s:
        bulk = await L.user_balances(s, [900_000, 900_001, 900_999])
        one = await L.user_balance(s, 900_000)
    check("まとめて引いた値が1件ずつと同じ ★", bulk[900_000] == one, (bulk, one))
    check("元帳に無い人は0 ★", bulk[900_999] == 0, bulk)
    async with session_scope() as s:
        check("空で渡しても落ちない", await L.user_balances(s, []) == {})
        many = await L.user_balances(s, list(range(800_000, 800_000 + 1200)))
    check("1200人ぶんでもまとめて引ける（IN句の上限）★", len(many) == 1200, len(many))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
