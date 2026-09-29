# 07. mcdon.asia 解析レポート

> 取得日: 2026-09-29 / 対象: `https://mcdon.asia/order/` と `https://mcdon.asia/static/index-DHCU0fIL.js`（1.76MB）

---

## 1. 正体 — 本物のマクドナルドSPAの再ホスト版

mcdon.asia は独自実装ではなく、**マクドナルド公式モバイルオーダーのフロントエンド（React SPA）をそのまま再ホストし、
JavaScript を注入して挙動を書き換えたもの**である。

根拠:
- `<title>マクドナルド モバイルオーダー | McDonald's Japan</title>`
- favicon / logo192.png を `www.mcdonalds.co.jp` から直接参照
- バンドル内に `https://frontend.dir.prod.mop.mcd.qorcommerce.com/static/...` への参照
- バンドルに `mcdord` 名前空間の **protobuf.js 定義が丸ごと含まれている**

---

## 2. Hex 取得の仕組み

HTML 末尾に `window.fetch` のオーバーライドが注入されている。

```js
window.fetch = function (...args) {
  let urlStr = args[0] instanceof Request ? args[0].url : String(args[0]);

  // ★ CheckoutOrder を検出したらURLを自ドメインへ書き換え
  if (urlStr.includes('mcdord.OrderService/CheckoutOrder')) {
    const u = new URL(urlStr);
    const pxUrl = window.location.origin + u.pathname + u.search + u.hash;
    ...
    p.then(r => r.clone().json().then(d => {
      if (d && d.status === 'intercepted_success') showModal(d.hexStream);
    }));
  }
  return _origFetch.apply(this, args);
};
```

### 流れ

```
① 利用者が mcdon.asia 上で（本物のUIで）マクドナルドにログインし、カートを組む
② 「注文を確定」を押すと SPA が CheckoutOrder を呼ぶ
③ 注入スクリプトがその fetch を mcdon.asia 自身のサーバーへ向け直す
④ mcdon.asia のサーバーが リクエストbody を16進化して
   {"status":"intercepted_success","hexStream":"<hex>"} を返す
⑤ 「✅ Hex取得完了」モーダルに hex が表示され、利用者がコピーする
```

### 結論

**hex ＝ `mcdord.OrderService/CheckoutOrder` のリクエストボディ（protobuf）を16進化したもの。**
マクドナルド公式フロントエンドが生成したペイロードそのものである。

### ⚠️ セキュリティ上の注意（設計判断に関わる）

この仕組みは、**マクドナルドの認証情報とリクエストが mcdon.asia のサーバーを経由する**ことを意味する。
BOT 運用で登録するマクドナルドアカウントを mcdon.asia 経由で使う場合、
その運営者にセッションが渡りうる点を認識しておくこと。

→ **`docs/04` Phase 6 で BOT 側が直接 hex を生成できるようにすれば、この経路は不要になる。**
   本レポート §4 の protobuf 定義により、それが実現可能になった。

---

## 3. 注文番号表示ページ（添付画像3の作り方）

もう一つの注入スクリプトが、URLパラメータから**任意の注文番号で完了画面を描画**する。

```
https://mcdon.asia/order/{5桁の店舗ID}?orderId={注文番号}
例: https://mcdon.asia/order/10528?orderId=7161
```

内部では `mock_order_id` / `mock_store_id` / `mock_order_hex` / `mock_short_code` を
cookie と sessionStorage に保存し、SPA の完了画面をその番号で表示させている。
`orderId` は `[^0-9A-Za-z_-]` を除去、`storeId` はパスの `/order/(\d{5})` から取得。

### 設計への反映

添付画像3は**この画面のスクリーンショット**である。したがって DM パネルでは両方を提供するのがよい。

| 手段 | 長所 | 採否 |
|---|---|---|
| **Pillow でテンプレートの番号を差し替え**（`docs/04` §6） | 約50ms・メモリ数MB。MWS(1GB)で問題なし | ✅ **採用** |
| Playwright で上記URLを実描画 | 完全に本物 | ❌ メモリ数百MB。MWSで不可 |
| **上記URLをボタンとしてDMに添える** | 利用者が本物の画面を自分で開ける | ✅ **追加採用** |

→ DM パネルに `[ 🧾 受け取り画面を開く ]` リンクボタンを追加し、
  `https://mcdon.asia/order/{store_id}?orderId={receipt_number}` を指す。

---

## 4. ★ protobuf 定義の入手 — V-1 / V-2 の解決

バンドルに `mcdord` の完全な protobuf.js 定義が含まれていたため、**推測が不要になった。**

### 4.1 `CreateOrderInput`（= hex の中身）

| field | 名前 | 型 |
|---|---|---|
| 1 | `storeId` | string |
| 2 | `deliveryMethod` | `DeliveryMethod` |
| 3 | `createPaymentMethod` | `CreatePaymentMethod` |
| **7** | **`createDeliveryMethod`** | **`CreateDeliveryMethod`** ← **受取方法の指定場所** |
| 8 | （注文本体 / products・items） | |
| 12 | `userPosIdToken` | string |

`docs/01` §3 で推測していた構造と**完全に一致**した。

### 4.2 ★ `CreateDeliveryMethod`（oneof）— V-1 解決

