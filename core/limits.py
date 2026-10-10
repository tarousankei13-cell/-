"""
注文の入口の条件

「最低いくらから注文できるか」「はじめての注文の前にいくらチャージ
してもらうか」を判断する。

⚠️ ここで止めるのは **お金が動く前**。先に残高を押さえてから断ると、
   押さえたぶんを戻す処理が要る。注文を組み立てる前に確かめること。
"""

from __future__ import annotations

import logging
import math

from sqlalchemy import func, select

import config
from core import settings
from db.models import KyashReceipt, User
from db.session import session_scope

log = logging.getLogger("bot.limits")

# チャージの記帳まで終わった状態（services/kyash/charge.py と同じ値）
CREDITED = "CREDITED"


class LimitError(Exception):
    """利用者にそのまま見せてよい、注文を受けられない理由。"""


# ============================================================
#  チャージ率
# ============================================================

# 口座の種類。設定のキーに使うので、**勝手に変えないこと**
# （変えると既に保存されている設定が読めなくなる）。
PROVIDERS = ("kyash", "paypay")
PROVIDER_LABEL = {"kyash": "Kyash", "paypay": "PayPay"}


def _rate_key(provider: str) -> str:
    return f"charge_rate_{provider}"


def charge_rate(provider: str | None = None) -> int:
    """チャージ率（％）。100 なら送金額がそのまま残高になる。

    ⚠️ 口座の種類ごとに決められる。**設定が無ければ共通の値に戻る**
       ので、片方だけ決めてももう片方が壊れない。

    ⚠️ 0 や負の値を許さない。0にすると、いくら送っても残高が
       1円も増えないのに受け取りだけ成立する。
    """
    common = max(1, int(settings.get("charge_rate", config.CHARGE_RATE)))
    if not provider:
        return common
    key = _rate_key(str(provider).lower())
    v = settings.get(key, None)
    if v in (None, ""):
        return common
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        log.warning("チャージ率の設定が読めません: %s=%r。共通の値を使います", key, v)
        return common


def rate_is_set(provider: str) -> bool:
    """その口座に専用のチャージ率が入っているか（共通との区別）。"""
    return settings.get(_rate_key(str(provider).lower()), None) not in (None, "")


def credited_for(sent: int, provider: str | None = None) -> int:
    """
    その送金額で、残高にいくら入るか。

    ⚠️ 端数は切り捨てる。切り上げると、1円ずつとはいえ毎回
       運営の持ち出しが増えるため。
    """
    sent = max(0, int(sent))
    return math.floor(sent * charge_rate(provider) / 100)


def bonus_for(sent: int, provider: str | None = None) -> int:
    """チャージ率で上乗せされるぶん。"""
    return credited_for(sent, provider) - max(0, int(sent))


# ============================================================
#  注文の入口
# ============================================================

def order_min() -> int:
    """注文できる最低額（定価）。0 なら制限なし。"""
    return max(0, int(settings.get("order_min", config.ORDER_MIN)))


def first_charge_gate() -> bool:
    return bool(settings.get("first_charge_gate", config.FIRST_CHARGE_GATE))


def first_charge_min() -> int:
    return max(0, int(settings.get("first_charge_min", config.FIRST_CHARGE_MIN)))


async def charged_total(discord_id: int) -> int:
    """
    その人が **実際に送金した** 累計額。

    ⚠️ 元帳ではなく受取の記録から数える。元帳にはチャージ率で
       上乗せしたあとの額が入っているので、率を上げるほど条件が
       緩くなってしまう。
    """
    # ⚠️ Kyash と PayPay の **両方** を数えること。片方しか見ないと、
    #    そちらで入れた人がいつまでも条件を満たせない。
    from db.models import PayPayReceipt

    total = 0
    async with session_scope() as s:
        for model in (KyashReceipt, PayPayReceipt):
            total += int(
                (
                    await s.execute(
                        select(func.coalesce(func.sum(model.amount), 0))
                        .where(
                            model.discord_id == discord_id,
                            model.status == CREDITED,
                        )
                    )
                ).scalar() or 0
            )
    return total


async def check_order(discord_id: int, list_price: int) -> None:
    """
    注文を受けてよいか。受けられないときは LimitError を投げる。

    ⚠️ 判定に使うのは **定価**。負担率を変えても基準がぶれないため。
    """
    low = order_min()
    if low > 0 and int(list_price or 0) < low:
        raise LimitError(
            f"ご注文は **¥{low:,}**（定価）以上から承っております。\n"
            f"いまのご注文は ¥{int(list_price or 0):,} です。\n"
            "商品を追加してから、もう一度お試しください。"
        )

    if not first_charge_gate():
        return

    need = first_charge_min()
    if need <= 0:
        return

    async with session_scope() as s:
        row = await s.get(User, discord_id)
        done = int(getattr(row, "total_orders", 0) or 0) if row else 0
    if done >= 1:
        return                      # 2回目以降は見ない

    paid = await charged_total(discord_id)
    if paid >= need:
        return
    raise LimitError(
        f"はじめてのご注文の前に、**¥{need:,}** 以上のチャージをお願いしております。\n"
        f"これまでのチャージ: **¥{paid:,}**（あと **¥{need - paid:,}**）\n\n"
        "チャージパネルからチャージしてください。\n"
        "※2回目以降のご注文では、この条件はありません。"
    )


async def first_order_notice(discord_id: int) -> str:
    """
    まだ条件を満たしていない人に見せる一言。満たしていれば空。

    注文パネルやチャージ画面で、先に知らせるために使う。
    """
    if not first_charge_gate() or first_charge_min() <= 0:
        return ""
    async with session_scope() as s:
        row = await s.get(User, discord_id)
        done = int(getattr(row, "total_orders", 0) or 0) if row else 0
    if done >= 1:
        return ""
    need = first_charge_min()
    paid = await charged_total(discord_id)
    if paid >= need:
        return ""
    return (
        f"はじめてのご注文には、累計 **¥{need:,}** 以上のチャージが必要です"
        f"（いま ¥{paid:,}／あと ¥{need - paid:,}）。"
    )
