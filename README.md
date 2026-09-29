# McDonald's Concierge Bot

Discord上でMcDonald'sモバイルオーダーの代行注文を管理するBotです。

## 機能

**注文**
- パネルベースのUI（ボタン操作、再起動後も持続）
- Hex入力による注文解析・実行
- お気に入り登録＆ワンタップ再注文
- 注文完了画像の自動生成（実際の注文番号を差し込んだ完了画面）
- 注文ステータスのDM通知
- 一時的な通信障害の自動リトライ（指数バックオフ）

**残高・特典**
- 残高管理（入金申請 → 管理者承認フロー）
- 残高不足時の自動入金案内
- ランク制度（利用回数で割引率アップ）
- ポイント還元＆残高への交換
- クーポン／キャンペーン
- VIP個別負担率

**管理**
- 管理者コマンド一式（残高・注文・入金・統計・エクスポート）
- 実績投稿チャンネル＋代理投稿・再投稿
- 管理者ログチャンネル
- 日次レポート自動送信（ASCIIグラフ付き）
- ブラックリスト
- 商品名辞書（CSV一括登録・未登録ID検出）
- 各種上限／クールダウン設定
- Hex重複利用の防止

**運用**
- SQLite自動バックアップ（世代管理）
- スキーマ自動マイグレーション
- Graceful Shutdown（処理中の注文を待って終了）
- 設定・残高のメモリキャッシュ
- Docker対応

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

### 4. トークン設定

`main.py` の先頭を編集します。

```python
DISCORD_TOKEN = "ここにBotトークンを貼り付け"
OWNER_IDS = {123456789012345678}   # 管理者のDiscordユーザーID（複数可）
MCD_REFRESH_TOKEN = ""             # 任意。未設定なら手動処理モード
```

### 5. 起動

```bash
python main.py
```

Dockerの場合:

```bash
docker compose up -d
```

### 6. 初期設定（Discord上）

```
/setup_panel                          パネルを設置
/admin channel achievement #実績       実績チャンネルを設定
/admin channel admin_log #ログ         管理者ログチャンネルを設定
/admin rate 60                        負担率60%（40% OFF）
/admin minimum 400                    最低注文額
```

コマンドが表示されない場合は `/sync` または `!sync` を実行してください。

設定に問題がないかは **`/admin diagnose`** で自動診断できます。

## コマンド一覧

### ユーザー

| コマンド | 説明 |
|---------|------|
| `/profile` | ランク・残高・ポイント・節約総額を表示 |
| `/coupon <code>` | クーポンを適用（次回注文で自動使用） |
| `/points` | ポイント確認・残高への交換 |
| `/favorites` | お気に入りから再注文 |
| `/notify <bool>` | DM通知のON/OFF |

### 管理者

| コマンド | 説明 |
|---------|------|
| `/setup_panel` | パネルを設置 |
| `/sync [scope]` | コマンド同期（global＝既定 / repair＝重複修復 / guild＝即時） |
| `!sync [scope]` | テキスト版の同期（スラッシュが壊れた時用） |
| `/admin diagnose` | 設定の問題を自動診断 |
| `/restart` | Bot再起動 |
| `/admin balance add/remove/set/view/history/top` | 残高管理 |
| `/admin order view/search/complete/refund/review/retry` | 注文管理 |
| `/admin achievement repost/post/preview` | 実績の再投稿・代理投稿・画像プレビュー |
| `/admin deposits list/approve/reject` | 入金管理 |
| `/admin blacklist add/remove/list` | 利用制限 |
| `/admin vip set/clear/list` | 個別負担率 |
| `/admin coupon create/list/delete` | クーポン |
| `/admin campaign start/stop` | キャンペーン |
| `/admin points add/rate` | ポイント付与・還元率 |
| `/admin product add/list/delete/unknown/import` | 商品名辞書 |
| `/admin channel achievement/admin_log/report` | チャンネル設定 |
| `/admin limit order_max/deposit/daily/cooldown/hex_check` | 上限・制限 |
| `/admin backup now/list/config` | バックアップ |
| `/admin report now/config` | レポート |
| `/admin panel refresh/delete` | パネル管理 |
| `/admin rate` / `minimum` / `maintenance` / `accepting` | 基本設定 |
| `/admin stats` / `user` / `settings` / `export` | 情報・出力 |

## 割引率の決まり方

適用順に上書き・加算されます。

1. **基本負担率** — `/admin rate` の設定値
2. **VIP個別率** — 設定されていれば基本値を置き換え
3. **キャンペーン** — より有利なら適用
4. **ランク割引** — 完了注文数に応じて減算
5. **クーポン** — 適用中なら減算

最終的に 1〜100% にクランプされます。

### ランク

