# 高性能 Kyash 自動チャージ Discord Bot / 運用ドキュメント

Discord サーバーごとに独立した **内部残高** を管理するチャージ Bot です。
利用者は常設チャージパネルから金額を入力し、Kyash の **送金リンク** を提出します。
Bot は管理者が登録した **受取用 Kyash アカウント** でリンクを検証・受取し、
サーバーごとのチャージ率を適用して内部残高を付与します。

> 内部残高は Discord サーバー内の数値です。現金化・出金・外部商品との交換には対応しません。
> サーバー内のロール購入 (VIP など) と招待キャンペーンの報酬にのみ使用します。

**v2 の主な追加機能**
- VIP ロールショップ (内部残高で購入 → ロール付与 → **そのロールでチャージ率が上がる**)
- 招待キャンペーン (Bot 発行の個人専用リンク / チャージ完了で報酬確定 / 多層の不正対策)
- 管理者向けの詳細な残高操作 (付け替え・操作取消・突合・修復・一括・台帳検索)
- 残高操作ログチャンネル / 監査 CSV 出力 / 日次サマリ自動投稿
- 管理ダッシュボード (常設・自動更新) / 統合調査ビュー `/inspect`
- 期間別ランキング (週間・月間・招待) / 設定の入出力 / 期限付きサーバー許可
- トークン失効の事前警告 / 受取アカウント残高しきい値 / 連続失敗クールダウン
- 死活監視 (ハートビート) / バックアップの外部保存 / GitHub Actions による自動テスト

**v4.0 の追加機能**

v4 は「サーバーを続けて使ってもらう仕組み」と「運用者が異変に気付ける仕組み」を足しました。

- **累計チャージによる自動昇格 (段位)**
  累計額のしきい値ごとにロールを自動付与。一度得た段位は下がりません
  (`/tier add|remove|list|sweep`)
- **ランキング報酬の自動配布**
  締めた週・月のランキング上位へ残高とロールを自動で配ります。
  同じ期間に二度配らないよう配布記録で守ります (`/ranking_reward set|remove|list|run`)
- **受取用 Kyash アカウントの複数登録**
  1台が残高しきい値に達してもチャージが止まりません。優先度順に余裕のある
  アカウントを選び、全台が満杯のときだけ停止します (`/kyash add|remove|priority`)
- **ショップの商品タイプ拡張とサブスク**
  ロール付与に加えて「カスタムロール作成」「ニックネーム変更」「チャージ率ブースト」
  「専用チャンネル」を販売できます。期限つき商品は**自動更新 (サブスク)** にもできます
  (`/shop add type:... subscription:True` / `/shop cancel` / `/shop subscriptions`)
- **オークション**
  内部残高でロールを競り落とせます。入札した分はその場で預かり、上回られたら
  全額を即返金。締切直前の入札では締切を延長します
  (`/auction create|bid|list|close|cancel|bids`)
- **サーバー全体のチャージ目標**
  みんなで目標額を目指し、達成したら**期間中にチャージした全員**へ報酬を配ります。
  進捗パネルは自動更新です (`/goal create|panel|progress|list|close|cancel|grants`)
- **不正の兆候の自動検知**
  短時間の大量チャージ / 送金者名の共有 / 使い切って退出 / 招待報酬のみ /
  返金の繰り返し を検知して審査チャンネルへ知らせます。
  **検知しても自動で凍結・処分はしません** (判断は必ず人が行います)
  (`/fraud list|resolve|ignore|scan|thresholds`)
- **日別推移のグラフ画像**
  チャージの推移を画像で確認できます (`/stats days:14`)。日次サマリにも添付します。
  描画は Pillow のみで行い、集計データを外部サービスへ送りません
- **返金申請フロー**
  利用者が `/refund request` で申請し、管理者が審査します。承認すると内部残高を
  取り消します。**Kyash への実際の返金は管理者の手作業**です (自動送金は行いません)
- **署名つきレシート**
  `/receipt show` でチャージの控えを発行できます。`/receipt verify` は署名に加えて
  **いまの記録との一致**も確認するため、取消済みの控えは「無効」と判定します

**v3.1 の追加機能・修正**
- **Kyash 請求リンク**でのチャージを追加。Bot が金額入りの請求リンクを発行し、
  利用者は**開いて支払うだけ**。金額の打ち間違いが原理的に起きず、承認も不要
- 招待リンク / LTC アドレス / PayPay ID を**コピー用ブロック + タップできる素のリンク**の
  両方で表示 (コードブロック内の URL はタップできないため)
- **コマンドが2つずつ表示される問題を修正** (グローバルとギルドの二重登録)。
  既に登録済みの重複は起動時に自動削除します
- 重複・冗長なコマンドを整理 (118 → 115)
- **請求リンクで他人の支払いを自分のものと誤認する不具合を修正**
  (残高差分ではなく履歴の一致を必須にした)
- **`Authorization: Bearer <token>` がログでマスクされない不具合を修正**

**v3.0 の追加機能 (PayPay / Litecoin チャージ)**
- **PayPay** と **Litecoin (LTC)** でもチャージできるようになりました
  (どちらも「利用者が申請 → 管理者が承認」の承認制)
- **方式ごとにチャージ率・金額上下限・受付の有効/無効**を設定できます
- LTC は **CoinGecko API でその時のレートを取得**し、案内した時点の単価を申請に固定します
  (取得できないときは固定価格へフォールバック、どちらも無ければ LTC の受付を停止)
- 申請は**審査チャンネルに承認/却下ボタン付きのカード**として投稿されます
- 承認された申請は **Kyash と同じ残高付与経路**を通るため、二重付与防止・実績投稿・
  ランキング・招待報酬の確定・監査ログがそのまま働きます
- 同じ取引ID / txid は**二度使えません** (DB の UNIQUE 制約で担保)

**v2.1 の変更点 (UI の分かりやすさとバグ修正)**
- チャージが **3ステップ表示** になり、「いま何をすればよいか」が常に見えます
  (`ステップ 1/3 金額入力` → `2/3 リンク送信` → `3/3 受取処理中`)
- すべてのエラー表示に **「▶ 次にどうすればいいですか？」** を追加 (37 種すべて)
- **ショップ購入も実績チャンネルへ投稿**。返金・期限切れのときは同じ投稿を更新します
- 購入前に **残高不足・在庫切れ・購入上限・所持済み** を理由つきで表示 (押してから失敗しない)
- 残高・ヘルプ・履歴・ショップ・招待パネルの文面を手順つきに書き換え
- 招待キャンペーン作成時のエラー (`TypeError: _confirm() ... 'danger'`) を修正
- `/server allow` / `/server sync` がコマンド同期の失敗で落ちる問題を修正
- Embed が Discord の上限を超えたときに無言で失敗しないよう、送信前に自動で切り詰め
- 静的解析 (mypy) を 0 エラーにし、同種の引数ミスを検出できるようにしました

- Bot バージョン: `4.0.0` / DB スキーマ: `v4` (v1 / v2 / v3 の DB は起動時に自動移行)
- 想定環境: Python 3.11+ / discord.py 2.x / SQLite (WAL) / Linux VPS + systemd
- Kyash 連携: 同梱の添付モジュール `vendor/Kyasher` (Kyasher 1.5.0) のみを使用
- PayPay / LTC: **外部 API は使いません** (LTC の価格取得のみ CoinGecko を参照)。
  着金確認は管理者が自分の目で行う承認制です

---

## C. ディレクトリ構成

```
discord-charge-bot/
├── main.py                     # Token/Owner/Bootstrap の直書き・Bot本体・起動/終了・パネル操作ハンドラ
├── config.py                   # 定数・デフォルト値・状態定義・エラーコード・配色
├── utils.py                    # 金額(Decimal)・リンク正規化/ハッシュ・マスキング・レート制限・トークン暗号化
├── database.py                 # SQLite スキーマ / マイグレーション / 全クエリ / 冪等な残高付与
├── kyash_service.py            # 添付モジュールの安全な抽象化 (直列化・タイムアウト・受取確認)
├── price_service.py            # LTC/JPY 価格の取得 (厳格な検証・キャッシュ・異常値ガード)
├── charge_service.py           # チャージ・ショップ・招待・残高操作・通知・ランキング・
│                               # 段位・オークション・目標・不正検知・返金申請・レシート
├── chart.py                    # 日別推移のグラフ描画 (Pillow のみ・外部送信なし)
├── ui.py                       # Embed / Persistent View / Modal
├── commands.py                 # スラッシュコマンド (Owner / Admin / 利用者)
├── tasks.py                    # バックグラウンドタスク (キューワーカー等) + 自己復旧監視
├── requirements.txt
├── discord-charge-bot.service  # systemd unit
├── tests/
│   ├── test_charge_flow.py     # 統合テスト: チャージ本体 (91項目)
│   ├── test_v2_features.py     # 統合テスト: ショップ/招待/残高操作/実績投稿 (89項目)
│   ├── test_v4_features.py     # 統合テスト: v4 の全機能 (278項目)
│   ├── test_migration.py       # 旧DBの自動移行 v1→v4 (29項目)
│   ├── test_providers.py       # PayPay / LTC の申請・承認 (75項目)
│   ├── test_claim_link.py      # Kyash 請求リンクの支払い (56項目)
│   ├── test_ui_render.py       # 全 Embed の描画と文字数上限 (290項目)
│   ├── test_commands_smoke.py  # 全コマンド/全ボタンの実行と権限 (225項目)
│   ├── test_concurrency.py     # 同時実行でお金が増減しないこと (26項目)
│   └── test_static_audit.py    # 静的監査: 起動せずに不整合を検出 (95項目)
├── .github/workflows/test.yml  # CI (lint + 起動前チェック + 全テスト)
├── vendor/
│   └── Kyasher/                # 添付モジュール (無変更で同梱・監査用)
└── data/                       # 実行時に生成 (DB / バックアップ / 鍵)
    ├── charge_bot.db
    ├── secret.key              # 0600  Kyash トークンの暗号化キー
    ├── receipt.key             # 0600  レシート署名の鍵 (別ファイルで保持)
    └── backups/
```

---

## D. VPS セットアップ手順

### 1. Python と依存パッケージ

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git

python3.11 --version   # 3.11 以上であることを確認
```

### 2. 実行用ユーザーとファイル配置

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin chargebot
sudo mkdir -p /opt/discord-charge-bot
sudo chown chargebot:chargebot /opt/discord-charge-bot

# ファイル一式を /opt/discord-charge-bot へ配置 (git clone / scp など)
sudo -u chargebot git clone <このリポジトリ> /opt/discord-charge-bot
```

### 3. venv と pip install

```bash
cd /opt/discord-charge-bot
sudo -u chargebot python3.11 -m venv venv
sudo -u chargebot ./venv/bin/pip install --upgrade pip
sudo -u chargebot ./venv/bin/pip install -r requirements.txt
```

### 4. main.py の3項目を書き換える

`main.py` の先頭にある以下の3つだけを直接編集します (`.env` は使用しません)。

```python
DISCORD_BOT_TOKEN = "あなたのBotトークン"
BOT_OWNER_ID = 123456789012345678      # あなたのDiscordユーザーID
BOOTSTRAP_GUILD_ID = 987654321098765432  # 最初に許可するサーバーID (0なら後から /server allow)
```

