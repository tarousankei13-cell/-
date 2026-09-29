# 01. HATTIMCD 解析レポート

> 対象: https://github.com/hatti1919/HATTIMCD (`HATTIMCD/main.py` / 1041行)
> 目的: マクドナルド モバイルオーダー非公式APIの挙動を確定し、Discord BOT設計の土台にする。
> 本レポートは**回答待ちの仕様に依存しない事実部分**のみを記載する。

---

## 1. 全体像

HATTIMCD は **「hex を受け取って決済を完了させる」** ライブラリである。
**hex を生成する機能は持っていない**（ここが本プロジェクト最大の未実装領域 → `docs/03` で設計）。

```
[mcdon.asia 等でhex生成] ──▶ hex文字列 ──▶ HATTIMCD.pay_from_hex() ──▶ 注文番号(receipt_number)
                                              ▲ 本ライブラリの守備範囲
```

---

## 2. 認証チェーン

マクドナルドJPアプリは **Plexure(vmob) 系** と **Qorcommerce(mop) 系** の2系統バックエンドを持ち、
前者の JWT を後者の PASETO に交換することで注文APIを叩く。

```
① login(email, password)
     POST https://con-japan-east-prod.vmobapps.com/v3/logins
     → jwtMfaToken

② login_with_mfa(mfa_token, otp)        ※SMS/メールOTP。人手介在が必須
     POST https://con-japan-east-prod.vmobapps.com/v3/loginwithmfa
     → jwtAccessToken / jwtRefreshToken   ★ refresh_token だけ保存すれば以後自動

③ _refresh_access_token()
     GET  https://authorization-vmob-prod-jpe.vmobapps.com/Authorization/AccessToken
     header: refreshToken: <refresh_token>
     → jwtAccessToken / jwtRefreshToken（★ refresh_token もローテーションする＝毎回保存必須）

④ _get_oauth2_code()
     POST https://authorization-vmob-prod-jpe.vmobapps.com///oauth2/code
     client_id = 164d4a98beb14f13ce02a6fb62c7712c
     → code（有効期限 約30秒）

⑤ _refresh_root_paseto()
     POST https://user-api.dir.prod.mop.mcd.qorcommerce.com/app/mcduser.AuthorizationService/VerifyPlexureAuthorizationCode
     body: protobuf { 1: code }
     → root PASETO ("v2.local.…" / 有効期限 約1時間)

⑥ get_pos_paseto(group)
     POST https://tid.ord.{group}.prod.mop.mcd.qorcommerce.com/app/mcdtid.UserPosIdService/GetUserPosIdToken
     Authorization: Bearer <root_paseto>
     → pos PASETO（StoreOrder の body に埋め込む）
```

### ⚠️ 設計上の重要ポイント

| # | 事実 | 設計への影響 |
|---|---|---|
| A | `refresh_token` は ③ のたびに**新しい値へローテーション**する | 取得した新トークンを**必ずDBへ即時永続化**。失敗すると当該アカウントが恒久的にログイン不能になる |
| B | `_ensure_auth()` は **毎API呼び出しで ③④⑤ を強制実行**（＋`authorise_order` 内でさらに⑤を再実行） | 1注文あたり無駄な往復が最低4回。**TokenVault によるキャッシュ化**で体感速度が大幅改善（`docs/03` 提案2） |
| C | OTP は人間が受け取る必要がある | アカウント登録フローは **Discord Modal で OTP 入力**させる2段構えが必須 |
| D | `root_paseto` の TTL はコード上 `time.time() + 3600` の固定値 | 実際の PASETO 内部有効期限とズレる可能性あり。**401 時の自動リトライ**を必ず実装 |

---

## 3. hex フォーマット（protobuf）の完全仕様

hex は **protobuf バイナリを16進文字列化したもの**。
`decode_hex()` と `_build_store_order_body()` の対応関係から、スキーマは以下と確定できる。

### 3.1 トップレベル

| field | 型 | 内容 |
|-------|-----|------|
| `1` | string | **store_id**（例: `"10528"`） |
| `2` / `3` | message | リダイレクトURL群（`{1,2,3}: string` のいずれかに `http…`） |
| `7` | message | **受け取り方法**（後述） |
| `8` | message | **注文本体**（`8.2` に実体） |

### 3.2 受け取り方法（field 7）

```
field[7] が存在しない / 空          → テイクアウト
field[7].field[1] が存在            → イートイン（店内）
field[7].field[2] が存在            → デリバリー
```

> **注意**: 実運用パネル（添付画像1）では「**カウンター受け取り（店内）**」という
> より細かいラベルが表示されている。HATTIMCD の3分類では表現しきれないため、
> `field[7]` のサブメッセージをさらに深く解析して
> 「店内カウンター受取 / 店内テーブルデリバリー / テイクアウト / 駐車場受取 / デリバリー」
> を判別する拡張が必要。→ **要検証項目 V-1**

### 3.3 注文本体（field 8.2）

