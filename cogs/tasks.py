"""Background tasks: automatic backup and daily report."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from models import local_now

logger = logging.getLogger("bot.tasks")


class TasksCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._backup_counter = 0
        self.hourly_loop.start()

    def cog_unload(self) -> None:
        self.hourly_loop.cancel()

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    @tasks.loop(hours=1)
    async def hourly_loop(self) -> None:
        if self.bot.is_closed() or self.db is None:
            return
        try:
            await self._maybe_backup()
        except Exception as exc:
            logger.error("Backup task failed: %s", exc)
        try:
            await self._maybe_report()
        except Exception as exc:
            logger.error("Report task failed: %s", exc)

    @hourly_loop.before_loop
    async def before_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # ログイン前にロードされた場合でもループを止めない
            pass

    async def _maybe_backup(self) -> None:
        if await self.db.get_setting("backup_enabled") != "1":
            return
        interval = await self.db.get_int_setting("backup_interval_hours", 6)
        self._backup_counter += 1
        if self._backup_counter < max(1, interval):
            return
        self._backup_counter = 0

        admin_cog = self.bot.get_cog("AdminCog")
        if admin_cog is None:
            return
        path, size = await admin_cog.create_backup()  # type: ignore[attr-defined]
        logger.info("Auto backup created: %s (%.1f KB)", path.name, size / 1024)

    async def _maybe_report(self) -> None:
        if await self.db.get_setting("report_enabled") != "1":
            return
        channel_id = await self.db.get_setting("report_channel_id")
        if not channel_id:
            return

        tz = await self.db.get_int_setting("tz_offset", 9)
        now = local_now(tz)
        target_hour = await self.db.get_int_setting("report_hour", 9)
        if now.hour != target_hour:
            return

        today = now.strftime("%Y-%m-%d")
        if await self.db.get_setting("report_last_date") == today:
            return

        from cogs.admin import build_report_embed

        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            embed = await build_report_embed(self.db, 7)
            await channel.send(embed=embed)  # type: ignore[union-attr]
            await self.db.set_setting("report_last_date", today)
            logger.info("Daily report sent for %s", today)
        except Exception as exc:
            logger.error("Failed to send daily report: %s", exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TasksCog(bot))
