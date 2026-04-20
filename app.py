import discord
from discord import app_commands
from discord.ext import tasks
import os
import sys
import json
import asyncio
import time
from datetime import timedelta


TOKEN = "ここに新しいトークンを貼り付け"
OWNER_ID = 1324938326741876758
ALLOWED_USERS_FILE = "allowed_users.json"

start_time = time.time()


def load_allowed_users() -> set:
    try:
        if os.path.exists(ALLOWED_USERS_FILE):
            with open(ALLOWED_USERS_FILE, "r") as f:
                return set(json.load(f))
    except Exception as e:
        print(f"allowed_users.json の読み込みに失敗しました: {e}")
    return set()


def save_allowed_users(users: set):
    with open(ALLOWED_USERS_FILE, "w") as f:
        json.dump(list(users), f)


def format_uptime(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


allowed_users = load_allowed_users()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def is_allowed(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID or interaction.user.id in allowed_users


# ── ステータス自動更新 ─────────────────────────────────────────

@tasks.loop(minutes=1)
async def update_status():
    uptime = format_uptime(time.time() - start_time)
    server_count = len(client.guilds)
    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name=f"⏱{uptime} | 🌐{server_count}サーバー"
    )
    await client.change_presence(activity=activity)

@update_status.before_loop
async def before_update_status():
    await client.wait_until_ready()


# ── モーダル ──────────────────────────────────────────────────

class AddUserModal(discord.ui.Modal, title="ユーザーを追加"):
    user_id = discord.ui.TextInput(label="ユーザーID", placeholder="例: 123456789012345678")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.response.send_message("無効なIDです。数字で入力してください。", ephemeral=True)
            return
        allowed_users.add(uid)
        save_allowed_users(allowed_users)
        await interaction.response.send_message(f"<@{uid}> を許可リストに追加しました。", ephemeral=True)


class RemoveUserModal(discord.ui.Modal, title="ユーザーを削除"):
    user_id = discord.ui.TextInput(label="ユーザーID", placeholder="例: 123456789012345678")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.response.send_message("無効なIDです。数字で入力してください。", ephemeral=True)
            return
        if uid in allowed_users:
            allowed_users.remove(uid)
            save_allowed_users(allowed_users)
            await interaction.response.send_message(f"<@{uid}> を許可リストから削除しました。", ephemeral=True)
        else:
            await interaction.response.send_message("そのユーザーは許可リストにいません。", ephemeral=True)


# ── ユーザー管理パネル（永続View） ────────────────────────────

class ManagementPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ユーザーを追加", style=discord.ButtonStyle.success, emoji="➕", custom_id="panel:add")
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナーのみ操作できます。", ephemeral=True)
            return
        await interaction.response.send_modal(AddUserModal())

    @discord.ui.button(label="ユーザーを削除", style=discord.ButtonStyle.danger, emoji="➖", custom_id="panel:remove")
    async def remove_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナーのみ操作できます。", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveUserModal())

    @discord.ui.button(label="一覧を表示", style=discord.ButtonStyle.primary, emoji="📋", custom_id="panel:list")
    async def list_users(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナーのみ操作できます。", ephemeral=True)
            return
        if not allowed_users:
            await interaction.response.send_message("許可リストは空です。", ephemeral=True)
            return
        user_list = "\n".join(f"<@{uid}> (`{uid}`)" for uid in allowed_users)
        embed = discord.Embed(title="許可ユーザー一覧", description=user_list, color=discord.Color.blue())
        embed.set_footer(text=f"合計: {len(allowed_users)}人")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── サーバーリスト＋退出ボタン（ページネーション対応） ─────────

SERVERS_PER_PAGE = 20


def build_serverlist_embed(guilds: list[discord.Guild], page: int) -> discord.Embed:
    total_pages = max(1, -(-len(guilds) // SERVERS_PER_PAGE))
    start = page * SERVERS_PER_PAGE
    page_guilds = guilds[start:start + SERVERS_PER_PAGE]
    lines = [
        f"`{start + i + 1}.` **{g.name}** (メンバー: {g.member_count or '不明'}人 / ID: `{g.id}`)"
        for i, g in enumerate(page_guilds)
    ]
    embed = discord.Embed(
        title="導入済みサーバー一覧",
        description="\n".join(lines) if lines else "サーバーがありません。",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"合計: {len(guilds)}サーバー | ページ {page + 1}/{total_pages} | 🚪ボタンで退出できます")
    return embed


class ServerListView(discord.ui.View):
    def __init__(self, guilds: list[discord.Guild], page: int = 0):
        super().__init__(timeout=120)
        self.guilds = guilds
        self.page = page
        self.total_pages = max(1, -(-len(guilds) // SERVERS_PER_PAGE))

        start = page * SERVERS_PER_PAGE
        for guild in guilds[start:start + SERVERS_PER_PAGE]:
            self.add_item(LeaveButton(guild))

        prev_btn = discord.ui.Button(label="◀ 前へ", style=discord.ButtonStyle.secondary, disabled=(page == 0), row=4)
        prev_btn.callback = self.prev_page

        page_btn = discord.ui.Button(label=f"{page + 1} / {self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True, row=4)

        next_btn = discord.ui.Button(label="次へ ▶", style=discord.ButtonStyle.secondary, disabled=(page >= self.total_pages - 1), row=4)
        next_btn.callback = self.next_page

        self.add_item(prev_btn)
        self.add_item(page_btn)
        self.add_item(next_btn)

    async def prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナーのみ操作できます。", ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_serverlist_embed(self.guilds, self.page - 1), view=ServerListView(self.guilds, self.page - 1))

    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナーのみ操作できます。", ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_serverlist_embed(self.guilds, self.page + 1), view=ServerListView(self.guilds, self.page + 1))


class LeaveButton(discord.ui.Button):
    def __init__(self, guild: discord.Guild):
        label = guild.name[:30] + "…" if len(guild.name) > 30 else guild.name
        super().__init__(label=label, style=discord.ButtonStyle.danger, emoji="🚪")
        self.guild_id = guild.id
        self.guild_name = guild.name

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("オーナーのみ操作できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            guild = client.get_guild(self.guild_id)
            if guild is None:
                await interaction.followup.send("すでに退出済みのサーバーです。", ephemeral=True)
                return

            name = guild.name
            await guild.leave()

            updated_guilds = list(client.guilds)
            if updated_guilds:
                new_page = min(self.view.page, max(0, -(-len(updated_guilds) // SERVERS_PER_PAGE) - 1))
                try:
                    await interaction.edit_original_response(
                        embed=build_serverlist_embed(updated_guilds, new_page),
                        view=ServerListView(updated_guilds, new_page)
                    )
                except Exception:
                    pass
            await interaction.followup.send(f"**{name}** から退出しました。", ephemeral=True)

        except Exception as e:
            try:
                await interaction.followup.send(f"エラーが発生しました: {e}", ephemeral=True)
            except Exception:
                pass


# ── コマンド ──────────────────────────────────────────────────

@tree.command(name="panel", description="ユーザー管理パネルを表示します（オーナー専用）")
async def panel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    embed = discord.Embed(
        title="ユーザー管理パネル",
        description="ボタンを押してBotの使用許可を管理できます。",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=ManagementPanel(), ephemeral=True)


@tree.command(name="serverlist", description="Botが導入されているサーバー一覧と退出ボタンを表示します（オーナー専用）")
async def serverlist(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    guilds = list(client.guilds)
    if not guilds:
        await interaction.response.send_message("導入済みサーバーがありません。", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_serverlist_embed(guilds, 0), view=ServerListView(guilds, 0), ephemeral=True)


@tree.command(name="restart", description="Botを再起動します（許可ユーザー専用）")
async def restart(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    await interaction.response.send_message("Botを再起動します...", ephemeral=True)
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@client.event
async def on_ready():
    await tree.sync()
    client.add_view(ManagementPanel())
    if not update_status.is_running():
        update_status.start()
    print(f"ログイン成功: {client.user} (ID: {client.user.id})")
    print("スラッシュコマンドを同期しました")


client.run(TOKEN)