```bash
sudo chmod 600 /opt/discord-charge-bot/main.py     # Token を含むため保護する
sudo chown chargebot:chargebot /opt/discord-charge-bot/main.py
```

### 5. DB 初期化 (初回起動で自動作成)

DB・テーブル・インデックスは起動時に自動作成されます。手動確認する場合:

```bash
cd /opt/discord-charge-bot
sudo -u chargebot ./venv/bin/python3 -c "
import asyncio, config, database
asyncio.run(database.Database(config.DB_PATH).connect())
print('DB OK:', config.DB_PATH)"
```

### 6. フォアグラウンドで起動確認

```bash
cd /opt/discord-charge-bot
sudo -u chargebot ./venv/bin/python3 main.py
```

起動前チェックで問題があれば原因を列挙して安全停止します。
`ログインしました: ...` が出れば成功です (Ctrl+C で停止)。

### 7. systemd 登録

```bash
sudo cp /opt/discord-charge-bot/discord-charge-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/discord-charge-bot.service   # User/パスを環境に合わせる
sudo systemctl daemon-reload
sudo systemctl enable discord-charge-bot
sudo systemctl start discord-charge-bot
sudo systemctl status discord-charge-bot
```

### 8. ログ確認

```bash
journalctl -u discord-charge-bot -f            # 追尾
journalctl -u discord-charge-bot --since today # 当日分
journalctl -u discord-charge-bot -p err        # エラーのみ
```

### 9. Discord Developer Portal 側の設定

- **Bot → Privileged Gateway Intents → SERVER MEMBERS INTENT を ON**
  (ランキングでの Bot 除外・表示名解決に使用します。MESSAGE CONTENT INTENT は不要)
- OAuth2 の招待スコープ: `bot` + `applications.commands`
- 必要権限 (最小): `View Channel` / `Send Messages` / `Embed Links` / `Read Message History`
  ※ Administrator 権限は不要です。

---

## E. Discord 初期設定 (実行順)

| 順 | 実行者 | コマンド | 内容 |
|---|---|---|---|
| 1 | Bot Owner | `/server allow` | サーバーの利用を許可 (Bootstrap 指定済みなら不要) |
| 2 | Bot Owner | `/kyash login` | 受取用 Kyash アカウントにログイン (メール+パスワード→SMS認証コード) |
| 3 | 管理者 | `/settings admin_role @ロール` | 管理者ロールを設定 |
| 4 | 管理者 | `/settings charge_rate 130` | チャージ率 (%) |
| 5 | 管理者 | `/settings charge_min 100` / `charge_max 50000` | 金額制限 |
| 6 | 管理者 | `/settings daily_limit 100000` | 1ユーザーの日次上限 |
| 7 | 管理者 | `/settings achievement_channel #charge-log` | 実績チャンネル |
| 8 | 管理者 | `/settings log_channel #bot-log` | ログチャンネル |
| 9 | 管理者 | `/charge_panel` | チャージパネルを設置 (実行したチャンネル) |
| 10 | 管理者 | `/ranking_panel #ranking` | ランキングパネルを設置 (**チャージパネルとは別**) |
| 11 | 管理者 | `/setup` | 未設定項目がないか確認 |

> `/kyash login` は Bot Owner 専用です (受取用アカウントは Bot 全体で共有する資源のため)。
> 入力は Ephemeral な Modal で受け取り、**パスワードは保存せず**、
> アクセストークンのみ `data/secret.key` で暗号化して保存します。OTP は保存もログ出力もしません。

---

## F. 管理者コマンド一覧

### Bot Owner 専用

| コマンド | 内容 |
|---|---|
| `/server allow [guild_id] [note]` | サーバーの利用を許可 (許可直後にコマンドを同期) |
| `/server deny <guild_id> [reason]` | サーバーの利用を禁止 (確認ボタンあり・データは保持) |
| `/server list` | 許可状態の一覧 (未登録で参加中のサーバーも表示) |
| `/server sync` | スラッシュコマンドの再同期 |
| `/kyash login` | 受取用アカウントへログイン (Modal → 必要なら SMS 認証コード) |
| `/kyash logout` | ログアウト (確認ボタン・保存済み認証情報を削除) |
| `/kyash reconnect` | 保存情報でセッションを再確認し受取を再開 |
| `/server suspend <guild_id> <until>` | 期限付き許可 (期限切れで自動的に利用不可) |
| `/data delete [guild_id]` | サーバーデータの完全削除 (**2段階確認** + 削除前バックアップ) |
| `/backup` | DB バックアップの手動実行 |
| `/stats global_scope:True` | Bot 全体の統計 |
| `/global overview` | 全サーバー横断の状況一覧 |
| `/global stats` | Bot 全体の統計とメトリクス |
| `/kyash threshold <amount> [account]` | 受取用アカウントの残高しきい値 |
| `/kyash add <label> [threshold] [priority]` | 受取用アカウントを追加 (v4) |
| `/kyash remove <account>` | 受取用アカウントを削除 (v4) |
| `/kyash priority <account> <priority>` | 受取に使う順番を変更 (v4) |
| `/system heartbeat <url>` | 死活監視URLの設定 |
| `/system backup_remote <mode> [directory]` | バックアップの外部保存 |
| `/balance repair <user> <mode> <reason>` | 残高の突合修復 (2段階確認) |

### Server Admin (管理者ロール / サーバーオーナー / Bot Owner)

| コマンド | 内容 |
|---|---|
| `/setup` | 初期設定チェックリスト |
| `/config` | 現在の設定一覧 (チャージ設定とランキング設定を分けて表示) |
| `/charge_panel` | チャージパネルを新規設置 (既存は削除しない・複数設置可) |
| `/panels <type> [disable_message_id]` | パネルの一覧 / 無効化 (チャージ・ランキング・ショップ・招待・管理) |
| `/ranking_panel [channel]` | ランキングパネルを新規設置 (複数設置可) |
| `/settings charge_rate <率>` | チャージ率 (例 `130` / `130.5`) |
| `/settings charge_min <円>` / `charge_max <円>` | 金額制限 |
| `/settings daily_limit <円>` | 日次上限 (ユーザー単位・0で無制限) |
| `/settings guild_daily_limit <円>` | 日次上限 (サーバー全体・0で無制限) |
| `/settings admin_role <ロール>` | 管理者ロール |
| `/settings achievement_channel <ch>` | 実績チャンネル |
| `/settings log_channel <ch>` | ログチャンネル |
| `/settings ranking_limit <5-25>` | ランキング表示件数 |
| `/settings ranking_interval <30-3600>` | ランキング自動更新間隔 (秒) |
| `/settings ranking_enabled <bool>` | ランキング表示の ON/OFF |
| `/settings ranking_hide_absent <bool>` | 退会ユーザーをランキングから隠す |
| `/balance add <user> <amount> <reason>` | 残高加算 |
| `/balance remove <user> <amount> <reason>` | 残高減算 (確認ボタン・0未満にはならない) |
| `/balance set <user> <amount> <reason>` | 残高設定 (確認ボタン) |
| `/user freeze <user> <reason>` | 凍結 (チャージ不可・ランキング対象外・残高確認は可) |
| `/user unfreeze <user>` | 凍結解除 |
| `/history [user] [transaction_id] [status] [date] [amount_min] [amount_max] [page]` | 取引検索 |
| `/transaction info <tx_id>` | 取引の詳細 |
| `/transaction verify <tx_id>` | Kyash 側の受取状態を再確認 |
| `/transaction resolve <tx_id> <complete> <reason>` | 確認中の取引を完了/失敗で確定 (確認ボタン) |
| `/transaction retry <tx_id>` | **未受取を確認できた場合のみ** 再受取キューへ戻す |
| `/stats [days] [global_scope]` | 統計 + 日別推移グラフ (v4・`days:0` でグラフなし) |
| `/tier add\|remove\|list\|sweep` | 累計チャージによる段位 (v4) |
| `/ranking_reward set\|remove\|list\|run` | ランキング報酬の自動配布 (v4) |
| `/shop add ... item_type subscription` | 商品タイプとサブスクの設定 (v4) |
| `/shop subscriptions [user]` | 自動更新中の購入を一覧 (v4) |
| `/auction create\|close\|cancel\|bids` | オークションの運営 (v4) |
| `/goal create\|panel\|close\|cancel\|grants` | サーバー全体のチャージ目標 (v4) |
| `/fraud list\|resolve\|ignore\|scan\|thresholds` | 不正の兆候の確認と処理 (v4) |
| `/refund approve\|reject` | 返金申請の審査 (v4) |
| `/queue` | 受取キュー (処理中 / 待機 / 手動確認待ち) |
| `/system` | Bot・DB・Kyash・キュー・タスク稼働・バージョン |
| `/logs [action] [actor] [page]` | 監査ログ |
| `/maintenance on` / `off` | メンテナンス (新規チャージ停止・読み取り機能は継続) |
| `/emergency_stop on` / `off` | 緊急停止 (新規チャージ + 新規受取を停止・確認ボタン) |
| `/achievement proxy <user> <amount> <reason> [charge_rate] [credited_amount]` | 代理実績 (確認ボタン) |
| `/config show` / `export` / `import` / `reset` | 設定の確認・JSON入出力・初期化 |
| `/system status` | システム状態 (旧 `/system`) |
| `/inspect <user>` | 統合調査ビュー (残高・取引・招待・購入・注意点) |
| `/admin_panel [channel]` | 管理ダッシュボードを設置 (常設・自動更新) |
| `/rate set <role> <rate> [priority]` / `remove` / `list` | ロール別チャージ率 (VIP優遇) |
| `/shop add/edit/remove/list/log/refund/panel` | ロールショップの管理 |
| `/campaign create/edit/end/list/review/stats/blacklist/panel` | 招待キャンペーンの管理 |
| `/balance move <from> <to> <amount> <reason>` | 残高の付け替え (確認ボタン) |
| `/balance ledger [user] [type] [date] [operator]` | 残高台帳の検索 |
| `/balance undo <history_id> <reason>` | 残高操作1件の取消 (確認ボタン) |
| `/balance audit <user>` | 残高と履歴合計の突合 |
| `/balance distribution` | 残高の分布分析 |
| `/balance bulk <role> <operation> <amount> <reason>` | 一括残高操作 (既定はドライラン) |
| `/transaction refund <tx_id> <reason>` | 完了済みチャージの取消 (確認ボタン) |
| `/export transactions/balances/ledger/audit` | CSV 出力 |
| `/user cooldown <user>` | 連続失敗クールダウンの解除 |
| `/settings max_balance <amount>` | 1ユーザーの残高上限 |
| `/settings manual_review_allow_new <bool>` | 確認中でも新規チャージを許可 |
| `/settings balance_log_channel <ch>` / `balance_log_scope` | 残高操作ログ |
| `/settings summary_channel <ch>` / `summary_enabled` | 日次サマリ |
| `/settings shop_enabled <bool>` | ショップの有効/無効 |
| `/settings panel_title` / `panel_description` / `accent_color` | パネルのブランディング |

---

#### チャージ方式 (PayPay / LTC)

