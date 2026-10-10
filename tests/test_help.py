"""コマンドの案内（/help）

⚠️ コマンドは 148 個ある。`/help` の役目は「やりたいことから探せる」
   ことで、**漏れがあれば役に立たない**。だからここで見るのは、
   見た目ではなく次の点。

   ① 一覧は**実際に登録されているコマンドから**作られているか
      （手書きの表を持つと、コマンドを足したとき必ずずれる）
   ② 仕分けに漏れが無いか（「その他」行きが残っていないか）
   ③ 権限で**中身が変わる**か。とくに、見せない分類のコマンドが
      「その他」から漏れないか
   ④ 細かく書いた指定が、大きな指定に食われていないか
   ⑤ Discord の上限（欄1024・説明4096・合計6000・選択肢25）に
      収まっているか
   ⑥ 案内に書いたコマンド名が**実在する**か
      （説明文を書き換えたときに、ここだけ古くなるのを防ぐ）
"""
import asyncio, importlib, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord import app_commands

from _fake_discord import FakeClient, FakeInteraction, FakeUser
from core import settings
from core.crypto import init_cipher
from db.session import close_db, init_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


def load_cogs(client):
    """本物の cog をすべて読み込む（一覧は実物から作られるため）"""
    cogs = {}
    for f in sorted(os.listdir("cogs")):
        if not f.endswith(".py") or f.startswith("_"):
            continue
        mod = importlib.import_module(f"cogs.{f[:-3]}")
        for nm in dir(mod):
            o = getattr(mod, nm)
            if (isinstance(o, type) and nm.endswith("Cog")
                    and o.__module__ == mod.__name__):
                cogs[nm] = o(client)
    client.cogs = cogs
    return cogs


def embed_size(e: discord.Embed) -> int:
    n = len(e.title or "") + len(e.description or "")
    for f in e.fields:
        n += len(f.name or "") + len(str(f.value) or "")
    return n


OWNER, ADMIN, USER = {"everyone", "admin", "owner"}, {"everyone", "admin"}, {"everyone"}


