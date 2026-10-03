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

    # ── お支払い ──
    # ⚠️ 「○% OFF」という書き方はしない。その数字は運営の負担率
    #    そのものなので、いくら持ち出しているかが分かってしまう。
    #    定価と、実際に払う額だけを出す。
    if user_rate < 100:
        e.add_field(
            name="💴 お支払いについて",
            value=(
                f"定価の **{user_rate:g}%** のお支払いで注文できます\n"
                f"　例）定価 ¥590 → お支払い **¥{math.ceil(590 * user_rate / 100):,}**"
            ),
            inline=False,
        )
    low = int(settings.get("order_min", 0) or 0)
    if low > 0:
        e.add_field(
            name=f"{E.INFO} ご注文の条件",
            value=f"ご注文は **{yen(low)}**（定価）以上から承っております。",
            inline=False,
        )

    e.set_footer(text="残高が足りないときは、チャージパネルからチャージしてください")
    return e


def charge_panel() -> discord.Embed:
    cmin = int(settings.get("charge_min", 100))
    cmax = int(settings.get("charge_max", 50_000))
    from core import limits

    rate = limits.charge_rate()
    desc = "Kyash の送金リンクで残高をチャージできます。"
    if rate > 100:
        example = limits.credited_for(1000)
        desc += (
            f"\n\n{E.PARTY} **いまなら {rate}% 増量中**\n"
            f"　例）{yen(1000)} の送金で **{yen(example)}** ぶんの残高になります"
        )
    e = discord.Embed(title=f"{E.YEN} 残高チャージ", description=desc, color=BLUE)
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
    if limits.first_charge_gate() and limits.first_charge_min() > 0:
        e.add_field(
            name=f"{E.INFO} はじめてご注文の方へ",
            value=(
                f"はじめてのご注文には、累計 **{yen(limits.first_charge_min())}** 以上の"
                "チャージが必要です。\n"
                "2回目以降のご注文では、この条件はありません。"
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
    # ⚠️ 「負担 40% 適用」のような、運営がいくら持ち出しているかが
    #    分かる書き方はしない。利用者が知るべきなのは、定価と
    #    自分がいくら払うかだけ。
    e.add_field(
        name=f"{E.YEN} お支払い",
        value=f"**{yen(quote.user_amount)}**\n{quote.describe()}",
        inline=True,
    )
    e.add_field(
        name=f"{E.WALLET} 残高",
        value=f"{yen(balance)} → **{yen(max(after, 0))}**",
        inline=True,
    )
    if quote.capped:
        e.add_field(
            name=f"{E.INFO} お支払いについて",
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
        value=f"**{yen(user_amount)}**（定価 {yen(list_price)}）",
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


def store_unavailable(store_name: str, store_id: str, av) -> discord.Embed:
    """
    その店舗がいま注文を受け付けていないことを伝える。

    商品を選び終えてから断られるのが一番つらいので、
    店舗を選んだ時点でここまで説明する。
    """
    e = discord.Embed(
        title=f"{E.WARN} {av.title or 'ただいま注文できません'}",
        description=av.reason or "この店舗はただいま注文を受け付けていません。",
        color=ORANGE,
    )
    e.add_field(
        name=f"{E.STORE} 店舗",
        value=f"{store_name or '—'}\n`{store_id}`",
        inline=True,
    )
    if av.windows:
        e.add_field(
            name=f"{E.SYNC} 注文できる時間",
            value=av.window_text(),
            inline=True,
        )
    if av.hint:
        e.add_field(name=f"{E.INFO} ご案内", value=av.hint, inline=False)
    if av.methods:
        from services.mcd.protocol import PICKUP_LABEL

        e.add_field(
            name=f"{E.PIN} この店舗の受け取り方法",
            value="・" + "\n・".join(
                PICKUP_LABEL.get(m, m) for m in av.methods
            ),
            inline=False,
        )
    e.set_footer(text="別の店舗を選ぶか、時間をおいてお試しください")
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


def invite_panel() -> discord.Embed:
    """紹介プログラムの常設パネル。"""
    from core import invite as inv

    if not inv.enabled():
        return discord.Embed(
            title=f"{E.CHARGE} 紹介プログラムのご案内",
            description=(
                "ただいま開催しておりません。\n"
                "次回の開催までお待ちください。"
            ),
            color=GREY,
        )

    every = inv.reward_every()
    amount = inv.reward_amount()
    invitee = inv.invitee_amount()
    low = inv.min_order()

    if every <= 1:
        bonus = (
            f"招待された方がご注文を完了されるごとに、"
            f"紹介者様へ **{yen(amount)}** を残高に加算いたします。"
        )
    else:
        bonus = (
            f"招待した方が **{every}名様** ご注文を完了されるごとに、"
            f"紹介者様へ **{yen(amount)}** を残高に加算いたします。"
        )
    if invitee:
        bonus += f"\n招待された方ご本人にも **{yen(invitee)}** 差し上げます。"

    e = discord.Embed(
        title="紹介プログラムの案内",
        description="お友達を紹介いただき、双方で残高を積み上げましょう。",
        color=GREEN,
    )
    e.add_field(name=f"{E.GIFT} 紹介の特典", value=bonus, inline=False)

    steps = [
        f"❶ 「{E.CHARGE} 招待リンクを発行」でご自身専用のリンクを作成"
        "（1サーバーにつき1個）",
        "❷ そのリンクをお友達に共有（参加時に自動でコード適用）",
        f"❸ 手動でコードを入力される方は「{E.TICKET} プロモコード入力」より貼り付け",
        f"❹ お友達が DM の受取ボタンを押し、ご注文"
        + (f"（定価 {yen(low)}以上）" if low else "")
        + "を完了",
    ]
    if every <= 1:
        steps.append(f"❺ 1名様ごとに **{yen(amount)}** をあなたへ")
    else:
        steps.append(
            f"❺ {every}名様の完了で **{yen(amount)}** をあなたへ"
            f"（以降 {every}名ごとに繰り返し発火）"
        )
    e.add_field(name="利用の流れ", value="\n".join(steps), inline=False)

    conds = []
    if inv.min_account_days():
        conds.append(
            f"Discord アカウント作成から **{inv.min_account_days()}日以上**"
        )
    if inv.min_member_hours():
        conds.append(
            f"サーバー参加から **{inv.min_member_hours()}時間以上**（手動入力時）"
        )
    conds.append("ご自身の招待コードはご利用いただけません")
    e.add_field(
        name="利用条件（被招待者様）", value="・" + "\n・".join(conds), inline=False
    )

    limit = inv.max_per_user()
    foot = "押した先の画面は、あなたにしか見えません。"
    if limit:
        foot += f" お一人あたり {limit} 名まで対象です。"
    e.set_footer(text=foot)
    return e


def balance_change(
    *,
    display_name: str | None,
    anon_code: str,
    amount: int,
    balance: int,
    reason: str,
    fields: list[str],
    total_orders: int = 0,
) -> discord.Embed:
    """
    残高の増減を、誰でも見える形で出す。

    ⚠️ 何を出すかは管理者が選ぶ（/config balance_panel）。
       既定では残高を出さない。いくら持っているかは
       知られたくない人が多いため。
    """
    plus = amount >= 0
    who = (
        display_name if ("name" in fields and display_name) else f"`{anon_code}`"
    )
    e = discord.Embed(
        title=f"{E.CHARGE if plus else E.BURGER} {'チャージ' if plus else 'ご注文'}",
        color=GREEN if plus else BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name=f"{E.USER} ご利用者", value=who, inline=True)
    if "amount" in fields:
        # 符号は ¥ の外に出す。「¥-480」は読みにくい。
        e.add_field(
            name=f"{E.YEN} 増減",
            value=f"**{'+' if plus else '-'}{yen(abs(amount))}**",
            inline=True,
        )
    if "balance" in fields:
        e.add_field(name=f"{E.WALLET} 残高", value=f"**{yen(balance)}**", inline=True)
    if "reason" in fields and reason:
        e.add_field(name=f"{E.NOTE} 内容", value=reason, inline=False)
    if "orders" in fields and total_orders:
        e.add_field(name=f"{E.FRIES} ご利用回数",
                    value=f"通算 **{total_orders}** 回目", inline=True)
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
    # ⚠️ 負担率はここに出さない。公開パネルは誰でも見られるので、
    #    運営がいくら持ち出しているかが分かってしまう。
    #    管理者が見たいときは /config subsidy と管理パネルにある。
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