| コマンド | 内容 | 権限 |
|---|---|---|
| `/provider status` | 方式の状態・レート・価格の取得状況をまとめて表示 | 管理者 |
| `/provider enable` | 方式ごとに受付を切り替え | 管理者 |
| `/provider rate` | 方式ごとのチャージ率 (`clear` で既定に戻す) | 管理者 |
| `/provider limits` | 方式ごとの金額上下限 (`0 0` で既定に戻す) | 管理者 |
| `/provider destination` | 入金先 (PayPay ID / LTC アドレス) を登録 | **Owner** |
| `/provider destination_clear` | 入金先の登録を削除 | **Owner** |
| `/provider review_channel` | 申請の審査チャンネルを設定 | **Owner** |
| `/provider delegate` | そのサーバーの管理者にも承認を許可 | **Owner** |
| `/provider price_source` | LTC 価格の取得元 (API / 固定価格) | **Owner** |
| `/provider price` | LTC の固定価格 (API 障害時のフォールバック) | **Owner** |
| `/provider price_check` | 価格を実際に取得して確認 | **Owner** |
| `/request pending` | 未処理の申請をまとめて表示 | 管理者 |
| `/request list` | 申請の一覧 (状態・方式・利用者で絞り込み) | 管理者 |
| `/request show` | 申請の詳細 (照合用の値) | 管理者 |
| `/request approve` | 承認して残高を付与 (`amount` で額を変更可) | 承認者 |
| `/request reject` | 却下 (理由は利用者へ DM) | 承認者 |
| `/request cancel` | 申請を取り消す | 承認者 |

> 「承認者」= Bot Owner、または Owner が `/provider delegate` で承認を委任した
> サーバーの管理者。入金先が Owner のものであるため、既定では Owner だけが承認できます。

## G. 利用者の操作 (コマンド不要)

チャージパネルのボタンだけで完結します。

| ボタン | 内容 |
|---|---|
| 💰 チャージ | 金額入力 Modal → 送金リンク送信ボタン |
| 💳 残高 | 現在残高・累計チャージ回数・累計獲得 (Ephemeral) |
| 📜 履歴 | 自分の履歴のみ・ページング (Ephemeral) |
| ❓ ヘルプ | チャージ方法・チャージ率・上限・注意事項 (Ephemeral) |
| 🔄 更新 | パネル表示を最新の設定値に更新 |

ショップパネル (`/shop panel` で設置):

| ボタン | 内容 |
|---|---|
| 🛒 ショップを開く | 商品を選んで購入 (確認 → 残高から支払い → ロール付与)。残高不足・在庫切れ・購入上限・所持済みは**押す前に理由を表示**します |
| 📦 購入履歴 | 自分の購入履歴と有効期限 (Ephemeral) |

招待パネル (`/campaign panel` で設置):

| ボタン | 内容 |
|---|---|
| 🔗 招待リンクを取得 | 自分専用の招待リンクを発行 (Ephemeral) |
| 📊 自分の招待状況 | 確定/保留/要確認/無効の件数と獲得報酬 |
| 🏆 招待ランキング | 確定した招待数のランキング |

チャージ目標パネル (`/goal create ... channel:` または `/goal panel` で設置・v4):

| ボタン | 内容 |
|---|---|
| 🔄 最新の進捗 | いまの達成率と**自分の参加額** (Ephemeral) |

オークションのパネル (`/auction create` で自動設置・v4):

| ボタン | 内容 |
|---|---|
| 💸 入札する | 入札額を入力。その場で残高を預かり、上回られたら全額返金します |

### 利用者が使えるコマンド (v4)

| コマンド | 内容 |
|---|---|
| `/goal progress` | チャージ目標の進捗と自分の参加額 |
| `/goal list` | 目標の一覧 |
| `/auction bid <auction_id> <amount>` | 入札 (パネルが流れた場合の代替) |
| `/auction list` | 開催中・終了したオークションの一覧 |
| `/shop cancel <purchase_id>` | 自分のサブスクの自動更新を止める |
| `/refund request <transaction_id> <reason>` | 返金 (残高の取消) を申請 |
| `/refund cancel <request_id>` | 自分の申請を取り下げる (審査前のみ) |
| `/refund list` | 自分の申請の状態 |
| `/receipt show [transaction_id]` | チャージの控えを発行 (署名つき) |
| `/receipt verify <code>` | 控えが本物か・いまも有効かを確認 |

### 値のコピーについて

宛先や招待リンクは**コピー用のコードブロック**と、**タップできる素のリンク**の
両方で表示します (コードブロック内の URL は Discord ではリンクにならず
タップできないため)。

| 表示 | 用途 |
|---|---|
| 📋 コピー用 (コードブロック) | PC はブロック右上のボタン、スマホは長押しで正確にコピー |
| 🔗 タップして共有 / 支払う | そのまま開く・共有シートへ渡す |

対象: 招待リンク / LTC アドレス / PayPay ID / 送金する金額と数量 /
請求リンク / 審査カードの取引ID・txid。

### チャージ方法が複数あるとき

`💰 チャージ` を押すと方法の選択メニューが出ます (使えない方法は理由つきで表示)。
使える方法が Kyash だけのサーバーでは選択画面は出ず、従来どおり金額入力へ進みます。

| 方法 | 反映 | 手順 |
|---|---|---|
| 💰 Kyash (送金リンク) | **自動** (10〜60秒) | 自分で送金リンクを作って貼る |
| 🧾 Kyash (請求リンク) | **自動** (最大1分) | Bot が出すリンクを開いて支払うだけ |
| 🅿️ PayPay | 管理者の承認後 | 送金 → 取引ID を申請 |
| Ł Litecoin (LTC) | 管理者の承認後 | 表示された数量を送金 → txid を申請 |

### チャージの流れ (Kyash: 3 ステップ / PayPay・LTC: 4 ステップ)

```
[ステップ 1/3] 💰 チャージ → 金額入力 Modal (半角数字のみ)
[ステップ 2/3] 「🔗 リンクを送信」 → 送金リンク入力 Modal (Ephemeral / 期限は相対時刻で表示)
[ステップ 3/3] 自動検証 (金額一致・受取可否) → 受取キュー (順番待ち件数を表示) → 自動受取
   → 受取確認 → チャージ率適用 → 内部残高へ加算 → DM 通知 → 実績投稿 → ランキング更新
```

- 各画面に `ステップ n / 3 ●●○` と「▶ やること」を表示するため、操作の途中で迷いません。
- 中断して `💰 チャージ` を押し直すと、**同じ取引のステップ 2 から再開**します。
- エラー時は必ず「▶ 次にどうすればいいですか？」を表示します (エラーコードも脚注に記載)。

- 送金リンクは **Ephemeral Modal** で入力するため、公開チャンネルに残りません。
- 入力した金額と送金リンクの金額が **一致しない場合は処理しません** (差額処理もしません)。
- 請求リンクは受け取れません (送金リンクを作成してください)。
- リンク入力期限は約 15 分、同時に進行できるチャージは 1 ユーザー 1 件です。

---

## H-0. PayPay / LTC を使う場合の設定順

```bash
# --- Bot Owner が一度だけ ---
/provider destination provider:PayPay address:<PayPay ID> label:受取用
/provider destination provider:Litecoin\ (LTC) address:<LTC アドレス> label:受取用
/provider review_channel channel:#charge-review
/provider price_check                      # LTC の価格取得を確認

# --- 各サーバーの管理者 ---
/provider rate provider:PayPay charge_rate:120
/provider rate provider:Litecoin\ (LTC) charge_rate:150
/provider status                           # 🟢 になったか確認
/charge_panel                              # パネルを貼り直すと方式が反映されます
```

詳細は本文後半の「v3: PayPay / Litecoin チャージ (承認制)」を参照してください。

---

## H. ランキングパネルの設定 (チャージパネルとは完全に独立)

ランキングは **専用パネル** で提供します。チャージパネルにランキングボタンは追加されません。

```bash
/ranking_panel                 # 実行したチャンネルへ設置
/ranking_panel channel:#ranking  # 指定チャンネルへ設置
```

- 同じサーバーの複数チャンネルへ設置可能 (`#ranking` と `#balance-ranking` など)。
  すべて同じサーバーのランキングを表示します。
- 既存パネルは削除されません。無効化は `/panels type:🏆\ ランキングパネル disable_message_id:<ID>`。
- DB は `ranking_panels` テーブルで `panels` (チャージパネル) とは別管理です。
- 表示内容: そのサーバーの **現在の内部残高** (`balances` が Source of Truth) を高い順に表示。
  - 並び順: `balance DESC, user_id ASC` (同額でも順位が安定します)
  - Bot ユーザーは対象外 / 凍結ユーザーは対象外 / 退会ユーザーは取得できる範囲で表示
  - `/settings ranking_hide_absent true` でサーバー不在ユーザーを非表示にできます
- 更新タイミング
  - チャージ完了 / 管理者の加算・減算・設定 / 凍結・解除の直後 (約1.5秒のデバウンスでまとめて1回)
  - `/settings ranking_interval` の間隔でのバックグラウンド更新
  - 🔄 更新ボタン (DB から再取得)
  - **表示内容が変化していない場合は Discord API を呼びません** (無駄な編集をしない)
- 📜 自分の順位 ボタンで、押した本人だけに Ephemeral で順位を表示します。
- Bot 再起動後も Persistent View と自動更新が維持されます。

---

## I. テスト手順

### 1. 自動テスト (Kyash の HTTP のみモック / 実通信なし)

```bash
cd /opt/discord-charge-bot
./venv/bin/python3 tests/test_charge_flow.py     #  91 件  Kyash 送金リンクの本流と異常系
./venv/bin/python3 tests/test_claim_link.py      #  56 件  Kyash 請求リンクの支払い
./venv/bin/python3 tests/test_v2_features.py     #  89 件  ショップ・招待・残高操作・実績投稿
./venv/bin/python3 tests/test_v4_features.py     # 278 件  段位・ランキング報酬・複数アカウント・
                                                 #         サブスク・オークション・目標・検知・
                                                 #         返金申請・レシート・グラフ
./venv/bin/python3 tests/test_migration.py       #  29 件  旧DBの自動移行 (v1 → v4)
./venv/bin/python3 tests/test_providers.py       #  75 件  PayPay / LTC の申請・承認
./venv/bin/python3 tests/test_ui_render.py       # 290 件  全 Embed の描画と文字数上限
./venv/bin/python3 tests/test_commands_smoke.py  # 225 件  全コマンド・全ボタンの実行
./venv/bin/python3 tests/test_concurrency.py     #  26 件  同時実行でお金が増減しないこと
./venv/bin/python3 tests/test_static_audit.py    #  95 件  静的監査 (起動せずに不整合を検出)
# → 合計 1254 件成功 / 0 件失敗
```

まとめて実行する場合:

```bash
for t in tests/test_*.py; do
  echo "--- $t"; ./venv/bin/python3 "$t" | tail -3
done
```

静的解析も併せて実行できます (どちらも 0 件が正常)。

```bash
./venv/bin/python3 -m pyflakes *.py tests/*.py
./venv/bin/python3 -m mypy --ignore-missing-imports \
    config.py utils.py database.py kyash_service.py price_service.py \
    charge_service.py ui.py commands.py tasks.py main.py
```

