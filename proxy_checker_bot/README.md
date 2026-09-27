# 🛰️ 簡易プロキシチェッカー Discord BOT

HTTP / HTTPS プロキシの **生存 ・ 応答速度 ・ 出口IP ・ 国名** を Discord 上でまとめて判定する BOT です。
プロキシ用のライブラリ (requests / aiohttp / PySocks など) は**一切使わず**、
`asyncio` の生 TCP ストリームへ HTTP を直接書き込んで判定しています。

> このリポジトリにある既存の BOT とは独立した、**単体で動く別の BOT** です。

---

## セットアップ

必要なもの: **Python 3.11 以上** (3.9 / 3.10 でも動きますが、後述の CONNECT フォールバックが無効になります)

```bash
cd proxy_checker_bot
pip install -r requirements.txt
```

### 1. トークンを書く

`main.py` の先頭にある `TOKEN` に、BOT のトークンを貼り付けます (**設定のうちトークンだけがファイル直書き**です)。

```python
# ===========================================================================
#  ここにトークンを貼り付ける (前後のダブルクォートは消さないでください)
# ===========================================================================
TOKEN = "ここにBOTのトークンを貼り付けてください"
```

トークンは [Discord Developer Portal](https://discord.com/developers/applications) →
対象アプリ → **Bot** → *Reset Token* で取得できます。
特権インテント (Message Content など) は**不要**です。

### 2. BOT をサーバーに招待する

Developer Portal → **OAuth2 → URL Generator** で

- SCOPES: `bot` と `applications.commands`
- BOT PERMISSIONS: `Send Messages` / `Embed Links` / `Attach Files`

を選び、生成された URL から招待します。

### 3. 起動

```bash
python main.py
```

起動時にスラッシュコマンドを同期します。グローバル同期は反映に数分かかることがありますが、
**招待済みのサーバーに BOT が新しく参加したときは即時同期**されます。

---

## コマンド

| コマンド | 説明 |
| --- | --- |
| `/check proxies:<一覧>` | プロキシをまとめて判定する |
| `/check` (引数なし) | **複数行そのまま貼れる入力ウィンドウ**が開く |
| `/config view` | 現在の設定を表示 (★ が変更済みの項目) |
| `/config set ...` | 設定を変更 (サーバー管理権限が必要) |
| `/config reset` | 設定を既定値に戻す |
| `/help` | 使い方を表示 |

`/check` にはその場限りの上書き引数もあります: `timeout` / `concurrency` / `https_check` / `private`。

### 対応している書き方

```
123.45.67.89:8080
http://123.45.67.89:8080
user:pass@123.45.67.89:8080
123.45.67.89:8080:user:pass
```

区切りは **改行・カンマ・空白・セミコロン・パイプ** のいずれでも可。重複は自動で除去します。

### 結果の見かた

- 応答速度バッジ … 🟢 0.5秒未満 ・ 🟡 1.5秒未満 ・ 🟠 3秒未満 ・ 🔴 それ以上
- `🔒 HTTPS可` … CONNECT で HTTPS 中継もできたプロキシ
- `🚇 CONNECT専用` … 平文 HTTP の中継は拒否するが、HTTPS なら使えるプロキシ
- 生存プロキシは**応答が速い順**に並びます
- セレクトメニューで **生存のみ / 全件 / 失敗のみ** を切り替え、◀ ▶ でページ送り
- 📄 ボタンで `alive_proxies.txt` (貼り付け用) と `proxy_report.txt` (全件詳細) を取得
- 🔄 ボタンで**失敗した分だけ再チェック**

---

## 設定 (トークン以外はすべてコマンドで設定)

`/config set` で変更します。設定は**サーバーごと**(DM では実行者ごと)に `settings.json` へ保存されます。

| キー | 既定値 | 説明 |
| --- | --- | --- |
| `timeout` | `8.0` 秒 | プロキシ1台に待つ最大時間 (1〜30) |
| `concurrency` | `50` 台 | 同時に検査する台数 (1〜200) |
| `retries` | `0` 回 | 失敗時の再試行回数 (0〜3) |
| `https_check` | オン | CONNECT で HTTPS 中継対応も調べる |
| `max_proxies` | `200` 件 | 1回で受け付ける最大件数 (1〜1000) |
| `show_dead` | オン | 失敗したプロキシと理由も一覧に出す |
| `attach_file` | オン | 生存リストの `.txt` を自動添付する |
| `ephemeral` | オフ | 結果を実行者だけに表示する |
| `judge_url` | ip-api (http) | 出口IP・国名の取得先 (`http://` のみ) |
| `https_judge_url` | ipwho.is (https) | CONNECT で判定するときの取得先 (`https://` のみ) |

例:

```
/config set timeout: 5 concurrency: 100 show_dead: false
```

---

## 判定の仕組み

プロキシ1台につき、次の順で試します。

1. **HTTP中継 (絶対URI)** — 普通の HTTP プロキシの作法で、判定先を丸ごと URI に入れて送る

   ```
   GET http://ip-api.com/json/?fields=... HTTP/1.1
   Host: ip-api.com
   ```

   200 が返れば生存。本文の JSON から出口IPと国名を取り出し、接続からここまでを応答速度とします。

2. **HTTPSトンネル (CONNECT)** — 上が `405` / `403` / `501` / `400` で拒否された場合のみ

   ```
   CONNECT ipwho.is:443 HTTP/1.1
   ```

   トンネルが張れたら標準ライブラリの `ssl` で TLS を張り、その中で普通の GET を送って読みます。
   「平文 HTTP の中継は断るが HTTPS なら通す」プロキシもこれで正しく生存判定できます
   (`🚇 CONNECT専用` と表示)。

チャンク転送・`Content-Length`・接続クローズ終端のいずれの応答形式にも対応し、
`407` (認証要求) やタイムアウト、接続拒否などは理由付きで失敗として表示します。

---

## ファイル構成

```
proxy_checker_bot/
├── main.py           # 起動用。★トークンはここに直接記入★
├── checker.py        # 生ソケットによる判定ロジック (プロキシライブラリ不使用)
├── settings.py       # /config で変更する設定の保存と検証
├── formatting.py     # 表示の整形 (国旗・バッジ・進捗バー・レポート)
├── cogs/
│   └── proxy.py      # スラッシュコマンドと結果表示UI
├── requirements.txt
└── settings.json     # 自動生成 (git 管理外)
```

---

## 注意

- **自分が利用を許可されたプロキシに対してのみ**使用してください。
- 判定には外部サービス (既定では ip-api.com / ipwho.is) へのアクセスが発生します。
  無料枠にはレート制限があるため、大量に検査する場合は `judge_url` の変更や
  `concurrency` を下げる運用を検討してください。
