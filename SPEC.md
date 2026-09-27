# SMS 受信 BOT 設計仕様

Discord 上で、**自分名義で Twilio から借りた米国番号**をプールし、サーバーメンバーに固定時間で貸し出して、
着信 SMS を借り主に DM で通知する開発用受信箱 BOT。

---

## 1. スコープ

### 作るもの
- 自分の Twilio アカウントで購入した US 番号のプール管理（購入・貸出・返却・解放）
- 固定時間の自動返却つきレンタル
- 着信 SMS の Webhook 受信 → 借り主への DM 即時通知 → OTP 自動抽出
- 受信履歴の閲覧
- クレジット残高と上限管理、Kyash による入金照合

### 作らないもの（明示的な非スコープ）
- SMS activation 系リセラー（Eveses / 5SIM / SMS-Activate / DaisySMS 等）との連携
- 受信対象サービスを指定して他社プラットフォームの認証コードを取得する機能
- captcha 解決、residential プロキシ、アンチ検知系の統合

---

## 2. 決定事項

| 項目 | 決定 |
|---|---|
| 提供形態 | 独立した Discord BOT（新規プロセス・新規トークン） |
| 言語 / FW | Python 3.11+ / discord.py 2.x（既存 BOT と同じ cogs 構成） |
| 利用者 | 自分の Discord サーバーのメンバー（ロール制限） |
| 番号取得元 | `SmsProvider` 抽象層 ＋ `MockProvider` ＋ `TwilioProvider` |
| 対象国 | 米国（US）のみ |
| 受信経路 | BOT プロセス内に aiohttp Webhook サーバーを同梱 |
| DB | SQLite ＋ aiosqlite（SQL 層を分離し PostgreSQL へ移行可能にする） |
| レンタル | 固定時間で自動返却（`/extend` で延長可） |
| 通知 | 借り主に DM で即時通知 ＋ OTP を正規表現で抽出して強調 |
| クレジット | 管理者手動付与 ＋ Kyash 入金による購入（有料、規約リスクは承知の上） |
| ホスティング | VPS ＋ Docker Compose ＋ Cloudflare Tunnel |

---

## 3. プロセス構成

```
                    ┌──────────────────────────────────┐
  Twilio ──HTTPS──> │ cloudflared (Cloudflare Tunnel)  │
                    └───────────────┬──────────────────┘
                                    │ localhost:8080
                    ┌───────────────▼──────────────────┐
                    │  bot プロセス (1 つの event loop) │
                    │  ├─ discord.py Client            │
                    │  ├─ aiohttp web.Application      │
                    │  │    POST /webhooks/twilio/sms  │
                    │  │    POST /mock/inbound         │
                    │  │    GET  /healthz              │
                    │  ├─ ReaperTask (期限切れ自動返却) │
                    │  └─ TopupWatcher (Kyash 入金照合) │
                    └───────────────┬──────────────────┘
                                    │
                            SQLite (data/bot.db)
```

Twilio SDK は同期なので、すべての API 呼び出しは `asyncio.to_thread()` 経由で実行する。

### ディレクトリ構成

```
main.py                  エントリポイント（BOT ＋ Web サーバーを同時起動）
config.py                環境変数の読み込みとバリデーション
cogs/
  admin.py               /restart 等（既存を流用）
  rental.py              /rent /mynumbers /release /extend
  inbox.py               /inbox
  credits.py             /balance /topup
  pool_admin.py          /pool /credit /user /mock
providers/
  base.py                SmsProvider ABC ＋ dataclass 群
  mock.py                MockProvider
  twilio_provider.py     TwilioProvider
payments/
  base.py                PaymentProvider ABC
  mock.py                MockPayment
  kyash.py               KyashPayment（モジュール提供待ち）
db/
  schema.sql
  repository.py          SQL をすべてここに閉じ込める
web/
  server.py              aiohttp アプリ
  twilio_webhook.py      署名検証 ＋ 着信ハンドラ
services/
  rental_service.py      レンタルのビジネスロジック（トランザクション境界）
  otp.py                 OTP 抽出
  notifier.py            DM 送信と失敗時のフォールバック
tasks/
  reaper.py              期限切れレンタルの自動返却
  topup_watcher.py       Kyash 入金のポーリング照合
```

---

## 4. SmsProvider 抽象層

