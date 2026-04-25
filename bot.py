import asyncio
import discord
from discord import app_commands

# ===== トークンをここに入力 =====
TOKEN = "ここにトークン"
# ================================

intents = discord.Intents.default()
intents.members = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("スラッシュコマンドを同期しました。")


bot = Bot()


@bot.event
async def on_ready():
    print(f"ログイン成功: {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(activity=discord.Game(name="/banall"))


# ─────────────────────────────────────────────
#  /banall  サーバーの全メンバーをBAN
# ─────────────────────────────────────────────
@bot.tree.command(
    name="banall",
    description="⚠️ サーバーの全メンバーをBANします（Bot・実行者は除外）",
)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    confirm="確認のため 'confirm' と入力",
    reason="BANの理由（省略可）",
)
async def ban_all(
    interaction: discord.Interaction,
    confirm: str,
    reason: str = "banall コマンドによる一括BAN",
):
    # 確認入力チェック
    if confirm.lower() != "confirm":
        embed = discord.Embed(
            title="❌ キャンセル",
            description="`confirm` パラメータに **confirm** と入力してください。",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Botの権限チェック
    if not interaction.guild.me.guild_permissions.ban_members:
        embed = discord.Embed(
            title="❌ 権限不足",
            description="BotにBAN権限がありません。",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # BAN対象メンバー一覧（Bot・実行者を除外）
    targets = [
        m for m in interaction.guild.members
        if m.id != bot.user.id and m.id != interaction.user.id
    ]
    total = len(targets)

    if total == 0:
        embed = discord.Embed(
            title="ℹ️ 対象者なし",
            description="BANするメンバーがいません。",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # 開始メッセージ
    embed_start = discord.Embed(
        title="🔨 BAN実行中...",
        description=f"対象: **{total}人**\n理由: {reason}",
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed_start, ephemeral=True)

    banned = 0
    failed = 0

    for i, member in enumerate(targets, 1):
        try:
            await member.ban(reason=reason, delete_message_days=0)
            banned += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

        # 10人ごとに進捗を更新
        if i % 10 == 0 or i == total:
            embed_progress = discord.Embed(
                title="🔨 BAN実行中...",
                description=f"進捗: {i}/{total}\n✅ 成功: {banned}人　❌ 失敗: {failed}人",
                color=discord.Color.orange(),
            )
            await interaction.edit_original_response(embed=embed_progress)

        await asyncio.sleep(0.5)  # レート制限対策

    # 完了メッセージ
    embed_done = discord.Embed(
        title="✅ BAN完了",
        description=(
            f"**結果**\n"
            f"✅ 成功: **{banned}人**\n"
            f"❌ 失敗: **{failed}人**\n"
            f"理由: {reason}"
        ),
        color=discord.Color.green(),
    )
    await interaction.edit_original_response(embed=embed_done)


@ban_all.error
async def ban_all_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        embed = discord.Embed(
            title="❌ 権限不足",
            description="このコマンドは **管理者** のみ使用できます。",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        embed = discord.Embed(
            title="❌ エラー",
            description=f"予期しないエラーが発生しました。\n`{error}`",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


bot.run(TOKEN)
