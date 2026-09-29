"""
パネルの見た目

絵文字は emoji.py の定数のみを使う（カスタム絵文字はサーバーを跨ぐと表示されない）。
文言の仕様は docs/05。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import discord

import emoji as E
from core import settings
from core.subsidy import Quote

GREEN = 0x2ECC71
BLUE = 0x3498DB
ORANGE = 0xE67E22
RED = 0xE74C3C
GREY = 0x95A5A6
YELLOW = 0xF1C40F


def yen(v: int) -> str:
    return f"¥{v:,}"


# ============================================================
#  常設パネル
# ============================================================

ORDER_MODE_LABEL = {
    "both": "注文コード・メニューどちらでも",
    "hex": "注文コード(HEX)のみ",
    "menu": "メニューから選ぶ方式のみ",
}


def order_panel(mode: str | None = None) -> discord.Embed:
    """
    注文パネル。

    はじめて使う人でも迷わないよう、手順を番号付きで書く。
    注文方式（mode）によって説明を切り替える。
    """
    mode = mode or settings.get("order_mode", "both")
    rate = float(settings.get("subsidy_rate", 40.0))
    user_rate = 100 - rate

    e = discord.Embed(
        title=f"{E.BURGER} マクドナルド注文",
        description=(
            "**下のボタンを押すだけで注文できます。**\n"
            f"{E.INFO} 押した先の画面は、あなたにしか見えません。"
        ),
        color=GREEN,
    )

    # ── 手順 ──
    if mode == "hex":
        steps = (
            "**1.** 💴 チャージパネルで残高を入れる\n"
            "**2.** 🍔 下の「注文する」を押す\n"
            "**3.** 📋 注文コード(HEX)を貼り付ける\n"
            "**4.** 📍 受取方法を選んで確定\n"
            "**5.** 🧾 DMに届く**注文番号**をお店で伝える"
        )
    elif mode == "menu":
        steps = (
            "**1.** 💴 チャージパネルで残高を入れる\n"
            "**2.** 🍔 下の「注文する」を押す\n"
            "**3.** 🏪 お店を選ぶ（**店名の一部**でさがせます）\n"
            "**4.** 🍔 商品を選んでカートに入れる\n"
            "**5.** 📍 受取方法を選んで確定\n"
            "**6.** 🧾 DMに届く**注文番号**をお店で伝える"
        )
    else:
        steps = (
            "**1.** 💴 チャージパネルで残高を入れる\n"
            "**2.** 🍔 下の「注文する」を押す\n"
            "**3.** どちらかを選ぶ\n"
            "　　📋 注文コードを貼る　／　🍔 メニューから選ぶ\n"
            "**4.** 📍 受取方法を選んで確定\n"
            "**5.** 🧾 DMに届く**注文番号**をお店で伝える"
        )
    e.add_field(name="📖 はじめての方へ", value=steps, inline=False)

    # ── ボタンの説明 ──
    buttons = [f"{E.BURGER} **注文する**\n　ご注文はここから始めます。"]
    if mode != "menu":
        buttons.append(
            f"{E.RECEIPT} **注文コードを作る**\n"
            "　コードを作るだけ。**お金はかかりません。**"
        )
    buttons.append(f"{E.HISTORY} **履歴**\n　これまでの注文と残高を確認します。")
    e.add_field(name="💡 ボタンの説明", value="\n".join(buttons), inline=False)

    # ── 割引 ──
    e.add_field(
        name="💴 いまの割引",
        value=(
            f"定価の **{user_rate:g}%** のお支払いで注文できます"
            f"（**{rate:g}% OFF**）\n"
            f"　例）定価 ¥590 → お支払い **¥{math.ceil(590 * user_rate / 100):,}**"
        ),
        inline=False,
    )

    e.set_footer(text="残高が足りないときは、チャージパネルからチャージしてください")
    return e


def charge_panel() -> discord.Embed:
    cmin = int(settings.get("charge_min", 100))
    cmax = int(settings.get("charge_max", 50_000))
    e = discord.Embed(
        title=f"{E.YEN} 残高チャージ",
        description="Kyash の送金リンクで残高をチャージできます。",
        color=BLUE,
    )
    e.add_field(
        name="手順",
        value=(
            "**1.** Kyash アプリで「送金リンク」を作成\n"
            "**2.** 下の「チャージする」からリンクを貼り付け\n"
            "**3.** 受け取りが完了すると即座に残高へ反映されます"
        ),
        inline=False,
    )
    e.add_field(
        name=f"{E.INFO} ご利用にあたって",
        value=(
            f"・1回のチャージは {yen(cmin)} 〜 {yen(cmax)} です\n"
            f"{E.WARN} 請求リンクは受け付けできません\n"
            f"{E.WARN} チャージした残高の払い戻しはできません"
        ),
        inline=False,
    )
    return e


def admin_panel(stats: dict) -> discord.Embed:
    e = discord.Embed(
        title=f"{E.GEAR} 管理パネル",
        color=ORANGE,
        timestamp=datetime.now(timezone.utc),
    )
    maint = settings.get("maintenance", False)
    e.description = (
        f"{E.MAINTENANCE} **メンテナンス中**（注文を停止しています）"
        if maint else f"{E.GREEN} 稼働中"
    )
    e.add_field(
        name=f"{E.KEY} アカウント",
        value=(
            f"マクドナルド **{stats.get('mcd_active', 0)}** / {stats.get('mcd_total', 0)} 件\n"
            f"Kyash **{stats.get('kyash_active', 0)}** / {stats.get('kyash_total', 0)} 件"
        ),
        inline=True,
    )
    e.add_field(
        name=f"{E.CHART} 本日",
        value=(
            f"注文 **{stats.get('orders_today', 0)}** 件\n"
            f"負担 **{yen(stats.get('subsidy_today', 0))}**"
        ),
        inline=True,
    )
    e.add_field(
        name=f"{E.WALLET} 残高",
        value=(
            f"利用者合計 **{yen(stats.get('outstanding', 0))}**\n"
            f"要確認 **{stats.get('review', 0)}** 件"
        ),
        inline=True,
    )
    e.set_footer(text="数値は「更新」を押すと最新になります")
    return e


# ============================================================
#  注文フロー
# ============================================================

def choose_method(balance: int, quote_rate: float) -> discord.Embed:
    e = discord.Embed(
        title=f"{E.BURGER} 注文方法を選んでください",
        color=GREEN,
    )
    e.add_field(name=f"{E.WALLET} 残高", value=f"**{yen(balance)}**", inline=True)
    e.add_field(
        name=f"{E.CHART} あなたの支払い率",
        value=f"**{quote_rate:g}%**",
        inline=True,
    )
    return e


def order_preview(
    *,
    store_name: str,
    store_id: str,
    item_lines: list[str],
    quote: Quote,
    balance: int,
    pickup_label: str | None,
    warning: str | None = None,
) -> discord.Embed:
    e = discord.Embed(title=f"{E.BURGER} 注文内容の確認", color=GREEN)
    e.add_field(
        name=f"{E.STORE} 店舗",
        value=f"{store_name or '（名称不明）'}\n`{store_id}`",
        inline=True,
    )
    e.add_field(
        name=f"{E.PIN} 受取方法",
        value=pickup_label or f"{E.WARN} 未選択",
        inline=True,
    )
    e.add_field(name="​", value="​", inline=True)

    body = "\n".join(item_lines) if item_lines else "（商品情報を取得できませんでした）"
    e.add_field(name=f"{E.CART} ご注文", value=body[:1024], inline=False)

    after = balance - quote.user_amount
    e.add_field(
        name=f"{E.YEN} お支払い",
        value=(
            f"**{yen(quote.user_amount)}**\n"
            f"{quote.describe()}（負担 {quote.subsidy_rate:g}% 適用）"
        ),
        inline=True,
    )
    e.add_field(
        name=f"{E.WALLET} 残高",
        value=f"{yen(balance)} → **{yen(max(after, 0))}**",
        inline=True,
    )
    if quote.capped:
        e.add_field(
            name=f"{E.WARN} 負担率について",
            value=quote.source,
            inline=False,
        )
    if warning:
        e.color = ORANGE
        e.add_field(name=f"{E.WARN} 確認してください", value=warning, inline=False)
    return e


PROGRESS_STEPS = [
    ("hold", "残高を確保"),
    ("store", "店舗を確認"),
    ("send", "マクドナルドへ送信"),
    ("receipt", "注文番号を取得"),
]


def progress(done: list[str], current: str | None = None, detail: dict | None = None) -> discord.Embed:
    detail = detail or {}
    lines = []
    for key, label in PROGRESS_STEPS:
        extra = f"　{detail[key]}" if key in detail else ""
        if key in done:
            lines.append(f"{E.OK} {label}{extra}")
        elif key == current:
            lines.append(f"{E.PROGRESS} {label}…")
        else:
            lines.append(f"{E.WAIT} {label}")
    return discord.Embed(
        title=f"{E.LOADING} 注文を処理しています…",
        description="\n".join(lines),
        color=BLUE,
    )


# ============================================================
#  DM完了パネル（docs/05 §4）
# ============================================================

def dm_complete(
    *,
    receipt_number: str,
    store_name: str,
    store_id: str,
    pickup_label: str,
    list_price: int,
    user_amount: int,
    subsidy_rate: float,
    balance: int,
    total_orders: int,
    feedback_required: bool = False,
) -> discord.Embed:
    user_rate = 100 - subsidy_rate
    e = discord.Embed(
        title=f"{E.OK} ご注文が確定しました",
        description=(
            "マクドナルドでの注文が成立しました。\n"
            "下のレシート画像は必ず保存しておいてください。"
        ),
        color=GREEN,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name=f"{E.RECEIPT} 注文番号", value=f"```\n{receipt_number or '----'}\n```", inline=True)
    e.add_field(
        name=f"{E.STORE} 店舗",
        value=f"{store_name or '—'}\n`{store_id}`",
        inline=True,
    )
    e.add_field(name=f"{E.PIN} 受取方法", value=pickup_label, inline=True)
    e.add_field(
        name=f"{E.YEN} お支払い",
        value=(
            f"**{yen(user_amount)}**\n"
            f"定価 {yen(list_price)} のうち {user_rate:g}%（負担 {subsidy_rate:g}% 適用）"
        ),
        inline=False,
    )
    e.add_field(name=f"{E.WALLET} 残りの残高", value=f"**{yen(balance)}**", inline=True)
    first = f"　{E.PARTY} はじめてのご注文ありがとうございます！" if total_orders <= 1 else ""
    e.add_field(name=f"{E.FRIES} ご利用回数", value=f"通算 **{total_orders}** 回目{first}", inline=True)

    if feedback_required:
        e.add_field(
            name=f"{E.NOTE} ご感想のお願い",
            value=(
                "このメッセージへの返信でご感想をお送りください。\n"
                "次回のご注文時に必要となります。\n"
                "・テキストのみで構いません\n"
                "・お写真の添付は任意です"
            ),
            inline=False,
        )
    e.set_footer(text="ご利用ありがとうございます")
    e.set_image(url="attachment://receipt.png")
    return e


def receipt_fallback(
    *,
    receipt_number: str,
    store_name: str,
    store_id: str,
    pickup_label: str,
    with_image: bool = True,
) -> discord.Embed:
    """
    DMが送れなかったときに、その場で出す控え。

    店頭で必要なのは注文番号なので、まずそれを大きく出す。
    """
    e = discord.Embed(
        title=f"{E.RECEIPT} ご注文の控え",
        description=(
            "この画面をお店で提示してください。\n"
            "**閉じると再表示できません。** 画像の保存か、番号の書き留めをお願いします。"
        ),
        color=GREEN,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(
        name=f"{E.RECEIPT} 注文番号",
        value=f"```\n{receipt_number or '----'}\n```",
        inline=True,
    )
    e.add_field(
        name=f"{E.STORE} 店舗", value=f"{store_name or '—'}\n`{store_id}`", inline=True
    )
    e.add_field(name=f"{E.PIN} 受取方法", value=pickup_label or "—", inline=True)
    e.set_footer(text="ご利用ありがとうございます")
    if with_image:
        e.set_image(url="attachment://receipt.png")
    return e


# ============================================================
#  実績パネル（プライバシー重視 / docs/05 §5）
# ============================================================

def achievement(
    *,
    anon_code: str,
    username: str | None,
    list_price: int,
    subsidy_rate: float,
    user_amount: int,
    daily_count: int,
    store_name: str | None,
    receipt_number: str | None,
    pickup_label: str | None,
    fields: list[str],
) -> discord.Embed:
    e = discord.Embed(
        title=f"{E.OK} 注文が成立しました",
        color=GREEN,
        timestamp=datetime.now(timezone.utc),
    )
    if "anon_code" in fields:
        e.add_field(name=f"{E.USER} 利用者", value=f"`{anon_code}`", inline=True)
    if "username" in fields and username:
        e.add_field(name=f"{E.USER} ユーザー", value=username, inline=True)
    if "list_price" in fields:
        e.add_field(name=f"{E.YEN} 定価", value=yen(list_price), inline=True)
    if "subsidy_rate" in fields:
        e.add_field(name=f"{E.CHART} 負担率", value=f"{subsidy_rate:g}%", inline=True)
    if "user_amount" in fields:
        e.add_field(name=f"{E.YEN} お支払い", value=f"**{yen(user_amount)}**", inline=True)
    if "daily_count" in fields:
        e.add_field(name=f"{E.FRIES} 通算", value=f"本日 {daily_count} 件目", inline=True)
    if "store_name" in fields and store_name:
        e.add_field(name=f"{E.STORE} 店舗", value=store_name, inline=True)
    if "pickup_label" in fields and pickup_label:
        e.add_field(name=f"{E.PIN} 受取方法", value=pickup_label, inline=True)
    if "receipt_number" in fields and receipt_number:
        e.add_field(name=f"{E.RECEIPT} 注文番号", value=f"`{receipt_number}`", inline=True)
    return e


# ============================================================
#  共通
# ============================================================

def error(message: str, *, title: str | None = None) -> discord.Embed:
    return discord.Embed(
        title=title or f"{E.NG} エラー", description=message, color=RED
    )


def warn(message: str, *, title: str | None = None) -> discord.Embed:
    return discord.Embed(
        title=title or f"{E.WARN} ご確認ください", description=message, color=ORANGE
    )


def ok(message: str, *, title: str | None = None) -> discord.Embed:
    return discord.Embed(
        title=title or f"{E.OK} 完了", description=message, color=GREEN
    )


def info(message: str, *, title: str | None = None) -> discord.Embed:
    return discord.Embed(
        title=title or f"{E.INFO} お知らせ", description=message, color=BLUE
    )


def balance_card(*, balance: int, held: int, rate: float, total_orders: int) -> discord.Embed:
    e = discord.Embed(title=f"{E.WALLET} 残高", color=BLUE)
    e.add_field(name="利用可能", value=f"**{yen(balance)}**", inline=True)
    if held:
        e.add_field(name="処理中", value=yen(held), inline=True)
    e.add_field(name=f"{E.CHART} あなたの支払い率", value=f"**{rate:g}%**", inline=True)
    e.add_field(name=f"{E.FRIES} ご利用回数", value=f"{total_orders} 回", inline=True)
    return e
