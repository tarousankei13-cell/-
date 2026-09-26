# 高性能 Kyash 自動チャージ Discord Bot / 運用ドキュメント

Discord サーバーごとに独立した **内部残高** を管理するチャージ Bot です。
利用者は常設チャージパネルから金額を入力し、Kyash の **送金リンク** を提出します。
Bot は管理者が登録した **受取用 Kyash アカウント** でリンクを検証・受取し、
サーバーごとのチャージ率を適用して内部残高を付与します。

> 内部残高は Discord サーバー内の数値です。現金化・出金・外部商品との交換には対応しません。

- Bot バージョン: `1.0.0` / DB スキーマ: `v1`
- 想定環境: Python 3.11+ / discord.py 2.x / SQLite (WAL) / Linux VPS + systemd
- Kyash 連携: 同梱の添付モジュール `vendor/Kyasher` (Kyasher 1.5.0) のみを使用

---

## C. ディレクトリ構成

```
discord-charge-bot/
├── main.py                     # Token/Owner/Bootstrap の直書き・Bot本体・起動/終了・パネル操作ハンドラ
├── config.py                   # 定数・デフォルト値・状態定義・エラーコード・配色
├── utils.py                    # 金額(Decimal)・リンク正規化/ハッシュ・マスキング・レート制限・トークン暗号化
├── database.py                 # SQLite スキーマ / マイグレーション / 全クエリ / 冪等な残高付与
├── kyash_service.py            # 添付モジュールの安全な抽象化 (直列化・タイムアウト・受取確認)
├── charge_service.py           # チャージのライフサイクル・キュー処理・通知・ランキング更新
├── ui.py                       # Embed / Persistent View / Modal
├── commands.py                 # スラッシュコマンド (Owner / Admin)
├── tasks.py                    # バックグラウンドタスク (キューワーカー等) + 自己復旧監視
├── requirements.txt
├── discord-charge-bot.service  # systemd unit
├── tests/
│   └── test_charge_flow.py     # 統合テスト (Kyash の HTTP のみモック / 79項目)
├── vendor/
│   └── Kyasher/                # 添付モジュール (無変更で同梱・監査用)
└── data/                       # 実行時に生成 (DB / バックアップ / 暗号化キー)
    ├── charge_bot.db
    ├── secret.key              # 0600
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
| `/data delete [guild_id]` | サーバーデータの完全削除 (**2段階確認** + 削除前バックアップ) |
| `/backup` | DB バックアップの手動実行 |
| `/stats global_scope:True` | Bot 全体の統計 |

### Server Admin (管理者ロール / サーバーオーナー / Bot Owner)

| コマンド | 内容 |
|---|---|
| `/setup` | 初期設定チェックリスト |
| `/config` | 現在の設定一覧 (チャージ設定とランキング設定を分けて表示) |
| `/charge_panel` | チャージパネルを新規設置 (既存は削除しない・複数設置可) |
| `/charge_panels [disable_message_id]` | チャージパネル一覧 / 指定パネルの無効化 |
| `/ranking_panel [channel]` | ランキングパネルを新規設置 (複数設置可) |
| `/ranking_panels [disable_message_id]` | ランキングパネル一覧 / 指定パネルの無効化 |
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
| `/balance info <user>` | 残高・順位・残高変更履歴 |
| `/user freeze <user> <reason>` | 凍結 (チャージ不可・ランキング対象外・残高確認は可) |
| `/user unfreeze <user>` | 凍結解除 |
| `/user info <user>` | 利用者の状態・進行中の取引 |
| `/history [user] [transaction_id] [status] [date] [amount_min] [amount_max] [page]` | 取引検索 |
| `/transaction info <tx_id>` | 取引の詳細 |
| `/transaction verify <tx_id>` | Kyash 側の受取状態を再確認 |
| `/transaction resolve <tx_id> <complete> <reason>` | 確認中の取引を完了/失敗で確定 (確認ボタン) |
| `/transaction retry <tx_id>` | **未受取を確認できた場合のみ** 再受取キューへ戻す |
| `/stats` | 統計 (回数・送金額・付与残高・今日/今月・成功率・キュー・ユーザー数・稼働時間) |
| `/queue` | 受取キュー (処理中 / 待機 / 手動確認待ち) |
| `/system` | Bot・DB・Kyash・キュー・タスク稼働・バージョン |
| `/logs [action] [actor] [page]` | 監査ログ |
| `/maintenance on` / `off` | メンテナンス (新規チャージ停止・読み取り機能は継続) |
| `/emergency_stop on` / `off` | 緊急停止 (新規チャージ + 新規受取を停止・確認ボタン) |
| `/achievement proxy <user> <amount> <reason> [charge_rate] [credited_amount]` | 代理実績 (確認ボタン) |

---

## G. 利用者の操作 (コマンド不要)

チャージパネルのボタンだけで完結します。

| ボタン | 内容 |
|---|---|
| 💰 チャージ | 金額入力 Modal → 送金リンク送信ボタン |
| 💳 残高 | 現在残高・累計チャージ回数・累計獲得 (Ephemeral) |
| 📜 履歴 | 自分の履歴のみ・ページング (Ephemeral) |
| ❓ ヘルプ | チャージ方法・チャージ率・上限・注意事項 (Ephemeral) |
| 🔄 更新 | パネル表示を最新の設定値に更新 |

### チャージの流れ

```
💰 チャージ → 金額入力 → 「🔗 送金リンクを送信」 → リンク入力 (Ephemeral Modal)
   → 自動検証 (金額一致・受取可否) → 受取キュー → 自動受取 → 受取確認
   → チャージ率適用 → 内部残高へ加算 → DM 通知 → 実績投稿 → ランキング更新
