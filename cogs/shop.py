import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E
from utils.views import (
    ShopHomeView, ProductListView, ProductDetailView,
    CartView, AddToCartModal, BuyNowModal, CheckoutModal
)


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ── /shop ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="shop", description="ショップを開きます")
    async def shop(self, interaction: discord.Interaction):
        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否", "ショップの利用が制限されています。"), ephemeral=True)
            return
        categories = await self.db.get_categories()
        embed = E.shop_home(categories)
        view = ShopHomeView(categories, self.bot, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /product ──────────────────────────────────────────────────────────────

    @app_commands.command(name="product", description="商品の詳細を表示します")
    @app_commands.describe(product_id="商品ID")
    async def product(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません", f"商品ID {product_id} は存在しません。"), ephemeral=True)
            return
        rating = await self.db.get_product_rating(product_id)
        reviews = await self.db.get_product_reviews(product_id)
        embed = E.product_detail(p, rating, reviews)
        view = ProductDetailView(p, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /search ───────────────────────────────────────────────────────────────

    @app_commands.command(name="search", description="商品を検索します")
    @app_commands.describe(query="検索キーワード")
    async def search(self, interaction: discord.Interaction, query: str):
        if len(query) < 2:
            await interaction.response.send_message(embed=E.error("検索ワードが短すぎます", "2文字以上で入力してください。"), ephemeral=True)
            return
        products = await self.db.search_products(query)
        embed = E.search_results(query, products)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /cart ─────────────────────────────────────────────────────────────────

    cart_group = app_commands.Group(name="cart", description="カート操作")

    @cart_group.command(name="view", description="カートを表示します")
    async def cart_view(self, interaction: discord.Interaction):
        items = await self.db.get_cart(interaction.user.id)
        total = await self.db.cart_total(interaction.user.id)
        embed = E.cart_embed(items, total)
        view = CartView(items, total, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @cart_group.command(name="add", description="商品をカートに追加します")
    @app_commands.describe(product_id="商品ID", quantity="数量（デフォルト: 1）")
    async def cart_add(self, interaction: discord.Interaction, product_id: int, quantity: int = 1):
        if quantity <= 0 or quantity > Config.MAX_CART_QUANTITY:
            await interaction.response.send_message(embed=E.error("無効な数量"), ephemeral=True)
            return

        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        p = await self.db.get_product(product_id)
        if not p or not p["is_available"] or (p["stock"] != -1 and p["stock"] <= 0):
            await interaction.response.send_message(embed=E.error("商品が利用できません"), ephemeral=True)
            return

        ok = await self.db.add_to_cart(interaction.user.id, product_id, quantity)
        if not ok:
            await interaction.response.send_message(embed=E.error("追加失敗", "カートの上限に達しているか、数量が多すぎます。"), ephemeral=True)
            return

        await interaction.response.send_message(
            embed=E.success("カートに追加しました", f"**{p['name']}** × {quantity}  {p['price'] * quantity:,} {Config.CURRENCY_NAME}"),
            ephemeral=True
        )

    @cart_group.command(name="remove", description="商品をカートから削除します")
    @app_commands.describe(product_id="商品ID")
    async def cart_remove(self, interaction: discord.Interaction, product_id: int):
        await self.db.remove_from_cart(interaction.user.id, product_id)
        await interaction.response.send_message(embed=E.success("カートから削除しました"), ephemeral=True)

    @cart_group.command(name="clear", description="カートを空にします")
    async def cart_clear(self, interaction: discord.Interaction):
        await self.db.clear_cart(interaction.user.id)
        await interaction.response.send_message(embed=E.success("カートをクリアしました"), ephemeral=True)

    # ── /checkout ─────────────────────────────────────────────────────────────

    @app_commands.command(name="checkout", description="カートの商品を注文します")
    async def checkout(self, interaction: discord.Interaction):
        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        cart = await self.db.get_cart(interaction.user.id)
        if not cart:
            await interaction.response.send_message(embed=E.error("カートが空です", "`/cart add` で商品を追加してください。"), ephemeral=True)
            return

        total = await self.db.cart_total(interaction.user.id)
        if user["balance"] < total:
            await interaction.response.send_message(
                embed=E.error("残高不足", f"必要: **{total:,}** {Config.CURRENCY_NAME}\n残高: **{user['balance']:,}** {Config.CURRENCY_NAME}"),
                ephemeral=True
            )
            return

        await interaction.response.send_modal(CheckoutModal())

    # ── /buy ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="buy", description="商品を今すぐ購入します")
    @app_commands.describe(product_id="商品ID", quantity="数量（デフォルト: 1）")
    async def buy(self, interaction: discord.Interaction, product_id: int, quantity: int = 1):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await interaction.response.send_modal(BuyNowModal(p))


async def setup(bot):
    await bot.add_cog(Shop(bot))
