"""
サーバー管理：コマンドとイベントを実際に呼ぶ

services 側はテストできていても、**コマンドの中身とイベント処理**は
通さないと分からない。ここが落ちると利用者には
「ボタンを押しても何も起きない」としか見えない。

⚠️ 「応答していない」も失敗として扱う。
   Discordは3秒以内に応答しないと「アプリケーションが応答しませんでした」に
   なるため、黙って終わるのは落ちるのと同じくらい困る。
"""
import asyncio, os, sys, tempfile, traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord import app_commands

from _fake_discord import FakeChannel, FakeClient, FakeGuild, FakeInteraction, FakeUser
from core.crypto import init_cipher
from db.session import init_db, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class Role:
    def __init__(self, rid, name="r", position=1):
        self.id = rid; self.name = name; self.position = position
        self.mention = f"<@&{rid}>"; self.members = []
    def __ge__(s, o): return s.position >= o.position
    def __lt__(s, o): return s.position < o.position
    def __hash__(s): return hash(s.id)
    def __eq__(s, o): return isinstance(o, Role) and o.id == s.id


class Member(FakeUser):
    def __init__(self, uid, *, roles=(), admin=False, bot=False, age_days=365):
        super().__init__(uid)
        self.bot = bot; self.roles = list(roles)
        self.guild_permissions = discord.Permissions(administrator=admin)
        self.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.guild = None; self.timed_out_until = None
        self.added = []; self.removed = []; self.nick = None
    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position) if self.roles else Role(0, position=0)
    async def add_roles(self, *r, reason=None): self.added += list(r)
    async def remove_roles(self, *r, reason=None): self.removed += list(r)
    async def timeout(self, u, reason=None): self.timed_out_until = u
    async def kick(self, reason=None): pass


class Guild(FakeGuild):
    def __init__(self, gid=1):
        super().__init__(gid)
        self.owner_id = 99999
        self.default_role = Role(gid, "@everyone", 0)
        self.member_count = 42
        self.me = Member(100, roles=[Role(50, "bot", 50)])
        self.me.guild_permissions = discord.Permissions.all()
        self.me.guild = self
        self._roles = {}
        self.edits = []
    def get_role(self, rid): return self._roles.get(int(rid))
    def add_role(self, r): self._roles[r.id] = r; return r
    async def create_text_channel(self, name, **kw):
        ch = Ch(500 + len(self.channels), name); self.add_channel(ch); return ch
    async def edit(self, **kw): self.edits.append(kw)


class Ch(FakeChannel):
    def __init__(self, cid=1, name="ch"):
        super().__init__(cid)
        self.name = name; self.mention = f"<#{cid}>"; self.guild = None
    async def delete(self, reason=None): pass
    async def edit(self, **kw): pass
    async def set_permissions(self, *a, **kw): pass
    def overwrites_for(self, obj): return discord.PermissionOverwrite()
    def history(self, **kw):
        async def g():
            if False: yield None
        return g()


class Thread(Ch):
    """プライベートスレッド。閉じるときに消さず書庫に入れる。"""
    def __init__(self, cid, name):
        super().__init__(cid, name)
        self.added_users = []; self.archived = False; self.locked = False
    async def add_user(self, u): self.added_users.append(u)
    async def remove_user(self, u): pass
    async def edit(self, **kw):
        self.archived = kw.get("archived", self.archived)
        self.locked = kw.get("locked", self.locked)


class Parent(discord.TextChannel):
    """
    スレッドの親チャンネル。

    ⚠️ 本物の discord.TextChannel を継承する。
       tickets.create() が isinstance で種類を見ているため、
       似せただけの作り物では通らない（通らないことも含めて正しい）。
       mention / threads は読み取り専用なので触らない。
    """
    def __init__(self, cid, name, guild):
        self.id = cid; self.name = name; self.guild = guild
        self.sent = []; self.made = []
    async def create_thread(self, name, **kw):
        t = Thread(900 + len(self.made), name); t.guild = self.guild
        self.made.append(t); self.guild.add_channel(t); return t
    async def send(self, content=None, **kw): self.sent.append(kw)


