"""すべてのテストをまとめて実行する

⚠️ 時計を固定して走らせる。

   商品の提供時間帯は時刻で変わるため、固定しないと**実行した時刻で
   結果が変わる**。実際に、朝マックのセット(9030)を使うテストが
   05:25 に落ちた（08:00 なら通る）。時刻で結果が変わるテストは、
   本当のバグを隠すので無いほうがましである。

⚠️ 1つの時刻だけで走らせると、別の時刻でしか出ないバグを見逃す。
   別の時刻でも確かめられるように、環境変数が既にあればそれを使う。

       BOT_FAKE_JST="2026-10-06 14:00" python3 tests/run_all.py
       BOT_FAKE_JST="2026-10-06 23:30" python3 tests/run_all.py
"""
import subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))

# 既に指定があればそれを尊重する（別の時刻で確かめるため）
FAKE_JST = os.environ.setdefault("BOT_FAKE_JST", "2026-10-06 08:00")
print(f"⏰ 時計を {FAKE_JST} に固定して実行します"
      f"（別の時刻で試すには BOT_FAKE_JST を指定）")
TESTS = [
    ("モジュールの読み込み", "test_imports.py"),
    ("起動シーケンス・コマンド登録", "test_boot.py"),
    ("protobuf層（実HEXで検証）", "test_protocol.py"),
    ("メニュー解析（実データ247商品）", "test_menu.py"),
    ("暗号化・複式元帳・排他制御", "test_phase0.py"),
    ("DBの日時の扱い", "test_db_datetime.py"),
    ("相関IDと通信計測", "test_telemetry.py"),
    ("カート組み立て", "test_cart.py"),
    ("カートの編集と再開", "test_cart_edit.py"),
    ("セットの選択枠・時間帯", "test_menu_choices.py"),
    ("画面が作れること（全商品・全画面）", "test_views.py"),
    ("断られた組み合わせの学習とドリンク選び", "test_rejected.py"),
    ("選択枠の学習", "test_slot_rules.py"),
    ("複数個必須の選択枠", "test_slot_quantity.py"),
    ("数量のまとめ方と中間ノード", "test_quantity_and_bridge.py"),
    ("参照の無い選択枠（ハッピーセット）", "test_slot_hints.py"),
    ("注文できない商品の除外", "test_menu_filter.py"),
    ("具材の調整（抜き・増量）", "test_customize.py"),
    ("商品詳細と受取方法", "test_product_detail.py"),
    ("セット注文の組み立て", "test_set_order.py"),
    ("セットの金額（選んだ中身の差額）", "test_set_price.py"),
    ("店名検索", "test_store_search.py"),
    ("店舗の注文可否", "test_availability.py"),
    ("店舗一覧の定期同期", "test_store_sync.py"),
    ("利用者の操作フロー", "test_ui_flows.py"),
    ("使いやすさ", "test_usability.py"),
    ("チャージ処理", "test_charge.py"),
    ("口座ごとのチャージ率", "test_charge_rate.py"),
    ("アカウント登録（OTP）", "test_account_setup.py"),
    ("同時注文のアカウント割り当て", "test_account_pick.py"),
    ("決済カードとまとめ登録", "test_account_cards.py"),
    ("全スラッシュコマンドの総当たり", "test_all_commands.py"),
    ("管理者コマンド", "test_admin_cmds.py"),
    ("管理操作の記録", "test_audit.py"),
    ("アカウント健全性パネル", "test_admin_panel.py"),
    ("利用者カード", "test_user_card.py"),
    ("気になる動きの検知", "test_fraud.py"),
    ("一斉通知", "test_broadcast.py"),
    ("チャージ率・注文の条件", "test_limits.py"),
    ("紹介プログラム", "test_invite.py"),
    ("紹介プログラムの画面", "test_invite_ui.py"),
    ("バックアップと復元", "test_backup.py"),
    ("まるごとバックアップと項目復元", "test_full_backup.py"),
    ("再送の方針（冪等性）", "test_retry.py"),
    ("サーキットブレーカー", "test_breaker.py"),
    ("名前解決の控え", "test_dns.py"),
    ("並列化と下ごしらえ", "test_warmup.py"),
    ("メニュー同期", "test_menu_sync.py"),
    ("外形監視", "test_monitor.py"),
    ("注文の順番待ち", "test_queue.py"),
    ("注文Saga（端から端まで）", "test_order_e2e.py"),
    ("注文経路の総点検", "test_order_audit.py"),
    ("エラーの判別と対処", "test_errors.py"),
    ("代理実績の同一性", "test_achievement.py"),
    ("残高の増減パネル", "test_balance_panel.py"),
    ("控えの届け方", "test_delivery.py"),
    ("注文番号ページ", "test_web.py"),
    ("別置きの注文番号ページ", "test_web_remote.py"),
    ("定期処理・バックアップ", "test_tasks.py"),
    ("定期更新の総点検", "test_periodic.py"),
    ("通信を使う新しい機能", "test_new_features.py"),
    ("PayPayでのチャージ", "test_paypay.py"),
    ("PayPayの保留", "test_paypay_hold.py"),
    ("プロキシ（通信の出口）", "test_proxy.py"),
    ("サーバー管理（チケット・認証・監視・処分）", "test_server.py"),
    ("サーバー管理：コマンドとイベント", "test_server_cmds.py"),
    ("利用を広げる機能（声かけ・再注文・ランキング）", "test_growth.py"),
    ("人数が増えたときの挙動", "test_scale.py"),
    ("鍵が合わないとき", "test_key_mismatch.py"),
    ("コマンドの案内（/help）", "test_help.py"),
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
