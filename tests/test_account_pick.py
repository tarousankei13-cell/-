"""同時注文でアカウントが取り合いにならないか

⚠️ `last_used_at` だけでは**同時に呼ばれたときに防げない**。
   3件が同時に来ると3件とも書き込み前の値を読み、同じアカウントを選ぶ。
   実際に「同時に3件 → [1, 2, 1]」になっていた（1番に集中、3番は未使用）。

   害は2つ。
     ・同時注文が実質1〜2アカウントしか使わず、並列にならない
     ・1つのアカウントに注文が集中し、機械的な使い方に見える

⚠️ 直し方の副作用に注意。**閉じ忘れると、そのアカウントは二度と
   選ばれなくなる。** 元のバグより悪い。ここで必ず確かめる。
"""
import ast, asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.crypto import get_cipher, init_cipher
from db.models import McdAccount
from db.session import close_db, init_db, session_scope

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/pick.db")
    c = get_cipher()
    async with session_scope() as s:
        for i in (1, 2, 3):
            s.add(McdAccount(
                id=i, label=f"acc{i}", email_enc=c.encrypt(f"a{i}@x"),
                refresh_token_enc=c.encrypt("rt"), card_id=f"card{i}",
                device_uid="d", wmop_device_id="w", fb_instance_id="f",
                home_lat=35.0, home_lng=139.0, status="ACTIVE"))
        # カード未設定は選ばれないこと（既存の決まり）
        s.add(McdAccount(
            id=9, label="カード無し", email_enc=c.encrypt("x@x"),
            refresh_token_enc=c.encrypt("rt"), card_id="",
            device_uid="d", wmop_device_id="w", fb_instance_id="f",
            home_lat=35.0, home_lng=139.0, status="ACTIVE"))

    from services.mcd import accounts as A

    print("\n[1] 同時に来ても取り合いにならない ★")
    hs = await asyncio.gather(*[A.pick_account() for _ in range(3)])
    picked = [h.account_id for h in hs]
    check("3件が3つとも別のアカウント ★", len(set(picked)) == 3, picked)
    check("カード未設定は選ばれない", 9 not in picked, picked)
    check("使用中として記録される", A.in_use() == {1, 2, 3}, A.in_use())

    print("\n[2] 全部ふさがっていても止まらない ★")
    # ⚠️ ここで例外にすると、混んでいる時間に注文が丸ごと失敗する。
    #    待たせるより使い回すほうがよい（同時数は別で絞っている）。
    h4 = await A.pick_account()
    check("4件目も選べる ★", h4.account_id in (1, 2, 3), h4.account_id)
    await h4.aclose()

    print("\n[3] 閉じたら必ず手放す ★")
    for h in hs:
        await h.aclose()
    check("全部手放されている ★", A.in_use() == set(), A.in_use())
    check("二重に閉じても落ちない", (await _twice(hs[0])) is True)

    hs2 = await asyncio.gather(*[A.pick_account() for _ in range(3)])
    check("もう一度でも3つに割れる ★",
          len({h.account_id for h in hs2}) == 3, [h.account_id for h in hs2])
    for h in hs2:
        await h.aclose()

    print("\n[4] 閉じ忘れても自力で戻る ★")
    # ⚠️ 印が残り続けると、そのアカウントは二度と選ばれない。
    #    元のバグより悪いので、時間で自動的に外す。
    import time as _t
    h = await A.pick_account()
    A._in_use[h.account_id] = _t.monotonic() - A._HOLD_LIMIT_SECONDS - 1
    check("古い印は自動で外れる ★", h.account_id not in A.in_use())
    await h.aclose()
    A.release_all()

    print("\n[5] 呼び出し元が必ず手放しているか ★")
    # ⚠️ 1か所でも finally を忘れると、そのアカウントが消える。
    #    同じ誤りは1か所とは限らないので、全部数える。
    missing = []
    for root in (".", "ui", "cogs", "core", "services", "services/mcd"):
        if not os.path.isdir(root):
            continue
        for f in sorted(os.listdir(root)):
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            if "/test" in path or path.startswith("./test"):
                continue
            src = open(path).read()
            if "mcd_accounts.pick_account(" not in src and not (
                path.endswith("services/mcd/accounts.py")
                and "await pick_account()" in src
            ):
                continue
            tree = ast.parse(src)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                body = ast.unparse(fn)
                if "pick_account()" not in body:
                    continue
                if "aclose()" not in body:
                    missing.append(f"{path}:{fn.name} 閉じていない")
                    continue
                # finally の中で閉じているか
                has_finally = any(
                    isinstance(t, ast.Try) and t.finalbody
                    and "aclose()" in ast.unparse(t.finalbody)
                    for t in ast.walk(fn)
                )
                if not has_finally:
                    missing.append(f"{path}:{fn.name} finally の外")
    check("すべての呼び出し元が finally で手放す ★", not missing, missing[:4])

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


async def _twice(handle) -> bool:
    try:
        await handle.aclose()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
