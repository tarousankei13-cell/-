"""BOTの貸し出し

⚠️ ここで守りたいのは2つ。

   ① **貸していないサーバーでは、何も動かない。**
      マクドナルドのアカウントとカードはこちら持ちなので、
      勝手に招待されたサーバーで注文されると、そのまま損害になる。

   ② **持ち主が自分を締め出さない。**
      ホームが未設定だと `/lend` すら打てず詰む。
"""
import asyncio, os, sys, tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord

from core import license as lic
from core.crypto import init_cipher
from db.models import GuildLicense, utcnow
from db.session import close_db, init_db, session_scope

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def _expire(guild_id, days_ago=1):
    async with session_scope() as s:
        r = await s.get(GuildLicense, guild_id)
        r.expires_at = utcnow() - timedelta(days=days_ago)


async def main():
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/lend.db")

    print("\n[1] 貸していないサーバーでは使えない ★")
    st = await lic.status(999)
    check("使えない ★", st.allowed is False)
    check("理由は『貸していない』", st.reason == lic.NONE)
    check("理由を文章で説明する", "まだ使えません" in st.message(), st.message())
    # ⚠️ DM（サーバーの外）も使えない扱い
    check("DM でも使えない ★", (await lic.status(None)).allowed is False)

    print("\n[2] ホームは止まらない ★")
    await lic.ensure_home(1)
    st = await lic.status(1)
    check("ホームは使える ★", st.allowed is True)
    check("ホームに期限は無い ★", st.expires_at is None)
    check("残り日数も無い", st.days_left is None)
    check("2度目の ensure_home は何もしない", (await lic.ensure_home(2)) is False)
    check("ホームは1つのまま", (await lic.home_guild_id()) == 1)

    print("\n[3] 貸す・延ばす")
    st = await lic.grant(10, 7, guild_name="お試し")
    check("7日貸せる", st.allowed and st.days_left == 7, st.days_left)
    st = await lic.extend(10, 30)
    check("30日延ばせる", st.days_left == 37, st.days_left)
    try:
        await lic.extend(10, 0); check("0日は断る ★", False)
    except lic.LicenseError: check("0日は断る ★", True)
    try:
        await lic.extend(10, 99999); check("長すぎる日数は断る ★", False)
    except lic.LicenseError: check("長すぎる日数は断る ★", True)
    try:
        await lic.extend(777, 7); check("貸していない先は延ばせない ★", False)
    except lic.LicenseError: check("貸していない先は延ばせない ★", True)

    print("\n[4] 期限が切れたら止まる ★")
    await _expire(10)
    st = await lic.status(10)
    check("切れたら使えない ★", st.allowed is False)
    check("理由は『期限切れ』", st.reason == lic.EXPIRED)
    check("期限を文章に入れる", "期限" in st.message())

    # ⚠️ 切れたあとの延長は「今から」数える。過去に足すと切れたまま。
    st = await lic.extend(10, 3)
    check("切れたあとは今から数える ★", st.allowed and st.days_left == 3, st.days_left)

    print("\n[5] 手で止める・戻す")
    await lic.revoke(10, reason="規約違反")
    st = await lic.status(10)
    check("止められる", st.allowed is False and st.reason == lic.SUSPENDED)
    check("理由が見える", "規約違反" in st.message())
    check("期限は消えない ★", st.expires_at is not None)
    await lic.resume(10)
    check("戻せる", (await lic.status(10)).allowed is True)
    try:
        await lic.revoke(1); check("ホームは止められない ★", False)
    except lic.LicenseError: check("ホームは止められない ★", True)

    print("\n[6] grant は置き換え、extend は足し算 ★")
    await lic.grant(20, 100)
    await lic.grant(20, 5)       # 置き換え
    check("grant は期限を置き換える ★", (await lic.status(20)).days_left == 5)
    await lic.extend(20, 5)
    check("extend は足す ★", (await lic.status(20)).days_left == 10)

    print("\n[7] ホームの付け替え ★")
    await lic.set_home(30)
    check("新しいホームになる", (await lic.home_guild_id()) == 30)
    st1 = await lic.status(1)
    # ⚠️ 元ホームを期限なしのまま降格させると、黙って止まる
    check("前のホームは止まらない ★", st1.allowed is True, st1.reason)
    check("前のホームに猶予が入る ★", st1.days_left is not None and st1.days_left > 20,
          st1.days_left)
    await lic.set_home(1)        # 戻す

    print("\n[8] 予告は1回ずつ ★")
    await lic.grant(40, 2)       # 残り2日 → 「3」の予告
    st = await lic.status(40)
    tag = lic.due_notice(st)
    check("残り2日なら『3日前』の予告 ★", tag == "3", tag)
    check("1度目は送る", (await lic.mark_notified(40, tag)) is True)
    check("2度目は送らない ★", (await lic.mark_notified(40, tag)) is False)
    # ⚠️ 延長したら予告をやり直す。やり直さないと次の期間で一度も出ない。
    await lic.extend(40, 1)
    check("延長すると予告がやり直される ★",
          (await lic.mark_notified(40, "3")) is True)
    await _expire(40)
    check("切れたら『0』の予告", lic.due_notice(await lic.status(40)) == "0")
    check("ホームには予告を出さない ★", lic.due_notice(await lic.status(1)) is None)

    print("\n[9] 関所 — 画面がすべて守られているか ★")
    # ⚠️ discord.ui.View を直に継承すると関所を通らない。
    #    1つでも漏れると、そこだけ無料で使われる。
    import ast
    import ui.gate as gate

    naked, overridden, guarded = [], [], 0
    for root in ("ui", "cogs"):
        for f in sorted(os.listdir(root)):
            if not f.endswith(".py"):
                continue
            path = f"{root}/{f}"
            for n in ast.walk(ast.parse(open(path).read())):
                if not isinstance(n, ast.ClassDef):
                    continue
                bases = [ast.unparse(b) for b in n.bases]
                if not any("View" in b for b in bases):
                    continue
                if path == "ui/gate.py":
                    continue
                if bases[0] not in ("GuardedView", "OwnerOnlyView"):
                    naked.append(f"{path}:{n.name}({bases[0]})")
                    continue
                guarded += 1
                if any(isinstance(m, ast.AsyncFunctionDef)
                       and m.name == "interaction_check" for m in n.body):
                    overridden.append(f"{path}:{n.name}")
    check(f"すべての画面が関所を通る（{guarded}個）★", not naked, naked[:4])
    check("関所を上書きしている画面が無い ★", not overridden, overridden[:4])

    print("\n[10] 関所が実際に止めるか ★")
    from _fake_discord import FakeClient, FakeInteraction, FakeUser

    it = FakeInteraction(FakeUser(5))
    it.guild_id = 999                     # 貸していないサーバー
    allowed = await gate.check(it)
    check("貸していないサーバーで止まる ★", allowed is False)
    # ⚠️ 必ず応答する。黙って止めると「応答しませんでした」になる。
    check("必ず応答する ★", bool(it.actions))
    check("理由が利用者に見える ★", "使えません" in it.text() or "まだ使えません" in it.text(),
          it.text()[:80])

    it = FakeInteraction(FakeUser(5))
    it.guild_id = 1                       # ホーム
    check("ホームでは通る ★", (await gate.check(it)) is True)

    print("\n[11] 貸し出しコマンドは関所を素通りする ★")
    # ⚠️ 素通りしないと、ホーム未設定のとき `/lend home` が打てず詰む。
    tree = gate.LicensedTree.__new__(gate.LicensedTree)
    class _C:
        qualified_name = "lend grant"
    check("/lend は素通り ★", tree._is_always(_C()) is True)
    class _C2:
        qualified_name = "order panel"
    check("それ以外は素通りしない ★", tree._is_always(_C2()) is False)

    print("\n[12] コマンドが落ちずに応答するか")
    import cogs.lend as lend_cog
    client = FakeClient()
    client.guilds = []
    client.get_guild = lambda gid: None
    cog = lend_cog.LendCog(client)

    async def run(label, coro_fn, **kw):
        it = FakeInteraction(FakeUser(1, "オーナー"))
        it.guild_id = 1
        try:
            await coro_fn(cog, it, **kw)
        except Exception as e:
            check(label, False, f"{type(e).__name__}: {e}")
            return None
        check(label, bool(it.actions), "応答していない")
        return it

    await run("/lend list", lend_cog.LendCog.lend_list.callback)
    await run("/lend status", lend_cog.LendCog.lend_status.callback)
    await run("/lend grant", lend_cog.LendCog.lend_grant.callback,
              days=7, guild_id="50")
    await run("/lend extend", lend_cog.LendCog.lend_extend.callback,
              days=3, guild_id="50")
    await run("/lend revoke", lend_cog.LendCog.lend_revoke.callback,
              guild_id="50", reason="テスト")
    await run("/lend resume", lend_cog.LendCog.lend_resume.callback, guild_id="50")
    await run("/lend home", lend_cog.LendCog.lend_home.callback)
    it = await run("/lend grant（数字でないID）", lend_cog.LendCog.lend_grant.callback,
                   days=7, guild_id="あいう")
    if it:
        check("数字でないIDを断る ★", "数字" in it.text(), it.text()[:60])

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
