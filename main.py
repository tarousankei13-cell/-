import asyncio
import logging
import signal
import sys
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
SHUTDOWN_TIMEOUT = 30  # 処理中の注文を待つ最大秒数


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            owner_ids=OWNER_IDS,
            help_command=None,
        )
        self.db = None  # type: ignore[assignment]
        self.mcd = None  # type: ignore[assignment]
        self.inflight = 0

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

        for cog_file in sorted(COGS_DIR.glob("*.py")):
            if cog_file.stem.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension: %s", ext)
            except Exception as e:
                logger.error("Failed to load extension %s: %s", ext, e)

        try:
            synced = await self.tree.sync()
            logger.info("Slash commands synced (%d)", len(synced))
        except Exception as e:
            logger.error("Command sync failed: %s", e)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        logger.info("discord.py version: %s", discord.__version__)

        panels = await self.db.get_all_panels()
        for p in panels:
            try:
                ch = self.get_channel(p["channel_id"])
                if ch is None:
                    ch = await self.fetch_channel(p["channel_id"])
                await ch.fetch_message(p["message_id"])  # type: ignore[union-attr]
                logger.info(
                    "Panel verified: guild=%s channel=%s",
                    p["guild_id"], p["channel_id"],
                )
            except discord.NotFound:
                logger.warning(
                    "Panel message not found, removing: guild=%s", p["guild_id"]
                )
                await self.db.delete_panel(p["guild_id"])
            except Exception as exc:
                logger.warning("Panel check failed: %s", exc)

    async def wait_for_inflight(self, timeout: int = SHUTDOWN_TIMEOUT) -> None:
        """処理中の注文が終わるまで待つ。"""
        waited = 0.0
        while self.inflight > 0 and waited < timeout:
            logger.info("処理中の注文が %d 件あります。待機中...", self.inflight)
            await asyncio.sleep(1)
            waited += 1
        if self.inflight > 0:
            logger.warning(
                "タイムアウト: %d 件の注文が未完了のまま終了します。", self.inflight
            )

    async def close(self) -> None:
        if self.mcd:
            try:
                await self.mcd.close()
            except Exception:
                pass
        if self.db:
            try:
                await self.db.close()
            except Exception:
                pass
        await super().close()


async def run_bot() -> None:
    bot = Bot()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    start_task = asyncio.create_task(bot.start(DISCORD_TOKEN))
    stop_task = asyncio.create_task(stop.wait())

    done, pending = await asyncio.wait(
        {start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )

    if stop_task in done:
        logger.info("シャットダウン信号を受信しました。")
        await bot.wait_for_inflight()

    for task in pending:
        task.cancel()

    if not bot.is_closed():
        await bot.close()

    if start_task in done:
        exc = start_task.exception()
        if exc is not None:
            raise exc

    logger.info("正常に終了しました。")


def main() -> None:
    if DISCORD_TOKEN == "ここにBotトークンを貼り付け" or not DISCORD_TOKEN:
        logger.critical("DISCORD_TOKEN が設定されていません。main.py を編集してください。")
        sys.exit(1)

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
