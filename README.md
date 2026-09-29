# マクドナルド Discord 注文BOT

利用者は Kyash でチャージし、Discord のパネルから注文します。
管理者が設定した**負担率**の分だけ支払いが割り引かれ、注文後は注文番号を
差し替えたレシート画像が DM に届きます。

---

## できること

| | 内容 |
|---|---|
| 💴 チャージ | 利用者が Kyash の送金リンクを貼るだけで残高に反映 |
| 🍔 注文 | 注文コード(HEX)を貼る／Discord のメニューから組み立てる の2通り |
| 📊 負担率 | 全体・ロール別・個人別に設定。月間上限も指定可能 |
| 🧾 レシート | 注文番号を実際の値に差し替えた画像を DM へ送信 |
| 🏪 実績 | 指定チャンネルへ匿名で投稿（表示項目は選択可能） |
| 🔑 複数アカウント | マクドナルド・Kyash とも複数登録。自動で切り替え |
| 🔄 自動更新 | メニューを定期取得。新商品・値上げを管理者へ通知 |

**利用者はパネルのボタンだけで操作します。**（スラッシュコマンドは使いません）

---

## 1. 動かすまで

### 1-1. 必要なもの

- Python 3.11 以上
- Discord BOT のトークン
- マクドナルドのアカウント（決済カード登録済み）
- Kyash のアカウント

### 1-2. インストール

```bash
pip install -r requirements.txt
```

### 1-3. `main.py` の設定

ファイル冒頭の「設定ブロック」を書き換えます。

```python
DISCORD_TOKEN = ""                      # BOTトークン
OWNER_IDS = [あなたのDiscordユーザーID]   # 全権限を持つ人
ADMIN_ROLE_IDS = []                     # 管理者ロール（任意）
GUILD_ID = None                         # サーバーIDを入れると即時反映
ENCRYPTION_KEY = ""                     # 下のコマンドで生成
DATABASE_URL = "sqlite+aiosqlite:///./data/bot.db"
```

暗号化キーの生成:

```bash
python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

> ### ⚠️ このファイルを公開リポジトリへ push しないでください
> トークンと暗号化キーが平文で入っています。GitHub に上げる場合は
> `.gitignore` の `# main.py` のコメントを外してください。
>
> `ENCRYPTION_KEY` は**一度決めたら変更しないでください。**
> 変更すると保存済みのアカウント情報が読めなくなります。

### 1-4. Discord 側の設定

Developer Portal → あなたのアプリで:

1. **Bot** タブ → トークンを取得
2. **OAuth2 → URL Generator** → スコープ `bot` と `applications.commands`
3. 権限: メッセージを送信 / 埋め込みリンク / ファイルを添付 / メッセージ履歴を読む
4. 生成された URL でサーバーへ招待

> 特権インテントの申請は**不要**です（感想ゲート機能を ON にする場合のみ必要）。

### 1-5. 起動

```bash
python main.py
```

`ログインしました: ...` と表示されれば成功です。

---

## 2. 初回セットアップ（この順番で）

BOT を招待したら、管理者が次の順に実行します。

```
1.  /config subsidy global 40          負担率を決める（40なら利用者は60%払う）
2.  /config channel admin   #bot-管理   管理者への通知先
3.  /config channel achievement #実績   実績の投稿先
4.  /mcd add                           マクドナルドアカウントを登録
5.  /kyash add                         Kyashアカウントを登録
6.  /panel charge #チャージ             チャージパネルを設置
7.  /panel order  #注文                注文パネルを設置
8.  /panel admin  #bot-管理             管理パネルを設置
```

### アカウント登録の流れ

`/mcd add` と `/kyash add` は、メールとパスワードを入力したあと
**SMS やメールで届く6桁の認証コード**が必要です。手元に端末を用意してください。

- Kyash は一度登録すれば、次回から認証コードなしで再ログインできます
- マクドナルドは登録後に**決済カードを選ぶ画面**が出ます

---

## 3. 利用者の使い方

すべてパネルのボタンから操作します。

**チャージ** → Kyash アプリで送金リンクを作る → パネルの `🔗 チャージする` に貼る

**注文** → パネルの `🍔 注文する` →
- `📋 注文コード(HEX)を貼る` … 他で作ったコードで注文
- `🍔 メニューから選ぶ` … 店舗を選んで商品を組み立てる

**注文コードだけ作る** → `🧾 注文コードを作る`（決済はしません）

---

## 4. 管理者コマンド

<details>
<summary>一覧（全46コマンド）</summary>

### パネル
| コマンド | 説明 |
|---|---|
| `/panel order <ch>` | 注文パネルを設置 |
| `/panel charge <ch>` | チャージパネルを設置 |
| `/panel admin <ch>` | 管理パネルを設置 |
| `/panel refresh` | 設置済みパネルを最新にする |

