"""PayPay の保留（受け取りリンクが一時的に止められる）

PayPay は 2024-02-28 から、受け取りリンクを**保留**にすることがある。
送った側に警告を出し、「送る」か「キャンセル」を選ばせる仕組み。
  https://paypay.ne.jp/notice/20240228/f-p2p-money-link/

⚠️ 以前は「PENDING でなければ受け取り・辞退・取り消し済み」と
   決めつけていた。そのため**保留中の人に「このリンクはもう使えません」
   と伝えていた**。お金はまだ送り主の手元にあるのに、諦めさせていた。

⚠️ 迷ったら「終わっていない」側に倒す。
   ・終わったものを保留と誤れば → 無駄に数回見に行って期限切れ。害は小さい
   ・保留を終わったと誤れば     → 届くはずのお金を断る。害が大きい
"""
import asyncio, os, sys, tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import settings
from core.crypto import init_cipher
from db.models import PayPayReceipt, utcnow
from db.session import close_db, init_db, session_scope

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


def _info(status, amount=1000):
    from services.paypay.client import LinkInfo
    return LinkInfo(amount=amount, order_id=f"ord-{status}-{amount}",
                    message_id="m", chat_room_id="c", status=status,
                    has_password=False, sender_name="送った人")


async def main():
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/pp.db")
    await settings.load_all()

    from services.paypay import charge as C
    from services.paypay.client import LinkInfo

    print("\n[1] 状態の見分け ★")
    check("PENDING は受け取れる", _info("PENDING").receivable)
    for st in ("COMPLETED", "REJECTED", "CANCELED", "EXPIRED", "DECLINED"):
        check(f"{st} は終わっている", _info(st).terminal and not _info(st).on_hold)
    # ⚠️ ここが肝。知らない状態を「終わった」にしない。
    for st in ("WAITING_APPROVAL", "ON_HOLD", "なぞの値", ""):
        i = _info(st)
        check(f"{st or '(空)'} は保留扱い ★", i.on_hold and not i.terminal, st)
    check("小文字でも終わりと分かる", _info("completed").terminal)

    print("\n[2] 保留を『使えません』と言わない ★")
    # ⚠️ ChargeError（駄目でした）と ChargeOnHold（まだです）は
    #    利用者への伝え方がまったく違う。必ず別の型にする。
    check("別の型である ★", not issubclass(C.ChargeOnHold, C.ChargeError))
    check("金額を持ち歩く", C.ChargeOnHold(1500, "太郎", "X").amount == 1500)

    print("\n[3] 保留を覚えて見張りに入れる ★")
    await C._remember_hold("r1", 777, "LINKCODE1", _info("ON_HOLD", 1200))
    held = await C.held_links()
    check("1件見張っている ★", len(held) == 1, held)
    check("金額を覚えている", held[0]["amount"] == 1200)
    check("誰のものか覚えている", held[0]["discord_id"] == 777)
    # ⚠️ 保留の状態値が何かは分かっていない。実際に来た値を残して学ぶ。
    check("相手が返した生の状態を残す ★", held[0]["link_status"] == "ON_HOLD",
          held[0]["link_status"])
    check("次に見る時刻が入る", held[0]["next_check_at"] is not None)

    print("\n[4] 何度貼っても二重にならない ★")
    # 利用者は焦って何度も貼る。
    await C._remember_hold("r2", 777, "LINKCODE1", _info("ON_HOLD", 1200))
    await C._remember_hold("r3", 777, "LINKCODE1", _info("ON_HOLD", 1200))
    check("1件のまま ★", len(await C.held_links()) == 1)

    print("\n[5] 受け取り済みを保留に戻さない ★")
    # ⚠️ 戻すと、すでに残高に入れたものをもう一度受け取りに行く。
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, "r1")
        row.status = C.CREDITED
    await C._remember_hold("r9", 777, "LINKCODE1", _info("ON_HOLD", 1200))
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, "r1")
        check("CREDITED のままである ★", row.status == C.CREDITED, row.status)

    print("\n[6] 解けたら受け取る ★")
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, "r1")
        row.status = C.HELD
        row.next_check_at = None

    calls = {"charge": 0}

    class FakeClient:
        def __init__(self, status): self.status = status
        async def link_check(self, code): return _info(self.status, 1200)
        async def aclose(self): pass

    class FakeHandle:
        def __init__(self, status):
            self.client = FakeClient(status)
            self.account_id = 1
        async def aclose(self): pass

    from services.paypay import accounts as PA

    async def fake_pick(amount, _st=["PENDING"]):
        return FakeHandle(_st[0])
    PA.pick_account = fake_pick

    async def fake_charge(discord_id, url, passcode=None):
        calls["charge"] += 1
        return C.ChargeResult(amount=1200, balance=1200, credited=1200)
    real_charge = C.charge_from_link
    C.charge_from_link = fake_charge

    out = await C.recheck_held()
    check("受け取りに進む ★", calls["charge"] == 1, calls)
    check("結果を返す", out and out[0][0] == "credited", out)
    check("見張りから消える ★", len(await C.held_links()) == 0)

    print("\n[7] まだ保留なら、間を空けて待つ ★")
    await C._remember_hold("r10", 888, "LINK2", _info("ON_HOLD", 500))
    async def still_pick(amount):
        return FakeHandle("ON_HOLD")
    PA.pick_account = still_pick
    async with session_scope() as s:
        (await s.get(PayPayReceipt, "r10")).next_check_at = None
    out = await C.recheck_held()
    check("まだ保留と分かる ★", out and out[0][0] == "still", out)
    held = await C.held_links()
    check("見張りに残る ★", len(held) == 1)
    check("回数が増える", held[0]["checks"] == 1, held[0]["checks"])
    check("次に見る時刻が先に延びる ★", held[0]["next_check_at"] is not None)

    print("\n[8] 取り消されたら諦めて知らせる ★")
    async def gone_pick(amount):
        return FakeHandle("CANCELED")
    PA.pick_account = gone_pick
    async with session_scope() as s:
        (await s.get(PayPayReceipt, "r10")).next_check_at = None
    out = await C.recheck_held()
    check("もう駄目だと分かる ★", out and out[0][0] == "gone", out)
    check("見張りから外れる", len(await C.held_links()) == 0)

    print("\n[9] 期限を過ぎたら諦める ★")
    # ⚠️ リンク自体に期限がある。いつまでも見に行っても無駄。
    await C._remember_hold("r11", 999, "LINK3", _info("ON_HOLD", 300))
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, "r11")
        row.created_at = utcnow() - timedelta(hours=C.HOLD_GIVE_UP_HOURS + 1)
        row.next_check_at = None
    out = await C.recheck_held()
    check("諦める ★", out and out[0][0] == "gone", out)
    async with session_scope() as s:
        row = await s.get(PayPayReceipt, "r11")
        check("理由が残る", "時間" in (row.error or ""), row.error)

    print("\n[10] 保留を本人に知らせる ★")
    # ⚠️ 画面に出す案内は本人にしか見えず、閉じると消える。保留が解ける
    #    のは送った側の操作次第で、何時間も先かもしれない。その間、
    #    手元に「待っている」記録が何も残らないのは不親切なのでDMも送る。
    await C._remember_hold("r20", 1234, "LINK9", _info("ON_HOLD", 800))
    check("1回目は送る ★", (await C.mark_notified("r20")) is True)
    # ⚠️ 焦って何度も貼る人がいる。DMは1回だけ。
    check("2回目は送らない ★", (await C.mark_notified("r20")) is False)
    check("3回目も送らない", (await C.mark_notified("r20")) is False)
    check("知らせた印が残る",
          [h for h in await C.held_links() if h["id"] == "r20"][0]["notified"])
    check("知らない受取IDでも落ちない", (await C.mark_notified("ないID")) is False)

    print("\n[11] 保留の例外が受取IDを持ち歩く ★")
    # これが無いと、どの保留について知らせたのか印を付けられない。
    held_id = await C._remember_hold("r21", 1234, "LINK10", _info("ON_HOLD", 900))
    check("覚えた行のIDを返す ★", held_id == "r21", held_id)
    # 同じリンクを貼り直したら、**最初の行のID**が返る（二重に作らない）
    again = await C._remember_hold("r22", 1234, "LINK10", _info("ON_HOLD", 900))
    check("貼り直しても同じIDを返す ★", again == "r21", again)
    e = C.ChargeOnHold(900, "太郎", "ON_HOLD", "r21")
    check("例外が受取IDを持つ ★", e.receipt_id == "r21")

    C.charge_from_link = real_charge
    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
