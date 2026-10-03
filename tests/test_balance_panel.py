"""
残高の増減パネルの検証

確かめること:
  - 使ったチャンネルに、誰にでも見える形（ephemeral でない）で出る
  - 既定では残高そのものを出さない（プライバシー）
  - 表示項目の設定が効く
  - DM・権限不足・設定OFF では静かに何もしない（本処理を止めない）
  - チャージは「＋」、注文は「−」で出る
"""
import asyncio, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from core import settings, users as user_repo
from ui import balance_panel, embeds
from ui import flows as flows_mod
import config

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class FakePerms:
    def __init__(self, send=True, embed=True):
        self.send_messages = send
        self.embed_links = embed


class FakeChannel:
    def __init__(self, cid=100, name="general", perms=None):
        self.id = cid
        self.name = name
        self.sent = []
        self._perms = perms or FakePerms()
    def permissions_for(self, _member):
        return self._perms
    async def send(self, **kw):
        self.sent.append(kw)


class ForbiddenChannel(FakeChannel):
    async def send(self, **kw):
        raise discord.Forbidden(_Resp(403), "no")


class _Resp:
    def __init__(self, status): self.status = status
    reason = "Forbidden"


class FakeGuild:
    def __init__(self): self.me = object()


class FakeUser:
    def __init__(self, uid, name="テスト太郎"):
        self.id = uid
        self.display_name = name


class FakeClient:
    def __init__(self, channels=None): self.channels = channels or {}
    def get_channel(self, cid): return self.channels.get(int(cid))


_KEEP = object()   # guild=None を「サーバー外」として扱えるようにする番人


class FakeInteraction:
    def __init__(self, uid, channel, guild=_KEEP, client=None):
        self.user = FakeUser(uid)
        self.channel = channel
        self.guild = FakeGuild() if guild is _KEEP else guild
        self.client = client or FakeClient()


