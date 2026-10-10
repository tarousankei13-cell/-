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

    print("\n[6] 止まった理由の記録 ★")
    # ⚠️ last_error は最後の1件しか残らない。止まった理由は後から
    #    調べるものなので、そのときには上書きされている。
    await A.report_failure(1, '{"error":"invalid_grant","token":"SECRET123456"}')
    await A.report_failure(1, "HTTP 401 Unauthorized")
    await A.report_failure(1, "HTTP 401 Unauthorized")
    evs = await A.account_events(1)
    check("経緯が複数残る ★", len(evs) >= 3, len(evs))
    check("ログイン切れを AUTH と分かる ★",
          all(e.kind == "AUTH" for e in evs[:3]), [e.kind for e in evs[:3]])
    check("連続失敗の回数も残る", evs[0].failures >= 3, evs[0].failures)
    check("状態の変化が残る", any(e.action in ("degrade", "quarantine") for e in evs))
    # ⚠️ 生の応答には認証情報が混ざりうる。必ず伏せる。
    check("生の記録に秘密が残らない ★",
          all("SECRET123456" not in (e.raw or "") for e in evs))
    check("伏せたことは分かる", any("伏せました" in (e.raw or "") for e in evs))
    await A.report_success_healthcheck(1)
    check("復帰も残る ★", (await A.account_events(1))[0].action == "recover")

    print("\n[7] エラーの分類 ★")
    from services.mcd.errors import parse as _parse
    cases = [
        (401, "{}", "AUTH"),
        (0, "HTTP 401 Unauthorized", "AUTH"),
        (0, '{"error":"invalid_grant"}', "AUTH"),
        (0, "ログインに失敗しました", "AUTH"),
        (402, "{}", "PAYMENT"),
        (0, "残高が不足しています", "PAYMENT"),
        (0, "ErrorCode_Authorisation", "PAYMENT"),
        (0, '{"message":"9030 > 9997925 > 3120 product not found"}', "PRODUCT_GONE"),
        (0, "card not found", "CARD"),
        (0, "store closed", "STORE"),
        (0, "ただいまのお時間はお取り扱いがありません", "PRODUCT_TIME"),
        (0, "よく分からない何か", "UNKNOWN"),
    ]
    wrong = [(b, _parse(st, b).kind, w) for st, b, w in cases
             if _parse(st, b).kind != w]
    check("12通りすべて正しく分かれる ★", not wrong, wrong[:3])

    # ⚠️ "authoriz" は "unauthorized" に含まれる。PAYMENT を先に判定するので、
    #    401 が全部「決済の問題」になっていた。管理者はカードを疑い、
    #    本当の原因（ログイン）に辿り着けない。
    check("401 を決済の問題と取り違えない ★",
          _parse(0, "HTTP 401 Unauthorized").kind == "AUTH")
    # ⚠️ extract_strings は文章を単語に分解するので、複数語の判定語が
    #    一度も一致しない。"product not found" が "found product" になる。
    check("複数語の判定語が効く ★",
          _parse(0, "product not found").kind == "PRODUCT_GONE")

    # ⚠️ 印を付けたあとで失敗したら、印を外さなければならない。
    #    呼び出し側は handle を受け取れないので閉じようがなく、
    #    外し忘れるとそのアカウントは15分間選ばれない。
    #    アカウントが1件しかない環境では、それで注文が止まる。
    A.release_all()
    before = set(A.in_use())
    real = A.build_client
    A.build_client = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("端末情報が壊れている"))
    try:
        await A.pick_account()
        check("作れないときは例外になる ★", False, "例外が出なかった")
    except RuntimeError:
        check("作れないときは例外になる ★", True)
    finally:
        A.build_client = real
    check("作るのに失敗しても使用中の印が残らない ★",
          set(A.in_use()) == before, sorted(A.in_use()))
    h = await A.pick_account()
    check("失敗の直後でも普通に選べる ★", h is not None)
    await h.aclose()

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
