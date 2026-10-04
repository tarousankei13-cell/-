"""
プロキシの一元管理

BOT が外へ出る通信を、どの出口から出すかをここだけで決める。

⚠️ **決め方の優先順位**（上が強い）

    ① 口座ごとの指定        McdAccount.proxy_url など
    ② サービスごとの設定    /proxy set service:マクドナルド url:...
    ③ 全体の設定            /proxy set url:...
    ④ 環境変数              BOT_PROXY
    ⑤ 指定なし（直接出る）

   ①を一番強くしてあるのは、口座ごとに出口を分けたいことがあるため
   （同じIPから複数アカウントで入ると目立つ）。

⚠️ **プロキシのURLには認証情報が入る。**
   画面にもログにも、そのままの形で出してはいけない。
   表示には必ず mask() を通すこと。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit, urlunsplit

from core import settings

log = logging.getLogger("bot.proxy")

ENV_KEY = "BOT_PROXY"

# 「外から見えるIP」を教えてくれる先。
#   ⚠️ 1つが塞がれていても確認できるように複数持つ。
#      どれも JSON で {"ip": "..."} を返す。
CHECK_URLS = (
    "https://api.ipify.org?format=json",
    "https://ipinfo.io/json",
    "https://api.my-ip.io/v2/ip.json",
)

# main.py の設定欄に書かれた値。起動時に set_bootstrap() で渡す。
#   ⚠️ Discord への接続は、DBの設定を読み込む**前**に始まる
#      （ログイン → setup_hook → 接続 の順）。
#      そのため Discord だけは、この値か環境変数しか使えない。
#      /proxy set で変えた分は、次の起動から効く。
_bootstrap: str = ""
_bootstrap_from: str = ""


def set_bootstrap(url: str, *, source: str = "main.py") -> None:
    """起動時に、設定欄・環境変数から読んだ値を登録する。"""
    global _bootstrap, _bootstrap_from
    _bootstrap = (url or "").strip()
    _bootstrap_from = source if _bootstrap else ""


def bootstrap() -> str:
    """設定欄・環境変数から読んだ値。"""
    return _bootstrap


def source_of(service: str = "") -> str:
    """
    いま効いている設定が、どこから来たかを日本語で返す。

    ⚠️ 「設定したのに効かない」の調べ物で一番役に立つ情報なので、
       画面に必ず出すこと。
    """
    service = (service or "").strip().lower()
    if service and _get(f"proxy_{service}"):
        return f"/proxy set の{SERVICES.get(service, service)}の設定"
    if service == "paypay" and _get("paypay_proxy"):
        return "/paypay proxy（古い設定・/proxy set で上書きできます）"
    if _get("proxy_all"):
        return "/proxy set の全体設定"
    # bootstrap を先に見る。main.py 側で
    # 「設定欄 → 環境変数」の順に解決済みなので、そちらの判断が正しい。
    if _bootstrap:
        return _bootstrap_from
    if os.getenv(ENV_KEY, "").strip():
        return f"環境変数 {ENV_KEY}"
    return "設定なし（そのまま出ます）"

# 設定できるサービス。名前 → 画面に出す呼び名
SERVICES: dict[str, str] = {
    "mcd": "マクドナルド",
    "kyash": "Kyash",
    "paypay": "PayPay",
    "discord": "Discord",
    "web": "注文番号ページ・画像取得",
}


def _get(key: str) -> str:
    return str(settings.get(key, "") or "").strip()


def global_proxy() -> str:
    """全体の既定。設定 → 環境変数 → 設定欄 の順に探す。"""
    return (_get("proxy_all")
            or os.getenv(ENV_KEY, "").strip()
            or _bootstrap)


def for_service(service: str) -> str:
    """
    そのサービスで使うプロキシ。無ければ空文字。

    ⚠️ PayPay は先に `paypay_proxy` という名前で入れてしまったので、
       古い名前も読む（設定し直さなくても効くように）。
    """
    service = (service or "").strip().lower()
    if not service:
        return global_proxy()
    own = _get(f"proxy_{service}")
    if not own and service == "paypay":
        own = _get("paypay_proxy")
    return own or global_proxy()


def resolve(service: str = "", account: str | None = None) -> str | None:
    """
    実際に使うプロキシ。使わないときは None。

    account に口座ごとの指定を渡すと、それが最優先になる。
    """
    if account and str(account).strip():
        return _usable(str(account).strip())
    return _usable(for_service(service))


def mask(url: str | None) -> str:
    """
    画面やログに出してよい形にする。

        http://user:pass@proxy.example.com:8080
        → http://user:***@proxy.example.com:8080

    ⚠️ 失敗しても生のURLを返さないこと。分からないときは伏せる。
    """
    if not url:
        return "（なし）"
    try:
        parts = urlsplit(str(url))
        if not parts.hostname:
            return "（設定あり・形式不明）"
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        if parts.username:
            host = f"{parts.username}:***@{host}"
        return urlunsplit((parts.scheme, host, "", "", ""))
    except Exception:
        return "（設定あり・表示できません）"


def split_auth(url: str | None) -> tuple[str | None, tuple[str, str] | None]:
    """
    URLを (認証情報を除いたURL, (ユーザー, パスワード)) に分ける。

    ⚠️ Discord（aiohttp）は認証情報を URL に入れた形を受け取らない。
       proxy と proxy_auth に分けて渡す必要がある。
    """
    if not url:
        return None, None
    try:
        parts = urlsplit(str(url))
        if not parts.hostname:
            return None, None
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        clean = urlunsplit((parts.scheme, host, "", "", ""))
        if parts.username:
            return clean, (parts.username, parts.password or "")
        return clean, None
    except Exception:
        log.warning("プロキシのURLを読めませんでした")
        return None, None


def socks_ready() -> bool:
    """
    socks プロキシが使える環境か。

    ⚠️ httpx は socks を使うのに socksio が必要で、無いまま socks の
       URLを渡すと **クライアントを作る時点で ImportError** になる。
       つまり設定を1つ間違えるだけで、BOTの通信が全部止まる。
       必ずここで確かめてから渡すこと。
    """
    try:
        import socksio  # noqa: F401
    except ImportError:
        return False
    return True


def problem(url: str) -> str:
    """
    設定してはいけない値なら、その理由を日本語で返す。問題なければ空文字。
    """
    if not url:
        return ""            # 空は「解除」なので正しい
    try:
        parts = urlsplit(url)
    except Exception:
        return "URLとして読めません。"
    if parts.scheme not in ("http", "https", "socks5", "socks5h"):
        return (
            "先頭を `http://`、`https://`、`socks5://` のいずれかに"
            "してください。"
        )
    if not parts.hostname:
        return "ホスト名が入っていません。`http://ホスト:ポート` の形です。"
    if parts.scheme.startswith("socks") and not socks_ready():
        return (
            "socks を使うには追加の部品が必要です。\n"
            "`pip install \"httpx[socks]\"` を実行してから設定してください"
            "（入れずに設定すると、BOTの通信が全て止まります）。"
        )
    return ""


def valid(url: str) -> bool:
    """設定してよい形か。"""
    return not problem(url)


# socks を使えないのに設定されている、と気付いた回数（警告は1度だけ出す）
_warned_socks = False


def _usable(url: str) -> str | None:
    """
    実際に渡してよい値にする。渡せないものは None にして警告する。

    ⚠️ ここで弾かないと、設定を間違えた瞬間に全ての通信が
       ImportError で落ちる。落とすよりは、警告して直接つなぐ。
    """
    global _warned_socks
    if not url:
        return None
    if url.lower().startswith("socks") and not socks_ready():
        if not _warned_socks:
            _warned_socks = True
            log.error(
                "socks プロキシ（%s）が設定されていますが、socksio が入って"
                "いないため使えません。プロキシを通さず直接つなぎます。"
                '`pip install "httpx[socks]"` を実行してください。',
                mask(url),
            )
        return None
    return url


def describe() -> list[tuple[str, str]]:
    """いまの設定を (呼び名, 伏せたURL) で返す。画面に出す用。"""
    out = [("全体", mask(global_proxy()))]
    for key, name in SERVICES.items():
        own = _get(f"proxy_{key}") or (
            _get("paypay_proxy") if key == "paypay" else ""
        )
        out.append((name, mask(own) if own else "（全体に従う）"))
    return out


async def check(url: str | None = None, *, timeout: float = 15.0) -> tuple[bool, str]:
    """
    実際に通って、外から見える出口のIPを確かめる。

    ⚠️ 設定しただけでは効いているか分からない。
       「日本から出ているつもりで海外から出ていた」を防ぐために、
       必ず **出口のIP** を見せること。
    """
    from core.http import build_async_client

    target = url if url is not None else global_proxy()
    if target and (why := problem(target)):
        return False, why.replace("\n", " ")

    client = None
    last = "確認先につながりませんでした"
    try:
        client = build_async_client(timeout=timeout, proxy=target or None)
        for endpoint in CHECK_URLS:
            try:
                r = await client.get(endpoint)
            except Exception as e:
                last = f"{type(e).__name__}: {str(e)[:120]}"
                continue
            ip = ""
            try:
                ip = str((r.json() or {}).get("ip") or "")
            except Exception:
                ip = (r.text or "").strip()[:40]
            if ip:
                return True, ip
            last = f"応答を読めませんでした（HTTP {r.status_code}）"
        return False, last
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if client:
            await client.aclose()
