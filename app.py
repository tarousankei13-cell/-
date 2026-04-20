import discord
from discord import app_commands
from discord.ext import tasks
import os
import sys
import json
import asyncio
import time
import re
from datetime import timedelta, timezone, datetime


TOKEN = "ここに新しいトークンを貼り付け"
OWNER_ID = 1324938326741876758
ALLOWED_USERS_FILE = "allowed_users.json"
PAYPAY_CHANNEL_FILE = "paypay_channel.json"

PAYPAY_REGEX = re.compile(r'https?://pay\.paypay\.ne\.jp/\S+')

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
    try:
        with open(ALLOWED_USERS_FILE, "w") as f:
            json.dump(list(users), f)
    except Exception as e:
        print(f"allowed_users.json の保存に失敗しました: {e}")


def load_paypay_channels() -> dict:
    try:
        if os.path.exists(PAYPAY_CHANNEL_FILE):
            with open(PAYPAY_CHANNEL_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"paypay_channel.json の読み込みに失敗しました: {e}")
    return {}


def save_paypay_channels(data: dict):
    try:
        with open(PAYPAY_CHANNEL_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"paypay_channel.json の保存に失敗しました: {e}")


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
paypay_channels = load_paypay_channels()

intents = discord.Intents.default()
intents.message_content = True
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


# ── PayPayリンク検知 ──────────────────────────────────────────

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    links = PAYPAY_REGEX.findall(message.content)
    if not links:
        return

    channel_id = paypay_channels.get(str(message.guild.id))
    if not channel_id:
        return

    notify_channel = message.guild.get_channel(channel_id)
    if notify_channel is None:
        return

    jst = timezone(timedelta(hours=9))
    timestamp = datetime.now(jst).strftime("%Y/%m/%d %H:%M:%S")

    for link in links:
        embed = discord.Embed(
            title="💰 PayPayリンクを検知しました",
            color=discord.Color.red(),
            timestamp=message.created_at
        )
        embed.add_field(name="送信者", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="投稿チャンネル", value=message.channel.mention, inline=False)
        embed.add_field(name="リンク", value=link, inline=False)
        embed.add_field(name="メッセージへ移動", value=f"[クリックしてジャンプ]({message.jump_url})", inline=False)
        embed.set_footer(text=f"検知時刻: {timestamp} (JST)")

        await notify_channel.send(embed=embed)


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


# ── ユーザー管理パネル（永続View・オーナー専用） ──────────────

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


# ── サーバーリスト（ページネーション） ───────────────────────

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
    embed.set_footer(text=f"合計: {len(guilds)}サーバー | ページ {page + 1}/{total_pages}")
    return embed


class ServerListView(discord.ui.View):
    def __init__(self, guilds: list[discord.Guild], page: int = 0):
        super().__init__(timeout=120)
        self.guilds = guilds
        self.page = page
        self.total_pages = max(1, -(-len(guilds) // SERVERS_PER_PAGE))

        prev_btn = discord.ui.Button(label="◀ 前へ", style=discord.ButtonStyle.secondary, disabled=(page == 0))
        prev_btn.callback = self.prev_page

        page_btn = discord.ui.Button(label=f"{page + 1} / {self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True)

        next_btn = discord.ui.Button(label="次へ ▶", style=discord.ButtonStyle.secondary, disabled=(page >= self.total_pages - 1))
        next_btn.callback = self.next_page

        self.add_item(prev_btn)
        self.add_item(page_btn)
        self.add_item(next_btn)

    async def prev_page(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_serverlist_embed(self.guilds, self.page - 1),
            view=ServerListView(self.guilds, self.page - 1)
        )

    async def next_page(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_serverlist_embed(self.guilds, self.page + 1),
            view=ServerListView(self.guilds, self.page + 1)
        )


# ── オートコンプリート ─────────────────────────────────────────

async def server_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f"{g.name}", value=str(g.id))
        for g in client.guilds
        if current.lower() in g.name.lower()
    ][:25]


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


@tree.command(name="setpaypay", description="PayPayリンク検知の通知チャンネルを設定します（許可ユーザー専用）")
@app_commands.describe(channel="PayPayリンクを検知したときに通知するチャンネル")
async def setpaypay(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    paypay_channels[str(interaction.guild_id)] = channel.id
    save_paypay_channels(paypay_channels)
    embed = discord.Embed(
        title="✅ PayPay通知チャンネルを設定しました",
        description=f"{channel.mention} にPayPayリンク検知通知を送信します。",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="paypayoff", description="PayPayリンク検知をオフにします（許可ユーザー専用）")
async def paypayoff(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    key = str(interaction.guild_id)
    if key in paypay_channels:
        del paypay_channels[key]
        save_paypay_channels(paypay_channels)
        await interaction.response.send_message("PayPayリンク検知をオフにしました。", ephemeral=True)
    else:
        await interaction.response.send_message("このサーバーでは検知が設定されていません。", ephemeral=True)


@tree.command(name="serverlist", description="Botが導入されているサーバー一覧を表示します（許可ユーザー専用）")
async def serverlist(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    guilds = list(client.guilds)
    if not guilds:
        await interaction.response.send_message("導入済みサーバーがありません。", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=build_serverlist_embed(guilds, 0),
        view=ServerListView(guilds, 0),
        ephemeral=True
    )


@tree.command(name="kick", description="指定したサーバーからBotを退出させます（許可ユーザー専用）")
@app_commands.describe(server_id="退出するサーバー名を入力または選択")
@app_commands.autocomplete(server_id=server_autocomplete)
async def kick(interaction: discord.Interaction, server_id: str):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        gid = int(server_id)
    except ValueError:
        await interaction.followup.send("無効なサーバーIDです。", ephemeral=True)
        return

    guild = client.get_guild(gid)
    if guild is None:
        await interaction.followup.send("指定されたサーバーが見つかりません。", ephemeral=True)
        return

    name = guild.name
    try:
        await guild.leave()
        await interaction.followup.send(f"**{name}** から退出しました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"退出に失敗しました: {e}", ephemeral=True)


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
