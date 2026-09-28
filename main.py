import os
import sys
import logging
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")

COGS_DIR = Path(__file__).parent / "cogs"


def parse_owner_ids() -> set[int]:
    raw = os.getenv("OWNER_IDS", "")
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip().isdigit()}


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        owner_ids = parse_owner_ids()
        super().__init__(
            command_prefix="!",
            intents=intents,
            owner_ids=owner_ids or None,
        )
        self.db = None  # type: ignore[assignment]
        self.mcd = None  # type: ignore[assignment]

    async def setup_hook(self) -> None:
        from db import Database
        from mcd_adapter import MCDAdapter
        from views import DepositApproveButton, DepositRejectButton, PanelView

        self.db = Database()
        await self.db.initialize()

        self.mcd = MCDAdapter()
        await self.mcd.initialize()

        self.add_view(PanelView())
        self.add_dynamic_items(DepositApproveButton, DepositRejectButton)

        for cog_file in COGS_DIR.glob("*.py"):
            if cog_file.stem.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension: %s", ext)
            except Exception as e:
                logger.error("Failed to load extension %s: %s", ext, e)

        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        logger.info("discord.py version: %s", discord.__version__)

        panels = await self.db.get_all_panels()
        for p in panels:
            try:
                ch = self.get_channel(p["channel_id"])
                if ch is None:
                    ch = await self.fetch_channel(p["channel_id"])
                msg = await ch.fetch_message(p["message_id"])  # type: ignore[union-attr]
                logger.info(
                    "Panel verified: guild=%s channel=%s msg=%s",
                    p["guild_id"], p["channel_id"], p["message_id"],
                )
            except discord.NotFound:
                logger.warning(
                    "Panel message not found, removing: guild=%s", p["guild_id"]
                )
                await self.db.delete_panel(p["guild_id"])
            except Exception as exc:
                logger.warning("Panel check failed: %s", exc)

    async def close(self) -> None:
        if self.db:
            await self.db.close()
        await super().close()


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("DISCORD_TOKEN is not set. Check your .env file.")
        sys.exit(1)

    bot = Bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
