"""すべてのテストをまとめて実行する"""
import subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = [
    ("モジュールの読み込み", "test_imports.py"),
    ("起動シーケンス・コマンド登録", "test_boot.py"),
    ("protobuf層（実HEXで検証）", "test_protocol.py"),
    ("メニュー解析（実データ247商品）", "test_menu.py"),
    ("暗号化・複式元帳・排他制御", "test_phase0.py"),
    ("カート組み立て", "test_cart.py"),
    ("店名検索", "test_store_search.py"),
    ("利用者の操作フロー", "test_ui_flows.py"),
    ("チャージ処理", "test_charge.py"),
    ("注文Saga（端から端まで）", "test_order_e2e.py"),
    ("代理実績の同一性", "test_achievement.py"),
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
