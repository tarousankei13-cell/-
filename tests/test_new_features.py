"""
通信を使う新しい機能

  ① できあがり通知
  ② 近くの店舗をさがす
  ③ 入金の照合（Kyash）
  ④ 返金（実際にお金が出ていく）
  ⑧ 新商品・価格改定のお知らせ

⚠️ ④はお金が出ていくので、歯止めが外れていないかを特に厚く見る。
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
    await init_db(f"sqlite+aiosqlite:///{tmp}/nf.db")
    from core import settings, ledger as L, users as user_repo
    from db.models import KyashAccount, KyashReceipt, Order, Refund, utcnow
    await settings.load_all()

    async def balance(uid):
        async with session_scope() as s:
            return await L.user_balance(s, uid)

    # ========================================================
    print("── ① できあがり通知 ──")
    from services import order_watch

    check("既定では無効 ★", not order_watch.enabled())
    check("無効なら見張らない ★", await order_watch.pending() == [])
    check("無効なら何も起きない ★", await order_watch.sweep() == [])
    await settings.set_value("ready_notify", True)
    check("有効にできる", order_watch.enabled())
    check("見張る時間が出る", order_watch.watch_minutes() == 30)
    check("間隔が出る", order_watch.poll_seconds() == 45)
    await settings.set_value("ready_poll_seconds", 5)
    check("短すぎる間隔は15秒に丸める ★", order_watch.poll_seconds() == 15,
          order_watch.poll_seconds())
    await settings.set_value("ready_poll_seconds", 45)

    from core import saga
    oid = str(uuid.uuid4())
    await user_repo.get_or_create(5001)
    async with session_scope() as s:
        s.add(Order(
            id=oid, idempotency_key=str(uuid.uuid4()), discord_id=5001,
            state=saga.COMPLETED, hex_payload="00", store_id="13934",
            store_name="南砂町店", group_name="group-f",
            list_price=500, subsidy_rate=0, user_amount=500, subsidy_amount=0,
            order_token="tok", mcd_account_id=None, receipt_number="7161",
        ))
    check("アカウントが無い注文は見張らない ★", await order_watch.pending() == [])

    async with session_scope() as s:
        # ⚠️ トークンの無い口座は照合の対象外（確かめる相手がいない）
        s.add(KyashAccount(id=1, label="t", email_enc=b"x",
                           access_token_enc=b"tok", token_obtained_at=utcnow()))
    from db.models import McdAccount
    async with session_scope() as s:
        s.add(McdAccount(
            id=1, label="m", email_enc=b"x", device_uid="d",
            wmop_device_id="w", fb_instance_id="f", home_lat=35.0, home_lng=139.0,
        ))
        row = await s.get(Order, oid)
        row.mcd_account_id = 1
    got = await order_watch.pending()
    check("条件がそろえば見張る ★", len(got) == 1, len(got))

    async with session_scope() as s:
        (await s.get(Order, oid)).ready_notified = True
    check("知らせ済みは見張らない ★", await order_watch.pending() == [])

    print("\n── ② 近くの店舗をさがす ──")
    from services.mcd import store_index as SI

    check("東京駅→新宿駅がおよそ6km ★",
          5.5 < SI.distance_km(35.6812, 139.7671, 35.6896, 139.7006) < 6.5,
          SI.distance_km(35.6812, 139.7671, 35.6896, 139.7006))
    check("同じ地点は0km", SI.distance_km(35.0, 139.0, 35.0, 139.0) == 0)
    check("位置が無ければ空を返す ★（間違った距離を出さない）",
          SI.nearby("99999") == [])

    idx = SI.StoreIndex()
    idx._entries = [
        SI.StoreEntry("1", "本店", "東京", "g", True, 35.6812, 139.7671),
        SI.StoreEntry("2", "近い店", "東京", "g", True, 35.6896, 139.7006),
        SI.StoreEntry("3", "遠い店", "大阪", "g", True, 34.7024, 135.4959),
        SI.StoreEntry("4", "非対応店", "東京", "g", False, 35.6850, 139.7500),
        SI.StoreEntry("5", "位置なし", "東京", "g", True, None, None),
    ]
    idx._by_id = {e.store_id: e for e in idx._entries}
    SI._index = idx
    near = SI.nearby("1", limit=5)
    check("近い順に並ぶ ★", [e.store_id for e, _ in near] == ["2"],
          [(e.store_id, round(k, 1)) for e, k in near])
    check("非対応店は出さない ★", "4" not in [e.store_id for e, _ in near])
    check("位置の無い店は出さない ★", "5" not in [e.store_id for e, _ in near])
    check("遠すぎる店は出さない ★（既定30km）", "3" not in [e.store_id for e, _ in near])
    near_all = SI.nearby("1", limit=5, mop_only=False)
    check("非対応店も出せる", "4" in [e.store_id for e, _ in near_all],
          [e.store_id for e, _ in near_all])
    check("自分自身は出さない ★", "1" not in [e.store_id for e, _ in near_all])
    far = SI.nearby("1", limit=5, max_km=1000)
    check("範囲を広げれば遠い店も出る", "3" in [e.store_id for e, _ in far])

    print("\n── ③ 入金の照合 ──")
    from services.kyash import reconcile

    async def receipt(amount, status="CREDITED", account_id=1):
        async with session_scope() as s:
            s.add(KyashReceipt(
                id=str(uuid.uuid4()), link_uuid=str(uuid.uuid4()),
                kyash_account_id=account_id, discord_id=5001, amount=amount,
                sender_name="t", status=status, raw_link="x",
            ))

    check("入金の向きを見分ける（受取）",
          reconcile._is_incoming({"type": "RECEIVED", "amount": 100}))
    check("入金の向きを見分ける（送金）",
          not reconcile._is_incoming({"type": "SEND", "amount": 100}))
    check("向きが書いていなければ金額で判断",
          reconcile._is_incoming({"amount": 100})
          and not reconcile._is_incoming({"amount": -100}))
    check("入れ子の金額も取り出せる",
          reconcile._amount_of({"amount": {"value": 350}}) == 350)
    check("金額が無ければ0", reconcile._amount_of({}) == 0)

    import services.kyash.accounts as kyash_accounts

    class FakeClient:
        def __init__(self, rows): self.rows = rows
        async def get_history(self, limit=10): return self.rows
        async def aclose(self): pass

    CUR = {"rows": []}
    kyash_accounts.build_client = lambda acc: FakeClient(CUR["rows"])

    await receipt(1000)
    CUR["rows"] = [{"type": "RECEIVED", "amount": 1000}]
    rep = await reconcile.check()
    check("そろっていれば食い違い無し ★", rep.clean, rep.summary())
    check("件数を数える", rep.checked == 1, rep.checked)

    CUR["rows"] = [{"type": "RECEIVED", "amount": 1000},
                   {"type": "RECEIVED", "amount": 500}]
    rep = await reconcile.check()
    check("Kyashにしか無い入金を見つける ★", len(rep.missing_here) == 1,
          rep.summary())
    check("食い違いがあると clean は False", not rep.clean)

    CUR["rows"] = []
    rep = await reconcile.check()
    check("こちらにしか無い記録を見つける ★", len(rep.missing_there) == 1,
          rep.summary())

    async with session_scope() as s:
        s.add(KyashAccount(id=2, label="まだ未ログイン", email_enc=b"y"))
    CUR["rows"] = [{"type": "RECEIVED", "amount": 1000}]
    rep = await reconcile.check()
    check("トークンの無い口座は飛ばす ★（確かめる相手がいない）",
          not rep.errors, rep.errors)

    await receipt(300, status="RECEIVING")
    CUR["rows"] = [{"type": "RECEIVED", "amount": 1000}]
    rep = await reconcile.check()
    check("途中で止まっているものを見つける ★", len(rep.stuck) == 1, rep.summary())
    check("止まっているものは照合の数に入れない ★", rep.checked == 1, rep.checked)

    print("\n── ④ 返金 ──")
    from services.kyash import refund as refund_svc

    check("既定では無効 ★", not refund_svc.enabled())
    try:
        await refund_svc.send(5001, 100)
        check("無効なら返金できない ★", False)
    except refund_svc.RefundError as e:
        check("無効なら返金できない ★", "受け付けていません" in str(e), e)

    await settings.set_value("refund_enabled", True)
    await settings.set_value("refund_max", 10000)
    for bad, why in ((0, "0円"), (-100, "マイナス")):
        try:
            await refund_svc.send(5001, bad)
            check(f"{why}は断る ★", False)
        except refund_svc.RefundError:
            check(f"{why}は断る ★", True)
    try:
        await refund_svc.send(5001, 99999)
        check("上限を超えたら断る ★", False)
    except refund_svc.RefundError as e:
        check("上限を超えたら断る ★", "¥10,000" in str(e), e)

    before = await balance(5001)
    try:
        await refund_svc.send(5001, 500)
        check("残高が足りなければ断る ★", False)
    except refund_svc.RefundError as e:
        check("残高が足りなければ断る ★", "足りません" in str(e), e)
    check("断ったとき残高は動かない ★", await balance(5001) == before)

    from db.session import user_scope
    async with user_scope(5001) as s:
        await L.adjust(s, 5001, 3000, memo="test")
    before = await balance(5001)

    class FakeHandle:
        account_id = 1
        def __init__(self, url): self.client = self; self.url = url
        async def create_link(self, amount, message=""): 
            if self.url is None:
                raise RuntimeError("作れません")
            return self.url
        async def aclose(self): pass

    LINK = {"url": "https://kyash.me/payments/refund1"}
    async def fake_pick(amount): return FakeHandle(LINK["url"])
    kyash_accounts.pick_account = fake_pick

    made = await refund_svc.send(5001, 1000, reason="テスト", requested_by=9)
    check("返金できた ★", made.link_url == LINK["url"], made.link_url)
    check("残高が引かれた ★", await balance(5001) == before - 1000,
          await balance(5001) - before)
    check("記録が残る", made.status == "PENDING" and made.amount == 1000)
    check("誰の指示かも残る", made.requested_by == 9)

    from sqlalchemy import select
    from db.models import Ledger
    async with session_scope() as s:
        rows = (await s.execute(
            select(Ledger).where(Ledger.memo.like("%返金%"))
        )).scalars().all()
    check("元帳に返金の記録がある ★", len(rows) > 0, len(rows))

    LINK["url"] = None
    before2 = await balance(5001)
    try:
        await refund_svc.send(5001, 500)
        check("リンクを作れなければ失敗する ★", False)
    except refund_svc.RefundError as e:
        check("リンクを作れなければ失敗する ★", "元に戻して" in str(e), e)
    check("失敗したら残高を戻す ★（引いたままにしない）",
          await balance(5001) == before2, await balance(5001) - before2)
    hist = await refund_svc.history(5001)
    check("失敗も記録に残る ★",
          any(r.status == "FAILED" for r in hist), [r.status for r in hist])

    print("\n── ⑧ 新商品・価格改定のお知らせ ──")
    from services import tasks as jobs
    from services.tasks import MenuDiff

    check("変更が無ければ出さない ★", jobs.format_menu_news({}) is None)
    d1 = MenuDiff()
    d1.added = [("1", "新商品A")]
    d1.price_changed = [("2", "商品B", 100, 150)]
    d1.removed = [("3", "商品C")]
    d2 = MenuDiff()
    d2.added = [("1", "新商品A")]          # 同じ商品が別店舗にも
    text = jobs.format_menu_news({"13934": d1, "10528": d2})
    check("新商品が出る", "新商品A" in text, text)
    check("同じ商品を2回出さない ★", text.count("新商品A") == 1, text)
    check("値段の変化が出る", "¥100" in text and "¥150" in text, text)
    check("値上げは↑で示す", "↑" in text, text)
    check("販売終了が出る", "商品C" in text, text)
    check("店舗名や店舗IDは出さない ★（全店まとめ）",
          "13934" not in text and "10528" not in text, text)
    d3 = MenuDiff(); d3.added = [("9", "1店だけの商品")]
    check("1店だけの変更を除ける ★",
          jobs.format_menu_news({"1": d3}, min_stores=2) is None)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
