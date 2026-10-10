"""
声かけのDM

⚠️ **どのDMにも「もう送らない」を付ける。**
   止める手段が無いDMは迷惑でしかない。ボタン1つで切れるようにする。

⚠️ custom_id は永久に変えないこと。変えると、過去に送ったDMの
   ボタンが反応しなくなる（古いDMほど、止めたい人が押す）。
"""

from __future__ import annotations

import logging

import discord

import config
import emoji as E
from ui import embeds
from ui.gate import GuardedView

log = logging.getLogger("bot.ui.nudge")


class NudgeView(GuardedView):
    """声かけのDMに付けるボタン。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="このお知らせを止める", emoji="🔕",
        style=discord.ButtonStyle.secondary, custom_id="nudge:stop",
    )
    async def stop(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        from core import users as user_repo
        from services import outreach

        await interaction.response.defer(ephemeral=True, thinking=True)
        await user_repo.get_or_create(interaction.user.id)
        await outreach.set_notify(interaction.user.id, False)
        await interaction.followup.send(
            embed=embeds.ok(
                "これ以降、この種類のお知らせは送りません。\n"
                f"{E.INFO} ご注文やチャージの結果のお知らせは、"
                "これまでどおりお送りします。"
            ),
            ephemeral=True,
        )


def _footer(e: discord.Embed) -> discord.Embed:
    e.set_footer(text="不要な場合は下のボタンで止められます")
    return e


def welcome_embed(guild_name: str) -> discord.Embed:
    """
    ようこそ案内。

    ⚠️ やることを**3つだけ**に絞る。多いと読まれない。
    """
    e = discord.Embed(
        title=f"{E.PARTY} ようこそ",
        description=(
            f"**{guild_name}** へのご参加ありがとうございます。\n"
            "マクドナルドのご注文を、このBOTから承ります。"
        ),
        color=embeds.GREEN,
    )
    e.add_field(
        name="はじめかた（3ステップ）",
        value=(
            f"**1.** チャージパネルから残高を入れます\n"
            f"**2.** 注文パネルの「{E.BURGER} 注文する」を押します\n"
            f"**3.** お店と商品を選んで確定\n\n"
            f"{E.RECEIPT} 注文番号が届いたら、店頭でお伝えください。"
        ),
        inline=False,
    )
    e.add_field(
        name=f"{E.INFO} 困ったら",
        value="お問い合わせパネルから、担当者にご相談いただけます。",
        inline=False,
    )
    return _footer(e)


def cart_left_embed(store_id: str) -> discord.Embed:
    """
    カートを残したまま離れた方へ。

    ⚠️ 急かさない。「まだ残っています」とだけ伝える。
    """
    mins = int(config.CART_RESUME_MINUTES)
    e = discord.Embed(
        title=f"{E.CART} 途中のご注文が残っています",
        description=(
            "組み立て途中の内容をお預かりしています。\n"
            f"注文パネルから、**続きから**進められます。"
        ),
        color=embeds.BLUE,
    )
    e.add_field(
        name=f"{E.LOADING} お預かりできる時間",
        value=f"最後の操作から約 {mins} 分です",
        inline=False,
    )
    return _footer(e)


def charged_unused_embed(balance: int) -> discord.Embed:
    """
    チャージしたのに、まだ一度も注文していない方へ。

    ⚠️ 「使ってください」ではなく「いつでも使えます」と伝える。
       預けたお金が宙に浮いている不安を解くのが目的。
    """
    e = discord.Embed(
        title=f"{E.WALLET} 残高をご用意できています",
        description=(
            "チャージいただいた残高が、いつでもお使いいただけます。\n"
            "注文パネルから、お店と商品を選ぶだけでご注文いただけます。"
        ),
        color=embeds.GREEN,
    )
    e.add_field(name="ご利用可能な残高",
                value=f"**{embeds.yen(balance)}**", inline=True)
    e.add_field(
        name=f"{E.INFO} 進め方が分からないとき",
        value="お問い合わせパネルからご相談ください。お手伝いします。",
        inline=False,
    )
    return _footer(e)


def idle_balance_embed(balance: int, days: int) -> discord.Embed:
    """
    残高があるのに、しばらく使っていない方へ。

    ⚠️ 「久しぶりですね」と責めない。残高が残っていることだけを伝える。
    """
    e = discord.Embed(
        title=f"{E.WALLET} 残高が残っています",
        description=(
            f"前回のご注文から {days} 日ほど経っています。\n"
            "残高はそのままお預かりしていますので、いつでもお使いいただけます。"
        ),
        color=embeds.BLUE,
    )
    e.add_field(name="ご利用可能な残高",
                value=f"**{embeds.yen(balance)}**", inline=True)
    return _footer(e)


# main.py の setup_hook が add_view() で復元する
PERSISTENT_VIEWS = [NudgeView]
