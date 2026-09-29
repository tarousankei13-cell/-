"""User-facing commands for McDonald's Concierge Bot."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from models import EmbedColor, next_rank, resolve_rank
from views import (
    FavoritesView,
    format_rate_breakdown,
)

logger = logging.getLogger("bot.user")


class UserCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    @app_commands.command(
        name="profile", description="あなたのプロフィールを表示します"
    )
    async def profile(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        db = self.db
        balance = await db.get_balance(uid)
        points = await db.get_points(uid)
        completed = await db.get_user_completed_count(uid)
        total_orders = await db.get_user_order_count(uid)
        savings = await db.get_user_savings(uid)
        rank = resolve_rank(completed)
        nxt = next_rank(completed)
        br = await db.resolve_rate(uid)

        embed = discord.Embed(
            title=f"{interaction.user.display_name} のプロフィール",
            color=EmbedColor.PRIMARY,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="残高", value=f"¥{balance:,}", inline=True)
        embed.add_field(name="ポイント", value=f"{points:,}pt", inline=True)
        embed.add_field(
            name="ランク", value=f"{rank.emoji} {rank.name}", inline=True
        )
        embed.add_field(name="完了注文", value=f"{completed:,}回", inline=True)
        embed.add_field(name="総注文", value=f"{total_orders:,}回", inline=True)
        embed.add_field(name="節約総額", value=f"¥{savings:,}", inline=True)
        embed.add_field(
            name="現在の割引", value=format_rate_breakdown(br), inline=False
        )
        if nxt:
            remain = nxt.threshold - completed
            embed.add_field(
                name="次のランク",
                value=(
                    f"{nxt.emoji} {nxt.name} まであと **{remain}回**\n"
                    f"達成で +{nxt.bonus}% OFF"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="次のランク", value="最高ランクに到達しています。", inline=False
            )
        embed.set_footer(text="McDonald's Concierge")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="coupon", description="クーポンを適用します")
    @app_commands.describe(code="クーポンコード")
    async def coupon(
        self, interaction: discord.Interaction, code: str
    ) -> None:
        db = self.db
        code = code.strip().upper()
        coupon = await db.get_coupon(code)
        if not coupon:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="クーポンエラー",
                    description="そのクーポンコードは存在しません。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return
        if not await db.is_coupon_usable(code, interaction.user.id):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="クーポンエラー",
                    description=(
                        "このクーポンは使用できません。\n"
                        "（期限切れ・使用上限に達している可能性があります）"
                    ),
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return
        await db.set_active_coupon(interaction.user.id, code)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="クーポン適用",
                description=(
                    f"クーポン **{code}** を適用しました。\n"
                    f"追加割引: **+{coupon['bonus']}%**\n\n"
                    "次回の注文で自動的に使用されます。"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="points", description="ポイントを確認・交換します")
    async def points(self, interaction: discord.Interaction) -> None:
        from views import BalanceDetailView, build_balance_embed

        db = self.db
        embed = await build_balance_embed(db, interaction.user)
        await interaction.response.send_message(
            embed=embed,
            view=BalanceDetailView(interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(
        name="favorites", description="お気に入りから再注文します"
    )
    async def favorites(self, interaction: discord.Interaction) -> None:
        db = self.db
        favs = await db.get_favorites(interaction.user.id)
        if not favs:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="お気に入り",
                    description=(
                        "お気に入りは登録されていません。\n"
                        "注文完了後に「お気に入りに保存」から登録できます。"
                    ),
                    color=EmbedColor.DARK,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="お気に入り",
                description="再注文する項目を選択してください。",
                color=EmbedColor.PRIMARY,
            ),
            view=FavoritesView(interaction.user.id, favs),
            ephemeral=True,
        )

    @app_commands.command(
        name="notify", description="DM通知のON/OFFを切り替えます"
    )
    @app_commands.describe(enabled="通知を受け取る")
    async def notify(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.db.set_notify(interaction.user.id, enabled)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="通知設定",
                description=f"DM通知を **{'ON' if enabled else 'OFF'}** にしました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UserCog(bot))
