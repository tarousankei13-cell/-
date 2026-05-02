import discord
from discord.ext import commands
import asyncio
import logging
import sys
from config import Config
from database.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ShopBot")


class ShopBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix=Config.PREFIX,
            intents=intents,
            help_command=None,
        )
        self.db = Database()

    async def setup_hook(self):
        await self.db.initialize()
        log.info("Database initialized.")

        cogs = [
            "cogs.shop",
            "cogs.admin",
            "cogs.economy",
            "cogs.orders",
            "cogs.tickets",
            "cogs.reviews",
        ]
        for cog in cogs:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded: {cog}")
            except Exception as exc:
                log.error(f"Failed to load {cog}: {exc}", exc_info=True)

        if Config.GUILD_ID:
            guild = discord.Object(id=Config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Commands synced to guild {Config.GUILD_ID}")
        else:
            await self.tree.sync()
            log.info("Commands synced globally")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="🛒 Shop | /shop"
            ),
        )

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        from utils.embeds import error as err_embed
        msg = "予期しないエラーが発生しました。しばらくしてから再試行してください。"
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            msg = f"クールダウン中です。あと **{error.retry_after:.1f}秒** 待ってください。"
        elif isinstance(error, discord.app_commands.MissingPermissions):
            msg = "このコマンドを実行する権限がありません。"
        elif isinstance(error, discord.app_commands.BotMissingPermissions):
            msg = f"Botに必要な権限がありません: {', '.join(error.missing_permissions)}"
        else:
            log.error(f"App command error in {interaction.command}: {error}", exc_info=True)

        embed = err_embed("エラー", msg)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    async def close(self):
        await self.db.close()
        await super().close()


async def main():
    if not Config.TOKEN:
        log.critical("BOT_TOKEN が設定されていません。.env ファイルを確認してください。")
        sys.exit(1)

    bot = ShopBot()
    async with bot:
        await bot.start(Config.TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped.")
