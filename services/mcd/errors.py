"""
マクドナルドのエラー応答を読み解く

注文が失敗したとき、応答は protobuf で返ってくるため、そのまま表示すると

    ErrorCode_ProductValidation\x12\x08products ... \x12| x 1110 9003 > 1110 ...

のような文字列になり、利用者にも管理者にも何が起きたか分からない。

ここでは本文から「エラー符号」と「日本語の説明」を取り出し、
何が起きたのかを区別する。特に区別したいのは次の2つ。

  ・**決済の拒否**（カードの残高不足・期限切れ）
      そのアカウントを使い続けると、以降の注文が全部失敗し続ける。
      アカウントを候補から外して、管理者に知らせる必要がある。

  ・**取り扱い時間外**
      利用者の選び方の問題なので、選び直してもらえばよい。
      アカウントは正常なので、外してはいけない。

⚠️ 応答の構造（protobufの定義）は公開されていないため、
   確実に分かるのは本文に含まれる文字列だけ。
   判別できないものは UNKNOWN として、安全側（手動確認）に倒す。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("bot.mcd.errors")

# エラーの種類
PAYMENT = "PAYMENT"              # 決済の拒否（残高不足・期限切れ・利用不可）
CARD = "CARD"                    # カードが登録されていない・使えない
PRODUCT_TIME = "PRODUCT_TIME"    # その時間は取り扱いがない
PRODUCT_GONE = "PRODUCT_GONE"    # 商品が見つからない・売り切れ
STORE = "STORE"                  # 店舗が閉まっている・受付停止
AUTH = "AUTH"                    # 認証が切れている
UNKNOWN = "UNKNOWN"

# 本文に含まれる文字列から種類を当てる。上にあるものから順に照合する。
#   （日本語の文言は実際の応答から採取したもの）
_RULES: list[tuple[str, tuple[str, ...]]] = [
    (PRODUCT_TIME, (
        "お取り扱いがありません", "お取扱いがありません", "時間帯", "販売時間",
        "ただいまのお時間", "提供時間",
    )),
    (PAYMENT, (
        "残高", "残高不足", "限度額", "ご利用いただけません", "決済", "支払い",
        "カードが拒否", "承認されませんでした", "authoriz", "payment", "declin",
        "insufficient", "ErrorCode_Payment", "ErrorCode_Authorisation",
        "ErrorCode_Settlement", "PaymentError", "CreditCardError",
    )),
    (CARD, (
        "カードが登録", "カード情報", "有効期限", "card not found", "ErrorCode_Card",
        "invalid card", "CardError",
    )),
    (STORE, (
        "店舗", "閉店", "営業時間外", "受付を終了", "ErrorCode_Store",
        "store closed", "StoreError",
    )),
    (PRODUCT_GONE, (
        "product not found", "売り切れ", "在庫", "取り扱いを終了",
        "ErrorCode_ProductValidation", "ProductsError", "ErrorCode_Product",
    )),
    (AUTH, (
        "unauthenticated", "unauthorized", "token", "ErrorCode_Auth",
        "認証",
    )),
]

# 利用者に見せる文と、管理者がすべきこと
_GUIDE: dict[str, tuple[str, str]] = {
    PAYMENT: (
        "ただいま注文を受け付けられませんでした。\n"
        "管理者に連絡しましたので、しばらくお待ちください。\n"
        "残高は元に戻っています。",
        "決済が拒否されました。**カードの残高・利用限度額・有効期限**を確認してください。\n"
        "このアカウントは候補から外しました。`/mcd list` で状態を確認できます。",
    ),
    CARD: (
        "ただいま注文を受け付けられませんでした。\n"
        "管理者に連絡しましたので、しばらくお待ちください。\n"
        "残高は元に戻っています。",
        "カードが使えない状態です。`/mcd card <ID>` で登録し直してください。",
    ),
    PRODUCT_TIME: (
        "選んだ商品が、ただいまの時間は取り扱いされていません。\n"
        "お手数ですが、商品を選び直してください。\n"
        "残高は元に戻っています。",
        "利用者が時間外の商品を選びました。メニューの時間帯が古い可能性があります。",
    ),
    PRODUCT_GONE: (
        "選んだ商品がただいま取り扱いされていません。\n"
        "お手数ですが、商品を選び直してください。\n"
        "残高は元に戻っています。",
        "商品が見つかりませんでした。メニューの同期を確認してください。",
    ),
    STORE: (
        "お店がただいま注文を受け付けていません。\n"
        "別の店舗を選ぶか、時間をおいてお試しください。\n"
        "残高は元に戻っています。",
        "店舗が受付を停止しています。",
    ),
    AUTH: (
        "ただいま注文を受け付けられませんでした。\n"
        "管理者に連絡しましたので、しばらくお待ちください。\n"
        "残高は元に戻っています。",
        "認証が切れています。`/mcd relogin <ID>` で入り直してください。",
    ),
    UNKNOWN: (
        "注文を完了できませんでした。\n"
        "管理者が確認しますので、しばらくお待ちください。\n"
        "残高は元に戻っています。",
        "原因を特定できませんでした。下の詳細を確認してください。",
    ),
}

# アカウント側の問題＝そのアカウントを使い続けても直らないもの
_ACCOUNT_FAULT = {PAYMENT, CARD, AUTH}

_IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_./]{3,}")
_JA = re.compile(
    r"[぀-ヿ一-鿿]"
    r"[぀-ヿ一-鿿ー、。ー（）()、。・％%¥\w\s]{2,}"
)
_ERRCODE = re.compile(r"ErrorCode_\w+")


# 断られた商品の経路。
#
#   9030 > 9997925 > 3120 product not found
#   └セット  └選択枠   └商品
#
# ⚠️ **マクドナルドは「どれが駄目か」をここで正確に教えてくれている。**
#    かつては注文全体から疑わしいものを絞り込んで推測していたが、
#    2つ以上疑わしいと何も学習できず、同じ失敗を繰り返していた。
#    推測せず、ここを読むこと。
_PATH = re.compile(r"\b\d{3,8}(?:\s*>\s*\d{3,8})+")


def rejected_pairs(body: bytes | str) -> list[tuple[str, str]]:
    """
    断られた (枠のコード, 商品のコード) を取り出す。

    経路の**末尾2つ**が「どの枠に、どの商品を入れたのが駄目だったか」。
    経路が1段しかない（商品だけ）場合は、枠が分からないので返さない。
    """
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    out: list[tuple[str, str]] = []
    for m in _PATH.finditer(text):
        parts = [p.strip() for p in m.group(0).split(">") if p.strip()]
        if len(parts) >= 2:
            pair = (parts[-2], parts[-1])
            if pair not in out:
                out.append(pair)
    return out


@dataclass
class McdErrorInfo:
    """解析したエラー。"""
    kind: str = UNKNOWN
    code: str = ""              # ErrorCode_... （分かれば）
    message: str = ""           # マクドナルドの日本語の文言
    status: int = 0             # HTTPの状態
    raw: str = ""               # 元の本文（管理者向け）
    # 断られた (枠, 商品) の組。相手が教えてくれたものだけが入る。
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def account_fault(self) -> bool:
        """このアカウントを使い続けても直らない種類か。"""
        return self.kind in _ACCOUNT_FAULT

    @property
    def user_text(self) -> str:
        """利用者に見せる文。内部の事情は出さない。"""
        text = _GUIDE.get(self.kind, _GUIDE[UNKNOWN])[0]
        # マクドナルド側の文言があれば、それも伝えたほうが親切
        if self.kind in (PRODUCT_TIME, PRODUCT_GONE, STORE) and self.message:
            return f"{self.message}\n\n{text}"
        return text

    @property
    def admin_text(self) -> str:
        """管理者に見せる文。何をすべきかまで書く。"""
        return _GUIDE.get(self.kind, _GUIDE[UNKNOWN])[1]

    def summary(self) -> str:
        """ログ用の1行。"""
        parts = [self.kind]
        if self.code:
            parts.append(self.code)
        if self.message:
            parts.append(self.message)
        return " / ".join(parts)


def _clean(text: str) -> str:
    """
    抜き出した日本語から、protobufの区切りが紛れ込んだ末尾を落とす。

    「…ありません2<選択された商品は」のように、タグの値が
    そのまま文字として見えてしまうため。
    """
    text = text.strip()
    # 末尾の記号や1桁の数字（タグの値）を削る
    return re.sub(r"[\s0-9<>*~\"\'|]+$", "", text)


def extract_strings(body: bytes | str) -> tuple[list[str], list[str]]:
    """本文から (識別子, 日本語の文) を取り出す。"""
    if isinstance(body, str):
        raw = body
        data = body.encode("utf-8", "replace")
    else:
        data = body
        raw = body.decode("utf-8", "replace")

    idents = sorted({m.decode("ascii", "replace") for m in _IDENT.findall(data)})
    messages = []
    for m in _JA.findall(raw):
        cleaned = _clean(m)
        if len(cleaned) >= 4 and cleaned not in messages:
            messages.append(cleaned)
    return idents, messages


def parse(status: int, body: bytes | str) -> McdErrorInfo:
    """
    エラー応答を読み解く。

    分からないときは UNKNOWN を返す。呼び出し側は安全側に倒すこと。
    """
    idents, messages = extract_strings(body)
    raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    haystack = (" ".join(idents) + " " + " ".join(messages)).lower()

    kind = UNKNOWN
    for candidate, needles in _RULES:
        if any(n.lower() in haystack for n in needles):
            kind = candidate
            break

    # HTTPの状態からも補う
    if kind == UNKNOWN:
        if status in (401, 403):
            kind = AUTH
        elif status == 402:
            kind = PAYMENT

    code = ""
    m = _ERRCODE.search(" ".join(idents))
    if m:
        code = m.group(0)

    # 一番説明らしい日本語を選ぶ（短すぎる断片は避ける）
    message = max(messages, key=len) if messages else ""

    info = McdErrorInfo(
        kind=kind, code=code, message=message, status=status, raw=raw[:1000],
        rejected=rejected_pairs(body),
    )
    if info.rejected:
        log.info("断られた組み合わせを相手が教えてくれました: %s", info.rejected)
    log.info("注文のエラーを解析しました: %s (HTTP %s)", info.summary(), status)
    return info


# ------------------------------------------------------------
# 応答を人に渡すときの伏せ字
# ------------------------------------------------------------

# ⚠️ 調査のために応答の全文を管理者へ渡すが、そのまま誰かに貼られると
#    トークンが一緒に流れる。応答の本文に認証情報が入ることは普通ないが、
#    「普通ない」で漏らすと取り返しがつかないので、渡す前に伏せる。
_SECRET_KEYS = (
    "token", "authorization", "auth", "password", "passwd", "pwd",
    "secret", "apikey", "api_key", "accesskey", "access_token",
    "refresh_token", "id_token", "session", "cookie", "credential",
    "bearer",
)

# "token": "xxxx" / token=xxxx / "Authorization":"Bearer xxxx"
_KV = re.compile(
    r'("?(?:' + "|".join(_SECRET_KEYS) + r')"?\s*[:=]\s*"?)([^"\s,&}\]]{8,})',
    re.IGNORECASE,
)
# 単体で転がっている JWT
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")


def scrub(text: str | None) -> str:
    """人に渡す前に、認証情報らしきものを伏せる。

    ⚠️ 伏せすぎて原因が読めなくなっては意味がない。
       商品コードや経路（9030 > 9997925 > 3120）は数字なので、
       鍵の名前の後ろにある長い文字列だけを狙う。
    """
    if not text:
        return ""

    def _hide(m: re.Match) -> str:
        head, value = m.group(1), m.group(2)
        # 全部消すと「入っていた」ことまで分からなくなる。頭だけ残す。
        return f"{head}{value[:4]}…（伏せました:{len(value)}文字）"

    out = _KV.sub(_hide, text)
    out = _JWT.sub("eyJ…（伏せました:JWT）", out)
    return out
