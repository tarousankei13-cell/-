# vendor/

このディレクトリは外部の非公式ライブラリを同梱したものです。
どちらも PyPI 未公開のため、pip ではなくソース同梱で取り込んでいます。

## HATTIMCD

- 出典: https://github.com/hatti1919/HATTIMCD
- ライセンス: MIT (`HATTIMCD/LICENSE` を参照)
- 変更: なし（取得時のまま）

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