async def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/help.db")
    await settings.load_all()

    client = FakeClient()
    client.owner_ids = {1}
    load_cogs(client)
    import cogs.help_cmd as H
    cog = H.HelpCog(client)

    # ── ① 一覧は実物から作られているか ───────────────────────
    print("\n[1] 一覧の作り方")
    names = [q for q, _ in cog._all_commands()]
    check("登録コマンドを100個以上拾えている", len(names) > 100, f"{len(names)}個")
    check("実在するコマンドが入っている",
          "mcd add" in names and "config show" in names)
    check("/help 自身は一覧に出ない", "help" not in names)

    # 新しい cog を足したら、仕分け表に無くても案内に現れること
    class _NewCog(discord.ext.commands.Cog):
        grp = app_commands.Group(name="zzznew", description="あとから足した入口")
        @grp.command(name="thing", description="あとから足したコマンド")
        async def thing(self, interaction): ...
    client.cogs = dict(client.cogs, _NewCog=_NewCog())
    buckets, rest = cog._sorted_into_topics(OWNER)
    everywhere = [q for _, h in buckets for q, _ in h] + [q for q, _ in rest]
    check("表に無い新しいコマンドも案内から消えない",
          "zzznew thing" in everywhere)
    check("表に無いものは「その他」へ入る",
          "zzznew thing" in [q for q, _ in rest])
    client.cogs = {k: v for k, v in client.cogs.items() if k != "_NewCog"}

    # ── ② 仕分けに漏れが無いか ───────────────────────────────
    print("\n[2] 仕分けの漏れ")
    buckets, rest = cog._sorted_into_topics(OWNER)
    total = sum(len(h) for _, h in buckets) + len(rest)
    check("オーナーには全コマンドが見える", total == len(names),
          f"{total} / {len(names)}")
    check("「その他」行きが残っていない", not rest,
          f"残り: {[q for q, _ in rest]}")
    dup = [q for q, _ in sum((h for _, h in buckets), []) ]
    check("同じコマンドが2か所に出ない", len(dup) == len(set(dup)))

    # ── ③ 権限で中身が変わるか ──────────────────────────────
    print("\n[3] 権限ごとの見え方")
    MAINT = {"restart", "sync", "admin backup", "admin restore", "menu sync"}
    for label, levels, want in (("オーナー", OWNER, True),
                                ("管理者（オーナーではない）", ADMIN, False)):
        b, r = cog._sorted_into_topics(levels)
        seen = {q for _, h in b for q, _ in h} | {q for q, _ in r}
        has = bool(MAINT & seen)
        check(f"{label}に保守コマンドが{'見える' if want else '見えない'}",
              has == want, f"{sorted(MAINT & seen)}")
    b, r = cog._sorted_into_topics(USER)
    check("一般の利用者にはコマンドを出さない", not b and not r)

    # 実際にコマンドを呼んで確かめる
    general = FakeUser(999, "一般の人")
    i = FakeInteraction(general, client)
    await cog.help_cmd.callback(cog, i)
    e = i.last_embed()
    check("一般の人には応答が返る", e is not None)
    check("一般の人向けの画面にコマンドが並んでいない",
          e is not None and "`/" not in (e.description or ""))
    check("一般の人にはボタンを出さない", i.last_view() is None)

    owner = FakeUser(1, "オーナー")
    i = FakeInteraction(owner, client)
    await cog.help_cmd.callback(cog, i)
    check("オーナーには分類のボタンが出る", i.last_view() is not None)

    # ── ④ 細かい指定が大きい指定に食われないか ──────────────
    print("\n[4] 仕分けの優先順位（長く一致したほうが勝ち）")
    b, _ = cog._sorted_into_topics(OWNER)
    where = {q: t.key for t, h in b for q, _ in h}
    for q, want in (("mcd health", "trouble"), ("mcd history", "trouble"),
                    ("mcd add", "account"), ("admin audit", "trouble"),
                    ("admin ban", "server"), ("mod ban", "server"),
                    ("admin review", "order"), ("config campaign start", "growth"),
                    ("config show", "config"), ("menu sync", "maint")):
        check(f"/{q} は「{want}」に入る", where.get(q) == want,
              f"実際は {where.get(q)}")

    # ── ⑤ Discord の上限 ────────────────────────────────────
    print("\n[5] Discord の上限")
    for levels, label in ((OWNER, "オーナー"), (ADMIN, "管理者"), (USER, "一般")):
        screens = []
        if levels is USER:
            screens.append(("ご利用方法", cog._user_embed()))
        else:
            screens.append(("入口", cog._admin_embed(levels)))
            for t, _ in cog._sorted_into_topics(levels)[0]:
                screens.append((t.title, cog.topic_embed(t.key, levels)))
            screens.append(("取り違えやすい組", cog.topic_embed("_confuse", levels)))
            screens.append(("その他", cog.topic_embed("_other", levels)))
            screens.append(("検索(config)", cog._search_embed("config", levels)))
            screens.append(("検索(返金)", cog._search_embed("返金", levels)))
        bad = []
        for nm, e in screens:
            if len(e.description or "") > 4096: bad.append(f"{nm}:説明")
            if len(e.fields) > 25: bad.append(f"{nm}:欄数")
            if embed_size(e) > 6000: bad.append(f"{nm}:合計{embed_size(e)}")
            for f in e.fields:
                if len(str(f.value)) > 1024: bad.append(f"{nm}/{f.name}:{len(str(f.value))}")
                if len(f.name or "") > 256: bad.append(f"{nm}:欄名")
        check(f"{label}の全画面が上限に収まる（{len(screens)}画面）", not bad, str(bad))

    view = H.HelpView(1, cog, OWNER)
    opts = view._sel.options
    check("選択肢が25個以内", len(opts) <= 25, f"{len(opts)}個")
    check("選択肢の値が重複していない", len({o.value for o in opts}) == len(opts))
    check("行数が5以内", len(view.children) <= 5)

    # ── ⑥ 案内に書いた名前が実在するか ──────────────────────
    print("\n[6] 案内に書いたコマンド名")
    exists = set(names) | {"help"}
    def resolve(token: str) -> list[str]:
        """「/kyash add か /paypay add」のような書き方もほどく"""
        return [p.strip().lstrip("/") for p in token.split(" か ")]
    missing = [c for cmd, _ in H.FIRST_STEPS for c in resolve(cmd)
               if c not in exists]
    check("はじめての設定のコマンドが全部実在する", not missing, str(missing))
    missing = [c.lstrip("/") for a, _, b, _ in H.CONFUSING for c in (a, b)
               if c.lstrip("/") not in exists]
    check("取り違えやすい組のコマンドが全部実在する", not missing, str(missing))

    # 取り違えやすい組は、説明が**互いに違う**ことに意味がある
    sames = [a for a, aw, b, bw in H.CONFUSING if aw == bw]
    check("取り違えやすい組の説明が対になっている", not sames, str(sames))
    descs = {q: d for q, d in cog._all_commands()}
    flat = [c.lstrip("/") for a, _, b, _ in H.CONFUSING for c in (a, b)]
    pairs = [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]
    same_desc = [p for p in pairs
                 if descs.get(p[0]) and descs.get(p[0]) == descs.get(p[1])]
    check("似た名前のコマンド自体の説明文も違う", not same_desc, str(same_desc))

    # ── ⑦ 検索 ─────────────────────────────────────────────
    print("\n[7] 言葉で探す")
    e = cog._search_embed("返金", OWNER)
    txt = " ".join(str(f.value) for f in e.fields)
    check("「返金」で2つのコマンドが出る",
          "/admin refund" in txt and "/config refund" in txt)
    check("どちらがお金を動かすか分かる", "お金が動きます" in txt)
    e = cog._search_embed("存在しない言葉zz", OWNER)
    check("見つからないときも案内が出る",
          "見つかりませんでした" in (e.title or ""))
    e = cog._search_embed("restart", ADMIN)
    check("権限の無いコマンドは検索にも出ない",
          "見つかりませんでした" in (e.title or ""))
    e = cog._search_embed("RESTART", OWNER)
    check("大文字小文字を区別しない", "restart" in " ".join(
        str(f.value) for f in e.fields))

    i = FakeInteraction(owner, client)
    await cog.help_cmd.callback(cog, i, search="カード")
    check("検索つきで呼んでも応答する", i.last_embed() is not None)

    # ── ⑧ ボタンの動き ─────────────────────────────────────
    print("\n[8] ボタンの動き")
    view = H.HelpView(1, cog, OWNER)
    for o in view._sel.options:
        view._sel._values = [o.value]
        i2 = FakeInteraction(owner, client)
        await view._pick(i2)
        got = i2.last_embed()
        if got is None:
            check(f"「{o.label}」を選べる", False, "応答が無い")
            break
    else:
        check(f"全ての分類を選べる（{len(view._sel.options)}個）", True)
    check("他人はボタンを押せない",
          not await view.interaction_check(FakeInteraction(FakeUser(2), client)))
    check("本人はボタンを押せる",
          await view.interaction_check(FakeInteraction(owner, client)))
    # ⚠️ 何も選ばれずに届いても落ちないこと
    view._sel._values = []
    i3 = FakeInteraction(owner, client)
    await view._pick(i3)
    check("選択が空で届いても落ちない", i3.last_embed() is not None)
    view._sel._values = ["存在しない分類"]
    i4 = FakeInteraction(owner, client)
    await view._pick(i4)
    check("知らない分類が届いても落ちない", i4.last_embed() is not None)

    await close_db()
    print(f"\n成功 {ok} / 失敗 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    import discord.ext.commands  # noqa: F401  (テスト内で Cog を作るため)
    sys.exit(asyncio.run(main()))
