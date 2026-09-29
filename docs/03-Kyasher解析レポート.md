# 03. Kyasher（Kyash非公式API）解析レポート

> 対象: `Kyasher` v1.5.0 / https://github.com/taka-4602/Kyasher
> 依存: `requests`, `bs4`, `pkce`

---

## 1. 役割の確定（重要）

当初「Kyashカードでマクドナルドを決済する」と想定していたが、**実際の用途は逆**。

```
利用者 ──[Kyash送金リンク]──▶ BOTのKyashアカウント ──▶ 内部残高に加算
                                    (link_recieve)

注文時 ──▶ 内部残高から自己負担分を減算
       ──▶ マクドナルドは【アカウントに登録済みのカード】で全額決済
```

つまり **Kyash＝入金（チャージ）の受け口**であり、決済手段ではない。
マクドナルド側の決済カードは `mcd_accounts.card_id`（登録済み）を使う。

---

## 2. API サーフェス

### 2.1 コンストラクタ / ログイン

```python
Kyash(email=None, password=None, client_uuid=None,
      installation_uuid=None, access_token=None, proxy=None)
```

| 初期化パターン | OTP | 備考 |
|---|---|---|
| `Kyash(email, password)` → `login(otp)` | **必要**（SMS 6桁） | 初回のみ。`client_uuid` / `installation_uuid` が自動生成される |
| `Kyash(email, password, client_uuid, installation_uuid)` | **不要** | 登録済みUUIDペアでOTPをスキップ |
| `Kyash(access_token=...)` | 不要 | トークン直指定。UUIDは任意値でよい |

`login(otp)` 後に取得できるもの:

```python
kyash.access_token      # ★有効期限 1ヶ月
kyash.refresh_token
kyash.client_uuid       # ★2つで1セット。必ず保存
kyash.installation_uuid
```

- エンドポイント: `POST https://api.kyash.me/v2/login` → `POST /v2/login/mobile/verify`
- UUID不一致時は `KyashLoginError("登録されていないUUID")`

### 2.2 残高・プロフィール

```python
profile = kyash.get_profile()
#   .username .icon .myouzi .namae .phone .is_kyc

wallet = kyash.get_wallet()
#   .uuid         おさいふUUID
#   .all_balance  合計残高
#   .money        出金可能なキャッシュマネー
#   .value        出金不可のキャッシュバリュー
#   .point        ポイント
```

### 2.3 送金リンク（チャージの核心）

```python
info = kyash.link_check(url)
#   .amount       金額
#   .uuid         ★リンクUUID（これが本体。URL中のIDは飾り）
#   .send_to_me   True=受け取りリンク / False=請求リンク
#   .public_id    請求リンクのみ必要
#   .sender_name  作成者のユーザーネーム

kyash.link_recieve(url=..., link_uuid=...)   # 受け取り実行（※綴りは recieve）
kyash.link_cancel(url=..., link_uuid=...)
kyash.create_link(amount, message, is_claim=False)   # → .link
kyash.send_to_link(url=..., message=..., link_info=...)
```

- `link_check` は **HTMLをスクレイピングして link_uuid を抽出**している
- 処理済みリンクは `KyashError("処理済みのリンクのため、チェックに失敗しました")`
- `link_uuid` を渡せば `link_check` をスキップできる

---

## 3. チャージ方式の設計

### 3.1 採用方式 — 「利用者が送金リンクを貼る」

```
① 利用者が Kyash アプリで送金リンクを作成（金額は利用者が決める）
② Discord のチャージパネル →「🔗 リンクを貼る」→ Modal に URL 入力
③ BOT: link_check(url)
      ├ send_to_me が False → ❌「請求リンクです。送金リンクを作ってください」
      ├ amount が 0 / 下限未満 → ❌
      └ link_uuid が既に DB にある → ❌「このリンクは処理済みです」
④ BOT: get_wallet() で受取前残高を記録
⑤ BOT: link_recieve(link_uuid=...)          ★ここで物理的にお金が動く
⑥ BOT: get_wallet() で受取後残高を検証（差分 == amount）
⑦ BOT: ledger に charge を記帳 → 内部残高に反映
⑧ 利用者へ完了パネルを ephemeral 返信
```

