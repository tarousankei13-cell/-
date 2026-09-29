"""定期処理の検証 — バックアップ・元帳点検・レシート画像"""
import asyncio, sys, os, tempfile, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from core import ledger as L, settings, users as user_repo
from services import tasks as jobs

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/t.db")
    await settings.load_all()

    UID = 9001
    await user_repo.get_or_create(UID)
    async with user_scope(UID) as s:
        await L.charge(s, UID, 4200, receipt_id="tk-1")

    print("\n[1] バックアップ")
    name, data = await jobs.make_backup()
    check("ファイル名に日時が入る", name.startswith("backup-") and name.endswith(".db"), name)
    check(f"中身がある（{len(data)/1024:.0f}KB）", len(data) > 1000, len(data))
    check("SQLiteの形式になっている", data[:16] == b"SQLite format 3\x00", data[:16])

    # 取り出したバックアップから残高を復元できるか
    bpath = f"{tmp}/restored.db"
    open(bpath, "wb").write(data)
    conn = sqlite3.connect(bpath)
    rows = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE account = ?", (f"user:{UID}",)
    ).fetchone()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    check("バックアップから残高を読み出せる（¥4,200）", rows[0] == 4200, rows[0])
    check("主要なテーブルが揃っている",
          {"ledger", "orders", "users", "mcd_accounts", "kyash_accounts"} <= tables,
          sorted(tables)[:8])
    check("暗号化キーは含まれない（別ファイル）", "encryption_key" not in str(tables))

    print("\n[2] 元帳の点検")
    ok_flag, msg = await jobs.check_ledger()
    check("正常と判定される", ok_flag is True, msg)

    # わざと壊して検出できるか確かめる
    from db.models import Ledger
    async with session_scope() as s:
        s.add(Ledger(tx_id="broken-tx", account="user:9001", amount=999, kind="adjust"))
    ok_flag, msg = await jobs.check_ledger()
    check("不整合を検出する", ok_flag is False, msg[:80])
    check("内容を報告する", "貸借" in msg or "一致" in msg, msg[:120])

    async with session_scope() as s:
        from sqlalchemy import delete
        await s.execute(delete(Ledger).where(Ledger.tx_id == "broken-tx"))
    ok_flag, _ = await jobs.check_ledger()
    check("直せば正常に戻る", ok_flag is True)

    print("\n[3] メニュー差分の文面")
    from services.mcd.menu import MenuDiff
    d = MenuDiff(added=[("1", "新商品A"), ("2", "新商品B")],
                 removed=[("3", "終売C")],
                 price_changed=[("4", "値上げD", 100, 150)])
    text = jobs.format_menu_diff("13934", "南砂町店", d)
    check("新商品が載る", "新商品A" in text, text[:80])
    check("終売が載る", "終売C" in text, text[:120])
    check("価格変更が載る", "¥100" in text and "¥150" in text, text[:200])
    check("変更なしなら None", jobs.format_menu_diff("1", "店", MenuDiff()) is None)

    print("\n[4] リセット処理")
    from db.models import KyashAccount, McdAccount, utcnow
    async with session_scope() as s:
        s.add(McdAccount(id=1, label="m", email_enc=b"x", device_uid="d",
                         wmop_device_id="w", fb_instance_id="f",
                         home_lat=35.0, home_lng=139.0, orders_today=7))
        s.add(KyashAccount(id=1, label="k", email_enc=b"x",
                           received_this_month=12345, token_obtained_at=utcnow()))
    await jobs.daily_reset()
    async with session_scope() as s:
        check("当日の注文数がリセットされる", (await s.get(McdAccount, 1)).orders_today == 0)
    month = await jobs.monthly_reset_if_needed(None)
    async with session_scope() as s:
        check("月間受取額がリセットされる", (await s.get(KyashAccount, 1)).received_this_month == 0)
    check("実行月を返す", 1 <= month <= 12, month)

    print("\n[5] Kyashトークンの期限通知")
    from datetime import datetime, timedelta, timezone
    async with session_scope() as s:
        acc = await s.get(KyashAccount, 1)
        acc.token_obtained_at = datetime.now(timezone.utc) - timedelta(days=28)
    warns = await jobs.kyash_token_warnings()
    check("期限が近いと警告が出る", len(warns) == 1, warns)
    check("残り日数を伝える", "残り" in warns[0] or "失効" in warns[0], warns)

    print("\n[6] レシート画像")
    from services.receipt import self_check, render, receipt_view_url
    check("生成の準備ができている", self_check() == "", self_check())
    for num in ["7161", "7", "12345"]:
        buf = await render(num)
        data = buf.getvalue()
        check(f"注文番号 {num} の画像を生成（{len(data)//1024}KB）",
              len(data) > 10000 and data[:8] == b"\x89PNG\r\n\x1a\n", len(data))
    url = receipt_view_url("13934", "7161")
    check("受け取り画面のURLが作れる", "13934" in url and "7161" in url, url)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
