import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E


class Watchlist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ── /watch ─────────────────────────────────────────────────────────────

    @app_commands.command(name="watch", description="商品の入荷通知を受け取ります")
    @app_commands.describe(product_id="商品ID")
    async def watch(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return

        if p["stock"] != 0 and (p["stock"] == -1 or p["stock"] > 0):
            await interaction.response.send_message(
                embed=E.info("在庫あり", f"**{p['name']}** は現在在庫があります！`/shop` で購入できます。"),
                ephemeral=True
            )
            return

        await self.db.add_to_watchlist(interaction.user.id, product_id)
        await interaction.response.send_message(
            embed=E.success(
                "ウォッチリストに追加しました",
                f"**{p['name']}** の入荷時にDMでお知らせします。"
            ),
            ephemeral=True
        )

    @app_commands.command(name="unwatch", description="入荷通知を解除します")
    @app_commands.describe(product_id="商品ID")
    async def unwatch(self, interaction: discord.Interaction, product_id: int):
        await self.db.remove_from_watchlist(interaction.user.id, product_id)
        await interaction.response.send_message(
            embed=E.success("ウォッチリストから削除しました"),
            ephemeral=True
        )

    @app_commands.command(name="watchlist", description="ウォッチリストを表示します")
    async def watchlist_view(self, interaction: discord.Interaction):
        items = await self.db.get_user_watchlist(interaction.user.id)
        if not items:
            await interaction.response.send_message(
                embed=E.info("ウォッチリストは空です", "`/watch <商品ID>` でウォッチできます。"),
                ephemeral=True
            )
            return

        embed = discord.Embed(title="👀  ウォッチリスト", color=Config.COLOR_INFO)
        for item in items:
            stock_str = "在庫あり ✅" if (item["stock"] == -1 or item["stock"] > 0) else "在庫なし ❌"
            embed.add_field(
                name=f"#{item['product_id']}  {item['name']}",
                value=f"{item['price']:,} {Config.CURRENCY_NAME}  |  {stock_str}  |  追加: {item['added_at'][:10]}",
                inline=False
            )
        embed.set_footer(text="Shop Bot • 入荷時にDMでお知らせします")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Notify watchers (called by admin stock update or tasks) ────────────

    @staticmethod
    async def notify_watchers(client, product_id: int, product_name: str):
        db = client.db
        watchers = await db.get_product_watchers(product_id)
        if not watchers:
            return

        embed = discord.Embed(
            title="🔔  入荷通知！",
            description=f"ウォッチリストの **{product_name}** が入荷しました！\nお早めにご購入ください。",
            color=Config.COLOR_SUCCESS
        )
        embed.set_footer(text="Shop Bot • /shop で確認")

        notified = 0
        for user_id in watchers:
            user = client.get_user(user_id)
            if user:
                try:
                    await user.send(embed=embed)
                    notified += 1
                except Exception:
                    pass

        await db.mark_watchers_notified(product_id)
        return notified


async def setup(bot):
    await bot.add_cog(Watchlist(bot))
