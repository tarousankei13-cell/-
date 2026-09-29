"""
注文が失敗したときの扱いの検証

マクドナルドの応答は protobuf なので、そのまま見せても原因が分からない。
種類を判別して、利用者には対処を、管理者には詳細を伝えられるか確かめる。

特に大事なのは**カードが使えない場合**。そのアカウントを使い続けると
以降の注文が全部失敗し続けるので、すぐ候補から外す必要がある。
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from services.mcd import errors as E

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


# 実際に返ってきた応答（画像から書き起こしたもの）
REAL_422 = (
    "ErrorCode_ProductValidation\x12\x08products ErrorCode_ProductValidation*\x1a"
    "(type.googleapis.com/mcdord.ProductsError~\x12| x 1110 9003 > 1110 "
    "product not found\"Qただいまのお時間は選択した商品のお取り扱いがありません"
    "2<選択された商品は"
)


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/err.db")

    print("\n[1] 実際の応答を読み解く")
    i = E.parse(422, REAL_422)
    check("種類を判別できる", i.kind == E.PRODUCT_TIME, i.kind)
    check("日本語の文言を取り出せる",
          i.message == "ただいまのお時間は選択した商品のお取り扱いがありません", i.message)
    check("protobufの区切りを文言に混ぜない",
          "2<" not in i.message and not i.message.endswith("2"), i.message)
    check("エラー符号を拾える", i.code == "ErrorCode_ProductValidation", i.code)
    check("アカウントの問題ではないと判断する", not i.account_fault)
    check("利用者には選び直しを案内する", "選び直して" in i.user_text, i.user_text[:60])

    print("\n[2] 決済が拒否された場合")
    i = E.parse(422, "ErrorCode_PaymentDeclined payment declined: insufficient funds "
                     "クレジットカードのご利用限度額を超えています")
    check("決済の問題と判別する", i.kind == E.PAYMENT, i.kind)
    check("アカウントの問題と判断する ★", i.account_fault)
    check("管理者にはカードを確認するよう伝える",
          "カード" in i.admin_text and "残高" in i.admin_text, i.admin_text[:60])
    check("利用者にはカードの事情を見せない",
          "カード" not in i.user_text and "残高は元に戻" in i.user_text, i.user_text[:60])

    print("\n[3] その他の種類")
    for body, status, want in [
        ("unauthenticated: token expired", 401, E.AUTH),
        ("ErrorCode_Card: card not found", 422, E.CARD),
        ("この店舗は営業時間外です", 422, E.STORE),
        ("\x00\x01\x02", 500, E.UNKNOWN),
    ]:
        i = E.parse(status, body)
        check(f"{want} と判別する（HTTP {status}）", i.kind == want, i.kind)

    print("\n[4] 壊れた応答でも落ちない")
    for body in [b"", b"\xff\xfe\x00", "あ", "x" * 5000, b"\x00" * 100]:
        try:
            i = E.parse(500, body)
            err = None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        check(f"{str(body)[:14]!r} を処理できる", err is None, err or "")

    print("\n[5] 利用者に見せる文に内部の事情を出さない")
    for kind_body in [
        "ErrorCode_PaymentDeclined insufficient funds",
        "unauthenticated token expired",
        "ErrorCode_Card card not found",
    ]:
        i = E.parse(422, kind_body)
        leaked = [w for w in ("ErrorCode", "token", "insufficient", "card not found")
                  if w in i.user_text]
        check(f"{i.kind}: 内部の語を含まない", not leaked, leaked)

    print("\n[6] カードが使えないアカウントはすぐ外す ★")
    from db.models import McdAccount
    from services.mcd import accounts as acc_mod
    async with session_scope() as s:
        for i_ in (1, 2):
            s.add(McdAccount(
                id=i_, label=f"acc{i_}", email_enc=b"x", status="ACTIVE", card_id="card",
                device_uid="d", wmop_device_id="w", fb_instance_id="f",
                home_lat=35.0, home_lng=139.0,
            ))
    st = await acc_mod.report_failure(1, "決済が拒否されました", fatal=True)
    check("1回でも隔離される", st == acc_mod.STATUS_QUARANTINED, st)
    st = await acc_mod.report_failure(2, "一時的な通信エラー")
    check("ふつうの失敗では隔離しない", st == acc_mod.STATUS_ACTIVE, st)

    print("\n[7] 隔離したアカウントは選ばれない")
    handle = await acc_mod.pick_account()
    check("残っているアカウントが選ばれる", handle.account_id == 2, handle.account_id)
    await handle.aclose()

    print("\n[8] 失敗の結果から利用者向けの文が出る")
    from core import saga
    r = saga.OrderResult(order_id="x", state=saga.REFUNDED)
    check("解析が無ければ無難な文", "管理者" in r.user_message, r.user_message[:40])
    r.error_info = E.parse(422, REAL_422)
    check("解析があればその案内", "選び直して" in r.user_message, r.user_message[:60])

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
