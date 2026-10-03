"""
利用者カードの検証

複数のコマンドを行き来しないと全体像がつかめない状態を解消するための画面。
特に「なぜこの負担率なのか」が分かることが大事。
"""
import asyncio, os, sys, tempfile, uuid
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from _fake_discord import FakeInteraction, FakeUser, FakeClient

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

TARGET = 5555


class Role:
    def __init__(self, rid): self.id = rid


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/uc.db")
    from core import ledger as L, saga, settings, users as user_repo
    await settings.load_all()
    await settings.set_value("subsidy_rate", 40.0, updated_by=1)

    from db.models import Order
    from ui import admin_flows

    await user_repo.get_or_create(TARGET)
    async with user_scope(TARGET) as s:
        await L.charge(s, TARGET, 5000, receipt_id="r1")

    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        for i, (amount, price, state) in enumerate([
            (480, 800, saga.COMPLETED),
            (360, 600, saga.COMPLETED),
            (300, 500, saga.REFUNDED),
        ]):
            s.add(Order(
                id=str(uuid.uuid4()), discord_id=TARGET, state=state,
                store_id="13934", store_name="南砂町店", pickup_method="eatIn",
                hex_payload="0a05313339", items_json="[]", subsidy_amount=price - amount,
                list_price=price, user_amount=amount, subsidy_rate=40,
                receipt_number=f"700{i}", idempotency_key=str(uuid.uuid4()),
                created_at=now - timedelta(hours=i),
            ))
        u = await s.get(__import__("db.models", fromlist=["User"]).User, TARGET)
        u.total_orders = 12

    admin = FakeUser(1)
    target = FakeUser(TARGET, name="利用者さん")
    target.roles = []
    client = FakeClient()

    print("\n[1] 1画面にまとまる ★")
    itx = FakeInteraction(admin, client)
    await admin_flows.show_user(itx, target)
    t = itx.text()
    check("残高が出る", "5,000" in t or "¥5,000" in t or "4,160" in t or "5000" in t, t[:200])
    check("通算の注文回数が出る", "12" in t, t[:300])
    check("今月の支払いと負担が出る", "今月" in t, t[:300])
    check("直近の注文が出る", "南砂町店" in t, t[:400])
    check("注文番号も出る", "7000" in t, t[:500])
    check("チャージ履歴が出る", "チャージ" in t, t[:600])

    print("\n[2] 負担率の根拠が分かる ★")
    check("負担率が出る", "40" in t, t[:400])
    check("利用者の支払い割合も出る", "60" in t, t[:400])
    check("根拠が書いてある ★", "根拠" in t and "全体設定" in t, t[:500])

    print("\n[3] ロール別の設定だと根拠が変わる ★")
    from db.models import SubsidyRule
    async with session_scope() as s:
        s.add(SubsidyRule(scope="role", target_id=999, subsidy_rate=70,
                          enabled=True, priority=0))
    target.roles = [Role(999)]
    itx2 = FakeInteraction(admin, client)
    await admin_flows.show_user(itx2, target)
    t2 = itx2.text()
    check("ロールの負担率が適用される ★", "70" in t2, t2[:400])
    check("根拠がロールになる ★", "ロール" in t2, t2[:500])

    print("\n[4] 利用停止が目立つ ★")
    async with session_scope() as s:
        from db.models import User
        u = await s.get(User, TARGET)
        u.is_banned = True
        u.note = "規約違反のため"
    itx3 = FakeInteraction(admin, client)
    await admin_flows.show_user(itx3, target)
    t3 = itx3.text()
    check("停止中と分かる ★", "利用停止中" in t3, t3[:150])
    check("理由も出る", "規約違反" in t3, t3[:200])
    async with session_scope() as s:
        from db.models import User
        u = await s.get(User, TARGET)
        u.is_banned = False

    print("\n[5] そのまま深掘りできる")
    view = itx.last_view()
    labels = [getattr(c, "label", "") for c in view.children]
    check("履歴ボタンがある", any("履歴" in l for l in labels), labels)
    check("操作記録ボタンがある", any("操作記録" in l for l in labels), labels)

    hist = next(c for c in view.children if "履歴" in getattr(c, "label", ""))
    itx4 = FakeInteraction(admin, client)
    await hist.callback(itx4)
    check("履歴が開ける", "南砂町店" in itx4.text(), itx4.text()[:200])

    print("\n[6] 操作記録が引ける")
    from core import audit
    await audit.record(actor_id=1, actor_name="管理者", action="balance.grant",
                       target=str(TARGET), after="+1,000円", reason="お詫び")
    aud = next(c for c in view.children if "操作記録" in getattr(c, "label", ""))
    itx5 = FakeInteraction(admin, client)
    await aud.callback(itx5)
    check("この人への操作が出る", "残高を付与" in itx5.text(), itx5.text()[:200])
    check("理由も出る", "お詫び" in itx5.text(), itx5.text()[:250])

    print("\n[7] 初めての利用者でも落ちない")
    newbie = FakeUser(9999, name="新人")
    newbie.roles = []
    itx6 = FakeInteraction(admin, client)
    await admin_flows.show_user(itx6, newbie)
    check("表示できる", bool(itx6.actions), itx6.kinds)
    check("注文が無い旨を伝える", "まだありません" in itx6.text(), itx6.text()[:300])

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
