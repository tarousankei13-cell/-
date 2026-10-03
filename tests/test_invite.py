"""
招待キャンペーンの検証

残高を配る＝実際にお金が出ていく。配りすぎ・二重払い・自作自演を
止められているかを、ここで徹底的に確かめる。
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from _fake_discord import FakeInteraction, FakeUser, FakeClient

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/inv.db")
    from core import settings, invite as inv, ledger as L, users as user_repo
    from db.models import Invite, User
    await settings.load_all()

    async def balance(uid):
        async with session_scope() as s:
            return await L.user_balance(s, uid)

    async def mark_ordered(uid, n=1):
        async with session_scope() as s:
            row = await s.get(User, uid)
            row.total_orders = n

    async def reset():
        async with session_scope() as s:
            for r in (await s.execute(__import__("sqlalchemy").select(Invite))).scalars().all():
                await s.delete(r)

    A, B, C, D = 7001, 7002, 7003, 7004
    for uid in (A, B, C, D):
        await user_repo.get_or_create(uid)

    await settings.set_value("invite_enabled", True)
    await settings.set_value("invite_reward", 300)
    await settings.set_value("invite_reward_invitee", 0)
    await settings.set_value("invite_max_per_user", 5)
    await settings.set_value("invite_budget", 10000)
    await settings.set_value("invite_condition", "first_order")

    # ========================================================
    print("\n[1] 招待コード ★")
    code_a = await inv.ensure_code(A)
    check("同じ人なら毎回同じ ★", code_a == inv.code_for(A) == await inv.ensure_code(A))
    check("長さが決まっている", len(code_a) == inv.CODE_LENGTH, code_a)
    check("紛らわしい文字を使わない ★",
          not any(c in "01ILO" for c in inv.ALPHABET), inv.ALPHABET)
    check("人が違えば違うコード", inv.code_for(A) != inv.code_for(B))
    check("IDが推測できない形",
          str(A) not in code_a and code_a != str(A), code_a)

    print("\n[2] 入力のゆれを吸収する")
    check("小文字でも通る", inv.normalize(code_a.lower()) == code_a)
    check("空白・ハイフンを無視", inv.normalize(f" {code_a[:3]}-{code_a[3:]} ") == code_a)
    check("コードから本人を引ける ★", await inv.find_inviter(code_a) == A)
    check("出鱈目なコードでは引けない", await inv.find_inviter("ZZZZZZ") is None)
    check("長さが違えば引かない", await inv.find_inviter("ABC") is None)

    print("\n[3] 自作自演を止める ★")
    try:
        await inv.link(A, A)
        check("自分のコードは使えない ★", False, "通ってしまった")
    except inv.InviteError as e:
        check("自分のコードは使えない ★", "ご自身" in str(e), str(e))

    print("\n[4] 条件を満たすまで渡さない ★")
    before = await balance(A)
    await inv.link(B, A)
    paid = await inv.grant_if_ready(B)
    check("注文前は渡さない ★", paid == [] and await balance(A) == before, paid)

    await mark_ordered(B)
    paid = await inv.grant_if_ready(B)
    check("初回注文で渡す ★", paid == [(A, 300)], paid)
    check("残高が増える ★", await balance(A) == before + 300, await balance(A))

    print("\n[5] 二重に渡さない ★")
    again = await inv.grant_if_ready(B)
    check("2回目は何も起きない ★", again == [], again)
    check("残高も増えない ★", await balance(A) == before + 300, await balance(A))
    for _ in range(5):
        await inv.grant_if_ready(B)
    check("何度呼んでも増えない ★", await balance(A) == before + 300, await balance(A))

    print("\n[6] 1人が二度は招待されない ★")
    try:
        await inv.link(B, C)
        check("すでに登録済みなら断る ★", False, "通ってしまった")
    except inv.InviteError as e:
        check("すでに登録済みなら断る ★", "登録済み" in str(e), str(e))

    print("\n[7] 招待した人ごとの上限 ★")
    await settings.set_value("invite_max_per_user", 1)
    try:
        await inv.link(C, A)
        check("上限を超えたら断る ★", False, "通ってしまった")
    except inv.InviteError as e:
        check("上限を超えたら断る ★", "上限" in str(e), str(e))
    await settings.set_value("invite_max_per_user", 5)

    print("\n[8] 全体の予算を超えない ★")
    await reset()
    await settings.set_value("invite_budget", 500)
    await settings.set_value("invite_reward", 300)
    bal_c = await balance(C)
    await inv.link(C, A); await mark_ordered(C)
    p1 = await inv.grant_if_ready(C)
    check("1件目は渡せる", p1 == [(A, 300)], p1)
    await inv.link(D, A); await mark_ordered(D)
    p2 = await inv.grant_if_ready(D)
    check("予算を超える2件目は渡さない ★", p2 == [], p2)
    st = await inv.stats()
    check("配った額が予算内 ★", st.spent <= 500, st.spent)
    check("のこりが分かる", st.budget_left == 200, st.budget_left)

    print("\n[9] 停止中は何もしない ★")
    await settings.set_value("invite_enabled", False)
    await reset()
    try:
        await inv.link(D, B)
        check("停止中は紐づけない ★", False, "通ってしまった")
    except inv.InviteError as e:
        check("停止中は紐づけない ★", "行っていません" in str(e), str(e))
    check("停止中は特典も渡さない ★", await inv.grant_if_ready(D) == [])
    await settings.set_value("invite_enabled", True)

    print("\n[10] 利用停止中の人のコードは使えない ★")
    await reset()
    async with session_scope() as s:
        (await s.get(User, A)).is_banned = True
    try:
        await inv.link(D, A)
        check("停止中の人は招待できない ★", False, "通ってしまった")
    except inv.InviteError as e:
        check("停止中の人は招待できない ★", "使えません" in str(e), str(e))
    async with session_scope() as s:
        (await s.get(User, A)).is_banned = False

    print("\n[11] 存在しない人のコードでは紐づかない")
    try:
        await inv.link(D, 999999)
        check("知らない人には紐づけない", False, "通ってしまった")
    except inv.InviteError as e:
        check("知らない人には紐づけない", "見つかりません" in str(e), str(e))

    print("\n[12] 条件を join にすると即座に渡る ★")
    await reset()
    await settings.set_value("invite_condition", "join")
    await settings.set_value("invite_budget", 10000)
    bal_b = await balance(B)
    await inv.link(D, B)
    paid = await inv.grant_if_ready(D)
    check("注文していなくても渡る ★", paid == [(B, 300)], paid)
    check("残高に入る", await balance(B) == bal_b + 300)
    await settings.set_value("invite_condition", "first_order")

    print("\n[13] 招待された人にも渡せる ★")
    await reset()
    await settings.set_value("invite_reward_invitee", 100)
    bal_a, bal_c = await balance(A), await balance(C)
    await inv.link(C, A); await mark_ordered(C)
    paid = await inv.grant_if_ready(C)
    check("2人ぶん渡る ★", len(paid) == 2, paid)
    check("招待した人へ300", await balance(A) == bal_a + 300, await balance(A))
    check("招待された人へ100", await balance(C) == bal_c + 100, await balance(C))
    await settings.set_value("invite_reward_invitee", 0)

    print("\n[14] 元帳に残る（残高の出どころが追える）★")
    from db.models import Ledger
    from sqlalchemy import select
    async with session_scope() as s:
        rows = (
            await s.execute(select(Ledger).where(Ledger.memo.like("%招待%")))
        ).scalars().all()
    check("招待の記録が元帳にある ★", rows, len(rows))
    check("理由が書いてある", all("招待" in (r.memo or "") for r in rows))

    print("\n[15] 集計とランキング")
    st = await inv.stats()
    check("件数が数えられる", st.total >= 1, st)
    check("成立数が数えられる", st.rewarded >= 1, st)
    rank = await inv.ranking(5)
    check("ランキングが出る", rank, rank)
    check("多い順に並ぶ", all(rank[i][1] >= rank[i+1][1] for i in range(len(rank)-1)), rank)

    print("\n[16] パネルの文言 ★")
    from ui import embeds as em
    e = em.invite_panel()
    text = (e.title or "") + (e.description or "") + "".join(
        f.name + f.value for f in e.fields)
    check("いくらもらえるか書いてある ★", "¥300" in text, text[:200])
    check("条件が書いてある ★", "注文" in text, text[:300])
    check("自分のコードは使えないと書く ★", "ご自身" in text, text[:600])
    await settings.set_value("invite_enabled", False)
    e2 = em.invite_panel()
    check("停止中はそう書く ★", "開催していません" in (e2.description or ""), e2.description)
    await settings.set_value("invite_enabled", True)

    print("\n[17] 利用者の画面")
    from ui import invite_flows as IF
    client = FakeClient()
    itx = FakeInteraction(FakeUser(A), client)
    await IF.show_my_code(itx)
    body = itx.text()
    check("自分のコードが出る", code_a in body, body[:200])
    check("本人にしか見えない ★",
          all(kw.get("ephemeral") for k, kw in itx.actions if k == "followup"),
          itx.actions)

    itx2 = FakeInteraction(FakeUser(B), client)
    await IF.show_status(itx2)
    check("状況が見られる", "招待" in itx2.text(), itx2.text()[:150])
    check("他人の名前が出ない ★",
          str(A) not in itx2.text() and str(C) not in itx2.text(),
          itx2.text()[:300])

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
