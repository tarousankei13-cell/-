import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E
from utils.views import Paginator


class Orders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ── /orders ────────────────────────────────────────────────────────────

    @app_commands.command(name="orders", description="注文履歴を表示します")
    async def orders(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        order_list = await self.db.get_user_orders(interaction.user.id)
        if not order_list:
            await interaction.followup.send(
                embed=E.info("注文履歴なし", "まだ注文がありません。\n`/shop` で商品を購入してみましょう！"),
                ephemeral=True
            )
            return

        status_icons = {"pending": "⏳", "processing": "🔄", "completed": "✅", "cancelled": "❌"}
        status_labels = {"pending": "保留中", "processing": "処理中", "completed": "完了", "cancelled": "キャンセル"}

        PAGE = 8
        pages = []
        for i in range(0, len(order_list), PAGE):
            chunk = order_list[i:i+PAGE]
            embed = discord.Embed(
                title="📦  注文履歴",
                color=Config.COLOR_PRIMARY
            )
            for o in chunk:
                icon = status_icons.get(o["status"], "❓")
                label = status_labels.get(o["status"], o["status"])
                embed.add_field(
                    name=f"{icon} 注文 #{o['id']:05d}  [{label}]",
                    value=f"💰 {o['total_price']:,} {Config.CURRENCY_NAME}  |  🗓️ {o['created_at'][:10]}\n`/order <{o['id']}>` で詳細を確認",
                    inline=False
                )
            embed.set_footer(text=f"ページ {i//PAGE+1}/{(len(order_list)+PAGE-1)//PAGE} | 合計 {len(order_list)} 件")
            pages.append(embed)

        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
        else:
            view = Paginator(pages, interaction.user.id)
            await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)

    # ── /order ─────────────────────────────────────────────────────────────

    @app_commands.command(name="order", description="注文の詳細を表示します")
    @app_commands.describe(order_id="注文ID")
    async def order(self, interaction: discord.Interaction, order_id: int):
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.response.send_message(embed=E.error("注文が見つかりません"), ephemeral=True)
            return
        if order["user_id"] != interaction.user.id:
            is_admin = interaction.user.guild_permissions.administrator
            is_staff = any(r.name in (Config.ADMIN_ROLE, Config.STAFF_ROLE) for r in interaction.user.roles)
            if not (is_admin or is_staff):
                await interaction.response.send_message(embed=E.error("権限不足", "他のユーザーの注文は確認できません。"), ephemeral=True)
                return

        order_items = await self.db.get_order_items(order_id)
        embed = E.order_detail(order, order_items)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /order_cancel ──────────────────────────────────────────────────────

    @app_commands.command(name="order_cancel", description="保留中の注文をキャンセルします")
    @app_commands.describe(order_id="注文ID")
    async def order_cancel(self, interaction: discord.Interaction, order_id: int):
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.response.send_message(embed=E.error("注文が見つかりません"), ephemeral=True)
            return
        if order["user_id"] != interaction.user.id:
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        if order["status"] != "pending":
            await interaction.response.send_message(
                embed=E.error("キャンセルできません", "保留中の注文のみキャンセル可能です。"),
                ephemeral=True
            )
            return

        await self.db.update_order_status(order_id, "cancelled")
        await self.db.update_balance(interaction.user.id, order["total_price"])
        await self.db.add_transaction(interaction.user.id, order["total_price"], "refund", f"注文 #{order_id:05d} キャンセル返金")

        await interaction.response.send_message(
            embed=E.success(
                "注文をキャンセルしました",
                f"注文 **#{order_id:05d}** をキャンセルし、**{order['total_price']:,}** {Config.CURRENCY_NAME} を返金しました。"
            ),
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Orders(bot))
