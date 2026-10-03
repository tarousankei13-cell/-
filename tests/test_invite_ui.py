"""
紹介プログラムの画面操作

パネルのボタンを実際に押して、画面に何が出るかを確かめる。
利用者が触るのはここだけなので、黙って何も起きない・二重に応答する
といった不具合は、ここで捕まえる。
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from _fake_discord import (
    FakeInteraction, FakeUser, FakeClient, FakeChannel, FakeGuild, press,
)

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/iu.db")
    from core import settings, invite as inv, users as user_repo
    from ui import invite_flows as F
    from ui.panels import InvitePanel
    await settings.load_all()

    client = FakeClient()
    guild = FakeGuild(gid=42)
    link_ch = guild.add_channel(FakeChannel(555, name="ようこそ"))
    HOST, FRIEND, FRIEND2 = 9001, 9002, 9003
    for uid in (HOST, FRIEND, FRIEND2):
        await user_repo.get_or_create(uid)
    host = FakeUser(HOST, "紹介する人")
    friend = FakeUser(FRIEND, "お友達")
    client.users = {HOST: host, FRIEND: friend}

    async def on(**kw):
        for k, v in (
            ("invite_enabled", True), ("invite_reward", 500),
            ("invite_reward_every", 2), ("invite_min_order", 400),
            ("invite_budget", 0), ("invite_max_per_user", 0),
            ("invite_link_channel", 555), ("invite_link_days", 3),
            ("invite_min_account_days", 14), ("invite_min_member_hours", 1),
        ):
            await settings.set_value(k, kw.get(k, v))

    def act(user, **kw):
        return FakeInteraction(user, client, guild=guild, **kw)

    print("── パネルのボタン ──")
    view = InvitePanel()
    labels = [c.label for c in view.children]
    check("ボタンが5つ", len(view.children) == 5, labels)
    for want in ("招待リンクを発行", "プロモコード入力", "DMを再送信",
                 "紹介状況", "通知設定"):
        check(f"「{want}」がある", want in labels, labels)
    check("1段目が2つ", len([c for c in view.children if c.row == 0]) == 2)
    check("2段目が3つ", len([c for c in view.children if c.row == 1]) == 3)

    print("\n── 停止中は全部そう言う ──")
    await settings.set_value("invite_enabled", False)
    for name, fn in (("発行", F.issue_link), ("状況", F.show_status)):
        i = act(host)
        await fn(i)
        check(f"{name}: 開催していないと伝える ★", "開催" in i.text() or "行って" in i.text(),
              i.text()[:60])
    i = act(host)
    await F.open_code_modal(i)
    check("コード入力: モーダルを出さない ★", i.last_modal() is None, i.kinds)

    print("\n── 発行先が未設定なら発行させない ──")
    await on(invite_link_channel=0)
    i = act(host)
    await F.issue_link(i)
    check("理由を伝える ★", "発行先" in i.text(), i.text()[:80])
    check("管理者への案内も出す", "config campaign" in i.text(), i.text()[:160])
    check("招待を作っていない ★", not link_ch.invites_made)

    print("\n── 招待リンクの発行 ──")
    await on()
    i = act(host)
    await F.issue_link(i)
    e = i.last_embed()
    check("発行できた ★", e is not None and "発行しました" in e.title, i.text()[:80])
    body = (e.description or "") + "".join(f.name + f.value for f in e.fields)
    check("サーバー名が出る", guild.name in body)
    check("招待URLが出る ★", "discord.gg/" in body, body[:200])
    check("有効期限が出る", "3 日" in body or "3日" in body, body[:300])
    check("報酬が出る", "¥500" in body)
    check("2名ごとだと書いてある", "2名様" in body or "2名" in body)
    check("利用条件が出る", "14日以上" in body)
    check("1人1リンクと書いてある", "1人1リンク" in (e.footer.text or ""))
    code = link_ch.invites_made[0].code
    check("発行した招待を控えている ★", await inv.owner_of_link(code) == HOST)
    check("本人にだけ見せている ★", all(a[1].get("ephemeral") for a in i.actions
                                         if "ephemeral" in a[1]), i.actions)

    print("\n── 2回押しても1本だけ ──")
    i2 = act(host)
    await F.issue_link(i2)
    check("同じコードを出す ★", code in i2.text(), i2.text()[:200])
    check("招待を作り直していない ★", len(link_ch.invites_made) == 1,
          len(link_ch.invites_made))

    print("\n── 権限が無いとき ──")
    link_ch.can_invite = False
    other = FakeUser(9009, "別の人")
    await user_repo.get_or_create(9009)
    i = act(other)
    await F.issue_link(i)
    check("権限が無いと伝える ★", "権限" in i.text(), i.text()[:80])
    check("管理者への案内を出す", "招待を作成" in i.text(), i.text()[:160])
    link_ch.can_invite = True

    print("\n── コードを入れて紐づく ──")
    i = act(friend)
    await F.finish_link(i, code, source="code")
    check("登録できた ★", "登録しました" in (i.last_embed().title or ""), i.text()[:80])
    check("DMを送った ★", len(friend.dms) == 1, friend.dms)
    dm = friend.dms[0]
    check("DMに受取ボタンが付いている ★",
          dm.get("view") is not None and len(dm["view"].children) == 1)
    check("まだ受け取っていない", not await inv.is_claimed(FRIEND))

    print("\n── 自分のコードは使えない ──")
    i = act(host)
    await F.finish_link(i, code, source="code")
    check("自分のコードを弾く ★", "ご自身" in i.text(), i.text()[:80])

    print("\n── 無いコード ──")
    i = act(FakeUser(9010))
    await user_repo.get_or_create(9010)
    await F.finish_link(i, "ZZZZZZZZ", source="code")
    check("見つからないと伝える ★", "見つかりません" in i.text(), i.text()[:80])

    print("\n── 作りたてのアカウントを弾く ──")
    newbie = FakeUser(9011, "新規")
    newbie.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    await user_repo.get_or_create(9011)
    i = act(newbie)
    await F.finish_link(i, code, source="code")
    check("日数が足りないと伝える ★", "14日以上" in i.text(), i.text()[:100])

    print("\n── 参加直後の手動入力を弾く ──")
    fresh = FakeUser(9012, "入ったばかり")
    fresh.joined_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await user_repo.get_or_create(9012)
    i = act(fresh)
    await F.finish_link(i, code, source="code")
    check("参加からの時間が足りないと伝える ★", "1時間以上" in i.text(), i.text()[:100])

    print("\n── DMが閉じている人 ──")
    shy = FakeUser(9013, "DM拒否")
    shy.dm_ok = False
    await user_repo.get_or_create(9013)
    i = act(shy)
    await F.finish_link(i, code, source="code")
    check("送れなかったと伝える ★", "送りできません" in i.text() or "できませんでした" in i.text(),
          i.text()[:100])
    check("その場で受け取れるボタンを出す ★", i.last_view() is not None, i.kinds)

    print("\n── 受取ボタンを押す ──")
    i = act(friend)
    await F.ClaimView().claim.callback(i)
    check("受け取れた ★", "受け取りました" in (i.last_embed().title or ""), i.text()[:80])
    check("claimed になった ★", await inv.is_claimed(FRIEND))
    check("次にすることを伝える", "ご注文" in i.text(), i.text()[:140])
    check("最低注文額を伝える ★", "¥400" in i.text(), i.text()[:140])
    i = act(friend)
    await F.ClaimView().claim.callback(i)
    check("二度押しても増えない ★", "すでに" in i.text(), i.text()[:80])

    print("\n── DMを再送信 ──")
    i = act(friend)
    await F.resend_dm(i)
    check("受け取り済みならそう言う ★", "すでに" in i.text(), i.text()[:80])
    nobody = FakeUser(9014)
    await user_repo.get_or_create(9014)
    i = act(nobody)
    await F.resend_dm(i)
    check("紐づいていなければそう言う ★", "見つかりません" in i.text(), i.text()[:80])
    f2 = FakeUser(FRIEND2, "お友達2")
    client.users[FRIEND2] = f2
    i = act(f2)
    await F.finish_link(i, code, source="code")
    f2.dms.clear()
    i = act(f2)
    await F.resend_dm(i)
    check("未受取なら送り直す ★", len(f2.dms) == 1 and "送りしました" in i.text(),
          (len(f2.dms), i.text()[:60]))

    print("\n── 紹介状況 ──")
    i = act(host)
    await F.show_status(i)
    e = i.last_embed()
    vals = {f.name: f.value for f in e.fields}
    check("紹介人数が出る", "ご紹介いただいた方" in vals, list(vals))
    # friend / f2 / DMを閉じている人 の3名が紐づいている
    check("紐づいた人数が正しい", "**3** 名" in vals.get("ご紹介いただいた方", ""), vals)
    check("達成0名", "**0** 名" in vals.get("達成（注文完了）", ""), vals)
    check("次の特典までが出る ★", any("次の特典" in k for k in vals), list(vals))

    print("\n── 達成して特典が入るまで ──")
    await inv.on_order_completed(FRIEND, 1000)
    await F.ClaimView().claim.callback(act(f2))
    paid = await inv.on_order_completed(FRIEND2, 1000)
    check("2名達成で特典 ★", paid == [(HOST, 500)], paid)
    await F.announce(client, FRIEND2, HOST, paid)
    check("紹介者へDMで知らせる ★", len(host.dms) == 1, host.dms)
    check("金額が書いてある", "¥500" in str(host.dms[0].get("embed").description))
    i = act(host)
    await F.show_status(i)
    vals = {f.name: f.value for f in i.last_embed().fields}
    check("受け取った額が出る ★", "¥500" in vals.get("受け取った特典", ""), vals)

    print("\n── 通知設定 ──")
    i = act(host)
    await F.toggle_notify(i)
    check("切ったと伝える ★", "受け取らない" in i.text(), i.text()[:80])
    check("切っても特典は入ると伝える ★", "残高に入ります" in i.text(), i.text()[:140])
    host.dms.clear()
    await F.announce(client, FRIEND2, HOST, [(HOST, 500)])
    check("切った人にはDMしない ★", not host.dms, host.dms)
    i = act(host)
    await F.toggle_notify(i)
    check("戻せる ★", "受け取る" in i.text(), i.text()[:80])

    print("\n── どのボタンも必ず返事をする ──")
    silent = []
    for name, fn, user in (
        ("発行", F.issue_link, host), ("状況", F.show_status, host),
        ("再送信", F.resend_dm, friend), ("通知", F.toggle_notify, host),
    ):
        i = act(user)
        await fn(i)
        if not i.text().strip():
            silent.append(name)
    check("黙って終わるボタンが無い ★", not silent, silent)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
