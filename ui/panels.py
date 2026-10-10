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
from ui.gate import GuardedView

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

class OrderPanel(GuardedView):
    """
    注文パネル。

    設置するときは現在の注文方式に合わせてボタンを出し分ける。
    一方、起動時の復元（add_view）では**すべてのボタンを登録**しておく。
    こうしておくと、設定を変えても既に貼ってあるパネルのボタンが死なない。
    """

    def __init__(self, mode: str | None = None) -> None:
        super().__init__(timeout=None)
        if mode is None:
            mode = "both"          # 復元時は全ボタンを登録する
            show_hex_builder = True
        else:
            show_hex_builder = mode != "menu"
        if not show_hex_builder:
            self.remove_item(self.make_hex)

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
        if settings.get("order_mode", "both") == "menu":
            await interaction.response.send_message(
                embed=embeds.info(
                    "このサーバーでは注文コードを使わない設定になっています。\n"
                    f"{E.BURGER}「注文する」からメニューを選んでご注文ください。"
                ),
                ephemeral=True,
            )
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

class ChargePanel(GuardedView):
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

class AdminPanel(GuardedView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def allow(self, interaction: discord.Interaction) -> bool:
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
        label="お知らせを送る", emoji=E.BELL,
        style=discord.ButtonStyle.primary, custom_id="panel:admin:broadcast", row=2,
    )
    async def broadcast(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.start_broadcast(interaction, "dm")

    @discord.ui.button(
        label="利用者を調べる", emoji=E.USER,
        style=discord.ButtonStyle.secondary, custom_id="panel:admin:finduser", row=2,
    )
    async def finduser(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=embeds.info(
                "`/admin user @利用者` で、その方の残高・利用状況・"
                "適用中の負担率をまとめて確認できます。"
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="更新", emoji=E.SYNC,
        style=discord.ButtonStyle.secondary, custom_id="panel:admin:refresh", row=1,
    )
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import admin_flows

        await admin_flows.refresh_admin_panel(interaction)


class InvitePanel(GuardedView):
    """
    紹介プログラムの常設パネル。

    ・招待リンクを発行（本人専用・1サーバーにつき1本）
    ・プロモコード入力（手でコードを貼る方）
    ・DMを再送信（受取ボタンのDMが届かなかった方）
    ・紹介状況（何名達成したか・次の特典まであと何名か）
    ・通知設定（お知らせを受け取るかどうか）
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="招待リンクを発行", emoji=E.CHARGE,
        style=discord.ButtonStyle.primary, custom_id="panel:invite:link", row=0,
    )
    async def issue(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import invite_flows

        await invite_flows.issue_link(interaction)

    @discord.ui.button(
        label="プロモコード入力", emoji=E.TICKET,
        style=discord.ButtonStyle.success, custom_id="panel:invite:enter", row=0,
    )
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import invite_flows

        await invite_flows.open_code_modal(interaction)

    @discord.ui.button(
        label="DMを再送信", emoji=E.MAIL,
        style=discord.ButtonStyle.secondary, custom_id="panel:invite:resend", row=1,
    )
    async def resend(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import invite_flows

        await invite_flows.resend_dm(interaction)

    @discord.ui.button(
        label="紹介状況", emoji=E.CHART,
        style=discord.ButtonStyle.secondary, custom_id="panel:invite:status", row=1,
    )
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import invite_flows

        await invite_flows.show_status(interaction)

    @discord.ui.button(
        label="通知設定", emoji=E.BELL,
        style=discord.ButtonStyle.secondary, custom_id="panel:invite:notify", row=1,
    )
    async def notify(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import invite_flows

        await invite_flows.toggle_notify(interaction)


# main.py の setup_hook が、ここに並んだビューを add_view() で復元する
# ⚠️ ClaimView は DM に出す。再起動したあとも押せるように、
#    ここに並べて add_view() で復元する。
from ui.invite_flows import ClaimView  # noqa: E402

# サーバー管理（チケット・認証）のボタンも同じ仕組みで復元する
from ui.nudge_views import PERSISTENT_VIEWS as _NUDGE_VIEWS  # noqa: E402
from ui.server_views import (  # noqa: E402
    PERSISTENT_VIEWS as _SERVER_VIEWS, TicketPanel, VerifyPanel,
)

PERSISTENT_VIEWS = [
    OrderPanel, ChargePanel, AdminPanel, InvitePanel, ClaimView,
    *_SERVER_VIEWS, *_NUDGE_VIEWS,
]

def build_order_panel() -> tuple[discord.Embed, discord.ui.View]:
    """設置・貼り直し用。現在の注文方式を反映したパネルを作る。"""
    mode = settings.get("order_mode", "both")
    return embeds.order_panel(mode), OrderPanel(mode)


PANEL_BUILDERS = {
    "order": (embeds.order_panel, OrderPanel),
    "charge": (embeds.charge_panel, ChargePanel),
    "invite": (embeds.invite_panel, InvitePanel),
    "ticket": (embeds.ticket_panel, TicketPanel),
    "verify": (embeds.verify_panel, VerifyPanel),
}

# ⚠️ 中身を作るのに DB を読むパネル。PANEL_BUILDERS（同期）には置けない。
#    /panel refresh と定期更新は、こちらを使う。
ASYNC_PANEL_BUILDERS = {
    "ranking": embeds.ranking_panel,
}
