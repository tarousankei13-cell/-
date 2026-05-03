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

    # ── /shop ──────────────────────────────────────────────────────────────

    @app_commands.command(name="shop", description="ショップを開きます")
    async def shop(self, interaction: discord.Interaction):
        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否", "ショップの利用が制限されています。"), ephemeral=True)
            return
        categories = await self.db.get_categories()
        active_sales = await self.db.get_active_flash_sales()
        embed = E.shop_home(categories, active_sales)
        view = ShopHomeView(categories, self.bot, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /product ───────────────────────────────────────────────────────────

    @app_commands.command(name="product", description="商品の詳細を表示します")
    @app_commands.describe(product_id="商品ID")
    async def product(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません", f"商品ID {product_id} は存在しません。"), ephemeral=True)
            return
        rating  = await self.db.get_product_rating(product_id)
        reviews = await self.db.get_product_reviews(product_id)
        sale    = await self.db.get_product_flash_sale(product_id)
        embed   = E.product_detail(p, rating, reviews, sale)
        view    = ProductDetailView(p, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /search ────────────────────────────────────────────────────────────

    @app_commands.command(name="search", description="商品を検索します（名前・説明・タグ）")
    @app_commands.describe(query="検索キーワード")
    async def search(self, interaction: discord.Interaction, query: str):
        if len(query) < 2:
            await interaction.response.send_message(embed=E.error("検索ワードが短すぎます", "2文字以上で入力してください。"), ephemeral=True)
            return
        products = await self.db.search_products(query)
        active_sales = await self.db.get_active_flash_sales()
        flash_map = {s["product_id"]: s for s in active_sales}
        embed = E.search_results(query, products, flash_map)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /sales ─────────────────────────────────────────────────────────────

    @app_commands.command(name="sales", description="開催中のセールを表示します")
    async def sales(self, interaction: discord.Interaction):
        active_sales = await self.db.get_active_flash_sales()
        if not active_sales:
            await interaction.response.send_message(embed=E.info("セールなし", "現在開催中のセールはありません。"), ephemeral=True)
            return
        embed = discord.Embed(title="⚡  開催中のフラッシュセール", color=Config.COLOR_FLASH)
        for s in active_sales:
            embed.add_field(
                name=f"🔥 {s['product_name']}  `{s['discount_percent']}%OFF`",
                value=f"~~{s['original_price']:,}~~ → **{s['sale_price']:,}** {Config.CURRENCY_NAME}\n⏰ 終了: {s['end_time'][:16].replace('T', ' ')}",
                inline=False
            )
        embed.set_footer(text="Shop Bot • /shop で購入！")
        await interaction.response.send_message(embed=embed)

    # ── /cart ──────────────────────────────────────────────────────────────

    cart_group = app_commands.Group(name="cart", description="カート操作")

    @cart_group.command(name="view", description="カートを表示します")
    async def cart_view(self, interaction: discord.Interaction):
        items = await self.db.get_cart(interaction.user.id)
        total = await self.db.cart_total(interaction.user.id)
        embed = E.cart_embed(items, total)
        view  = CartView(items, total, interaction.user.id)
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
            await interaction.response.send_message(embed=E.error("追加失敗", "カートの上限か数量超過です。"), ephemeral=True)
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

    # ── /checkout ──────────────────────────────────────────────────────────

    @app_commands.command(name="checkout", description="カートの商品を注文します（クーポン使用可）")
    async def checkout(self, interaction: discord.Interaction):
        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return
        cart = await self.db.get_cart(interaction.user.id)
        if not cart:
            await interaction.response.send_message(embed=E.error("カートが空です", "`/cart add` で商品を追加してください。"), ephemeral=True)
            return
        await interaction.response.send_modal(CheckoutModal())

    # ── /buy ───────────────────────────────────────────────────────────────

    @app_commands.command(name="buy", description="商品を今すぐ購入します（クーポン使用可）")
    @app_commands.describe(product_id="商品ID", quantity="数量（デフォルト: 1）")
    async def buy(self, interaction: discord.Interaction, product_id: int, quantity: int = 1):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await interaction.response.send_modal(BuyNowModal(p))

    # ── /referral ──────────────────────────────────────────────────────────

    @app_commands.command(name="referral", description="紹介コードを確認・使用します")
    @app_commands.describe(code="紹介コード（持っている場合は入力）")
    async def referral(self, interaction: discord.Interaction, code: str = None):
        user = await self.db.get_user(interaction.user.id)

        if code:
            if user.get("referred_by"):
                await interaction.response.send_message(embed=E.error("既に使用済み", "紹介コードは一度のみ使用できます。"), ephemeral=True)
                return
            referrer = await self.db.get_user_by_referral_code(code)
            if not referrer:
                await interaction.response.send_message(embed=E.error("無効なコード", "紹介コードが見つかりません。"), ephemeral=True)
                return
            if referrer["user_id"] == interaction.user.id:
                await interaction.response.send_message(embed=E.error("自分のコードは使用できません"), ephemeral=True)
                return
            ok = await self.db.process_referral(referrer["user_id"], interaction.user.id)
            if not ok:
                await interaction.response.send_message(embed=E.error("適用失敗"), ephemeral=True)
                return
            # Referred user bonus
            await self.db.update_balance(interaction.user.id, Config.REFERRAL_REWARD)
            await self.db.add_transaction(interaction.user.id, Config.REFERRAL_REWARD, "referral", "紹介ボーナス（被紹介者）")
            await interaction.response.send_message(
                embed=E.success("紹介コードを適用しました！", f"**+{Config.REFERRAL_REWARD:,}** {Config.CURRENCY_NAME} を受け取りました！"),
                ephemeral=True
            )
            # Check achievements
            await self.db.check_and_grant_achievements(referrer["user_id"])
            return

        # Show own referral code
        embed = discord.Embed(title="🤝  紹介プログラム", color=Config.COLOR_INFO)
        embed.add_field(name="あなたの紹介コード", value=f"```{user.get('referral_code', '—')}```", inline=False)
        embed.add_field(name="報酬", value=f"紹介者: **+{Config.REFERRAL_REWARD:,}** {Config.CURRENCY_NAME}\n被紹介者: **+{Config.REFERRAL_REWARD:,}** {Config.CURRENCY_NAME}", inline=False)
        embed.set_footer(text="Shop Bot • 友達に紹介して両方ボーナスをもらおう！")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /achievements ──────────────────────────────────────────────────────

    @app_commands.command(name="achievements", description="実績一覧を確認します")
    @app_commands.describe(user="確認するユーザー（省略時：自分）")
    async def achievements(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        all_ach  = await self.db.get_achievements(include_secret=False)
        user_ach = await self.db.get_user_achievements(target.id)
        embed = E.achievements_embed(all_ach, user_ach)
        embed.title = f"🏆  {target.display_name} の実績"
        await interaction.response.send_message(embed=embed, ephemeral=(user is None))


async def setup(bot):
    await bot.add_cog(Shop(bot))
