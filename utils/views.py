import discord
from typing import Optional, List
from config import Config
import utils.embeds as E


# ── Pagination helper ─────────────────────────────────────────────────────────

class Paginator(discord.ui.View):
    def __init__(self, pages: List[discord.Embed], author_id: int):
        super().__init__(timeout=120)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("他の人の操作は行えません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Shop home ─────────────────────────────────────────────────────────────────

class ShopHomeView(discord.ui.View):
    def __init__(self, categories, bot, author_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.author_id = author_id
        if categories:
            options = [
                discord.SelectOption(label=cat["name"], value=str(cat["id"]), emoji=cat["emoji"], description=cat["description"][:50] or None)
                for cat in categories
            ]
            self.add_item(CategorySelect(options))

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("他の人の操作は行えません。", ephemeral=True)
            return False
        return True


class CategorySelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="カテゴリーを選択してください…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        cat_id = int(self.values[0])
        db = interaction.client.db
        category = await db.get_category(cat_id)
        products = await db.get_products(category_id=cat_id, available_only=True)

        # Build flash sale dict
        active_sales = await db.get_active_flash_sales()
        flash_map = {s["product_id"]: s for s in active_sales}

        PAGE = 5
        pages = []
        for i in range(0, max(1, len(products)), PAGE):
            chunk = products[i:i+PAGE]
            total_pages = max(1, (len(products) + PAGE - 1) // PAGE)
            pages.append(E.product_list(category, chunk, i // PAGE + 1, total_pages, flash_map))

        view = ProductListView(products, category, interaction.user.id)
        await interaction.response.edit_message(embed=pages[0], view=view)


# ── Product list ──────────────────────────────────────────────────────────────

class ProductListView(discord.ui.View):
    def __init__(self, products, category, author_id: int):
        super().__init__(timeout=180)
        self.products = products
        self.category = category
        self.author_id = author_id
        if products:
            options = [
                discord.SelectOption(
                    label=f"#{p['id']} {p['name']}"[:100],
                    value=str(p["id"]),
                    description=f"{p['price']:,} {Config.CURRENCY_NAME}"[:100]
                )
                for p in products[:25]
            ]
            self.add_item(ProductSelect(options))

    @discord.ui.button(label="🔙 カテゴリー一覧", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("他の人の操作は行えません。", ephemeral=True)
            return
        db = interaction.client.db
        categories = await db.get_categories()
        active_sales = await db.get_active_flash_sales()
        embed = E.shop_home(categories, active_sales)
        view = ShopHomeView(categories, interaction.client, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


class ProductSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="商品を選択して詳細を見る…", options=options)

    async def callback(self, interaction: discord.Interaction):
        product_id = int(self.values[0])
        db = interaction.client.db
        product = await db.get_product(product_id)
        if not product:
            await interaction.response.send_message("商品が見つかりませんでした。", ephemeral=True)
            return
        rating = await db.get_product_rating(product_id)
        reviews = await db.get_product_reviews(product_id)
        sale = await db.get_product_flash_sale(product_id)
        embed = E.product_detail(product, rating, reviews, sale)
        view = ProductDetailView(product, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Product detail ────────────────────────────────────────────────────────────

class ProductDetailView(discord.ui.View):
    def __init__(self, product, author_id: int):
        super().__init__(timeout=180)
        self.product = product
        self.author_id = author_id
        can_buy = product["is_available"] and (product["stock"] == -1 or product["stock"] > 0)
        self.add_to_cart_btn.disabled = not can_buy
        self.buy_now_btn.disabled = not can_buy

    async def _check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("他の人の操作は行えません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🛒 カートに追加", style=discord.ButtonStyle.primary)
    async def add_to_cart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(AddToCartModal(self.product))

    @discord.ui.button(label="⚡ 今すぐ購入", style=discord.ButtonStyle.success)
    async def buy_now_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(BuyNowModal(self.product))

    @discord.ui.button(label="👀 ウォッチ", style=discord.ButtonStyle.secondary)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        p = self.product
        if p["stock"] == 0 or (p["stock"] != -1 and p["stock"] <= 0):
            await db.add_to_watchlist(interaction.user.id, p["id"])
            await interaction.response.send_message(embed=E.success("ウォッチリストに追加しました", "入荷時にDMでお知らせします。"), ephemeral=True)
        else:
            await interaction.response.send_message(embed=E.info("在庫あり", "この商品は現在在庫があります。"), ephemeral=True)

    @discord.ui.button(label="⭐ レビューを見る", style=discord.ButtonStyle.secondary)
    async def reviews_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        reviews = await db.get_product_reviews(self.product["id"])
        embed = E.reviews_embed(self.product, reviews)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔙 一覧に戻る", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        db = interaction.client.db
        category = await db.get_category(self.product["category_id"])
        products = await db.get_products(category_id=self.product["category_id"], available_only=True)
        active_sales = await db.get_active_flash_sales()
        flash_map = {s["product_id"]: s for s in active_sales}
        embed = E.product_list(category, products[:5], 1, max(1, (len(products) + 4) // 5), flash_map)
        view = ProductListView(products, category, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Cart view ─────────────────────────────────────────────────────────────────

class CartView(discord.ui.View):
    def __init__(self, items, total: int, author_id: int):
        super().__init__(timeout=180)
        self.author_id = author_id
        if not items:
            self.checkout_btn.disabled = True
            self.clear_btn.disabled = True

    async def _check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("他の人の操作は行えません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ 注文確定", style=discord.ButtonStyle.success)
    async def checkout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(CheckoutModal())

    @discord.ui.button(label="🗑️ カートを空にする", style=discord.ButtonStyle.danger)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        db = interaction.client.db
        await db.clear_cart(interaction.user.id)
        await interaction.response.edit_message(embed=E.success("カートをクリアしました"), view=None)

    @discord.ui.button(label="🛒 ショップへ", style=discord.ButtonStyle.secondary)
    async def shop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        db = interaction.client.db
        categories = await db.get_categories()
        active_sales = await db.get_active_flash_sales()
        embed = E.shop_home(categories, active_sales)
        view = ShopHomeView(categories, interaction.client, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Checkout confirmation view ────────────────────────────────────────────────

class CheckoutConfirmView(discord.ui.View):
    def __init__(self, items, total: int, original: int, coupon_result: dict, notes: str, author_id: int):
        super().__init__(timeout=120)
        self.items = items
        self.total = total
        self.original = original
        self.coupon_result = coupon_result
        self.notes = notes
        self.author_id = author_id

    async def _check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("他の人の操作は行えません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ 注文を確定する", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return

        db = interaction.client.db
        user = await db.get_user(interaction.user.id)
        if user["balance"] < self.total:
            await interaction.response.edit_message(
                embed=E.error("残高不足", f"残高が変動しました。\n現在の残高: **{user['balance']:,}** {Config.CURRENCY_NAME}"),
                view=None
            )
            return

        # Final stock check
        for item in self.items:
            p = await db.get_product(item["product_id"])
            if not p or (p["stock"] != -1 and p["stock"] < item["quantity"]):
                await interaction.response.edit_message(
                    embed=E.error("在庫不足", f"**{item['name']}** の在庫が変動しました。"),
                    view=None
                )
                return

        # Process order
        for item in self.items:
            await db.decrement_stock(item["product_id"], item["quantity"])

        coupon_id = self.coupon_result["coupon"]["id"] if self.coupon_result and self.coupon_result.get("valid") else None
        discount = self.coupon_result["discount"] if self.coupon_result and self.coupon_result.get("valid") else 0

        await db.update_balance(interaction.user.id, -self.total)
        order_id = await db.create_order(
            interaction.user.id, self.items, self.total, self.notes,
            original_price=self.original, coupon_id=coupon_id
        )
        await db.add_transaction(interaction.user.id, -self.total, "purchase", f"注文 #{order_id:05d} ({len(self.items)}点)")
        if coupon_id:
            await db.use_coupon(coupon_id, interaction.user.id, order_id, discount)
        await db.clear_cart(interaction.user.id)

        # Digital key delivery
        from cogs.digital import Digital
        delivered_keys = await Digital.deliver_keys_for_order(interaction.client, interaction.user.id, order_id, self.items)

        embed = E.order_confirm(order_id, self.items, self.total, discount)
        if delivered_keys:
            key_lines = [f"**{name}**\n```{key}```" for name, key in delivered_keys]
            embed.add_field(name="🔑 デジタルキー", value="\n".join(key_lines), inline=False)
        await interaction.response.edit_message(embed=embed, view=None)

        # Order log
        await _notify_order(interaction.client, interaction.user, order_id, self.items, self.total)

        # Post-purchase: achievements, rank update
        old_vip = user["vip_tier"]
        new_achievements = await db.check_and_grant_achievements(interaction.user.id)
        new_tier = await db.update_user_vip_tier(interaction.user.id)

        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member and new_tier:
                from cogs.ranks import Ranks
                changed = await Ranks.apply_rank_role(interaction.guild, member, new_tier, old_vip, db)
                if changed:
                    await _notify_rank_up(interaction.client, interaction.user, new_tier)

        if new_achievements:
            await _notify_achievements(interaction.client, interaction.user, new_achievements)

    @discord.ui.button(label="✏️ 修正する", style=discord.ButtonStyle.secondary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        db = interaction.client.db
        items = await db.get_cart(interaction.user.id)
        total = await db.cart_total(interaction.user.id)
        embed = E.cart_embed(items, total)
        view = CartView(items, total, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Modals ────────────────────────────────────────────────────────────────────

class AddToCartModal(discord.ui.Modal, title="カートに追加"):
    quantity = discord.ui.TextInput(label="数量", placeholder="1", default="1", min_length=1, max_length=3)

    def __init__(self, product):
        super().__init__()
        self.product = product

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(embed=E.error("無効な数量"), ephemeral=True)
            return
        db = interaction.client.db
        user = await db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return
        ok = await db.add_to_cart(interaction.user.id, self.product["id"], qty)
        if not ok:
            await interaction.response.send_message(embed=E.error("追加失敗", "カートの上限か数量超過です。"), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=E.success("カートに追加しました！", f"**{self.product['name']}** × {qty}\n`/cart view` でカートを確認"),
            ephemeral=True
        )


class BuyNowModal(discord.ui.Modal, title="今すぐ購入"):
    quantity   = discord.ui.TextInput(label="数量",             default="1", max_length=3)
    coupon     = discord.ui.TextInput(label="クーポンコード（任意）", required=False, max_length=20)
    notes      = discord.ui.TextInput(label="備考（任意）", required=False, max_length=200, style=discord.TextStyle.paragraph)

    def __init__(self, product):
        super().__init__()
        self.product = product

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(embed=E.error("無効な数量"), ephemeral=True)
            return

        db = interaction.client.db
        user = await db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        # Flash sale price check
        sale = await db.get_product_flash_sale(self.product["id"])
        price = sale["sale_price"] if sale else self.product["price"]
        subtotal = price * qty

        # Coupon
        coupon_result = None
        if self.coupon.value.strip():
            coupon_result = await db.validate_coupon(self.coupon.value.strip(), interaction.user.id, subtotal)
            if not coupon_result["valid"]:
                await interaction.response.send_message(embed=E.error("クーポンエラー", coupon_result["reason"]), ephemeral=True)
                return

        final = coupon_result["final"] if coupon_result and coupon_result["valid"] else subtotal

        if user["balance"] < final:
            await interaction.response.send_message(
                embed=E.error("残高不足", f"必要: **{final:,}** {Config.CURRENCY_NAME}\n残高: **{user['balance']:,}** {Config.CURRENCY_NAME}"),
                ephemeral=True
            )
            return

        ok = await db.decrement_stock(self.product["id"], qty)
        if not ok:
            await interaction.response.send_message(embed=E.error("在庫不足"), ephemeral=True)
            return

        await db.update_balance(interaction.user.id, -final)
        coupon_id = coupon_result["coupon"]["id"] if coupon_result and coupon_result["valid"] else None
        discount = coupon_result["discount"] if coupon_result and coupon_result["valid"] else 0
        items = [{"product_id": self.product["id"], "name": self.product["name"], "price": price, "quantity": qty}]
        order_id = await db.create_order(
            interaction.user.id, items, final, self.notes.value or "",
            original_price=subtotal, coupon_id=coupon_id
        )
        await db.add_transaction(interaction.user.id, -final, "purchase", f"注文 #{order_id:05d}: {self.product['name']}")
        if coupon_id:
            await db.use_coupon(coupon_id, interaction.user.id, order_id, discount)

        # Digital key delivery
        from cogs.digital import Digital
        delivered_keys = await Digital.deliver_keys_for_order(interaction.client, interaction.user.id, order_id, items)

        embed = E.order_confirm(order_id, items, final, discount)
        if delivered_keys:
            key_lines = [f"**{name}**\n```{key}```" for name, key in delivered_keys]
            embed.add_field(name="🔑 デジタルキー", value="\n".join(key_lines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _notify_order(interaction.client, interaction.user, order_id, items, final)

        # Post-purchase effects
        old_vip = user["vip_tier"]
        new_achievements = await db.check_and_grant_achievements(interaction.user.id)
        new_tier = await db.update_user_vip_tier(interaction.user.id)
        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member and new_tier:
                from cogs.ranks import Ranks
                changed = await Ranks.apply_rank_role(interaction.guild, member, new_tier, old_vip, db)
                if changed:
                    await _notify_rank_up(interaction.client, interaction.user, new_tier)
        if new_achievements:
            await _notify_achievements(interaction.client, interaction.user, new_achievements)


class CheckoutModal(discord.ui.Modal, title="注文を確定する"):
    coupon = discord.ui.TextInput(label="クーポンコード（任意）", required=False, max_length=20, placeholder="持っている場合は入力")
    notes  = discord.ui.TextInput(label="備考（任意）", required=False, max_length=300, style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        db = interaction.client.db
        user = await db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        cart = await db.get_cart(interaction.user.id)
        if not cart:
            await interaction.response.send_message(embed=E.error("カートが空です"), ephemeral=True)
            return

        subtotal = await db.cart_total(interaction.user.id)

        # Coupon validation
        coupon_result = None
        if self.coupon.value.strip():
            coupon_result = await db.validate_coupon(self.coupon.value.strip(), interaction.user.id, subtotal)
            if not coupon_result["valid"]:
                await interaction.response.send_message(embed=E.error("クーポンエラー", coupon_result["reason"]), ephemeral=True)
                return

        final = coupon_result["final"] if coupon_result and coupon_result["valid"] else subtotal

        if user["balance"] < final:
            await interaction.response.send_message(
                embed=E.error("残高不足", f"必要: **{final:,}** {Config.CURRENCY_NAME}\n残高: **{user['balance']:,}** {Config.CURRENCY_NAME}"),
                ephemeral=True
            )
            return

        for item in cart:
            if not item["in_stock"]:
                await interaction.response.send_message(embed=E.error("在庫不足", f"**{item['name']}** の在庫が不足しています。"), ephemeral=True)
                return

        items = [{"product_id": it["product_id"], "name": it["name"], "price": it["price"], "quantity": it["quantity"]} for it in cart]
        embed = E.checkout_preview(items, subtotal, coupon_result)
        view = CheckoutConfirmView(items, final, subtotal, coupon_result, self.notes.value or "", interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ── Admin modals ──────────────────────────────────────────────────────────────

class AddProductModal(discord.ui.Modal, title="商品を追加"):
    name        = discord.ui.TextInput(label="商品名",              max_length=100)
    description = discord.ui.TextInput(label="説明",  style=discord.TextStyle.paragraph, max_length=500, required=False)
    price       = discord.ui.TextInput(label="価格",               max_length=10)
    stock       = discord.ui.TextInput(label="在庫数（-1=無制限）", default="-1", max_length=6)
    image_url   = discord.ui.TextInput(label="画像URL（任意）",    required=False, max_length=500)

    def __init__(self, category_id: int):
        super().__init__()
        self.category_id = category_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.price.value)
            stock = int(self.stock.value)
        except ValueError:
            await interaction.response.send_message(embed=E.error("入力エラー", "価格・在庫は整数で入力してください。"), ephemeral=True)
            return
        db = interaction.client.db
        pid = await db.add_product(self.category_id, self.name.value, self.description.value or "", price, stock, self.image_url.value or "")
        await interaction.response.send_message(
            embed=E.success("商品を追加しました", f"商品ID: **#{pid}**\n**{self.name.value}** — {price:,} {Config.CURRENCY_NAME}"),
            ephemeral=True
        )


class EditProductModal(discord.ui.Modal, title="商品を編集"):
    name        = discord.ui.TextInput(label="商品名", max_length=100)
    description = discord.ui.TextInput(label="説明", style=discord.TextStyle.paragraph, max_length=500, required=False)
    price       = discord.ui.TextInput(label="価格", max_length=10)
    stock       = discord.ui.TextInput(label="在庫数（-1=無制限）", max_length=6)
    image_url   = discord.ui.TextInput(label="画像URL", required=False, max_length=500)

    def __init__(self, product):
        super().__init__()
        self.product = product
        self.name.default = product["name"]
        self.description.default = product["description"]
        self.price.default = str(product["price"])
        self.stock.default = str(product["stock"])
        self.image_url.default = product["image_url"]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.price.value)
            stock = int(self.stock.value)
        except ValueError:
            await interaction.response.send_message(embed=E.error("入力エラー"), ephemeral=True)
            return
        db = interaction.client.db
        await db.update_product(self.product["id"], name=self.name.value, description=self.description.value or "", price=price, stock=stock, image_url=self.image_url.value or "")
        await interaction.response.send_message(embed=E.success("商品を更新しました", f"**{self.name.value}** の情報を更新しました。"), ephemeral=True)


class AddCategoryModal(discord.ui.Modal, title="カテゴリーを追加"):
    name        = discord.ui.TextInput(label="カテゴリー名", max_length=50)
    description = discord.ui.TextInput(label="説明", max_length=200, required=False)
    emoji       = discord.ui.TextInput(label="絵文字", max_length=10, default="🛒")

    async def on_submit(self, interaction: discord.Interaction):
        db = interaction.client.db
        try:
            cid = await db.add_category(self.name.value, self.description.value or "", self.emoji.value or "🛒")
        except Exception:
            await interaction.response.send_message(embed=E.error("追加失敗", "同名のカテゴリーが既に存在します。"), ephemeral=True)
            return
        await interaction.response.send_message(embed=E.success("カテゴリーを追加しました", f"**{self.emoji.value} {self.name.value}** (ID: {cid})"), ephemeral=True)


class FlashSaleModal(discord.ui.Modal, title="フラッシュセールを作成"):
    discount_pct = discord.ui.TextInput(label="割引率（%）",           max_length=3, placeholder="例: 30")
    duration_hrs = discord.ui.TextInput(label="開催時間（時間）",       max_length=4, placeholder="例: 2")
    notify       = discord.ui.TextInput(label="アナウンス（yes/no）",   default="yes", max_length=3)

    def __init__(self, product):
        super().__init__()
        self.product = product

    async def on_submit(self, interaction: discord.Interaction):
        try:
            pct  = int(self.discount_pct.value)
            hrs  = float(self.duration_hrs.value)
            if not (1 <= pct <= 99) or hrs <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(embed=E.error("入力エラー", "割引率は1〜99%、時間は正の数で入力してください。"), ephemeral=True)
            return

        from datetime import timedelta
        now = __import__("datetime").datetime.now()
        start = now.isoformat()
        end   = (now + timedelta(hours=hrs)).isoformat()

        db = interaction.client.db
        sale_id = await db.create_flash_sale(self.product["id"], pct, self.product["price"], start, end, Config.FLASH_SALE_CHANNEL)
        sale_price = int(self.product["price"] * (1 - pct / 100))
        await db.update_product(self.product["id"], price=sale_price)

        await interaction.response.send_message(
            embed=E.success(
                "フラッシュセール開始！",
                f"**{self.product['name']}**\n{self.product['price']:,} → **{sale_price:,}** {Config.CURRENCY_NAME} (`{pct}%OFF`)\n終了: {end[:16].replace('T', ' ')}"
            ),
            ephemeral=True
        )

        # Announce
        if self.notify.value.lower().strip() in ("yes", "y"):
            channel_id = Config.FLASH_SALE_CHANNEL or Config.ANNOUNCE_CHANNEL
            if channel_id:
                channel = interaction.client.get_channel(channel_id)
                if channel:
                    product_full = await db.get_product(self.product["id"])
                    sale = await db.get_product_flash_sale(self.product["id"])
                    if sale:
                        await channel.send(embed=E.flash_sale_announce(sale, product_full))

        # Notify watchlist
        from cogs.watchlist import Watchlist
        await Watchlist.notify_watchers(interaction.client, self.product["id"], self.product["name"])


class AnnounceModal(discord.ui.Modal, title="お知らせを送信"):
    title_field = discord.ui.TextInput(label="タイトル", max_length=100)
    body        = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, max_length=2000)

    async def on_submit(self, interaction: discord.Interaction):
        channel_id = Config.ANNOUNCE_CHANNEL
        channel = interaction.client.get_channel(channel_id) if channel_id else interaction.channel
        embed = discord.Embed(title=f"📢  {self.title_field.value}", description=self.body.value, color=Config.COLOR_SHOP)
        embed.set_footer(text=f"発信者: {interaction.user.display_name}")
        if channel:
            await channel.send(embed=embed)
            await interaction.response.send_message(embed=E.success("お知らせを送信しました"), ephemeral=True)
        else:
            await interaction.response.send_message(embed=E.error("チャンネルが見つかりません"), ephemeral=True)


# ── Ticket modals ─────────────────────────────────────────────────────────────

class TicketCreateModal(discord.ui.Modal, title="サポートチケットを作成"):
    subject     = discord.ui.TextInput(label="件名",   max_length=100)
    description = discord.ui.TextInput(label="詳細説明", style=discord.TextStyle.paragraph, max_length=1000)
    priority    = discord.ui.TextInput(label="優先度 (normal / high)", default="normal", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        priority = self.priority.value.lower().strip()
        if priority not in ("normal", "high"):
            priority = "normal"

        db = interaction.client.db
        existing = await db.get_user_open_ticket(interaction.user.id)
        if existing:
            ch = interaction.guild.get_channel(existing["channel_id"])
            ch_mention = ch.mention if ch else f"#{existing['channel_id']}"
            await interaction.followup.send(embed=E.error("既存のチケット", f"オープン中のチケット: {ch_mention}"), ephemeral=True)
            return

        guild = interaction.guild
        category = guild.get_channel(Config.TICKET_CATEGORY) if Config.TICKET_CATEGORY else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user:   discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        staff_role = discord.utils.get(guild.roles, name=Config.STAFF_ROLE)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        priority_icon = "🔴" if priority == "high" else "⚪"
        channel = await guild.create_text_channel(
            name=f"{priority_icon}ticket-{interaction.user.name[:15]}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket | {self.subject.value} | {interaction.user}"
        )

        ticket_id = await db.create_ticket(interaction.user.id, channel.id, self.subject.value, priority)
        embed = discord.Embed(
            title=f"🎫  チケット #{ticket_id:04d}",
            description=f"**件名:** {self.subject.value}\n\n**内容:**\n{self.description.value}",
            color=Config.COLOR_ERROR if priority == "high" else Config.COLOR_INFO
        )
        embed.add_field(name="優先度", value=f"{priority_icon} {'高' if priority == 'high' else '通常'}", inline=True)
        embed.set_footer(text=f"ユーザー: {interaction.user} | チケットID: {ticket_id}")
        view = TicketControlView()
        msg = await channel.send(
            content=f"{interaction.user.mention}" + (f" | {staff_role.mention}" if staff_role else ""),
            embed=embed, view=view
        )
        await msg.pin()
        await interaction.followup.send(embed=E.success("チケットを作成しました", f"チャンネル: {channel.mention}"), ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 チケットを閉じる", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        ticket = await db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(embed=E.error("チケットが見つかりません"), ephemeral=True)
            return
        is_owner = interaction.user.id == ticket["user_id"]
        is_staff = any(r.name in (Config.STAFF_ROLE, Config.ADMIN_ROLE) for r in interaction.user.roles)
        if not (is_owner or is_staff or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        await db.close_ticket(ticket["id"])
        embed = discord.Embed(title="🔒  チケットが閉じられました", description=f"クローズ実行者: {interaction.user.mention}", color=Config.COLOR_ERROR)
        await interaction.response.send_message(embed=embed)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


# ── Order admin view ──────────────────────────────────────────────────────────

class OrderAdminView(discord.ui.View):
    def __init__(self, order, author_id: int):
        super().__init__(timeout=120)
        self.order = order
        self.author_id = author_id
        if order["status"] != "pending":
            self.complete_btn.disabled = True
            self.cancel_btn.disabled = True

    async def _check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("権限がありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ 完了にする", style=discord.ButtonStyle.success)
    async def complete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        db = interaction.client.db
        await db.update_order_status(self.order["id"], "completed")
        button.disabled = True
        self.cancel_btn.disabled = True
        await interaction.response.edit_message(embed=E.success("注文を完了にしました"), view=self)
        user = interaction.client.get_user(self.order["user_id"])
        if user:
            try:
                await user.send(embed=E.success("注文が完了しました！", f"注文番号 **#{self.order['id']:05d}** が処理されました。ありがとうございます！"))
            except Exception:
                pass
        # Check achievements after order completion
        await db.check_and_grant_achievements(self.order["user_id"])
        new_tier = await db.update_user_vip_tier(self.order["user_id"])
        if new_tier and user:
            await _notify_rank_up(interaction.client, user, new_tier)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        db = interaction.client.db
        order = await db.get_order(self.order["id"])
        await db.update_order_status(self.order["id"], "cancelled")
        await db.update_balance(order["user_id"], order["total_price"])
        await db.add_transaction(order["user_id"], order["total_price"], "refund", f"注文 #{order['id']:05d} キャンセル返金")
        button.disabled = True
        self.complete_btn.disabled = True
        await interaction.response.edit_message(
            embed=E.success("注文をキャンセルしました", f"**{order['total_price']:,}** {Config.CURRENCY_NAME} を返金しました。"),
            view=self
        )
        user = interaction.client.get_user(order["user_id"])
        if user:
            try:
                await user.send(embed=E.info("注文がキャンセルされました", f"注文 **#{order['id']:05d}** がキャンセルされ、**{order['total_price']:,}** {Config.CURRENCY_NAME} が返金されました。"))
            except Exception:
                pass


# ── Notify helpers ────────────────────────────────────────────────────────────

async def _notify_order(client, user: discord.User, order_id: int, items, total: int):
    if not Config.ORDER_LOG_CHANNEL:
        return
    channel = client.get_channel(Config.ORDER_LOG_CHANNEL)
    if not channel:
        return
    embed = discord.Embed(title=f"🛒  新しい注文 #{order_id:05d}", color=Config.COLOR_INFO)
    embed.add_field(name="ユーザー",  value=f"{user.mention} ({user})", inline=True)
    embed.add_field(name="合計金額", value=f"{total:,} {Config.CURRENCY_NAME}", inline=True)
    lines = [f"{it['name']} × {it['quantity']}" for it in items]
    embed.add_field(name="注文内容", value="\n".join(lines), inline=False)
    await channel.send(embed=embed)


async def _notify_rank_up(client, user: discord.User, new_tier):
    channel_id = Config.RANK_UP_CHANNEL or Config.ANNOUNCE_CHANNEL
    embed = discord.Embed(
        title=f"🎉  ランクアップ！",
        description=f"{user.mention} が **{new_tier['emoji']} {new_tier['name']}** にランクアップしました！",
        color=new_tier["color"]
    )
    embed.add_field(name="✨ 新しい特典", value=new_tier["perks"] or "—", inline=False)
    if channel_id:
        channel = client.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed)
    try:
        await user.send(embed=embed)
    except Exception:
        pass


async def _notify_achievements(client, user: discord.User, achievements):
    channel_id = Config.ACHIEVEMENT_CHANNEL or Config.ANNOUNCE_CHANNEL
    for ach in achievements:
        embed = E.new_achievement_embed(ach)
        if channel_id:
            channel = client.get_channel(channel_id)
            if channel:
                await channel.send(content=user.mention, embed=embed)
        try:
            await user.send(embed=embed)
        except Exception:
            pass
