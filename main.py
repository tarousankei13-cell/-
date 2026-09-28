import sys
import logging
from pathlib import Path

import discord
from discord.ext import commands

# ── ここにトークンとオーナーIDを直接記入 ──
DISCORD_TOKEN = "ここにBotトークンを貼り付け"
OWNER_IDS = {1324938326741876758}  # 管理者のDiscordユーザーID（複数可）
# ── MCD連携用（任意：不要なら空文字のまま） ──
MCD_REFRESH_TOKEN = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")

COGS_DIR = Path(__file__).parent / "cogs"


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            owner_ids=OWNER_IDS,
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
        await self.mcd.initialize(refresh_token=MCD_REFRESH_TOKEN)

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
    if DISCORD_TOKEN == "ここにBotトークンを貼り付け" or not DISCORD_TOKEN:
        logger.critical("DISCORD_TOKEN が設定されていません。main.py を編集してください。")
        sys.exit(1)

    bot = Bot()
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