| ランク | 必要完了数 | 追加割引 |
|-------|----------|---------|
| 🥉 ブロンズ | 0 | +0% |
| 🥈 シルバー | 10 | +2% |
| 🥇 ゴールド | 30 | +4% |
| 💎 プラチナ | 60 | +6% |
| 👑 ダイヤモンド | 100 | +8% |

## 実績の代理投稿

実績の送信に失敗した場合に使えます。**通常の実績と見た目は完全に同一**です。

```
/admin achievement repost <order_id>
```
既存注文のデータをそのまま再投稿します（完了画像付き）。

```
/admin achievement post <total_amount> <user_amount> [store] [receipt_number] [order_id]
```
DBに存在しない実績を手動で投稿します。`order_id` 省略時は最新注文IDの次が使われます。

## データベース

SQLite（`concierge.db`、スキーマ v2）。テーブル:

| テーブル | 内容 |
|---------|------|
| `users` | 残高・ポイント・ランク関連・VIP率・制限状態 |
| `transactions` | 全取引履歴（残高の増減を完全記録） |
| `orders` | 注文情報（Hexハッシュ・適用率・リトライ回数含む） |
| `deposits` | 入金申請 |
| `favorites` | お気に入り |
| `coupons` / `coupon_uses` | クーポンと使用履歴 |
| `product_names` / `unknown_products` | 商品名辞書と未登録ID |
| `panels` | パネル位置情報 |
| `settings` | 設定値 |

起動時に既存DBのスキーマを自動でマイグレーションします（既存データは保持）。

## テスト

```bash
python -m unittest tests.test_bot -v
```

87件のテストが含まれます（残高整合性・二重処理防止・レース条件・マイグレーション・割引率解決・画像生成など）。

## ファイル構成

```
├── main.py              # エントリポイント（トークン設定もここ）
├── models.py            # データモデル・定数・ランク定義
├── db.py                # データベース層（キャッシュ・マイグレーション）
├── views.py             # Discord UIコンポーネント
├── mcd_adapter.py       # McDonald's API連携（aiohttp・リトライ）
├── image_gen.py         # 注文完了画像の生成
├── assets/
│   └── order_complete_template.png
├── cogs/
│   ├── admin.py         # 管理者コマンド
│   ├── user.py          # ユーザーコマンド
│   └── tasks.py         # 自動バックアップ・日次レポート
├── tests/test_bot.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## トラブルシューティング

まず **`/admin diagnose`** を実行してください。外部APIの設定漏れ、チャンネル権限、コマンド重複、メンテナンスモードの状態などを自動で検出します。

### コマンドが2つずつ表示される

グローバルコマンドとサーバー個別コマンドが**両方**登録されている状態です。

```
/sync repair
```

サーバー個別のコマンドを削除し、グローバルのみに統一します（`!sync repair` でも可）。

以後は `/sync`（既定＝グローバル）を使ってください。`/sync guild` は即時反映される代わりに、グローバル側が残っていると重複の原因になります。

### 正しいHexなのに注文できない

| 表示 | 原因と対処 |
|------|-----------|
| 入力エラー（長さが不正） | Hexが途中で切れています。4000文字を超える場合は2つ目の入力欄に続きを貼ってください |
| 重複した注文 | そのHexは既に注文済みです。新しくHexを取得してください |
| 残高不足 | 入金が必要です。表示されるボタンから申請できます |
| 解析エラー（金額を取得できません） | Hexの形式が想定と異なります |
| 操作が早すぎます | クールダウン中です。`/admin limit cooldown` で調整できます |
| 🔍 確認待ちになる | `MCD_REFRESH_TOKEN` が未設定です。設定しない限り自動決済されません |

改行・空白・全角文字が混ざったHexは自動で正規化されるため、そのまま貼り付けて問題ありません。

### 入金できない

- `最低入金額は ¥XXX です` → `/admin limit deposit` で下限を調整
- `操作が早すぎます` → `/admin limit cooldown` でクールダウンを調整
- 承認ボタンが出ない → `/admin channel admin_log` が未設定です。設定しない場合は `/admin deposits approve <ID>` で承認してください

## 注意事項

- 商品名辞書は初期状態では空です。`/admin product unknown` で実際に出現した商品IDを確認し、`/admin product add` または `/admin product import`（CSV）で登録してください。
- `MCD_REFRESH_TOKEN` 未設定時は全注文が手動確認（manual_review）になります。
- 決済の二重実行を防ぐため、**タイムアウトは自動リトライしません**（要確認扱い）。自動リトライは接続拒否など「リクエストが届いていないことが明らかな障害」のみ対象です。
- 本番運用ではsystemdまたはDockerでのプロセス管理を推奨します。`stop_grace_period` は Graceful Shutdown 用に40秒を確保しています。
- バックアップは `backups/` に保存されます。定期的に外部へ退避してください。
