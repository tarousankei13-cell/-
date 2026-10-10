"""全スラッシュコマンドの総当たり

⚠️ 137 個あるコマンドのうち 58 個が、どのテストからも一度も呼ばれて
   いなかった。呼ばれないコマンドは壊れていても気づけない。実際に
   `/panel refresh` の抜けや `/debug fails` の NameError は、呼ばれて
   いなかったせいで見逃していた。

ここでは**引数を型から自動で作って全部呼ぶ**。中身の正しさまでは見ない。
見るのは次の2つだけ。

   ① 例外を出さずに終わること
   ② 必ず何か応答すること（Discord は3秒で「応答しませんでした」になる）

⚠️ これは中身の検査の代わりにはならない。「落ちない」だけを保証する
   網であって、個別の検査は各テストの仕事。
"""
import asyncio, os, sys, tempfile, traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord import app_commands

import config
from _fake_discord import FakeClient, FakeInteraction, FakeUser
from core import settings
from core.crypto import init_cipher
from db.session import close_db, init_db, session_scope
from db.models import KyashAccount, McdAccount, utcnow

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


# ⚠️ 呼んではいけないもの。
#    restart は os.execv でこのテスト自体を置き換えてしまう。
#    sync は本物の Discord と通信する。
SKIP = {"restart", "sync_commands",
        # ⚠️ 添付ファイルが要るコマンド。添付は偽物を作れず、本物の
        #    Discord は必須の添付が無ければそもそも呼ばせない。
        #    ここで「落ちた」と数えると、起きないことを追いかけることになる。
        "admin_restore"}

# ⚠️ 外と通信するコマンド。この環境では相手がいないので時間切れになる。
#    「落ちた」とは区別する。通信そのものは別のテストで見ている。
NETWORK = {"/store reindex", "/menu sync", "/proxy test", "/web test"}


# ⚠️ 偽物が薄いと、コマンドではなく偽物が落ちる。それを「バグ」と
#    数えると本物が埋もれる。本物の discord が持っている属性のうち、
#    コマンドが実際に触るものは一通り持たせる。

class Role:
    def __init__(self, rid=70, name="テスト役職", pos=1):
        self.id = rid; self.name = name; self.mention = f"<@&{rid}>"
        self.position = pos
        self.members = []
    async def edit(self, **kw): pass
    # ⚠️ 本物の discord.Role は位置で比べられる。モデレーションは
    #    「自分より上の役職か」を比較で判断するので、これが無いと
    #    コマンドではなく偽物が落ちる。
    def __lt__(self, o): return self.position < o.position
    def __le__(self, o): return self.position <= o.position
    def __gt__(self, o): return self.position > o.position
    def __ge__(self, o): return self.position >= o.position


class Overwrite:
    def __init__(self): self.send_messages = None
    def update(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)


class Channel:
    def __init__(self, cid=700, name="テスト部屋", guild=None):
        self.id = cid; self.name = name; self.mention = f"<#{cid}>"
        self.sent = []; self.guild = guild
        self.slowmode_delay = 0
        self.type = discord.ChannelType.text
        self.permissions_synced = False
    async def send(self, content=None, **kw):
        self.sent.append(kw)
        return type("M", (), {"id": 1, "jump_url": "https://x",
                              "pin": _anoop, "edit": _anoop})()
    async def edit(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)
    def history(self, **kw):
        async def gen():
            if False: yield None
        return gen()
    def overwrites_for(self, obj): return Overwrite()
    async def set_permissions(self, *a, **kw): pass
    def permissions_for(self, obj):
        return discord.Permissions.all()
    async def create_thread(self, name, **kw):
        return Channel(900, name, self.guild)
    async def purge(self, **kw): return []
    async def fetch_message(self, mid): raise discord.NotFound(_resp(404), "no")


async def _anoop(*a, **kw): return None


def _resp(status):
    return type("R", (), {"status": status, "reason": "x"})()


