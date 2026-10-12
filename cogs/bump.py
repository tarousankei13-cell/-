import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("bot.bump")

# DISBOARD公式BOTのユーザーID
DISBOARD_BOT_ID = 302050872383242240

# DISBOARDのbumpクールダウン (2時間)
BUMP_COOLDOWN = timedelta(hours=2)

# 設定の保存先 (再起動してもタイマーを引き継ぐため)
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "bump_reminder.json"

# bump成功メッセージの判定キーワード (日本語 / 英語)
SUCCESS_KEYWORDS = ("表示順をアップしました", "アップしました", "Bump done")

# 「あと○分お待ちください」からクールダウンの残り時間を拾う
WAIT_PATTERNS = (
    re.compile(r"あと\s*(\d+)\s*分"),
    re.compile(r"(\d+)\s*minutes?"),
)


def extract_text(message: discord.Message) -> str:
    """メッセージ本文と埋め込みのテキストをまとめて返す"""
    parts = [message.content or ""]
    for embed in message.embeds:
        parts.append(embed.title or "")
        parts.append(embed.description or "")
        for field in embed.fields:
            parts.append(field.name or "")
            parts.append(field.value or "")
    return "\n".join(parts)


def parse_bump_result(text: str) -> timedelta | None:
    """DISBOARDの応答から「次にbumpできるまでの時間」を判定する

    bump成功ならクールダウン(2時間)、「あと○分お待ちください」ならその残り時間を返す。
    bumpと無関係なメッセージの場合はNoneを返す。
    """
    if any(keyword in text for keyword in SUCCESS_KEYWORDS):
        return BUMP_COOLDOWN

    for pattern in WAIT_PATTERNS:
        match = pattern.search(text)
        if match:
            # 分単位で切り捨てられているため1分だけ余裕を持たせる
            return timedelta(minutes=int(match.group(1)) + 1)

    return None


class BumpReminder(commands.Cog):
    """DISBOARD bumpのリマインダー

    DISBOARD BOTの応答を検知し、クールダウン明けに通知を送る。
    bumpの実行自体は人間が行う必要がある (自動bumpはDiscord/DISBOARDの
    規約違反になるため、意図的に実装していない)。
    """

    group = app_commands.Group(
        name="bump-reminder",
        description="DISBOARD bumpリマインダーの設定",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, dict[str, Any]] = self._load()

    async def cog_load(self):
        self.check_reminders.start()

    async def cog_unload(self):
        self.check_reminders.cancel()

    # ---------- 設定の読み書き ----------

    def _load(self) -> dict[str, dict[str, Any]]:
        if not DATA_FILE.exists():
            return {}
        try:
            with DATA_FILE.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to load %s: %s", DATA_FILE, e)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = DATA_FILE.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            tmp.replace(DATA_FILE)
        except OSError as e:
            logger.error("Failed to save %s: %s", DATA_FILE, e)

    # ---------- DISBOARDの応答を検知 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.id != DISBOARD_BOT_ID:
            return

        conf = self.config.get(str(message.guild.id))
        if conf is None:
            return

        wait = parse_bump_result(extract_text(message))
        if wait is None:
            return

        next_at = message.created_at + wait
        conf["next_bump_at"] = next_at.isoformat()
        conf["notified"] = False
        self._save()
        logger.info("Guild %s: next bump at %s", message.guild.id, next_at.isoformat())

    # ---------- 通知ループ ----------

    @tasks.loop(seconds=30)
    async def check_reminders(self):
        now = datetime.now(timezone.utc)
        changed = False

        for guild_id, conf in list(self.config.items()):
            if conf.get("notified", True) or not conf.get("next_bump_at"):
                continue

            try:
                next_at = datetime.fromisoformat(conf["next_bump_at"])
            except (TypeError, ValueError):
                logger.warning("Guild %s: invalid next_bump_at, skipping", guild_id)
                conf["notified"] = True
                changed = True
                continue

            if next_at > now:
                continue

            conf["notified"] = True
            changed = True
            await self._send_reminder(guild_id, conf)

        if changed:
            self._save()

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    async def _send_reminder(self, guild_id: str, conf: dict[str, Any]) -> None:
        channel_id = conf.get("channel_id")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            logger.warning("Guild %s: reminder channel %s is unavailable", guild_id, channel_id)
            return

        role_id = conf.get("role_id")
        embed = discord.Embed(
            title="bumpの時間です",
            description=":arrow_up: `/bump` を実行してサーバーの表示順を上げましょう！",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="bumpは2時間ごとに実行できます")

        try:
            await channel.send(
                content=f"<@&{role_id}>" if role_id else None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.HTTPException as e:
            logger.error("Guild %s: failed to send reminder: %s", guild_id, e)

    # ---------- 設定コマンド ----------

    @group.command(name="setup", description="リマインダーの通知先を設定します")
    @app_commands.describe(
        channel="リマインダーを送るチャンネル",
        role="メンションするロール (省略するとメンションなし)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_reminder(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role | None = None,
    ):
        conf = self.config.setdefault(str(interaction.guild_id), {})
        conf["channel_id"] = channel.id
        conf["role_id"] = role.id if role else None
        conf.setdefault("next_bump_at", None)
        conf.setdefault("notified", True)
        self._save()

        embed = discord.Embed(
            title="bumpリマインダーを設定しました",
            description=(
                f"通知先: {channel.mention}\n"
                f"メンション: {role.mention if role else 'なし'}\n\n"
                "次に `/bump` が実行されると、2時間後にここへ通知します。"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)
        logger.info(
            "Guild %s: reminder set to channel %s (role: %s)",
            interaction.guild_id,
            channel.id,
            role.id if role else None,
        )

    @group.command(name="off", description="リマインダーを無効にします")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable_reminder(self, interaction: discord.Interaction):
        removed = self.config.pop(str(interaction.guild_id), None)
        if removed is None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="設定されていません",
                    description="このサーバーではリマインダーが有効になっていません。",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        self._save()
        await interaction.response.send_message(
            embed=discord.Embed(
                title="bumpリマインダーを無効にしました",
                description="再度有効にするには `/bump-reminder setup` を実行してください。",
                color=discord.Color.green(),
            )
        )

    @group.command(name="status", description="リマインダーの状態を表示します")
    async def status(self, interaction: discord.Interaction):
        conf = self.config.get(str(interaction.guild_id))
        if conf is None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="未設定",
                    description="`/bump-reminder setup` で通知先を設定してください。",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        channel_id = conf.get("channel_id")
        role_id = conf.get("role_id")
        next_at_raw = conf.get("next_bump_at")

        if next_at_raw:
            next_at = datetime.fromisoformat(next_at_raw)
            if next_at <= datetime.now(timezone.utc):
                next_text = "いま実行できます"
            else:
                next_text = f"{discord.utils.format_dt(next_at, 'R')} ({discord.utils.format_dt(next_at, 'T')})"
        else:
            next_text = "まだbumpを検知していません"

        embed = discord.Embed(
            title="bumpリマインダーの状態",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="通知先", value=f"<#{channel_id}>" if channel_id else "なし", inline=False)
        embed.add_field(name="メンション", value=f"<@&{role_id}>" if role_id else "なし", inline=False)
        embed.add_field(name="次のbump", value=next_text, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="権限エラー",
                    description=":no_entry: このコマンドには「サーバー管理」権限が必要です。",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
        else:
            logger.error("Unexpected error in bump-reminder command: %s", error)
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(BumpReminder(bot))
