"""
注文番号ページの見た目

⚠️ このファイルは**BOT本体にも、別置きのページにも同じものを置く**。
   標準ライブラリ以外を import しないこと。BOT の設定やDBに触れると、
   ページ単体で動かせなくなる。

   配布ZIPを作るとき、このファイルをページ側へそのままコピーする。
   2か所に書き分けると、片方だけ直して見た目がずれる。
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

STYLE = """
:root{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--sub:#6e6e73;--line:#e3e3e6;--accent:#bf0a30}
@media (prefers-color-scheme:dark){
  :root{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;--sub:#98989d;--line:#2c2c2e;--accent:#ff5a5f}
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
  display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:var(--card);border-radius:20px;padding:32px 24px;width:100%;max-width:420px;
  box-shadow:0 2px 24px rgba(0,0,0,.08);text-align:center}
.label{font-size:13px;color:var(--sub);letter-spacing:.08em;margin:0 0 4px}
.number{font-size:72px;font-weight:700;line-height:1.1;margin:0;
  font-variant-numeric:tabular-nums;letter-spacing:.02em}
.store{font-size:18px;font-weight:600;margin:24px 0 2px}
.meta{font-size:14px;color:var(--sub);margin:0}
hr{border:0;border-top:1px solid var(--line);margin:24px 0}
.note{font-size:13px;color:var(--sub);line-height:1.7;text-align:left}
.bad{font-size:17px;font-weight:600;margin:0 0 8px}
.accent{color:var(--accent)}
"""


def shell(title: str, body: str) -> str:
    """1枚のHTMLにまとめる。外部のCSSや画像は読み込まない。"""
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><div class='card'>{body}</div></body></html>"
    )


def not_found_html() -> str:
    return shell(
        "見つかりません",
        "<p class='bad'>このページは見つかりませんでした</p>"
        "<p class='note'>リンクが間違っているか、"
        "受け取りの期限を過ぎています。<br>"
        "注文番号は Discord に届いた控えでも確認できます。</p>",
    )


def order_html(
    *, receipt_number: str, store_name: str = "", store_id: str = "",
    pickup_label: str = "", created: datetime | None = None,
) -> str:
    """
    注文番号のページ。

    ⚠️ 受け取った文字はすべてエスケープすること。
       店名にタグが入っていてもページを壊されないように。
    """
    when = ""
    if created is not None:
        try:
            when = created.astimezone(JST).strftime("%-m月%-d日 %H:%M")
        except (ValueError, OSError):
            # %-m は環境によって使えない
            when = created.astimezone(JST).strftime("%m月%d日 %H:%M")

    rows = "".join(
        f"<p class='meta'>{html.escape(x)}</p>"
        for x in (pickup_label, when) if x
    )
    body = (
        "<p class='label'>ご注文番号</p>"
        f"<p class='number accent'>{html.escape(receipt_number)}</p>"
        f"<p class='store'>{html.escape(store_name or store_id)}</p>"
        f"{rows}"
        "<hr>"
        "<p class='note'>カウンターでこの番号をお伝えください。<br>"
        "お受け取りの際、画面をそのままお見せいただけます。</p>"
    )
    return shell(f"ご注文番号 {receipt_number}", body)