| field | 名前 | 日本語 | BOTでの扱い |
|---|---|---|---|
| **1** | `eatIn` | 店内（カウンター受取） | ✅ 対応する |
| **2** | `takeOut` | テイクアウト | ✅ 対応する |
| **3** | `tableDelivery` | 店内（席まで届ける） | ⚠️ テーブル番号の入力が必要 |
| **4** | `curbsidePickUp` | 駐車場で受け取る | ⚠️ 駐車場番号の入力が必要 |
| **5** | `driveThru` | ドライブスルー | 任意 |
| **6** | `addressDelivery` | デリバリー（McDelivery） | ❌ 住所指定が必要。BOTでは非対応を推奨 |

内部フィールド:
- `EatIn` … ほぼ空メッセージ
- `TakeOut` … ほぼ空メッセージ
- `TableDelivery` … `tableNumber`
- `CurbsidePickUp` … `curbsideNumber`
- `AddressDelivery` / `DriveThru` … `address` / `expectedDeliveryAt` / `isContactless` / `createDropOff` など

**実装用ペイロード**（`docs/04` §5.4 の `PICKUP_PAYLOADS`）

```python
PICKUP_PAYLOADS = {
    "テイクアウト":            _pb_msg(2, b""),           # takeOut
    "店内（カウンター受取）":   _pb_msg(1, b""),           # eatIn
    "店内（席まで）":          _pb_msg(3, _pb_int(1, table_no)),   # tableDelivery ※要検証
    "駐車場で受け取る":        _pb_msg(4, _pb_int(1, curbside_no)),# curbsidePickUp ※要検証
}
# これを createDeliveryMethod として field 7 に入れる:
#   body += _pb_msg(7, PICKUP_PAYLOADS[選択値])
```

### 4.3 `Order`（レスポンス）— `docs/01` §4.1 の確定版

| field | 名前 |
|---|---|
| 1 | `orderCode` |
| 2 | `shortOrderCode` |
| 3 | `status` |
| 4 | `storeId` |
| 5 | `deliveryMethod` |
| 6 | `paymentMethod` |
| 7 | `totalAmount` |
| **9** | **`displayOrderNumber`** ← **これが注文番号（画像の 7161）** |
| 10 | `orderToken` |
| 11 | `consumerViewStatus` |

HATTIMCD が `receipt_number` と呼んでいるものの正式名称は **`displayOrderNumber`**。

---

## 5. 🐛 HATTIMCD のバグを2件発見

### 5.1 受取方法の判定が誤っている

```python
# HATTIMCD/main.py _detect_pickup_method()
if 2 in inner: return "デリバリー"     # ❌ field 2 は takeOut
if 1 in inner: return "イートイン"     # ✅ field 1 は eatIn（正しい）
```

`field 2` は **takeOut（テイクアウト）** であり、デリバリーは **field 6（addressDelivery）** である。
またコードは「field 7 が空 → テイクアウト」としているが、正しくは**受取方法が未指定**の状態。

**修正版**

```python
_PICKUP_NAMES = {
    1: "店内（カウンター受取）",   # eatIn
    2: "テイクアウト",             # takeOut
    3: "店内（席まで）",           # tableDelivery
    4: "駐車場で受け取る",         # curbsidePickUp
    5: "ドライブスルー",           # driveThru
    6: "デリバリー",               # addressDelivery
}

def detect_pickup_method(hex_str: str) -> str:
    top = _proto_parse(bytes.fromhex(hex_str.strip()))
    f7 = top.get(7, [])
    if not f7 or not isinstance(f7[0], bytes) or not f7[0]:
        return "未指定"
    inner = _proto_parse(f7[0])
    for fn, name in _PICKUP_NAMES.items():
        if fn in inner:
            return name
    return "未指定"
```

### 5.2 StoreOrder が受取方法を送っていない

`_build_store_order_body()` は `_pb_msg(7, b"")` と**空の createDeliveryMethod** を送るため、
hex に何が書かれていても受取方法が伝わらない（`docs/01` §11）。
`pickup_payload` を注入できるようフォークすること。

---

## 6. 残る未解明点

| ID | 内容 | 状況 |
|----|------|------|
| ~~V-1~~ | 受取方法のペイロード | ✅ **解決**（§4.2）。`tableNumber` / `curbsideNumber` のフィールド番号のみ実測で確認 |
| V-2 | 商品 `field 3` が数量かフラグか | バンドル内の `Product` / `OrderItem` 定義を追えば確定可能 |
| V-3 | `shortOrderCode` の生成元 | `Order` のレスポンスに含まれる＝**サーバー払い出し**。`CreateOrder` の応答から得ると推定 |
| V-4 | 商品マスタの取得元 | バンドルに `Menu` メッセージあり。`data.cat` JSON と突き合わせて確認 |
| V-5 | リダイレクトURL(field 2/3)の要否 | field 2 は `deliveryMethod`、field 3 は `createPaymentMethod` と判明。URLではなかった |

> バンドル `index-DHCU0fIL.js` は**マクドナルドAPIの完全なスキーマ辞書**として使える。
> 今後フィールドの意味が不明になったら、まずこのバンドルを当たること。
> （解析用コピーはリポジトリに含めない。必要になったら再取得する）