def dump(e: discord.Embed) -> dict:
    return {
        "title": e.title,
        "color": e.color.value if e.color else None,
        "fields": {f.name: f.value for f in e.fields},
    }


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/bp.db")
    await settings.load_all()

    UID = 777001
    await user_repo.get_or_create(UID)
    async with session_scope() as s:
        from db.models import User
        anon = (await s.get(User, UID)).anon_code

    print("\n[1] 既定の設定で、使ったチャンネルに出る")
    ch = FakeChannel()
    it = FakeInteraction(UID, ch)
    sent = await balance_panel.post(
        it, amount=3000, balance=5000, reason="管理者による調整：チャージ分"
    )
    check("投稿された", sent is True)
    check("使ったチャンネルに出た", len(ch.sent) == 1, ch.sent)
    kw = ch.sent[0]
    check("ephemeral を付けていない（誰にでも見える）",
          "ephemeral" not in kw or kw.get("ephemeral") is not True, kw)
    d = dump(kw["embed"])
    check("プラスだと分かる", "チャージ" in (d["title"] or ""), d["title"])
    check("増減額が出ている", any("+¥3,000" in v for v in d["fields"].values()), d)

    print("\n[2] 既定では残高そのものを出さない（プライバシー）")
    check("既定の項目に balance が無い",
          "balance" not in config.BALANCE_PANEL_FIELDS_DEFAULT,
          config.BALANCE_PANEL_FIELDS_DEFAULT)
    check("¥5,000（残高）がどこにも出ていない",
          all("¥5,000" not in v for v in d["fields"].values()), d)
    check("残高の見出しが無い", not any("残高" in k for k in d["fields"]), d)

    print("\n[3] 表示名を出す/出さないが設定で切り替わる")
    check("既定では表示名が出る",
          any("テスト太郎" in v for v in d["fields"].values()), d)
    await settings.set_value("balance_panel_fields", ["amount", "reason"])
    ch2 = FakeChannel()
    await balance_panel.post(
        FakeInteraction(UID, ch2), amount=3000, balance=5000, reason="x"
    )
    d2 = dump(ch2.sent[0]["embed"])
    check("name を外すと表示名が消える",
          all("テスト太郎" not in v for v in d2["fields"].values()), d2)
    check("代わりに匿名コードが出る",
          any(anon in v for v in d2["fields"].values()), (anon, d2))

    print("\n[4] balance を入れると残高が出る（管理者が明示したときだけ）")
    await settings.set_value("balance_panel_fields", ["name", "amount", "balance", "reason"])
    ch3 = FakeChannel()
    await balance_panel.post(
        FakeInteraction(UID, ch3), amount=3000, balance=5000, reason="x"
    )
    d3 = dump(ch3.sent[0]["embed"])
    check("残高が出る", any("¥5,000" in v for v in d3["fields"].values()), d3)

    print("\n[5] 注文は減少として出る")
    await settings.set_value("balance_panel_fields", config.BALANCE_PANEL_FIELDS_DEFAULT)
    ch4 = FakeChannel()
    await balance_panel.post(
        FakeInteraction(UID, ch4), amount=-480, balance=4520,
        reason="管理者による調整：返金", total_orders=12,
    )
    d4 = dump(ch4.sent[0]["embed"])
    check("マイナスだと分かる", "注文" in (d4["title"] or ""), d4["title"])
    check("マイナス表記", any("-¥480" in v for v in d4["fields"].values()), d4)
    check("色がチャージと違う", d4["color"] != d["color"], (d4["color"], d["color"]))
    check("理由が内容に入る", any("返金" in v for v in d4["fields"].values()), d4)

    print("\n[6] orders は既定では出ない / 設定すれば出る")
    check("既定では利用回数が出ない",
          not any("12" in v and "回" in v for v in d4["fields"].values()), d4)
    await settings.set_value(
        "balance_panel_fields", ["name", "amount", "reason", "orders"]
    )
    ch5 = FakeChannel()
    await balance_panel.post(
        FakeInteraction(UID, ch5), amount=-480, balance=1, reason="x", total_orders=12
    )
    d5 = dump(ch5.sent[0]["embed"])
    check("orders を入れると出る",
          any("12" in v for v in d5["fields"].values()), d5)
    await settings.set_value("balance_panel_fields", config.BALANCE_PANEL_FIELDS_DEFAULT)

    print("\n[7] 送り先の設定があればそちらへ出る")
    dest = FakeChannel(cid=555, name="balance-log")
    used = FakeChannel(cid=100)
    client = FakeClient({555: dest})
    await settings.set_value("channel_balance", 555)
    await balance_panel.post(
        FakeInteraction(UID, used, client=client), amount=100, balance=1, reason="x"
    )
    check("指定チャンネルに出た", len(dest.sent) == 1, dest.sent)
    check("操作したチャンネルには出ない", len(used.sent) == 0, used.sent)

    print("\n[8] 送り先が消えていたら操作したチャンネルへ落とす")
    used2 = FakeChannel(cid=100)
    await balance_panel.post(
        FakeInteraction(UID, used2, client=FakeClient({})), amount=100, balance=1, reason="x"
    )
    check("消えた送り先の代わりにその場へ出た", len(used2.sent) == 1, used2.sent)
    await settings.set_value("channel_balance", None)

    print("\n[9] 出さない設定・DM・権限不足では静かに何もしない")
    await settings.set_value("balance_panel", False)
    chx = FakeChannel()
    r = await balance_panel.post(FakeInteraction(UID, chx), amount=100, balance=1, reason="x")
    check("OFF なら出さない", r is False and not chx.sent)
    await settings.set_value("balance_panel", True)

    # 本物の DMChannel 型で判定されることを確かめる（中身は使わない）
    dm = object.__new__(discord.DMChannel)
    r = await balance_panel.post(
        FakeInteraction(UID, dm, guild=None), amount=100, balance=1, reason="x"
    )
    check("DM では出さない", r is False)

    gdm = object.__new__(discord.GroupChannel)
    r = await balance_panel.post(
        FakeInteraction(UID, gdm, guild=None), amount=100, balance=1, reason="x"
    )
    check("グループDM でも出さない", r is False)

    r = await balance_panel.post(
        FakeInteraction(UID, None, guild=None), amount=100, balance=1, reason="x"
    )
    check("チャンネルが取れないときも出さない", r is False)

    it_noguild = FakeInteraction(UID, FakeChannel(), guild=None)
    r = await balance_panel.post(it_noguild, amount=100, balance=1, reason="x")
    check("サーバー外なら出さない", r is False and not it_noguild.channel.sent)

    noperm = FakeChannel(perms=FakePerms(send=False))
    r = await balance_panel.post(FakeInteraction(UID, noperm), amount=100, balance=1, reason="x")
    check("送信権限が無ければ出さない", r is False and not noperm.sent)

    noembed = FakeChannel(perms=FakePerms(send=True, embed=False))
    r = await balance_panel.post(FakeInteraction(UID, noembed), amount=100, balance=1, reason="x")
    check("埋め込み権限が無ければ出さない", r is False and not noembed.sent)

    forb = ForbiddenChannel()
    r = await balance_panel.post(FakeInteraction(UID, forb), amount=100, balance=1, reason="x")
    check("Forbidden でも例外を出さない", r is False)

    print("\n[10] 増減ゼロでは出さない（意味が無いので黙る）")
    chz = FakeChannel()
    r = await balance_panel.post(FakeInteraction(UID, chz), amount=0, balance=1, reason="x")
    check("0円なら出さない", r is False and not chz.sent)

    print("\n[11] 他人の分（管理者調整）も出せる")
    OTHER = 777002
    await user_repo.get_or_create(OTHER)
    cha = FakeChannel()
    await balance_panel.post(
        FakeInteraction(UID, cha),
        amount=-1000, balance=0,
        reason=f"{balance_panel.REASON_GRANT}：返金",
        display_name="対象の人", discord_id=OTHER,
    )
    da = dump(cha.sent[0]["embed"])
    check("対象の人の名前で出る",
          any("対象の人" in v for v in da["fields"].values()), da)
    check("操作した管理者の名前は出ない",
          all("テスト太郎" not in v for v in da["fields"].values()), da)

    print("\n[12] 登録前の利用者でも匿名コードで出せる（落ちない）")
    chn = FakeChannel()
    r = await balance_panel.post(
        FakeInteraction(999999, chn), amount=500, balance=500, reason="x"
    )
    check("投稿できる", r is True, chn.sent)

    print("\n[13] 設定の既定が安全側か")
    check("パネルは既定で有効（利用者の要望）", settings.DEFAULTS["balance_panel"] is True)
    check("送り先は既定で未設定＝操作したチャンネル",
          settings.DEFAULTS["channel_balance"] is None)

    print("\n[14] チャージでは出さない ★")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _fake_discord import (
        FakeClient as FakeBot,
        FakeInteraction as RealishInteraction,
        FakeUser as RealishUser,
    )
    import services.kyash.charge as kyash_charge

    class Result:
        amount = 3000          # 実際に送金された額
        credited = 3000        # 残高に入れた額（チャージ率を掛けたあと）
        rate = 100
        bonus = 0
        balance = 8000
        sender_name = "送った人"

    async def fake_charge(uid, link):
        return Result()

    kyash_charge.charge_from_link = fake_charge
    kyash_charge.ChargeError = type("ChargeError", (Exception,), {})

    cu = RealishUser(UID)
    itc = RealishInteraction(cu, FakeBot())
    modal = flows_mod.ChargeModal()
    modal.link._value = "https://kyash.me/payments/aaa"
    await modal.on_submit(itc)

    check("チャージでは公開パネルを出さない ★", len(itc.public_embeds) == 0,
          str(itc.public_embeds)[:120])
    check("本人向けの完了案内は出る",
          any(k == "followup" for k, _ in itc.actions), itc.actions)
    check("本人にしか見えない ★",
          all(kw.get("ephemeral") for k, kw in itc.actions if k == "followup"),
          itc.actions)

    print("\n[15] 出すのは管理者の増減だけ ★")
    import inspect
    from ui import flows as F
    src = inspect.getsource(F)
    check("注文の経路から呼んでいない ★", "balance_panel" not in src, "まだ残っている")
    from cogs import admin as A
    check("/admin grant からは呼んでいる ★",
          "balance_panel.post" in inspect.getsource(A))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
