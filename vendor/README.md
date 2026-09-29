# vendor/

このディレクトリは外部の非公式ライブラリを同梱したものです。
どちらも PyPI 未公開のため、pip ではなくソース同梱で取り込んでいます。

## HATTIMCD

- 出典: https://github.com/hatti1919/HATTIMCD
- ライセンス: MIT (`HATTIMCD/LICENSE` を参照)
- 変更: あり。受取方法が注文に反映されないバグを修正しています。

### 受取方法 (field 7) が注文に引き継がれない問題

`_detect_pickup_method()` は HEX の field[7] を読んで
「テイクアウト / イートイン / デリバリー」を判定していましたが、
実際に StoreOrder へ送る `_build_store_order_body()` は
`_pb_msg(7, b"")` と**空の field[7] を固定で送っていました**。

デコーダ自身の規則では「field[7] が空 = テイクアウト」なので、
**HEX がイートインやテーブルデリバリーを指定していても、
注文は必ずテイクアウトとして発注されます。**
店舗側の POS にテイクアウトとして入るため、席まで運ばれません。

修正内容:

| 箇所 | 変更 |
|------|------|
| `DecodedOrder` | `raw_pickup: bytes` を追加し、HEX の field[7] を保持 |
| `decode_hex()` | field[7] の生バイトを `raw_pickup` に格納 |
| `_build_store_order_body()` | 引数 `pickup_blob` を追加し、`_pb_msg(7, pickup_blob)` を送る |
| `MCD.store_order()` | `pickup_blob=decoded.raw_pickup` を渡す |

HEX の field[7] をそのまま透過させる形にしているので、
プロトコルの中身を推測せずに、指定どおりの受取方法で発注されます。

## Kyasher

- 出典: https://github.com/taka-4602/Kyasher
- 変更: あり。以下のバグを修正しています。

| # | 箇所 | 内容 |
|---|------|------|
| A | `__init__` | 引数ガードが `email==password==access_token` になっており、メールだけ渡しても通ってしまう問題を修正 |
| B | `__init__` | UUID指定でOTPをスキップする経路で `X-Auth` に取得済みトークンではなく引数の `access_token`(None) を代入していた問題を修正 |
| C | `get_wallet()` | `Wallet` NamedTuple を `wallet_uuid=` で構築しており `TypeError` で必ず失敗していた問題を修正 |
| D | `link_check()` | `LinkInfo` のキーワード不一致、未定義の `self.link_uuid` 参照、例外を握り潰して誤ったメッセージを出す bare except を修正 |
| E | `send_to_link()` | `link_info` を渡した経路で未定義変数を参照して `NameError` になる問題を修正 |

`refresh_token` を使ったトークン更新はライブラリ側に実装がないため、
アクセストークンの有効期限監視は `mcd/kyash.py` 側で行っています。