検証内容: 金額検証 / 正常チャージ (130%→1300) / 二重付与防止 / 同一リンク再利用拒否 /
金額不一致 / 請求リンク・無効リンク / 同一ユーザーの二重チャージ / **同一リンクの同時送信** /
タイムアウト後の未受取確認と再試行 / 受取応答はあるが痕跡なし → MANUAL_REVIEW /
受取拒否 / **再起動復旧 (受取済み→付与 / 不明→手動確認・再受取しない)** /
管理者確定 / 日次上限 / 凍結 / メンテナンス / 緊急停止 / 未許可サーバー / サーバー間分離 /
期限切れ / 管理者残高操作 / ランキング (降順・同額順・凍結除外・署名) / 代理実績 /
端数処理 / チャージ率スナップショット / 整合性 / 統計 / バックアップ / 通知キュー /
レート制限 / コマンドツリー / 再起動後の永続化 /
**ショップ購入の実績投稿 (返金・期限切れで同じ投稿を更新・重複投稿しない)** /
**招待確定の実績投稿** / 全 Embed の文字数上限 / 管理コマンドが一般利用者を拒否すること /
**段位の自動昇格 (降格しない・二重通知しない)** / **ランキング報酬の二重配布防止** /
**受取アカウントのフェイルオーバーと全台満杯の判定** /
**商品タイプごとの付与と後片付け (返金・期限切れ・用意の失敗)** /
**サブスクの自動更新 (同時8件でも1回だけ・残高不足で終了し特典も取消)** /
**オークション (預かり・即返金・締切延長・二重締切の防止・総額の保存)** /
**チャージ目標 (期間の境界・二重配布の防止・凍結の除外)** /
**不正検知 (5ルール・二重フラグの防止・送金者名を伏せる・1ルール故障時の継続)** /
**返金申請 (期限・二重申請・上限・承認で取消・却下で不変)** /
**レシート (改ざん・別の鍵・取消後・記録との不一致をすべて無効と判定)** /
**グラフ (0埋め・上限日数・Pillow が無い環境でも統計は出る)**

#### 静的監査で見ているもの (`tests/test_static_audit.py`)

起動しないと分からない壊れ方を、ソースを読んで先に見つけます。

- `custom_id` の衝突・未使用、Persistent View の登録漏れ (再起動後にボタンが死ぬ)
- 到達できない状態遷移、案内文の無いエラーコード
- `CREATE TABLE` と後方互換列の食い違い (古い DB で落ちる)
- トランザクションの入れ子 (実行時にしか出ない例外)
- 秘密情報をログの引数へ渡していないか
- `defer` の後に Modal を出していないか (Discord が拒否する)
- 金額を `float` で扱っていないか

#### 同時実行の監査で見ているもの (`tests/test_concurrency.py`)

残高が動く経路を同時に叩き、**お金が増えも減りもしない**ことを確認します。

- 同じ取引の残高付与 / ショップの在庫と購入上限 / サブスクの更新
- オークションの入札 (「残高 + 預かり額」の合計が常に一定) と締切
- 目標報酬の配布 / 返金申請と取消 / 不正検知フラグの作成
- 最後に `PRAGMA quick_check` と「残高と履歴合計の一致」を確認

### 2. 手動テスト (実環境・少額で実施)

| 項目 | 手順 | 期待結果 |
|---|---|---|
| 正常チャージ | 100円で実施 | 130 付与・DM・実績 🟢・ランキング更新 |
| 金額不一致 | 1000円申請 → 500円リンク | 失敗・受取されない・Kyash 残高不変 |
| 使用済みリンク | 受取済みリンクを再送信 | `LINK_ALREADY_USED` で拒否 |
| 期限切れ | リンクを15分放置 | `EXPIRED`・以後受取されない |
| 二重送信 | 同じリンクを連続送信 | 1件のみ受理・二重受取なし |
| Bot 再起動 | 受取直後に `systemctl restart` | 二重受取せず復旧 (完了 or 手動確認) |
| DM 拒否 | DM を無効にして実施 | チャージは成功・ログに記録 |
| パネル削除 | パネルメッセージを削除 | DB 上 inactive・Bot は停止しない |
| チャンネル削除 | 実績チャンネルを削除 | 設定が自動解除・Owner へ通知・Bot 継続 |
| ロール削除 | 管理者ロールを削除 | 管理コマンド拒否・Owner へ通知 |
| 権限 | 一般利用者が `/settings` 実行 | 「権限がありません」 |
| 未許可サーバー | 許可前のサーバーでパネル操作 | 利用不可の案内 |
| メンテナンス | `/maintenance on` | 新規チャージ不可・残高/履歴/ランキングは可 |
| 緊急停止 | `/emergency_stop on` | 新規チャージ・新規受取停止・キューは保持 |
| 複数サーバー | 2サーバーで同一ユーザーがチャージ | 残高・設定・ランキングが混ざらない |

---

## J. トラブルシューティング

### 起動しない

| ログ | 原因と対処 |
|---|---|
| `DISCORD_BOT_TOKEN が未設定です` | `main.py` の先頭を書き換える |
| `BOT_OWNER_ID が未設定です` | 同上 |
| `Discord Token が正しくありません` | Token を再発行して設定し直す |
| `SERVER MEMBERS INTENT が無効です` | Developer Portal で SERVER MEMBERS INTENT を ON |
| `Kyash モジュール (vendor/Kyasher) を import できません` | `vendor/Kyasher/` が配置されているか確認 |
| `データディレクトリへ書き込めません` | `chown chargebot:chargebot data` |
| `必須パッケージがありません` | `pip install -r requirements.txt` |
| `DBのスキーマバージョンがBotより新しい` | Bot を新しいバージョンへ更新する (ダウングレード禁止) |

### チャージできない

1. `/setup` で未設定項目を確認
2. `/kyash status` で受取用アカウントの状態を確認
   - `🟠 再認証が必要` → `/kyash login` (端末情報が残っていれば SMS 認証なしで通ることがあります)
   - `🔴 エラー` → `last_error` を確認し、`/kyash reconnect`
3. `/system` でメンテナンス / 緊急停止 / キュー / タスク稼働を確認
4. `/queue` で滞留・手動確認待ちを確認

### `🟠 確認中 (MANUAL_REVIEW)` の取引がある

外部処理の結果が不明なため、**Bot は絶対に自動で再受取しません**。

```bash
/transaction info <tx_id>      # 状況を確認
/transaction verify <tx_id>    # Kyash の履歴・残高から再判定
```

- 判定が `🟢 受取済みを確認` → `/transaction resolve <tx_id> complete:True reason:...` で完了
- 判定が `🔴 受取の痕跡なし` → `/transaction retry <tx_id>` で再受取、または `complete:False` で失敗確定
- 判定が `🟠 判定不能` → Kyash アプリで実際の入金を確認してから `resolve` で確定

> 利用者には「現在処理結果を確認しています」と通知され、残高は付与されません。
> 対象ユーザーはこの取引が解決するまで新しいチャージを開始できません (安全側の仕様)。

### PayPay / LTC のチャージができない

`💰 チャージ` に方法が出てこない、または選ぶと拒否される場合は
`/provider status` で原因を確認してください。

| 表示 | 原因と対処 |
|---|---|
| 🔴 入金先が未登録です | `/provider destination` で登録 (Owner) |
| 🔴 審査チャンネルが未設定です | `/provider review_channel` で設定 (Owner) |
| 🔴 管理者が停止しています | `/provider enable` で受付を再開 |
| LTC だけ使えない | `/provider price_check` を実行。価格が取得できていません |

利用者が「レートを取得できないため利用できません」と言われる場合は、
CoinGecko へ到達できていません。上の「価格が取得できないとき」を参照してください。

### 請求リンクで支払ったのに反映されない

1. `🔄 支払いを確認` を押す (履歴の反映に時間差があります)
2. それでも反映されない場合、Bot は **45 秒ごとに自動確認**しているので少し待つ
3. 20 分の期限を過ぎると失敗扱いになります。その場合は管理者へ取引IDを添えて連絡

Bot は「受取用アカウントの残高が増えた」だけでは付与しません。
**履歴にそのリンクの識別子が現れること**を条件にしているため、
他人の支払いで誤って付与されることはありません。

### コマンドが2つずつ表示される

v3.1 で修正済みです。起動時に重複したギルド専用コマンドを自動削除します。
それでも残る場合は手動で削除できます。

```bash
/server sync cleanup:True
```

コマンドは**グローバルにのみ登録**する方針です (ギルドへも登録すると
Discord が両方を別枠で表示するため二重に見えます)。

### 申請が承認できない

| メッセージ | 原因 |
|---|---|
| `その申請は既に処理されています` | 他の管理者が処理済み、または期限切れ |
| `操作できません` | 承認権限がありません (`/provider delegate` で委任) |
| `その取引は既に申請されています` | 同じ取引ID / txid が既に使われています |

`/request show request_id:<ID>` で現在の状態を確認できます。

### グラフが表示されない

`/stats days:14` で画像が出ない場合:

```bash
./venv/bin/pip install Pillow          # 未導入なら入れる
sudo apt-get install -y fonts-noto-cjk # 日本語ラベル用 (無いと英字ラベルになる)
sudo systemctl restart discord-charge-bot
```

Pillow が無くても Bot は動きます (グラフだけ省略し、数字は Embed に出ます)。

### サブスクが更新されない

- 利用者の残高が足りているか (`/balance` または `/inspect`)
- 商品が販売停止になっていないか (`/shop list` で ⚫ 表示)
- 利用者が凍結されていないか
- `/shop subscriptions` で `次回` の日時を確認 (更新は 10 分ごとに判定します)

残高不足で終了した場合は、利用者へ DM で通知し、特典も取り消しています。
再開するにはショップから買い直してもらってください。

### オークションの景品が渡されない

落札者へロールを付与できなかった場合、Bot は**落札額を返金して中止扱い**にし、
管理者へ通知します。原因はほぼ次のどちらかです。

- 景品のロールが Bot のロールより**上**にある → ロールの並び順を入れ替える
- Bot に「ロールの管理」権限が無い

直したうえで、オークションを作り直してください。

### 検知が出すぎる / 出ない

`/fraud thresholds` で現在の条件を確認できます。条件は `config.py` の
`FRAUD_*` 定数で調整します (変更後は再起動が必要です)。

- 検知は**通知だけ**です。自動での凍結・没収は行いません
- 「送金者名の共有」は Kyash の送金リンク経由のチャージだけが対象です
  (PayPay / LTC の申請や請求リンクでは送金者名を取得できません)

### 返金申請ができない

- チャージ完了から **14 日**を過ぎていないか
- 審査待ちの申請が**すでに 2 件**ないか (`/refund list`)
- 対象の取引が既に取消済みでないか

管理者が承認しても、**Kyash へのお金の返却は自動では行われません**。
内部残高を取り消したうえで、管理者が Kyash 側で手作業の送金を行ってください。

### レシートが「無効」と出る

`/receipt verify` は署名だけでなく、**いまの記録との一致**も見ます。
次の場合は正しく「無効」と判定されます。

- その取引が取消 (返金) された
- 取引の金額が後から変更された
- 別の Bot / 別の鍵で発行された控え

`data/receipt.key` を作り直した場合、それ以前に発行した控えはすべて検証できなくなります。

### 実績投稿について

実績チャンネル (`/settings achievement_channel`) には次の 3 種類を投稿します。

| 種別 | 投稿されるタイミング | 更新 |
|---|---|---|
| 🟢 チャージ実績 | チャージ完了 / 手動確認の確定 | 状態が変わると同じ投稿を更新 |
| 🛒 ショップ購入実績 | ロール購入の成立 | **返金・期限切れで同じ投稿を更新** (重複投稿しません) |
| 🤝 招待実績 | 招待報酬の確定 | 確定は 1 回だけなので更新なし |