```python
@dataclass(frozen=True)
class AvailableNumber:
    phone_number: str          # E.164
    country: str
    monthly_cost_cents: int
    sms_capable: bool

@dataclass(frozen=True)
class ProvisionedNumber:
    provider_sid: str          # プロバイダ側の一意 ID
    phone_number: str
    country: str

@dataclass(frozen=True)
class InboundMessage:
    provider_message_id: str   # 冪等キー
    to_number: str
    from_number: str
    body: str
    received_at: datetime

class SmsProvider(ABC):
    name: str

    async def search_available(self, country: str, limit: int) -> list[AvailableNumber]: ...
    async def provision(self, phone_number: str, webhook_url: str) -> ProvisionedNumber: ...
    async def release(self, provider_sid: str) -> None: ...
    async def list_owned(self) -> list[ProvisionedNumber]: ...

    def verify_webhook(self, url: str, form: Mapping[str, str], headers: Mapping[str, str]) -> bool: ...
    def parse_webhook(self, form: Mapping[str, str]) -> InboundMessage: ...
```

### TwilioProvider の対応 API
| メソッド | Twilio 呼び出し |
|---|---|
| `search_available` | `available_phone_numbers("US").local.list(sms_enabled=True, limit=n)` |
| `provision` | `incoming_phone_numbers.create(phone_number=..., sms_url=..., sms_method="POST")` |
| `release` | `incoming_phone_numbers(sid).delete()` |
| `list_owned` | `incoming_phone_numbers.list()` |
| `verify_webhook` | `twilio.request_validator.RequestValidator(auth_token).validate(...)` |

### MockProvider
- `+1555xxxxxxx` の架空番号を発行し、状態はメモリと DB に保持
- `POST /mock/inbound` または `/mock inbound` コマンドで着信を注入でき、通知から DM までの全経路を Twilio 契約前にテストできる

---

## 5. 番号プールのライフサイクル

```
                  /pool add
                     │
                     ▼
  (Twilio 購入) ─> available ──/rent──> rented ──返却/期限切れ──> cooldown
                     ▲                                              │
                     └──────────── cooldown 満了 ────────────────────┘
                     │
                  /pool remove ─> released（Twilio から解放）
```

**既定は事前プール方式**（`/pool add` で管理者がまとめて購入）。プールが空のときは上限付きで自動購入するオプションを設ける。
Twilio の番号月額は日割りされないため、レンタルごとに購入・解放すると毎回 $1.15 かかる。プールを回す方がはるかに安い。

**cooldown は必須**: 返却直後に別人へ再割当すると、前の借り主宛ての遅延 SMS がその別人に届く。既定 24 時間空ける。
受信履歴は `rental_id` に紐付けるので、次の借り主に前の履歴は見えない。

---

## 6. DB スキーマ

```sql
CREATE TABLE users (
  discord_id   INTEGER PRIMARY KEY,
  credits      INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0),
  is_banned    INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT    NOT NULL
);

CREATE TABLE numbers (
  id                 INTEGER PRIMARY KEY,
  provider           TEXT    NOT NULL,
  provider_sid       TEXT    NOT NULL UNIQUE,
  phone_number       TEXT    NOT NULL UNIQUE,
  country            TEXT    NOT NULL,
  status             TEXT    NOT NULL CHECK (status IN ('available','rented','cooldown','released')),
  cooldown_until     TEXT,
  monthly_cost_cents INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT    NOT NULL
);

CREATE TABLE rentals (
  id               INTEGER PRIMARY KEY,
  number_id        INTEGER NOT NULL REFERENCES numbers(id),
  user_id          INTEGER NOT NULL REFERENCES users(discord_id),
  started_at       TEXT    NOT NULL,
  expires_at       TEXT    NOT NULL,
  returned_at      TEXT,
  credits_charged  INTEGER NOT NULL DEFAULT 0,
  status           TEXT    NOT NULL CHECK (status IN ('active','expired','released'))
);
CREATE UNIQUE INDEX idx_rentals_one_active ON rentals(number_id) WHERE status = 'active';

CREATE TABLE messages (
  id                  INTEGER PRIMARY KEY,
  rental_id           INTEGER REFERENCES rentals(id),
  number_id           INTEGER NOT NULL REFERENCES numbers(id),
  provider_message_id TEXT    NOT NULL UNIQUE,   -- Twilio のリトライを冪等化
  from_number         TEXT    NOT NULL,
  body                TEXT    NOT NULL,
  extracted_otp       TEXT,
  received_at         TEXT    NOT NULL,
  delivered_at        TEXT
);

CREATE TABLE credit_ledger (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(discord_id),
  delta      INTEGER NOT NULL,
  reason     TEXT    NOT NULL,   -- 'admin_grant' | 'rental' | 'refund' | 'topup' | 'monthly_quota'
  ref        TEXT,
  created_at TEXT    NOT NULL
);

CREATE TABLE topups (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(discord_id),
  match_code  TEXT    NOT NULL UNIQUE,   -- 入金メモに入れてもらう照合コード
  amount_yen  INTEGER NOT NULL,
  credits     INTEGER NOT NULL,
  status      TEXT    NOT NULL CHECK (status IN ('pending','matched','expired','cancelled')),
  kyash_tx_id TEXT    UNIQUE,
  created_at  TEXT    NOT NULL,
  matched_at  TEXT
);

CREATE TABLE audit_log (
  id         INTEGER PRIMARY KEY,
  actor_id   INTEGER,
  action     TEXT    NOT NULL,
  target     TEXT,
  detail     TEXT,            -- SMS 本文は絶対に入れない
  created_at TEXT    NOT NULL
);
```

