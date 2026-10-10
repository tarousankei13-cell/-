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


# 使えるプロキシの種類。
#
#   ⚠️ **http と https だけ**にしてある。socks は使わない。
#      理由は2つ。
#        ① httpx で socks を使うには socksio を追加で入れる必要があり、
#           入れずに socks のURLを渡すと **クライアントを作る時点で
#           ImportError** になる。設定を1つ間違えるだけで、
#           BOTの通信が全部止まる。
#        ② Discord への接続（aiohttp）は、そもそも socks に対応していない。
#           socks を許すと「他は通るのに Discord だけ通らない」という
#           分かりにくい状態になる。
#
#      追加の部品を入れずに、どこでも同じように動く形だけを残す。
SCHEMES = ("http", "https")


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
    if parts.scheme.lower().startswith("socks"):
        return (
            "socks のプロキシには対応していません。\n"
            "`http://ホスト:ポート` の形でご指定ください"
            "（socks は追加の部品が必要になるうえ、"
            "Discord への接続では使えないためです）。"
        )
    if parts.scheme not in SCHEMES:
        return "先頭を `http://` か `https://` にしてください。"
    if not parts.hostname:
        return "ホスト名が入っていません。`http://ホスト:ポート` の形です。"
    return ""


def advice(url: str, detail: str) -> str:
    """通らなかったときの、次にやることの案内。無ければ空文字。

    ⚠️ `ProxyError: 400 Bad Request` とだけ出しても、何を直せばいいか
       分からない。実際に `http://45.146.163.31/` を設定して400になり、
       原因（ポート番号の書き忘れ）に辿り着けなかった。
       **出た症状から、次にやることを名指しする。**
    """
    tips: list[str] = []
    low = (detail or "").lower()
    try:
        parts = urlsplit(url or "")
    except Exception:
        parts = None

    # ⚠️ いちばん多い書き忘れ。ポートを書かないと 80番 につなぎに行く。
    #    そこに普通のWebサーバーが居れば「400 Bad Request」が返る。
    #    プロキシの待ち受けはたいてい 8080 / 3128 / 8000 などで、
    #    80番であることはまず無い。
    if parts is not None and parts.hostname and not parts.port:
        tips.append(
            f"**ポート番号が入っていません。** `{url}` は80番につなぎに行きます。"
            "買ったプロキシの案内にある番号を付けてください"
            "（例 `http://ホスト:8080`）。"
        )
    if "400" in low or "405" in low or "501" in low:
        tips.append(
            "その住所に居るのが**プロキシではない**かもしれません"
            "（普通のWebサーバーは、プロキシ宛ての要求を断ります）。"
        )
    if "407" in low or "proxy authentication" in low or "401" in low:
        tips.append(
            "**ユーザー名とパスワードが要ります。**"
            "`http://ユーザー名:パスワード@ホスト:ポート` の形で入れ直してください。"
        )
    if "403" in low:
        tips.append(
            "プロキシ側で、**このサーバーのIPが許可されていません**。"
            "プロキシの管理画面で許可リストに追加してください。"
        )
    if "timeout" in low or "timedout" in low or "connecterror" in low:
        tips.append(
            "応答がありません。ホスト名とポート番号の打ち間違いか、"
            "プロキシが止まっている可能性があります。"
        )
    if "ssl" in low or "certificate" in low:
        tips.append(
            "`https://` ではつながりませんでした。"
            "プロキシへの接続は `http://` で指定するものが多いです。"
        )
    return "\n".join(f"・{t}" for t in tips)


def valid(url: str) -> bool:
    """設定してよい形か。"""
    return not problem(url)


# 使えない形だと気付いた分（警告は1度だけ出す）
_warned: set[str] = set()


def _usable(url: str) -> str | None:
    """
    実際に渡してよい値にする。渡せないものは None にして警告する。

    ⚠️ コマンドからの設定は problem() で断っているが、
       **main.py の PROXY_URL と環境変数はそこを通らない**。
       そこに socks を書かれると、ここで弾かない限り
       クライアントを作る時点で落ち、全ての通信が止まる。
       落とすよりは、警告して直接つなぐ。
    """
    if not url:
        return None
    why = problem(url)
    if why:
        if url not in _warned:
            _warned.add(url)
            log.error(
                "プロキシの設定（%s）は使えません。%s "
                "プロキシを通さず直接つなぎます。",
                mask(url), why.replace("\n", " "),
            )
        return None
    return url


def configured() -> bool:
    """
    どこかにプロキシが設定されているか。

    ⚠️ describe() の文面で判定しないこと。
       「（全体に従う）」も文字列なので、設定が無いのに
       「あり」と判定してしまう。
    """
    if global_proxy():
        return True
    return any(for_service(k) for k in SERVICES)


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
