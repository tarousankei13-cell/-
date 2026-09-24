# COSMIC RNG — 導入手順

ZIPを展開して `python main.py` を実行するだけで起動します。
データベースサーバもNode.jsも不要です（フロントエンドはビルド済みで同梱）。

---

## 1. いちばん短い手順

```
1. ZIP をアップロードして展開する
2. main.py を実行する（パネルによっては「起動」ボタン）
3. ブラウザでサイトを開く
```

初回起動時に自動で次を行います。

| やること | 内容 |
|---|---|
| `.env` 生成 | セッション署名鍵（SECRET_KEY）をランダム生成 |
| 依存関係 | 不足していれば `requirements.txt` から自動インストール |
| DB作成 | `data/cosmic.db`（SQLite・53テーブル） |
| 初期データ投入 | アイテム142・Biome17・装備21・実績92・クエスト33・ショップ30・Admin Artifact 32 ほか |

起動すると次のように表示されます。

```
[cosmic] データベーススキーマを確認しています
[cosmic] 初期コンテンツを投入しています（アイテム・Biome・装備・実績ほか）
[cosmic] データベース: SQLite (./data/cosmic.db)
[cosmic] 待ち受け: http://0.0.0.0:8000/
```

---

## 2. 必要な環境

| 項目 | 必要なもの |
|---|---|
| Python | 3.11 以上 |
| プロセス | **常駐**できること（リクエスト毎に起動するCGI型では動きません） |
| WebSocket | 使えること（無くても遊べますが、リアルタイム通知が届きません） |
| 書き込み可能ディスク | `data/`（DB）、`backups/`、`logs/` を作ります |
| 外部サービス | **不要**（PostgreSQL も Redis も要りません） |

> **注意** — 画像のパネルが「静的サイト」モードのままだと、HTML/CSS/JS を配るだけで
> Python は実行されません。このゲームは常駐サーバが必須なので、**Python アプリを
> 実行するモードに切り替えてから**アップロードしてください。切り替えられない場合は、
> 同梱の `deploy/` を使って普通の VPS に置く方法（後述）をご利用ください。

---

## 3. 最初にやるべき設定（重要）

初回起動時の `.env` は **動作確認モード** です。この状態では
**Discord認証なしで誰でも任意のIDとしてログインできます。**
起動時にもその旨の警告が表示されます。

一般公開する前に `.env` を次のように変更してください。

```ini
ENVIRONMENT=production
DEV_LOGIN_ENABLED=false

# 実際にブラウザからアクセスするURL（ここが違うとログインが 403 になります）
PUBLIC_BASE_URL=https://あなたのドメイン
COOKIE_SECURE=true

# Discord Developer Portal で取得
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
DISCORD_REDIRECT_URI=https://あなたのドメイン/api/auth/callback

# 管理者にしたい Discord ユーザーID（カンマ区切り）
ADMIN_DISCORD_IDS=123456789012345678
```

`ENVIRONMENT=production` にすると、開発用ログインのURLは**そもそも存在しなくなります**
（ルート登録自体をしないので、正しいリクエストでも壊れたリクエストでも 404 を返します）。

### Discord アプリの作り方

1. <https://discord.com/developers/applications> で「New Application」
2. OAuth2 → Redirects に `PUBLIC_BASE_URL` + `/api/auth/callback` を登録
3. Client ID と Client Secret を `.env` へ
4. 自分の Discord ユーザーID（開発者モードで右クリック→「IDをコピー」）を
   `ADMIN_DISCORD_IDS` に入れる
5. 再起動し、Discordでログインすると右上に **Admin** が出ます

DM通知を使う場合のみ、Bot を作って `DISCORD_BOT_TOKEN` も設定してください。

---

## 4. サブパスで配信する場合

`https://example.com/s/kazino/` のように、ドメイン直下ではない場所で配信するときは
`.env` に次を設定してください。これだけで、アセット・API・WebSocket・画面遷移の
すべてがそのパス配下に揃います。

```ini
BASE_PATH=/s/kazino
PUBLIC_BASE_URL=https://example.com/s/kazino
```

ドメイン直下（`https://example.com/`）で配信するなら `BASE_PATH` は空のままです。

---

## 5. よく使う設定

