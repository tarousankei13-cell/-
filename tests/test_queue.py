"""
注文の同時実行と待機列の検証

一番大事なのは「**待っている間も残高は確保したまま**」で、
諦めたときに**必ず解放される**こと。片方でも崩れると
残高が合わなくなる。
"""
import asyncio, os, sys, tempfile, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from core.queue import OrderGate, QueueFull, QueueTimeout
from db.session import init_db, session_scope, user_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    print("\n[1] 同時に入れる数を守る ★")
    g = OrderGate(limit=2, max_wait=5)
    peak = [0]
    async def work(i, delay=0.05):
        async with g.enter(i):
            peak[0] = max(peak[0], g.running)
            await asyncio.sleep(delay)
    await asyncio.gather(*[work(i) for i in range(6)])
    check("上限を超えない ★", peak[0] == 2, peak[0])
    check("全員が処理される", g.total_queued == 6, g.total_queued)
    check("最後は空になる", g.running == 0 and g.waiting == 0, g.describe())

    print("\n[2] 順番待ちの人数が分かる")
    g = OrderGate(limit=1, max_wait=5)
    started = asyncio.Event()
    async def hold():
        async with g.enter(1):
            started.set()
            await asyncio.sleep(0.2)
    t = asyncio.create_task(hold())
    await started.wait()
    waiters = [asyncio.create_task(work_gate(g, i)) for i in (2, 3, 4)]
    await asyncio.sleep(0.05)
    check("待っている人数が分かる", g.waiting == 3, g.waiting)
    check("何番目か分かる", g.position(3) == 2, g.position(3))
    check("並んでいない人は0", g.position(99) == 0, g.position(99))
    await asyncio.gather(t, *waiters)

    print("\n[3] 待機列がいっぱいなら断る")
    g = OrderGate(limit=1, max_wait=5, max_queue=2)
    started = asyncio.Event()
    async def hold2():
        async with g.enter(1):
            started.set()
            await asyncio.sleep(0.3)
    t = asyncio.create_task(hold2())
    await started.wait()
    ws = [asyncio.create_task(work_gate(g, i)) for i in (2, 3)]
    await asyncio.sleep(0.05)
    try:
        await g.acquire(4)
        err = None
    except QueueFull as e:
        err = str(e)
    check("QueueFull になる", err is not None, err)
    check("混んでいる旨を伝える", err and "混み合って" in err, err)
    check("断った数を数える", g.total_rejected == 1, g.total_rejected)
    await asyncio.gather(t, *ws)

    print("\n[4] 待ちすぎたら諦める")
    g = OrderGate(limit=1, max_wait=0.1)
    async def long_hold():
        async with g.enter(1):
            await asyncio.sleep(0.5)
    t = asyncio.create_task(long_hold())
    await asyncio.sleep(0.05)
    try:
        await g.acquire(2)
        err = None
    except QueueTimeout as e:
        err = str(e)
    check("QueueTimeout になる", err is not None, err)
    check("待機列から外れる", g.waiting == 0, g.waiting)
    await t
    check("席は空く", g.running == 0, g.running)

    print("\n[5] 例外で抜けても席を返す ★")
    g = OrderGate(limit=1, max_wait=2)
    try:
        async with g.enter(1):
            raise RuntimeError("途中で失敗")
    except RuntimeError:
        pass
    check("席が空いている ★", g.running == 0, g.running)
    async with g.enter(2):
        check("次の人が入れる ★", g.running == 1)

    print("\n[6] 上限を変えられる")
    g = OrderGate(limit=2)
    g.set_limit(5)
    check("上限が変わる", g.limit == 5, g.limit)
    g.set_limit(0)
    check("0以下は1にする", g.limit == 1, g.limit)

    print("\n[7] 諦めたときに残高が戻る ★")
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/q.db")
    from core import ledger as L, saga
    from core.subsidy import Quote, calculate
    from services.mcd.protocol import DecodedOrder, OrderItem

    uid = 7001
    async with user_scope(uid) as s:
        await L.charge(s, uid, 5000, receipt_id="r1")
    u, sub = calculate(800, 40.0)
    quote = Quote(list_price=800, subsidy_rate=40.0, user_amount=u,
                  subsidy_amount=sub, source="test")
    decoded = DecodedOrder(store_id="13934", pickup_method="eatIn",
                           items=[OrderItem("9180", amount=800)], raw_hex="0a")
    order_id = await saga.create_order(
        discord_id=uid, decoded=decoded, quote=quote, pickup_method="eatIn",
        store_name="テスト店", group="group-f", idempotency_key=str(uuid.uuid4()),
    )
    async with session_scope() as s:
        from db.models import Order
        o = await s.get(Order, order_id)
        before = await L.user_balance(s, uid)
    check("作った直後はまだ残高を押さえていない", o.state == saga.QUOTED, o.state)
    check("残高はそのまま", before == 5000, before)

    # 残高を確保した状態にして、そこから取り消す
    async with user_scope(uid) as s:
        await L.hold(s, uid, u, order_id=order_id)
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        o.state = saga.BALANCE_HELD
        held = await L.user_balance(s, uid)
    check("確保すると残高が減る ★", held == 5000 - u, held)

    await saga.cancel_waiting(order_id, "混み合っています")
    async with session_scope() as s:
        after = await L.user_balance(s, uid)
    check("諦めたら全額戻る ★", after == 5000, after)
    async with session_scope() as s:
        from db.models import Order
        o = await s.get(Order, order_id)
    check("注文は返金済みになる", o.state == saga.REFUNDED, o.state)

    print("\n[8] 送信を始めていたら取り消さない ★")
    order_id2 = await saga.create_order(
        discord_id=uid, decoded=decoded, quote=quote, pickup_method="eatIn",
        store_name="テスト店", group="group-f", idempotency_key=str(uuid.uuid4()),
    )
    async with session_scope() as s:
        from db.models import Order
        o = await s.get(Order, order_id2)
        o.state = saga.MCD_AUTHORISING     # 決済を試みた状態
    await saga.cancel_waiting(order_id2, "混み合っています")
    async with session_scope() as s:
        o = await s.get(Order, order_id2)
    check("状態を変えない ★", o.state == saga.MCD_AUTHORISING, o.state)
    async with session_scope() as s:
        bal = await L.user_balance(s, uid)
    check("残高も動かさない ★", bal == 5000, bal)

    print("\n[9] 知らない注文でも落ちない")
    try:
        await saga.cancel_waiting("存在しないID", "理由")
        err = None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    check("例外を出さない", err is None, err or "")

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


async def work_gate(g, i):
    async with g.enter(i):
        await asyncio.sleep(0.01)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
