import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E


class Ranks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ── /rank ──────────────────────────────────────────────────────────────

    @app_commands.command(name="rank", description="自分のVIPランクを確認します")
    @app_commands.describe(user="確認するユーザー（省略時：自分）")
    async def rank(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        u = await self.db.get_user(target.id)
        tier = await self.db.get_user_rank_tier(target.id)
        next_tier = await self.db.get_next_rank_tier(target.id)

        embed = discord.Embed(
            title=f"{tier['emoji']}  {target.display_name} のランク",
            color=tier["color"],
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="現在のランク", value=f"{tier['emoji']} **{tier['name']}**", inline=True)
        embed.add_field(name="累計購入",      value=f"{u['total_spent']:,} {Config.CURRENCY_NAME}", inline=True)
        embed.add_field(name="現在の残高",    value=f"{u['balance']:,} {Config.CURRENCY_NAME}", inline=True)

        if tier["perks"]:
            embed.add_field(name="✨ 特典", value=tier["perks"], inline=False)

        if next_tier:
            needed = next_tier["min_spent"] - u["total_spent"]
            progress = min(int((u["total_spent"] - tier["min_spent"]) / max(next_tier["min_spent"] - tier["min_spent"], 1) * 20), 20)
            bar = "█" * progress + "░" * (20 - progress)
            embed.add_field(
                name=f"次のランク: {next_tier['emoji']} {next_tier['name']}",
                value=f"`{bar}` あと **{needed:,}** {Config.CURRENCY_NAME}",
                inline=False
            )
        else:
            embed.add_field(name="🏆 最高ランク到達！", value="おめでとうございます！最高ランクです。", inline=False)

        embed.set_footer(text=f"デイリーボーナス +{tier['daily_bonus']} {Config.CURRENCY_NAME} | Shop Bot")
        await interaction.response.send_message(embed=embed)

    # ── /ranks ─────────────────────────────────────────────────────────────

    @app_commands.command(name="ranks", description="ランク一覧と特典を表示します")
    async def ranks_list(self, interaction: discord.Interaction):
        tiers = await self.db.get_rank_tiers()
        user = await self.db.get_user(interaction.user.id)
        current_tier = await self.db.get_user_rank_tier(interaction.user.id)

        embed = discord.Embed(
            title="🏆  VIPランク一覧",
            description="累計購入金額に応じてランクが上がり、特典が増えます！",
            color=Config.COLOR_GOLD,
        )
        for t in tiers:
            is_current = current_tier and t["id"] == current_tier["id"]
            marker = " ← 現在" if is_current else ""
            embed.add_field(
                name=f"{t['emoji']} **{t['name']}**{marker}",
                value=f"必要累計: **{t['min_spent']:,}** {Config.CURRENCY_NAME}\nデイリーボーナス: +{t['daily_bonus']}\n{t['perks'] or '—'}",
                inline=True
            )
        await interaction.response.send_message(embed=embed)

    # ── Admin: set rank role IDs ───────────────────────────────────────────

    @app_commands.command(name="rank_setrole", description="ランクに紐付けるロールを設定します（管理者専用）")
    @app_commands.describe(tier_id="ランクID（/ranks で確認）", role="付与するロール")
    async def rank_setrole(self, interaction: discord.Interaction, tier_id: int, role: discord.Role):
        if not (interaction.user.guild_permissions.administrator or any(r.name == Config.ADMIN_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        tier = await self.db.get_rank_tier(tier_id)
        if not tier:
            await interaction.response.send_message(embed=E.error("ランクが見つかりません"), ephemeral=True)
            return
        await self.db.update_rank_tier(tier_id, role_id=role.id)
        await interaction.response.send_message(
            embed=E.success("ロールを設定しました", f"**{tier['name']}** → {role.mention}"),
            ephemeral=True
        )

    # ── Helper: apply rank role to guild member ────────────────────────────

    @staticmethod
    async def apply_rank_role(guild: discord.Guild, member: discord.Member, new_tier, old_tier_id: int, db):
        """Remove old rank role, add new rank role. Returns True if rank changed."""
        if not new_tier:
            return False
        if new_tier["id"] == old_tier_id:
            return False

        tiers = await db.get_rank_tiers()

        # Remove all old rank roles
        for t in tiers:
            if t["role_id"]:
                role = guild.get_role(t["role_id"])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="ランク変更")
                    except Exception:
                        pass

        # Add new rank role
        if new_tier["role_id"]:
            role = guild.get_role(new_tier["role_id"])
            if role:
                try:
                    await member.add_roles(role, reason=f"ランクアップ: {new_tier['name']}")
                    return True
                except Exception:
                    pass
        return True


async def setup(bot):
    await bot.add_cog(Ranks(bot))
