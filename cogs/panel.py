"""パネルの設置"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import emoji as E
from core import settings
from db.models import Panel
from db.session import session_scope
from cogs._checks import admin_only, handle_check_failure
from ui import admin_flows, embeds, panels

log = logging.getLogger("bot.cogs.panel")


async def refresh_all(bot: commands.Bot) -> list[str]:
    """
    設置済みパネルの文言とボタンを最新にする。

    `/panel refresh` からも、起動時からも、ここだけを通す。
    別々に書くと、片方を直し忘れて食い違う。

    戻り値は1行ずつの結果（表示用）。例外は外へ出さない。
    """
    async with session_scope() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(Panel))).scalars().all()
        targets = [(r.kind, r.channel_id, r.message_id) for r in rows]

    if not targets:
        return []

    stats = await admin_flows.collect_stats()
    builders = {
        "order": panels.build_order_panel(),
        "charge": (embeds.charge_panel(), panels.ChargePanel()),
        "admin": (embeds.admin_panel(stats), panels.AdminPanel()),
        "invite": (embeds.invite_panel(), panels.InvitePanel()),
    }

    lines: list[str] = []
    for kind, channel_id, message_id in targets:
        channel = bot.get_channel(channel_id)
        if channel is None or kind not in builders:
            lines.append(f"{E.NG} {kind}: チャンネルが見つかりません")
            continue
        embed, view = builders[kind]
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed, view=view)
            lines.append(f"{E.OK} {kind}: 更新しました")
        except discord.NotFound:
            lines.append(f"{E.NG} {kind}: メッセージが見つかりません（再設置してください）")
        except discord.HTTPException as e:
            lines.append(f"{E.NG} {kind}: {e}")
    return lines


class PanelCog(commands.Cog):
    """利用者パネル・管理者パネルの設置"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(name="panel", description="パネルを設置します（管理者用）")

    async def _deploy(
        self, interaction: discord.Interaction, kind: str,
        channel: discord.TextChannel, embed: discord.Embed, view: discord.ui.View,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.error(
                    f"{channel.mention} にメッセージを送信する権限がありません。"
                ),
                ephemeral=True,
            )
            return

        async with session_scope() as s:
            row = await s.get(Panel, kind)
            if row is None:
                row = Panel(
                    kind=kind, guild_id=channel.guild.id, channel_id=channel.id,
                    message_id=message.id, deployed_by=interaction.user.id,
                )
                s.add(row)
            else:
                row.guild_id = channel.guild.id
                row.channel_id = channel.id
                row.message_id = message.id
                row.deployed_by = interaction.user.id

        await interaction.followup.send(
            embed=embeds.ok(f"{channel.mention} にパネルを設置しました。"), ephemeral=True
        )

    @group.command(name="order", description="注文パネルを設置します")
    @app_commands.describe(channel="設置するチャンネル")
    @admin_only()
    async def order(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        embed, view = panels.build_order_panel()
        await self._deploy(interaction, "order", channel, embed, view)

    @group.command(name="charge", description="チャージパネルを設置します")
    @app_commands.describe(channel="設置するチャンネル")
    @admin_only()
    async def charge(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await self._deploy(
            interaction, "charge", channel, embeds.charge_panel(), panels.ChargePanel()
        )
        await settings.set_value("channel_charge", channel.id, updated_by=interaction.user.id)

    @group.command(name="invite", description="招待キャンペーンのパネルを設置します")
    @app_commands.describe(channel="設置するチャンネル")
    @admin_only()
    async def invite(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        from core import invite as inv

        await self._deploy(
            interaction, "invite", channel, embeds.invite_panel(), panels.InvitePanel()
        )
        if not inv.enabled():
            await interaction.followup.send(
                embed=embeds.warn(
                    "パネルは設置しましたが、キャンペーンはまだ始まっていません。\n"
                    "`/config campaign start` で特典の額と上限を決めてください。"
                ),
                ephemeral=True,
            )

    @group.command(name="ticket", description="お問い合わせパネルを設置します")
    @app_commands.describe(channel="設置するチャンネル")
    @admin_only()
    async def ticket(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        from services.server import tickets

        await self._deploy(
            interaction, "ticket", channel,
            embeds.ticket_panel(), panels.TicketPanel(),
        )
        # スレッド方式のときは、このチャンネルの中にスレッドを作る
        if tickets.mode() == "thread":
            await settings.set_value(
                "ticket_channel", channel.id, updated_by=interaction.user.id,
            )

    @group.command(name="verify", description="認証パネルを設置します")
    @app_commands.describe(channel="設置するチャンネル")
    @admin_only()
    async def verify(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        await self._deploy(
            interaction, "verify", channel,
            embeds.verify_panel(), panels.VerifyPanel(),
        )

    @group.command(name="admin", description="管理者パネルを設置します")
    @app_commands.describe(channel="設置するチャンネル")
    @admin_only()
    async def admin(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        stats = await admin_flows.collect_stats()
        await self._deploy(
            interaction, "admin", channel, embeds.admin_panel(stats), panels.AdminPanel()
        )

    @group.command(name="refresh", description="設置済みパネルの文言を最新にします")
    @admin_only()
    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = await refresh_all(self.bot)
        if not lines:
            await interaction.followup.send(
                embed=embeds.info("設置済みのパネルがありません。"), ephemeral=True
            )
            return

        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.SYNC} パネルの更新", description="\n".join(lines), color=embeds.BLUE
            ),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("panel コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCog(bot))