投稿したメッセージ ID は DB (`shop_purchases.achievement_message_id` など) に保存するため、
Bot を再起動しても同じ投稿を更新し続けます。手動で消した場合は次の更新時に再投稿します。

### 実績・DM が届かない

- DM 拒否ユーザー / 権限不足でも **チャージ自体は成功扱い** です。
- 送信に失敗した通知は `notification_queue` に積まれ、1分間隔で最大5回再送します。
- 実績チャンネルの権限 (メッセージ送信 / 埋め込みリンク) を確認してください。

### ランキングが更新されない

- `/settings ranking_enabled true` を確認
- `/panels type:🏆\ ランキングパネル` でパネルが 🟢 有効か確認 (メッセージ削除時は自動で無効化されます)
- **表示内容が前回と同じ場合は意図的に更新しません**。🔄 更新ボタンで強制再取得できます。

### DB / データ

```bash
# 整合性チェック (6時間ごとに自動実行・問題は Owner へ DM 通知)
sqlite3 data/charge_bot.db "PRAGMA integrity_check;"

# バックアップ (毎日自動 / 14世代保持)
ls -l data/backups/
/backup            # Discord から手動実行 (対象は DB のみ)

# リストア (Bot を停止してから)
sudo systemctl stop discord-charge-bot
cp data/backups/charge_bot_YYYYmmdd_HHMMSS.db data/charge_bot.db
sudo systemctl start discord-charge-bot
```

> `/backup` と自動バックアップの対象は **DB だけ**です。
> `data/secret.key` (Kyash トークンの復号) と `data/receipt.key` (レシートの検証) は
> 別途バックアップしてください。失うと、保存済みトークンの復号と、
> 発行済みレシートの検証ができなくなります。
>
> ```bash
> # 例: 鍵だけを安全な場所へ (権限 0600 のまま扱うこと)
> sudo cp -p data/secret.key data/receipt.key /root/keybackup/
> ```

### エラーコード

利用者には安全な日本語メッセージのみ表示され、管理者向けにはコードと詳細が残ります。

全 63 種。すべてに「▶ 次にどうすればいいですか？」の案内文があります。

`ACTIVE_TRANSACTION_EXISTS` / `ALREADY_HIGHEST` / `AMOUNT_ABOVE_MAX` / `AMOUNT_BELOW_MIN` /
`AMOUNT_MISMATCH` / `ASSET_AMOUNT_TOO_SMALL` / `AUCTION_LIMIT_REACHED` / `AUCTION_NOT_OPEN` /
`BID_TOO_LOW` / `CAMPAIGN_NOT_ACTIVE` / `CLAIM_LINK_FAILED` / `COOLDOWN` /
`DAILY_LIMIT_EXCEEDED` / `DATABASE_ERROR` / `DUPLICATE_PROOF` / `EMERGENCY_STOP` /
`FRAUD_FLAG_NOT_FOUND` / `GOAL_ALREADY_OPEN` / `GOAL_NOT_FOUND` / `GUILD_DAILY_LIMIT_EXCEEDED` /
`GUILD_DISABLED` / `INSUFFICIENT_BALANCE` / `INVALID_AMOUNT` / `INVALID_LINK` /
`INVALID_PROOF` / `INVITE_NOT_AVAILABLE` / `ITEM_INPUT_INVALID` / `ITEM_SETUP_FAILED` /
`KYASH_AUTH_ERROR` / `KYASH_NETWORK_ERROR` / `KYASH_REJECTED` / `KYASH_TIMEOUT` /
`KYASH_UNAVAILABLE` / `LINK_ALREADY_USED` / `LINK_EXPIRED` / `LINK_IS_CLAIM` /
`MAINTENANCE` / `MANUAL_REVIEW` / `MAX_BALANCE_EXCEEDED` / `NOT_ALLOWED` /
`NO_KYASH_CAPACITY` / `OPEN_REQUEST_LIMIT` / `PAYMENT_NOT_FOUND` / `PRICE_UNAVAILABLE` /
`PROVIDER_DISABLED` / `PROVIDER_NOT_CONFIGURED` / `QUOTE_EXPIRED` / `RATE_LIMITED` /
`REFUND_ALREADY_REQUESTED` / `REFUND_NOT_ELIGIBLE` / `REQUEST_ALREADY_HANDLED` / `REQUEST_NOT_FOUND` /
`REVIEW_CHANNEL_NOT_SET` / `ROLE_ASSIGN_FAILED` / `SHOP_ALREADY_OWNED` / `SHOP_ITEM_UNAVAILABLE` /
`SHOP_LIMIT_REACHED` / `SHOP_OUT_OF_STOCK` / `SUBSCRIPTION_NOT_FOUND` / `TRANSACTION_EXPIRED` /
`UNKNOWN_ERROR` / `USER_FROZEN` / `WALLET_LIMIT`

---

## 添付モジュール (vendor/Kyasher) の扱い

`vendor/Kyasher/` は添付された Kyasher 1.5.0 を **一切変更せず** 同梱しています
(監査可能性のため)。解析の結果、以下の不具合が存在するため、
`kyash_service.py` 側で回避しています。**存在しない API は作っていません。**

| 箇所 | 不具合 | 本Botでの対処 |
|---|---|---|
| `get_wallet()` (line 147) | NamedTuple のフィールドは `uuid` だが `wallet_uuid=` で生成しており常に `TypeError` | まず本来の呼び出しを試し、`TypeError` のときのみ **同一エンドポイント/同一ヘッダ** (`/v1/me/primary_wallet`) で取得し直す |
| `link_check()` (line 263) | 未定義属性 `self.link_uuid` を参照し `AttributeError` | モジュール自身が `link_recieve()` (lines 300-310) で使っているスクレイピング手順を同じ方法で再実装 |
| `link_check()` (line 282) | NamedTuple の生成キーワードが全て不一致で `TypeError` | 同上 (戻り値は `LinkInfo` dataclass) |
| `link_check()` の金額 | `int("1,000")` がカンマで失敗 (1000円以上で常に失敗) | 金額表記から数字のみを抽出して整数化 |
| `__init__` UUIDログイン (line 73) | `headers["X-Auth"]` に引数の `access_token` (=None) を代入 | 生成直後に実トークンでヘッダを補正 |
| 全 `requests` 呼び出し | timeout 未指定 (無限待機の可能性) | `socket.setdefaulttimeout(30s)` + `asyncio.wait_for(75s)` で二重に上限を強制 |
| `send_to_link()` | 変数未束縛のバグあり | **使用しません** (請求リンクへの送金は行わない) |

使用している添付モジュールの API: `Kyash(...)` / `login(otp)` / `get_profile()` /
`get_history(...)` / `link_recieve(link_uuid=...)`、および上記互換経路。

### 受取成功の判定 (最重要)

「受取関数を呼んだ」だけでは成功としません。以下を組み合わせて判定します。

1. `link_recieve()` の戻り値 (`code == 200`)
2. `get_history()` の履歴に **リンクUUID** が現れるか
3. `get_wallet()` の残高が受取前より **受取額以上** 増えているか

| 判定 | 動作 |
|---|---|
| 🟢 受取を確認 | 残高付与へ進む |
| 🔴 痕跡なし (未受取と確認) | 再試行可能 (指数バックオフ 最大5回) / 受取拒否時は失敗確定 |
| 🟠 判定不能 | **再受取せず** `MANUAL_REVIEW` へ移行し管理者へ通知 |

---

## セキュリティ / 整合性の方針 (v3 の追加分)

- **PayPay / LTC で外部の決済 API は使いません。** 着金確認は管理者が行う承認制です。
  そのため決済サービスの規約違反やアカウント凍結のリスクを負いません。
- LTC の入金先は**アドレスだけ**を登録します。**秘密鍵は一切扱いません**。
  Bot が侵害されても LTC を動かすことはできません。
- 価格は「取れたら使う」のではなく「**信用できるときだけ使う**」方針です。
  検証・範囲・鮮度・異常値ガードのいずれかに引っかかれば採用せず、
  代替値も無ければ LTC の受付を止めます (推測した価格で残高を発行しません)。
- 承認は既定で Bot Owner のみ。入金先が Owner のものなので、
  確認できる人だけが承認できる構成にしています。
- 同じ証拠 (取引ID / txid) は DB の UNIQUE 制約で一度しか使えません。
- 承認は `BEGIN IMMEDIATE` 内の状態遷移で直列化され、同時押しでも一度しか付与されません。
- 申請の承認は Kyash と同じ `credit_transaction` を通るため、
  冪等性・監査ログ・実績・ランキング・招待確定の保証をそのまま受け継ぎます。

## セキュリティ / 整合性の方針

- **秘密情報をログ・DB に残さない**: Token / パスワード / OTP / Cookie / Authorization /
  完全な送金リンクは出力しません (`utils.sanitize_for_log` でマスク)。
- **送金リンクの完全な URL は保存しない**: 二重送信判定用の SHA-256 ハッシュと、
  受取・状態確認に必要なリンクUUIDのみ保存します。
- **パスワードは保存しない**: アクセストークンのみ `data/secret.key` (0600) で暗号化保存。
- **鍵は用途ごとに分ける**: レシートの署名は `data/receipt.key` (0600) を使い、
  トークンの暗号鍵とは別に持ちます (片方が漏れても他方に影響させないため)。
- **不正検知は通知だけ**: 自動での凍結・残高没収・BAN は一切行いません。
  検知の記録には Kyash 送金者名をそのまま残さず、先頭だけ残して伏せます。
- **返金は内部残高の取消のみ**: Kyash への送金は自動化しません (添付モジュールに手段が無いため)。
  実際の返金は管理者の手作業で、その旨を申請・審査・結果のすべてに明記します。
- **二重受取防止**: `link_hash` / `link_uuid` の UNIQUE 制約 + プロセス内ロック +
  受取処理の完全直列化 (単一スレッド + Lock)。
- **二重残高付与防止**: `balance_history(transaction_id, type)` の UNIQUE 制約 +
  `BEGIN IMMEDIATE` 内での存在確認。残高更新と履歴保存は同一トランザクション。
- **チャージ率のスナップショット**: Transaction 作成時点の率を保存し、後の設定変更で再計算しません。
- **金額は Decimal / 整数**: float を使わず、付与額は四捨五入 (101×130% = 131)。
- **サーバー完全分離**: すべてのクエリに `guild_id` を含め、残高・履歴・ランキング・統計・実績が混ざりません。
- **Discord 通知の失敗でチャージを巻き戻さない**: 通知・ランキング更新はすべて例外を吸収します。
- **graceful shutdown**: SIGTERM/SIGINT で新規受付停止 → タスク停止 → セッション破棄 → DB commit → 切断。
- **自己復旧**: 停止したバックグラウンドタスクを定期タスクが相互監視して再開します。

---

# v3: PayPay / Litecoin チャージ (承認制)

Kyash は Bot が自動で受け取りますが、**PayPay と LTC は「利用者が申請 → 管理者が承認」**
という流れです。外部の決済 API を使わないので、規約リスクも API 障害もありません。
その代わり、**入金が実際に届いているかは管理者が自分の目で確認**します。

## Kyash 請求リンク (推奨・自動)

