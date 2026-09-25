# COSMIC RNG — 導入手順

ZIPを展開して `python main.py` を実行するだけで起動します。
設定は `main.py` の先頭にある `CONFIG` だけ。`.env` ファイルもデータベースサーバも
Node.js も不要です（フロントエンドはビルド済みで同梱）。

---

## 1. いちばん短い手順

```
1. ZIP をアップロードして展開する
2. main.py を実行する（パネルによっては「起動」ボタン）
3. ブラウザでサイトを開き、下の管理者アカウントでログインする
```

### 管理者アカウント（初回起動時に自動作成）

| 項目 | 値 |
|---|---|
| ユーザー名 | `admin` |
| メールアドレス | `admin@cosmic-rng.local` |
| パスワード | `Runakunn0513` |

ログインはユーザー名・メールアドレスのどちらでも通ります。

> **このZIPにはパスワードがそのまま書かれています。** 配布・共有しないでください。
> 公開サーバーで運用するなら、ログイン後に **設定 → アカウント → パスワード変更**
> から変えてください（`main.py` 側は変更不要です）。

初回起動時に自動で次を行います。

| やること | 内容 |
|---|---|
| 署名鍵の生成 | `data/secret.key`（削除すると全員ログアウトになります） |
| 依存関係 | 不足していれば `requirements.txt` から自動インストール |
| DB作成 | `data/cosmic.db`（SQLite・53テーブル） |
| 初期データ投入 | アイテム142・Biome17・装備21・実績92・クエスト33・ショップ30・Admin Artifact 32 ほか |
| 管理者作成 | 上のアカウント |

---

## 2. 必要な環境

| 項目 | 必要なもの |
|---|---|
| Python | 3.11 以上 |
| プロセス | **常駐**できること（リクエスト毎に起動するCGI型では動きません） |
| WebSocket | 使えること（無くても遊べますが、リアルタイム通知が届きません） |
| 書き込み可能ディスク | `data/`（DBと署名鍵）、`backups/` を作ります |
| 外部サービス | **不要**（PostgreSQL も Redis も Discord も要りません） |

> **注意** — ホスティングパネルが「静的サイト」モードだと、HTML/CSS/JS を配るだけで
> Python は実行されません。このゲームは常駐サーバが必須なので、**Python アプリを
> 実行するモードに切り替えてから**アップロードしてください。切り替えられない場合は、
> 同梱の `deploy/` を使って通常の VPS に置いてください（後述）。

---

## 3. 設定 — `main.py` の CONFIG

`main.py` を開くと先頭に `CONFIG = { ... }` があります。ここだけ書き換えます。

```python
CONFIG = {
    # --- 管理者アカウント ---
    "ADMIN_USERNAME": "admin",
    "ADMIN_EMAIL":    "admin@cosmic-rng.local",
    "ADMIN_PASSWORD": "Runakunn0513",
    "ADMIN_RESET_PASSWORD": False,

    # --- 公開設定 ---
    "PUBLIC_BASE_URL": "",     # ブラウザで開くURL。空ならlocalhost
    "BASE_PATH": "",           # サブパス配信のときだけ（例 "/s/kazino"）
    "PORT": None,              # None ならホストのPORT、無ければ8000
    "HOST": "0.0.0.0",

    # --- データベース ---
    "DATABASE_URL": "sqlite+aiosqlite:///./data/cosmic.db",
    "WORKERS": 1,
    ...
}
```

### 公開するときに必ず設定する項目

```python
"PUBLIC_BASE_URL": "https://あなたのドメイン",
```

**ブラウザに入力するURLと完全に一致**させてください（ポート番号まで）。
ここが違うと、画面は出るのに**ログインだけが失敗**します。起動時にヒントを出します。

`https://` にすると Cookie の Secure 属性が自動で有効になります。

### 管理者パスワードを忘れたとき

`"ADMIN_RESET_PASSWORD": True` にして再起動すると、`ADMIN_PASSWORD` の値に
戻ります（既存のログインはすべて無効化されます）。戻したら `False` に戻してください。

### 新規登録を止めたいとき

管理パネルの「設定」から `features.registration_open` を切り替えます。
オフにすると、ログイン画面の「新規登録」タブが無効になります。

---

## 4. アカウントの仕組み

* 登録に必要なのは **メールアドレス・ユーザー名・パスワード** の3つだけです。
* **確認メールは送信されません**（メール送信サーバを必要としない設計です）。
  メールアドレスはログインIDとして使います。
* パスワードは `scrypt`（メモリハード関数）でハッシュ化して保存します。
  平文はどこにも保存されず、APIからも返りません。
* ログイン失敗が8回続くとそのアカウントは15分間ロックされます。
* パスワードを変更すると、**変更した端末以外のログインはすべて無効**になります。
* 「メールアドレスかパスワードが違います」というメッセージは、存在しない
  アカウントでも同じ文面を返します（どのアドレスが登録済みか分からないように）。

### 管理者権限について

* `CONFIG` の `ADMIN_EMAIL` と一致するアカウントが**スーパー管理者**です。
  バックアップの復元や他ユーザーの権限変更など、最も危険な操作ができます。
* 管理パネルから他のユーザーを管理者に昇格させることもできますが、その場合は
  **通常の管理者止まり**で、スーパー管理者にはなりません。権限の最上位は
  設定ファイル側にしか存在しない、という形にしてあります。
* 管理APIは、管理者以外には **404** を返します（URLを知っていても存在が分かりません）。
* 管理操作を行うには、パネル内で **ADMIN MODE をオンにする**必要があります。
  すべての操作は監査ログに記録されます。

