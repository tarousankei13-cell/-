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
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        owner_ids = parse_owner_ids()
        super().__init__(
            command_prefix="!",
            intents=intents,
            owner_ids=owner_ids or None,
        )

    async def setup_hook(self):
        for cog_file in COGS_DIR.glob("*.py"):
            if cog_file.stem.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension: %s", ext)
            except Exception as e:
                logger.error("Failed to load extension %s: %s", ext, e)

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        logger.info("discord.py version: %s", discord.__version__)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("DISCORD_TOKEN is not set. Check your .env file.")
        sys.exit(1)

    bot = Bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
