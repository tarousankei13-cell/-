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
from services.mcd import accounts as A2

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

    # ============================================================
    print("\n[6] プロキシの設定 ★")
    # ⚠️ McdAccount.proxy_url は読まれているのに、設定する手段が
    #    無かった。全アカウントが同じ出口から出てしまっていた。
    import core.proxy as CP

    RESULT = [(True, "203.0.113.9", "JP")]
    asked: list[str] = []

    async def fake_check(url=None, *, timeout=15.0):
        asked.append(url)
        return RESULT[0]

    real_check = CP.check_exit
    CP.check_exit = fake_check

    async def proxy_of(i: int):
        async with session_scope() as s:
            a = await s.get(McdAccount, i)
            return a.proxy_url if a else None

    # socks は断る（追加の部品が要るうえ Discord で使えない）
    itx8 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(cog, itx8, url="socks5://u:p@1.2.3.4:1080")
    check("socks は断る ★", "socks" in itx8.text(), itx8.text()[:160])
    check("socks のときは確かめに行かない ★", not asked, asked)
    check("socks では設定されない ★", await proxy_of(1) is None)

    # 通らないプロキシは設定しない
    RESULT[0] = (False, "ProxyError: 400 Bad Request", "")
    itx9 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(cog, itx9, url="http://1.2.3.4")
    check("通らなければ設定しない ★", await proxy_of(1) is None)
    check("通らない理由を見せる ★", "通りませんでした" in itx9.text(),
          itx9.text()[:200])
    check("次にやることを案内する ★", "ポート番号" in itx9.text(),
          itx9.text()[:400])

    # 海外の出口は断る
    RESULT[0] = (True, "198.51.100.7", "US")
    itx10 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(cog, itx10, url="http://1.2.3.4:8080")
    check("海外の出口は断る ★", await proxy_of(1) is None)
    check("日本の外だと伝える ★", "日本の外" in itx10.text(), itx10.text()[:200])
    check("通らなくなると伝える ★", "通りません" in itx10.text(),
          itx10.text()[:300])

    # force なら海外でも入れる
    itx11 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(
        cog, itx11, url="http://1.2.3.4:8080", account_id=1, force=True)
    check("force なら設定できる ★",
          await proxy_of(1) == "http://1.2.3.4:8080", await proxy_of(1))
    check("force では確かめに行かない ★", len(asked) == 2, len(asked))

    # 日本なら、そのまま全件に設定
    RESULT[0] = (True, "203.0.113.9", "JP")
    itx12 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(
        cog, itx12, url="http://user:secret@5.6.7.8:3128")
    got_all = [await proxy_of(i) for i in (1, 2, 3)]
    check("全件に設定できる ★",
          all(g == "http://user:secret@5.6.7.8:3128" for g in got_all), got_all)
    check("出口のIPを見せる ★", "203.0.113.9" in itx12.text(), itx12.text()[:300])
    check("日本だと伝える ★", "日本" in itx12.text(), itx12.text()[:300])
    # ⚠️ ここが漏れると、画面共有や配信でプロキシの資格情報が流出する
    check("パスワードを画面に出さない ★", "secret" not in itx12.text(),
          itx12.text()[:300])

    # 1件だけ解除
    itx13 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(cog, itx13, url="clear", account_id=2)
    check("1件だけ解除できる ★", await proxy_of(2) is None, await proxy_of(2))
    check("他は残る ★", await proxy_of(3) is not None, await proxy_of(3))
    check("解除では確かめに行かない ★", len(asked) == 3, len(asked))

    # 全件解除
    itx14 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(cog, itx14, url="解除")
    got_clear = [await proxy_of(i) for i in (1, 2, 3)]
    check("全件解除できる ★", all(g is None for g in got_clear), got_clear)
    check("解除したと伝える", "解除" in itx14.text(), itx14.text()[:160])

    # 見つからないID
    itx15 = FakeInteraction(owner, dclient)
    await cog.mcd_proxy.callback(
        cog, itx15, url="http://5.6.7.8:3128", account_id=999)
    check("見つからないIDは断る ★", "見つかりません" in itx15.text(),
          itx15.text()[:160])

    CP.check_exit = real_check

    # ============================================================
    print("\n[7] しきい値の設定 ★")
    # ⚠️ /config 側はサブコマンドの上限(25)に達しているので、
    #    アカウントの設定は /mcd settings に置いてある。
    itx16 = FakeInteraction(owner, dclient)
    await cog.mcd_settings.callback(cog, itx16)
    check("何も指定しなければ今の値を見せる ★", "いまの設定" in itx16.text(),
          itx16.text()[:200])
    check("書き換えない ★", int(settings.get("mcd_low_accounts")) == 2,
          settings.get("mcd_low_accounts"))

    itx17 = FakeInteraction(owner, dclient)
    await cog.mcd_settings.callback(cog, itx17, low=4, expiry_days=10, trash_days=7)
    check("3つとも設定できる ★",
          (int(settings.get("mcd_low_accounts")) == 4
           and int(settings.get("mcd_card_expiry_days")) == 10
           and int(settings.get("mcd_trash_days")) == 7),
          [settings.get(k) for k in
           ("mcd_low_accounts", "mcd_card_expiry_days", "mcd_trash_days")])
    check("設定したと伝える", "設定しました" in itx17.text(), itx17.text()[:200])

    # 1つだけ変えても他は残る
    itx18 = FakeInteraction(owner, dclient)
    await cog.mcd_settings.callback(cog, itx18, low=3)
    check("1つだけ変えられる ★",
          int(settings.get("mcd_low_accounts")) == 3
          and int(settings.get("mcd_trash_days")) == 7,
          [settings.get("mcd_low_accounts"), settings.get("mcd_trash_days")])

    # あり得ない値は断る
    for kw, why in (({"low": 0}, "1以上"), ({"expiry_days": 400}, "0〜365"),
                    ({"trash_days": 0}, "1〜365")):
        itxX = FakeInteraction(owner, dclient)
        await cog.mcd_settings.callback(cog, itxX, **kw)
        check(f"あり得ない値を断る（{list(kw)[0]}）★", why in itxX.text(),
              itxX.text()[:160])
    check("断ったときは書き換えない ★",
          int(settings.get("mcd_low_accounts")) == 3,
          settings.get("mcd_low_accounts"))

    # 設定が実際に効く
    await settings.set_value("mcd_trash_days", 7)
    check("ゴミ箱の日数が効く ★", A2.trash_days() == 7, A2.trash_days())
    await settings.set_value("mcd_low_accounts", 2)
    await settings.set_value("mcd_card_expiry_days", 45)
    await settings.set_value("mcd_trash_days", 30)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
