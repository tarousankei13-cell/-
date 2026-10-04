"""
サーバー管理（チケット・認証・監視・モデレーション）

自動で人を罰する機能が入っているので、
  ・誤って巻き込まないか（管理者・BOT・除外ロール）
  ・内容を読む権限が無い環境でも壊れないか
  ・権限不足のときに黙って失敗しないか
を重点的に見る。

⚠️ 記録先チャンネルの出来事を記録しない（記録が記録を呼ばない）ことも
   必ず確かめる。ここが抜けると、ログが無限に増える。
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


# ------------------------------------------------------------
#  この試験だけで使う作り物
# ------------------------------------------------------------

class Role:
    def __init__(self, rid, name="role", position=1):
        self.id = rid; self.name = name; self.position = position
        self.mention = f"<@&{rid}>"; self.members = []
    def __ge__(self, other): return self.position >= other.position
    def __lt__(self, other): return self.position < other.position
    def __hash__(self): return hash(self.id)
    def __eq__(self, o): return isinstance(o, Role) and o.id == self.id


class Member:
    def __init__(self, uid, *, roles=(), admin=False, bot=False, age_days=365):
        self.id = uid; self.bot = bot; self.name = f"u{uid}"
        self.display_name = self.name; self.mention = f"<@{uid}>"
        self.roles = list(roles)
        self.guild_permissions = discord.Permissions(administrator=admin)
        self.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.added = []; self.removed = []; self.dms = []
        self.timed_out_until = None
        self.guild = None
    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position) if self.roles else Role(0, position=0)
    async def add_roles(self, *roles, reason=None): self.added += list(roles)
    async def remove_roles(self, *roles, reason=None): self.removed += list(roles)
    async def send(self, **kw): self.dms.append(kw)
    async def timeout(self, until, reason=None): self.timed_out_until = until


class Guild:
    def __init__(self, gid=1):
        self.id = gid; self.name = "テストサーバー"; self.owner_id = 999
        self.roles = {}; self.me = Member(100, roles=[Role(50, "bot", 50)])
        self.me.guild = self
        self.created = []
        self.default_role = Role(gid, "@everyone", 0)
        self.member_count = 10
    def get_role(self, rid): return self.roles.get(int(rid))
    def add_role(self, role): self.roles[role.id] = role; return role
    async def create_text_channel(self, name, **kw):
        ch = Channel(1000 + len(self.created), name=name); ch.guild = self
        self.created.append(ch); return ch


class Channel:
    def __init__(self, cid=1, name="ch"):
        self.id = cid; self.name = name; self.mention = f"<#{cid}>"
        self.sent = []; self.guild = None; self.deleted = False
    async def send(self, **kw): self.sent.append(kw); return None
    async def delete(self, reason=None): self.deleted = True
    async def edit(self, **kw): pass
    def history(self, **kw):
        async def gen():
            if False: yield None
        return gen()


class Message:
    def __init__(self, author, *, content="", mentions=(), roles=(),
                 everyone=False, channel=None, guild=None):
        self.author = author; self.content = content
        self.mentions = list(mentions); self.role_mentions = list(roles)
        self.mention_everyone = everyone
        self.channel = channel or Channel(7)
        self.guild = guild or Guild()
        self.attachments = []; self.embeds = []
        self.deleted = False
    async def delete(self): self.deleted = True


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/srv.db")
    from core import settings
    from services.server import guard, logs, mod, tickets, verify
    await settings.load_all()

    async def put(**kw):
        for k, v in kw.items():
            await settings.set_value(k, v)

    # ========================================================
    print("\n[ 検知：数え方 ]")
    # ========================================================
    w = guard.Window(5.0)
    counts = [w.add(1, now=t) for t in (0, 1, 2, 3, 4, 10, 11)]
    check("窓の外に出た分は数えない ★", counts == [1, 2, 3, 4, 5, 1, 2], counts)
    check("利用者ごとに分かれている", w.add(2, now=11) == 1)
    w.clear(1)
    check("消せる", w.count(1, now=11) == 0)
    w2 = guard.Window(1.0); w2.add(5, now=0)
    check("古いだけの記録は捨てられる ★", w2.prune(now=100) == 1)

    print("\n[ 検知：形をそろえる ]")
    check("全角→半角", guard.normalize("ＮＧ") == "ng")
    check("大文字→小文字", guard.normalize("AbC") == "abc")
    check("空白を抜ける", guard._squeeze("あ い\nう") == "あいう")

    print("\n[ 検知：招待リンク ]")
    for text, want in [
        ("https://discord.gg/abc123", "abc123"),
        ("discord.com/invite/xyz", "xyz"),
        ("discordapp.com/invite/q", "q"),
        ("dsc.gg/zz", "zz"),
        ("ふつうの文章です", None),
        ("https://example.com/discord", None),
    ]:
        m = guard.INVITE_RE.search(text)
        got = m.group("code") if m else None
        check(f"「{text[:28]}」→ {want}", got == want, got)

    # ========================================================
    print("\n[ 検知：メッセージ ]")
    # ========================================================
    await put(guard_spam_enabled=False, guard_mention_limit=3,
              guard_invite_block=False, guard_words=[])
    u = Member(10)
    m = Message(u, mentions=[Member(1), Member(2)])
    check("上限以下なら何も起きない", guard.detector.check_message(m) == [])
    m = Message(u, mentions=[Member(1), Member(2), Member(3), Member(4)])
    hits = guard.detector.check_message(m)
    check("メンション爆撃を見つける ★",
          len(hits) == 1 and hits[0].kind == "mention", hits)
    check("対処は『消す』", hits[0].action == guard.ACT_DELETE)
    m = Message(u, mentions=[Member(1)], roles=[Role(1), Role(2), Role(3)])
    check("ロールへのメンションも数える",
          any(h.kind == "mention" for h in guard.detector.check_message(m)))
    m = Message(u, mentions=[Member(i) for i in range(5)])
    hits = guard.detector.check_message(m, has_content=False)
    check("内容を読めなくてもメンションは数えられる ★",
          any(h.kind == "mention" for h in hits), hits)
    await put(guard_mention_limit=0)
    m = Message(u, mentions=[Member(i) for i in range(30)])
    check("0なら無制限", guard.detector.check_message(m) == [])

    print("\n[ 検知：連投 ]")
    await put(guard_spam_enabled=True, guard_spam_messages=3,
              guard_spam_seconds=5, guard_spam_action="timeout")
    guard.detector.spam_window().clear()
    got = []
    for t in (0, 1, 2):
        got.append(guard.detector.check_message(Message(Member(11)), now=t))
    check("しきい値未満では動かない", got[0] == [] and got[1] == [])
    check("しきい値で検知する ★", any(h.kind == "spam" for h in got[2]), got[2])
    check("対処は『発言停止』", got[2][0].action == guard.ACT_TIMEOUT)
    guard.detector.spam_window().clear()
    slow = [guard.detector.check_message(Message(Member(12)), now=t)
            for t in (0, 10, 20)]
    check("ゆっくりなら検知しない ★", all(r == [] for r in slow), slow)

    print("\n[ 検知：同じ文の繰り返し ]")
    guard.detector.forget(13)
    rep = [guard.detector.check_message(
        Message(Member(13), content="あああ"), now=100 + i * 30) for i in range(3)]
    check("3回目で検知する ★", any(h.kind == "repeat" for h in rep[2]), rep[2])
    guard.detector.forget(14)
    diff = [guard.detector.check_message(
        Message(Member(14), content=f"ちがう文{i}"), now=200 + i * 30)
        for i in range(3)]
    check("違う文なら検知しない", not any(
        h.kind == "repeat" for r in diff for h in r))
    guard.detector.forget(15)
    noread = [guard.detector.check_message(
        Message(Member(15), content="あああ"), now=300 + i * 30,
        has_content=False) for i in range(3)]
    check("内容を読めないときは繰り返しを見ない ★",
          not any(h.kind == "repeat" for r in noread for h in r))

    print("\n[ 検知：招待リンクとNGワード ]")
    await put(guard_spam_enabled=False, guard_invite_block=True,
              guard_words=["ばつわーど", "NG語"])
    hits = guard.detector.check_message(
        Message(Member(20), content="こっちきて https://discord.gg/aaaa"))
    check("招待リンクを見つける ★", any(h.kind == "invite" for h in hits), hits)
    hits = guard.detector.check_message(
        Message(Member(21), content="これはばつわーどです"))
    check("NGワードを見つける ★", any(h.kind == "word" for h in hits), hits)
    hits = guard.detector.check_message(
        Message(Member(22), content="これはＮＧ語です"))
    check("全角でも見つける ★", any(h.kind == "word" for h in hits), hits)
    hits = guard.detector.check_message(
        Message(Member(23), content="ば つ わ ー ど"))
    check("字を離しても見つける ★", any(h.kind == "word" for h in hits), hits)
    hits = guard.detector.check_message(
        Message(Member(24), content="ふつうの話をします"))
    check("ふつうの文は素通し ★", hits == [], hits)
    hits = guard.detector.check_message(
        Message(Member(25), content="ばつわーど"), has_content=False)
    check("内容を読めないならNGワードは見ない ★", hits == [], hits)
    await put(guard_invite_block=False, guard_words=[])

    print("\n[ 検知：巻き込まない相手 ]")
    await put(guard_exempt_roles=[])
    check("BOTは対象外 ★", guard.exempt(Member(30, bot=True)))
    check("管理者は対象外 ★", guard.exempt(Member(31, admin=True)))
    normal = Member(32, roles=[Role(7)])
    check("ふつうの人は対象", not guard.exempt(normal))
    await put(guard_exempt_roles=[7])
    check("除外ロールを持つ人は対象外 ★", guard.exempt(normal))
    await put(guard_exempt_roles=[])

    print("\n[ 検知：荒らし（短時間の大量入室）]")
    await put(guard_raid_enabled=True, guard_raid_joins=3, guard_raid_seconds=10)
    guard.detector.reset_joins(1)
    r = [guard.detector.note_join(1, now=t) for t in (0, 1, 2)]
    check("しきい値未満では鳴らない", r[0] is None and r[1] is None)
    check("しきい値で鳴る ★", r[2] is not None and r[2].kind == "raid", r[2])
    guard.detector.reset_joins(1)
    slow = [guard.detector.note_join(1, now=t) for t in (0, 100, 200)]
    check("ゆっくりなら鳴らない ★", all(x is None for x in slow))
    await put(guard_raid_enabled=False)
    guard.detector.reset_joins(1)
    off = [guard.detector.note_join(1, now=t) for t in (0, 1, 2)]
    check("切っていれば鳴らない", all(x is None for x in off))
    check("切っていても数えてはいる ★",
          guard.detector.join_window().count(1, now=2) == 3)

    print("\n[ 検知：作りたてのアカウント ]")
    await put(guard_new_account_days=7)
    check("作りたてなら知らせる ★",
          guard.new_account_hit(Member(40, age_days=1)) is not None)
    check("古ければ知らせない",
          guard.new_account_hit(Member(41, age_days=100)) is None)
    await put(guard_new_account_days=0)
    check("0なら見ない", guard.new_account_hit(Member(42, age_days=1)) is None)

    # ========================================================
    print("\n[ 記録：ログが無限に増えないこと ]")
    # ========================================================
    await put(guard_log_channel=555, mod_log_channel=None,
              guard_events=["join", "msgdelete"])
    check("記録先そのものは除く ★", logs.is_log_channel(555))
    check("ふつうのチャンネルは除かない", not logs.is_log_channel(556))
    check("処分の記録先は未設定なら同じ場所", logs.mod_channel_id() == 555)
    await put(mod_log_channel=777)
    check("別に設定すればそちらへ", logs.mod_channel_id() == 777)
    check("そちらも記録先として除く ★", logs.is_log_channel(777))
    check("選んだ出来事は記録する", logs.enabled("join"))
    check("選んでいない出来事は記録しない ★", not logs.enabled("leave"))

    print("\n[ 記録：長さの上限 ]")
    check("1024文字を超えない ★", len(logs.trim("あ" * 5000)) <= 1024)
    check("短い文はそのまま", logs.trim("みじかい") == "みじかい")
    check("空は『なし』", logs.trim(None) == "（なし）")
    check("消えた人でも落ちない", logs.who(None) == "（不明）")

    # ========================================================
    print("\n[ チケット ]")
    # ========================================================
    await put(ticket_enabled=True, ticket_mode="channel",
              ticket_staff_roles=[88], ticket_max_open=2,
              ticket_auto_close_hours=72, ticket_category=None)
    check("方式を読める", tickets.mode() == "channel")
    check("既定の種別が4つ", len(tickets.kinds()) == 4)
    check("知らない種別でも落ちない", tickets.kind_of("zzz")["label"] == "zzz")
    staff = Member(50, roles=[Role(88)])
    check("担当ロールの人は担当者 ★", tickets.is_staff(staff))
    check("ふつうの人は担当者でない ★", not tickets.is_staff(Member(51)))
    check("管理者も担当者として扱う", tickets.is_staff(Member(52, admin=True)))
    check("None でも落ちない", not tickets.is_staff(None))

    g = Guild(); g.add_role(Role(88, "staff", 3))
    opener = Member(60); opener.guild = g
    t1, ch1, _ = await tickets.create(g, opener, kind="order", subject="件名A")
    check("チケットを作れる ★", t1.id > 0 and ch1 is not None)
    check("番号は4桁", t1.number == f"#{t1.id:04d}", t1.number)
    check("場所をキャッシュに覚える ★", tickets.is_ticket_channel(ch1.id))
    check("関係ない場所は覚えない", not tickets.is_ticket_channel(99999))

    t2, ch2, _ = await tickets.create(g, opener, kind="other")
    try:
        await tickets.create(g, opener, kind="other")
        check("同時に開ける数の上限が効く ★", False, "3件目が作れてしまった")
    except tickets.TicketError as e:
        check("同時に開ける数の上限が効く ★", "すでに" in str(e), str(e))

    other = Member(61); other.guild = g
    t3, ch3, _ = await tickets.create(g, other, kind="other")
    check("別の人は作れる ★", t3.id != t1.id)

    got = await tickets.claim(ch1.id, staff.id)
    check("担当者を決められる ★", got.claimed_by == staff.id)
    check("状態が変わる", got.status == "CLAIMED")
    try:
        await tickets.claim(ch1.id, 77)
        check("他の人は横取りできない ★", False)
    except tickets.TicketError as e:
        check("他の人は横取りできない ★", "すでに" in str(e))

    await put(ticket_enabled=False)
    try:
        await tickets.create(g, Member(62), kind="other")
        check("受け付けOFFなら作れない ★", False)
    except tickets.TicketError:
        check("受け付けOFFなら作れない ★", True)
    await put(ticket_enabled=True)

    class Bot:
        def get_channel(self, cid): return None
        async def fetch_channel(self, cid): raise discord.NotFound(
            type("R", (), {"status": 404, "reason": ""})(), "x")
    await put(ticket_log_channel=None, guard_log_channel=None)
    closed = await tickets.close(Bot(), ch1, closed_by=staff.id, reason="解決")
    check("閉じられる ★", closed.status == "CLOSED")
    check("閉じたらキャッシュから消える ★", not tickets.is_ticket_channel(ch1.id))
    check("場所を片づける", ch1.deleted)
    try:
        await tickets.close(Bot(), ch1, closed_by=staff.id)
        check("二重に閉じられない ★", False)
    except tickets.TicketError:
        check("二重に閉じられない ★", True)

    opened = await tickets.open_tickets(g.id)
    check("開いているものだけ数える ★", len(opened) == 2, len(opened))
    c = await tickets.counts(g.id)
    check("状態ごとに数えられる", c.get("CLOSED") == 1, c)

    n = await tickets.reload_cache()
    check("再起動後に覚え直せる ★", n == 2 and tickets.is_ticket_channel(ch2.id), n)

    await put(ticket_auto_close_hours=0)
    check("0なら自動で閉じない ★", await tickets.stale() == [])
    await put(ticket_auto_close_hours=1)
    from db.models import Ticket
    async with session_scope() as s:
        row = await s.get(Ticket, t2.id)
        row.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=5)
    st = await tickets.stale()
    check("放置されたものを見つける ★", [x.id for x in st] == [t2.id],
          [x.id for x in st])
    await put(ticket_auto_close_hours=72)

    # ========================================================
    print("\n[ 認証 ]")
    # ========================================================
    await put(verify_enabled=True, verify_mode="captcha",
              verify_captcha_length=5, verify_max_attempts=3)
    check("やり方を読める", verify.mode() == "captcha")
    code = verify.new_code()
    check("長さの設定が効く", len(code) == 5, code)
    import config as C
    check("見間違える字を使わない ★",
          not (set("01OI258BSZ") & set(C.VERIFY_CAPTCHA_CHARS)))
    check("毎回ちがう問題 ★", len({verify.new_code() for _ in range(20)}) > 10)

    c1 = verify.issue(70)
    check("そのまま合う", verify.answer_matches(70, c1) is True)
    check("一度合ったら使えない ★", verify.answer_matches(70, c1) is None)
    c2 = verify.issue(71)
    check("小文字でも合う ★", verify.answer_matches(71, c2.lower()) is True)
    c3 = verify.issue(72)
    wide = "".join(chr(ord(x) + 0xFEE0) for x in c3)
    check("全角でも合う ★", verify.answer_matches(72, wide) is True)
    c4 = verify.issue(73)
    check("空白が混ざっても合う", verify.answer_matches(73, " ".join(c4)) is True)
    c5 = verify.issue(74)
    bad = "AAAAA" if c5 != "AAAAA" else "CCCCC"
    check("違えば False", verify.answer_matches(74, bad) is False)
    check("間違えても問題は残る ★", verify.answer_matches(74, c5) is True)
    check("出していなければ None ★", verify.answer_matches(7777, "ABCDE") is None)
    verify._puzzles[75] = ("ABCDE", -99999.0)
    check("時間切れは None ★", verify.answer_matches(75, "ABCDE") is None)

    f = verify.render("A3K7M")
    check("画像を作れる ★", f.filename == "verify.png" and len(f.fp.getvalue()) > 1000)

    await put(verify_min_account_days=30)
    check("新しいアカウントは断る ★",
          verify.account_too_new(Member(80, age_days=1)) is not None)
    check("古いアカウントは通す",
          verify.account_too_new(Member(81, age_days=100)) is None)
    await put(verify_min_account_days=0)
    check("0なら条件なし", verify.account_too_new(Member(82, age_days=0)) is None)

    g2 = Guild(2)
    low = g2.add_role(Role(90, "認証済み", 3))
    await put(verify_role=90)
    target = Member(83); target.guild = g2
    await verify.grant(target, method="button")
    check("ロールが付く ★", low in target.added, target.added)
    check("認証済みとして残る ★", await verify.already(2, 83))

    high = g2.add_role(Role(91, "高すぎるロール", 99))
    await put(verify_role=91)
    t2m = Member(84); t2m.guild = g2
    try:
        await verify.grant(t2m)
        check("BOTより上のロールは断る ★", False, "付けられてしまった")
    except verify.VerifyError as e:
        check("BOTより上のロールは断る ★", "上" in str(e), str(e))
    await put(verify_role=None)
    try:
        await verify.grant(t2m)
        check("ロール未設定なら断る ★", False)
    except verify.VerifyError as e:
        check("ロール未設定なら断る ★", "設定" in str(e))
    await put(verify_role=90)

    n1 = await verify.note_attempt(2, 85)
    n2 = await verify.note_attempt(2, 85)
    check("失敗を数える ★", (n1, n2) == (1, 2), (n1, n2))
    check("残り回数が減る ★", await verify.attempts_left(2, 85) == 1)
    await verify.note_attempt(2, 85)
    check("上限で0になる ★", await verify.attempts_left(2, 85) == 0)
    st = await verify.stats(2)
    check("認証済みと失敗中を分けて数える ★",
          st["verified"] >= 1 and st["failing"] >= 1, st)

    # ========================================================
    print("\n[ 処分 ]")
    # ========================================================
    await put(mod_warn_timeout_at=3, mod_warn_kick_at=0, mod_warn_ban_at=0)
    check("2回では何もしない", mod.escalation(2) is None)
    check("3回で発言停止 ★", mod.escalation(3)[0] == "timeout")
    await put(mod_warn_kick_at=5)
    check("5回でキック", mod.escalation(5)[0] == "kick")
    await put(mod_warn_ban_at=5)
    check("強い方が勝つ ★", mod.escalation(5)[0] == "ban", mod.escalation(5))
    await put(mod_warn_timeout_at=0, mod_warn_kick_at=0, mod_warn_ban_at=0)
    check("全部0なら何もしない", mod.escalation(99) is None)
    await put(mod_warn_timeout_at=3)

    g3 = Guild(3)
    boss = Member(90, roles=[Role(10, "上", 10)]); boss.guild = g3
    low_m = Member(91, roles=[Role(1, "下", 1)]); low_m.guild = g3
    check("自分は処分できない ★", mod.can_act(boss, boss) is not None)
    check("BOTは処分できない ★",
          mod.can_act(boss, Member(92, bot=True)) is not None)
    owner = Member(999); owner.guild = g3
    check("所有者は処分できない ★", mod.can_act(boss, owner) is not None)
    check("下の人は処分できる ★", mod.can_act(boss, low_m) is None,
          mod.can_act(boss, low_m))
    tall = Member(93, roles=[Role(80, "とても上", 80)]); tall.guild = g3
    check("BOTより上の人は断る ★", mod.can_act(boss, tall) is not None)
    check("理由が日本語で返る ★", "ロール" in (mod.can_act(boss, tall) or ""))

    wid, cnt = await mod.add_warning(3, 91, 90, "テストの理由")
    check("警告を残せる ★", wid > 0 and cnt == 1, (wid, cnt))
    _, cnt2 = await mod.add_warning(3, 91, 90, "2回目")
    check("たまっていく", cnt2 == 2)
    check("取り消せる ★", await mod.clear_warning(wid, 90))
    check("二重には取り消せない", not await mod.clear_warning(wid, 90))
    live = await mod.active_warnings(3, 91)
    check("有効な分だけ数える ★", len(live) == 1, len(live))
    allw = await mod.all_warnings(3, 91)
    check("履歴そのものは消さない ★", len(allw) == 2, len(allw))
    n = await mod.clear_all_warnings(3, 91, 90)
    check("まとめて取り消せる", n == 1 and not await mod.active_warnings(3, 91))

    print("\n[ 処分：Discord の制限 ]")
    tm = Member(94, roles=[Role(1, "下", 1)]); tm.guild = g3
    try:
        await mod.timeout(tm, 60 * 24 * 40, reason="長すぎ", by=90)
        check("28日を超える発言停止は断る ★", False)
    except mod.ModError as e:
        check("28日を超える発言停止は断る ★", "28" in str(e), str(e))
    try:
        await mod.timeout(tm, 0, reason="0分", by=90)
        check("0分は断る", False)
    except mod.ModError:
        check("0分は断る", True)
    await mod.timeout(tm, 10, reason="ふつう", by=90)
    check("ふつうの長さは通る ★", tm.timed_out_until is not None)

    print("\n[ 処分：本人への連絡 ]")
    await put(mod_dm_on_action=True)
    m1 = Member(95)
    got = await mod.notify(m1, guild_name="テスト", action="警告", reason="理由")
    check("DMが届く ★", got and len(m1.dms) == 1)
    body = str(m1.dms[0].get("embed").description)
    check("理由が入っている ★", "理由" in body, body[:80])
    class NoDM(Member):
        async def send(self, **kw):
            raise discord.Forbidden(
                type("R", (), {"status": 403, "reason": ""})(), "閉じています")
    got = await mod.notify(NoDM(96), guild_name="テスト", action="警告", reason="r")
    check("DMを閉じていても落ちない ★", got is False)
    await put(mod_dm_on_action=False)
    m2 = Member(97)
    check("設定を切れば送らない ★",
          await mod.notify(m2, guild_name="t", action="a", reason="r") is False
          and not m2.dms)
    await put(mod_dm_on_action=True)

    # ========================================================
    print("\n[ コマンドと画面 ]")
    # ========================================================
    import cogs.guard as cg, cogs.mod as cm, cogs.ticket as ct, cogs.verify as cv
    for name, group, want in [
        ("/ticket", ct.TicketCog.group,
         {"setup", "status", "staff", "rules", "kinds", "close", "add", "remove"}),
        ("/verify", cv.VerifyCog.group,
         {"setup", "rules", "status", "user", "reset", "bulk"}),
        ("/guard", cg.GuardCog.group,
         {"setup", "status", "events", "raid", "unlock", "spam", "words",
          "exempt", "counter", "recent"}),
        ("/mod", cm.ModCog.group,
         {"warn", "warnings", "unwarn", "warn_rules", "timeout", "untimeout",
          "kick", "ban", "unban", "purge", "slowmode", "lock"}),
    ]:
        got = {c.name for c in group.commands}
        check(f"{name} のコマンドがそろっている", got == want, got ^ want)
        check(f"{name} は25個以内", len(got) <= 25, len(got))

    listeners = {m for m in dir(cg.GuardCog) if m.startswith("on_")}
    check("監視するイベントがそろっている ★", {
        "on_member_join", "on_member_remove", "on_member_ban",
        "on_member_unban", "on_member_update", "on_message_delete",
        "on_message_edit", "on_guild_channel_create",
        "on_guild_channel_delete", "on_voice_state_update", "on_message",
    } <= listeners, listeners)

    from ui import panels
    names = [c.__name__ for c in panels.PERSISTENT_VIEWS]
    check("チケットのボタンが再起動後も効く ★",
          {"TicketPanel", "TicketControls"} <= set(names), names)
    check("認証のボタンが再起動後も効く ★", "VerifyPanel" in names)
    check("パネルを貼れる", {"ticket", "verify"} <= set(panels.PANEL_BUILDERS))
    ids = set()
    for cls in panels.PERSISTENT_VIEWS:
        for item in cls().children:
            cid = getattr(item, "custom_id", None)
            if cid:
                check(f"custom_id が重複していない（{cid}）", cid not in ids, cid)
                ids.add(cid)

    from ui import embeds, server_views
    await put(ticket_enabled=True, verify_enabled=True)
    check("チケットパネルを作れる", embeds.ticket_panel().title is not None)
    check("認証パネルを作れる", embeds.verify_panel().title is not None)
    e = embeds.ticket_closed(closed)
    check("終了の記録を作れる", "お問い合わせ" in e.title)
    v = server_views.TicketPanel()
    sel = v.children[0]
    check("種別の選択肢が出る ★", len(sel.options) == 4, len(sel.options))
    await put(ticket_kinds=[{"key": f"k{i}", "label": f"種別{i}", "emoji": "💬",
                            "desc": ""} for i in range(30)])
    sel = server_views.TicketPanel().children[0]
    check("選択肢は25個までに切る ★", len(sel.options) == 25, len(sel.options))
    import config as C2
    await put(ticket_kinds=list(C2.TICKET_KINDS_DEFAULT))
    await put(ticket_kinds=[])
    check("種別が空でも既定へ落ちる ★", len(tickets.kinds()) == 4)
    await put(ticket_kinds=list(C2.TICKET_KINDS_DEFAULT))

    print("\n[ 設定 ]")
    for key in ("ticket_enabled", "verify_enabled", "guard_log_channel",
                "guard_events", "mod_warn_timeout_at", "guard_exempt_roles"):
        check(f"{key} が既定にある", key in settings.DEFAULTS)
    check("監視できる出来事の名前が一致 ★",
          set(logs.EVENTS) == set(C2.GUARD_EVENTS_ALL),
          set(logs.EVENTS) ^ set(C2.GUARD_EVENTS_ALL))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