class Member(FakeUser):
    """コマンドが触る属性を一通り持った人。"""
    def __init__(self, uid=4242, name="対象の人", guild=None, bot=False):
        super().__init__(uid, name)
        self.bot = bot
        self.guild = guild
        self.roles = [Role(1, "ふつう", 1)]
        self.top_role = self.roles[0]
        self.timed_out_until = None
        self.joined_at = utcnow()
        self.created_at = utcnow()
        self.display_avatar = type("A", (), {"url": "https://x/a.png"})()
        self.added = []; self.removed = []
        self.guild_permissions = discord.Permissions.all()
    async def add_roles(self, *roles, **kw): self.added += list(roles)
    async def remove_roles(self, *roles, **kw): self.removed += list(roles)
    async def timeout(self, until, **kw): self.timed_out_until = until
    async def kick(self, **kw): pass
    async def ban(self, **kw): pass
    async def edit(self, **kw): pass
    async def send(self, *a, **kw): return None


class Guild:
    def __init__(self, gid=1):
        self.id = gid; self.name = "テストサーバー"
        self.roles = [Role(1, "ふつう", 1)]
        self.channels = []
        self.members = []
        self.member_count = 42
        self.me = Member(99, "BOT", bot=True)
        self.me.top_role = Role(98, "BOTの役職", 50)
        self.default_role = self.roles[0]
        self.owner_id = 1
        self.created_at = utcnow()
        self.icon = None
    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)
    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)
    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)
    async def edit(self, **kw): pass
    async def fetch_member(self, uid):
        return self.get_member(uid) or Member(uid, guild=self)
    async def ban(self, user, **kw): pass
    async def unban(self, user, **kw): pass