### Discord ログインも使いたい場合（任意）

`CONFIG` の `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` / `DISCORD_REDIRECT_URI`
を設定すると、ログイン画面に Discord ボタンが追加されます。設定しなければ
メール認証のみで動作します。

---

## 5. サブパスで配信する場合

`https://example.com/s/kazino/` のように、ドメイン直下ではない場所で配信するときは
`CONFIG` に次を設定してください。アセット・API・WebSocket・画面遷移のすべてが
そのパス配下に揃います。

```python
"BASE_PATH": "/s/kazino",
"PUBLIC_BASE_URL": "https://example.com/s/kazino",
```

---

## 6. 管理コマンド

`backend/` ディレクトリで実行します。

```bash
cd backend
PYTHONPATH=. python -m app.cli check            # 設定と接続の自己診断
PYTHONPATH=. python -m app.cli migrate          # スキーマ作成・更新
PYTHONPATH=. python -m app.cli seed             # 不足コンテンツの投入
PYTHONPATH=. python -m app.cli seed --force     # 初期コンテンツを既定値に戻す
PYTHONPATH=. python -m app.cli backup --kind manual         # バックアップ
PYTHONPATH=. python -m app.cli restore <ファイル名> --yes    # 復元（破壊的）
```

バックアップは管理パネルからも作成・ダウンロード・復元できます（復元は文字入力による
強い確認が必要）。SQLite では SQLite のオンラインバックアップAPIを使うため、
**サーバを止めずに**整合性のあるコピーが取れます。

---

## 7. PostgreSQL に切り替える（任意・本格運用向け）

同時接続が多い場合や、複数ワーカーで動かしたい場合に推奨します。
`CONFIG` の2行を書き換えるだけで、コードの変更は不要です。

```python
"DATABASE_URL": "postgresql+asyncpg://cosmic:パスワード@127.0.0.1:5432/cosmic_rng",
"WORKERS": 4,
```

| | SQLite（既定） | PostgreSQL |
|---|---|---|
| 準備 | 不要 | DBサーバの用意が必要 |
| ワーカー数 | 1（書き込みが単一のため） | 複数可 |
| ワーカー間通知 | プロセス内 | LISTEN/NOTIFY |
| バックアップ | ファイル（オンラインAPI） | `pg_dump` / `pg_restore` |
| 向いている規模 | 個人〜中規模 | 大規模・常時高負荷 |

切り替え先が空のDBなら、初期コンテンツと管理者が自動で作り直されます
（データは引き継がれません）。

---

## 8. 通常のVPSに置く場合

`deploy/` にひと通り揃っています。VPS運用では `main.py` の代わりに systemd と
`.env`（`.env.example` を参照）を使う構成も選べます。

```bash
sudo deploy/scripts/setup.sh      # PostgreSQL・venv・ビルド・systemd 登録
sudo certbot --nginx -d ドメイン   # HTTPS
sudo deploy/scripts/deploy.sh     # 更新デプロイ
```

詳細な運用手順は `README.md` にあります。

---

## 9. 困ったとき

| 症状 | 原因と対処 |
|---|---|
| ログインが 403 になる | `PUBLIC_BASE_URL` が実際のアクセスURLと違います。ポート番号も含めて一致させてください |
| 管理者でログインできない | `ADMIN_RESET_PASSWORD` を `True` にして再起動 → 戻す |
| ログインが「多すぎます」で弾かれる | 失敗8回でアカウントが15分ロックされます。待つか、上の方法でリセットしてください |
| 他の人のログインまで弾かれる | リバースプロキシのアドレスを `TRUSTED_PROXIES` に追加してください。未設定だと全員が同じ接続元と見なされ、回数制限を共有します |
| ページは出るが真っ白 | `frontend/dist/` が欠けています。ZIPを展開し直してください |
| ログイン画面が出ない／星空だけ | ブラウザに古いページが残っています。**スーパーリロード**（Windows: `Ctrl+F5` / Mac: `Cmd+Shift+R`）してください。v1.5より前の版には、ログイン画面が背景の下に描画されて見えなくなる不具合がありました |
| 「サーバーに接続できません」と出る | サーバーの起動待ちか、応答が遅すぎます。自動で再接続を試みるので数十秒待つか「今すぐ再試行」を押してください。続く場合はサーバーのログを確認してください |
| `ModuleNotFoundError` | `python -m pip install -r requirements.txt` を手動実行してください |
| `address already in use` | 前のプロセスが残っています。停止してから起動してください |
| 再起動したら全員ログアウトした | `data/secret.key` が消えています（セッション署名鍵） |
| サブパスでアセットが404 | `BASE_PATH` を設定してください（例 `/s/kazino`） |
| 動作が重い | 管理パネルでエフェクト量を下げるか、PostgreSQL に切り替えてください |

ログは標準出力に出ます。`CONFIG` の `LOG_FILE` にパスを書くとファイルにも残ります。

---

## 10. 同梱物

```
main.py             起動スクリプト兼設定ファイル（これを実行）
requirements.txt    Python依存関係
SETUP.md            このファイル
README.md           VPS向けの詳しい運用マニュアル
.env.example        VPS運用で .env を使う場合のひな形
backend/            FastAPI アプリ本体（app/・migrations/・tests/）
frontend/dist/      ビルド済みSPA（そのまま配信されます）
frontend/src/       フロントエンドのソース（改造する場合のみ必要）
deploy/             systemd / Nginx / 運用スクリプト
```