| field | 型 | 内容 |
|-------|-----|------|
| `8.2.2` | string | **short_order_code** |
| `8.2.3` | varint | 固定値 `1` |
| `8.2.4` | varint | **合計金額（円。名前は amount_cents だが実体は円単位）** |
| `8.2.5` | message[] | **商品リスト**（繰り返し） |

### 3.4 商品アイテム（8.2.5 / 再帰構造）

| field | 型 | 内容 |
|-------|-----|------|
| `1` | varint | `1`（`has_field1`。存在有無に意味がある） |
| `2` | string | **product_id** |
| `3` | varint | 固定値 `1`（数量と推定 → **要検証 V-2**） |
| `5` | message[] | **アドオン（同じ商品アイテム構造で再帰）** |

### 3.5 hex ビルダー実装の指針

`_build_order_item()` / `_build_store_order_body()` が**エンコーダの完成形に極めて近い**。
これを流用して hex 側のトップレベル（1 / 7 / 8）を組めば **hex 生成は実装可能**。

```python
hex_bytes  = _pb_str(1, store_id)
hex_bytes += _pb_msg(7, pickup_submessage)          # テイクアウトなら省略
inner  = _pb_str(2, short_order_code)
inner += _pb_int(3, 1)
inner += _pb_int(4, total_amount)
for p in products:
    inner += _pb_msg(5, _build_order_item(p))
hex_bytes += _pb_msg(8, _pb_msg(2, inner))
hex_str = hex_bytes.hex()
```

> ⛑️ **必須の安全装置 — hex ラウンドトリップ検証**
> 生成した hex を必ず `decode_hex()` に通し、
> `store_id / amount / product_id列 / pickup_method` が意図通りか照合してから決済に進むこと。
> これを省くと「意図しない金額で決済が通る」事故が発生する。

### 3.6 未解明（mcdon.asia の解析が必要）

- `short_order_code` の**生成規則**（サーバー払い出しか、クライアント生成か）
- `field 2 / 3` のリダイレクトURLが決済に**必須か否か**
- 商品 `field 3` が数量なのか固定フラグなのか
- `product_id` の**マスタ取得元**（→ `data.cat` JSON を要調査）

---

## 4. 注文フロー（pay_from_hex）

```
_ensure_auth()                     ③④⑤ を実行
      ↓
decode_hex(hex)                    → store_id / amount / products / pickup
      ↓
store_order(decoded, card_id)      group-e → f → g → h を総当たり
      │  POST https://ord.{group}.prod.mop.mcd.qorcommerce.com
      │       /app/mcdord.UserOrderService/StoreOrder
      └→ order_token
      ↓
authorise_order(order_token)       ★ここで課金が確定する
      │  POST …/AuthoriseOrder    （内部で root_paseto を再取得）
      └→ receipt_number が返ることもある
      ↓
get_paid_order(order_token)        receipt_number が空なら補完取得
      └→ receipt_number  ★ 添付画像の「注文番号 7161」はこれ
      ↓
get_store_name(store_id)           → 店名
```

### 4.1 レスポンス構造（`_parse_order_response`）

| field | 内容 |
|-------|------|
| `1` | order_code |
| `2` | short_code |
| `3` | status |
| `4` | store_id |
| **`9`** | **receipt_number ＝ 注文番号（画像の「7161」）** |
| `10` | order_token |

### 4.2 ⚠️ 分散トランザクション上の致命的リスク

`StoreOrder` と `AuthoriseOrder` は**別リクエスト**であり、間で落ちうる。

| 障害点 | 結果 | 必要な補償 |
|--------|------|-----------|
| StoreOrder 成功 → AuthoriseOrder 失敗 | 注文は登録済み・課金未確定 | 利用者残高のホールド解放 |
| AuthoriseOrder 成功 → 応答喪失 | **課金済みだが注文番号不明** | `GetPaidOrder` で order_token から復旧 |
| AuthoriseOrder 成功 → BOT クラッシュ | **課金済み・残高未引落 or 二重引落** | 起動時の未完了 saga 再開 |

→ **注文を状態機械（Saga）として永続化することが必須**。`docs/03` 提案1で詳述。

### 4.3 group 総当たりのコスト

`store_order()` は `group-e, f, g, h` を順に試し、各試行で `get_pos_paseto()` を叩く。
**最悪ケースで 8 リクエスト**が無駄になる。
→ `store_id → group` の解決結果を**DBにキャッシュ**すれば2回目以降は1発（提案4）。

> なお `_DATA_GROUPS`（h,g,f,e）と `MCD._GROUPS`（e,f,g,h）で**順序が逆**。
> 実測でヒット率の高い順に統一すべき。

---

## 5. カード登録（Veritrans + 3DS）

Kyash バーチャルカードをマクドナルドアカウントへ登録する経路。