def fake_arg(param, guild=None, as_choice=True):
    """コマンドの引数を、型から自動で作る。"""
    # ⚠️ 選択肢つきの引数は、コマンドによって Choice を受けるものと
    #    文字列を受けるものがある。装飾子からは区別できないので、
    #    まず Choice で呼び、駄目なら文字列で呼び直す。
    if getattr(param, "choices", None):
        c = param.choices[0]
        return c if as_choice else c.value
    t = param.type
    T = discord.AppCommandOptionType
    if t is T.string:
        # ⚠️ 長さ制限に収まる値にする。はみ出すと Discord が弾く。
        lo = getattr(param, "min_value", None) or 1
        v = "test"
        hi = getattr(param, "max_value", None)
        if isinstance(hi, int) and hi < len(v):
            v = v[:hi]
        if isinstance(lo, int) and len(v) < lo:
            v = (v * lo)[:lo]
        return v
    if t is T.integer:
        lo = param.min_value if param.min_value is not None else 1
        hi = param.max_value if param.max_value is not None else 10
        return max(lo, min(hi, 1))
    if t is T.number:
        lo = param.min_value if param.min_value is not None else 1
        hi = param.max_value if param.max_value is not None else 10
        return float(max(lo, min(hi, 1)))
    if t is T.boolean:
        return True
    if t in (T.user, T.mentionable):
        return Member(4242, "対象の人", guild=guild)
    if t is T.role:
        return Role()
    if t is T.channel:
        return Channel(guild=guild)
    if t is T.attachment:
        # 添付は「無い」で呼ぶ。必須なら Discord 側が弾くので、
        # ここで作り込むより「無くても落ちない」ことを見たい。
        return None
    return None


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/all.db")
    await settings.load_all()

    async with session_scope() as s:
        s.add(McdAccount(id=1, label="mcd-1", email_enc=b"x", card_id="c1",
                         device_uid="d", wmop_device_id="w", fb_instance_id="f",
                         home_lat=35.0, home_lng=139.0))
        s.add(KyashAccount(id=1, label="kyash-1", email_enc=b"x",
                           token_obtained_at=utcnow()))

    client = FakeClient()
    client.intents = type("I", (), {"members": True, "message_content": True})()
    guild = Guild()
    ch = Channel(guild=guild)
    guild.channels.append(ch)
    guild.members.append(Member(4242, "対象の人", guild=guild))
    client.guilds = [guild]
    client.get_channel = lambda cid: ch
    client.get_guild = lambda gid: guild
    client.user = guild.me

    # ⚠️ cogs/ を自動で全部読む。手で並べると、新しい cog を足したときに
    #    ここへ書き足し忘れ、またテストされないコマンドが生まれる。
    import importlib

    mods = []
    for f in sorted(os.listdir("cogs")):
        if f.endswith(".py") and not f.startswith("_"):
            mods.append(importlib.import_module(f"cogs.{f[:-3]}"))
    print(f"[ 読み込んだ cogs: {len(mods)} 個 ]")

    cogs_list = []
    for mod in mods:
        for nm in dir(mod):
            obj = getattr(mod, nm)
            if (isinstance(obj, type) and nm.endswith("Cog")
                    and obj.__module__ == mod.__name__):
                try:
                    cogs_list.append(obj(client))
                except Exception:
                    check(f"{nm} を作れる", False,
                          traceback.format_exc().strip().splitlines()[-1])
    print(f"\n[ 読み込めた Cog: {len(cogs_list)} 個 ]")

    # ⚠️ 本物の Bot と同じく client から cog を引けるようにする。
    #    `/help` は登録済みのコマンドをここから数えるため、
    #    入れておかないと `/help` だけが落ちる。
    client.cogs = {type(c).__name__: c for c in cogs_list}

    total = crashed = silent = skipped = network_slow = 0
    problems = []
    for cog in cogs_list:
        for cmd in cog.walk_app_commands():
            if not isinstance(cmd, app_commands.Command):
                continue
            if cmd.callback.__name__ in SKIP:
                skipped += 1
                continue
            total += 1
            label = f"/{cmd.qualified_name}"

            async def call(as_choice: bool, _cmd=cmd, _cog=cog):
                args = {}
                for prm in _cmd.parameters:
                    v = fake_arg(prm, guild=guild, as_choice=as_choice)
                    # 省略できる引数は省略して、既定の道も通す
                    if v is not None or prm.required:
                        args[prm.name] = v
                caller = Member(1, "オーナー", guild=guild)
                it = FakeInteraction(caller, client)
                it.channel = ch
                it.channel_id = ch.id
                it.guild = guild
                it.guild_id = guild.id
                # ⚠️ 外と通信しようとするコマンドがある（経路の試験など）。
                #    時間を切らないと、1つのコマンドで全体が止まる。
                await asyncio.wait_for(_cmd.callback(_cog, it, **args), timeout=20)
                return it

            try:
                it = await call(True)
            except asyncio.TimeoutError:
                if label in NETWORK:
                    network_slow += 1
                    continue
                crashed += 1
                problems.append((label, "20秒で終わらない（外と通信している可能性）"))
                continue
            except AttributeError as e:
                # ⚠️ 選択肢つきの引数は、Choice を受けるコマンドと文字列を
                #    受けるコマンドがある。装飾子からは区別できないので、
                #    Choice で駄目なら文字列で呼び直す。
                if "Choice" in str(e) or "'value'" in str(e):
                    try:
                        it = await call(False)
                    except Exception as e2:
                        crashed += 1
                        problems.append((label, f"{type(e2).__name__}: {e2}"))
                        continue
                else:
                    crashed += 1
                    problems.append((label, f"AttributeError: {e}"))
                    continue
            except Exception as e:
                crashed += 1
                problems.append((label, f"{type(e).__name__}: {e}"))
                continue
            if not it.actions:
                silent += 1
                problems.append((label, "応答していない（3秒で切れる）"))

    print(f"\n[ 呼んだコマンド {total} 個"
          f"（除外 {skipped} 個 / 外部通信で時間切れ {network_slow} 個）]")
    check("例外を出すコマンドが無い ★", crashed == 0, f"{crashed} 個")
    check("応答しないコマンドが無い ★", silent == 0, f"{silent} 個")
    if problems:
        print("\n  ── 問題のあったコマンド ──")
        for label, why in problems[:40]:
            print(f"   {label:<34} {why}")

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
