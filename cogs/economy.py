import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ── /balance ───────────────────────────────────────────────────────────

    @app_commands.command(name="balance", description="残高を確認します")
    @app_commands.describe(user="確認するユーザー（省略時：自分）")
    async def balance(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        u = await self.db.get_user(target.id)
        tier = await self.db.get_user_rank_tier(target.id)
        embed = E.balance_embed(target, u, tier)
        await interaction.response.send_message(embed=embed, ephemeral=(user is None))

    # ── /daily ─────────────────────────────────────────────────────────────

    @app_commands.command(name="daily", description="デイリーボーナスを受け取ります")
    async def daily(self, interaction: discord.Interaction):
        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return

        result = await self.db.claim_daily(interaction.user.id)
        if not result["success"]:
            await interaction.response.send_message(
                embed=E.error("既に受け取り済み", "デイリーボーナスは1日1回のみです。明日またどうぞ！"),
                ephemeral=True
            )
            return

        updated = await self.db.get_user(interaction.user.id)
        embed = discord.Embed(
            title="🎁  デイリーボーナス！",
            color=Config.COLOR_SUCCESS
        )
        embed.add_field(name="基本ボーナス",    value=f"+{Config.DAILY_REWARD:,} {Config.CURRENCY_NAME}", inline=True)
        if result["bonus"] > 0:
            embed.add_field(name="VIPボーナス", value=f"+{result['bonus']:,} {Config.CURRENCY_NAME}", inline=True)
        if result["streak_bonus"] > 0:
            embed.add_field(name="🔥 ストリーク", value=f"+{result['streak_bonus']:,} {Config.CURRENCY_NAME}", inline=True)
        embed.add_field(name="合計獲得",        value=f"**+{result['reward']:,}** {Config.CURRENCY_NAME}", inline=False)
        embed.add_field(name="🔥 連続日数",     value=f"**{result['streak']}** 日", inline=True)
        embed.add_field(name="現在の残高",      value=f"**{updated['balance']:,}** {Config.CURRENCY_NAME}", inline=True)
        embed.set_footer(text="Shop Bot • 明日またどうぞ！")
        await interaction.response.send_message(embed=embed)

        # Check achievements (streak achievements)
        new_achievements = await self.db.check_and_grant_achievements(interaction.user.id)
        if new_achievements:
            from utils.views import _notify_achievements
            await _notify_achievements(interaction.client, interaction.user, new_achievements)

    # ── /transfer ──────────────────────────────────────────────────────────

    @app_commands.command(name="transfer", description="他のユーザーにポイントを送ります")
    @app_commands.describe(user="送り先ユーザー", amount="送金額")
    async def transfer(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if user.id == interaction.user.id:
            await interaction.response.send_message(embed=E.error("送金エラー", "自分自身には送金できません。"), ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message(embed=E.error("送金エラー", "Botには送金できません。"), ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(embed=E.error("無効な金額"), ephemeral=True)
            return

        sender = await self.db.get_user(interaction.user.id)
        if sender["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return
        if sender["balance"] < amount:
            await interaction.response.send_message(
                embed=E.error("残高不足", f"残高: **{sender['balance']:,}** {Config.CURRENCY_NAME}"),
                ephemeral=True
            )
            return

        await self.db.update_balance(interaction.user.id, -amount)
        await self.db.update_balance(user.id, amount)
        await self.db.add_transaction(interaction.user.id, -amount, "transfer_out", f"{user} への送金")
        await self.db.add_transaction(user.id, amount, "transfer_in", f"{interaction.user} からの受け取り")

        embed = discord.Embed(title="💸  送金完了", description=f"{user.mention} に **{amount:,}** {Config.CURRENCY_NAME} を送りました。", color=Config.COLOR_SUCCESS)
        sender_updated = await self.db.get_user(interaction.user.id)
        embed.add_field(name="残高", value=f"{sender_updated['balance']:,} {Config.CURRENCY_NAME}", inline=True)
        await interaction.response.send_message(embed=embed)

        try:
            dm = discord.Embed(title="💰  ポイントを受け取りました！", description=f"**{interaction.user.display_name}** から **{amount:,}** {Config.CURRENCY_NAME} を受け取りました。", color=Config.COLOR_SUCCESS)
            await user.send(embed=dm)
        except Exception:
            pass

    # ── /leaderboard ───────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="ランキングを表示します")
    @app_commands.describe(mode="ランキング種別")
    @app_commands.choices(mode=[
        app_commands.Choice(name="残高ランキング",  value="balance"),
        app_commands.Choice(name="購入額ランキング", value="spending"),
    ])
    async def leaderboard(self, interaction: discord.Interaction, mode: str = "balance"):
        if mode == "spending":
            rows = await self.db.get_spending_rank()
        else:
            rows = await self.db.get_balance_rank()
        embed = E.leaderboard_embed(rows, interaction.guild, mode)
        await interaction.response.send_message(embed=embed)

    # ── /history ───────────────────────────────────────────────────────────

    @app_commands.command(name="history", description="取引履歴を表示します")
    async def history(self, interaction: discord.Interaction):
        rows = await self.db.get_transactions(interaction.user.id, limit=15)
        embed = E.transaction_history(rows, interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Economy(bot))
