# 09. HEX 実データ検証レポート

> 実際の HEX 1本を `docs/07` のスキーマで解析し、`docs/08` のメニューカタログと突き合わせた結果。
> **解析結果が実データと完全に一致することを確認した。**

---

## 1. 検証結果 — 完全一致 ✅

| 項目 | HEXから読んだ値 | カタログ照合 |
|---|---|---|
| 店舗ID | `13934` | ✅ **南砂町店**（東京都江東区新砂3-3-52）/ group-f |
| 受取方法 | `field 7.1` = `eatIn` | ✅ **店内（カウンター受取）** — `docs/07` §4.2 の通り |
| 商品 | `9180` | ✅ **月見バーガー セット**（VALUE_MEAL） |
| サイド | 枠 `9987009` → `2020` | ✅ **マックフライポテト® M** |
| ドリンク | 枠 `9997918` → `9997914` → `3120` | ✅ **コカ・コーラ M** |
| 合計金額 | `800` | ✅ **¥800** |

### 金額の内訳まで一致

```
月見バーガー セット (9180)
  prePrice（セット表示価格）      ¥800
  ├ price（バーガー分）           ¥340
  ├ 選択枠 9987009（サイド）      ¥370   ← 既定 2020 = ポテトM
  └ 選択枠 9997918（ドリンク）    ¥ 90
                                 ─────
                        合計      ¥800   ←→  HEX の totalAmount = 800 ✅
```

**メニューJSONから計算した金額と、HEXに書かれた金額が1円違わず一致した。**
これで「BOT が自前で注文を組み、金額を正しく算出できる」ことが実証された。

---

## 2. 確定した HEX スキーマ

```
1  storeId               string          "13934"
3  createPaymentMethod   message
   └ 8 { 1: successUrl, 3: failUrl }     ※mcdon.asia経由の場合はそのURLが入る
7  createDeliveryMethod  message (oneof) ← docs/07 §4.2
   └ 1 eatIn {} / 2 takeOut {} / 3 tableDelivery / 4 curbsidePickUp
     5 driveThru / 6 addressDelivery
8  order                 message
   └ 2  item  (repeated)  ★複数商品なら field 2 が複数並ぶ
        ├ 2  productCode   string   "9180"
        ├ 3  quantity      varint   1
        ├ 4  totalAmount   varint   800
        └ 5  component     (repeated / 再帰)
             ├ 1  (present flag)  varint 1
             ├ 2  productCode     string   "9987009"（選択枠）
             ├ 3  quantity        varint   1
             └ 5  component       (再帰) → 実際に選んだ商品 "2020"
```

> `component` は**再帰構造**。セット → 選択枠 → 実商品 → さらにサイズ枠…と入れ子になる。
> 実例では `9997918 → 9997914 → 3120` の3段だった。

---

## 3. 🐛 HATTIMCD のバグ（追加2件・計4件）

### 3.1 `8.2.2` は `shortOrderCode` ではなく `productCode`

```python
# HATTIMCD decode_hex()
for v in f2.get(2, []):
    short_code = _try_str(v)      # ❌ 実際には productCode ("9180")
```

`_build_store_order_body` で同じ位置に書き戻すため**往復では壊れない**が、
`DecodedOrder.short_order_code` の値は商品コードであり、意味が誤っている。
BOT 側では `product_code` として扱うこと。

> あわせて `amount_cents` も名前が誤り。**単位は「円」**であって cents ではない。

### 3.2 ⚠️ 複数商品の注文が壊れる

```python
for f2_raw in _proto_parse(f8_raw).get(2, []):   # 商品ごとにループするが
    f2 = _proto_parse(f2_raw)
    for v in f2.get(2, []): short_code = _try_str(v)   # ❌ 上書きされる
    for v in f2.get(4, []): amount = v                 # ❌ 上書きされる
    for pr in f2.get(5, []): products.append(...)      # 構成品だけが合算される
```

商品が2点以上ある HEX では:
- `short_code` と `amount` が**最後の商品の値で上書き**される
- `_build_store_order_body` は**1商品にまとめて**送ってしまう

