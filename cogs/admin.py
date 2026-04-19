import os
import sys
import logging

import discord
from discord.ext import commands

logger = logging.getLogger("bot.admin")


class Admin(commands.Cog):
    """BOT管理コマンド"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="restart", aliases=["re"])
    @commands.is_owner()
    async def restart(self, ctx: commands.Context):
        """BOTを再起動します (オーナー限定)"""
        embed = discord.Embed(
            title="再起動中...",
            description=":arrows_counterclockwise: BOTを再起動しています。少々お待ちください。",
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)
        logger.info("Restart requested by %s (ID: %s)", ctx.author, ctx.author.id)

        await self.bot.close()
        # 現在のPythonインタープリタで同じスクリプトを再実行する
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @restart.error
    async def restart_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            embed = discord.Embed(
                title="権限エラー",
                description=":no_entry: このコマンドはBOTオーナーのみ使用できます。",
                color=discord.Color.red(),
            )
            await ctx.send(embed=embed)
        else:
            logger.error("Unexpected error in restart command: %s", error)
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
