import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E


class GiftModal(discord.ui.Modal, title="プレゼントを送る"):
    quantity = discord.ui.TextInput(label="数量", default="1", max_length=3)
    message  = discord.ui.TextInput(
        label="メッセージ（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        placeholder="相手に伝えたいことを書いてください"
    )

    def __init__(self, product, recipient: discord.Member):
        super().__init__()
        self.product = product
        self.recipient = recipient

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity.value)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(embed=E.error("無効な数量"), ephemeral=True)
            return

        db = interaction.client.db
        sender = await db.get_user(interaction.user.id)
        if sender["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        total = self.product["price"] * qty

        # Check flash sale
        sale = await db.get_product_flash_sale(self.product["id"])
        if sale:
            total = sale["sale_price"] * qty

        if sender["balance"] < total:
            await interaction.response.send_message(
                embed=E.error("残高不足", f"必要: **{total:,}** {Config.CURRENCY_NAME}\n残高: **{sender['balance']:,}** {Config.CURRENCY_NAME}"),
                ephemeral=True
            )
            return

        ok = await db.decrement_stock(self.product["id"], qty)
        if not ok:
            await interaction.response.send_message(embed=E.error("在庫不足"), ephemeral=True)
            return

        await db.update_balance(interaction.user.id, -total)
        items = [{"product_id": self.product["id"], "name": self.product["name"], "price": self.product["price"], "quantity": qty}]
        order_id = await db.create_order(
            interaction.user.id, items, total, f"ギフト → {self.recipient}",
            recipient_id=self.recipient.id, is_gift=True
        )
        await db.add_transaction(interaction.user.id, -total, "gift_sent", f"ギフト #{order_id:05d} → {self.recipient}")

        # Sender confirmation
        confirm_embed = discord.Embed(
            title="🎁  ギフトを送りました！",
            description=f"{self.recipient.mention} に **{self.product['name']}** ×{qty} を送りました！",
            color=Config.COLOR_SUCCESS
        )
        confirm_embed.add_field(name="合計金額", value=f"{total:,} {Config.CURRENCY_NAME}", inline=True)
        confirm_embed.add_field(name="注文番号",  value=f"#{order_id:05d}", inline=True)
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)

        # Deliver digital keys if applicable
        from cogs.digital import Digital
        delivered_keys = await Digital.deliver_keys_for_order(interaction.client, self.recipient.id, order_id, items)

        # Recipient DM
        try:
            gift_embed = discord.Embed(
                title="🎁  プレゼントが届きました！",
                description=f"**{interaction.user.display_name}** からプレゼントが届きました！",
                color=Config.COLOR_SUCCESS
            )
            gift_embed.add_field(name="商品",   value=f"{self.product['name']} × {qty}", inline=True)
            gift_embed.add_field(name="注文番号", value=f"#{order_id:05d}", inline=True)
            if self.message.value:
                gift_embed.add_field(name="💌 メッセージ", value=self.message.value, inline=False)
            if self.product["image_url"]:
                gift_embed.set_thumbnail(url=self.product["image_url"])

            if delivered_keys:
                key_lines = [f"**{name}**\n```{key}```" for name, key in delivered_keys]
                gift_embed.add_field(name="🔑 デジタルキー", value="\n".join(key_lines), inline=False)

            await self.recipient.send(embed=gift_embed)
        except Exception:
            pass

        # Check achievements for sender
        await db.check_and_grant_achievements(interaction.user.id)

        # Rank update
        old_vip = sender["vip_tier"]
        new_tier = await db.update_user_vip_tier(interaction.user.id)
        if new_tier and interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                from cogs.ranks import Ranks
                await Ranks.apply_rank_role(interaction.guild, member, new_tier, old_vip, db)


class Gifts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    @app_commands.command(name="gift", description="商品を別のユーザーにプレゼントします")
    @app_commands.describe(product_id="商品ID", user="プレゼントする相手")
    async def gift(self, interaction: discord.Interaction, product_id: int, user: discord.Member):
        if user.id == interaction.user.id:
            await interaction.response.send_message(embed=E.error("自分自身へのギフトはできません"), ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message(embed=E.error("Botへのギフトはできません"), ephemeral=True)
            return

        sender = await self.db.get_user(interaction.user.id)
        if sender["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        p = await self.db.get_product(product_id)
        if not p or not p["is_available"]:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return

        await interaction.response.send_modal(GiftModal(p, user))

    @app_commands.command(name="gifts_received", description="受け取ったギフトの履歴を確認します")
    async def gifts_received(self, interaction: discord.Interaction):
        orders = await self.db.get_user_orders(interaction.user.id)
        gifts = [o for o in orders if o["is_gift"] and o["recipient_id"] == interaction.user.id]

        if not gifts:
            await interaction.response.send_message(
                embed=E.info("受け取ったギフトなし", "まだギフトを受け取っていません。"),
                ephemeral=True
            )
            return

        embed = discord.Embed(title="🎁  受け取ったギフト", color=Config.COLOR_SUCCESS)
        for g in gifts[:10]:
            items = await self.db.get_order_items(g["id"])
            item_names = ", ".join(f"{i['product_name']}×{i['quantity']}" for i in items)
            embed.add_field(
                name=f"#{g['id']:05d}  {g['created_at'][:10]}",
                value=f"{item_names}\n送り主: <@{g['user_id']}>",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Gifts(bot))