**Bot が金額入りの請求リンクを発行し、利用者は開いて支払うだけ**です。
送金リンク方式と違い、利用者がリンクを作る必要がなく、**金額の打ち間違いが
原理的に起きません**。管理者の承認も不要で、自動で残高に反映されます。

```
[1/2] 💰 チャージ → 🧾 Kyash (請求リンク) を選ぶ → 金額を入力
[2/2] Bot が請求リンクを出す → 開いて支払う
        → 自動で確認して残高に反映 (最大1分ほど)
        → 急ぐときは `🔄 支払いを確認` を押す
```

設定は方式ごとのレートと同じコマンドで行います (入金先の登録は不要)。

```bash
/provider rate provider:Kyash\ (請求リンク) charge_rate:130
/provider limits provider:Kyash\ (請求リンク) minimum:100 maximum:50000
/provider enable provider:Kyash\ (請求リンク) enabled:true
```

### 送金リンク方式との違い

| | 💰 Kyash (送金リンク) | 🧾 Kyash (請求リンク) |
|---|---|---|
| リンクを作る人 | 利用者 | **Bot** |
| 金額の指定 | 利用者 (打ち間違いが起きる) | **Bot (一致は保証)** |
| 手順 | 3 ステップ | **2 ステップ** |
| 反映 | 自動 | 自動 |
| 受取操作 | Bot が受け取る | 利用者が支払う (受取操作なし) |

### 安全性

| 仕組み | 内容 |
|---|---|
| 発行の検証 | 発行直後にリンクを読み直し、**請求リンクであること**と**金額が一致すること**を確認。違えば取引を作らずリンクを破棄します |
| 支払いの確認 | **履歴に自分のリンク識別子が現れたときだけ**付与します。受取用アカウントの残高が増えただけでは付与しません (他人の支払いを自分のものと誤認しないため) |
| 二重付与 | 状態遷移と `UNIQUE` 制約で、同じ取引では一度しか付与されません (同時実行でも検証済み) |
| リンクの再利用 | 支払い確認後・取消後・期限切れ後は**リンクを無効化**します |
| 同時進行 | 1 人が同時に持てる請求リンクは 1 本まで |
| 期限 | 発行から 20 分。期限切れの直前に支払われていた場合も取りこぼしません |

> ⚠️ 請求リンクは Kyash の仕様上「繰り返し支払える」形で発行されます。
> そのため Bot は**支払いを確認した時点でリンクを無効化**し、画面にも
> 「支払いは1回だけ」と明記しています。

---

## 利用者から見た流れ (4 ステップ)

```
[1/4] 💰 チャージ → 方法を選ぶ  [Kyash / PayPay / LTC]
[2/4] 金額を入力 (LTC も「円」で入力します)
[3/4] Bot が宛先と送る金額を表示 → 送金 → 「✅ 送金しました」
        → PayPay: 取引ID を入力
        → LTC:   txid と実際に送った数量を入力
[4/4] 申請完了 → 管理者の承認待ち
        承認 → 残高に反映 + DM + 実績投稿 + ランキング更新 + 招待報酬の確定
        却下 → DM で理由を通知 (残高は動きません)
```

- 使える方法が Kyash だけのサーバーでは、**選択画面は出ません** (従来どおり 3 ステップ)。
- `📜 履歴` の先頭に「進行中の申請」が出るので、利用者は状態をいつでも確認できます。
- 申請の受付期限: 送金待ち **30 分** / 承認待ち **72 時間** (期限切れは DM で通知)。
- 1 人が同時に持てる未処理の申請は **3 件**まで。

## 初期設定 (Bot Owner)

```bash
# 1) 入金先を登録する (全サーバー共通)
/provider destination provider:PayPay address:<PayPay ID> label:受取用 \
    note:メモ欄には何も書かないでください
/provider destination provider:Litecoin\ (LTC) address:<LTC アドレス> label:受取用

# 2) 審査チャンネルを設定する (全サーバーの申請がここへ集まります)
/provider review_channel channel:#charge-review

# 3) LTC の価格取得を確認する
/provider price_check
#   🟢 取得できた → そのまま使えます
#   🔴 取得できない → 下の「価格が取得できないとき」を参照

# 4) (任意) 相場 API 障害時のフォールバック価格を入れておく
/provider price jpy:12000
```

**入金先は Bot Owner のものなので、承認できるのは既定で Bot Owner だけです。**
信頼できるサーバーの管理者にも承認させたい場合だけ、明示的に委任します。

```bash
/provider delegate guild_id:<サーバーID> enabled:true
```

> ⚠️ 委任したサーバーの管理者は、入金を確認せずに残高を発行できてしまいます。
> 入金先を共有している相手にだけ委任してください。

## サーバーごとの設定 (管理者)

```bash
/provider status                              # 方式の状態をまとめて確認
/provider enable provider:PayPay enabled:true # 方式ごとに受付を切り替え
/provider rate provider:PayPay charge_rate:120   # 方式ごとのチャージ率
/provider rate provider:Litecoin\ (LTC) charge_rate:150
/provider rate provider:PayPay charge_rate:clear # サーバー既定に戻す
/provider limits provider:PayPay minimum:1000 maximum:30000  # 0 0 で既定に戻す
```

### チャージ率の決まり方

```
方式別レート が サーバー既定 を置き換える
    ↓
ロール別レート (VIP 等) があれば、その 高い方 を採用する
```

| 設定 | VIP なし | VIP (150%) |
|---|---|---|
| サーバー既定 130% のみ | 130% | 150% |
| PayPay 100% | **100%** | 150% |
| LTC 200% | **200%** | **200%** |

こうすることで「PayPay は低めにする」意図も、「LTC を高くしたのに VIP が損をする」
という逆転も同時に避けられます。適用したレートは**申請作成時に保存**されるので、
後から設定を変えても既存の申請には影響しません。

## 審査 (承認 / 却下)

申請が来ると審査チャンネルにカードが投稿されます。

| ボタン | 動作 |
|---|---|
| 🟢 承認 | 申請どおりの額を付与 |
| ✏️ 金額を直して承認 | 実際の入金額と違うとき。**理由が必須**で監査ログに残ります |
| 🔴 却下 | 理由を入力して却下。残高は動かず、利用者へ DM で理由が届きます |
| 🔍 詳細 | txid・宛先・確定レート・その利用者の直近の申請・現在残高を表示 |

カードのボタンは**再起動後も動きます** (Persistent View)。処理済みのカードは
自動で更新され、ボタンが外れます。コマンドでも同じことができます。

```bash
/request pending                    # 未処理の申請をまとめて見る
/request list status:🟡\ 承認待ち    # 絞り込み
/request show request_id:12         # 詳細 (照合用の値をコピーできます)
/request approve request_id:12                        # 申請どおり承認
/request approve request_id:12 amount:900 note:入金が900円だったため
/request reject request_id:12 reason:入金を確認できませんでした
/request cancel request_id:12       # 取り消し
```

未処理の申請が **6 時間**を超えると Owner へ DM で催促します。

### 確認のしかた

| 方式 | 照合する値 | 確認場所 |
|---|---|---|
| PayPay | 取引ID・金額 | PayPay アプリの取引履歴 |
| LTC | txid・宛先・数量・承認数 | ブロックエクスプローラ (txid で検索) |

**LTC は txid が公開情報なので、PayPay より確実に検証できます。**
`🔍 詳細` に txid がコードブロックで出るので、そのままコピーして
エクスプローラで検索してください。

## LTC のレート

利用者が**円**で金額を入力すると、Bot がその時のレートで「送る LTC 数量」を計算します。

```
入力: 1,000 円
  ↓ CoinGecko から 1 LTC = 12,000 円 を取得
表示: 0.08333334 LTC を送ってください (切り上げ / 不足が出ないように)
  ↓ この単価 12,000 円を申請に固定
承認: 1,000 円 × チャージ率 150% = 1,500 を付与
```

申請を作った後に相場が動いても、**その申請の単価は変わりません**
(チャージ率スナップショットと同じ考え方)。

### 価格の安全弁

| 仕組み | 内容 |
|---|---|
| 厳格な検証 | 応答の型・範囲を検査。想定外の形なら採用しません |
| 範囲チェック | 100 円〜1 億円の外は拒否 (桁違いの応答を弾く) |
| 鮮度チェック | 15 分より古い価格は使いません |
| 異常値ガード | 直近の採用値から **35% 以上**跳ねたら採用せず Owner へ通知 |
| キャッシュ | 60 秒。API を叩きすぎません |
| レート制限 | 価格取得の前にレート制限をかけるので、連打で API を叩けません |
| フォールバック | 取得失敗時は `/provider price` の固定価格 → 直近の採用値 の順 |
| 最終手段 | どれも使えない場合は **LTC チャージを拒否**します (推測した価格は使いません) |

### 価格が取得できないとき

`/provider price_check` が 🔴 になる主な原因は**ネットワーク**です。

```bash
# VPS から到達できるか確認する
curl -sS "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=jpy"
# → {"litecoin":{"jpy":12345}} が返れば OK
```

到達できない場合 (ファイアウォール・プロキシ環境など) は固定価格で運用できます。

```bash
/provider price jpy:12000            # 1 LTC = 12,000 円
/provider price_source source:管理者が設定した固定価格
```

> ⚠️ 固定価格のまま相場が動くと、差額を突かれる恐れがあります。
> `/provider status` に最終更新時刻が出るので、定期的に見直してください。

## 不正防止

| 手口 | 対策 |
|---|---|
| 同じ取引ID / txid で何度も申請 | `charge_requests.proof_hash` に **UNIQUE 制約**。却下・取消済みのものも占有し続けます |
| 2 人の管理者が同時に承認 | `BEGIN IMMEDIATE` 内で状態遷移を確認。**一度しか付与されません** |
| 承認を何度も押す | 同上。2 回目は `🟡 承認待ち からは変更できません` で拒否 |
| 送金せずに申請 | 管理者が入金を確認してから承認します (Bot は自動承認しません) |
| 申請を大量に作る | 同時 **3 件**まで + レート制限 |
| 承認権限の乗っ取り | 既定は Bot Owner のみ。委任は Owner が明示的に行います |
| 期限切れの申請を後から承認 | 状態遷移表で拒否されます |

## 返金について

**LTC の返金は Bot からはできません。** ブロックチェーンは取り消せないため、
`/transaction refund` で内部残高を取り消しても、LTC 自体は戻りません。
返金が必要な場合は、内部残高を取り消したうえで**管理者が手動で送金**してください。
この点は申請画面と `🔍 詳細` にも明記してあります。

PayPay は PayPay 側の操作で対応してください (Bot は内部残高の取り消しのみ行います)。


---

# v4 追加機能ガイド

## 累計チャージによる段位 (自動昇格)

累計チャージ額のしきい値ごとにロールを自動で付けます。**一度得た段位は下がりません**
(返金で累計が減っても剥奪しない)。チャージ完了のたびに判定し、取りこぼしは
15 分ごとのタスクが拾います。

```bash
/tier add name:ブロンズ threshold:10000 role:@ブロンズ description:レート+5%
/tier add name:シルバー threshold:50000 role:@シルバー
/tier list          # 設定した段位の一覧
/tier sweep         # いま条件を満たしている人へまとめて付与 (後から追加したとき用)
/tier remove tier_id:2
```

