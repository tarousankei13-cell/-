import discord
from discord import app_commands
import os
import sys
import subprocess


TOKEN = os.environ.get("DISCORD_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))  # 再起動を許可するユーザーID

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="restart", description="Botを再起動します（管理者専用）")
async def restart(interaction: discord.Interaction):
    if ALLOWED_USER_ID != 0 and interaction.user.id != ALLOWED_USER_ID:
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return

    await interaction.response.send_message("Botを再起動します...", ephemeral=True)

    # 現在のプロセスをPythonで再実行
    os.execv(sys.executable, [sys.executable] + sys.argv)


@client.event
async def on_ready():
    await tree.sync()
    print(f"ログイン成功: {client.user} (ID: {client.user.id})")
    print("スラッシュコマンドを同期しました")


client.run(TOKEN)
