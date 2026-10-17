"""アカウント管理の道具 — CSV出力・表示名の一括付け替え

⚠️ 次を固定する。
   ① CSVに**パスワードは入らない**（保存していない）
   ② Excel で開いても日本語が化けない（BOM付き）
   ③ 付け替えは、見つからないIDと読めない行を必ず知らせる
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select

from _fake_discord import FakeClient, FakeInteraction, FakeUser
from core import settings
from core.crypto import get_cipher, init_cipher
from db.models import McdAccount
from db.session import close_db, init_db, session_scope

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class FakeValue:
    def __init__(self, v): self.value = v
    def __str__(self): return self.value


async def seed(n: int) -> None:
    cipher = get_cipher()
    async with session_scope() as s:
        for i in range(1, n + 1):
            s.add(McdAccount(
                id=i, label=f"acc{i}",
                email_enc=cipher.encrypt(f"user{i}@example.com"),
                refresh_token_enc=cipher.encrypt("r"),
                device_uid=f"d{i}", wmop_device_id=f"w{i}",
                fb_instance_id=f"f{i}",
                home_lat=35.0, home_lng=139.0, status="ACTIVE",
            ))


async def main() -> int:
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/tools.db")
    await settings.load_all()

    import cogs.account as acc
    from cogs.account import AccountCog

    owner = FakeUser(1, "オーナー")
    dclient = FakeClient()
    cog = AccountCog(dclient)

    # ============================================================
    print("\n[1] 0件のとき")
    itx = FakeInteraction(owner, dclient)
    await cog.mcd_export.callback(cog, itx)
    check("0件なら案内する", "ありません" in itx.text(), itx.text()[:120])

    await seed(3)

    # ============================================================
    print("\n[2] CSV出力 ★")
    data, n = await acc._export_csv()
    check("件数を返す", n == 3, n)
    # ⚠️ BOM が無いと Excel で日本語が化ける
    check("BOM付きで出す（Excel対策）★", data.startswith(b"\xef\xbb\xbf"),
          data[:6])
    text = data.decode("utf-8-sig")
    head = text.splitlines()[0]
    check("見出しがある", "表示名" in head and "メール" in head, head)
    check("3件ぶんの行がある", len(text.strip().splitlines()) == 4,
          len(text.strip().splitlines()))
    check("メールが入る", "user2@example.com" in text)
    check("状態が入る", "ACTIVE" in text)
    # ⚠️ パスワードは保存していないので、出てはいけない
    check("パスワードの列が無い ★",
          "パスワード" not in head and "password" not in head.lower(), head)

    itx2 = FakeInteraction(owner, dclient)
    await cog.mcd_export.callback(cog, itx2)
    files = [kw.get("file") for _, kw in itx2.actions if kw.get("file")]
    check("ファイルを渡す ★", bool(files), len(files))
    check("パスワードが無いことを伝える ★",
          "パスワードは保存していない" in itx2.text(), itx2.text()[:300])
    check("メールの扱いに注意と伝える ★", "ご注意" in itx2.text(),
          itx2.text()[:300])

    # ============================================================
    print("\n[3] 名前＋連番でそろえる ★")
    itx3 = FakeInteraction(owner, dclient)
    await cog.mcd_relabel.callback(cog, itx3, prefix="メイン")
    async with session_scope() as s:
        rows = (await s.execute(
            select(McdAccount).order_by(McdAccount.id))).scalars().all()
        got = [(a.id, a.label) for a in rows]
    check("全件そろう ★", got == [(1, "メイン1"), (2, "メイン2"), (3, "メイン3")], got)
    check("件数を伝える", "3件" in itx3.text(), itx3.text()[:160])

    # ============================================================
    print("\n[4] IDを指定して付け替える ★")
    m = acc.McdRelabelModal()
    m.lines = FakeValue("1,朝用\n3:夜用\n9,居ないID\nこわれた行\n")
    itx4 = FakeInteraction(owner, dclient)
    await m.on_submit(itx4)
    async with session_scope() as s:
        rows = (await s.execute(
            select(McdAccount).order_by(McdAccount.id))).scalars().all()
        got = {a.id: a.label for a in rows}
    check("指定した分だけ変わる ★",
          got == {1: "朝用", 2: "メイン2", 3: "夜用"}, got)
    check("見つからないIDを知らせる ★", "見つからないID" in itx4.text(),
          itx4.text()[:300])
    check("読めない行を知らせる ★", "読めなかった行" in itx4.text(),
          itx4.text()[:300])

    # ============================================================
    print("\n[5] 読み取りの細かい作法")
    want, bad = acc._parse_relabel("1,あ\n# めも\n\n2 い\n3=う\nxx,え\n")
    check("区切りはカンマ・スペース・イコールどれでも",
          want == {1: "あ", 2: "い", 3: "う"}, want)
    check("#と空行は読めない行に数えない", len(bad) == 1, bad)
    check("数字でないIDは断る ★", bad == [(6, "xx,え")], bad)
    want, _ = acc._parse_relabel("1," + "あ" * 50)
    check("長すぎる名前は切る ★", len(want[1]) == 32, len(want[1]))

    itx5 = FakeInteraction(owner, dclient)
    m2 = acc.McdRelabelModal()
    m2.lines = FakeValue("ぜんぶこわれ\n")
    await m2.on_submit(itx5)
    check("1行も読めなければ断る ★", "読み取れる行が" in itx5.text(),
          itx5.text()[:160])

    itx6 = FakeInteraction(owner, dclient)
    await cog.mcd_relabel.callback(cog, itx6, prefix="   ")
    check("空白だけの名前は断る ★", "名前を入れて" in itx6.text(),
          itx6.text()[:160])

    itx7 = FakeInteraction(owner, dclient)
    await cog.mcd_relabel.callback(cog, itx7)
    check("名前を付けなければ貼り付け窓 ★",
          isinstance(itx7.last_modal(), acc.McdRelabelModal),
          type(itx7.last_modal()).__name__)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
