import asyncio
import os
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.members = True

class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = Bot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="banall", description="サーバーの全メンバーをBANします（自分とBotは除外）")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(reason="BANの理由", confirm="実行確認のため 'confirm' と入力してください")
async def ban_all(interaction: discord.Interaction, confirm: str, reason: str = "banall コマンドによる一括BAN"):
    if confirm.lower() != "confirm":
        await interaction.response.send_message(
            "キャンセルしました。実行するには `confirm` と入力してください。",
            ephemeral=True,
        )
        return

    if not interaction.guild.me.guild_permissions.ban_members:
        await interaction.response.send_message(
            "Botにバン権限がありません。",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("BANを開始します...", ephemeral=True)

    members = [
        m for m in interaction.guild.members
        if m.id != bot.user.id and m.id != interaction.user.id
    ]

    banned = 0
    failed = 0

    for member in members:
        try:
            await member.ban(reason=reason, delete_message_days=0)
            banned += 1
            # Discordのレート制限を避けるため少し待機
            await asyncio.sleep(0.5)
        except discord.Forbidden:
            failed += 1
        except discord.HTTPException:
            failed += 1

    await interaction.followup.send(
        f"完了: {banned} 人をBANしました。失敗: {failed} 人。",
        ephemeral=True,
    )


@ban_all.error
async def ban_all_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "このコマンドを実行するには管理者権限が必要です。",
            ephemeral=True,
        )


bot.run(os.environ["DISCORD_TOKEN"])
