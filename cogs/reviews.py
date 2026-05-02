import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E


class Reviews(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ── /review ────────────────────────────────────────────────────────────

    @app_commands.command(name="review", description="商品にレビューを投稿します")
    @app_commands.describe(
        product_id="商品ID",
        rating="評価（1〜5）",
        comment="コメント（任意）"
    )
    @app_commands.choices(rating=[
        app_commands.Choice(name="⭐ 1 — とても悪い",   value=1),
        app_commands.Choice(name="⭐⭐ 2 — 悪い",        value=2),
        app_commands.Choice(name="⭐⭐⭐ 3 — 普通",       value=3),
        app_commands.Choice(name="⭐⭐⭐⭐ 4 — 良い",      value=4),
        app_commands.Choice(name="⭐⭐⭐⭐⭐ 5 — 素晴らしい", value=5),
    ])
    async def review(self, interaction: discord.Interaction, product_id: int, rating: int, comment: str = ""):
        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        product = await self.db.get_product(product_id)
        if not product:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return

        # Verify purchase
        orders = await self.db.get_user_orders(interaction.user.id)
        purchased = False
        for o in orders:
            if o["status"] == "completed":
                items = await self.db.get_order_items(o["id"])
                if any(it["product_id"] == product_id for it in items):
                    purchased = True
                    break

        if not purchased:
            await interaction.response.send_message(
                embed=E.error("購入履歴なし", "この商品を購入した後にレビューを投稿できます。"),
                ephemeral=True
            )
            return

        existing = await self.db.get_user_review(interaction.user.id, product_id)
        if existing:
            await interaction.response.send_message(
                embed=E.error("レビュー済み", "この商品には既にレビューを投稿しています。"),
                ephemeral=True
            )
            return

        ok = await self.db.add_review(interaction.user.id, product_id, rating, comment)
        if not ok:
            await interaction.response.send_message(embed=E.error("レビュー投稿に失敗しました"), ephemeral=True)
            return

        stars = "⭐" * rating + "☆" * (5 - rating)
        await interaction.response.send_message(
            embed=E.success(
                "レビューを投稿しました！",
                f"**{product['name']}**\n{stars}\n{comment or '*(コメントなし)*'}"
            )
        )

    # ── /reviews ───────────────────────────────────────────────────────────

    @app_commands.command(name="reviews", description="商品のレビューを見ます")
    @app_commands.describe(product_id="商品ID")
    async def reviews(self, interaction: discord.Interaction, product_id: int):
        product = await self.db.get_product(product_id)
        if not product:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        review_list = await self.db.get_product_reviews(product_id)
        embed = E.reviews_embed(product, review_list)
        await interaction.response.send_message(embed=embed)

    # ── Admin: delete review ───────────────────────────────────────────────

    @app_commands.command(name="review_delete", description="レビューを削除します（管理者専用）")
    @app_commands.describe(review_id="レビューID")
    async def review_delete(self, interaction: discord.Interaction, review_id: int):
        if not (interaction.user.guild_permissions.administrator or any(r.name == Config.ADMIN_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        await self.db.delete_review(review_id)
        await interaction.response.send_message(embed=E.success("レビューを削除しました"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Reviews(bot))
