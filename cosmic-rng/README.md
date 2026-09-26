# COSMIC RNG

宇宙を舞台にしたオンラインRNGゲーム。メールアドレスとパスワードでアカウントを作り、
個人に割り当てられたBiomeの中でRollを重ね、世界にまだ存在しないアイテムの
「世界初発見」を目指します。

手早く動かしたいだけなら `SETUP.md` を参照してください（`python main.py` だけで起動します）。

RNGの判定・Luck計算・クールダウン・オフライン進行はすべてサーバー側で行われ、
クライアントからの改ざんは一切受け付けません。

```
Browser (React SPA)
   │  HTTPS / WSS
   ▼
Nginx ──► FastAPI (uvicorn, 複数worker)
             ├─ REST API / WebSocket
             ├─ Auth (email + password / 任意で Discord OAuth2)
             ├─ RNG Engine (versioned)
             ├─ Biome Engine (毎秒抽選をイベント駆動で再現)
             ├─ Inventory / Equipment / Crafting / Shop
             ├─ Market / Trade / Gift
             ├─ Quest / Achievement / Season / Ranking
             ├─ Admin System + Admin Artifact
             └─ Scheduler (leader election)
                       │
                       ▼
                  PostgreSQL
              (LISTEN/NOTIFY でworker間イベント配信)
```

---

## 目次

