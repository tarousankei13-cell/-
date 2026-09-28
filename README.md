# McDonald's Concierge Bot

Discord上でMcDonald'sモバイルオーダーの代行注文を管理するBotです。

## 機能

- パネルベースのUI（ボタン操作、再起動後も持続）
- Hex入力による注文解析・実行
- 残高管理（入金申請 → 管理者承認フロー）
- 注文履歴・取引履歴（ページネーション付き）
- 管理者コマンド一式（残高操作・注文管理・統計・エクスポート）
- 実績投稿チャンネル
- 管理者ログチャンネル
- メンテナンスモード
- 注文完了画像の自動生成（実際の注文番号を差し込んだ完了画面を投稿）

## セットアップ

### 1. 前提条件

- Python 3.11+
- Discord Bot Token ([Discord Developer Portal](https://discord.com/developers/applications))

### 2. Bot権限

Discord Developer Portalで以下を設定:

- **Bot Permissions**: Send Messages, Embed Links, Attach Files, Read Message History, Use Slash Commands
- **Privileged Intents**: Message Content Intent

### 3. インストール

```bash
pip install -r requirements.txt
```

### 4. 環境変数

`.env.example` をコピーして `.env` を作成:

```bash
cp .env.example .env
```

必須項目:
- `DISCORD_TOKEN` - Discord Bot Token
- `OWNER_IDS` - 管理者のDiscordユーザーID（カンマ区切り）

任意項目:
- `MCD_REFRESH_TOKEN` - McDonald's API refresh token（未設定時は手動処理モード）

### 5. 起動

```bash
python main.py
```

### 6. 初期設定（Discord上）

1. `/setup_panel` でパネルを設置
2. `/admin channel achievement #channel` で実績チャンネルを設定
3. `/admin channel admin_log #channel` で管理者ログチャンネルを設定
4. `/admin rate 60` でユーザー負担率を設定（60 = 40%OFF）
5. `/admin minimum 400` で最低注文額を設定

## 管理者コマンド

| コマンド | 説明 |
|---------|------|
| `/setup_panel` | パネルを設置 |
| `/restart` | Bot再起動 |
| `/admin balance add/remove/set/view/history/top` | 残高管理 |
| `/admin order view/search/complete/refund/review/retry` | 注文管理 |
| `/admin deposits list/approve/reject` | 入金管理 |
| `/admin channel achievement/admin_log` | チャンネル設定 |
| `/admin rate` | 負担率設定 |
| `/admin minimum` | 最低注文額設定 |
| `/admin maintenance` | メンテナンスモード |
| `/admin accepting` | 注文受付ON/OFF |
| `/admin stats` | 統計情報 |
| `/admin user` | ユーザー情報 |
| `/admin export` | データエクスポート（CSV） |
| `/admin panel refresh/delete` | パネル管理 |

## データベース

SQLite（`concierge.db`）を使用。テーブル:

- `users` - ユーザー情報・残高
- `transactions` - 全取引履歴
- `orders` - 注文情報
- `deposits` - 入金申請
- `panels` - パネル位置情報
- `settings` - 設定値

## テスト

```bash
python -m pytest tests/ -v
```

## ファイル構成

```
├── main.py           # エントリポイント
├── models.py         # データモデル・定数
├── db.py             # データベース層
├── views.py          # Discord UIコンポーネント
├── mcd_adapter.py    # McDonald's API連携
├── image_gen.py      # 注文完了画像の生成
├── assets/
│   └── order_complete_template.png  # 完了画面テンプレート
├── cogs/
│   └── admin.py      # 管理者コマンド
├── tests/
│   └── test_bot.py   # テスト
├── requirements.txt
├── .env.example
└── README.md
```

## 注意事項

- 本番運用時はsystemdなどでプロセス管理を推奨
- SQLiteのバックアップを定期的に行うこと
- `MCD_REFRESH_TOKEN` 未設定時は全注文が手動確認（manual_review）になります
