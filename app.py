import discord
from discord import app_commands
import os
import sys


TOKEN = os.environ.get("DISCORD_TOKEN", "")
OWNER_ID = 1324938326741876758

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


@tree.command(name="restart", description="Botを再起動します（オーナー専用）")
async def restart(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return

    await interaction.response.send_message("Botを再起動します...", ephemeral=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@client.event
async def on_ready():
    await tree.sync()
    print(f"ログイン成功: {client.user} (ID: {client.user.id})")
    print("スラッシュコマンドを同期しました")


client.run(TOKEN)
