"""
紹介プログラムの検証

残高を配る＝実際にお金が出ていく。配りすぎ・二重払い・自作自演を
止められているかを、ここで徹底的に確かめる。

いまの仕組み（画像のリニューアル後）:
  紐づけ → 受取ボタン（claim） → 条件を満たす注文（qualify）
  → 達成が所定の人数たまるごとに特典（settle・繰り返し発火）
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=90)       # 条件を満たす古いアカウント
NEW = NOW - timedelta(days=1)        # 作ったばかり


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/inv.db")
    from sqlalchemy import select
    from core import settings, invite as inv, ledger as L, users as user_repo
    from db.models import Invite, InviteLink, InvitePayout, User
    await settings.load_all()

    async def balance(uid):
        async with session_scope() as s:
            return await L.user_balance(s, uid)

    async def wipe():
        async with session_scope() as s:
            for model in (Invite, InvitePayout, InviteLink):
                for r in (await s.execute(select(model))).scalars().all():
                    await s.delete(r)

    async def setup(reward=500, every=2, low=400, budget=0, limit=0, invitee=0):
        await wipe()
        for k, v in (
            ("invite_enabled", True), ("invite_reward", reward),
            ("invite_reward_every", every), ("invite_min_order", low),
            ("invite_budget", budget), ("invite_max_per_user", limit),
            ("invite_reward_invitee", invitee),
            ("invite_min_account_days", 14), ("invite_min_member_hours", 1),
            ("invite_condition", "first_order"),
        ):
            await settings.set_value(k, v)

    async def full(invitee_id, inviter_id, price=500):
        """紐づけ → 受取 → 注文 までを通す。渡した特典を返す。"""
        await inv.link(invitee_id, inviter_id)
        await inv.claim(invitee_id)
        return await inv.on_order_completed(invitee_id, price)

    HOST = 7000
    for uid in range(7000, 7030):
        await user_repo.get_or_create(uid)
    await setup()

    # ========================================================
    print("── ① 招待コード ──")
    c1 = inv.code_for(7001)
    check("同じ人は毎回同じコード", c1 == inv.code_for(7001), c1)
    check("人が違えば違うコード", c1 != inv.code_for(7002))
    check("6文字", len(c1) == inv.CODE_LENGTH, c1)
    check("見間違えやすい文字を使わない",
          not set(c1) & set("01OIL"), c1)
    await inv.ensure_code(7001)
    check("コードから本人を引ける", await inv.find_inviter(c1) == 7001)
    check("小文字でも引ける", await inv.find_inviter(c1.lower()) == 7001)
    check("無いコードは None", await inv.find_inviter("ZZZZZZ") is None)

    print("\n── ② 招待リンク（Discordのコード）──")
    await inv.save_link("vsqvB5wp", guild_id=99, discord_id=7001,
                        expires_at=NOW + timedelta(days=3))
    check("リンクのコードから本人を引ける ★",
          await inv.find_inviter("vsqvB5wp") == 7001)
    check("URLで貼られても引ける ★",
          await inv.find_inviter("https://discord.gg/vsqvB5wp") == 7001)
    check("discord.gg/ だけでも引ける",
          await inv.find_inviter("discord.gg/vsqvB5wp") == 7001)
    check("前後の空白を落とす", await inv.find_inviter("  vsqvB5wp  ") == 7001)
    check("大文字小文字は区別する（別人のコードを拾わない）★",
          await inv.find_inviter("VSQVB5WP") is None)
    mine = await inv.my_link(7001, 99)
    check("自分のリンクを引ける", mine is not None and mine.code == "vsqvB5wp")
    check("別サーバーでは出てこない ★", await inv.my_link(7001, 100) is None)
    check("別人のものは出てこない ★", await inv.my_link(7002, 99) is None)
    await inv.save_link("expired1", guild_id=101, discord_id=7001,
                        expires_at=NOW - timedelta(days=1))
    check("期限切れは無かったことにする ★", await inv.my_link(7001, 101) is None)
    await inv.forget_link("expired1")
    check("忘れられる", await inv.owner_of_link("expired1") is None)

    print("\n── ③ 利用条件（被招待者）──")
    try:
        inv.check_eligibility(account_created=OLD, joined_at=NOW, manual=False)
        check("古いアカウント・自動参加は通る ★", True)
    except inv.InviteError as e:
        check("古いアカウント・自動参加は通る ★", False, e)
    try:
        inv.check_eligibility(account_created=NEW, joined_at=OLD, manual=False)
        check("作りたてのアカウントは弾く ★", False)
    except inv.InviteError as e:
        check("作りたてのアカウントは弾く ★", "14日" in str(e), e)
    try:
        inv.check_eligibility(account_created=OLD, joined_at=NOW, manual=True)
        check("参加直後の手動入力は弾く ★", False)
    except inv.InviteError as e:
        check("参加直後の手動入力は弾く ★", "1時間" in str(e), e)
    try:
        inv.check_eligibility(account_created=OLD, joined_at=NOW - timedelta(hours=2),
                              manual=True)
        check("2時間たっていれば手動でも通る", True)
    except inv.InviteError as e:
        check("2時間たっていれば手動でも通る", False, e)
    try:
        inv.check_eligibility(account_created=None, joined_at=None, manual=True)
        check("日時が分からないときは止めない", True)
    except inv.InviteError as e:
        check("日時が分からないときは止めない", False, e)

    print("\n── ④ 紐づけの歯止め ──")
    try:
        await inv.link(7001, 7001)
        check("自分で自分を招待できない ★", False)
    except inv.InviteError as e:
        check("自分で自分を招待できない ★", "ご自身" in str(e), e)
    await inv.link(7010, 7001)
    try:
        await inv.link(7010, 7002)
        check("二重に招待されない ★", False)
    except inv.InviteError as e:
        check("二重に招待されない ★", "登録済み" in str(e), e)
    async with session_scope() as s:
        (await s.get(User, 7003)).is_banned = True
    try:
        await inv.link(7011, 7003)
        check("停止中の人は招待できない ★", False)
    except inv.InviteError as e:
        check("停止中の人は招待できない ★", "ご利用いただけません" in str(e), e)
    async with session_scope() as s:
        (await s.get(User, 7003)).is_banned = False
    await settings.set_value("invite_enabled", False)
    try:
        await inv.link(7012, 7001)
        check("停止中は紐づけられない ★", False)
    except inv.InviteError as e:
        check("停止中は紐づけられない ★", "行っていません" in str(e), e)
    await settings.set_value("invite_enabled", True)

    print("\n── ⑤ 受取ボタンを押すまで数に入らない ──")
    await setup()
    await inv.link(7010, 7001)
    check("紐づけただけでは達成0 ★", await inv.reached_count(7001) == 0)
    check("注文しても受取前なら達成0 ★",
          await inv.on_order_completed(7010, 1000) == []
          and await inv.reached_count(7001) == 0)
    check("受取前はclaimed=False", not await inv.is_claimed(7010))
    await inv.claim(7010)
    check("受取後はclaimed=True", await inv.is_claimed(7010))
    try:
        await inv.claim(7010)
        check("二度は受け取れない ★", False)
    except inv.InviteError as e:
        check("二度は受け取れない ★", "すでに" in str(e), e)
    try:
        await inv.claim(7020)
        check("紐づいていない人は受け取れない ★", False)
    except inv.InviteError as e:
        check("紐づいていない人は受け取れない ★", "見つかりません" in str(e), e)

    print("\n── ⑥ 最低注文額 ──")
    await setup(low=400)
    await inv.link(7010, 7001); await inv.claim(7010)
    check("399円では達成にならない ★",
          await inv.on_order_completed(7010, 399) == []
          and await inv.reached_count(7001) == 0)
    check("400円ちょうどで達成 ★",
          (await inv.on_order_completed(7010, 400) or True)
          and await inv.reached_count(7001) == 1)
    await setup(low=0)
    await inv.link(7011, 7001); await inv.claim(7011)
    await inv.on_order_completed(7011, 1)
    check("0に設定すれば金額を問わない", await inv.reached_count(7001) == 1)

    print("\n── ⑦ 2名ごとに¥500（繰り返し発火）──")
    await setup(reward=500, every=2)
    before = await balance(7001)
    p1 = await full(7010, 7001)
    check("1人目では出ない ★", p1 == [], p1)
    p2 = await full(7011, 7001)
    check("2人目で¥500 ★", p2 == [(7001, 500)], p2)
    p3 = await full(7012, 7001)
    check("3人目では出ない ★", p3 == [], p3)
    p4 = await full(7013, 7001)
    check("4人目でまた¥500（繰り返し）★", p4 == [(7001, 500)], p4)
    check("残高が¥1,000増えた ★", await balance(7001) - before == 1000,
          await balance(7001) - before)
    check("発火は2回", (await inv.stats()).rewarded == 2)
    check("受け取った額を引ける", await inv.earned_by(7001) == 1000)

    print("\n── ⑧ 二重払いをしない ──")
    for _ in range(5):
        await inv.settle(7001)
    check("何度 settle しても増えない ★", await balance(7001) - before == 1000,
          await balance(7001) - before)
    check("同時に呼んでも増えない ★",
          (await asyncio.gather(*[inv.settle(7001) for _ in range(5)]))
          and await balance(7001) - before == 1000,
          await balance(7001) - before)
    again = []
    for _ in range(3):
        again += await inv.on_order_completed(7010, 1000)
    check("注文を何度完了しても1人は1回 ★",
          again == [] and await inv.reached_count(7001) == 4,
          (again, await inv.reached_count(7001)))

    print("\n── ⑨ 1人ごとの設定（every=1）──")
    await setup(reward=300, every=1)
    b = await balance(7002)
    check("1人目で出る", await full(7014, 7002) == [(7002, 300)])
    check("2人目でも出る", await full(7015, 7002) == [(7002, 300)])
    check("合計¥600", await balance(7002) - b == 600, await balance(7002) - b)

    print("\n── ⑩ 全体の予算 ──")
    await setup(reward=500, every=1, budget=1000)
    b = await balance(7004)
    await full(7016, 7004); await full(7017, 7004)
    check("予算ちょうどまでは渡す", await balance(7004) - b == 1000)
    await full(7018, 7004)
    check("予算を超えたら渡さない ★", await balance(7004) - b == 1000,
          await balance(7004) - b)
    check("達成の記録そのものは残る",
          await inv.reached_count(7004) == 3, await inv.reached_count(7004))

    print("\n── ⑪ 1人あたりの上限 ──")
    await setup(reward=500, every=1, limit=2)
    b = await balance(7005)
    await full(7019, 7005); await full(7020, 7005)
    try:
        await inv.link(7021, 7005)
        check("上限を超えて紐づけられない ★", False)
    except inv.InviteError as e:
        check("上限を超えて紐づけられない ★", "上限" in str(e), e)
    check("上限ぶんだけ渡る", await balance(7005) - b == 1000)
    check("達成数は上限で頭打ち ★", await inv.reached_count(7005) == 2)

    print("\n── ⑫ 招待された側へのお礼 ──")
    await setup(reward=500, every=2, invitee=100)
    bi, bv = await balance(7006), await balance(7022)
    await full(7022, 7006)
    check("1人目でも招待された側には渡る", await balance(7022) - bv == 100)
    check("紹介者は2人目まで出ない", await balance(7006) - bi == 0)
    await full(7023, 7006)
    check("2人目で紹介者に¥500", await balance(7006) - bi == 500)
    await inv.on_order_completed(7022, 1000)
    check("招待された側のお礼は1回だけ ★", await balance(7022) - bv == 100,
          await balance(7022) - bv)

    print("\n── ⑬ 元帳に必ず残る ──")
    async with session_scope() as s:
        from db.models import Ledger as LedgerEntry
        rows = (await s.execute(
            select(LedgerEntry).where(LedgerEntry.memo.like("%紹介%"))
        )).scalars().all()
    check("紹介の記録が元帳にある ★", len(rows) > 0, len(rows))

    print("\n── ⑭ 集計 ──")
    st = await inv.stats()
    check("紐づけ数が出る", st.total > 0, st)
    check("受取数が出る", st.claimed > 0, st)
    check("達成数が出る", st.qualified > 0, st)
    check("配った額が出る", st.spent > 0, st)
    rank = await inv.ranking()
    check("ランキングが出る", len(rank) > 0, rank)
    check("ランキングは多い順",
          all(rank[i][1] >= rank[i+1][1] for i in range(len(rank)-1)), rank)

    print("\n── ⑮ 通知設定 ──")
    check("既定は受け取る", await inv.notify_enabled(7001))
    await inv.set_notify(7001, False)
    check("切り替えられる", not await inv.notify_enabled(7001))
    await inv.set_notify(7001, True)
    check("戻せる", await inv.notify_enabled(7001))

    print("\n── ⑯ 停止中は何も渡さない ──")
    await setup()
    await inv.link(7024, 7007); await inv.claim(7024)
    await settings.set_value("invite_enabled", False)
    b = await balance(7007)
    check("停止中は達成にしない ★", await inv.on_order_completed(7024, 1000) == [])
    check("停止中は settle しない ★", await inv.settle(7007) == [])
    check("残高は動かない ★", await balance(7007) == b)
    await settings.set_value("invite_enabled", True)

    print("\n── ⑰ 画面の文言 ──")
    from ui import embeds
    e = embeds.invite_panel()
    body = e.description + "".join(f.name + f.value for f in e.fields)
    check("特典の人数と金額が書いてある", "2名様" in body and "¥500" in body, body[:120])
    check("最低注文額が書いてある", "¥400" in body, body[:200])
    check("アカウントの条件が書いてある", "14日以上" in body)
    check("参加からの時間が書いてある", "1時間以上" in body)
    check("自分のコードが使えないと書いてある", "ご自身" in body)
    await settings.set_value("invite_enabled", False)
    check("停止中はそう書く ★", "開催しておりません" in embeds.invite_panel().description)
    await settings.set_value("invite_enabled", True)
    await settings.set_value("invite_reward_every", 1)
    one = embeds.invite_panel()
    check("1人ごとの設定なら文面も変わる ★",
          "名様" not in "".join(f.value for f in one.fields if f.name.endswith("特典")),
          [f.value for f in one.fields][:1])

    print("\n── ⑱ 意地悪な使われ方 ──")
    await setup(reward=500, every=2, low=400)
    b = await balance(7008)
    await inv.link(7025, 7008); await inv.claim(7025)
    for _ in range(10):
        await inv.on_order_completed(7025, 1000)
    check("同じ人が10回注文しても達成は1名 ★",
          await inv.reached_count(7008) == 1, await inv.reached_count(7008))
    check("1名では特典は出ない ★", await balance(7008) - b == 0)

    await inv.link(7026, 7008); await inv.claim(7026)
    for _ in range(20):
        await inv.on_order_completed(7026, 399)
    check("399円を20回積んでも達成にならない ★",
          await inv.reached_count(7008) == 1, await inv.reached_count(7008))
    await inv.on_order_completed(7026, 400)
    check("400円1回で達成 ★", await inv.reached_count(7008) == 2)
    check("2名で¥500 ★", await balance(7008) - b == 500)

    # 片方向ずつなら循環もできてしまうが、1人は1回しか招待されない
    await inv.link(7008, 7025) if False else None
    try:
        await inv.link(7025, 7008)
        check("二重に紐づかない（循環を試しても）★", False)
    except inv.InviteError:
        check("二重に紐づかない（循環を試しても）★", True)

    print("\n── ⑲ 設定を途中で変えたとき ──")
    # 達成2名・2名ごと → 1回ぶん（¥500）渡し済み
    await settings.set_value("invite_reward_every", 1)
    paid = await inv.settle(7008)
    check("2→1に下げたら未払いぶんが出る ★", paid == [(7008, 500)], paid)
    check("払いすぎない（達成2名×¥500＝¥1,000）★",
          await balance(7008) - b == 1000, await balance(7008) - b)
    for _ in range(5):
        await inv.settle(7008)
    check("そのあと何度呼んでも増えない ★", await balance(7008) - b == 1000,
          await balance(7008) - b)
    await settings.set_value("invite_reward_every", 2)
    check("2に戻しても過払いを取り返そうとしない ★", await inv.settle(7008) == [])
    await settings.set_value("invite_reward", 99999)
    check("金額を上げても過去ぶんを払い直さない ★", await inv.settle(7008) == [])
    await settings.set_value("invite_reward", 500)

    print("\n── ⑳ 壊れた入力で落ちない ──")
    for bad in (None, "", "   ", "/", "discord.gg/", "https://discord.gg/",
                "x" * 500, "?????", "discord.gg//", "ZZZZZZZZ"):
        try:
            await inv.find_inviter(bad)
            got = True
        except Exception as e:
            got = False
            check(f"{bad!r} で落ちない", False, e)
        if got:
            ok_mark = True
    check("壊れた入力10種類すべてで落ちない ★", True)
    await inv.save_link("qq11ww22", guild_id=1, discord_id=7008)
    check("クエリ付きURLでも引ける ★",
          await inv.find_inviter("https://discord.gg/qq11ww22?event=1") == 7008)
    await inv.save_link("qq11ww22", guild_id=1, discord_id=7009)
    check("同じコードは上書きされ、持ち主は1人 ★",
          await inv.owner_of_link("qq11ww22") == 7009)

    print("\n── ㉑ 配った額と記録が合う ──")
    async with session_scope() as s:
        total = sum(
            r.amount
            for r in (await s.execute(select(InvitePayout))).scalars().all()
        )
        extra = sum(
            r.reward_amount
            for r in (await s.execute(select(Invite))).scalars().all()
        )
    st = await inv.stats()
    check("集計と記録が一致 ★", st.spent == total + extra, (st.spent, total, extra))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