`users.credits` が残高の正、`credit_ledger` が監査用の履歴。`SUM(delta)` と `credits` が一致することを
起動時と `/user info` で検算する。減算は `UPDATE users SET credits = credits - ? WHERE discord_id = ? AND credits >= ?`
で原子的に行い、`rowcount = 0` を残高不足として扱う。

---

## 7. コマンド

### 一般ユーザー（許可ロール必須）
| コマンド | 動作 |
|---|---|
| `/rent [duration]` | プールから番号を 1 つ借りる。クレジットを消費し、期限を DM で通知 |
| `/mynumbers` | 借りている番号と残り時間の一覧 |
| `/inbox [number]` | 受信履歴（ページング付き、ephemeral） |
| `/extend <number> <duration>` | 期限を延長（上限まで） |
| `/release <number>` | 早期返却 |
| `/balance` | クレジット残高と今月の利用状況 |
| `/topup <amount>` | Kyash 入金用の照合コードを発行 |

### 管理者（オーナー / 管理ロール）
| コマンド | 動作 |
|---|---|
| `/pool add <count>` | Twilio から番号を購入してプールに追加 |
| `/pool list` | プールの状態一覧 |
| `/pool remove <number>` | Twilio から解放 |
| `/credit add\|set <user> <n>` | クレジット操作 |
| `/user info\|ban\|unban <user>` | ユーザー管理 |
| `/mock inbound <number> <body>` | 着信注入（MockProvider 時のみ） |
| `/restart` | 再起動（既存実装を流用） |

---

## 8. 着信フロー

1. Twilio が `POST /webhooks/twilio/sms` を叩く
2. `X-Twilio-Signature` を検証。失敗は **403** を返して即終了
3. `provider_message_id` で既存チェック（重複なら 200 を返して終了 = 冪等）
4. `To` から `numbers` を引き、`status='rented'` の active な rental を特定
5. `messages` に INSERT し、OTP を抽出
6. **200 を即返す**（Twilio は 15 秒でタイムアウトするため、DM 送信は `asyncio.create_task` に逃がす）
7. 借り主へ DM。DM が閉じている場合は `delivered_at` を NULL のままにし、監査チャンネルに「DM 失敗」だけを記録（本文は出さない）

### OTP 抽出
- 4〜8 桁の連続数字、`123-456` 形式のハイフン区切りに対応
- `code` / `コード` / `認証` / `verification` / `OTP` の近傍にある候補を優先スコアリング
- 候補が複数ならスコア順に最大 3 件提示し、生の本文も必ず併記する（抽出ミスで詰まないように）

---

## 9. セキュリティと運用

- `TWILIO_AUTH_TOKEN` / Discord トークン / Kyash 認証情報は `.env` のみ。リポジトリには絶対に入れない
- Webhook 署名検証は必須。検証なしで本文を信用しない
- SMS 本文は **DM と ephemeral レスポンスのみ**。公開チャンネルには出さない
- 受信本文は既定 **7 日**で自動削除（OTP は機密情報のため保持期間を短くする）
- 監査ログに SMS 本文を含めない
- レート制限: 1 ユーザーの同時保有数、1 時間あたりの `/rent` 回数に上限
- `numbers.status` の遷移はすべて 1 トランザクション内で行い、Twilio API 失敗時はロールバックする

### コスト目安
US ローカル番号 $1.15/月、受信 $0.0075/通。プール 10 番号なら月 $11.5 ＋ 受信分。

---

## 10. 未確定事項

- **Kyash モジュールのインターフェース**（コード提供待ち）
- クレジットと円のレート、1 レンタルあたりの消費クレジット数
- 利用許可ロール名 / 管理ロール名
- 自動返却の既定時間（案: 30 分、`/extend` 上限 24 時間）
- プール初期サイズ（案: 5 番号）
- 受信本文の保持期間（案: 7 日）
