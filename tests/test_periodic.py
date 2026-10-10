"""
定期更新の総点検

通信する機能が、ちゃんと定期的に更新されるようになっているかを確かめる。

ここで守りたいのは2つ。
  ・ループを足したのに **起動し忘れる** こと
    （その機能だけ黙って更新されなくなり、気付くのは利用者が
      古い情報で注文に失敗したとき）
  ・外に出る機能のどれかが、どのループにも面倒を見られていないこと
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


# 通信する機能と、その面倒を見るループ。
# ⚠️ 外に出る機能を足したら、ここにも足すこと。
COVERAGE = {
    "メニューカタログ":            "menu_sync",
    "店舗一覧":                    "store_index_sync",
    "マクドナルドのトークン":      "token_warm",
    "マクドナルドの口座の生存":    "health_check",
    "Kyashの口座の生存":           "health_check",
    "PayPayの口座の生存":          "health_check",
    "Kyashのトークン期限の通知":   "hourly_checks",
    "外形監視（障害検知）":        "outage_watch",
    "バックアップの外部保存":      "hourly_checks",
    "元帳の整合性":                "hourly_checks",
    "日次・月次リセット":          "hourly_checks",
    "不正利用の見張り":            "hourly_checks",
    "できあがり通知":              "ready_watch",
    "入金の照合（Kyash）":         "hourly_checks",
    "新商品・価格改定のお知らせ":  "menu_sync",
}


async def main():
    import config
    from core.crypto import init_cipher
    from db.session import init_db, close_db
    from discord.ext import tasks as dtasks
    import cogs.tasks as tasks_cog
    from _fake_discord import FakeClient

    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/p.db")
    from core import settings
    await settings.load_all()

    cog = tasks_cog.TasksCog(FakeClient())
    declared = {
        name for name, v in vars(tasks_cog.TasksCog).items()
        if isinstance(v, dtasks.Loop)
    }

    print("── ループを取りこぼしていないか ──")
    check(f"ループが{len(declared)}本ある", len(declared) >= 6, sorted(declared))
    found = {l.coro.__name__ for l in cog._loops()}
    check("宣言したループを全部拾える ★", found == declared,
          sorted(declared ^ found))

    print("\n── 全部きちんと起動されるか ──")
    cog._started = True
    cog._start_loops()
    running = {l.coro.__name__ for l in cog._loops() if l.is_running()}
    check("全部走り出す ★", running == declared, sorted(declared - running))

    print("\n── 止めるときに全部止まるか ──")
    await cog.cog_unload()
    await asyncio.sleep(0)
    left = {l.coro.__name__ for l in cog._loops() if l.is_running()}
    check("全部止まる ★", not left, sorted(left))

    print("\n── 間隔が妥当か ──")
    # ⚠️ できあがり通知だけは1分より短くてよい。注文直後の数十分しか
    #    動かず、機能が無効なら毎回すぐ戻る（通信もしない）。
    FAST_OK = {"ready_watch"}
    for loop in cog._loops():
        name = loop.coro.__name__
        secs = (loop.seconds or 0) + (loop.minutes or 0) * 60 + (loop.hours or 0) * 3600
        low = 15 if name in FAST_OK else 60
        reasonable = low <= secs <= 24 * 3600
        check(f"{name} は {secs:.0f}秒ごと", reasonable, secs)

    print("\n── 準備完了を待つか（待たないと静かに死ぬ）──")
    for loop in cog._loops():
        name = loop.coro.__name__
        check(f"{name} は準備を待つ ★", loop._before_loop is not None, name)

    print("\n── 通信する機能がどれも放置されていないか ──")
    for feature, loop_name in COVERAGE.items():
        check(f"{feature} → {loop_name}", loop_name in declared, loop_name)

    print("\n── 実際に通信する関数がそろっているか ──")
    from services import tasks as jobs
    from services.mcd import accounts as mcd_accounts
    from services.kyash import accounts as kyash_accounts
    check("メニュー同期がある", callable(getattr(jobs, "sync_all_menus", None)))
    check("トークン事前更新がある", callable(getattr(jobs, "warm_tokens", None)))
    check("マクドナルドの生存確認がある",
          callable(getattr(mcd_accounts, "healthcheck_all", None)))
    check("Kyashの生存確認がある ★",
          callable(getattr(kyash_accounts, "healthcheck_all", None)))
    check("Kyashの復帰処理がある",
          callable(getattr(kyash_accounts, "report_success_healthcheck", None)))
    check("Kyashの期限通知がある",
          callable(getattr(jobs, "kyash_token_warnings", None)))
    from services import order_watch
    from services.kyash import reconcile, refund
    check("できあがりの確認がある ★", callable(getattr(order_watch, "sweep", None)))
    check("入金の照合がある ★", callable(getattr(reconcile, "check", None)))
    check("返金がある ★", callable(getattr(refund, "send", None)))
    check("利用者向けのメニュー告知がある ★",
          callable(getattr(jobs, "format_menu_news", None)))
    check("できあがり通知は既定で無効 ★", not order_watch.enabled())
    check("返金は既定で無効 ★", not refund.enabled())
    from services.paypay import accounts as pp_accounts, charge as pp_charge
    check("PayPayの生存確認がある ★",
          callable(getattr(pp_accounts, "healthcheck_all", None)))
    check("PayPayのチャージがある ★",
          callable(getattr(pp_charge, "charge_from_link", None)))
    check("PayPayの期限通知もある ★",
          callable(getattr(pp_accounts, "expiring_accounts", None)))

    print("\n── 名前解決の控えが対象を網羅しているか ──")
    from core import dns
    hosts = dns.known_hosts()
    check(f"ホストが{len(hosts)}件ある", len(hosts) >= 10, len(hosts))
    check("マクドナルドの認証が入っている",
          any("authorization" in h for h in hosts))
    check("Kyash が入っている", any("kyash" in h for h in hosts))
    check("カタログが6グループぶん入っている",
          len([h for h in hosts if h.startswith("data.cat.group-")]) == 6,
          [h for h in hosts if h.startswith("data.cat.group-")])
    check("注文が6グループぶん入っている",
          len([h for h in hosts if h.startswith("ord.group-")]) == 6)
    check("控えの寿命が決めてある", 0 < dns.TTL <= 3600, dns.TTL)

    print("\n[起動時の復旧が、口座の種類ごとに漏れていないか ★]")
    # ⚠️ 受け取りの途中で落ちた行は、放っておくと誰も見ない。
    #    「送ったのに残高に入らない」が残るだけになる。
    #    Kyash だけ書いて PayPay を忘れていた（実際の抜け）。
    #    口座を増やしたときに同じ抜けが起きないよう、ここで見る。
    import inspect

    import cogs.tasks as _T

    src = inspect.getsource(_T.TasksCog)
    for mod in ("services.kyash.charge", "services.paypay.charge"):
        check(f"{mod} の復旧を呼ぶ ★", mod in src)
    for mod in ("services.kyash.charge", "services.paypay.charge"):
        import importlib
        m = importlib.import_module(mod)
        check(f"{mod}.recover_pending がある ★",
              callable(getattr(m, "recover_pending", None)))

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
