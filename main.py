"""マクドナルド注文BOT — 起動。

このファイルに直書きするのは次の2つだけ。

    DISCORD_TOKEN   Discord の BOT トークン
    OWNER_IDS       オーナーの Discord ユーザーID

それ以外（チャンネル・ロール・料率・上限・不正検知のしきい値など）は
すべて Discord 上の ``/config`` コマンドで設定する。値は SQLite に
保存されるので、再起動しても残る。

起動後の手順:
    1. /config setup        未設定の項目を一覧で確認
    2. /config channel ...  各チャンネルとロールを設定
    3. /mcd login           マクドナルドにログイン
    4. /kyash login         Kyash にログイン
    5. /panel               パネルを設置
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import discord
from discord.ext import commands

from mcd import cards, logsetup
from mcd.fraud import FraudEngine
from mcd.kyashclient import KyashPool
from mcd.mcdclient import McdPool
from mcd.settings import Settings
from mcd.store import Store

# =====================================================================
#  ここだけ書き換えてください
# =====================================================================

DISCORD_TOKEN = "ここにDiscordのBOTトークンを貼り付け"

OWNER_IDS = {
    1324938326741876758,
    # 複数いる場合はカンマ区切りで足す
}

# =====================================================================
#  ここから下は通常さわらなくて構いません
#  （DBを開く前に必要な値と、表示用の定数だけ）
# =====================================================================

DB_PATH = "data/bot.sqlite3"
LOG_DIR = "logs"
LOG_LEVEL = logging.INFO
LOG_KEEP_DAYS = 14

# 絵文字は Discord で確実に表示されるものだけを使う。
# 結合絵文字や新しめのコードポイントを混ぜると環境によって表示が崩れるため、
# 古くから存在する単一コードポイントのものに限定している。
E_OK = "\N{WHITE HEAVY CHECK MARK}"
E_NG = "\N{CROSS MARK}"
E_WARN = "\N{WARNING SIGN}"
E_MONEY = "\N{MONEY BAG}"
E_FOOD = "\N{HAMBURGER}"
E_GIFT = "\N{WRAPPED PRESENT}"
E_MEMO = "\N{MEMO}"
E_CAMERA = "\N{CAMERA WITH FLASH}"
E_BELL = "\N{BELL}"
E_CHART = "\N{BAR CHART}"
E_LOCK = "\N{LOCK}"
E_CLOCK = "\N{HOURGLASS WITH FLOWING SAND}"
E_FLAG = "\N{TRIANGULAR FLAG ON POST}"
E_SHOP = "\N{CONVENIENCE STORE}"
E_MAIL = "\N{ENVELOPE}"
E_KEY = "\N{KEY}"

CONSTANTS = {
    name: value
    for name, value in list(globals().items())
    if name.startswith("E_") or name in ("DB_PATH", "LOG_DIR", "LOG_LEVEL", "LOG_KEEP_DAYS")
}

COGS_DIR = Path(__file__).parent / "cogs"
PLACEHOLDER = "ここにDiscordのBOTトークンを貼り付け"


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True   # DM の感想を読むために必要
        intents.members = True           # 紹介判定で参加日時を見るために必要
        intents.dm_messages = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            owner_ids=set(OWNER_IDS),
            help_command=None,
        )
        self.store = Store(DB_PATH)
        self.cfg = Settings(self.store, CONSTANTS)
        self.mcd = McdPool(self.store)
        self.kyash = KyashPool(self.store)
        self.fraud = FraudEngine(self.store, self.cfg.fraud_config)
        self.log = logging.getLogger("bot")

    async def setup_hook(self) -> None:
        for cog_file in sorted(COGS_DIR.glob("*.py")):
            if cog_file.stem.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                self.log.info("拡張を読み込みました: %s", ext)
            except Exception:
                self.log.exception("拡張の読み込みに失敗しました: %s", ext)

        await self.sync_commands()

    async def sync_commands(self) -> None:
        """設定された GUILD_ID に合わせてスラッシュコマンドを同期する。"""
        guild_id = int(self.cfg.GUILD_ID or 0)
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self.log.info("コマンド %s 個をギルド %s に同期しました", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            self.log.info("コマンド %s 個をグローバルに同期しました", len(synced))

    async def on_ready(self) -> None:
        self.log.info("ログイン: %s (ID: %s)", self.user, self.user.id)

        cards.configure_font(self.cfg.FONT_PATH)

        missing = self.cfg.missing_required()
        if missing:
            self.log.warning(
                "未設定の項目があります（/config setup で確認できます）: %s",
                ", ".join(s.key for s in missing),
            )
        self.log.info(
            "マクドナルドアカウント %s 件 / Kyash アカウント %s 件",
            len(self.store.list_mcd_accounts(only_enabled=True)),
            len(self.store.list_kyash_accounts(only_enabled=True)),
        )

    async def close(self) -> None:
        try:
            self.store.close()
        finally:
            await super().close()

    # ---- ログ送信の共通口 ------------------------------------------------

    async def send_log(self, channel_id: int, embed: discord.Embed) -> None:
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                self.log.warning("ログチャンネル %s を取得できませんでした", channel_id)
                return
        try:
            await channel.send(embed=embed)
        except Exception:
            self.log.exception("ログの送信に失敗しました (channel=%s)", channel_id)


def main() -> None:
    logsetup.setup(LOG_DIR, level=LOG_LEVEL, keep_days=LOG_KEEP_DAYS)
    log = logging.getLogger("bot")

    if not DISCORD_TOKEN or DISCORD_TOKEN == PLACEHOLDER:
        log.critical("main.py の DISCORD_TOKEN を設定してください。")
        sys.exit(1)
    if not OWNER_IDS:
        log.critical("main.py の OWNER_IDS を設定してください。")
        sys.exit(1)

    bot = Bot()
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "特権インテントが有効になっていません。Discord Developer Portal の "
            "Bot ページで MESSAGE CONTENT INTENT と SERVER MEMBERS INTENT を "
            "有効にしてください。"
        )
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
