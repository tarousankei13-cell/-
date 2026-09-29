"""管理者コマンドの共通チェック"""

from __future__ import annotations

import discord
from discord import app_commands

import emoji as E
from ui import embeds


def is_admin_user(interaction: discord.Interaction) -> bool:
    bot = interaction.client
    if interaction.user.id in (bot.owner_ids or set()):
        return True
    roles = getattr(interaction.user, "roles", [])
    if any(r.id in getattr(bot, "admin_role_ids", set()) for r in roles):
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_admin_user(interaction)
    return app_commands.check(predicate)


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id in (interaction.client.owner_ids or set())
    return app_commands.check(predicate)


async def handle_check_failure(interaction: discord.Interaction, error: Exception) -> bool:
    """権限エラーなら案内を返す。処理したら True。"""
    if isinstance(error, app_commands.CheckFailure):
        embed = embeds.error(
            "このコマンドは管理者のみ使用できます。", title=f"{E.BAN} 権限がありません"
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return True
    return False