`.env` の主な項目です。ゲームバランス（確率・報酬・クールダウンなど）は
`.env` ではなく**管理パネルから変更**します。コード変更も再起動も不要です。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `PORT` | `8000` | 待ち受けポート。パネルが `PORT` を渡す場合はそちらが優先 |
| `HOST` | `0.0.0.0` | 待ち受けアドレス |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/cosmic.db` | PostgreSQL にする場合は後述 |
| `BASE_PATH` | 空 | サブパス配信のとき |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | **実際のアクセスURL**。不一致だとログインが 403 |
| `COOKIE_SECURE` | `false` | HTTPS 公開時は `true` |
| `ADMIN_DISCORD_IDS` | 空 | 管理者のDiscord ID（カンマ区切り） |
| `LOG_LEVEL` | `INFO` | `DEBUG` で詳細ログ |
| `WORKERS` | `1` | PostgreSQL 利用時のみ 2 以上にできます |

---

## 6. 管理コマンド

`backend/` ディレクトリで実行します（`PYTHONPATH` は main.py が面倒を見るので、
手動実行時のみ下記のように指定してください）。

```bash
cd backend
PYTHONPATH=. python -m app.cli check            # 設定と接続の自己診断
PYTHONPATH=. python -m app.cli migrate          # スキーマ作成・更新
PYTHONPATH=. python -m app.cli seed             # 不足コンテンツの投入
PYTHONPATH=. python -m app.cli seed --force     # 初期コンテンツを既定値に戻す
PYTHONPATH=. python -m app.cli set-role <DiscordID> admin   # 管理者にする
PYTHONPATH=. python -m app.cli backup --kind manual         # バックアップ
PYTHONPATH=. python -m app.cli restore <ファイル名> --yes    # 復元（破壊的）
```

バックアップは管理パネルからも作成・ダウンロード・復元できます（復元は文字入力による
強い確認が必要）。SQLite では SQLite のオンラインバックアップAPIを使うため、
**サーバを止めずに**整合性のあるコピーが取れます。

---

## 7. PostgreSQL に切り替える（任意・本格運用向け）

同時接続が多い場合や、複数ワーカーで動かしたい場合は PostgreSQL を推奨します。
`.env` の1行を書き換えるだけで、コードの変更は不要です。

```ini
DATABASE_URL=postgresql+asyncpg://cosmic:パスワード@127.0.0.1:5432/cosmic_rng
WORKERS=4
```

| | SQLite（既定） | PostgreSQL |
|---|---|---|
| 準備 | 不要 | DBサーバの用意が必要 |
| ワーカー数 | 1（書き込みが単一のため） | 複数可 |
| ワーカー間通知 | プロセス内 | LISTEN/NOTIFY |
| バックアップ | ファイル（オンラインAPI） | `pg_dump` / `pg_restore` |
| 向いている規模 | 個人〜中規模 | 大規模・常時高負荷 |

切り替え時は、切り替え先が空のDBになる点にご注意ください（データは引き継がれません）。

---

## 8. 通常のVPSに置く場合

`deploy/` にひと通り揃っています。

```bash
sudo deploy/scripts/setup.sh      # PostgreSQL・venv・ビルド・systemd 登録
sudo certbot --nginx -d ドメイン   # HTTPS
sudo deploy/scripts/deploy.sh     # 更新デプロイ（migrate → build → restart → ヘルスチェック）
```

* `deploy/systemd/cosmic-rng.service` — 4ワーカー・自動再起動・権限制限つき
* `deploy/systemd/cosmic-backup.timer` — 毎日 04:30 に自動バックアップ
* `deploy/nginx/cosmic-rng.conf` — HTTPS・WebSocket・レート制限・SPAフォールバック

詳細な運用手順は `README.md` にあります。

---

## 9. 困ったとき

| 症状 | 原因と対処 |
|---|---|
| ログインが 403 になる | `PUBLIC_BASE_URL` が実際のアクセスURLと違います。ポート番号も含めて一致させてください |
| ページは出るが真っ白 | `frontend/dist/` が欠けています。ZIPを展開し直してください |
| `ModuleNotFoundError` | `python -m pip install -r requirements.txt` を手動実行してください |
| `address already in use` | 前のプロセスが残っています。停止してから起動してください |
| 管理パネルが出ない | `ADMIN_DISCORD_IDS` に自分のIDを入れて再起動し、ログインし直してください |
| サブパスでアセットが404 | `BASE_PATH` を設定してください（例 `/s/kazino`） |
| 動作が重い | 管理パネルの設定でエフェクト量を下げるか、PostgreSQL に切り替えてください |

ログは標準出力に出ます。`.env` に `LOG_FILE=logs/cosmic.log` を足すとファイルにも残ります。

---

## 10. 同梱物

```
main.py             起動スクリプト（これを実行）
requirements.txt    Python依存関係
.env.example        設定のひな形（初回起動時は .env が自動生成されます）
SETUP.md            このファイル
README.md           VPS向けの詳しい運用マニュアル
backend/            FastAPI アプリ本体（app/ 63ファイル・migrations/・tests/）
frontend/dist/      ビルド済みSPA（そのまま配信されます）
frontend/src/       フロントエンドのソース（改造する場合のみ必要）
deploy/             systemd / Nginx / 運用スクリプト
```
