"""アカウントの在庫ぎれと、カードの期限・未設定の知らせ

⚠️ 次を固定する。
   ① 使えるのは ACTIVE / DEGRADED だけ（隔離・凍結を数えない）
   ② カードは**その月の末日まで**使える（月初で切れたと言わない）
   ③ 同じ知らせを毎時間繰り返さない。片付いたら、また知らせられる
   ④ カード未設定のアカウントは注文に選ばれないので別に挙げる
"""
import asyncio, os, sys, tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fake_discord import FakeClient
from core import settings
from core.crypto import get_cipher, init_cipher
from db.models import McdAccount
from db.session import close_db, init_db, session_scope
from services.mcd import accounts as A

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

TODAY = date(2026, 10, 17)


async def wipe():
    from sqlalchemy import text
    async with session_scope() as s:
        await s.execute(text("DELETE FROM mcd_accounts"))


async def add(i, status="ACTIVE", card="c1", expiry="12/28"):
    cipher = get_cipher()
    async with session_scope() as s:
        s.add(McdAccount(
            id=i, label=f"acc{i}",
            email_enc=cipher.encrypt(f"u{i}@example.com"),
            refresh_token_enc=cipher.encrypt("r"),
            device_uid=f"d{i}", wmop_device_id=f"w{i}", fb_instance_id=f"f{i}",
            home_lat=35.0, home_lng=139.0, status=status,
            card_id=card, card_expiry=expiry,
        ))


