"""
アカウント健全性パネルの検証

一覧を見て「止まっている」と分かっても、別のコマンドを打ちに行くのでは
手間がかかる。その場で手当てできることと、何をすべきかが伝わることを確かめる。
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from _fake_discord import FakeInteraction, FakeUser, FakeClient

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


def mcd(**kw):
    from db.models import McdAccount
    base = dict(label="acc", email_enc=b"x", status="ACTIVE", card_id="card",
                device_uid="d", wmop_device_id="w", fb_instance_id="f",
                home_lat=35.0, home_lng=139.0)
    base.update(kw)
    return McdAccount(**base)


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/ap.db")
    from core import settings, breaker
    await settings.load_all()

    from db.models import KyashAccount
    from ui import admin_flows
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        s.add(mcd(id=1, label="メイン", orders_today=5, last_used_at=now))
        s.add(mcd(id=2, label="カード無し", card_id=None))
        s.add(mcd(id=3, label="隔離中", status="QUARANTINED",
                  consecutive_failures=5, last_error="決済が拒否されました"))
        s.add(KyashAccount(id=1, label="k1", email_enc=b"x", password_enc=b"x",
                           status="ACTIVE",
                           token_obtained_at=now - timedelta(days=28)))

    admin = FakeUser(1)
    client = FakeClient()

    print("\n[1] 一覧が出る")
    itx = FakeInteraction(admin, client)
    await admin_flows.show_accounts(itx)
    text = itx.text()
    check("マクドナルドのアカウントが並ぶ", "メイン" in text and "隔離中" in text, text[:120])
    check("カード未設定が分かる", "カード未設定" in text, text[:200])
    check("Kyashも出る", "Kyash" in text, text[:200])
    view = itx.last_view()
    check("手当てのボタンが付く", view is not None and len(view.children) >= 2,
          len(view.children) if view else 0)

    print("\n[2] 隔離中があれば復帰ボタンが出る ★")
    labels = [getattr(c, "label", "") for c in view.children]
    check("復帰ボタンがある ★", any("復帰" in l for l in labels), labels)
    check("詳しく見るボタンがある", any("詳しく" in l for l in labels), labels)
    check("いま確かめるボタンがある", any("確かめる" in l for l in labels), labels)

    print("\n[3] 詳しい状態に「何をすべきか」が出る ★")
    detail = next(c for c in view.children if "詳しく" in getattr(c, "label", ""))
    itx2 = FakeInteraction(admin, client)
    await detail.callback(itx2)
    t = itx2.text()
    check("カード未設定への対処が出る ★", "/mcd card 2" in t, t[:300])
    check("決済拒否への対処が出る ★",
          "残高" in t and "限度額" in t, [l for l in t.split("\n") if "残高" in l][:2])
    check("連続失敗の回数が出る", "連続失敗" in t, t[:200])
    check("Kyashのトークン残り日数が出る", "残り" in t and "日" in t, t[:300])

    print("\n[4] 隔離を復帰できる ★")
    unlock = next(c for c in view.children if "復帰" in getattr(c, "label", ""))
    itx3 = FakeInteraction(admin, client)
    await unlock.callback(itx3)
    check("復帰した旨が出る", "戻しました" in itx3.text(), itx3.text()[:120])
    async with session_scope() as s:
        from db.models import McdAccount
        acc = await s.get(McdAccount, 3)
    check("状態がACTIVEに戻る ★", acc.status == "ACTIVE", acc.status)
    check("連続失敗が0に戻る", acc.consecutive_failures == 0, acc.consecutive_failures)
    check("原因の確認を促す", "確認" in itx3.text(), itx3.text()[-120:])

    print("\n[5] 止めている経路を戻せる ★")
    for _ in range(breaker.FAILURES_TO_OPEN):
        breaker.accounts.record_failure("mcd:1", "timeout")
    check("経路が止まっている", not breaker.accounts.allows("mcd:1"))
    itx4 = FakeInteraction(admin, client)
    await admin_flows.show_accounts(itx4)
    v2 = itx4.last_view()
    revive = next((c for c in v2.children if "戻す" in getattr(c, "label", "")), None)
    check("戻すボタンが出る ★", revive is not None,
          [getattr(c, "label", "") for c in v2.children])
    itx5 = FakeInteraction(admin, client)
    await revive.callback(itx5)
    check("経路が戻る ★", breaker.accounts.allows("mcd:1"))
    check("また止まりうる旨を伝える", "また止まります" in itx5.text(), itx5.text()[:150])

    print("\n[6] 操作が記録に残る ★")
    from core import audit
    rows = await audit.search(action="account")
    check("監査ログに残る ★", len(rows) >= 2, len(rows))
    check("操作者が分かる", all(r.actor_id == 1 for r in rows), [r.actor_id for r in rows])

    print("\n[7] 何をすべきかの判定")
    todo = admin_flows._account_todo
    check("カード未設定", "カード" in todo("ACTIVE", False, ""))
    check("決済拒否", "残高" in todo("ACTIVE", True, "決済が拒否されました"))
    check("認証切れ", "再ログイン" in todo("ACTIVE", True, "認証が切れています"))
    check("隔離中", "復帰" in todo("QUARANTINED", True, ""))
    check("正常なら何も出さない", todo("ACTIVE", True, "") == "")

    print("\n[8] アカウントが無くても落ちない")
    async with session_scope() as s:
        from db.models import McdAccount
        for i in (1, 2, 3):
            acc = await s.get(McdAccount, i)
            if acc:
                await s.delete(acc)
    itx6 = FakeInteraction(admin, client)
    await admin_flows.show_accounts(itx6)
    check("未登録の案内が出る", "未登録" in itx6.text(), itx6.text()[:150])

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
