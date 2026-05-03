import discord
from discord.ext import commands, tasks
from datetime import datetime
import logging
from config import Config
import utils.embeds as E

log = logging.getLogger("ShopBot.Tasks")


class Tasks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_flash_sales.start()
        self.check_low_stock.start()
        self.daily_stats.start()

    def cog_unload(self):
        self.check_flash_sales.cancel()
        self.check_low_stock.cancel()
        self.daily_stats.cancel()

    @property
    def db(self):
        return self.bot.db

    # ── Flash sale lifecycle ───────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_flash_sales(self):
        try:
            expired = await self.db.get_expired_flash_sales()
            for sale in expired:
                # Restore original price
                await self.db.update_product(sale["product_id"], price=sale["original_price"])
                await self.db.end_flash_sale(sale["id"])
                log.info(f"Flash sale ended for product {sale['product_id']}: {sale['product_name']}")

                # Notify channel
                channel_id = sale["notify_channel"] or Config.FLASH_SALE_CHANNEL
                if channel_id:
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        embed = discord.Embed(
                            title="⏰  フラッシュセール終了",
                            description=f"**{sale['product_name']}** のセールが終了しました。\n通常価格に戻りました: **{sale['original_price']:,}** {Config.CURRENCY_NAME}",
                            color=Config.COLOR_WARNING
                        )
                        await channel.send(embed=embed)

            # Activate pending flash sales
            now = datetime.now().isoformat()
            upcoming = await self.db._fetch_all(
                "SELECT * FROM flash_sales WHERE is_active=1 AND start_time<=? AND end_time>?", (now, now)
            )
            for sale in upcoming:
                prod = await self.db.get_product(sale["product_id"])
                if prod and prod["price"] != sale["sale_price"]:
                    await self.db.update_product(sale["product_id"], price=sale["sale_price"])
                    log.info(f"Flash sale started: {sale['product_name']} {sale['discount_percent']}% off")
        except Exception as e:
            log.error(f"check_flash_sales error: {e}", exc_info=True)

    @check_flash_sales.before_loop
    async def before_flash_sales(self):
        await self.bot.wait_until_ready()

    # ── Low stock alerts ───────────────────────────────────────────────────

    @tasks.loop(hours=6)
    async def check_low_stock(self):
        if not Config.ORDER_LOG_CHANNEL:
            return
        try:
            low_stock = await self.db.get_low_stock_products(threshold=3)
            if not low_stock:
                return
            channel = self.bot.get_channel(Config.ORDER_LOG_CHANNEL)
            if not channel:
                return

            embed = discord.Embed(
                title="⚠️  在庫少警告",
                color=Config.COLOR_WARNING,
                timestamp=datetime.utcnow()
            )
            for p in low_stock[:10]:
                embed.add_field(
                    name=f"#{p['id']} {p['name']}",
                    value=f"残り **{p['stock']}** 個  |  {p['cat_name']}",
                    inline=False
                )
            embed.set_footer(text="Shop Bot • /admin product_stock で在庫を更新")
            await channel.send(embed=embed)
            log.info(f"Low stock alert sent for {len(low_stock)} products")
        except Exception as e:
            log.error(f"check_low_stock error: {e}", exc_info=True)

    @check_low_stock.before_loop
    async def before_low_stock(self):
        await self.bot.wait_until_ready()

    # ── Daily stats summary ────────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def daily_stats(self):
        if not Config.ORDER_LOG_CHANNEL:
            return
        try:
            channel = self.bot.get_channel(Config.ORDER_LOG_CHANNEL)
            if not channel:
                return

            stats = await self.db.get_shop_stats()
            revenue_7d = await self.db.get_revenue_by_day(7)

            embed = discord.Embed(
                title="📊  本日のショップ集計",
                color=Config.COLOR_PRIMARY,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="今日の注文数",   value=f"{stats['today_orders']} 件", inline=True)
            embed.add_field(name="今日の売上",     value=f"{stats['today_revenue']:,} {Config.CURRENCY_NAME}", inline=True)
            embed.add_field(name="累計売上",       value=f"{stats['total_revenue']:,} {Config.CURRENCY_NAME}", inline=True)
            embed.add_field(name="保留中注文",     value=f"{stats['pending_orders']} 件", inline=True)
            embed.add_field(name="オープンチケット", value=f"{stats['active_tickets']} 件", inline=True)

            if revenue_7d:
                lines = [f"`{r['day']}` — {r['revenue']:,}pt ({r['order_count']}件)" for r in revenue_7d]
                embed.add_field(name="📅 直近7日", value="\n".join(lines[-5:]), inline=False)

            await channel.send(embed=embed)
        except Exception as e:
            log.error(f"daily_stats error: {e}", exc_info=True)

    @daily_stats.before_loop
    async def before_daily_stats(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Tasks(bot))