async def main() -> int:
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/stock.db")
    await settings.load_all()

    # ============================================================
    print("\n[1] カードの期限の読み取り ★")
    f = A.card_expiry_days
    check("MM/YY を読む", f("12/28", TODAY) == 806, f("12/28", TODAY))
    check("MM/YYYY を読む", f("3/2027", TODAY) == 165, f("3/2027", TODAY))
    check("YYYY-MM も読む", f("2028-03", TODAY) == 531, f("2028-03", TODAY))
    # ⚠️ 月末まで有効。10/17 から見て 10/26 はまだ14日ある。
    #    月初を期限にすると「もう切れた」と誤って知らせてしまう。
    check("その月の末日まで有効 ★", f("10/26", TODAY) == 14, f("10/26", TODAY))
    check("過ぎた期限は負の数 ★", f("09/26", TODAY) == -17, f("09/26", TODAY))
    check("読めないものは None", f("xx", TODAY) is None)
    check("空も None", f("", TODAY) is None)
    check("13月は None ★", f("13/26", TODAY) is None, f("13/26", TODAY))
    # ⚠️ 2桁の "99" は 2099年。遠いだけで正しい期限なので None にしない。
    check("2桁の年は2000年代として読む ★", f("12/99", date(2026, 1, 1)) == 27027,
          f("12/99", date(2026, 1, 1)))
    check("あり得ない年は None ★", f("12/1800", TODAY) is None,
          f("12/1800", TODAY))

    # ============================================================
    print("\n[2] 使えるアカウントの数え方 ★")
    await wipe()
    await add(1, "ACTIVE")
    await add(2, "DEGRADED")
    await add(3, "QUARANTINED")
    await add(4, "BANNED")
    rep = await A.stock_report(today=TODAY)
    check("登録は4件", rep.total == 4, rep.total)
    # ⚠️ 隔離・凍結は数えない。数に入れると「足りている」と誤認する。
    check("使えるのは ACTIVE と DEGRADED だけ ★", rep.usable == 2, rep.usable)
    check("状態ごとの数が出る",
          rep.by_status == {"ACTIVE": 1, "DEGRADED": 1,
                            "QUARANTINED": 1, "BANNED": 1}, rep.by_status)
    check("目安(2)を下回っていない", not rep.low, (rep.usable, rep.threshold))

    await wipe()
    await add(1, "ACTIVE")
    rep = await A.stock_report(today=TODAY)
    check("1件なら足りないと言う ★", rep.low, (rep.usable, rep.threshold))

    # しきい値は設定で変えられる
    await settings.set_value("mcd_low_accounts", 5)
    rep = await A.stock_report(today=TODAY)
    check("しきい値は設定で変わる ★", rep.threshold == 5, rep.threshold)
    await settings.set_value("mcd_low_accounts", 2)

    # ============================================================
    print("\n[3] カードの心配ごと ★")
    await wipe()
    await add(1, "ACTIVE", card="c1", expiry="12/28")   # 余裕
    await add(2, "ACTIVE", card="c2", expiry="11/26")   # 近い(45日以内)
    await add(3, "ACTIVE", card="c3", expiry="09/26")   # 切れている
    await add(4, "ACTIVE", card=None, expiry=None)      # 未設定
    rep = await A.stock_report(today=TODAY)
    check("期限切れを挙げる ★", [i for i, _, _ in rep.expired] == [3],
          rep.expired)
    check("期限が近いものを挙げる ★",
          [i for i, _, _, _ in rep.expiring] == [2], rep.expiring)
    check("余裕のあるカードは挙げない ★",
          1 not in [i for i, _, _, _ in rep.expiring], rep.expiring)
    check("カード未設定を挙げる ★", [i for i, _ in rep.no_card] == [4],
          rep.no_card)
    # ⚠️ 未設定のものを期限の一覧にも入れると、二重に数えてしまう
    check("未設定は期限の一覧に入れない ★",
          4 not in [i for i, _, _ in rep.expired]
          and 4 not in [i for i, _, _, _ in rep.expiring])
    # 10/17 から 11/30（月末）まで = 14 + 30 = 44日
    check("残り日数を添える", rep.expiring[0][3] == 44, rep.expiring)
    check("心配ごとがあると分かる", rep.has_warning)

    # 日数のしきい値も設定で変わる
    await settings.set_value("mcd_card_expiry_days", 5)
    rep2 = await A.stock_report(today=TODAY)
    check("期限の日数も設定で変わる ★", not rep2.expiring, rep2.expiring)
    await settings.set_value("mcd_card_expiry_days", 45)

    # 何も問題が無いとき
    await wipe()
    await add(1); await add(2); await add(3)
    clean = await A.stock_report(today=TODAY)
    check("問題が無ければ知らせない ★", not clean.has_warning,
          (clean.low, clean.no_card, clean.expiring, clean.expired))

    # ============================================================
    print("\n[4] 同じ知らせを繰り返さない ★")
    a = await A.stock_report(today=TODAY)
    b = await A.stock_report(today=TODAY)
    check("変わらなければ同じ印", a.signature() == b.signature())
    await add(4, "ACTIVE", card=None, expiry=None)
    c = await A.stock_report(today=TODAY)
    check("内容が変わると印も変わる ★", c.signature() != a.signature(),
          (a.signature(), c.signature()))

    # ============================================================
    print("\n[5] 定期点検での知らせ ★")
    import cogs.tasks as T
    from services.paypay import accounts as pp

    async def none_health():
        return []

    A.healthcheck_all = none_health
    from services.kyash import accounts as ky
    ky.healthcheck_all = none_health
    pp.healthcheck_all = none_health

    cog = T.TasksCog(FakeClient())
    cog._started = True
    sent: list = []

    async def grab(embed, **kw):
        sent.append(embed)

    cog.notify_admin = grab

    await cog.health_check.coro(cog)
    check("最初は知らせる ★", len(sent) == 1, len(sent))
    check("内容が入っている", sent and "カード" in (sent[0].description or ""),
          sent[0].description[:160] if sent else "")

    await cog.health_check.coro(cog)
    check("同じ状態では繰り返さない ★", len(sent) == 1, len(sent))

    # 状態が悪くなったら、また知らせる
    await add(5, "ACTIVE", card=None, expiry=None)
    await cog.health_check.coro(cog)
    check("悪くなればまた知らせる ★", len(sent) == 2, len(sent))

    # 片付いたら印を消し、次に悪化したとき再び知らせられる
    await wipe()
    await add(1); await add(2); await add(3)
    await cog.health_check.coro(cog)
    check("片付いたら知らせない ★", len(sent) == 2, len(sent))
    check("印が消える ★", cog._stock_sig == "", cog._stock_sig)
    await wipe()
    await add(1, "ACTIVE")
    await cog.health_check.coro(cog)
    check("また悪くなれば知らせる ★", len(sent) == 3, len(sent))

    # ============================================================
    print("\n[6] 知らせの見た目")
    from ui import embeds
    await wipe()
    await add(1, "ACTIVE", card=None, expiry=None)
    rep = await A.stock_report(today=TODAY)
    em = embeds.mcd_stock_alert(rep)
    check("注文が止まると伝える ★", "注文が全部止まります" in (em.description or ""),
          (em.description or "")[:200])
    check("未設定の手当てを案内する ★", "/mcd cards" in (em.description or ""),
          (em.description or "")[:300])
    check("使える件数も出す", "使える" in (em.description or ""))
    check("急ぐときは赤", em.color.value == embeds.RED, em.color)

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
