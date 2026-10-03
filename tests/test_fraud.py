"""
気になる動きの検知の検証

方針として「**疑わしきは止めない**」。ふつうに使っている人を誤って
止めるほうが痛いので、気付いて知らせるところまでにしてある。
その方針どおりに動くことを確かめる。
"""
import asyncio, os, sys, tempfile, uuid
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

UID = 8001


def order(uid, amount, price, state, when):
    from db.models import Order
    return Order(
        id=str(uuid.uuid4()), discord_id=uid, state=state,
        store_id="13934", store_name="テスト店", pickup_method="eatIn",
        hex_payload="0a05", items_json="[]",
        list_price=price, user_amount=amount, subsidy_rate=40,
        subsidy_amount=price - amount,
        idempotency_key=str(uuid.uuid4()), created_at=when,
    )


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/fr.db")
    from core import fraud, ledger as L, saga, settings, users as user_repo
    await settings.load_all()

    now = datetime.now(timezone.utc)
    await user_repo.get_or_create(UID)

    print("\n[1] ふつうの使い方では何も出さない ★")
    async with session_scope() as s:
        s.add(order(UID, 480, 800, saga.COMPLETED, now - timedelta(days=1)))
    r = await fraud.check_user(UID)
    check("何も見つからない ★", not r.any, [f.summary for f in r.findings])

    print("\n[2] 短時間に何度も注文したら気付く")
    async with session_scope() as s:
        for i in range(6):
            s.add(order(UID, 480, 800, saga.COMPLETED, now - timedelta(minutes=i)))
    r = await fraud.check_user(UID)
    burst = [f for f in r.findings if f.kind == "burst"]
    check("見つける", len(burst) == 1, [f.kind for f in r.findings])
    check("回数が文面に入る", "回注文" in burst[0].summary, burst[0].summary)
    check("注意どまり（要対応にはしない）", burst[0].level == fraud.WARN, burst[0].level)

    print("\n[3] 高額な注文に気付く")
    r = await fraud.check_order(UID, 15000)
    check("見つける", any(f.kind == "big_order" for f in r.findings), r.findings)
    r = await fraud.check_order(UID, 800)
    check("ふつうの金額では出さない", not r.any, [f.summary for f in r.findings])

    print("\n[4] チャージ直後の動きに気付く")
    async with user_scope(UID) as s:
        await L.charge(s, UID, 8000, receipt_id="big-1")
    r = await fraud.check_user(UID)
    quick = [f for f in r.findings if f.kind == "quick_spend"]
    check("見つける", len(quick) == 1, [f.kind for f in r.findings])
    check("参考どまり", quick[0].level == fraud.INFO, quick[0].level)

    print("\n[5] 設定で切れる ★")
    await settings.set_value("fraud_burst", False, updated_by=1)
    await settings.set_value("fraud_quick_spend", False, updated_by=1)
    r = await fraud.check_user(UID)
    check("切ったものは出なくなる ★",
          not [f for f in r.findings if f.kind in ("burst", "quick_spend")],
          [f.kind for f in r.findings])
    await settings.set_value("fraud_burst", True, updated_by=1)
    await settings.set_value("fraud_quick_spend", True, updated_by=1)

    print("\n[6] しきい値を変えられる")
    await settings.set_value("fraud_burst_count", 100, updated_by=1)
    r = await fraud.check_user(UID)
    check("上げれば出なくなる", not [f for f in r.findings if f.kind == "burst"],
          [f.kind for f in r.findings])
    await settings.set_value("fraud_burst_count", 5, updated_by=1)

    print("\n[7] 全体の点検")
    r = await fraud.scan_all()
    check("短時間の集中を拾う", any(f.kind == "burst" for f in r.findings),
          [f.kind for f in r.findings])
    check("本人のIDが分かる", any(f.discord_id == UID for f in r.findings),
          [f.discord_id for f in r.findings])

    print("\n[8] 残高のマイナスは要対応にする ★")
    from db.models import Ledger
    async with session_scope() as s:
        s.add(Ledger(account=f"user:{9002}", amount=-5000, tx_id=str(uuid.uuid4()),
                     kind="test", memo="検証用"))
    r = await fraud.scan_all()
    neg = [f for f in r.findings if f.kind == "negative"]
    check("見つける ★", len(neg) >= 1, [f.kind for f in r.findings])
    check("要対応にする ★", neg and neg[0].level == fraud.HIGH, neg[0].level if neg else None)
    check("誰の分か分かる", neg and neg[0].discord_id == 9002,
          neg[0].discord_id if neg else None)

    print("\n[9] 通知文")
    text = fraud.format_report(r)
    check("深刻な順に並ぶ", text.index("要対応") < text.index("注意")
          if "注意" in text else True, text[:120])
    check("自動で止めていないと明記する ★", "自動で止めたものではありません" in text,
          text[-120:])
    check("何も無ければ空", fraud.format_report(fraud.Report()) == "")

    print("\n[10] 注文は止まらない ★")
    import inspect
    from ui import flows
    src = inspect.getsource(flows.run_order)
    i = src.index("fraud.check_user")
    after = src[i:i + 600]
    check("検知の結果で return しない ★", "return" not in after.split("notify_admin_fraud")[0],
          after[:200])
    check("失敗しても注文を続ける", "注文には影響しません" in after, after[:400])

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
