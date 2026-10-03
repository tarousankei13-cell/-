"""
バックアップの保存先と復元の検証

保存先が1か所しか無いと、そこが消えたときに復旧できない。
復元は取り返しがつかないので、壊れたファイルを弾くことと、
戻す直前の内容を残すことを確かめる。
"""
import asyncio, os, sqlite3, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import backup

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


def make_db(rows=3, tables=("ledger", "orders", "users")):
    path = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(path)
    for t in tables:
        con.execute(f"CREATE TABLE {t} (id INTEGER)")
    if "ledger" in tables:
        for i in range(rows):
            con.execute("INSERT INTO ledger VALUES (?)", (i,))
    con.commit(); con.close()
    data = Path(path).read_bytes()
    Path(path).unlink()
    return data


async def main():
    print("\n[1] 中身を確かめる ★")
    good, msg = backup.verify(make_db(rows=5))
    check("正しいものを通す", good, msg)
    check("件数が分かる", "5" in msg, msg)
    good, msg = backup.verify("これはDBではありません".encode())
    check("壊れたものを弾く ★", not good, msg)
    check("理由が分かる", "読めません" in msg, msg)
    good, msg = backup.verify(make_db(tables=("other",)))
    check("中身が足りないものを弾く ★", not good, msg)
    check("何が足りないか分かる", "ledger" in msg, msg)
    good, msg = backup.verify(b"")
    check("空でも落ちない", not good, msg)

    print("\n[2] サーバー上に残す")
    tmpdir = Path(tempfile.mkdtemp())
    backup.LOCAL_DIR = tmpdir
    data = make_db()
    path = backup.save_local("backup-20261003-0100.db", data, keep=3)
    check("ファイルができる", path.exists(), path)
    check("中身が同じ", path.read_bytes() == data)
    check("一覧に出る", len(backup.list_local()) == 1, backup.list_local())

    print("\n[3] 古い世代を消す ★")
    for i in range(5):
        backup.save_local(f"backup-2026100{i}-0100.db", data, keep=3)
        time.sleep(0.01)
    files = backup.list_local()
    check("残す数を守る ★", len(files) == 3, [f[0] for f in files])
    check("新しいものが残る", files[0][0] > files[-1][0], [f[0] for f in files])

    print("\n[4] 一覧の中身")
    name, size, mtime = backup.list_local()[0]
    check("名前が取れる", name.startswith("backup-"), name)
    check("大きさが取れる", size == len(data), (size, len(data)))
    check("時刻が取れる", mtime > 0, mtime)

    print("\n[5] 復元できる ★")
    dbdir = Path(tempfile.mkdtemp())
    dbfile = dbdir / "bot.db"
    old = make_db(rows=1)
    dbfile.write_bytes(old)
    new = make_db(rows=99)
    good, msg = backup.restore(new, f"sqlite+aiosqlite:///{dbfile}")
    check("成功する ★", good, msg)
    check("中身が置き換わる ★", dbfile.read_bytes() == new)
    check("再起動を促す", "再起動" in msg, msg)

    print("\n[6] 戻す直前の内容を残す ★")
    kept = list(dbdir.glob("bot-before-restore-*.db"))
    check("退避ファイルができる ★", len(kept) == 1, [p.name for p in kept])
    check("退避した中身は元のもの ★", kept[0].read_bytes() == old)

    print("\n[7] 壊れたファイルでは上書きしない ★")
    before = dbfile.read_bytes()
    good, msg = backup.restore("壊れています".encode(), f"sqlite+aiosqlite:///{dbfile}")
    check("断る ★", not good, msg)
    check("中身を変えない ★", dbfile.read_bytes() == before)

    print("\n[8] 補助ファイルを片付ける")
    for suffix in ("-wal", "-shm"):
        Path(str(dbfile) + suffix).write_bytes("ふるい".encode())
    backup.restore(make_db(rows=7), f"sqlite+aiosqlite:///{dbfile}")
    left = [s for s in ("-wal", "-shm") if Path(str(dbfile) + s).exists()]
    check("古い補助ファイルを消す", not left, left)

    print("\n[9] PostgreSQL では断る")
    good, msg = backup.restore(make_db(), "postgresql+asyncpg://u:p@h/db")
    check("この操作からは戻さない", not good, msg)
    check("手順を案内する", "pg_restore" in msg, msg)

    print("\n[10] 保存先の設定で分かれる")
    import inspect
    from cogs import tasks as tasks_cog
    src = inspect.getsource(tasks_cog.save_backup)
    for place in ("channel", "dm", "local", "all"):
        check(f"{place} を扱う", f'"{place}"' in src)
    check("どれか成功すればよい", "saved" in src and "errors" in src)
    check("全部失敗したときだけ騒ぐ",
          "not result.where" in inspect.getsource(tasks_cog.TasksCog._send_backup))

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
