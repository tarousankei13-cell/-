import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from config import Config
import utils.embeds as E
from utils.views import (
    AddProductModal, EditProductModal, AddCategoryModal,
    AnnounceModal, OrderAdminView
)


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        role = discord.utils.get(interaction.user.roles, name=Config.ADMIN_ROLE)
        if role:
            return True
        await interaction.response.send_message(embed=E.error("権限不足", "管理者権限が必要です。"), ephemeral=True)
        return False
    return app_commands.check(predicate)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    admin = app_commands.Group(name="admin", description="管理者コマンド")

    # ── Stats ──────────────────────────────────────────────────────────────

    @admin.command(name="stats", description="ショップの統計を表示します")
    @is_admin()
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        stats = await self.db.get_shop_stats()
        await interaction.followup.send(embed=E.stats_embed(stats), ephemeral=True)

    # ── Announce ───────────────────────────────────────────────────────────

    @admin.command(name="announce", description="ショップからお知らせを送信します")
    @is_admin()
    async def announce(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AnnounceModal())

    # ── Category commands ──────────────────────────────────────────────────

    category_group = app_commands.Group(name="category", description="カテゴリー管理", parent=None)

    @admin.command(name="category_add", description="カテゴリーを追加します")
    @is_admin()
    async def category_add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddCategoryModal())

    @admin.command(name="category_list", description="カテゴリー一覧を表示します")
    @is_admin()
    async def category_list(self, interaction: discord.Interaction):
        categories = await self.db.get_categories()
        if not categories:
            await interaction.response.send_message(embed=E.info("カテゴリーなし", "カテゴリーが登録されていません。"), ephemeral=True)
            return
        embed = discord.Embed(title="📂  カテゴリー一覧", color=Config.COLOR_PRIMARY)
        for cat in categories:
            prod_count_row = await self.db._fetch_one("SELECT COUNT(*) as c FROM products WHERE category_id=?", (cat["id"],))
            count = prod_count_row["c"] if prod_count_row else 0
            embed.add_field(
                name=f"ID:{cat['id']}  {cat['emoji']} {cat['name']}",
                value=f"{cat['description'] or '説明なし'}  |  商品数: **{count}**",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="category_delete", description="カテゴリーを削除します（商品も削除されます）")
    @app_commands.describe(category_id="カテゴリーID")
    @is_admin()
    async def category_delete(self, interaction: discord.Interaction, category_id: int):
        cat = await self.db.get_category(category_id)
        if not cat:
            await interaction.response.send_message(embed=E.error("カテゴリーが見つかりません"), ephemeral=True)
            return
        await self.db.delete_category(category_id)
        await interaction.response.send_message(
            embed=E.success("カテゴリーを削除しました", f"**{cat['emoji']} {cat['name']}** を削除しました。"),
            ephemeral=True
        )

    # ── Product commands ───────────────────────────────────────────────────

    @admin.command(name="product_add", description="商品を追加します")
    @app_commands.describe(category_id="カテゴリーID")
    @is_admin()
    async def product_add(self, interaction: discord.Interaction, category_id: int):
        cat = await self.db.get_category(category_id)
        if not cat:
            await interaction.response.send_message(embed=E.error("カテゴリーが見つかりません"), ephemeral=True)
            return
        await interaction.response.send_modal(AddProductModal(category_id))

    @admin.command(name="product_edit", description="商品情報を編集します")
    @app_commands.describe(product_id="商品ID")
    @is_admin()
    async def product_edit(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await interaction.response.send_modal(EditProductModal(p))

    @admin.command(name="product_delete", description="商品を削除します")
    @app_commands.describe(product_id="商品ID")
    @is_admin()
    async def product_delete(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await self.db.delete_product(product_id)
        await interaction.response.send_message(
            embed=E.success("商品を削除しました", f"**{p['name']}** (ID:{product_id}) を削除しました。"),
            ephemeral=True
        )

    @admin.command(name="product_toggle", description="商品の販売状態を切り替えます")
    @app_commands.describe(product_id="商品ID")
    @is_admin()
    async def product_toggle(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        new_state = 0 if p["is_available"] else 1
        await self.db.update_product(product_id, is_available=new_state)
        label = "販売中" if new_state else "非販売"
        await interaction.response.send_message(
            embed=E.success("ステータスを変更しました", f"**{p['name']}** を「{label}」に設定しました。"),
            ephemeral=True
        )

    @admin.command(name="product_stock", description="在庫数を設定します")
    @app_commands.describe(product_id="商品ID", stock="在庫数（-1=無制限）")
    @is_admin()
    async def product_stock(self, interaction: discord.Interaction, product_id: int, stock: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await self.db.update_stock(product_id, stock)
        stock_str = "無制限" if stock == -1 else f"{stock:,} 個"
        await interaction.response.send_message(
            embed=E.success("在庫を更新しました", f"**{p['name']}** の在庫: {stock_str}"),
            ephemeral=True
        )

    @admin.command(name="product_list", description="全商品一覧を表示します")
    @app_commands.describe(category_id="カテゴリーID（省略時：全商品）")
    @is_admin()
    async def product_list(self, interaction: discord.Interaction, category_id: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        products = await self.db.get_products(category_id=category_id, available_only=False)
        if not products:
            await interaction.followup.send(embed=E.info("商品なし", "商品が登録されていません。"), ephemeral=True)
            return

        pages = []
        PAGE = 8
        for i in range(0, len(products), PAGE):
            chunk = products[i:i+PAGE]
            embed = discord.Embed(title="📦  商品管理一覧", color=Config.COLOR_PRIMARY)
            for p in chunk:
                stock_str = "∞" if p["stock"] == -1 else str(p["stock"])
                status = "🟢" if p["is_available"] else "🔴"
                embed.add_field(
                    name=f"{status} #{p['id']} {p['cat_emoji']} {p['name']}",
                    value=f"価格: {p['price']:,}  |  在庫: {stock_str}  |  売上: {p['sold_count']}個",
                    inline=False
                )
            embed.set_footer(text=f"ページ {i//PAGE+1}/{(len(products)+PAGE-1)//PAGE}")
            pages.append(embed)

        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
        else:
            from utils.views import Paginator
            view = Paginator(pages, interaction.user.id)
            await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)

    # ── Order commands ─────────────────────────────────────────────────────

    @admin.command(name="order_list", description="注文一覧を表示します")
    @app_commands.describe(status="ステータスフィルター")
    @app_commands.choices(status=[
        app_commands.Choice(name="全て",    value="all"),
        app_commands.Choice(name="保留中",  value="pending"),
        app_commands.Choice(name="処理中",  value="processing"),
        app_commands.Choice(name="完了",    value="completed"),
        app_commands.Choice(name="キャンセル", value="cancelled"),
    ])
    @is_admin()
    async def order_list(self, interaction: discord.Interaction, status: str = "all"):
        await interaction.response.defer(ephemeral=True)
        filter_status = None if status == "all" else status
        orders = await self.db.get_all_orders(status=filter_status, limit=50)
        if not orders:
            await interaction.followup.send(embed=E.info("注文なし", "該当する注文がありません。"), ephemeral=True)
            return

        status_icons = {"pending": "⏳", "processing": "🔄", "completed": "✅", "cancelled": "❌"}
        embed = discord.Embed(title=f"📋  注文一覧  [{status}]", color=Config.COLOR_PRIMARY)
        for o in orders[:20]:
            icon = status_icons.get(o["status"], "❓")
            embed.add_field(
                name=f"{icon} #{o['id']:05d}  ({o['created_at'][:10]})",
                value=f"ユーザー: <@{o['user_id']}>  |  合計: **{o['total_price']:,}** {Config.CURRENCY_NAME}",
                inline=False
            )
        embed.set_footer(text=f"最大50件表示 | 管理: /admin order_manage <注文ID>")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="order_manage", description="注文を管理します（完了/キャンセル）")
    @app_commands.describe(order_id="注文ID")
    @is_admin()
    async def order_manage(self, interaction: discord.Interaction, order_id: int):
        await interaction.response.defer(ephemeral=True)
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.followup.send(embed=E.error("注文が見つかりません"), ephemeral=True)
            return
        order_items = await self.db.get_order_items(order_id)
        embed = E.order_detail(order, order_items)
        view = OrderAdminView(order, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── User management ────────────────────────────────────────────────────

    @admin.command(name="user_balance", description="ユーザーの残高を調整します")
    @app_commands.describe(user="対象ユーザー", amount="変更量（負の値で減算）")
    @is_admin()
    async def user_balance(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        await self.db.get_user(user.id)
        ok = await self.db.update_balance(user.id, amount)
        if not ok:
            await interaction.response.send_message(embed=E.error("残高不足", "残高がマイナスになります。"), ephemeral=True)
            return
        sign = "+" if amount >= 0 else ""
        await self.db.add_transaction(user.id, amount, "admin", f"管理者による調整 ({interaction.user})")
        user_row = await self.db.get_user(user.id)
        await interaction.response.send_message(
            embed=E.success(
                "残高を調整しました",
                f"{user.mention} に **{sign}{amount:,}** {Config.CURRENCY_NAME} を付与しました。\n新残高: **{user_row['balance']:,}** {Config.CURRENCY_NAME}"
            ),
            ephemeral=True
        )

    @admin.command(name="user_ban", description="ユーザーのショップ利用を制限します")
    @app_commands.describe(user="対象ユーザー", banned="BANするか解除するか")
    @is_admin()
    async def user_ban(self, interaction: discord.Interaction, user: discord.Member, banned: bool = True):
        await self.db.ban_user(user.id, banned)
        action = "利用制限しました" if banned else "制限を解除しました"
        await interaction.response.send_message(
            embed=E.success(f"ユーザーを{action}", f"{user.mention} のショップ利用を{action}。"),
            ephemeral=True
        )

    @admin.command(name="user_info", description="ユーザー情報を表示します")
    @app_commands.describe(user="対象ユーザー")
    @is_admin()
    async def user_info(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        u = await self.db.get_user(user.id)
        orders = await self.db.get_user_orders(user.id, limit=5)
        embed = discord.Embed(title=f"👤  {user.display_name}", color=Config.COLOR_PRIMARY)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="残高", value=f"{u['balance']:,} {Config.CURRENCY_NAME}", inline=True)
        embed.add_field(name="累計購入", value=f"{u['total_spent']:,} {Config.CURRENCY_NAME}", inline=True)
        embed.add_field(name="BAN状態", value="🔴 制限中" if u["is_banned"] else "🟢 正常", inline=True)
        embed.add_field(name="デイリー最終", value=u["daily_last"] or "未取得", inline=True)
        embed.add_field(name="登録日", value=u["created_at"][:10], inline=True)
        if orders:
            lines = [f"#{o['id']:05d} {o['status']} {o['total_price']:,}pt" for o in orders]
            embed.add_field(name="最近の注文", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
