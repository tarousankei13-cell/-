import discord
from discord import app_commands
from discord.ext import commands
from config import Config
import utils.embeds as E
from utils.views import TicketCreateModal, TicketControlView


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Register the persistent view so buttons survive bot restarts
        bot.add_view(TicketControlView())

    @property
    def db(self):
        return self.bot.db

    # ── /ticket ────────────────────────────────────────────────────────────

    ticket_group = app_commands.Group(name="ticket", description="サポートチケット")

    @ticket_group.command(name="create", description="サポートチケットを作成します")
    async def ticket_create(self, interaction: discord.Interaction):
        user = await self.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return
        await interaction.response.send_modal(TicketCreateModal())

    @ticket_group.command(name="close", description="現在のチケットを閉じます")
    async def ticket_close(self, interaction: discord.Interaction):
        ticket = await self.db.get_ticket_by_channel(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(embed=E.error("これはチケットチャンネルではありません"), ephemeral=True)
            return

        is_owner = interaction.user.id == ticket["user_id"]
        is_staff = any(r.name in (Config.STAFF_ROLE, Config.ADMIN_ROLE) for r in interaction.user.roles)
        if not (is_owner or is_staff or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return

        await self.db.close_ticket(ticket["id"])
        embed = discord.Embed(
            title="🔒  チケットを閉じました",
            description=f"クローズ実行者: {interaction.user.mention}\n5秒後にチャンネルを削除します。",
            color=Config.COLOR_ERROR
        )
        await interaction.response.send_message(embed=embed)
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")

    @ticket_group.command(name="list", description="オープン中のチケット一覧（スタッフ専用）")
    async def ticket_list(self, interaction: discord.Interaction):
        is_staff = (
            interaction.user.guild_permissions.administrator
            or any(r.name in (Config.STAFF_ROLE, Config.ADMIN_ROLE) for r in interaction.user.roles)
        )
        if not is_staff:
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        tickets = await self.db.get_open_tickets()
        if not tickets:
            await interaction.followup.send(embed=E.info("チケットなし", "現在オープン中のチケットはありません。"), ephemeral=True)
            return

        embed = discord.Embed(title="🎫  オープン中のチケット", color=Config.COLOR_INFO)
        for t in tickets:
            ch = interaction.guild.get_channel(t["channel_id"])
            ch_mention = ch.mention if ch else f"（削除済み: {t['channel_id']}）"
            embed.add_field(
                name=f"#{t['id']:04d}  {t['subject'][:50]}",
                value=f"ユーザー: <@{t['user_id']}>  |  {ch_mention}  |  作成: {t['created_at'][:10]}",
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Panel command (sends a button to open a ticket) ────────────────────

    @app_commands.command(name="ticket_panel", description="チケット作成パネルを設置します（管理者専用）")
    async def ticket_panel(self, interaction: discord.Interaction):
        if not (interaction.user.guild_permissions.administrator or any(r.name == Config.ADMIN_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return

        embed = discord.Embed(
            title="🎫  サポートチケット",
            description="ご不明な点・ご要望がございましたら、下のボタンからチケットを作成してください。\nスタッフが対応いたします。",
            color=Config.COLOR_INFO
        )
        view = TicketPanelView()
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(embed=E.success("パネルを設置しました"), ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 チケットを作成", style=discord.ButtonStyle.primary, custom_id="ticket:create_panel")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = await interaction.client.db.get_user(interaction.user.id)
        if user["is_banned"]:
            await interaction.response.send_message(embed=E.error("アクセス拒否"), ephemeral=True)
            return
        await interaction.response.send_modal(TicketCreateModal())


async def setup(bot):
    bot.add_view(TicketPanelView())
    await bot.add_cog(Tickets(bot))
