import os
import sys
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("bot.admin")


def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)
    return app_commands.check(predicate)


class Admin(commands.Cog):
    """BOT管理コマンド"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="restart", description="BOTを再起動します (オーナー限定)")
    @is_owner()
    async def restart(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="再起動中...",
                description=":arrows_counterclockwise: BOTを再起動しています。少々お待ちください。",
                color=discord.Color.orange(),
            )
        )
        logger.info("Restart requested by %s (ID: %s)", interaction.user, interaction.user.id)

        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @restart.error
    async def restart_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="権限エラー",
                    description=":no_entry: このコマンドはBOTオーナーのみ使用できます。",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
        else:
            logger.error("Unexpected error in restart command: %s", error)
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