**請求リンク方式を採らない理由**: BOT が `create_link(is_claim=True)` で請求を出す形だと、
支払い完了の検知が `get_history()` のポーリング頼みになり、遅延と取りこぼしが発生する。
利用者が貼る方式なら **その場で同期的に確定**できる。

### 3.2 二重受取・取りこぼしの防止

| リスク | 対策 |
|---|---|
| 同じリンクを2回貼られる | `kyash_receipts.link_uuid` に **UNIQUE 制約**。④の前に INSERT で予約 |
| `link_recieve` 成功後にBOTが落ちる（金は入ったが残高未反映） | ⑤の前に `status='RECEIVING'` で行を確定コミット → 起動時に `RECEIVING` を洗い出して `get_history()` で照合・復旧 |
| `link_recieve` 失敗なのに加算 | **必ず ⑤成功 → ⑥検証 → ⑦記帳** の順。逆順にしない |
| 残高差分が amount と一致しない | 記帳せず `MANUAL_REVIEW` へ。管理者に通知 |
| 同時押し | `discord_id` 単位の advisory lock |

### 3.3 チャージの下限・上限

- 下限: `config.charge_min`（既定 ¥100）— 手数料的な意味ではなく荒らし対策
- 上限: `config.charge_max`（既定 ¥50,000）
- Kyash アカウント側の月間受取上限（KYC状況で変動）を `kyash_accounts.received_this_month` で管理

---

## 4. 複数 Kyash アカウントの運用

### 4.1 受取口座の選択ロジック

```
候補 = status == 'ACTIVE' のアカウント
score = 3.0 × (is_kyc ? 1 : 0)                       KYC済みを優先（受取上限が高い）
      + 2.0 × (1 - received_this_month / monthly_cap) 余裕のある口座を優先
      + 1.0 × (アクセストークンの残存期間 / 30日)      失効間際を避ける
```

同点ならラウンドロビン。

### 4.2 アクセストークンの寿命管理 ⚠️

`access_token` は **1ヶ月で失効**し、リフレッシュ用APIはラッパーに実装されていない。

- `kyash_accounts.token_obtained_at` を記録
- **残り7日を切ったら管理者へ自動DM**（「再ログインが必要です」）
- 再ログインは `client_uuid` + `installation_uuid` を保存してあれば **OTP不要**
  → 保存済みUUIDでの自動再ログインをバックグラウンドで試行し、失敗時のみ管理者へ通知
- 失効したアカウントは自動で `DEGRADED` にして受取プールから外す

---

## 5. 実装上の注意

| # | 事項 |
|---|---|
| 1 | **同期ライブラリ（requests）** → `asyncio.to_thread()` で必ずラップ。直接呼ぶと BOT が固まる |
| 2 | `link_recieve` は綴りが `recieve`（typo）。タイプミスではないので修正しないこと |
| 3 | `link_check` は HTML スクレイピング依存 → **Kyash 側の HTML 変更で壊れる**。失敗時に管理者へ通知する監視を入れる |
| 4 | UA は iOS 16.7.5 / iPhone8 固定。アカウントごとに `client_uuid` / `installation_uuid` が別なのは良い設計（そのまま使う） |
| 5 | `email` / `password` / `access_token` は **暗号化カラムに保存**。ログ出力厳禁 |
| 6 | `proxy` 引数がありアカウント別プロキシを割り当て可能 |

---

## 6. 法務上の留意（設計判断に関わる分のみ）

- 利用者から金銭を預かり内部残高として保持し、**払い戻しは行わない**方針（A-6）。
  自家型前払式支払手段に該当しうるため、**未使用残高の総額が基準日（3/31・9/30）に
  1,000万円を超えると届出義務**が生じる。残高総額の監視コマンドを用意する（`/stats ledger`）。
- Kyash 利用規約上、非公式APIによる自動操作・アカウントの業務利用は制限される可能性がある。
  アカウント凍結を前提に、**複数口座と自動フェイルオーバー**を設計に織り込む。

> 上記はリスク整理であり、最終的な運用判断は運営者が行うもの。