```

- 送金リンクは **Ephemeral Modal** で入力するため、公開チャンネルに残りません。
- 入力した金額と送金リンクの金額が **一致しない場合は処理しません** (差額処理もしません)。
- 請求リンクは受け取れません (送金リンクを作成してください)。
- リンク入力期限は約 15 分、同時に進行できるチャージは 1 ユーザー 1 件です。

---

## H. ランキングパネルの設定 (チャージパネルとは完全に独立)

ランキングは **専用パネル** で提供します。チャージパネルにランキングボタンは追加されません。

```bash
/ranking_panel                 # 実行したチャンネルへ設置
/ranking_panel channel:#ranking  # 指定チャンネルへ設置
```

- 同じサーバーの複数チャンネルへ設置可能 (`#ranking` と `#balance-ranking` など)。
  すべて同じサーバーのランキングを表示します。
- 既存パネルは削除されません。無効化は `/ranking_panels disable_message_id:<ID>`。
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
./venv/bin/python3 tests/test_charge_flow.py
# → 結果: 79 件成功 / 0 件失敗
```

検証内容: 金額検証 / 正常チャージ (130%→1300) / 二重付与防止 / 同一リンク再利用拒否 /
金額不一致 / 請求リンク・無効リンク / 同一ユーザーの二重チャージ / **同一リンクの同時送信** /
タイムアウト後の未受取確認と再試行 / 受取応答はあるが痕跡なし → MANUAL_REVIEW /
受取拒否 / **再起動復旧 (受取済み→付与 / 不明→手動確認・再受取しない)** /
管理者確定 / 日次上限 / 凍結 / メンテナンス / 緊急停止 / 未許可サーバー / サーバー間分離 /
期限切れ / 管理者残高操作 / ランキング (降順・同額順・凍結除外・署名) / 代理実績 /
端数処理 / チャージ率スナップショット / 整合性 / 統計 / バックアップ / 通知キュー /
レート制限 / コマンドツリー / 再起動後の永続化

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

### 実績・DM が届かない

- DM 拒否ユーザー / 権限不足でも **チャージ自体は成功扱い** です。
- 送信に失敗した通知は `notification_queue` に積まれ、1分間隔で最大5回再送します。
- 実績チャンネルの権限 (メッセージ送信 / 埋め込みリンク) を確認してください。

### ランキングが更新されない

- `/settings ranking_enabled true` を確認
- `/ranking_panels` でパネルが 🟢 有効か確認 (メッセージ削除時は自動で無効化されます)
- **表示内容が前回と同じ場合は意図的に更新しません**。🔄 更新ボタンで強制再取得できます。

### DB / データ

```bash
# 整合性チェック (6時間ごとに自動実行・問題は Owner へ DM 通知)
sqlite3 data/charge_bot.db "PRAGMA integrity_check;"

# バックアップ (毎日自動 / 14世代保持)
ls -l data/backups/
/backup            # Discord から手動実行

# リストア (Bot を停止してから)
sudo systemctl stop discord-charge-bot
cp data/backups/charge_bot_YYYYmmdd_HHMMSS.db data/charge_bot.db
sudo systemctl start discord-charge-bot
```

### エラーコード

利用者には安全な日本語メッセージのみ表示され、管理者向けにはコードと詳細が残ります。

`INVALID_AMOUNT` / `AMOUNT_MISMATCH` / `AMOUNT_BELOW_MIN` / `AMOUNT_ABOVE_MAX` /
`DAILY_LIMIT_EXCEEDED` / `GUILD_DAILY_LIMIT_EXCEEDED` / `INVALID_LINK` / `LINK_IS_CLAIM` /
`LINK_EXPIRED` / `LINK_ALREADY_USED` / `KYASH_AUTH_ERROR` / `KYASH_TIMEOUT` /
`KYASH_NETWORK_ERROR` / `KYASH_REJECTED` / `KYASH_UNAVAILABLE` / `DATABASE_ERROR` /
`USER_FROZEN` / `GUILD_DISABLED` / `MAINTENANCE` / `EMERGENCY_STOP` /
`TRANSACTION_EXPIRED` / `ACTIVE_TRANSACTION_EXISTS` / `RATE_LIMITED` / `MANUAL_REVIEW` /
`UNKNOWN_ERROR`

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

## セキュリティ / 整合性の方針

- **秘密情報をログ・DB に残さない**: Token / パスワード / OTP / Cookie / Authorization /
  完全な送金リンクは出力しません (`utils.sanitize_for_log` でマスク)。
- **送金リンクの完全な URL は保存しない**: 二重送信判定用の SHA-256 ハッシュと、
  受取・状態確認に必要なリンクUUIDのみ保存します。
- **パスワードは保存しない**: アクセストークンのみ `data/secret.key` (0600) で暗号化保存。
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
