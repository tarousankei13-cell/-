"""簡易プロキシチェッカー Discord BOT (単体で動作します)。

  ┌────────────────────────────────────────────────────────────┐
  │  下の TOKEN に BOT のトークンを貼り付けてください            │
  │  (Discord Developer Portal → Bot → Reset Token でコピー)    │
  │  それ以外の設定は起動後に /config set コマンドで行います     │
  └────────────────────────────────────────────────────────────┘

起動方法:
    pip install -r requirements.txt
    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import discord
from discord.ext import commands

# ===========================================================================
#  ここにトークンを貼り付ける (前後のダブルクォートは消さないでください)
# ===========================================================================
TOKEN = "ここにBOTのトークンを貼り付けてください"
# ===========================================================================

_TOKEN_PLACEHOLDER = "ここにBOTのトークンを貼り付けてください"

COGS_DIR = Path(__file__).parent / "cogs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("proxybot")


class ProxyCheckerBot(commands.Bot):
    def __init__(self) -> None:
        # スラッシュコマンドのみなので特権インテントは不要
        super().__init__(command_prefix="!", intents=discord.Intents.default(), help_command=None)

    async def setup_hook(self) -> None:
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                logger.info("拡張を読み込みました: %s", extension)
            except Exception as exc:  # noqa: BLE001 - 1つ失敗しても他は読み込む
                logger.error("拡張の読み込みに失敗しました %s: %s", extension, exc)

        synced = await self.tree.sync()
        logger.info("スラッシュコマンドを同期しました (%d件)", len(synced))

    async def on_ready(self) -> None:
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="/check でプロキシ判定"
            )
        )
        logger.info("ログイン成功: %s (ID: %s)", self.user, getattr(self.user, "id", "?"))
        logger.info("参加サーバー数: %d / discord.py %s", len(self.guilds), discord.__version__)
        logger.info("準備完了。/check または /help をお試しください。")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """招待直後からコマンドを使えるよう、そのサーバーへ即時同期する。"""
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("参加したサーバーへコマンドを同期しました: %s", guild.name)
        except discord.HTTPException as exc:
            logger.warning("サーバーへの同期に失敗しました (%s): %s", guild.name, exc)


def main() -> None:
    token = TOKEN.strip()
    if not token or token == _TOKEN_PLACEHOLDER:
        logger.critical(
            "トークンが設定されていません。main.py の TOKEN にBOTのトークンを貼り付けてください。"
        )
        sys.exit(1)

    bot = ProxyCheckerBot()
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.critical("トークンが正しくありません。main.py の TOKEN を確認してください。")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        logger.critical(
            "特権インテントが必要と言われました。Developer Portal の設定を確認してください。"
        )
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("停止しました。")


if __name__ == "__main__":
    try:
        main()
    except asyncio.CancelledError:
        pass
