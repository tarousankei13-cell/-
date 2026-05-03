import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E


class AddKeyModal(discord.ui.Modal, title="デジタルキーを追加"):
    key_value = discord.ui.TextInput(
        label="キー / コード", max_length=500,
        placeholder="例: XXXXX-XXXXX-XXXXX"
    )

    def __init__(self, product_id: int, product_name: str):
        super().__init__()
        self.product_id = product_id
        self.product_name = product_name

    async def on_submit(self, interaction: discord.Interaction):
        db = interaction.client.db
        await db.add_digital_key(self.product_id, self.key_value.value.strip())
        stats = await db.get_key_stats(self.product_id)
        await interaction.response.send_message(
            embed=E.success(
                "キーを追加しました",
                f"**{self.product_name}**\n利用可能キー数: **{stats['available']}** / 合計: {stats['total']}"
            ),
            ephemeral=True
        )


class BulkKeyModal(discord.ui.Modal, title="デジタルキーを一括追加"):
    keys_text = discord.ui.TextInput(
        label="キー一覧（1行に1つ）",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        placeholder="KEY-001\nKEY-002\nKEY-003"
    )

    def __init__(self, product_id: int, product_name: str):
        super().__init__()
        self.product_id = product_id
        self.product_name = product_name

    async def on_submit(self, interaction: discord.Interaction):
        keys = [k.strip() for k in self.keys_text.value.splitlines() if k.strip()]
        if not keys:
            await interaction.response.send_message(embed=E.error("キーがありません"), ephemeral=True)
            return
        db = interaction.client.db
        count = await db.add_digital_keys_bulk(self.product_id, keys)
        stats = await db.get_key_stats(self.product_id)
        await interaction.response.send_message(
            embed=E.success(
                f"{count} 個のキーを追加しました",
                f"**{self.product_name}**\n利用可能キー数: **{stats['available']}** / 合計: {stats['total']}"
            ),
            ephemeral=True
        )


class Digital(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    def _is_admin(self, user: discord.Member) -> bool:
        return user.guild_permissions.administrator or any(r.name == Config.ADMIN_ROLE for r in user.roles)

    # ── Admin: manage keys ─────────────────────────────────────────────────

    @app_commands.command(name="keys_add", description="商品にデジタルキーを追加します（管理者専用）")
    @app_commands.describe(product_id="商品ID")
    async def keys_add(self, interaction: discord.Interaction, product_id: int):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await interaction.response.send_modal(AddKeyModal(product_id, p["name"]))

    @app_commands.command(name="keys_import", description="デジタルキーを一括インポートします（管理者専用）")
    @app_commands.describe(product_id="商品ID")
    async def keys_import(self, interaction: discord.Interaction, product_id: int):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await interaction.response.send_modal(BulkKeyModal(product_id, p["name"]))

    @app_commands.command(name="keys_status", description="デジタルキーの在庫状況を確認します（管理者専用）")
    @app_commands.describe(product_id="商品ID")
    async def keys_status(self, interaction: discord.Interaction, product_id: int):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        stats = await self.db.get_key_stats(product_id)
        embed = discord.Embed(
            title=f"🔑  デジタルキー状況 — {p['name']}",
            color=Config.COLOR_INFO
        )
        embed.add_field(name="利用可能", value=f"**{stats['available']}** 個", inline=True)
        embed.add_field(name="配布済み", value=f"{stats['delivered']} 個", inline=True)
        embed.add_field(name="合計登録", value=f"{stats['total']} 個", inline=True)
        if stats["available"] == 0:
            embed.set_footer(text="⚠️ キー在庫が切れています！")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── User: view received keys ───────────────────────────────────────────

    @app_commands.command(name="mykeys", description="受け取ったデジタルキーを確認します")
    async def mykeys(self, interaction: discord.Interaction):
        keys = await self.db.get_user_delivered_keys(interaction.user.id)
        if not keys:
            await interaction.response.send_message(
                embed=E.info("キーなし", "まだデジタルキーを受け取っていません。"),
                ephemeral=True
            )
            return

        embed = discord.Embed(title="🔑  受け取ったデジタルキー", color=Config.COLOR_PRIMARY)
        for k in keys[:10]:
            embed.add_field(
                name=f"📦 {k['product_name']}  ({k['delivered_at'][:10] if k['delivered_at'] else '—'})",
                value=f"```{k['key_value']}```",
                inline=False
            )
        embed.set_footer(text="Shop Bot • このメッセージは自分だけに表示されています")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Helper (called from purchase flow) ────────────────────────────────

    @staticmethod
    async def deliver_keys_for_order(client, user_id: int, order_id: int, items: list) -> list:
        """Deliver digital keys for all digital items in an order. Returns list of (product_name, key) tuples."""
        db = client.db
        delivered = []
        for item in items:
            product = await db.get_product(item["product_id"])
            if not product or not product["is_digital"]:
                continue
            for _ in range(item["quantity"]):
                key = await db.get_available_key(item["product_id"])
                if key:
                    await db.deliver_key(key["id"], user_id, order_id)
                    delivered.append((item["name"], key["key_value"]))
        return delivered


async def setup(bot):
    await bot.add_cog(Digital(bot))