- [必要環境](#必要環境)
- [クイックスタート（開発）](#クイックスタート開発)
- [本番デプロイ](#本番デプロイ)
- [アカウントと管理者](#アカウントと管理者)
- [Discord OAuth の設定（任意）](#discord-oauth-の設定任意)
- [運用](#運用)
- [ゲーム仕様](#ゲーム仕様)
- [管理者機能](#管理者機能)
- [セキュリティ](#セキュリティ)
- [テスト](#テスト)
- [プロジェクト構成](#プロジェクト構成)

---

## 必要環境

| ソフトウェア | バージョン | 用途 |
|---|---|---|
| Linux (Ubuntu 22.04+ / Debian 12+) | — | VPS |
| Python | 3.11+ | バックエンド |
| PostgreSQL | 14+ | データベース |
| Node.js | 20+ | フロントエンドのビルド |
| Nginx | 1.18+ | リバースプロキシ / 静的配信 |

---

## クイックスタート（開発）

```bash
git clone <this-repo> && cd cosmic-rng

# PostgreSQL にデータベースとロールを用意（初回のみ）
sudo -u postgres psql -c "CREATE ROLE cosmic LOGIN PASSWORD 'cosmic';"
sudo -u postgres createdb -O cosmic cosmic_rng

# API と Vite を同時に起動（.env を自動生成、migration と seed も実行）
bash deploy/scripts/dev.sh
```

- ゲーム: http://localhost:5173
- API ドキュメント（開発時のみ）: http://localhost:8000/api/docs

トップページからメールアドレス・ユーザー名・パスワードでアカウントを作成できます。
`.env` の `ADMIN_EMAIL` と一致するアカウントがスーパー管理者になります
（起動時に `ADMIN_USERNAME` / `ADMIN_EMAIL` / `ADMIN_PASSWORD` で自動作成されます）。

---

## 本番デプロイ

### 1. サーバー初期設定

```bash
sudo bash deploy/scripts/setup.sh
```

このスクリプトは以下を行います。

- Python / PostgreSQL / Nginx / Node.js / certbot のインストール
- `cosmic` システムユーザーの作成
- データベースとロールの作成（パスワードは自動生成）
- `/opt/cosmic-rng/.env` の生成（`SECRET_KEY` と `DATABASE_URL` を自動設定）
- systemd ユニットの配置とバックアップタイマーの有効化

### 2. コードの配置と設定

```bash
sudo rsync -a --exclude node_modules --exclude .venv ./ /opt/cosmic-rng/
sudo nano /opt/cosmic-rng/.env    # 下記の Discord 設定と PUBLIC_BASE_URL、ADMIN_DISCORD_IDS
```

### 3. ビルドと起動

```bash
sudo bash deploy/scripts/deploy.sh
```

依存インストール → migration → seed → フロントエンドビルド → サービス再起動 →
ヘルスチェックまで一括で実行します。何度でも安全に実行できます（更新時もこれ）。

### 4. Nginx と HTTPS

```bash
sudo cp /opt/cosmic-rng/deploy/nginx/cosmic-rng.conf /etc/nginx/sites-available/cosmic-rng
sudo sed -i 's/rng.example.com/YOUR.DOMAIN/g' /etc/nginx/sites-available/cosmic-rng
sudo ln -sf /etc/nginx/sites-available/cosmic-rng /etc/nginx/sites-enabled/
sudo certbot --nginx -d YOUR.DOMAIN      # 証明書の取得と設定を自動で行う
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable --now cosmic-rng
```

certbot が証明書の自動更新（`certbot.timer`）も設定します。

### 5. 管理者の追加

`ADMIN_DISCORD_IDS` に書いた ID は**スーパー管理者**で、パネルから降格できません。
それ以外の管理者は一度ログインさせたうえで：

```bash
cd /opt/cosmic-rng/backend
sudo -u cosmic .venv/bin/python -m app.cli set-role <discord_id> admin
```

---

## アカウントと管理者

アカウントはメールアドレス・ユーザー名・パスワードで作成します。確認メールは
送信しません（メール送信サーバを必要としない設計です）。パスワードは `scrypt`
でハッシュ化して保存され、平文は保存もAPI応答もされません。

```ini
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=十分に長いパスワード
```

この3つを設定すると、初回起動時に管理者アカウントが作成されます。既存アカウントの
パスワードが勝手に上書きされることはありません（復旧が必要なときだけ
`ADMIN_RESET_PASSWORD=true` にして再起動し、終わったら戻します）。

**スーパー管理者**は `ADMIN_EMAIL`（または `ADMIN_DISCORD_IDS`）と一致する
アカウントだけです。バックアップ復元や権限変更はスーパー管理者に限られ、
管理パネルから昇格させた管理者は通常権限にとどまります。権限の最上位を
データベース側に置かないことで、パネル経由での自己昇格を防いでいます。

セキュリティ上の挙動:

- ログイン失敗8回でそのアカウントを15分ロックします。
- 認証失敗のメッセージは、存在しないアカウントでも同一文面です。
- パスワード変更時は、変更した端末以外のセッションをすべて失効させます。
- 管理APIは管理者以外に **404** を返します（存在自体を秘匿）。

---

## Discord OAuth の設定（任意）

メール認証だけで完結するため、Discord の設定は必須ではありません。追加の
ログイン手段として提供したい場合のみ設定してください。

1. https://discord.com/developers/applications で New Application
2. **OAuth2 → Redirects** に `https://YOUR.DOMAIN/api/auth/callback` を**完全一致**で追加
3. **OAuth2 → Client information** の Client ID / Client Secret を `.env` へ
4. DM通知を使う場合のみ **Bot → Reset Token** でトークンを取得し `DISCORD_BOT_TOKEN` へ
   （BotはDM送信先ユーザーと同じサーバーに参加している必要があります）

```ini
DISCORD_CLIENT_ID=1234567890
DISCORD_CLIENT_SECRET=xxxxxxxx
DISCORD_REDIRECT_URI=https://YOUR.DOMAIN/api/auth/callback
PUBLIC_BASE_URL=https://YOUR.DOMAIN
ADMIN_DISCORD_IDS=あなたのDiscordユーザーID
```

DM通知の送信先とレア度しきい値は、コードではなく**管理パネルの Settings**から変更します。

---

## 運用

### 起動・停止・再起動

```bash
sudo systemctl start   cosmic-rng
sudo systemctl stop    cosmic-rng
sudo systemctl restart cosmic-rng
sudo systemctl status  cosmic-rng
```

`Restart=always` なのでクラッシュしても自動復帰します。

### ログ

```bash
sudo journalctl -u cosmic-rng -f              # アプリケーション（追尾）
sudo journalctl -u cosmic-rng --since "1 hour ago"
sudo tail -f /var/log/cosmic-rng/app.log      # LOG_FILE を設定した場合
sudo tail -f /var/log/nginx/cosmic-rng.error.log
```

発生したエラーは DB にも記録され、**管理パネル → Logs → エラー**から
スタックトレース付きで確認できます（プレイヤーには一切出ません）。

### 自動バックアップ

`backup.auto_enabled`（既定オン）のとき、スケジューラのリーダーが**1日1回**バックアップを
取得し、`BACKUP_KEEP` を超えた古い自動バックアップを削除します。同じ日に2度は走りません。

### バックアップ

毎日 4:30 に自動実行（`cosmic-backup.timer`）。保存先は `BACKUP_DIR`、
`BACKUP_KEEP` 世代を超えた自動バックアップは削除されます。

```bash
# 手動バックアップ
cd /opt/cosmic-rng/backend
sudo -u cosmic .venv/bin/python -m app.cli backup --kind manual

# タイマーの状態
systemctl list-timers cosmic-backup.timer
```

管理パネル → Tools → バックアップ からも作成・一覧・削除ができます（スーパー管理者のみ）。

### リストア

```bash
sudo bash deploy/scripts/restore.sh cosmic-rng-20260101-043000-scheduled.dump
```

実行前に現在のDBの安全バックアップを自動で取得し、`RESTORE` の入力を求めます。
管理パネルからも実行できますが、`RESTORE <ファイル名>` の完全一致入力が必要です。

### 設定変更

ゲームバランス（確率・Luck倍率・Biome出現率・価格・しきい値・機能ON/OFF など）は
**すべて管理パネルから変更でき、コードの修正もサーバー再起動も不要**です。
変更は全workerへ即座に反映されます（content version による自動リロード）。

`.env` を変更した場合のみ `sudo systemctl restart cosmic-rng` が必要です。

### スケーリング

`cosmic-rng.service` の `Environment=WORKERS=4` を CPU コア数の 2 倍程度に調整します。
複数workerでは `EVENT_BUS=postgres` が必須です（WebSocketイベントの配信に使用）。
定期ジョブは PostgreSQL のアドバイザリロックで選ばれた 1 worker だけが実行します。

---

## ゲーム仕様

### RNG

アイテムは**レアな順に**判定されます。アイテム *i*（基礎確率 1/N）の判定成立確率は

```
c_i = min(1, L_i / N_i × Biome補正 × その他補正) ^ flatten
L_i = 最終Luck ^ レア度ごとのLuck指数     （既定: ???は0.8、Ultra Secretは0.88 …）
```

この連鎖判定は数学的に等価な**1つの確率分布へコンパイル**され、一様乱数1つで
サンプリングされます。これにより

- UIに表示する「実質確率」が正確に計算できる
- オフラインの数万Rollを高速に処理できる
- 統計検定（カイ二乗）で理論値と実測の一致を検証できる

超低確率のアイテムほど Luck 指数が 1 未満になり、Luck が効きにくくなります。
Luck に上限はありません。乱数は `secrets.SystemRandom`（CSPRNG）です。

RNG Engine は `RNG_VERSION` を持ち、すべての Roll に RNG version と content version が
記録されるため、確率を変更しても過去のログの解釈が崩れません。

**天井（Pity）は導入していません。** ただし RNG Engine は追加可能な構造になっています。

### Luck

```
最終Luck = Base × 装備 × Biome × 一時効果 × Special × イベント × ギルド × Artifact
```

装備は既定で加算合成（`1 + Σbonus`）、管理パネルから乗算合成にも変更できます。

### Special Roll

10回に1回（管理パネルで変更可）、Luck が少しだけ上昇する Special Roll が発生します。
さらに**公開されていない隠しSpecial**も存在します。

### 一時効果（Boost）

複数所持・重複可能。重複時の挙動はアイテムごとに選べます。

| stack_mode | 挙動 |
|---|---|
| `add` | 値を加算（+1000% × 2 → +2000%） |
| `multiply` | 倍率を乗算（×11 × ×11 → ×121） |
| `queue` | 同一Boostは古いものから1つずつ消費（ストック式） |
| `highest` | 同一Boostのうち最も強いものだけ発動・消費 |

Roll画面に有効なBoostと残りRoll数／残り時間が常時表示されます。

### Biome

各プレイヤーに個別のBiomeが割り当てられ、毎秒一定確率で変化します。
実装はイベント駆動（幾何分布による待ち時間サンプリング）なので、
全プレイヤーを毎秒処理することなく、毎秒判定と統計的に完全に同一の結果になります。
オフライン期間も同じ処理で正確に再現されます。

超低確率Biome（Void Rift / Singularity / Genesis）はさらに**特殊状態**を持ち、
Biome限定アイテムの中でもその状態でしか出ないものがあります。

### 自動削除フィルターと Auto Skip

**別機能**です。

- **自動削除**: 指定した確率以下／レア度のアイテムをインベントリに入れず、削除または自動売却。
  Roll数にはカウントされ、Collectionの発見数にはカウントされません。
  ⭐お気に入り登録したアイテムは常に保護。未発見アイテムの保護も設定できます。
- **Auto Skip**: ショップで購入するアイテムで、指定確率未満の**結果演出をスキップ**します。
  アイテムはインベントリに入ります。

### オフライン Roll（自動）

ブラウザを閉じている間のRollは、設定なしで自動的にたまります（Auto Roll の
スイッチは「ページを開いている間、自動でRollし続ける」ためのもので、離席中の
加算とは無関係です）。再ログイン時にサーバー側が

```
Roll数 = 経過秒数 × 効率 ÷ 実際のクールダウン   （上限あり）
```

で計算し、その間のBiome遷移も正確に再現したうえでまとめて処理します。
クライアントの時刻は一切信用しません（サーバー時刻と `offline_processed_until` のみ）。

### 進行と解放

最初からすべての機能は使えません。レベルに応じて段階的に解放されます
（解放レベルは管理パネルで変更可）。ただし**最初の1Rollでも最高レアを引く可能性は残されています**。

### 隠し要素

時間帯限定アイテム、Special Roll限定アイテム、最低Luck条件付きアイテム、
隠しクエスト、秘密のレシピ（Experimental Fusionで発見）、隠しSpecial Roll、
Biome特殊状態限定アイテムなどが用意されています。ヒントは表示されますが、答えは表示されません。

### お試しRoll（登録不要）

未登録の訪問者は、トップページで **10回だけ本物のRoll**が引けます。判定はサーバー側で、
Lv.1・初期Biomeの確率テーブルをそのまま使います。結果はHMAC署名済みCookieに記録され、
クライアントからは改竄できません。そのまま登録すると、引いたアイテムはすべて新しい
アカウントに引き継がれます（図鑑・最高記録にも反映）。

自動生成アイテムの枠を引いた場合は引き直します。ゲストがアイテム行をDBに作れないようにするためです。
管理パネルの `features.guest_rolls` で無効にできます。

### ログインボーナス / 連続ログイン

7日周期。`quests.reset_timezone`（既定 Asia/Tokyo）の日付が変わると次が受け取れます。
前日に受け取っていれば連続日数が伸び、1日空くと1に戻ります。7日目は最大報酬（✦6,000 +
星の欠片40 + Fortune Elixir）です。二重受け取りはサーバー側で 409 になります。

### 週間ランキング / シーズンパス

週間順位は ISO週（月曜0時リセット）で、Roll数と最高レアの2種。シーズンパスは
**無料20段**で、すでに貯まっているシーズンポイントがそのまま進行度になります。
各段は一度だけ受け取れます（PKで保証）。

### 世界目標（Community Goal）

サーバー全体のRoll数を目標にした共同イベント。達成するとオンラインかどうかに関わらず
**全員に一定時間のLuck倍率**が付きます。進捗はスケジューラが毎分確認し、達成は一度きりです。

### ギルド

設立 ✦10,000、1人1ギルドまで、定員30人。**同時にオンラインなメンバーが2人以上いると、
1人につきLuck +2%（最大5人分 = +10%）** がメンバー全員に付きます。オンライン数は30秒
キャッシュで、Rollのたびに数えなおすことはしません。週間Roll数でギルド対抗ランキングになります。

### 星の欠片 / セット収集 / 転生

- **星の欠片**: 重複アイテム（2つ目以降）を欠片に変換し、次の1回の最低レア度保証と交換できます。
  お気に入り・ロック・出品中・ショーケース表示中のものは対象外です。**確率表そのものは変わりません。**
- **セット収集**: 12のテーマ別セットを図鑑で完成させると、**永続Luck**（+0.5%〜+3.0%）と
  Stardust、一部は称号がもらえます。
- **転生（Prestige）**: 最大レベルに到達すると、レベルを1に戻して**永続Luck +5%**を得られます。
  回数に上限はなく、アイテム・図鑑・Stardust・装備はそのまま残ります。

### 運勢レポート

総Roll数、プレイヤー中の上位％、レア度ごとの「実際 / 期待値」比、直近14日のRoll推移を
まとめた自己分析画面です。期待値は現在の確率テーブルから算出しています。

### ライブ観戦 / シェア

秘匿級（tier 5）以上のドロップは、公開設定をオンにしているプレイヤー全員の画面に流れます
（受け取り側も `notifications.live` で切れます）。伝説級以上の結果にはOGP付きの公開ページの
共有リンクが付きます。URLはHMAC署名されているため、IDの総当たりでは開けません。

---

## 管理者機能

管理者判定は**必ずサーバー側**で行われ、管理者以外が `/api/admin/*` にアクセスすると
**404** が返ります（管理画面の存在自体が分かりません）。

### Player Mode / Admin Mode

管理者はいつでも切り替えられます。Player Mode では通常プレイヤーとして遊べ、
Admin Mode の操作はすべて監査ログに記録されます。

### 機能

| 画面 | 内容 |
|---|---|
| Dashboard | オンライン数・Rolls/sec・レアドロップ・世界初発見・Market/Trade・エラー・Biome分布をリアルタイム表示 |
| Retention | DAU・新規登録・D1/D7コホート・初回セッションのファネル（登録→1Roll→10Roll→Lv.5→翌日再訪） |
| Users | 検索、詳細閲覧、アイテム/Boost付与・削除、Luck変更、Biome変更、次回Roll指定、確率変更、任意効果付与、凍結、BAN、ロール変更 |
| Content | Items / Biomes / Equipment / Boosts / Recipes / Shops / Quests / Achievements / Cosmetics / Artifacts / Events / Rarities / Seasons / Item Parts の作成・編集・削除 |
| Artifacts | 30種のAdmin Artifactのカタログ、付与・回収・所持者一覧 |
| Settings | 60項目以上のゲーム設定（型・範囲つき検証、危険な設定は理由必須）。取引・市場の1日上限、自動バックアップの有無もここ |
| Logs | 監査ログ・Rollログ・Trade・Market（異常検知）・エラー |
| Tools | RNGシミュレーション（カイ二乗検定）、Biome移動、全体通知、バックアップ |

### 一時変更

コンテンツ編集では「一時変更」を選べます。指定時間後に**自動で元へ戻り**、
元データは一切書き換えません（イベント用の確率変更などに使用）。

### Admin Artifact（30種）

一般Rollからは絶対に出現せず、管理者のみが取得・使用できます。
売却・通常Trade・Gift は不可、管理者はいつでも回収できます。
プレイヤーに「使用権」付きで付与することもできます（使用回数・期限の指定可）。

単なるLuck上昇ではなく、ゲームシステムそのものに干渉します。

| Artifact | 効果 |
|---|---|
| INFINITE LUCK | 60秒間 Luck ×1,000,000 |
| TIME BREAKER | 120秒間 Roll間隔 1/20 |
| VOID KEY | Void Sanctum（Luck×25）を開く |
| REALITY SHIFT | 15Rollの間、RNGテーブルを平坦化 |
| STAR FORGER | Secret級以上の自動生成アイテムを鍛造 |
| SYSTEM OVERRIDE | 次のRoll結果を任意のアイテムに書き換え |
| COSMIC EYE | 全アイテムの最終確率と次のBiome変化時刻を可視化 |
| WORLD FRACTURE | 個人Biomeを Fractured Reality へ |
| OMEGA PROTOCOL | The Source + Luck×1兆 + Roll間隔1/10 |
| EVENT HORIZON | **全プレイヤー**のLuckを600秒間×2 |
| …他20種 | |

最上位（tier 5）は約20〜30秒の専用演出が入ります。

---

## セキュリティ

| 対策 | 実装 |
|---|---|
| サーバー側RNG | 確率・Luck・結果はすべてサーバーで決定。クライアントの入力は `auto` フラグのみ |
| セッション | 256bit乱数をHttpOnly Cookieに。DBにはSHA-256ハッシュのみ保存 |
| CSRF | セッションに紐づくトークンを `X-CSRF-Token` で二重送信 + Origin検証 |
| 権限 | 管理者判定はサーバー側のみ。非管理者には404 |
| Rate Limit | エンドポイント種別ごとのトークンバケット + Nginx側の制限 |
| SQL Injection | SQLAlchemyのパラメータバインドのみ。LIKE検索はエスケープ |
| XSS | ReactのJSXエスケープ、`dangerouslySetInnerHTML` 不使用、CSP付与 |
| IDOR | 全てのアイテム操作で `owner_id` を条件に含めて取得 |
| 二重処理 | Idempotency-Key を購入・Trade・Gift・Craft・売却に必須化（トランザクション内で確保） |
| 競合 | 行ロック（`SELECT FOR UPDATE`）+ 一意部分インデックスで二重購入・二重Tradeを排除 |
| WebSocket | Cookie認証 + Origin検証 + 接続数制限 + メッセージRate Limit |
| エラー | 内部情報は返さない。詳細はサーバーログとDBのみ |

これらは自動テストで検証されています（下記）。

---

## テスト

```bash
cd backend
. .venv/bin/activate

# テスト用DBを用意（初回のみ）
sudo -u postgres createdb -O cosmic cosmic_rng_test

pytest -q                      # 全テスト
pytest -q tests/test_rng_engine.py       # RNGのみ（DB不要）
pytest -q -k security                    # セキュリティ関連
```

| ファイル | 内容 |
|---|---|
| `test_rng_engine.py` | 確率モデル、Luck曲線、条件判定、**カイ二乗検定**による分布検証、自動生成アイテム |
| `test_modifiers.py` | Boost重複（4方式）、消費、装備パッシブ、クールダウン、Special Roll |
| `test_biome_engine.py` | Biome出現頻度の理論値との一致、継続時間、特殊状態、長期不在時の処理 |
| `test_api_auth_security.py` | 認証、CSRF、Origin、管理画面の秘匿、IDOR、改ざん、冪等性、**同時実行での二重課金防止**、SQLi、Rate Limit |
| `test_api_game.py` | Roll、クールダウン、自動削除、Boost、オフラインRoll、クエスト、クラフト、Market（**同時購入の競合**）、Trade、Gift、Artifact、管理操作 |
| `test_admin_actions.py` | 管理操作カタログとハンドラの整合、一括操作、権限、コンテンツの日本語名の網羅 |
| `test_ws.py` | WebSocket認証、Origin拒否、イベント配信 |
| `test_v2_features.py` | ログインボーナスの連続・二重取得防止、週間順位、シーズンパスの重複受け取り、フレンド、ギルド（1人1つ・権限・費用）、星の欠片、セット完成、転生、運勢、コスメ購入と日替わり割引、お試しRollと引き継ぎ・Cookie改竄、共有ページの署名、取引/市場の1日上限、継続ダッシュボードの非公開性、スケジューラのジョブ |

RNGの統計検証は管理パネル（Tools → RNGシミュレーション）でも任意の条件で実行できます。

### 画面が本当に描画されているかの検査

DOMを調べるテストは「要素は存在するのに画面には出ていない」状態を見抜けません。
実際にログイン画面が背景の下に描かれて見えなくなる不具合を通してしまったので、
描画そのものを確認するテストを用意しています。要素を隠して箇所を撮影し、表示して
もう一度撮影し、画素が変わらなければ「見えていない」と判定します。

```bash
cd frontend
npm i -D playwright && npx playwright install chromium   # 初回のみ
node tests/visible.mjs http://localhost:8000 admin <パスワード>
```

パスワードを省くとログイン画面だけを検査します。既にあるブラウザを使う場合は
`CHROMIUM_PATH` を指定してください。

---

## プロジェクト構成

```
cosmic-rng/
├── backend/
│   ├── app/
│   │   ├── main.py              FastAPI アプリ本体
│   │   ├── cli.py               管理CLI（migrate / seed / set-role / backup / restore / check）
│   │   ├── config.py            環境変数
│   │   ├── models.py            全テーブル定義
│   │   ├── core/                エラー / セキュリティ / RateLimit / 冪等性 / PubSub
│   │   ├── content/             シードデータとコンテンツレジストリ
│   │   ├── rng/                 RNG Engine / 効果合成 / Biome Engine / 自動生成
│   │   ├── services/            ゲームロジック（roll, market, trades, admin_*, …）
│   │   ├── api/                 REST エンドポイント
│   │   ├── ws/                  WebSocket
│   │   └── tasks/               定期ジョブ（リーダー選出付き）
│   ├── migrations/              Alembic
│   ├── tests/                   pytest
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── App.tsx              ルーティング / レイアウト / 演出キュー
│       ├── audio/               手続き的BGM・効果音（音声ファイル不要）
│       ├── visual/              Canvas宇宙背景 / 演出CSS
│       ├── components/          共通UI / アイテムアイコン / シネマティック
│       ├── pages/               各画面
│       └── store/               状態管理（zustand）
├── deploy/
│   ├── nginx/cosmic-rng.conf
│   ├── systemd/                 サービス + バックアップタイマー
│   └── scripts/                 setup.sh / deploy.sh / restore.sh / dev.sh
└── .env.example
```

### データベース

`users` `user_settings` `user_stats` `sessions` `items` `item_instances` `item_parts`
`item_revisions` `rarities` `biomes` `user_biomes` `equipment` `user_equipment` `boosts`
`inventory` `active_effects` `recipes` `user_recipes` `collections` `rolls` `roll_batches`
`shops` `shop_items` `shop_purchases` `user_unlocks` `cosmetics` `user_cosmetics` `showcases`
`market_listings` `trades` `trade_items` `gifts` `quests` `user_quests` `achievements`
`user_achievements` `seasons` `season_stats` `rankings` `world_events` `game_events`
`notifications` `discord_outbox` `admin_artifacts` `admin_grants` `artifact_cooldowns`
`audit_logs` `game_settings` `content_overrides` `idempotency_keys` `error_logs` `backups`

スキーマ変更は Alembic で管理します。

```bash
cd backend
.venv/bin/alembic revision --autogenerate -m "説明"
.venv/bin/python -m app.cli migrate
```

---

## ライセンス / クレジット

このリポジトリのコードはプロジェクトオーナーに帰属します。
フォントは Google Fonts（Orbitron / Exo 2、SIL Open Font License）を同梱しています。
BGMと効果音は実行時に Web Audio API で合成しており、外部素材は使用していません。

BGMはBiomeごとに一曲ずつ組まれています。旋法・4コードの進行・アルペジオの型・
テンポと打点の密度がBiomeごとに決まっていて、パッドは2小節ごとに次のコードへ滑り、
旋律は4小節ごとに生成し直したモチーフをコードの上に乗せます。合成したホールリバーブ
にセンドしており、演奏はすべて AudioContext の時計上に先読みで配置するので、
メインスレッドが忙しくても途切れません。強度はプレイに追従し（Rollで少し上がり、
Auto Roll中は前に出ます）、設定画面に「現在のBGM」として曲名・BPM・小節が出ます。