- 累計は**返金された取引を除いた送金額**で数えます
- 複数のしきい値を一度に超えた場合は、**すべての段位**を付与します
- 付与の記録を先に残してから `add_roles` するため、同じ昇格を二度通知しません
- ロール別レート (`/rate`) と組み合わせると「段位が上がるほどお得」になります

## ランキング報酬の自動配布

締めた期間 (週間・月間) の上位へ、残高とロールを自動で配ります。

```bash
/ranking_reward set period:週間 rank:1 amount:3000 role:@週間1位
/ranking_reward set period:月間 rank:1 amount:10000
/ranking_reward list
/ranking_reward run period:週間        # いますぐ配る (テスト・再配布用)
/ranking_reward remove period:週間 rank:1
```

- 対象は**締め切った直前の期間**です (週は月曜 00:00 JST 始まり)
- 同じ期間に二度配らないよう、配布記録 (`ranking_reward_grants`) で守ります
- 凍結された利用者と、返金された取引は集計から除きます

## 受取用 Kyash アカウントの複数登録

1台が残高しきい値に達してもチャージが止まらないようにします。

```bash
/kyash add label:sub1 threshold:80000 priority:1
/kyash login account:sub1              # 追加したアカウントでログイン
/kyash status                          # 全アカウントの状態を一覧
/kyash priority account:sub1 priority:2
/kyash threshold account:sub1 threshold:50000
/kyash remove account:sub1
```

- 受け取りは**優先度の高い順に、その金額が入るアカウント**を選びます
- 「1台が上限」と「全台が上限」は別扱いです。前者は継続、後者だけ停止します
- 取引ごとに「どのアカウントで受け取ったか」を記録し、確認も必ず同じアカウントで行います
  (他のアカウントの残高増加を自分の入金と誤認しないため)
- 異常・トークン失効・しきい値到達は**アカウント名つきで**通知します
- v3 以前の単一アカウントは、初回起動時に自動で移行します (`label=main`)

## ショップの商品タイプとサブスク

### 商品タイプ

| 種類 | 何が起きるか | 追加で必要な指定 |
|---|---|---|
| ロール付与 | 設定済みのロールを付ける | `role` |
| カスタムロール作成 | 購入者が名前と色を決めたロールを作って付ける | (色を固定するなら `role_color`) |
| ニックネーム変更 | 購入者のニックネームを変える | なし |
| チャージ率ブースト | 一定時間チャージ率が上がる | `bonus_rate` `boost_hours` |
| 専用チャンネル | 購入者だけが見えるチャンネルを作る | (置き場所は `category`) |

```bash
/shop add name:好きな名前のロール price:5000 item_type:カスタムロール作成
/shop add name:率ブースト price:2000 item_type:チャージ率ブースト bonus_rate:20 boost_hours:24 duration_days:1
/shop add name:自分の部屋 price:10000 item_type:専用チャンネル duration_days:30 category:#個室
```

- 入力が必要な商品は、購入ボタンの後に**入力欄 (Modal)** が開きます
- 入力値からはメンション記法・コードブロック記法・表示順を偽装する制御文字を除きます
- Bot が作ったロール・チャンネルと、変更前のニックネームは記録しておき、
  **返金・期限切れ・用意の失敗**で必ず元に戻します
- 購入前に権限と設定を検証するので、「買えたのに渡せない」は起きません
  (万一失敗した場合は自動で返金します)
- チャージ率ブーストは複数持っても**合算せず、最も大きい1つ**だけを適用します

### サブスク (自動更新)

`duration_days` を指定した商品に `subscription:True` を付けると、期限のたびに
残高から自動で引き落として期間を延長します。

```bash
/shop add name:月額VIP price:5000 duration_days:30 subscription:True role:@VIP item_type:ロール付与
/shop subscriptions          # 自動更新中の購入を一覧 (管理者)
/shop cancel purchase_id:12  # 自動更新を止める (利用者本人・期限までは使える)
```

- 更新の **1日前に DM で予告**します (残高不足なら不足額も知らせます)
- 残高が足りなければ自動更新を解除し、**特典もその場で取り消して** DM で知らせます
- 期限の起点は元の期限なので、処理が遅れても期間が削られません
- 返金すると自動更新は必ず止まります (翌期も課金されることはありません)

## オークション

内部残高でロールを競り落とせます。

```bash
/auction create name:レア称号 role:@レア start_price:1000 hours:24 min_increment:500 duration_days:30
/auction list
/auction bid auction_id:1 amount:2000     # パネルが流れた場合の代替
/auction close auction_id:1               # 締切前に締める (管理者)
/auction cancel auction_id:1 reason:中止  # 全額返金して中止 (管理者)
/auction bids auction_id:1                # 入札の履歴 (管理者)
```

- **入札した時点で残高を預かります**。支払えない入札で他の人を押しのけられません
- 上回られたら**その場で全額返金**し、DM で知らせます
- 締切の 2 分前以内の入札では、締切を 2 分延長します (駆け込み入札の対策)
- 落札時に追加の引き落としはありません (入札時に預かった分を充当します)
- 景品のロールを渡せなかった場合は**落札額を返金して中止扱い**にし、管理者へ通知します
- 期間つきの落札ロールは期限で自動的に外れます
  (ショップで同じロールを購入中の場合は外しません)

## サーバー全体のチャージ目標

みんなでチャージを積み上げ、達成したら**期間中にチャージした全員**へ報酬を配ります。

```bash
/goal create name:みんなで10万円 target_amount:100000 reward_amount:500 reward_role:@達成者 days:7 channel:#お知らせ
/goal progress      # いまの進捗と自分の参加額 (利用者も実行可)
/goal list
/goal panel channel:#お知らせ   # 進捗パネルを後から設置
/goal close goal_id:1           # 手動で締める (届いていれば達成扱い)
/goal cancel goal_id:1          # 中止 (報酬は配らない)
/goal grants goal_id:1          # 配布した報酬の一覧
```

- 集計は「期間内に完了したチャージの**送金額**」の合計です
  (付与額ではないので、チャージ率を変えても目標の意味が変わりません)
- 同時に集計できる目標は**1サーバー1つ**です
- 配布記録を先に立ててから残高を足すので、途中で落ちても二重には配りません
- 報酬ロールは目標の開始時に付与できるか検証します (達成後に配れない事態を防ぐため)

## 不正の兆候の自動検知

疑わしい動きを拾って審査チャンネルへ知らせます。
**検知しても自動で凍結・処分は一切しません。** 誤検知で利用者を止める方が害が大きいため、
判断は必ず管理者が行います。

```bash
/fraud list                    # 未処理の検知 (既定)
/fraud list status:すべて kind:送金者名の共有
/fraud scan                    # いますぐ検知を実行
/fraud thresholds              # どの条件で検知しているかを表示
/fraud resolve flag_id:3 note:凍結して対応済み
/fraud ignore flag_id:4 note:本人確認済み
```

| 種類 | 既定の条件 |
|---|---|
| 短時間の大量チャージ | 10 分以内に 5 件以上のチャージ完了 |
| 送金者名の共有 | 同じ Kyash 送金者名を 2 人以上が使用 |
| チャージ直後の使い切りと退出 | 退出前 60 分のチャージ分の 90% 以上を使って退出 |
| 招待報酬のみの収集 | 確定招待が 3 件以上でチャージが 0 件 |
| 返金申請の繰り返し | 返金申請が 3 件以上 |

- 検知の記録に**送金者名そのものは残しません** (先頭だけ残して伏せます)
- 同じ利用者・同じ種別の未処理フラグは1つだけです (通知が埋もれないように)
- 検知カードから `✅ 対処済み` `⚪ 問題なし` `🔎 詳細` を操作できます (メモを残せます)
- `/inspect` の調査ビューにも、その利用者の検知履歴が表示されます

## 日別推移のグラフ

```bash
/stats days:14        # 直近14日のグラフつき統計 (0 でグラフなし・最大90日)
/stats days:30 global_scope:True   # Bot 全体 (Bot Owner のみ)
```

- 棒グラフが送金額、折れ線が件数です (件数の目盛りは右側)
- チャージの無かった日も 0 として必ず表示します (日付が飛ぶと傾きが嘘になるため)
- 日次サマリにも直近 14 日のグラフを添付します
- 描画は **Pillow だけ**で行い、集計データを外部サービスへ送りません
- Pillow が無い環境でもグラフを省いて動作します (数字は Embed に出ます)

日本語のラベルには日本語フォントが必要です。無い場合は英字ラベルへ自動で切り替わります。

```bash
sudo apt-get install -y fonts-noto-cjk    # または fonts-ipafont-gothic
```

## 返金申請フロー

利用者が申請し、管理者が審査します。

```bash
/refund request transaction_id:TX-XXXX reason:間違えてチャージしました
/refund list                  # 自分の申請 (管理者は全員ぶん)
/refund cancel request_id:5   # 審査前なら本人が取り下げ可
/refund approve request_id:5  # 承認して内部残高を取り消す (管理者)
/refund reject request_id:5 reason:対象外です
```

- **この手続きで戻るのはサーバー内の残高の取消だけです。**
  Kyash へ実際にお金を返す作業は、承認後に**管理者が手作業**で行います
  (添付モジュールに送金の手段が無いため、自動送金は実装していません)
- 申請できるのはチャージ完了から **14 日以内**、審査待ちは**同時に 2 件まで**です
- 承認は申請の状態を進めてから残高を取り消します
  (逆順だと、途中で落ちたときに「残高は減ったが審査待ち」の状態が残るため)
- 審査カードの `✅ 承認して取消` は二段階で確認します

## 署名つきレシート (チャージの控え)

```bash
/receipt show                          # 直近のチャージの控え
/receipt show transaction_id:TX-XXXX
/receipt verify code:R1.XXXX.YYYY      # 控えが本物かを確認
```

- 控えは HMAC-SHA256 で署名してあるため、**利用者は偽造できません**
- 検証は署名の一致に加えて、**いまの記録との一致**も確認します。
  取消された取引や、金額が書き換えられた取引の控えは「無効」と判定します
- 署名鍵は `data/receipt.key` (0600) に保存します。
  Kyash トークンの暗号鍵とは**別のファイル**です (片方が漏れても他方に影響させないため)
- 控えのコードを他人に見せても、残高を操作されることはありません

> `data/receipt.key` を失うと、それ以前に発行した控えは検証できなくなります。
> バックアップに含めてください (`/backup` の対象は DB のみです)。

## v4 で必要になる Discord 権限

追加した機能で使う権限です。使わない機能の権限は不要です。

| 権限 | 使う機能 |
|---|---|
| ロールの管理 | 段位 / ランキング報酬 / 目標報酬 / オークション / カスタムロール |
| ニックネームの管理 | ショップのニックネーム変更 |
| チャンネルの管理 | ショップの専用チャンネル |
| ファイルを添付 | 日別推移のグラフ |

## v3 からのアップグレード

1. Bot を停止する (`sudo systemctl stop discord-charge-bot`)
2. DB をバックアップする (`cp data/charge_bot.db data/charge_bot.db.bak`)
3. ファイル一式を差し替える (`main.py` の3項目は書き換え直す)
4. 依存を更新する (`./venv/bin/pip install -r requirements.txt`)
   - グラフ描画のため **Pillow** が追加されています