### 設定
| コマンド | 説明 |
|---|---|
| `/config show` | 現在の設定を一覧 |
| `/config subsidy global <率>` | 全体の負担率 |
| `/config subsidy role <ロール> <率>` | ロール別の負担率 |
| `/config subsidy user <ユーザー> <率>` | 個人別の負担率 |
| `/config subsidy list` / `remove <ID>` | 一覧・削除 |
| `/config channel achievement <ch>` | 実績チャンネル |
| `/config channel admin <ch>` | 管理者通知チャンネル |
| `/config charge_limit <下限> <上限>` | チャージ額の範囲 |
| `/config order_limit <上限>` | 1注文の上限 |
| `/config maintenance <on/off>` | 注文の一時停止 |
| `/config feedback <on/off>` | 感想ゲート |
| `/config menu interval <時間>` | メニュー同期の間隔 |
| `/config menu notify <on/off>` | 差分通知 |
| `/config achievement_fields ...` | 実績の表示項目 |

### アカウント
| コマンド | 説明 |
|---|---|
| `/mcd add` / `list` / `remove` | マクドナルドアカウント |
| `/mcd card <ID>` | 決済カードを選び直す |
| `/mcd enable` / `disable` / `health` | 有効化・無効化・状態確認 |
| `/kyash add` / `list` / `remove` | Kyashアカウント |
| `/kyash relogin <ID>` | 再ログイン |

### 運用
| コマンド | 説明 |
|---|---|
| `/stats show` / `ledger` / `account` | 統計・元帳・アカウント使用状況 |
| `/admin grant <user> <額> <理由>` | 残高の手動増減 |
| `/admin ban` / `unban` | 利用停止・解除 |
| `/admin review` | 要確認の注文を処理 |
| `/admin achievement` | 実績を代理送信 |
| `/admin backup` | DBバックアップを取り出す |
| `/menu sync [店舗ID]` | メニューを手動同期 |
| `/debug hex <コード>` | 注文コードの中身を表示 |
| `/sync` / `/restart` | コマンド同期・再起動 |

</details>

---

## 5. 困ったときは

### コマンドが2つずつ表示される

同じコマンドがグローバルとサーバーの両方に登録されている状態です。
本BOTは起動時に自動で片方を消しますが、直らない場合は:

1. `main.py` の `GUILD_ID` を**どちらかに決める**（サーバーIDを入れる or `None`）
2. `/sync` を実行
3. Discord アプリを再起動

### コマンドが表示されない

- `GUILD_ID = None`（グローバル）の場合、反映に**最大1時間**かかります
- すぐ反映させたいときは `GUILD_ID` にサーバーIDを入れてください

### 「要確認」の注文が出た

課金が成立したかどうか分からない状態です。**BOTは勝手に返金しません。**

1. マクドナルドのアプリや店舗で、実際に注文が通っているか確認
2. 管理パネルの `⚠️ 要確認` から「成立」か「返金」を選ぶ

### 残高がおかしい

`/stats ledger` で元帳の整合性を確認できます。
「不整合あり」と出た場合は、その内容を添えて開発者に連絡してください。

### アカウントが `🔴` になった

連続して失敗したため自動で隔離されています。

```
/mcd health          状態を確認
/mcd enable <ID>     復帰させる
```

Kyash は**アクセストークンが1ヶ月で失効**します。
期限が近づくと管理者チャンネルへ通知が届くので `/kyash relogin <ID>` を実行してください。

---

## 6. バックアップ

SQLite を使っている場合、**毎日1回データベースを管理者チャンネルへ自動送信**します
（既定は午前4時）。`/admin backup` でいつでも手動取得もできます。

無料ホスティングでデータが消えても、このファイルがあれば残高を復元できます。
**管理者チャンネルは必ず設定してください。**

---

## 7. 開発者向け

### テスト

```bash
python tests/run_all.py
```

### 構成

```
main.py              起動・設定・コマンド同期
config.py / emoji.py 既定値・絵文字定数
db/                  モデル・接続
core/                元帳・排他制御・暗号化・負担率・注文Saga・設定
services/mcd/        protobuf・APIクライアント・メニュー・アカウント・店舗
services/kyash/      Kyash API・口座管理・チャージ
services/receipt.py  レシート画像生成
ui/                  パネル・埋め込み・操作フロー
cogs/                スラッシュコマンド・定期タスク
docs/                設計書（01〜09）
```

設計の詳細は `docs/README.md` を参照してください。
特に注文の状態遷移は `docs/04` §4、絶対に守るルールは `docs/06` §0 にあります。

### データベースを PostgreSQL に移す

`main.py` の `DATABASE_URL` を書き換えるだけです。

```python
DATABASE_URL = "postgresql+asyncpg://user:pass@host/dbname"
```

`pip install asyncpg` も必要です。

---

## 注意

- 本BOTはマクドナルド・Kyash の**非公式API**を利用しています。
  仕様変更やアカウント停止が起こりうる前提で運用してください
- 利用者から預かった残高の**払い戻しはできない**設計です
- 未使用残高の合計が大きくなる場合、資金決済法上の届出が必要になることがあります
  （`/stats ledger` で総額を確認できます）
