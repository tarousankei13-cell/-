"""
管理操作の記録の検証

お金を扱うので「誰がいつ何を変えたか」が残る必要がある。
設定の変更は自動で記録されること、記録できなくても元の操作は
止まらないことを確かめる。
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from core import audit, settings

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

ADMIN, OTHER, TARGET = 111, 222, 333


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/audit.db")
    await settings.load_all()

    print("\n[1] 記録する")
    await audit.record(
        actor_id=ADMIN, actor_name="管理者A", action="balance.grant",
        target=str(TARGET), after="+1,000円", reason="キャンペーン",
    )
    rows = await audit.search()
    check("1件残る", len(rows) == 1, len(rows))
    r = rows[0]
    check("操作者が分かる", r.actor_id == ADMIN and r.actor_name == "管理者A")
    check("対象が分かる", r.target == str(TARGET), r.target)
    check("理由が残る", r.reason == "キャンペーン", r.reason)
    check("日本語の見出しになる",
          audit.label("balance.grant") == "残高を付与", audit.label("balance.grant"))

    print("\n[2] 設定の変更は自動で記録される ★")
    before = await audit.count()
    await settings.set_value("subsidy_rate", 60.0, updated_by=ADMIN, actor_name="管理者A")
    rows = await audit.search(action="config")
    check("記録が増える", await audit.count() == before + 1)
    check("設定キーが対象になる", rows[0].target == "subsidy_rate", rows[0].target)
    check("変更後の値が残る", "60" in rows[0].after, rows[0].after)

    await settings.set_value("subsidy_rate", 40.0, updated_by=ADMIN)
    rows = await audit.search(action="config")
    check("変更前の値も残る ★", "60" in rows[0].before, rows[0].before)
    check("変更後の値も残る", "40" in rows[0].after, rows[0].after)

    print("\n[3] 記録しない指定")
    before = await audit.count()
    await settings.set_value("internal_thing", 1, updated_by=ADMIN, audit=False)
    check("audit=False なら残さない", await audit.count() == before)
    before = await audit.count()
    await settings.set_value("internal_thing", 2)
    check("操作者が分からなければ残さない", await audit.count() == before)

    print("\n[4] 絞り込み")
    await audit.record(actor_id=OTHER, actor_name="管理者B", action="user.ban",
                       target=str(TARGET))
    check("操作者で絞れる", len(await audit.search(actor_id=OTHER)) == 1)
    check("種類で絞れる", len(await audit.search(action="user.")) == 1)
    check("対象で絞れる", len(await audit.search(target=str(TARGET))) >= 2)
    check("件数を制限できる", len(await audit.search(limit=1)) == 1)

    print("\n[5] 新しい順に出る")
    rows = await audit.search(limit=50)
    times = [r.when for r in rows]
    check("時刻が降順", times == sorted(times, reverse=True))

    print("\n[6] 表示用の1行")
    line = rows[0].line()
    check("見出しが日本語", any(w in line for w in audit.ACTIONS.values()), line[:80])
    check("時刻が入る", "<t:" in line, line[:60])

    print("\n[7] 記録に失敗しても操作は止まらない ★")
    import core.audit as A
    real = A.session_scope
    class Boom:
        async def __aenter__(self): raise RuntimeError("DBが落ちている")
        async def __aexit__(self, *a): return False
    A.session_scope = lambda: Boom()
    try:
        await audit.record(actor_id=ADMIN, action="balance.grant", target="1")
        err = None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    A.session_scope = real
    check("例外を外に出さない", err is None, err or "")

    print("\n[8] 長すぎる内容を切り詰める")
    await audit.record(
        actor_id=ADMIN, action="config.set", target="x" * 200,
        reason="り" * 500, before={"a": "b" * 5000},
    )
    r = (await audit.search(limit=1))[0]
    check("対象を切り詰める", len(r.target) <= 64, len(r.target))
    check("理由を切り詰める", len(r.reason) <= 255, len(r.reason))
    check("変更前の内容を切り詰める", len(r.before) <= 2000, len(r.before))

    print("\n[9] 知らない種類でも落ちない")
    await audit.record(actor_id=ADMIN, action="まだ無い操作", target="x")
    r = (await audit.search(limit=1))[0]
    check("そのまま表示する", audit.label("まだ無い操作") == "まだ無い操作")
    check("行にできる", isinstance(r.line(), str) and r.line())

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