5. Bot を起動する (`sudo systemctl start discord-charge-bot`)
   - スキーマは v3 → v4 へ自動移行します (既存データはそのまま)
   - 受取用 Kyash アカウントは複数対応の表へ自動で移ります (再ログインは不要)
   - 起動時にコマンドを同期します。反映まで数分かかることがあります

移行後に確認すること:

```bash
/kyash status      # 受取アカウントが「main」として残っているか
/stats days:14     # グラフが表示されるか (出ない場合は Pillow と日本語フォント)
/shop list         # 既存の商品が「ロール付与」として残っているか
```

---

# v2 追加機能ガイド

## VIP ロールショップ

内部残高でロールを購入でき、購入したロールで**チャージ率が上がる**循環を作れます。

```bash
# 1) ロール別チャージ率を設定 (VIP を持つ人は 150%)
/rate set role:@VIP charge_rate:150 priority:10

# 2) そのロールを商品として販売
/shop add role:@VIP name:VIPロール price:5000 duration_days:30 stock:10 purchase_limit:1

# 3) ショップパネルを設置 (利用者はボタンから購入)
/shop panel channel:#shop
```

- **購入フロー**: 残高の引き落としと購入記録を**単一トランザクション**で確定 → ロール付与 →
  付与に失敗した場合は**自動で全額返金**します (残高が消えて終わることはありません)。
- **前提条件**: Bot に「ロールの管理」権限が必要で、**Bot のロールが販売するロールより上**にある
  必要があります。`/shop add` 実行時に検証し、満たない場合は登録を拒否します。
- **期限付きロール**: `duration_days` を設定すると期限切れで自動剥奪します
  (`TASK_SHOP_EXPIRY_INTERVAL` = 5分ごとに確認)。同じロールの有効な購入が他に残っている場合は剥奪しません。
- **管理**: `/shop list` `/shop edit` `/shop remove` `/shop log` `/shop refund`
- 適用されたチャージ率は Transaction 作成時に保存されるため、**後でレートを変えても過去の取引は変わりません**。

## 招待キャンペーン

### 開始手順

```bash
# 1) Bot に「サーバー管理」権限を付与する (招待者の特定に必須)
# 2) キャンペーンを開始
/campaign create name:春キャンペーン inviter_reward:500 invited_reward:300 \
    preset:標準 confirm_condition:チャージ完了で確定

# 3) 招待パネルを設置
/campaign panel channel:#invite

# 4) 招待ランキングパネル (任意)
/ranking_panel channel:#ranking type:招待ランキング
```

### 不正対策の仕組み (多層)

| 層 | 対策 |
|---|---|
| 帰属判定 | Bot が**個人専用の招待リンク**を発行し、参加時に招待の使用回数差分で招待者を特定 |
| 報酬の確定 | 参加時点では **PENDING (保留)**。条件 (既定: 招待された人のチャージ完了) を満たして初めて確定 |
| 冪等性 | `UNIQUE(guild_id, invited_id)` と `balance_history(transaction_id, type)` の UNIQUE 制約で**二重報酬を不可能に** |
| 自己招待 | 招待者と参加者が同一なら無効 |
| 再入場 | `guild_member_history` に初回参加を永続記録。**退出→再入場による周回は無効** |
| 捨てアカウント | Discord アカウント作成からの経過日数が条件未満なら無効 (標準: 7日) |
| 量産 | 招待者ごとの日次上限 (標準5人) / 累計上限 (標準50人)。**保留中も上限を消費** |
| Bot | Bot アカウントは無効 |
| 帰属不能 | バニティURL・サーバー発見経由は「招待者不明」として無効 (Discord の仕様上特定できません) |
| 曖昧 | 同時に複数の招待が使われた場合は保留 |
| 不審パターン | 短時間の大量参加 (5分に4件以上) / 招待者と参加者の**アカウント作成日が2日以内**は**保留 → 管理者レビュー** |
| ブラックリスト | `/campaign blacklist add` で招待報酬の対象外に |

> **却下と保留を分けています。** 明確な違反は自動で無効化し、「疑わしいが断定できない」ケースは
> 自動で剥奪せず保留し、`/campaign review` で管理者が判断します。

### 管理

```bash
/campaign review                    # 保留中の一覧
/campaign review record_id:12 approve:True reason:確認済み
/campaign stats                     # 確定/保留/無効の内訳と招待ランキング
/campaign blacklist action:追加 user:@spammer reason:不正招待
/campaign edit daily_limit:3        # 開催中に上限を変更
/campaign end                       # 終了
```

## 管理者による詳細な残高操作

| コマンド | 内容 |
|---|---|
| `/balance add/remove/set` | 加算・減算・設定 (減算と設定は確認ボタン) |
| `/balance move <from> <to> <amount>` | **利用者間の付け替え** (誤付与の是正。両者を単一トランザクションで更新) |
| `/balance ledger [user] [type] [date] [operator]` | **残高台帳の検索** (種別・期間・操作者で絞り込み) |
| `/balance undo <history_id>` | **残高操作1件を逆仕訳で取消** (元の履歴は書き換えず、二重取消も不可) |
| `/balance audit <user>` | 残高と履歴合計の**突合** (種別ごとの内訳つき) |
| `/balance repair <user> <mode>` | 不一致の**修復** (Owner 限定・2段階確認) |
| `/balance distribution` | 分布分析 (中位値・上位10%占有率・発行/消費総額) |
| `/balance bulk <role> <operation>` | ロール保持者へ**一括操作** (既定はドライラン) |
| `/inspect <user>` | 残高・順位・取引・招待・購入をまとめて確認 |
| `/transaction refund <tx_id>` | **完了済みチャージの取消** (原取引に紐づく逆仕訳 + 利用者へ通知) |

### 残高操作ログチャンネル

```bash
/settings balance_log_channel channel:#balance-log
/settings balance_log_scope scope:管理者の手動操作のみ   # または「すべての残高変動」
```

- 記録内容: 種別・対象・操作者・**変更前→変更後**・差分・理由・操作ID・履歴ID・取引ID
- **@everyone が閲覧できるチャンネルを指定すると確認を求めます** (他人の残高が見えるため)
- 送信に失敗しても残高操作自体は成功したまま維持されます
- `repair` を実行すると Owner へも通知されます

### 修復モードの違い

| mode | 動作 | 使いどころ |
|---|---|---|
| `history` | 履歴に差分行 (RECONCILE) を追記。**残高は変えない** | 残高が正しく、履歴が欠けている場合 |
| `balance` | 残高を履歴合計へ戻す。経済的増減なしとして記録 (change=0) | 履歴が正しく、残高が壊れた場合 |

## 管理ダッシュボード / 調査ビュー

```bash
/admin_panel channel:#admin      # 常設パネル (60秒ごとに自動更新)
/inspect user:@someone           # 1画面での統合調査
```

- ダッシュボード: キュー滞留・Kyash 状態・**トークン残り日数**・要確認件数・本日の統計・メトリクス
  ＋ボタン (更新 / メンテ切替 / キュー / 要確認)。**ボタン押下時にも毎回権限を確認**します。
- `/inspect`: 残高・突合結果・チャージ実績・凍結・クールダウン・**適用レート**・アカウント作成日・
  参加/退出回数・進行中取引・直近取引・招待実績・購入履歴・**注意すべき点の自動抽出**

## 運用の安全装置 (v2)

| 機能 | 設定 | 内容 |
|---|---|---|
| トークン失効の事前警告 | 自動 | 残り5日を切ると Owner へ**1日1回**通知 (自動更新はしません) |
| 受取残高しきい値 | `/kyash threshold amount:` | 到達で**新規チャージを停止**し Owner へ通知。受取前に判定するため取りこぼしません |
| 残高上限 | `/settings max_balance` | 上限を超えるチャージを**送金前に拒否** |
| 連続失敗クールダウン | 自動 | 5回連続失敗で10分制限。`/user cooldown` で解除 |
| 確認中のロック緩和 | `/settings manual_review_allow_new` | 手動確認待ちでも新規チャージを許可できる (既定は安全側で不可) |
| 手動確認の再通知 | 自動 | 30分以上未解決の案件を管理者へ再通知 |
| 死活監視 | `/system heartbeat url:` | `data/heartbeat` の更新＋外部URLへ定期通知 (クラッシュループの検知) |
| バックアップ外部保存 | `/system backup_remote` | Owner DM 送信 / 別ディレクトリへコピー |
| 期限付きサーバー許可 | `/server suspend guild_id: until:` | 期限を過ぎると自動で利用不可に |
| 日次サマリ | `/settings summary_channel` | 毎日 JST 00:05 に前日分を自動投稿 |
| 設定の入出力 | `/config export` / `import` / `reset` | 複数サーバーの初期構築を高速化 |
| CSV 出力 | `/export transactions|balances|ledger|audit` | 監査・経理用 (Excel 互換の BOM 付き UTF-8) |

## 期間別ランキング

同じサーバーに複数の集計方式のパネルを同時設置できます (すべて独立)。

```bash
/ranking_panel channel:#ranking type:残高ランキング
/ranking_panel channel:#weekly  type:週間チャージランキング
/ranking_panel channel:#monthly type:月間チャージランキング
/ranking_panel channel:#invite  type:招待ランキング
```

- 週間 = 直近7日 / 月間 = 当月 (JST) の**獲得残高**を集計。**取消済みチャージは除外**します。
- 招待 = 確定した招待数。
- 表示内容が前回と同じ場合は Discord API を呼びません (集計方式ごとに署名を比較)。

## v2 で必要になる Discord 権限

| 権限 | 用途 | 必須か |
|---|---|---|
| View Channel / Send Messages / Embed Links / Read Message History | 基本機能 | 必須 |
| **Manage Roles** | ショップのロール付与・剥奪 | ショップを使う場合 |
| **Create Invite** | 個人専用招待リンクの発行 | 招待キャンペーンを使う場合 |
| **Manage Server** | 招待の使用回数の取得 (**招待者の特定**) | 招待キャンペーンを使う場合 |

> ショップを使う場合、**Bot のロールを販売対象のロールより上**に配置してください。
> `/setup` で権限の状態も確認できます。

## v1 からのアップグレード

```bash
sudo systemctl stop discord-charge-bot
cd /opt/discord-charge-bot
sudo -u chargebot cp data/charge_bot.db data/charge_bot.db.pre-v2   # 念のため
# v2 のファイル一式を配置 (main.py の3項目は書き換え直してください)
sudo -u chargebot ./venv/bin/pip install -r requirements.txt
sudo systemctl start discord-charge-bot
journalctl -u discord-charge-bot -n 50
```

- スキーマは**起動時に自動で v1 → v2 へ移行**します (テーブル作成 → 列追加 → インデックス作成の順)。
- 残高・履歴・取引・パネル・設定はすべて保持されます (`tests/test_migration.py` で検証済み)。
- 移行後、追加設定は既定値 (残高上限=無制限 / 残高ログ=未設定 / ショップ=有効) になります。
- ダウングレードはできません (v2 の DB を v1 のコードで開くと起動時に安全停止します)。

## テスト

```bash
python3 tests/test_charge_flow.py    # チャージ本体 (87項目)
python3 tests/test_v2_features.py    # ショップ/招待/残高操作 (79項目)
python3 tests/test_migration.py      # v1 → v2 移行 (13項目)
```

GitHub Actions (`.github/workflows/test.yml`) で push 時に lint + 起動前チェック + 全テストを実行します。
