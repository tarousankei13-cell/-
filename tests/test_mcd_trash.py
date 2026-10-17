"""アカウントのゴミ箱（ソフト削除）と取り消し

⚠️ ここが狂うと「消したアカウントで注文してしまう」ので、次を固定する。

   ① ゴミ箱のアカウントは**注文に選ばれない**
   ② 一覧・点検・書き出し・一括復帰に**出てこない**
   ③ `/mcd enable` からは戻せない（戻すのは /mcd undo だけ）
   ④ 期限が来るまでは消えない。来たら本削除し、注文履歴は残す
"""
import asyncio, os, sys, tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select, text

from _fake_discord import FakeClient, FakeInteraction, FakeUser
from core import settings
from core.crypto import get_cipher, init_cipher
from db.models import McdAccount, McdToken, utcnow
from db.session import close_db, init_db, session_scope
from services.mcd import accounts as A

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def add(i, status="ACTIVE"):
    cipher = get_cipher()
    async with session_scope() as s:
        s.add(McdAccount(
            id=i, label=f"acc{i}",
            email_enc=cipher.encrypt(f"u{i}@example.com"),
            refresh_token_enc=cipher.encrypt("r"),
            device_uid=f"d{i}", wmop_device_id=f"w{i}", fb_instance_id=f"f{i}",
            home_lat=35.0, home_lng=139.0, status=status,
            card_id="c1", card_expiry="12/28",
        ))
        s.add(McdToken(mcd_account_id=i, access_token_enc=cipher.encrypt("a")))


async def status_of(i):
    async with session_scope() as s:
        a = await s.get(McdAccount, i)
        return a.status if a else None