```
tokenize_card(PAN, YYMM, CVV, name)
   POST https://api3.veritrans.co.jp/4gtoken
   token_api_key = 9d179769-488a-4769-b84a-e725f233257b
   → Veritrans token (UUID)
      ↓
add_card_with_3ds(v_token)
   POST https://pay.dir.prod.mop.mcd.qorcommerce.com/app/mcdpay.CardService/AddCardWithThreeDS
   → (three_ds_id, acs_url)
      ↓
   ★ acs_url を WebView/ブラウザで開いて 3Dセキュア認証を人手で完了させる
      ↓
get_add_card_result(three_ds_id) → card_id
```

### ⚠️ 設計への影響

- **3DS は完全自動化できない**（ACS画面での認証が必要）。
  → カード登録は**管理者が手動で行う運用**を前提とし、BOT は `card_id` を受け取って保持するだけにする。
- `tokenize_card` は**生のカード番号(PAN)を扱う**。
  → **PAN は絶対にDBへ保存しない**。保存するのは `card_id` と下4桁マスクのみ。

---

## 6. その他の利用可能API

| メソッド | 用途 | BOT での活用案 |
|---------|------|---------------|
| `get_cards()` | 登録カード一覧 | アカウント設定時の card_id 自動取得 |
| `get_store(store_id)` | 店舗情報フル取得（`data.cat.{group}…/{store_id}.json`） | **商品マスタ／営業時間／group 判定の取得元候補** |
| `get_order_buzzer_notification(order_token)` | 呼び出し（バズー）番号 | **「できあがりました」DM通知**に転用（提案6） |
| `get_remaining_coupons()` | 店頭提示クーポン一覧 | クーポン併用機能 |
| `get_user_tags()` / `get_user_prefs()` | ユーザー属性 | アカウント健全性チェック |

---

## 7. デバイスフィンガープリントの問題（重要）

`_VMOB_HEADERS` / `_QOR_HEADERS` に以下が**ハードコード**されている。

```
x-vmob-uid       : 2B616927-5749-4191-80FE-69D38D63719C
x-wmop-deviceid  : DF219981-0E63-4822-A6A7-B9D9852A76FB
x-fb-instance-id : 39841F4EE97D4ECB8135DCD2DDFC3B7F
x-vmob-location_latitude / longitude : 34.997026 / 135.730272（固定＝京都付近）
```

複数アカウントを運用すると **全アカウントが同一端末・同一座標から注文している**ことになり、
極めて不自然な挙動になる。

→ **アカウントごとに固定のランダム UUID / 座標を生成して永続化する**こと（提案3）。
  これは「正常なマルチ端末利用の模倣」であり、アカウント保全の観点で必須。

---

## 8. 要検証項目リスト（実装前に確定させる）

| ID | 内容 | 確定方法 |
|----|------|---------|
| V-1 | `field[7]` の詳細構造（受取方法の細分類） | 各受取方法で生成した hex を採取・比較 |
| V-2 | 商品 `field[3]` が数量かフラグか | 同一商品2個の hex を採取して差分確認 |
| V-3 | `short_order_code` の生成規則 | mcdon.asia の JS 解析 |
| V-4 | `data.cat` JSON に商品マスタ・価格が含まれるか | 任意 store_id の JSON を取得して確認 |
| V-5 | リダイレクトURL(field 2/3) が決済に必須か | 省略した hex で StoreOrder を試行 |
| V-6 | `amount` と実際の請求額の一致（改ざん検知の有無） | 少額で検証 |

---

## 9. 法務・規約上の留意（設計判断に直結する分のみ）

1. **前払式支払手段**: 利用者から金銭を預かって内部残高として保持する場合、
   資金決済法上の「自家型前払式支払手段」に該当しうる（未使用残高が基準日で1,000万円超なら届出義務）。
   → **残高の払い戻し可否・有効期限の設計**を先に決める必要がある（質問 A-6）。
2. **非公式API利用 / 複数アカウント**: マクドナルド・Kyash 双方の利用規約に抵触する可能性が高い。
   アカウント停止を**前提**とした設計（health 管理・即時切り離し・代替アカウントへのフェイルオーバー）にすること。
3. **商標**: マクドナルドのロゴを含むレシート画像を生成・配布する点は商標的にグレー。
   → レシート画像テンプレートのロゴを**自前アイコンへ差し替える**選択肢を残す（質問 E-3）。
4. **カード情報**: PAN・CVV は一切保存しない。`card_id` のみ保持。

> 上記は設計上のリスク整理であり、最終的な運用判断は運営者が行うもの。

---

## 10. 結論 — 本レポートから確定した設計前提

- ✅ hex の**デコードは完全に解明済み**、**エンコードも実装可能**（3.5節）
- ✅ 注文番号（画像の `7161`）は `GetPaidOrder` レスポンス **field 9** から取得できる
- ✅ 受け取り方法・店舗ID・金額はすべて hex から事前に取得でき、**決済前にプレビュー可能**
- ❌ hex の**商品マスタ（product_id ↔ 商品名/価格）は未取得** → mcdon.asia 解析 or `data.cat` 調査が必要
- ❌ 3DS を伴うカード登録は**自動化不可** → 管理者手動運用
- ⚠️ 注文は**分散トランザクション**であり、Saga による状態管理なしでは金銭事故が起きる
