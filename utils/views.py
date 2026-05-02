import discord
from typing import Optional, List
from config import Config
import utils.embeds as E


# ── Pagination helper ────────────────────────────────────────────────────────

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


# ── Shop home ────────────────────────────────────────────────────────────────

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

        PAGE = 5
        pages = []
        for i in range(0, max(1, len(products)), PAGE):
            chunk = products[i:i+PAGE]
            total_pages = max(1, (len(products) + PAGE - 1) // PAGE)
            pages.append(E.product_list(category, chunk, i // PAGE + 1, total_pages))

        view = ProductListView(products, category, interaction.user.id)
        await interaction.response.edit_message(embed=pages[0], view=view)


# ── Product list ─────────────────────────────────────────────────────────────

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
        embed = E.shop_home(categories)
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
        embed = E.product_detail(product, rating, reviews)
        view = ProductDetailView(product, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Product detail ───────────────────────────────────────────────────────────

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
        embed = E.product_list(category, products[:5])
        view = ProductListView(products, category, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Modals ───────────────────────────────────────────────────────────────────

class AddToCartModal(discord.ui.Modal, title="カートに追加"):
    quantity = discord.ui.TextInput(
        label="数量", placeholder="1", default="1", min_length=1, max_length=3
    )

    def __init__(self, product):
        super().__init__()
        self.product = product

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(E.error("無効な数量", "1以上の整数を入力してください。"), ephemeral=True)
            return
        db = interaction.client.db
        user = await db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(E.error("アクセス拒否", "ショップの利用が制限されています。"), ephemeral=True)
            return
        ok = await db.add_to_cart(interaction.user.id, self.product["id"], qty)
        if not ok:
            await interaction.response.send_message(E.error("追加失敗", "カートの上限に達しているか、数量が多すぎます。"), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=E.success("カートに追加しました！", f"**{self.product['name']}** × {qty} をカートに追加しました。\n`/cart` でカートを確認できます。"),
            ephemeral=True
        )


class BuyNowModal(discord.ui.Modal, title="今すぐ購入"):
    quantity = discord.ui.TextInput(
        label="数量", placeholder="1", default="1", min_length=1, max_length=3
    )
    notes = discord.ui.TextInput(
        label="備考（任意）", placeholder="配送メモなど", required=False, max_length=200, style=discord.TextStyle.paragraph
    )

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
            await interaction.response.send_message(embed=E.error("アクセス拒否", "ショップの利用が制限されています。"), ephemeral=True)
            return

        total = self.product["price"] * qty
        if user["balance"] < total:
            await interaction.response.send_message(
                embed=E.error("残高不足", f"必要: **{total:,}** {Config.CURRENCY_NAME}\n残高: **{user['balance']:,}** {Config.CURRENCY_NAME}"),
                ephemeral=True
            )
            return

        ok = await db.decrement_stock(self.product["id"], qty)
        if not ok:
            await interaction.response.send_message(embed=E.error("在庫不足", "在庫が不足しています。"), ephemeral=True)
            return

        await db.update_balance(interaction.user.id, -total)
        items = [{"product_id": self.product["id"], "name": self.product["name"], "price": self.product["price"], "quantity": qty}]
        order_id = await db.create_order(interaction.user.id, items, total, self.notes.value or "")
        await db.add_transaction(interaction.user.id, -total, "purchase", f"注文 #{order_id:05d}: {self.product['name']}")

        embed = E.order_confirm(order_id, items, total)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _notify_order(interaction.client, interaction.user, order_id, items, total)


class CheckoutModal(discord.ui.Modal, title="注文を確定する"):
    notes = discord.ui.TextInput(
        label="備考（任意）", placeholder="配送メモ、連絡事項など", required=False, max_length=300, style=discord.TextStyle.paragraph
    )

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

        total = await db.cart_total(interaction.user.id)
        if user["balance"] < total:
            await interaction.response.send_message(
                embed=E.error("残高不足", f"必要: **{total:,}** {Config.CURRENCY_NAME}\n残高: **{user['balance']:,}** {Config.CURRENCY_NAME}"),
                ephemeral=True
            )
            return

        for item in cart:
            if not item["in_stock"]:
                await interaction.response.send_message(
                    embed=E.error("在庫不足", f"**{item['name']}** の在庫が不足しています。カートを更新してください。"),
                    ephemeral=True
                )
                return

        for item in cart:
            ok = await db.decrement_stock(item["product_id"], item["quantity"])
            if not ok:
                await interaction.response.send_message(embed=E.error("在庫エラー", f"**{item['name']}** の処理中にエラーが発生しました。"), ephemeral=True)
                return

        await db.update_balance(interaction.user.id, -total)
        items = [{"product_id": it["product_id"], "name": it["name"], "price": it["price"], "quantity": it["quantity"]} for it in cart]
        order_id = await db.create_order(interaction.user.id, items, total, self.notes.value or "")
        await db.add_transaction(interaction.user.id, -total, "purchase", f"注文 #{order_id:05d} ({len(items)}点)")
        await db.clear_cart(interaction.user.id)

        embed = E.order_confirm(order_id, items, total)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _notify_order(interaction.client, interaction.user, order_id, items, total)


# ── Cart view ────────────────────────────────────────────────────────────────

class CartView(discord.ui.View):
    def __init__(self, items, total: int, author_id: int):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.has_items = bool(items)
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
        await interaction.response.edit_message(
            embed=E.success("カートをクリアしました", "カートが空になりました。"),
            view=None
        )

    @discord.ui.button(label="🛒 ショップへ", style=discord.ButtonStyle.secondary)
    async def shop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        db = interaction.client.db
        categories = await db.get_categories()
        embed = E.shop_home(categories)
        view = ShopHomeView(categories, interaction.client, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Admin product modal ──────────────────────────────────────────────────────

class AddProductModal(discord.ui.Modal, title="商品を追加"):
    name        = discord.ui.TextInput(label="商品名",  max_length=100)
    description = discord.ui.TextInput(label="説明",   style=discord.TextStyle.paragraph, max_length=500, required=False)
    price       = discord.ui.TextInput(label="価格（整数）", max_length=10)
    stock       = discord.ui.TextInput(label="在庫数（-1=無制限）", default="-1", max_length=6)
    image_url   = discord.ui.TextInput(label="画像URL（任意）", required=False, max_length=500)

    def __init__(self, category_id: int):
        super().__init__()
        self.category_id = category_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.price.value)
            stock = int(self.stock.value)
            if price < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(embed=E.error("入力エラー", "価格と在庫は整数で入力してください。"), ephemeral=True)
            return

        db = interaction.client.db
        pid = await db.add_product(
            self.category_id, self.name.value,
            self.description.value or "", price, stock,
            self.image_url.value or ""
        )
        await interaction.response.send_message(
            embed=E.success("商品を追加しました", f"商品ID: **#{pid}**\n**{self.name.value}** — {price:,} {Config.CURRENCY_NAME}"),
            ephemeral=True
        )


class EditProductModal(discord.ui.Modal, title="商品を編集"):
    name        = discord.ui.TextInput(label="商品名",  max_length=100)
    description = discord.ui.TextInput(label="説明",   style=discord.TextStyle.paragraph, max_length=500, required=False)
    price       = discord.ui.TextInput(label="価格",   max_length=10)
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
        await db.update_product(
            self.product["id"],
            name=self.name.value,
            description=self.description.value or "",
            price=price,
            stock=stock,
            image_url=self.image_url.value or ""
        )
        await interaction.response.send_message(
            embed=E.success("商品を更新しました", f"**{self.name.value}** の情報を更新しました。"),
            ephemeral=True
        )


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
        await interaction.response.send_message(
            embed=E.success("カテゴリーを追加しました", f"**{self.emoji.value} {self.name.value}** (ID: {cid})"),
            ephemeral=True
        )


class AnnounceModal(discord.ui.Modal, title="お知らせを送信"):
    title_field = discord.ui.TextInput(label="タイトル", max_length=100)
    body        = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, max_length=2000)

    async def on_submit(self, interaction: discord.Interaction):
        from config import Config as C
        channel_id = C.ANNOUNCE_CHANNEL
        if channel_id:
            channel = interaction.client.get_channel(channel_id)
        else:
            channel = interaction.channel

        embed = discord.Embed(
            title=f"📢  {self.title_field.value}",
            description=self.body.value,
            color=C.COLOR_SHOP
        )
        embed.set_footer(text=f"発信者: {interaction.user.display_name}")
        if channel:
            await channel.send(embed=embed)
            await interaction.response.send_message(embed=E.success("お知らせを送信しました"), ephemeral=True)
        else:
            await interaction.response.send_message(embed=E.error("チャンネルが見つかりません"), ephemeral=True)


# ── Ticket modal ─────────────────────────────────────────────────────────────

class TicketCreateModal(discord.ui.Modal, title="サポートチケットを作成"):
    subject     = discord.ui.TextInput(label="件名",   max_length=100)
    description = discord.ui.TextInput(label="詳細説明", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = interaction.client.db
        existing = await db.get_user_open_ticket(interaction.user.id)
        if existing:
            ch = interaction.guild.get_channel(existing["channel_id"])
            ch_mention = ch.mention if ch else f"#{existing['channel_id']}"
            await interaction.followup.send(
                embed=E.error("既存のチケット", f"既にオープン中のチケットがあります: {ch_mention}"),
                ephemeral=True
            )
            return

        guild = interaction.guild
        from config import Config as C
        category = guild.get_channel(C.TICKET_CATEGORY) if C.TICKET_CATEGORY else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user:   discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        staff_role = discord.utils.get(guild.roles, name=C.STAFF_ROLE)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name[:15]}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket | {self.subject.value} | {interaction.user}"
        )

        ticket_id = await db.create_ticket(interaction.user.id, channel.id, self.subject.value)

        embed = discord.Embed(
            title=f"🎫  チケット #{ticket_id:04d}",
            description=f"**件名:** {self.subject.value}\n\n**内容:**\n{self.description.value}",
            color=C.COLOR_INFO
        )
        embed.set_footer(text=f"ユーザー: {interaction.user} | チケットID: {ticket_id}")
        view = TicketControlView()
        msg = await channel.send(
            content=f"{interaction.user.mention}" + (f" | {staff_role.mention}" if staff_role else ""),
            embed=embed,
            view=view
        )
        await msg.pin()
        await interaction.followup.send(
            embed=E.success("チケットを作成しました", f"チャンネル: {channel.mention}"),
            ephemeral=True
        )


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 チケットを閉じる", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from config import Config as C
        db = interaction.client.db
        ticket = await db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(embed=E.error("チケットが見つかりません"), ephemeral=True)
            return

        is_owner = interaction.user.id == ticket["user_id"]
        is_staff = any(r.name in (C.STAFF_ROLE, C.ADMIN_ROLE) for r in interaction.user.roles)
        if not (is_owner or is_staff):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return

        await db.close_ticket(ticket["id"])
        embed = discord.Embed(
            title="🔒  チケットが閉じられました",
            description=f"クローズ実行者: {interaction.user.mention}",
            color=C.COLOR_ERROR
        )
        await interaction.response.send_message(embed=embed)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


# ── Order admin view ─────────────────────────────────────────────────────────

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
        await interaction.response.edit_message(
            embed=E.success("注文を完了にしました", f"注文 #{self.order['id']:05d} を完了に変更しました。"),
            view=self
        )
        user = interaction.client.get_user(self.order["user_id"])
        if user:
            try:
                await user.send(embed=E.success("注文が完了しました！", f"注文番号 **#{self.order['id']:05d}** が処理されました。"))
            except Exception:
                pass

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
            embed=E.success("注文をキャンセルしました", f"注文 #{self.order['id']:05d} をキャンセルし、{order['total_price']:,} {Config.CURRENCY_NAME} を返金しました。"),
            view=self
        )
        user = interaction.client.get_user(order["user_id"])
        if user:
            try:
                await user.send(embed=E.info("注文がキャンセルされました", f"注文 **#{order['id']:05d}** がキャンセルされ、**{order['total_price']:,}** {Config.CURRENCY_NAME} が返金されました。"))
            except Exception:
                pass


# ── Helper ───────────────────────────────────────────────────────────────────

async def _notify_order(client, user: discord.User, order_id: int, items, total: int):
    if not Config.ORDER_LOG_CHANNEL:
        return
    channel = client.get_channel(Config.ORDER_LOG_CHANNEL)
    if not channel:
        return
    embed = discord.Embed(
        title=f"🛒  新しい注文 #{order_id:05d}",
        color=Config.COLOR_INFO
    )
    embed.add_field(name="ユーザー", value=f"{user.mention} ({user})", inline=True)
    embed.add_field(name="合計金額", value=f"{total:,} {Config.CURRENCY_NAME}", inline=True)
    lines = [f"{it['name']} × {it['quantity']}" for it in items]
    embed.add_field(name="注文内容", value="\n".join(lines), inline=False)
    await channel.send(embed=embed)