→ **BOT 側では `DecodedOrder.items: list[OrderItem]` として複数商品を保持する構造に作り直すこと。**

```python
@dataclass
class OrderItem:
    product_code: str
    quantity: int
    amount: int
    components: list["OrderItem"]

@dataclass
class DecodedOrder:
    store_id: str
    pickup_method: str
    items: list[OrderItem]          # ★複数商品に対応
    total_amount: int               # sum(item.amount)
```

> **要検証**: `8.2.4` が「その商品の金額」か「カート合計」か。
> 単一商品の検証では区別できなかった。2点以上の HEX で確認すること（V-9）。

---

## 4. 実運用で判明したこと — HEXは壊れやすい

今回いただいた HEX は、**2バイト（商品コードの1文字ずつ）が欠落**していた。
コピー時の取りこぼしと思われる。

```
受け取った値: ...1207 393937393138 ...   → "997918"（6バイト）
長さプレフィックス 0x07 は 7バイトを要求 → 不整合
正しい値:     ...1207 39393937393138 ... → "9997918"（7バイト）✅ カタログに存在
```

### 設計への反映（`docs/04` §5.4 を強化）

HEX を受け取ったら、決済前に**3段階の検証**を必ず行う。

| 段階 | 検証内容 | 失敗時 |
|---|---|---|
| ① 構文 | protobuf として完全にパースできるか（**長さプレフィックスと実バイト数の一致**） | 「注文コードが壊れています。もう一度コピーしてください」 |
| ② 意味 | `storeId` が実在し、全 `productCode` がその店舗のカタログに存在するか | 「この店舗では取り扱いのない商品が含まれています」 |
| ③ 金額 | カタログから再計算した合計が `totalAmount` と一致するか | ⚠️ **即中断**。改ざんの可能性があるため管理者へ通知 |

> ③ は**金額改ざん対策の要**。今回「メニューJSONから計算した金額がHEXと一致する」ことを
> 実証できたので、この検証は**確実に実装できる**。

さらに、①で長さ不整合を検出したら、**長さプレフィックスを手がかりに自動修復を試みる**とよい。
今回の欠落は自動復元できた（復元後、カタログ照合も通った）。

---

## 5. 未確定事項の更新

| ID | 内容 | 状況 |
|----|------|------|
| V-3 | `shortOrderCode` の生成元 | HEXには**含まれていなかった**。HATTIMCD が `shortOrderCode` と呼ぶ値は productCode。→ **サーバー払い出しで、送信不要の可能性が高い**。`docs/08` §5 の手順で確認 |
| V-2 | `8.2.5.3` が数量か | ✅ **実機で確定（2026-10）**。同じ商品を3つ並べて送ったら、相手の金額は1個ぶん（¥300）だった。→ **並べてはいけない。数量はこの欄に入れる。** `protocol.merge_items()` で送信直前に必ずまとめる |
| **V-9** | `8.2.4` が商品単価かカート合計か | ⚠️ **まだ未確定**。上の実機結果から、相手は自分でカタログの値段を計算し直していると分かった（こちらが ¥900 と書いても ¥300 と返ってきた）。つまりこの欄は参考値らしい。いまは**単価**を入れている。もし合計が正だった場合も、金額が食い違えば `PriceChanged` が支払い前に止めるので、取り違えても過剰請求にはならない |
| **V-10** | 選択枠の中間ノードは枠ごとに違うのか | ⚠️ **推測で補っている**。実データで分かっているのは `9997918 → 9997914`（通常セットのドリンク）の1本だけ。ところが**朝マックのドリンク枠は 9997925 と別コード**で、中間ノードを付けずに送ったら断られた（実機・2026-10）。いまは「**同じカテゴリを指す枠は同じ中間ノードを使う**」とみなして補っている（`menu.bridge_for`）。朝マックのセットの注文コードを1本貼れば `slot_bridge.learn_from_order()` が本当の値を覚え、推測を上書きする |
