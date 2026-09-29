"""
常設パネル

チャンネルに貼りっぱなしにするビュー。再起動後もボタンが動くように
  - timeout=None
  - すべてのボタンに固定の custom_id
  - main.py の setup_hook で add_view() する
という3点を必ず守る。

⚠️ custom_id は永久に変えないこと。変えると、すでに貼ってあるパネルの
   ボタンが反応しなくなる。命名規則は panel:<パネル名>:<動作>。
"""

from __future__ import annotations

import logging

import discord

import emoji as E
from core import settings
from core import users as user_repo
from ui import embeds

log = logging.getLogger("bot.panels")


def is_admin(interaction: discord.Interaction) -> bool:
    bot = interaction.client
    if interaction.user.id in (bot.owner_ids or set()):
        return True
    roles = getattr(interaction.user, "roles", [])
    admin_roles = getattr(bot, "admin_role_ids", set())
    if any(r.id in admin_roles for r in roles):
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def guard_user(interaction: discord.Interaction) -> bool:
    """利用者向けボタンの共通チェック。使えないときは理由を返して False。"""
    if await user_repo.is_banned(interaction.user.id):
        await interaction.response.send_message(
            embed=embeds.error("ご利用が停止されています。", title=f"{E.BAN} 利用停止"),
            ephemeral=True,
        )
        return False
    if settings.get("maintenance", False):
        await interaction.response.send_message(
            embed=embeds.warn(
                "ただいまメンテナンス中です。しばらくお待ちください。",
                title=f"{E.MAINTENANCE} メンテナンス中",
            ),
            ephemeral=True,
        )
        return False
    return True


# ============================================================
#  注文パネル
# ============================================================

class OrderPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="注文する", emoji=E.BURGER,
        style=discord.ButtonStyle.success, custom_id="panel:order:start",
    )
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import flows

        if not await guard_user(interaction):
            return
        await flows.start_order(interaction)

    @discord.ui.button(
        label="注文コードを作る", emoji=E.RECEIPT,
        style=discord.ButtonStyle.secondary, custom_id="panel:order:hex",
    )
    async def make_hex(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import flows

        if not await guard_user(interaction):
            return
        await flows.start_hex_builder(interaction)

    @discord.ui.button(
        label="履歴", emoji=E.HISTORY,
        style=discord.ButtonStyle.secondary, custom_id="panel:order:history",
    )
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import flows

        await flows.show_history(interaction)


# ============================================================
#  チャージパネル
# ============================================================

class ChargePanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="チャージする", emoji=E.CHARGE,
        style=discord.ButtonStyle.primary, custom_id="panel:charge:start",
    )
    async def charge(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import flows

        if not await guard_user(interaction):
            return
        await flows.open_charge_modal(interaction)

    @discord.ui.button(
        label="残高を確認", emoji=E.WALLET,
        style=discord.ButtonStyle.secondary, custom_id="panel:charge:balance",
    )
    async def balance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import flows

        await flows.show_balance(interaction)


# ============================================================
#  管理者パネル
# ============================================================

class AdminPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction):
            return True
        await interaction.response.send_message(
            embed=embeds.error("この操作は管理者のみ行えます。", title=f"{E.BAN} 権限がありません"),
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="統計", emoji=E.CHART,
        style=discord.ButtonStyle.primary, custom_id="panel:admin:stats",
    )
    async def stats(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.show_stats(interaction)

    @discord.ui.button(
        label="負担率", emoji=E.YEN,
        style=discord.ButtonStyle.secondary, custom_id="panel:admin:subsidy",
    )
    async def subsidy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.show_subsidy(interaction)

    @discord.ui.button(
        label="アカウント", emoji=E.KEY,
        style=discord.ButtonStyle.secondary, custom_id="panel:admin:accounts",
    )
    async def accounts(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.show_accounts(interaction)

    @discord.ui.button(
        label="要確認", emoji=E.WARN,
        style=discord.ButtonStyle.danger, custom_id="panel:admin:review", row=1,
    )
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.show_review(interaction)

    @discord.ui.button(
        label="メニュー同期", emoji=E.SYNC,
        style=discord.ButtonStyle.secondary, custom_id="panel:admin:menusync", row=1,
    )
    async def menusync(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.sync_menus(interaction)

    @discord.ui.button(
        label="更新", emoji=E.SYNC,
        style=discord.ButtonStyle.secondary, custom_id="panel:admin:refresh", row=1,
    )
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.refresh_admin_panel(interaction)


# main.py の setup_hook が、ここに並んだビューを add_view() で復元する
PERSISTENT_VIEWS = [OrderPanel, ChargePanel, AdminPanel]

PANEL_BUILDERS = {
    "order": (embeds.order_panel, OrderPanel),
    "charge": (embeds.charge_panel, ChargePanel),
}
