import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from config import Config
import utils.embeds as E
from utils.views import (
    AddProductModal, EditProductModal, AddCategoryModal,
    AnnounceModal, OrderAdminView, FlashSaleModal, Paginator
)


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        if any(r.name == Config.ADMIN_ROLE for r in interaction.user.roles):
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

    # ── Stats & Dashboard ──────────────────────────────────────────────────

    @admin.command(name="stats", description="ショップの統計ダッシュボードを表示します")
    @is_admin()
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        stats = await self.db.get_shop_stats()
        revenue_7d = await self.db.get_revenue_by_day(7)
        embed = E.stats_embed(stats)
        if revenue_7d:
            lines = [f"`{r['day']}` {r['revenue']:,}pt / {r['order_count']}件" for r in revenue_7d]
            embed.add_field(name="📅 直近7日売上", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Announce ───────────────────────────────────────────────────────────

    @admin.command(name="announce", description="お知らせを送信します")
    @is_admin()
    async def announce(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AnnounceModal())

    # ── Category management ────────────────────────────────────────────────

    @admin.command(name="category_add", description="カテゴリーを追加します")
    @is_admin()
    async def category_add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddCategoryModal())

    @admin.command(name="category_list", description="カテゴリー一覧を表示します")
    @is_admin()
    async def category_list(self, interaction: discord.Interaction):
        categories = await self.db.get_categories()
        if not categories:
            await interaction.response.send_message(embed=E.info("カテゴリーなし"), ephemeral=True)
            return
        embed = discord.Embed(title="📂  カテゴリー一覧", color=Config.COLOR_PRIMARY)
        for cat in categories:
            row = await self.db._fetch_one("SELECT COUNT(*) as c FROM products WHERE category_id=?", (cat["id"],))
            count = row["c"] if row else 0
            embed.add_field(
                name=f"ID:{cat['id']}  {cat['emoji']} {cat['name']}",
                value=f"{cat['description'] or '説明なし'}  |  商品数: **{count}**",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="category_delete", description="カテゴリーを削除します（商品も全削除）")
    @app_commands.describe(category_id="カテゴリーID")
    @is_admin()
    async def category_delete(self, interaction: discord.Interaction, category_id: int):
        cat = await self.db.get_category(category_id)
        if not cat:
            await interaction.response.send_message(embed=E.error("カテゴリーが見つかりません"), ephemeral=True)
            return
        await self.db.delete_category(category_id)
        await interaction.response.send_message(embed=E.success("カテゴリーを削除しました", f"**{cat['emoji']} {cat['name']}**"), ephemeral=True)

    # ── Product management ─────────────────────────────────────────────────

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
        await interaction.response.send_message(embed=E.success("商品を削除しました", f"**{p['name']}** (ID:{product_id})"), ephemeral=True)

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
        await interaction.response.send_message(
            embed=E.success("ステータス変更", f"**{p['name']}** を「{'販売中' if new_state else '非販売'}」に設定しました。"),
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
        old_stock = p["stock"]
        await self.db.update_stock(product_id, stock)
        stock_str = "無制限" if stock == -1 else f"{stock:,} 個"
        await interaction.response.send_message(
            embed=E.success("在庫を更新しました", f"**{p['name']}** の在庫: {stock_str}"),
            ephemeral=True
        )
        # Notify watchlist if stock became available
        if (old_stock == 0 or (old_stock != -1 and old_stock <= 0)) and (stock == -1 or stock > 0):
            from cogs.watchlist import Watchlist
            notified = await Watchlist.notify_watchers(interaction.client, product_id, p["name"])
            if notified:
                await self.db.reset_watcher_notify(product_id)

    @admin.command(name="product_digital", description="商品のデジタル設定を切り替えます")
    @app_commands.describe(product_id="商品ID")
    @is_admin()
    async def product_digital(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        new_val = 0 if p["is_digital"] else 1
        await self.db.update_product(product_id, is_digital=new_val)
        label = "デジタル商品に設定しました" if new_val else "通常商品に変更しました"
        await interaction.response.send_message(embed=E.success(label, f"**{p['name']}**"), ephemeral=True)

    @admin.command(name="product_tags", description="商品にタグを設定します")
    @app_commands.describe(product_id="商品ID", tags="カンマ区切りのタグ（例: ゲーム,PC,Steam）")
    @is_admin()
    async def product_tags(self, interaction: discord.Interaction, product_id: int, tags: str):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        await self.db.update_product(product_id, tags=tags)
        await interaction.response.send_message(embed=E.success("タグを設定しました", f"**{p['name']}**\nタグ: {tags}"), ephemeral=True)

    @admin.command(name="product_list", description="全商品一覧を表示します")
    @app_commands.describe(category_id="カテゴリーID（省略時：全商品）")
    @is_admin()
    async def product_list(self, interaction: discord.Interaction, category_id: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        products = await self.db.get_products(category_id=category_id, available_only=False)
        if not products:
            await interaction.followup.send(embed=E.info("商品なし"), ephemeral=True)
            return
        pages = []
        PAGE = 8
        for i in range(0, len(products), PAGE):
            chunk = products[i:i+PAGE]
            embed = discord.Embed(title="📦  商品管理一覧", color=Config.COLOR_PRIMARY)
            for p in chunk:
                stock_str = "∞" if p["stock"] == -1 else str(p["stock"])
                status = "🟢" if p["is_available"] else "🔴"
                digital = "💾" if p.get("is_digital") else ""
                embed.add_field(
                    name=f"{status} #{p['id']} {p['cat_emoji']}{digital} {p['name']}",
                    value=f"価格: {p['price']:,}  |  在庫: {stock_str}  |  売上: {p['sold_count']}個",
                    inline=False
                )
            embed.set_footer(text=f"ページ {i//PAGE+1}/{(len(products)+PAGE-1)//PAGE}")
            pages.append(embed)
        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
        else:
            view = Paginator(pages, interaction.user.id)
            await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)

    # ── Flash sale management ──────────────────────────────────────────────

    @admin.command(name="flash_sale", description="フラッシュセールを開始します")
    @app_commands.describe(product_id="商品ID")
    @is_admin()
    async def flash_sale(self, interaction: discord.Interaction, product_id: int):
        p = await self.db.get_product(product_id)
        if not p:
            await interaction.response.send_message(embed=E.error("商品が見つかりません"), ephemeral=True)
            return
        existing_sale = await self.db.get_product_flash_sale(product_id)
        if existing_sale:
            await interaction.response.send_message(embed=E.error("既にセール中", f"**{p['name']}** は既にフラッシュセール中です。"), ephemeral=True)
            return
        await interaction.response.send_modal(FlashSaleModal(p))

    @admin.command(name="flash_sale_end", description="フラッシュセールを終了します")
    @app_commands.describe(product_id="商品ID")
    @is_admin()
    async def flash_sale_end(self, interaction: discord.Interaction, product_id: int):
        sale = await self.db.get_product_flash_sale(product_id)
        if not sale:
            await interaction.response.send_message(embed=E.error("セールが見つかりません"), ephemeral=True)
            return
        await self.db.update_product(product_id, price=sale["original_price"])
        await self.db.end_flash_sale(sale["id"])
        await interaction.response.send_message(
            embed=E.success("フラッシュセールを終了しました", f"価格を **{sale['original_price']:,}** {Config.CURRENCY_NAME} に戻しました。"),
            ephemeral=True
        )

    @admin.command(name="flash_sale_list", description="開催中・予定のフラッシュセール一覧")
    @is_admin()
    async def flash_sale_list(self, interaction: discord.Interaction):
        active = await self.db.get_active_flash_sales()
        upcoming = await self.db.get_upcoming_flash_sales()
        embed = discord.Embed(title="⚡  フラッシュセール", color=Config.COLOR_FLASH)
        if active:
            lines = [f"**{s['product_name']}** `{s['discount_percent']}%OFF` → 終了: {s['end_time'][:16]}" for s in active]
            embed.add_field(name="🔥 開催中", value="\n".join(lines), inline=False)
        if upcoming:
            lines = [f"**{s['product_name']}** `{s['discount_percent']}%OFF` → 開始: {s['start_time'][:16]}" for s in upcoming]
            embed.add_field(name="⏰ 予定", value="\n".join(lines), inline=False)
        if not active and not upcoming:
            embed.description = "開催中・予定のセールはありません。"
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Order management ───────────────────────────────────────────────────

    @admin.command(name="order_list", description="注文一覧を表示します")
    @app_commands.describe(status="ステータスフィルター")
    @app_commands.choices(status=[
        app_commands.Choice(name="全て",       value="all"),
        app_commands.Choice(name="保留中",     value="pending"),
        app_commands.Choice(name="処理中",     value="processing"),
        app_commands.Choice(name="完了",       value="completed"),
        app_commands.Choice(name="キャンセル", value="cancelled"),
    ])
    @is_admin()
    async def order_list(self, interaction: discord.Interaction, status: str = "all"):
        await interaction.response.defer(ephemeral=True)
        orders = await self.db.get_all_orders(status=None if status == "all" else status, limit=50)
        if not orders:
            await interaction.followup.send(embed=E.info("注文なし", "該当する注文がありません。"), ephemeral=True)
            return
        status_icons = {"pending": "⏳", "processing": "🔄", "completed": "✅", "cancelled": "❌"}
        embed = discord.Embed(title=f"📋  注文一覧  [{status}]", color=Config.COLOR_PRIMARY)
        for o in orders[:20]:
            icon = status_icons.get(o["status"], "❓")
            gift_tag = " 🎁" if o.get("is_gift") else ""
            embed.add_field(
                name=f"{icon} #{o['id']:05d}{gift_tag}  ({o['created_at'][:10]})",
                value=f"<@{o['user_id']}>  |  **{o['total_price']:,}** {Config.CURRENCY_NAME}",
                inline=False
            )
        embed.set_footer(text="/admin order_manage <ID> で管理")
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

    @admin.command(name="order_bulk_complete", description="保留中の注文を一括完了にします")
    @is_admin()
    async def order_bulk_complete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        pending = await self.db.get_all_orders(status="pending", limit=100)
        if not pending:
            await interaction.followup.send(embed=E.info("保留中注文なし"), ephemeral=True)
            return
        count = 0
        for order in pending:
            await self.db.update_order_status(order["id"], "completed")
            user = self.bot.get_user(order["user_id"])
            if user:
                try:
                    await user.send(embed=E.success("注文が完了しました！", f"注文 **#{order['id']:05d}** が処理されました。"))
                except Exception:
                    pass
            count += 1
        await interaction.followup.send(embed=E.success(f"{count} 件の注文を完了にしました"), ephemeral=True)

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
        await self.db.add_transaction(user.id, amount, "admin", f"管理者調整 by {interaction.user}")
        u = await self.db.get_user(user.id)
        await interaction.response.send_message(
            embed=E.success("残高を調整しました", f"{user.mention}  **{sign}{amount:,}** {Config.CURRENCY_NAME}\n新残高: **{u['balance']:,}** {Config.CURRENCY_NAME}"),
            ephemeral=True
        )

    @admin.command(name="user_ban", description="ユーザーのショップ利用を制限/解除します")
    @app_commands.describe(user="対象ユーザー", banned="制限するかどうか")
    @is_admin()
    async def user_ban(self, interaction: discord.Interaction, user: discord.Member, banned: bool = True):
        await self.db.ban_user(user.id, banned)
        action = "利用制限しました" if banned else "制限を解除しました"
        await interaction.response.send_message(embed=E.success(f"ユーザーを{action}", f"{user.mention}"), ephemeral=True)

    @admin.command(name="user_info", description="ユーザー情報を詳細表示します")
    @app_commands.describe(user="対象ユーザー")
    @is_admin()
    async def user_info(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        u = await self.db.get_user(user.id)
        tier = await self.db.get_user_rank_tier(user.id)
        orders = await self.db.get_user_orders(user.id, limit=5)
        achievements = await self.db.get_user_achievements(user.id)

        embed = discord.Embed(title=f"👤  {user.display_name}", color=tier["color"] if tier else Config.COLOR_PRIMARY)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="残高",     value=f"{u['balance']:,} {Config.CURRENCY_NAME}", inline=True)
        embed.add_field(name="累計購入", value=f"{u['total_spent']:,} {Config.CURRENCY_NAME}", inline=True)
        embed.add_field(name="ランク",   value=f"{tier['emoji']} {tier['name']}" if tier else "—", inline=True)
        embed.add_field(name="ストリーク", value=f"{u.get('daily_streak', 0)} 日", inline=True)
        embed.add_field(name="実績数",   value=f"{len(achievements)} 個", inline=True)
        embed.add_field(name="BAN",      value="🔴 制限中" if u["is_banned"] else "🟢 正常", inline=True)
        embed.add_field(name="紹介コード", value=f"`{u.get('referral_code', '—')}`", inline=True)
        embed.add_field(name="登録日",   value=u["created_at"][:10], inline=True)
        if orders:
            lines = [f"#{o['id']:05d} {o['status']} {o['total_price']:,}pt" for o in orders]
            embed.add_field(name="最近の注文", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="user_reset_streak", description="ユーザーのデイリーストリークをリセットします")
    @app_commands.describe(user="対象ユーザー")
    @is_admin()
    async def user_reset_streak(self, interaction: discord.Interaction, user: discord.Member):
        await self.db._execute("UPDATE users SET daily_streak=0 WHERE user_id=?", (user.id,))
        await interaction.response.send_message(embed=E.success("ストリークをリセットしました", f"{user.mention}"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
