"""
一斉通知の検証

一斉送信は取り消せないので、**送る前に必ず確認を挟む**ことと、
DMを拒否している人がいても最後まで配り切ることを確かめる。
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from _fake_discord import FakeInteraction, FakeUser, FakeClient

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/bc.db")
    from core import settings, users as user_repo
    await settings.load_all()
    from ui import admin_flows

    client = FakeClient()
    admin = FakeUser(1, name="管理者")

    # 利用者を5人。うち1人はDM拒否、1人は利用停止
    for uid in range(100, 105):
        await user_repo.get_or_create(uid)
        u = FakeUser(uid, name=f"利用者{uid}")
        client.users[uid] = u
    client.users[102].dm_ok = False
    async with session_scope() as s:
        from db.models import User
        banned = await s.get(User, 104)
        banned.is_banned = True

    HEAD, BODY = "メンテナンスのお知らせ", "本日22時から30分ほど注文を停止します。"

    print("\n[1] 送る前に確認が入る ★")
    itx = FakeInteraction(admin, client)
    await admin_flows.preview_broadcast(itx, HEAD, BODY, "dm")
    t = itx.text()
    check("確認を求める ★", "この内容で送ります" in t, t[:120])
    check("見出しが見える", HEAD in t, t[:200])
    check("本文が見える", BODY in t, t[:300])
    check("宛先の人数が出る ★", "4人" in t or "4" in t, t[:200])
    view = itx.last_view()
    labels = [getattr(c, "label", "") for c in view.children]
    check("送信とやめるが選べる", "送信する" in labels and "やめる" in labels, labels)

    print("\n[2] やめられる ★")
    cancel = next(c for c in view.children if getattr(c, "label", "") == "やめる")
    itx2 = FakeInteraction(admin, client)
    await cancel.callback(itx2)
    check("やめた旨が出る", "やめました" in itx2.text(), itx2.text()[:80])
    check("誰にも届いていない ★", all(not u.dms for u in client.users.values()),
          {k: len(v.dms) for k, v in client.users.items()})

    print("\n[3] 送れる")
    itx3 = FakeInteraction(admin, client)
    await admin_flows.preview_broadcast(itx3, HEAD, BODY, "dm")
    send = next(c for c in itx3.last_view().children
                if getattr(c, "label", "") == "送信する")
    itx4 = FakeInteraction(admin, client)
    await send.callback(itx4)
    got = {k: len(v.dms) for k, v in client.users.items()}
    check("DMが届く", got[100] == 1 and got[101] == 1 and got[103] == 1, got)
    check("DM拒否の人には届かない", got[102] == 0, got)
    check("利用停止の人には送らない ★", got[104] == 0, got)
    t4 = itx4.text()
    check("届いた件数を報告する", "3" in t4, t4[:150])
    check("DM拒否の件数も報告する ★", "受け取らない設定" in t4, t4[:200])

    print("\n[4] 中身が正しい")
    dm = client.users[100].dms[0]
    e = dm.get("embed")
    check("見出しが入る", e and HEAD in (e.title or ""), e.title if e else None)
    check("本文が入る", e and BODY in (e.description or ""), e.description if e else None)
    check("運営からと分かる", e and "運営" in (e.footer.text or ""),
          e.footer.text if e else None)

    print("\n[5] 記録に残る ★")
    from core import audit
    rows = await audit.search(action="broadcast")
    check("監査ログに残る ★", len(rows) == 1, len(rows))
    check("送った人が分かる", rows[0].actor_id == 1, rows[0].actor_id)
    check("見出しが残る", HEAD in rows[0].after, rows[0].after)

    print("\n[6] チャンネルへも送れる")
    await settings.set_value("channel_achievement", 777, updated_by=1)
    itx5 = FakeInteraction(admin, client)
    await admin_flows.preview_broadcast(itx5, HEAD, BODY, "channel")
    check("宛先がチャンネルになる", "チャンネル" in itx5.text(), itx5.text()[:150])
    send2 = next(c for c in itx5.last_view().children
                 if getattr(c, "label", "") == "送信する")
    itx6 = FakeInteraction(admin, client)
    await send2.callback(itx6)
    check("チャンネルへ投稿される", len(client.sent.get(777, [])) == 1,
          len(client.sent.get(777, [])))

    print("\n[7] チャンネル未設定なら分かる文面で断る")
    await settings.set_value("channel_achievement", None, updated_by=1)
    itx7 = FakeInteraction(admin, client)
    await admin_flows.preview_broadcast(itx7, HEAD, BODY, "channel")
    send3 = next(c for c in itx7.last_view().children
                 if getattr(c, "label", "") == "送信する")
    itx8 = FakeInteraction(admin, client)
    await send3.callback(itx8)
    check("設定方法を案内する", "/config channel achievement" in itx8.text(),
          itx8.text()[:200])

    print("\n[8] 1人失敗しても最後まで配る ★")
    for u in client.users.values():
        u.dms.clear()
    # 102 はすでに拒否。さらに2人増やして計3人が拒否になる
    client.users[101].dm_ok = False
    client.users[103].dm_ok = False
    itx9 = FakeInteraction(admin, client)
    await admin_flows.preview_broadcast(itx9, HEAD, BODY, "dm")
    send4 = next(c for c in itx9.last_view().children
                 if getattr(c, "label", "") == "送信する")
    itx10 = FakeInteraction(admin, client)
    await send4.callback(itx10)
    check("届く人には届く ★", len(client.users[100].dms) == 1,
          len(client.users[100].dms))
    check("拒否された数を数える", "**3** 件" in itx10.text(), itx10.text()[:200])

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