class PurgeMsg:
    """一括削除の確認に使う。新しいほど created_at が後になる。"""
    def __init__(self, i, uid, age_days=0):
        self.id = i
        self.author = type("U", (), {"id": uid})()
        self.created_at = (datetime.now(timezone.utc)
                           - timedelta(days=age_days, minutes=200 - i))
        self.gone = False
    async def delete(self): self.gone = True


class PurgeCh:
    id = 4242
    def __init__(self, msgs): self.msgs = msgs
    def history(self, limit=100):
        async def gen():
            for m in sorted(self.msgs, key=lambda x: x.created_at,
                            reverse=True)[:limit]:
                yield m
        return gen()
    async def delete_messages(self, chunk, reason=None):
        for m in chunk: m.gone = True


class Msg:
    def __init__(self, author, *, content="", channel=None, guild=None,
                 mentions=(), attachments=()):
        self.author = author; self.content = content
        self.channel = channel or Ch(7); self.guild = guild
        self.mentions = list(mentions); self.role_mentions = []
        self.mention_everyone = False
        self.attachments = list(attachments); self.embeds = []
        self.created_at = datetime.now(timezone.utc)
        self.jump_url = "https://x"; self.deleted = False
    async def delete(self): self.deleted = True


async def main():
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/sc.db")
    from core import settings
    await settings.load_all()

    import cogs.guard as cg, cogs.mod as cm, cogs.ticket as ct, cogs.verify as cv

    client = FakeClient()
    client.intents = type("I", (), {"members": True, "message_content": True})()
    g = Guild()
    client.get_channel = lambda cid: g.get_channel(cid)
    staff_role = g.add_role(Role(88, "staff", 3))
    verify_role = g.add_role(Role(89, "認証済み", 2))
    admin = Member(1, admin=True, roles=[Role(9, "admin", 9)]); admin.guild = g
    plain = Member(2, roles=[Role(1, "ふつう", 1)]); plain.guild = g
    ch = Ch(7, "general"); g.add_channel(ch)
    logch = Ch(555, "log"); g.add_channel(logch)

    def I(user=None, channel=None):
        it = FakeInteraction(user or admin, client,
                             channel=channel or ch, guild=g)
        it.channel_id = (channel or ch).id
        it.guild_id = g.id
        return it

    async def run(name, cmd, cog, inter, *a, **kw):
        """コマンドを呼んで、落ちず・必ず応答することを確かめる。"""
        try:
            await cmd.callback(cog, inter, *a, **kw)
        except Exception:
            check(name, False, traceback.format_exc().strip().splitlines()[-1])
            return
        check(name, bool(inter.actions), "応答していません")

    tc, vc = ct.TicketCog(client), cv.VerifyCog(client)
    gc, mc = cg.GuardCog(client), cm.ModCog(client)

    print("\n[ /ticket のコマンド ]")
    await run("setup", tc.setup_cmd, tc, I(), True)
    await run("status", tc.status, tc, I())
    await run("staff 追加", tc.staff, tc, I(), staff_role)
    await run("staff 削除", tc.staff, tc, I(), staff_role, True)
    await run("staff 追加し直し", tc.staff, tc, I(), staff_role)
    await run("rules", tc.rules, tc, I(), 2, 72, True)
    await run("kinds 既定へ", tc.kinds_cmd, tc, I(), "")
    await run("kinds 指定", tc.kinds_cmd, tc, I(), "返金|お金のこと, 質問")
    await run("kinds 多すぎ", tc.kinds_cmd, tc, I(), ",".join(f"k{i}" for i in range(30)))
    await run("close（チケット外）", tc.close, tc, I())
    await run("add（チケット外）", tc.add, tc, I(), plain)
    await run("remove（チケット外）", tc.remove, tc, I(), plain)

    print("\n[ /verify のコマンド ]")
    await run("setup", vc.setup_cmd, vc, I(), True, verify_role)
    await run("rules", vc.rules, vc, I(), 7, 0, 5)
    await run("rules 自動退出ON", vc.rules, vc, I(), 0, 24, 5)
    await run("status", vc.status, vc, I())
    await run("user 手動", vc.user_cmd, vc, I(), plain)
    await run("reset 個人", vc.reset, vc, I(), plain)
    await run("reset 全員", vc.reset, vc, I())
    await run("bulk", vc.bulk, vc, I())
    await settings.set_value("verify_kick_hours", 0)

    print("\n[ /guard のコマンド ]")
    await run("setup", gc.setup_cmd, gc, I(), logch)
    await run("status", gc.status, gc, I())
    for key, label in (("join", "入室"), ("msgdelete", "削除")):
        await run(f"events ON（{label}）", gc.events, gc, I(),
                  app_commands.Choice(name=label, value=key), True)
    await run("events OFF", gc.events, gc, I(),
              app_commands.Choice(name="入室", value="join"), False)
    await run("events ON に戻す", gc.events, gc, I(),
              app_commands.Choice(name="入室", value="join"), True)
    await run("raid", gc.raid, gc, I(), True, 3, 10,
              app_commands.Choice(name="知らせるだけ", value="notify"))
    await run("unlock", gc.unlock, gc, I())
    await run("spam", gc.spam, gc, I(), True, 6, 5,
              app_commands.Choice(name="発言停止", value="timeout"), 6, 10)
    await run("words", gc.words, gc, I(), "ばつわーど", True)
    await run("exempt", gc.exempt, gc, I(), staff_role)
    await run("counter", gc.counter, gc, I(), ch, "👥 {count}人")
    await run("counter 形が違う", gc.counter, gc, I(), ch, "人数だけ")
    await run("recent", gc.recent, gc, I())

    print("\n[ /mod のコマンド ]")
    await run("warn", mc.warn, mc, I(), plain, "テストの理由")
    await run("warn 長い理由 ★", mc.warn, mc, I(), plain, "あ" * 5000)
    await run("warnings", mc.warnings, mc, I(), plain)
    await run("unwarn 全部", mc.unwarn, mc, I(), plain)
    await run("unwarn 番号指定", mc.unwarn, mc, I(), plain, 99999)
    await run("warn_rules", mc.warn_rules, mc, I(), 3, 60, 0, 0)
    await run("timeout", mc.timeout_cmd, mc, I(), plain, 10, "理由")
    await run("untimeout", mc.untimeout_cmd, mc, I(), plain)
    await run("kick", mc.kick_cmd, mc, I(), plain, "理由")
    await run("unban 数字でない", mc.unban_cmd, mc, I(), "abc")
    await run("purge", mc.purge, mc, I(), 5)
    await run("slowmode", mc.slowmode, mc, I(), 10)
    await run("lock", mc.lock, mc, I(), False)
    await run("lock 解除", mc.lock, mc, I(), True)

    print("\n[ 処分できない相手を断る ]")
    for name, target in (("自分自身", admin), ("BOT", Member(3, bot=True))):
        inter = I()
        await mc.warn.callback(mc, inter, target, "理由")
        said = str([a[1].get("embed") and a[1]["embed"].description
                    for a in inter.actions])
        check(f"{name}は断る ★", "できません" in said, said[:80])

    # ========================================================
    print("\n[ イベント処理 ]")
    # ========================================================
    await settings.set_value("guard_log_channel", logch.id)
    await settings.set_value("guard_events", list(
        __import__("config").GUARD_EVENTS_ALL))

    async def fire(name, coro):
        try:
            await coro
            check(name, True)
        except Exception:
            check(name, False, traceback.format_exc().strip().splitlines()[-1])

    newbie = Member(200, age_days=0); newbie.guild = g
    await fire("入室", gc.on_member_join(newbie))
    await fire("退室", gc.on_member_remove(plain))
    await fire("BAN", gc.on_member_ban(g, plain))
    await fire("BAN解除", gc.on_member_unban(g, plain))

    before = Member(201, roles=[Role(1, "前", 1)]); before.guild = g
    after = Member(201, roles=[Role(2, "後", 2)]); after.guild = g
    await fire("ロール変更", gc.on_member_update(before, after))
    after2 = Member(201, roles=before.roles); after2.nick = "新しい名前"
    after2.guild = g
    await fire("表示名の変更", gc.on_member_update(before, after2))
    after3 = Member(201, roles=before.roles); after3.guild = g
    after3.timed_out_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    await fire("発言停止", gc.on_member_update(before, after3))
    await fire("発言停止の解除", gc.on_member_update(after3, before))

    await fire("メッセージ削除",
               gc.on_message_delete(Msg(plain, content="けした", guild=g)))
    await fire("メッセージ編集", gc.on_message_edit(
        Msg(plain, content="前", guild=g), Msg(plain, content="後", guild=g)))
    await fire("チャンネル作成", gc.on_guild_channel_create(ch))
    await fire("チャンネル削除", gc.on_guild_channel_delete(ch))

    class VS:
        def __init__(self, channel): self.channel = channel
    await fire("ボイス参加", gc.on_voice_state_update(plain, VS(None), VS(ch)))
    await fire("ボイス退出", gc.on_voice_state_update(plain, VS(ch), VS(None)))
    await fire("ボイス移動", gc.on_voice_state_update(plain, VS(ch), VS(logch)))
    await fire("ボイス以外の変化（記録しない）",
               gc.on_voice_state_update(plain, VS(ch), VS(ch)))

    print("\n[ イベント：壊れた入力でも落ちないこと ]")
    await fire("DMのメッセージ", gc.on_message(Msg(plain, content="x", guild=None)))
    await fire("BOTのメッセージ",
               gc.on_message(Msg(Member(9, bot=True), content="x", guild=g)))
    await fire("記録先そのものの削除",
               gc.on_message_delete(Msg(plain, content="x", channel=logch, guild=g)))
    await fire("内容が同じ編集（記録しない）", gc.on_message_edit(
        Msg(plain, content="同じ", guild=g), Msg(plain, content="同じ", guild=g)))
    await fire("チケットの発言記録",
               tc.on_message(Msg(plain, content="x", guild=g)))

    print("\n[ イベント：記録が記録を呼ばないこと ]")
    logch.sent.clear()
    await gc.on_message_delete(Msg(plain, content="x", channel=logch, guild=g))
    check("記録先の削除は記録しない ★", not logch.sent, logch.sent)
    logch.sent.clear()
    await gc.on_message_delete(Msg(plain, content="x", channel=ch, guild=g))
    check("ふつうのチャンネルの削除は記録する ★", len(logch.sent) == 1,
          len(logch.sent))

    print("\n[ イベント：自動で対処する ]")
    await settings.set_value("guard_mention_limit", 2)
    await settings.set_value("guard_spam_enabled", False)
    logch.sent.clear()
    bomb = Msg(plain, content="x", channel=ch, guild=g,
               mentions=[Member(i) for i in range(6)])
    await gc.on_message(bomb)
    check("メンション爆撃を消す ★", bomb.deleted)
    check("対処したことを記録する ★", len(logch.sent) >= 1, len(logch.sent))

    admin_bomb = Msg(admin, content="x", channel=ch, guild=g,
                     mentions=[Member(i) for i in range(6)])
    await gc.on_message(admin_bomb)
    check("管理者は巻き込まない ★", not admin_bomb.deleted)

    bot_bomb = Msg(Member(9, bot=True), content="x", channel=ch, guild=g,
                   mentions=[Member(i) for i in range(6)])
    await gc.on_message(bot_bomb)
    check("BOTは巻き込まない ★", not bot_bomb.deleted)
    await settings.set_value("guard_mention_limit", 0)

    normal = Msg(plain, content="ふつうの話", channel=ch, guild=g)
    await gc.on_message(normal)
    check("ふつうの発言は消さない ★", not normal.deleted)

    print("\n[ イベント：荒らし ]")
    await settings.set_value("guard_raid_enabled", True)
    await settings.set_value("guard_raid_joins", 3)
    await settings.set_value("guard_raid_action", "lockdown")
    from services.server import guard as gsvc
    gsvc.detector.reset_joins(g.id)
    g.edits.clear(); logch.sent.clear()
    for i in range(3):
        m = Member(300 + i, age_days=100); m.guild = g
        await gc.on_member_join(m)
    check("認証レベルを上げる ★",
          any("verification_level" in e for e in g.edits), g.edits)
    check("管理者に知らせる ★", len(logch.sent) >= 1)
    await settings.set_value("guard_raid_enabled", False)
    await settings.set_value("guard_raid_action", "notify")

    print("\n[ 定期処理 ]")
    import cogs.tasks as tasks_cog
    t = tasks_cog.TasksCog(client)
    t._started = True
    client.guilds = [g]
    # ⚠️ 定期処理は中で例外を握りつぶす作りなので、
    #    偽物が不完全だと「通った」ように見えてしまう。本物に寄せる。
    client.get_cog = lambda name: {"GuardCog": gc}.get(name)
    await settings.set_value("guard_counter_channel", ch.id)
    before_name = ch.name
    try:
        await t.server_upkeep()
        check("定期処理が通る ★", True)
    except Exception:
        check("定期処理が通る ★", False,
              traceback.format_exc().strip().splitlines()[-1])
    got, detail = await gc.update_counter()
    check("メンバー数を出せる ★", got and "42" in detail, detail)
    client.intents = type("I", (), {"members": False, "message_content": True})()
    gc.bot = client
    g.member_count = None
    got, detail = await gc.update_counter()
    check("人数が取れないとき嘘を出さない ★",
          not got and "SERVER MEMBERS" in detail, detail)
    g.member_count = 42
    client.intents = type("I", (), {"members": True, "message_content": True})()
    gc.bot = client
    await settings.set_value("guard_counter_channel", None)
    try:
        n = await t._kick_unverified()
        check("認証ロール未設定なら誰も追い出さない ★", n == 0, n)
    except Exception:
        check("認証ロール未設定なら誰も追い出さない ★", False,
              traceback.format_exc().strip().splitlines()[-1])

    # ========================================================
    print("\n[ 利用者が押すボタン（チケット）]")
    # ========================================================
    from ui import server_views as V

    await settings.set_value("ticket_enabled", True)
    await settings.set_value("ticket_mode", "channel")
    await settings.set_value("ticket_staff_roles", [staff_role.id])

    panel = V.TicketPanel()
    sel = panel.children[0]
    sel._values = ["order"]
    it = I(plain)
    await sel.callback(it)
    check("種別を選ぶと入力画面が出る ★",
          [a[0] for a in it.actions] == ["send_modal"], it.actions)
    modal = it.actions[0][1]["modal"]
    modal.subject._value = "注文できません"
    modal.body._value = "エラーが出ます"
    it2 = I(plain)
    await modal.on_submit(it2)
    made = [c for c in g.channels.values() if c.name.startswith("ticket-")]
    check("場所ができる ★", len(made) == 1, [c.name for c in made])
    check("最初の案内が入る ★", bool(made and made[0].sent))
    view = made[0].sent[0].get("view") if made and made[0].sent else None
    check("閉じるボタンが付く ★",
          bool(view) and any(getattr(i, "custom_id", "") == "ticket:close"
                             for i in view.children))
    check("書いた内容も転記される ★", len(made[0].sent) >= 2, len(made[0].sent))

    ctrl = V.TicketControls()
    close_btn = [i for i in ctrl.children if i.custom_id == "ticket:close"][0]
    it = I(plain, made[0])
    await close_btn.callback(it)
    check("開いた本人は閉じられる ★",
          "send_modal" in [a[0] for a in it.actions], it.actions)
    stranger = Member(777, roles=[Role(1, "ふつう", 1)]); stranger.guild = g
    it = I(stranger, made[0])
    await close_btn.callback(it)
    said = str(it.actions[0][1].get("embed").description)
    check("関係ない人は閉じられない ★", "開いた" in said, said[:60])

    claim_btn = [i for i in ctrl.children if i.custom_id == "ticket:claim"][0]
    it = I(stranger, made[0])
    await claim_btn.callback(it)
    said = str(it.actions[0][1].get("embed").description)
    check("担当者でない人は担当できない ★", "担当者" in said, said[:60])

    print("\n[ 利用者が押すボタン（認証）]")
    await settings.set_value("verify_enabled", True)
    await settings.set_value("verify_role", verify_role.id)
    await settings.set_value("verify_mode", "button")
    await settings.set_value("verify_min_account_days", 0)
    vp = V.VerifyPanel()
    vbtn = [i for i in vp.children if i.custom_id == "verify:start"][0]

    newcomer = Member(801, roles=[Role(1, "ふつう", 1)]); newcomer.guild = g
    it = I(newcomer)
    await vbtn.callback(it)
    check("ボタンで認証できる ★", verify_role in newcomer.added, newcomer.added)
    it = I(newcomer)
    await vbtn.callback(it)
    said = str(it.actions[0][1].get("embed").description)
    check("2回目は『すでに認証済み』 ★", "すでに" in said, said[:60])

    await settings.set_value("verify_mode", "captcha")
    c_user = Member(802, roles=[Role(1, "ふつう", 1)]); c_user.guild = g
    it = I(c_user)
    await vbtn.callback(it)
    sent = it.actions[0][1]
    check("画像が出る ★", sent.get("file") is not None, list(sent))
    check("入力ボタンが付く ★", sent.get("view") is not None)
    check("本人にだけ見える ★", sent.get("ephemeral") is True)

    from services.server import verify as vsvc
    code = vsvc._puzzles[c_user.id][0]
    cm_modal = V.CaptchaModal(); cm_modal.answer._value = code
    it = I(c_user)
    await cm_modal.on_submit(it)
    check("正しい文字で認証できる ★", verify_role in c_user.added, c_user.added)

    bad_user = Member(803, roles=[Role(1, "ふつう", 1)]); bad_user.guild = g
    it = I(bad_user); await vbtn.callback(it)
    cm_modal = V.CaptchaModal(); cm_modal.answer._value = "ちがう文字"
    it = I(bad_user); await cm_modal.on_submit(it)
    said = str(it.actions[-1][1].get("embed").description)
    check("違う文字は断る ★", "違う" in said, said[:60])
    check("違うときロールは付かない ★", verify_role not in bad_user.added)

    await settings.set_value("verify_min_account_days", 30)
    baby = Member(804, roles=[Role(1, "ふつう", 1)], age_days=1); baby.guild = g
    it = I(baby)
    await vbtn.callback(it)
    said = str(it.actions[0][1].get("embed").description)
    check("作りたてのアカウントは断る ★", "日" in said and verify_role not in baby.added,
          said[:60])
    await settings.set_value("verify_min_account_days", 0)

    await settings.set_value("verify_enabled", False)
    it = I(Member(805))
    await vbtn.callback(it)
    said = str(it.actions[0][1].get("embed").description)
    check("受け付けOFFなら断る ★", "受け付け" in said, said[:60])

    # ========================================================
    print("\n[ チケット：スレッド方式 ]")
    # ========================================================
    from services.server import tickets as tsvc

    parent = Parent(10, "問い合わせ", g); g.add_channel(parent)
    await settings.set_value("ticket_mode", "thread")
    await settings.set_value("ticket_channel", parent.id)
    th_user = Member(901, roles=[Role(1, "ふつう", 1)]); th_user.guild = g
    t_th, place, _ = await tsvc.create(g, th_user, kind="other", subject="件名")
    check("スレッドを作れる ★", place.name.startswith("ticket-"), place.name)
    check("本人をスレッドに入れる ★", th_user in place.added_users)
    check("スレッドとして記録する ★", t_th.is_thread)
    await tsvc.close(client, place, closed_by=1, reason="解決")
    check("閉じると書庫行き・施錠する ★", place.archived and place.locked,
          (place.archived, place.locked))

    await settings.set_value("ticket_channel", None)
    try:
        await tsvc.create(g, Member(902), kind="other")
        check("親チャンネル未設定なら断る ★", False, "作れてしまった")
    except tsvc.TicketError as e:
        check("親チャンネル未設定なら断る ★", "設定" in str(e), str(e))

    print("\n[ チケット：放置の自動終了 ]")
    from db.models import Ticket
    from db.session import session_scope

    await settings.set_value("ticket_mode", "channel")
    await settings.set_value("ticket_auto_close_hours", 1)
    st_user = Member(903, roles=[Role(1, "ふつう", 1)]); st_user.guild = g
    t_st, ch_st, _ = await tsvc.create(g, st_user, kind="other")
    async with session_scope() as s:
        row = await s.get(Ticket, t_st.id)
        row.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=5)
    n = await tsvc.close_stale(client)
    check("放置されたものを閉じる ★", n == 1, n)
    texts = [str(k.get("content", "")) for k in ch_st.sent]
    check("閉じる前にお知らせを出す ★", any("終了" in t for t in texts), texts)
    check("キャッシュから消す ★", not tsvc.is_ticket_channel(ch_st.id))

    gone_user = Member(904, roles=[Role(1, "ふつう", 1)]); gone_user.guild = g
    t_g, ch_g, _ = await tsvc.create(g, gone_user, kind="other")
    async with session_scope() as s:
        row = await s.get(Ticket, t_g.id)
        row.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=5)
    del g.channels[ch_g.id]
    n = await tsvc.close_stale(client)
    check("場所が消えていても記録は閉じる ★", n == 1, n)
    check("そのキャッシュも消す ★", not tsvc.is_ticket_channel(ch_g.id))
    await settings.set_value("ticket_auto_close_hours", 72)

    # ========================================================
    print("\n[ 連投 → 発言停止 ]")
    # ========================================================
    from services.server import guard as gsvc2

    await settings.set_value("guard_spam_enabled", True)
    await settings.set_value("guard_spam_messages", 3)
    await settings.set_value("guard_spam_seconds", 10)
    await settings.set_value("guard_spam_action", "timeout")
    await settings.set_value("guard_timeout_minutes", 7)
    await settings.set_value("mod_dm_on_action", True)
    spammer = Member(905, roles=[Role(1, "ふつう", 1)]); spammer.guild = g
    room = Ch(77, "room"); g.add_channel(room)
    gsvc2.detector.spam_window().clear()
    logch.sent.clear()
    msgs = []
    for i in range(3):
        m = Msg(spammer, content=f"れんとう{i}", channel=room, guild=g)
        msgs.append(m)
        await gc.on_message(m)
    check("3件目で発言を止める ★", spammer.timed_out_until is not None)
    check("そのメッセージを消す ★", msgs[-1].deleted)
    check("本人に理由をDMする ★", bool(spammer.dms), spammer.dms)
    check("管理者に記録する ★", bool(logch.sent))
    await settings.set_value("guard_spam_enabled", False)

    # ========================================================
    print("\n[ 一括削除が正しい分だけ消すか ]")
    # ========================================================
    from services.server import mod as msvc

    msgs = [PurgeMsg(i, 1) for i in range(50)]
    n = await msvc.purge(PurgeCh(msgs), 10, by=1)
    gone = sorted(m.id for m in msgs if m.gone)
    check("指定した件数だけ消す ★", n == 10, n)
    check("新しい方から消す ★", gone == list(range(40, 50)), gone)

    msgs = [PurgeMsg(i, 1 if i % 2 == 0 else 2) for i in range(60)]
    n = await msvc.purge(PurgeCh(msgs), 5, by=1,
                         user=type("U", (), {"id": 1})())
    gone = [m for m in msgs if m.gone]
    check("相手を絞っても件数を超えない ★", n == 5, n)
    check("他の人の分は消さない ★", all(m.author.id == 1 for m in gone))

    msgs = [PurgeMsg(i, 1, age_days=0 if i >= 47 else 20) for i in range(50)]
    n = await msvc.purge(PurgeCh(msgs), 10, by=1)
    cut = datetime.now(timezone.utc) - timedelta(days=14)
    old_gone = [m.id for m in msgs if m.gone and m.created_at < cut]
    check("14日より古いものに手を出さない ★", not old_gone, old_gone)
    check("消せた件数を正直に返す ★", n == 3, n)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
