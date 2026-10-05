"""
利用を広げる機能（声かけ・再注文・時間帯・紹介ランキング）

⚠️ 声かけは **送りすぎが一番の失敗**。
   「二度言わない」「本人が止められる」「一度に送りすぎない」の
   3つの歯止めを、端まで確かめる。

⚠️ 再注文は「前と同じ」を謳う以上、**終売・時間帯外の商品を
   そのまま持ち越してはいけない**。そこも確かめる。
"""
import asyncio, json, os, sys, tempfile, uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord

from _fake_discord import FakeChannel, FakeClient, FakeGuild, FakeInteraction, FakeUser
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class DMUser(FakeUser):
    def __init__(self, uid, *, dm_ok=True):
        super().__init__(uid)
        self.dm_ok = dm_ok
        self.guild = None
    async def send(self, **kw):
        if not self.dm_ok:
            raise discord.Forbidden(
                type("R", (), {"status": 403, "reason": ""})(), "DMを拒否")
        self.dms.append(kw)


class Bot(FakeClient):
    def __init__(self, users):
        super().__init__()
        self._users = {u.id: u for u in users}
    def get_user(self, uid): return self._users.get(int(uid))
    async def fetch_user(self, uid):
        u = self._users.get(int(uid))
        if u is None:
            raise discord.NotFound(
                type("R", (), {"status": 404, "reason": ""})(), "x")
        return u


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/gr.db")
    from core import ledger as L
    from core import settings
    from core import users as user_repo
    from db.models import Cart, KyashReceipt, Nudge, Order, User, utcnow
    from services import outreach
    await settings.load_all()

    async def put(**kw):
        for k, v in kw.items():
            await settings.set_value(k, v)

    from core.locks import lock_user

    async def add_balance(uid, amount):
        # ⚠️ 残高を動かすには利用者ロックが要る（core/ledger.py が確かめる）
        async with lock_user(uid):
            async with session_scope() as s:
                await user_repo.ensure_user(s, uid)
                await L.adjust(s, uid, amount, memo="テスト")

    async def add_order(uid, *, days_ago=0, state="COMPLETED",
                        items=None, store="13934"):
        async with session_scope() as s:
            await user_repo.ensure_user(s, uid)
            o = Order(
                id=str(uuid.uuid4()), idempotency_key=str(uuid.uuid4()),
                discord_id=uid, state=state, hex_payload="00",
                store_id=store, store_name="テスト店",
                pickup_method="takeOut",
                items_json=json.dumps(items if items is not None
                                      else [{"product_code": "2081",
                                             "quantity": 1, "amount": 310}]),
                list_price=500, subsidy_rate=40, user_amount=300,
                subsidy_amount=200,
                created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
            )
            s.add(o)
            await s.flush()
            return o.id

    # ========================================================
    print("\n[ 声かけ：送らない条件 ]")
    # ========================================================
    await put(nudge_enabled=False)
    check("全体がOFFなら何もしない ★", not outreach.enabled())
    check("種類ごとに見てもOFF ★", not outreach.enabled(outreach.WELCOME))
    await put(nudge_enabled=True, nudge_welcome=True,
              nudge_cart_minutes=8, nudge_charged_hours=6, nudge_idle_days=14)
    check("ONなら動く", outreach.enabled())
    for kind in (outreach.WELCOME, outreach.CART_LEFT,
                 outreach.CHARGED_UNUSED, outreach.IDLE_BALANCE):
        check(f"{outreach.LABELS[kind]} が有効", outreach.enabled(kind))
    await put(nudge_cart_minutes=0)
    check("0にすればその種類だけ止まる ★", not outreach.enabled(outreach.CART_LEFT))
    await put(nudge_cart_minutes=8)

    # ========================================================
    print("\n[ 声かけ：二度言わない ]")
    # ========================================================
    u1 = DMUser(101)
    bot = Bot([u1])
    await user_repo.get_or_create(101)
    from ui import nudge_views
    e = nudge_views.welcome_embed("テスト")

    got1 = await outreach.send(bot, 101, outreach.WELCOME, "once", e)
    check("1回目は送れる ★", got1 and len(u1.dms) == 1, (got1, len(u1.dms)))
    got2 = await outreach.send(bot, 101, outreach.WELCOME, "once", e)
    check("2回目は送らない ★", not got2 and len(u1.dms) == 1, (got2, len(u1.dms)))
    got3 = await outreach.send(bot, 101, outreach.WELCOME, "別の鍵", e)
    check("鍵が違えば送れる ★", got3 and len(u1.dms) == 2)
    got4 = await outreach.send(bot, 101, outreach.CART_LEFT, "once", e)
    check("種類が違えば送れる ★", got4 and len(u1.dms) == 3)

    # 同時に走っても二重に送らない
    u2 = DMUser(102); bot2 = Bot([u2])
    await user_repo.get_or_create(102)
    results = await asyncio.gather(*[
        outreach.send(bot2, 102, outreach.IDLE_BALANCE, "b0", e)
        for _ in range(5)
    ])
    check("同時に走っても1回だけ ★",
          sum(1 for r in results if r) == 1 and len(u2.dms) == 1,
          (results, len(u2.dms)))

    # ========================================================
    print("\n[ 声かけ：本人が止められる ]")
    # ========================================================
    u3 = DMUser(103); bot3 = Bot([u3])
    await user_repo.get_or_create(103)
    check("既定では受け取る", await outreach.wants(103))
    await outreach.set_notify(103, False)
    check("止められる ★", not await outreach.wants(103))
    got = await outreach.send(bot3, 103, outreach.WELCOME, "once", e)
    check("止めた人には送らない ★", not got and not u3.dms, (got, u3.dms))
    await outreach.set_notify(103, True)
    check("戻せる", await outreach.wants(103))
    got = await outreach.send(bot3, 103, outreach.WELCOME, "once", e)
    check("戻せば送れる", got and len(u3.dms) == 1)

    # ========================================================
    print("\n[ 声かけ：DMを閉じている人 ]")
    # ========================================================
    u4 = DMUser(104, dm_ok=False); bot4 = Bot([u4])
    await user_repo.get_or_create(104)
    got = await outreach.send(bot4, 104, outreach.WELCOME, "once", e)
    check("届かなくても落ちない ★", got is False)
    async with session_scope() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(Nudge).where(Nudge.discord_id == 104)
        )).scalar_one_or_none()
    check("届かなかったことを残す ★", row is not None and row.delivered is False,
          row)
    got = await outreach.send(bot4, 104, outreach.WELCOME, "once", e)
    check("閉じている人を何度も試さない ★", got is False)

    # ========================================================
    print("\n[ 声かけ：誰に送るか ]")
    # ========================================================
    print("  — カートの残り —")
    import config as C
    await put(nudge_cart_minutes=8)
    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        await user_repo.ensure_user(s, 201)
        await user_repo.ensure_user(s, 202)
        await user_repo.ensure_user(s, 203)
        await user_repo.ensure_user(s, 204)
        s.add(Cart(discord_id=201, purpose="order", store_id="1",
                   items_json='[{"product_code":"2081"}]',
                   updated_at=now - timedelta(minutes=10)))
        s.add(Cart(discord_id=202, purpose="order", store_id="1",
                   items_json='[{"product_code":"2081"}]',
                   updated_at=now - timedelta(minutes=2)))
        s.add(Cart(discord_id=203, purpose="order", store_id="1",
                   items_json="[]",
                   updated_at=now - timedelta(minutes=10)))
        s.add(Cart(discord_id=204, purpose="order", store_id="1",
                   items_json='[{"product_code":"2081"}]',
                   updated_at=now - timedelta(minutes=99)))
    ids = [u for u, _, _ in await outreach.cart_left_targets(50)]
    check("8分たった人は対象 ★", 201 in ids, ids)
    check("まだ2分の人は対象外 ★", 202 not in ids, ids)
    check("空のカートは対象外 ★", 203 not in ids, ids)
    check("消えたあとのカートは対象外 ★", 204 not in ids, ids)
    check(f"声かけ({C.NUDGE_CART_MINUTES}分)はカートの有効時間"
          f"({C.CART_RESUME_MINUTES}分)より短い ★",
          C.NUDGE_CART_MINUTES < C.CART_RESUME_MINUTES)

    print("  — チャージ後の未注文 —")
    await put(nudge_charged_hours=6)
    async def add_receipt(uid, hours_ago, amount=1000):
        async with session_scope() as s:
            await user_repo.ensure_user(s, uid)
            s.add(KyashReceipt(
                id=str(uuid.uuid4()), link_uuid=str(uuid.uuid4()),
                discord_id=uid, amount=amount, status="CREDITED",
                created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            ))
        await add_balance(uid, amount)
    await add_receipt(301, 10)       # 10時間前・未注文 → 対象
    await add_receipt(302, 1)        # 1時間前 → まだ早い
    await add_receipt(303, 10)       # 10時間前だが注文済み → 対象外
    await add_order(303)
    ids = [u for u, _, _ in await outreach.charged_unused_targets(50)]
    check("6時間たった未注文の人は対象 ★", 301 in ids, ids)
    check("まだ1時間の人は対象外 ★", 302 not in ids, ids)
    check("注文したことがある人は対象外 ★", 303 not in ids, ids)

    print("  — 残高のお知らせ —")
    await put(nudge_idle_days=14, nudge_idle_min_balance=300)
    await add_order(401, days_ago=30); await add_balance(401, 1000)
    await add_order(402, days_ago=2);  await add_balance(402, 1000)
    await add_order(403, days_ago=30); await add_balance(403, 100)
    rows = await outreach.idle_balance_targets(50)
    ids = [u for u, _, _, _ in rows]
    check("30日ぶりで残高がある人は対象 ★", 401 in ids, ids)
    check("2日前に注文した人は対象外 ★", 402 not in ids, ids)
    check("残高が少ない人は対象外 ★", 403 not in ids, ids)
    check("一度も注文していない人は対象外 ★", 301 not in ids, ids)
    got = [r for r in rows if r[0] == 401][0]
    check("残高と日数を一緒に返す ★", got[2] == 1000 and got[3] >= 29, got)
    check("鍵に期間が入っている（毎日言わない）★", got[1].startswith("b"), got[1])

    # ========================================================
    print("\n[ 声かけ：まとめて実行 ]")
    # ========================================================
    users = [DMUser(i) for i in (201, 301, 401)]
    botx = Bot(users)
    done = await outreach.run(botx)
    check("3種類とも送れる ★",
          done[outreach.CART_LEFT] >= 1 and done[outreach.CHARGED_UNUSED] >= 1
          and done[outreach.IDLE_BALANCE] >= 1, done)
    sent_before = sum(done.values())
    done2 = await outreach.run(botx)
    check("2回まわしても送り直さない ★", sum(done2.values()) == 0, done2)
    st = await outreach.stats()
    check("実績を数えられる ★", st and sum(v["sent"] for v in st.values()) > 0, st)
    await put(nudge_enabled=False)
    check("OFFなら何も送らない ★", sum((await outreach.run(botx)).values()) == 0)
    await put(nudge_enabled=True)

    # ========================================================
    print("\n[ 再注文 ]")
    # ========================================================
    from ui import flows
    import ui.menu_flows as mf

    oid = await add_order(501, items=[
        {"product_code": "2081", "quantity": 2, "amount": 310},
        {"product_code": "1610", "quantity": 1, "amount": 290},
    ])
    opened = {}
    async def fake_open(interaction, store_id, purpose, resume=None, *, reorder=False):
        opened.update(store_id=store_id, purpose=purpose,
                      resume=resume, reorder=reorder)
    real_open = mf.open_menu
    mf.open_menu = fake_open

    g = FakeGuild(); ch = FakeChannel(1)
    it = FakeInteraction(FakeUser(501), FakeClient(), channel=ch, guild=g)
    await flows.reorder(it, oid)
    check("過去の注文を開ける ★", opened.get("store_id") == "13934", opened)
    check("再注文として開く ★", opened.get("reorder") is True)
    check("商品を引き継ぐ ★",
          opened.get("resume") is not None and len(opened["resume"].items) == 2,
          opened.get("resume"))
    check("数量も引き継ぐ ★",
          opened["resume"].items[0].quantity == 2,
          opened["resume"].items[0])
    check("受取方法も引き継ぐ ★", opened["resume"].pickup == "takeOut")

    # 他人の注文は開けない
    opened.clear()
    it2 = FakeInteraction(FakeUser(999), FakeClient(), channel=ch, guild=g)
    await flows.reorder(it2, oid)
    check("他人の注文は開けない ★", not opened, opened)
    said = str(it2.actions[-1][1].get("embed").description)
    check("見つからないと伝える", "見つかりません" in said, said[:50])

    # 店舗の記録が無い注文
    opened.clear()
    bad = await add_order(502, store=None)
    it3 = FakeInteraction(FakeUser(502), FakeClient(), channel=ch, guild=g)
    await flows.reorder(it3, bad)
    check("店舗が無い注文は断る ★", not opened)

    # 中身が壊れている注文
    opened.clear()
    broken = await add_order(503)
    async with session_scope() as s:
        row = await s.get(Order, broken)
        row.items_json = "こわれています"
    it4 = FakeInteraction(FakeUser(503), FakeClient(), channel=ch, guild=g)
    await flows.reorder(it4, broken)
    check("読めない注文でも落ちない ★", not opened)
    mf.open_menu = real_open

    print("  — 履歴に出す注文の選び方 —")
    await add_order(601, state="COMPLETED")
    await add_order(601, state="REFUNDED")
    await add_order(601, state="MANUAL_REVIEW")
    from core import saga
    async with session_scope() as s:
        from sqlalchemy import select
        rows = list((await s.execute(
            select(Order).where(Order.discord_id == 601)
        )).scalars().all())
    repeatable = [
        o for o in rows
        if o.state in (saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED)
        and (o.items_json or "").strip("[] \n")
    ]
    check("成立した注文だけを出す ★", len(repeatable) == 1, [o.state for o in rows])

    # ========================================================
    print("\n[ 時間帯の案内 ]")
    # ========================================================
    from services.mcd import availability as av
    check("朝マックの呼び名", av.daypart_label("DAYPART_BREAKFAST") == "朝マック")
    check("夜マックの呼び名", av.daypart_label("DAYPART_YORU_MAC") == "夜マック")
    check("知らない値はそのまま", av.daypart_label("DAYPART_ZZZ") == "DAYPART_ZZZ")
    check("朝マックは伝える ★",
          av.notable_labels({"DAYPART_BREAKFAST", "DAYPART_REGULAR"}) == ["朝マック"])
    check("レギュラーだけなら何も言わない ★",
          av.notable_labels({"DAYPART_REGULAR"}) == [])
    check("空でも落ちない", av.notable_labels(set()) == []
          and av.notable_labels(None) == [])
    check("夜マックも伝える",
          av.notable_labels({"DAYPART_YORU_MAC"}) == ["夜マック"])
    for name, part in av.COLLECTION_DAYPART.items():
        check(f"カテゴリ「{name}」に呼び名がある", part in av.DAYPART_LABEL, part)

    # ========================================================
    print("\n[ 紹介ランキング ]")
    # ========================================================
    from core import invite as inv
    from db.models import Invite
    from ui import embeds

    await put(invite_enabled=True, ranking_top=3, ranking_show_names=True)
    now = datetime.now(timezone.utc)
    last_month = C.jst_month_start_utc() - timedelta(days=3)

    async def add_invite(invitee, inviter, when, *, qualified=True):
        async with session_scope() as s:
            await user_repo.ensure_user(s, invitee)
            await user_repo.ensure_user(s, inviter)
            s.add(Invite(
                discord_id=invitee, inviter_id=inviter, source="code",
                claimed=True, qualified=qualified,
                qualified_at=when if qualified else None,
            ))

    for i in range(3):
        await add_invite(1000 + i, 900, now)          # 900 さん 3件
    for i in range(2):
        await add_invite(1100 + i, 901, now)          # 901 さん 2件
    await add_invite(1200, 902, last_month)           # 先月 → 今月に入れない
    await add_invite(1300, 903, now, qualified=False)  # 未成立 → 数えない

    rank = await inv.monthly_ranking(3)
    check("多い順に並ぶ ★", [u for u, _ in rank] == [900, 901], rank)
    check("件数が正しい ★", dict(rank) == {900: 3, 901: 2}, rank)
    check("先月の分は入らない ★", 902 not in dict(rank), rank)
    check("未成立は数えない ★", 903 not in dict(rank), rank)
    check("今月の合計", await inv.monthly_total() == 5, await inv.monthly_total())

    e = await embeds.ranking_panel()
    check("パネルを作れる ★", "ランキング" in e.title, e.title)
    body = str([f.value for f in e.fields])
    check("名前を出す設定なら mention ★", "<@900>" in body, body[:80])
    await put(ranking_show_names=False)
    e = await embeds.ranking_panel()
    body = str([f.value for f in e.fields])
    check("匿名設定なら mention を出さない ★", "<@900>" not in body, body[:80])
    await put(ranking_show_names=True)

    await put(invite_enabled=False)
    e = await embeds.ranking_panel()
    check("開催していなければそう出す ★", "開催" in (e.description or ""), e.description)
    await put(invite_enabled=True)

    # 0件のとき
    async with session_scope() as s:
        from sqlalchemy import delete
        await s.execute(delete(Invite))
    e = await embeds.ranking_panel()
    check("0件でも落ちない ★", e.title is not None)
    check("0件なら誘う文面にする ★",
          any("1人目" in f.name for f in e.fields), [f.name for f in e.fields])

    # ========================================================
    print("\n[ コマンドとパネル ]")
    # ========================================================
    import cogs.growth as cg
    names = sorted(c.name for c in cg.GrowthCog.group.commands)
    check("/growth のコマンドがそろっている",
          names == ["nudge", "preview", "ranking", "run", "status"], names)
    from ui import panels
    check("声かけのボタンが再起動後も効く ★",
          "NudgeView" in [c.__name__ for c in panels.PERSISTENT_VIEWS])
    check("ランキングのパネルを貼れる ★", "ranking" in panels.ASYNC_PANEL_BUILDERS)

    # 設置できるパネルは、全部 /panel refresh で更新できること
    import re, pathlib
    src = pathlib.Path(__file__).parent.parent.joinpath("cogs/panel.py").read_text()
    deployed = set(re.findall(r'_deploy\(\s*\n?\s*interaction, "(\w+)"', src))
    block = re.search(r"builders = \{(.*?)\n    \}", src, re.S).group(1)
    builders = set(re.findall(r'"(\w+)":', block)) | set(panels.ASYNC_PANEL_BUILDERS)
    check("設置できるパネルは全部更新できる ★", deployed <= builders,
          deployed - builders)

    for key in ("nudge_enabled", "nudge_cart_minutes", "ranking_top"):
        check(f"{key} が既定にある", key in settings.DEFAULTS)

    # ========================================================
    print("\n[ /growth のコマンドを実際に呼ぶ ]")
    # ========================================================
    import traceback

    from discord import app_commands
    import test_server_cmds as TS

    cl = Bot([])
    gg = TS.Guild()
    cl.get_channel = lambda c: gg.get_channel(c)
    cl._users = {}
    cl.get_user = lambda u: cl._users.get(int(u))
    boss = TS.Member(1, admin=True); boss.guild = gg
    room = TS.Ch(7, "general"); gg.add_channel(room)
    gcog = cg.GrowthCog(cl)

    def GI():
        it = FakeInteraction(boss, cl, channel=room, guild=gg)
        it.channel_id = room.id; it.guild_id = gg.id
        return it

    async def run_cmd(name, cmd, *a, **kw):
        it = GI()
        try:
            await cmd.callback(gcog, it, *a, **kw)
        except Exception:
            check(name, False, traceback.format_exc().strip().splitlines()[-1])
            return
        check(name, bool(it.actions), "応答していません")

    await run_cmd("nudge 全部指定", cg.GrowthCog.nudge, True, True, 8, 6, 14, 300)
    await run_cmd("nudge 引数なし", cg.GrowthCog.nudge)
    await run_cmd("status", cg.GrowthCog.status)
    for v, n in (("welcome", "ようこそ"), ("cart_left", "カート"),
                 ("charged_unused", "チャージ"), ("idle_balance", "残高")):
        await run_cmd(f"preview（{n}）", cg.GrowthCog.preview,
                      app_commands.Choice(name=n, value=v))
    await run_cmd("ranking", cg.GrowthCog.ranking, 3, True)
    await put(nudge_enabled=False)
    await run_cmd("run（OFFのとき）", cg.GrowthCog.run_now)
    await put(nudge_enabled=True)
    await run_cmd("run（ONのとき）", cg.GrowthCog.run_now)

    # 危ない設定に気づけるか
    it = GI()
    await put(nudge_cart_minutes=C.CART_RESUME_MINUTES + 5)
    await cg.GrowthCog.nudge.callback(gcog, it)
    said = str(it.actions[0][1].get("embed").description)
    check("カートが消えてから声をかける設定に警告が出る ★",
          "消えてから" in said, said[-120:])
    await put(nudge_cart_minutes=8)

    print("\n[ 入室時のようこそ ]")
    await put(nudge_enabled=True, nudge_welcome=True, verify_enabled=True)
    n1 = TS.Member(700); n1.guild = gg; cl._users[700] = n1
    await gcog.on_member_join(n1)
    check("認証を使っているときは入室で送らない ★", not n1.dms,
          "認証後に送るので、ここで送ると二重になる")

    await put(verify_enabled=False)
    n2 = TS.Member(701); n2.guild = gg; cl._users[701] = n2
    await gcog.on_member_join(n2)
    check("認証を使っていなければ入室で送る ★", bool(n2.dms))
    await gcog.on_member_join(n2)
    check("入り直しても二度は送らない ★", len(n2.dms) == 1, len(n2.dms))

    n3 = TS.Member(702, bot=True); n3.guild = gg; cl._users[702] = n3
    await gcog.on_member_join(n3)
    check("BOTには送らない ★", not n3.dms)

    await put(nudge_welcome=False)
    n4 = TS.Member(703); n4.guild = gg; cl._users[703] = n4
    await gcog.on_member_join(n4)
    check("ようこそをOFFにすれば送らない ★", not n4.dms)
    await put(nudge_welcome=True)

    print("\n[ 声かけのDMが上限を超えないこと ]")
    for name, e2 in (
        ("ようこそ", nudge_views.welcome_embed("あ" * 300)),
        ("カート", nudge_views.cart_left_embed("1")),
        ("チャージ", nudge_views.charged_unused_embed(99999999)),
        ("残高", nudge_views.idle_balance_embed(99999999, 9999)),
    ):
        d = e2.to_dict()
        total = (len(d.get("title", "")) + len(d.get("description", ""))
                 + sum(len(f["name"]) + len(f["value"])
                       for f in d.get("fields", [])))
        check(f"{name}のDMが6000文字以内 ★", total <= 6000, total)
        check(f"{name}のDMに止めるボタンがある ★",
              any(getattr(i, "custom_id", "") == "nudge:stop"
                  for i in nudge_views.NudgeView().children))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
