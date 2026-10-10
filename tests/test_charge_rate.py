"""口座ごとのチャージ率

⚠️ Kyash と PayPay で率を変えられる。**設定が無ければ共通の値に戻る**。
   片方だけ決めても、もう片方が壊れないこと。

⚠️⚠️ **記帳する側に口座の種類を渡し忘れないこと。**
   渡し忘れると共通の率が使われ、設定していても効かない。しかも
   エラーにならず、静かに違う額を記帳する。いちばん気づきにくい。
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import limits, settings
from core.crypto import init_cipher
from db.session import close_db, init_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main() -> int:
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/r.db")
    await settings.load_all()

    print("\n[1] 設定が無ければ共通の値 ★")
    await settings.set_value("charge_rate", 110)
    check("共通 110%", limits.charge_rate() == 110, limits.charge_rate())
    check("Kyash も 110%", limits.charge_rate("kyash") == 110)
    check("PayPay も 110%", limits.charge_rate("paypay") == 110)
    check("個別設定は入っていない",
          not limits.rate_is_set("kyash") and not limits.rate_is_set("paypay"))

    print("\n[2] 片方だけ決めても、もう片方が壊れない ★")
    await settings.set_value("charge_rate_kyash", 150)
    check("Kyash 150%", limits.charge_rate("kyash") == 150, limits.charge_rate("kyash"))
    check("PayPay は共通のまま 110% ★", limits.charge_rate("paypay") == 110,
          limits.charge_rate("paypay"))
    check("共通は 110% のまま", limits.charge_rate() == 110)
    check("Kyash だけ個別と分かる ★",
          limits.rate_is_set("kyash") and not limits.rate_is_set("paypay"))

    print("\n[3] 金額の計算 ★")
    check("Kyash 1000 → 1500", limits.credited_for(1000, "kyash") == 1500,
          limits.credited_for(1000, "kyash"))
    check("PayPay 1000 → 1100", limits.credited_for(1000, "paypay") == 1100,
          limits.credited_for(1000, "paypay"))
    check("上乗せぶんも口座ごと", limits.bonus_for(1000, "kyash") == 500)
    # ⚠️ 端数は切り捨て。切り上げると毎回1円ずつ運営の持ち出しになる。
    await settings.set_value("charge_rate_paypay", 133)
    check("端数は切り捨て（333×133% = 442）★",
          limits.credited_for(333, "paypay") == 442,
          limits.credited_for(333, "paypay"))

    print("\n[4] 壊れた設定でも落ちない ★")
    await settings.set_value("charge_rate_paypay", "あいう")
    check("読めない値なら共通に戻る ★", limits.charge_rate("paypay") == 110,
          limits.charge_rate("paypay"))
    await settings.set_value("charge_rate_paypay", 0)
    # ⚠️ 0 を許すと、いくら送っても残高が増えないのに受け取りだけ成立する
    check("0% は許さない ★", limits.charge_rate("paypay") >= 1,
          limits.charge_rate("paypay"))
    await settings.set_value("charge_rate_paypay", -50)
    check("マイナスも許さない ★", limits.charge_rate("paypay") >= 1)
    check("知らない口座名でも落ちない", limits.charge_rate("なにか") == 110)
    await settings.set_value("charge_rate_paypay", 100)

    print("\n[5] 記帳する側が口座の種類を渡しているか ★")
    # ⚠️ ここが本丸。渡し忘れると静かに違う額を記帳する。
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for f, prov in (("services/kyash/charge.py", "kyash"),
                    ("services/paypay/charge.py", "paypay")):
        src = (root / f).read_text()
        check(f"{prov}: charge_rate に口座名を渡している ★",
              f'charge_rate("{prov}")' in src, f)
        check(f"{prov}: credited_for に口座名を渡している ★",
              f'credited_for(info.amount, "{prov}")' in src, f)
        check(f"{prov}: 引数なしで呼んでいない ★",
              "limits.charge_rate()" not in src and
              "limits.credited_for(info.amount)" not in src, f)

    print("\n[6] 画面に口座ごとの率が出るか ★")
    # ⚠️ Kyash 150% / PayPay 100% のときに「150%増量中」とだけ出すと、
    #    PayPayで送った人が「話が違う」ことになる。
    from ui import embeds
    import ui.flows as F
    real = F.charge_methods
    F.charge_methods = lambda: ["kyash", "paypay"]
    try:
        d = embeds.charge_panel().description or ""
        check("率が違うときは口座名を添えて出す ★",
              "Kyash" in d and "PayPay" in d and "150%" in d, d[:200])
        check("片方だけの率を全体のように見せない ★",
              not (("150% 増量中" in d) and "PayPay" not in d), d[:200])
        # 両方そろえたら、まとめて1行でよい
        await settings.set_value("charge_rate_paypay", 150)
        d2 = embeds.charge_panel().description or ""
        check("両方同じならまとめて出す", "150% 増量中" in d2, d2[:200])
        # 増量していなければ何も出さない
        await settings.set_value("charge_rate", 100)
        await settings.set_value("charge_rate_kyash", 100)
        await settings.set_value("charge_rate_paypay", 100)
        d3 = embeds.charge_panel().description or ""
        check("100%なら増量の話をしない ★", "増量" not in d3, d3[:160])
    finally:
        F.charge_methods = real

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
