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


def self_checked():
    """
    「このコマンドは、自分の中で相手を見て判断する」ことを表す印。

    管理者以外も使うコマンド（例：お問い合わせを閉じる）に付ける。
    ⚠️ **判定そのものは関数の中で必ず行うこと。** これは印であって、
       これだけでは誰でも実行できてしまう。
    ⚠️ 権限の確認を忘れたコマンドと見分けるために要る。
       付いていないコマンドは、安全性の検査（tests/test_security.py）で
       「権限チェックが無い」として落ちる。

    最低限、サーバーの中であることだけはここで確かめる
    （DMから呼ばれると、ロールを見る処理が成り立たないため）。
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.guild is not None
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
