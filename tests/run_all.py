"""すべてのテストをまとめて実行する"""
import subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = [
    ("モジュールの読み込み", "test_imports.py"),
    ("起動シーケンス・コマンド登録", "test_boot.py"),
    ("protobuf層（実HEXで検証）", "test_protocol.py"),
    ("メニュー解析（実データ247商品）", "test_menu.py"),
    ("暗号化・複式元帳・排他制御", "test_phase0.py"),
    ("DBの日時の扱い", "test_db_datetime.py"),
    ("相関IDと通信計測", "test_telemetry.py"),
    ("カート組み立て", "test_cart.py"),
    ("セットの選択枠・時間帯", "test_menu_choices.py"),
    ("注文できない商品の除外", "test_menu_filter.py"),
    ("具材の調整（抜き・増量）", "test_customize.py"),
    ("セット注文の組み立て", "test_set_order.py"),
    ("店名検索", "test_store_search.py"),
    ("店舗の注文可否", "test_availability.py"),
    ("店舗一覧の定期同期", "test_store_sync.py"),
    ("利用者の操作フロー", "test_ui_flows.py"),
    ("使いやすさ", "test_usability.py"),
    ("チャージ処理", "test_charge.py"),
    ("アカウント登録（OTP）", "test_account_setup.py"),
    ("管理者コマンド", "test_admin_cmds.py"),
    ("管理操作の記録", "test_audit.py"),
    ("アカウント健全性パネル", "test_admin_panel.py"),
    ("利用者カード", "test_user_card.py"),
    ("気になる動きの検知", "test_fraud.py"),
    ("一斉通知", "test_broadcast.py"),
    ("バックアップと復元", "test_backup.py"),
    ("再送の方針（冪等性）", "test_retry.py"),
    ("サーキットブレーカー", "test_breaker.py"),
    ("名前解決の控え", "test_dns.py"),
    ("並列化と下ごしらえ", "test_warmup.py"),
    ("メニュー同期", "test_menu_sync.py"),
    ("外形監視", "test_monitor.py"),
    ("注文の順番待ち", "test_queue.py"),
    ("注文Saga（端から端まで）", "test_order_e2e.py"),
    ("エラーの判別と対処", "test_errors.py"),
    ("代理実績の同一性", "test_achievement.py"),
    ("残高の増減パネル", "test_balance_panel.py"),
    ("控えの届け方", "test_delivery.py"),
    ("定期処理・バックアップ", "test_tasks.py"),
    ("安全性（認証情報・権限）", "test_security.py"),
]

results = []
for title, script in TESTS:
    print(f"\n{'━' * 56}\n▶ {title}\n{'━' * 56}")
    r = subprocess.run([sys.executable, os.path.join(HERE, script)],
                       capture_output=True, text=True)
    out = r.stdout
    passed = failed = 0
    for line in out.splitlines():
        if "成功 " in line and "/ 失敗 " in line:
            try:
                passed = int(line.split("成功 ")[1].split(" ")[0])
                failed = int(line.split("失敗 ")[1].strip())
            except (ValueError, IndexError):
                pass
    for line in out.splitlines():
        if line.strip().startswith(("✅", "❌", "[")):
            print(line)
    if r.returncode != 0 and failed == 0:
        failed = 1
        print(r.stderr[-800:])
    results.append((title, passed, failed))

print(f"\n{'═' * 56}\n  まとめ\n{'═' * 56}")
tp = tf = 0
for title, p, f in results:
    mark = "✅" if f == 0 else "❌"
    print(f"  {mark} {title:<34} {p:>3} 件成功" + (f" / {f} 件失敗" if f else ""))
    tp += p; tf += f
print(f"{'─' * 56}\n  合計 {tp} 件成功" + (f" / {tf} 件失敗" if tf else " / 失敗なし"))
sys.exit(1 if tf else 0)
