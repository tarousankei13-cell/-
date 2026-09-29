"""
DBから読んだ日時の扱いの検証

SQLite は DateTime(timezone=True) を指定してもタイムゾーンを保存しない。
書き込むときは付いていても、読み戻すと naive で返る。
そのまま datetime.now(timezone.utc) と引き算すると

    TypeError: can't subtract offset-naive and offset-aware datetimes

で落ちる。実際にこれで注文が一切通らなくなった。

⚠️ この種の不具合はモックでは絶対に見つからない。
   必ず**実際にDBへ書いて読み戻してから**計算すること。
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from db.models import as_utc, McdAccount, KyashAccount, StoreCache

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


def mcd_account(**kw):
    base = dict(label="テスト", email_enc=b"x", status="ACTIVE", card_id="card",
                device_uid="d", wmop_device_id="w", fb_instance_id="f",
                home_lat=35.0, home_lng=139.0)
    base.update(kw)
    return McdAccount(**base)


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/dt.db")
    now = datetime.now(timezone.utc)

    print("\n[1] as_utc の動き")
    check("naive はUTCとして扱う",
          as_utc(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=timezone.utc))
    check("aware はそのまま", as_utc(datetime(2026, 1, 1, tzinfo=timezone.utc)).tzinfo is not None)
    check("None は None のまま", as_utc(None) is None)
    other = timezone(timedelta(hours=9))
    check("UTC以外のtzは変換しない（値が狂わないように）",
          as_utc(datetime(2026, 1, 1, tzinfo=other)).utcoffset() == timedelta(hours=9))

    print("\n[2] SQLite に書いて読み戻すと naive になる（これが原因）")
    async with session_scope() as s:
        s.add(mcd_account(id=1, last_used_at=now))
    async with session_scope() as s:
        acc = await s.get(McdAccount, 1)
        naive = acc.last_used_at.tzinfo is None
        check("読み戻した値に tzinfo が無い（SQLiteの仕様）", naive,
              f"tzinfo={acc.last_used_at.tzinfo}")
        try:
            now - acc.last_used_at
            raw_ok = True
        except TypeError:
            raw_ok = False
        check("そのまま引き算すると落ちる（＝ガードが必要）", not raw_ok)
        check("as_utc を通せば引き算できる",
              abs((now - as_utc(acc.last_used_at)).total_seconds()) < 5)

    print("\n[3] アカウント選択が実際に動く（モックなし・実DB）")
    from services.mcd import accounts as mcd_accounts
    async with session_scope() as s:
        s.add(mcd_account(id=2, label="よく使う", last_used_at=now - timedelta(minutes=1)))
        s.add(mcd_account(id=3, label="しばらく未使用", last_used_at=now - timedelta(hours=5)))
        s.add(mcd_account(id=4, label="未使用", last_used_at=None))
    try:
        handle = await mcd_accounts.pick_account()
        picked, err = handle.label, None
        await handle.aclose()
    except Exception as e:
        picked, err = None, f"{type(e).__name__}: {e}"
    check("例外なくアカウントを選べる", err is None, err or "")
    check("何かしら選ばれている", picked is not None, picked)

    print("\n[4] 選び方が妥当か（長く使っていないものを優先）")
    async with session_scope() as s:
        rows = (await s.execute(__import__("sqlalchemy").select(McdAccount))).scalars().all()
        scored = sorted(rows, key=lambda a: mcd_accounts._score(a, now), reverse=True)
    check("直前に使ったアカウントは最優先にならない",
          scored[0].label != "よく使う", [a.label for a in scored])
    check("失敗が続くアカウントは下がる",
          mcd_accounts._score(mcd_account(consecutive_failures=3), now)
          < mcd_accounts._score(mcd_account(consecutive_failures=0), now))

    print("\n[5] Kyash のトークン残日数")
    from services.kyash import accounts as kyash_accounts
    async with session_scope() as s:
        s.add(KyashAccount(id=1, label="k1", email_enc=b"x", password_enc=b"x",
                           status="ACTIVE", token_obtained_at=now - timedelta(days=10)))
    async with session_scope() as s:
        acc = await s.get(KyashAccount, 1)
        try:
            days = kyash_accounts.token_days_left(acc)
            err = None
        except Exception as e:
            days, err = None, f"{type(e).__name__}: {e}"
    check("実DBの値で残日数を計算できる", err is None, err or "")
    check("10日経過ぶんが引かれている", days is not None and abs(days - 20) < 1.5, days)

    print("\n[6] メニューの古さ")
    from services.mcd import stores as mcd_stores
    async with session_scope() as s:
        s.add(StoreCache(store_id="13934", group_name="group-f", store_name="テスト店",
                         address="東京都", cat_root_url="x",
                         menu_synced_at=now - timedelta(minutes=42)))
    try:
        age = await mcd_stores.menu_age_minutes("13934")
        err = None
    except Exception as e:
        age, err = None, f"{type(e).__name__}: {e}"
    check("実DBの値で経過分を計算できる", err is None, err or "")
    check("42分前だと分かる", age is not None and abs(age - 42) < 2, age)

    print("\n[7] Python側で日時を計算している箇所に、ガード漏れがないか")
    import pathlib, re
    root = pathlib.Path(__file__).parent.parent
    leaks = []
    for f in root.rglob("*.py"):
        if "tests" in f.parts or ".git" in f.parts:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            # 「now - なにか_at」「datetime.now(...) - なにか」の形を探す
            if re.search(r"(now|utcnow\(\))\s*-\s*(?!timedelta)\w+", line) and "as_utc" not in line:
                if re.search(r"-\s*(acc|row|order|self)?\.?\w*_at\b", line):
                    leaks.append(f"{f.relative_to(root)}:{i}  {line.strip()}")
    check("DBの日時を直接引き算している箇所は無い", not leaks, leaks)

    print("\n[8] 提供時間帯は日本時間で判断する")
    import config, os, time, importlib
    from ui import menu_flows
    # 朝マックは日本時間 5:50〜10:20（350〜620分）
    saved = os.environ.get("TZ")
    results = {}
    for tz in ("UTC", "Asia/Tokyo", "America/New_York"):
        os.environ["TZ"] = tz
        try:
            time.tzset()
        except AttributeError:
            pass
        results[tz] = menu_flows.now_minutes()
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    try:
        time.tzset()
    except AttributeError:
        pass
    check("サーバーのタイムゾーンが変わっても同じ分を返す",
          len(set(results.values())) == 1, results)
    jst_now = config.now_jst()
    check("日本時間の時刻と一致する",
          abs(list(results.values())[0] - (jst_now.hour * 60 + jst_now.minute)) <= 1,
          f"{results} vs {jst_now.hour * 60 + jst_now.minute}")

    print("\n[9] 提供時間帯の日付キーは日本の日付")
    from datetime import datetime as _dt
    utc_date = _dt.now(timezone.utc).strftime("%Y-%m-%d")
    jst_date = config.today_jst()
    check("日本の日付を使っている", config.today_jst() == jst_now.strftime("%Y-%m-%d"))
    if utc_date != jst_date:
        check(f"UTCとずれる時間帯（UTC {utc_date} / 日本 {jst_date}）でも日本の日付",
              jst_date == jst_now.strftime("%Y-%m-%d"))
    else:
        check("（いまはUTCと日本の日付が同じ時間帯）", True)
    import pathlib as _p, re as _re
    src = _p.Path(__file__).parent.parent / "services" / "mcd" / "stores.py"
    body = src.read_text(encoding="utf-8")
    check("日付キーをUTCで作っていない",
          'datetime.now(timezone.utc).strftime("%Y-%m-%d")' not in body)

    print("\n[10] 「本日の件数」は日本時間の0時から")
    mid = config.jst_midnight()
    check("0時ちょうど", (mid.hour, mid.minute, mid.second) == (0, 0, 0), str(mid))
    check("日本時間である", mid.utcoffset() == timedelta(hours=9), str(mid.utcoffset()))
    check("未来にならない", mid <= config.now_jst())

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