async def main() -> int:
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/trash.db")
    await settings.load_all()

    import cogs.account as acc
    from cogs.account import AccountCog

    owner = FakeUser(1, "オーナー")
    dclient = FakeClient()
    cog = AccountCog(dclient)

    await add(1); await add(2); await add(3)

    # ============================================================
    print("\n[1] ゴミ箱に入れる ★")
    itx = FakeInteraction(owner, dclient)
    await cog.mcd_remove.callback(cog, itx, account_id=2)
    check("状態が DELETED になる ★", await status_of(2) == "DELETED",
          await status_of(2))
    check("行は消えていない ★", await status_of(2) is not None)
    check("戻せると伝える ★", "undo" in itx.text(), itx.text()[:200])
    check("注文に使われないと伝える", "注文には使われなくなります" in itx.text(),
          itx.text()[:200])

    # ⚠️ ここが本題。注文に使うアカウントの選び方から外れていること。
    check("DELETED は USABLE に入っていない ★", "DELETED" not in A.USABLE,
          A.USABLE)
    async with session_scope() as s:
        usable = (await s.execute(
            select(McdAccount).where(McdAccount.status.in_(A.USABLE))
        )).scalars().all()
    check("注文に選ばれる側に出てこない ★",
          2 not in [a.id for a in usable], [a.id for a in usable])

    # ============================================================
    print("\n[2] 一覧・点検・書き出しに出てこない ★")
    async with session_scope() as s:
        live = (await s.execute(A.live())).scalars().all()
    check("live() から外れる ★", 2 not in [a.id for a in live],
          [a.id for a in live])
    rep = await A.stock_report()
    check("在庫の数に入れない ★", rep.total == 2, rep.total)
    data, n = await acc._export_csv()
    check("CSVに出てこない ★", n == 2 and "u2@example.com" not in data.decode("utf-8-sig"),
          n)
    ov = await A.card_overview()
    check("カード一覧に出てこない ★", 2 not in [o.get("id") for o in ov],
          [o.get("id") for o in ov])

    # 一括の付け替えでも触らない
    itx2 = FakeInteraction(owner, dclient)
    await cog.mcd_relabel.callback(cog, itx2, prefix="名前")
    async with session_scope() as s:
        a2 = await s.get(McdAccount, 2)
        others = [(await s.get(McdAccount, i)).label for i in (1, 3)]
    check("ゴミ箱の名前は変えない ★", a2.label == "acc2", a2.label)
    check("他はそろう", others == ["名前1", "名前2"], others)

    # ============================================================
    print("\n[3] /mcd enable からは戻せない ★")
    # ⚠️ ここを許すと、消したつもりのアカウントが黙って注文に戻る。
    itx3 = FakeInteraction(owner, dclient)
    await cog.mcd_enable.callback(cog, itx3, account_id=2)
    check("enable では戻らない ★", await status_of(2) == "DELETED",
          await status_of(2))
    check("undo を案内する ★", "undo" in itx3.text(), itx3.text()[:200])

    # 一括復帰（隔離をまとめて戻す画面）でも蘇らない
    await add(4, "QUARANTINED")
    async with session_scope() as s:
        rows = (await s.execute(A.live())).scalars().all()
    check("一括復帰の対象にも入らない ★", 2 not in [a.id for a in rows],
          [a.id for a in rows])

    # ============================================================
    print("\n[4] 取り消して戻す ★")
    itx4 = FakeInteraction(owner, dclient)
    await cog.mcd_undo.callback(cog, itx4, account_id=2)
    check("戻ると ACTIVE ★", await status_of(2) == "ACTIVE", await status_of(2))
    async with session_scope() as s:
        a2 = await s.get(McdAccount, 2)
    check("ゴミ箱の日時が消える ★", a2.deleted_at is None, a2.deleted_at)
    check("戻したと伝える", "元に戻しました" in itx4.text(), itx4.text()[:160])

    # 入っていないものは戻せない
    itx5 = FakeInteraction(owner, dclient)
    await cog.mcd_undo.callback(cog, itx5, account_id=2)
    check("入っていなければ断る ★", "入っていません" in itx5.text(),
          itx5.text()[:160])

    # ============================================================
    print("\n[5] ゴミ箱の一覧 ★")
    itx6 = FakeInteraction(owner, dclient)
    await cog.mcd_undo.callback(cog, itx6)
    check("空なら空と言う ★", "空です" in itx6.text(), itx6.text()[:120])
    await cog.mcd_remove.callback(cog, FakeInteraction(owner, dclient), account_id=3)
    itx7 = FakeInteraction(owner, dclient)
    await cog.mcd_undo.callback(cog, itx7)
    check("中身を出す ★", "#3" in itx7.text(), itx7.text()[:200])
    check("あと何日かを出す ★", "日で本削除" in itx7.text(), itx7.text()[:200])
    rows = await A.trashed()
    check("残り日数は設定どおり ★", rows and rows[0][2] == A.trash_days(), rows)

    # ============================================================
    print("\n[6] 期限が来たら本削除、履歴は残す ★")
    # まだ期限内なら消さない
    gone = await A.purge_expired()
    check("期限内は消さない ★", gone == 0 and await status_of(3) == "DELETED",
          (gone, await status_of(3)))

    # 注文履歴をつけて、期限切れにする
    from db.models import Order
    async with session_scope() as s:
        s.add(Order(
            id="o1", idempotency_key="k1", discord_id=555, state="DONE",
            mcd_account_id=3, store_id="13934", hex_payload="00",
            list_price=500, subsidy_rate=0, user_amount=500, subsidy_amount=0,
        ))
        a3 = await s.get(McdAccount, 3)
        a3.deleted_at = utcnow() - timedelta(days=A.trash_days() + 1)

    gone = await A.purge_expired()
    check("期限切れは本削除する ★", gone == 1, gone)
    check("行が消える ★", await status_of(3) is None, await status_of(3))
    async with session_scope() as s:
        tok = (await s.execute(text(
            "SELECT COUNT(*) FROM mcd_tokens WHERE mcd_account_id=3"))).scalar()
        order = (await s.execute(text(
            "SELECT mcd_account_id FROM orders WHERE id='o1'"))).fetchone()
    check("トークンも消える ★", tok == 0, tok)
    # ⚠️ 注文の履歴は消さない。消すと売上や控えが合わなくなる。
    check("注文の履歴は残る ★", order is not None, order)
    check("注文の参照は空になる ★", order and order[0] is None, order)

    # 他のアカウントは巻き込まれない
    check("残りは無事 ★", await status_of(1) == "ACTIVE", await status_of(1))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
