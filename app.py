import discord
from discord import app_commands
from discord.ext import tasks
import os
import sys
import json
import asyncio
import time
import re
import io
from datetime import timedelta, timezone, datetime


TOKEN = "ここに新しいトークンを貼り付け"
OWNER_ID = 1324938326741876758
ALLOWED_USERS_FILE = "allowed_users.json"
PAYPAY_CHANNEL_FILE = "paypay_channel.json"
PAYPAY_LOG_FILE = "paypay_log.json"
TICKET_CONFIG_FILE = "ticket_config.json"
TICKET_DATA_FILE = "ticket_data.json"

PAYPAY_REGEX = re.compile(r'https?://pay\.paypay\.ne\.jp/\S+')

start_time = time.time()
JST = timezone(timedelta(hours=9))


# ── データ読み書き ─────────────────────────────────────────────

def load_allowed_users() -> set:
    try:
        if os.path.exists(ALLOWED_USERS_FILE):
            with open(ALLOWED_USERS_FILE, "r") as f:
                return set(json.load(f))
    except Exception as e:
        print(f"allowed_users.json の読み込みに失敗: {e}")
    return set()


def save_allowed_users(users: set):
    try:
        with open(ALLOWED_USERS_FILE, "w") as f:
            json.dump(list(users), f)
    except Exception as e:
        print(f"allowed_users.json の保存に失敗: {e}")


def load_paypay_channels() -> dict:
    try:
        if os.path.exists(PAYPAY_CHANNEL_FILE):
            with open(PAYPAY_CHANNEL_FILE, "r") as f:
                data = json.load(f)
                for k, v in data.items():
                    if isinstance(v, int):
                        data[k] = {"channel_id": v, "role_id": None}
                return data
    except Exception as e:
        print(f"paypay_channel.json の読み込みに失敗: {e}")
    return {}


def save_paypay_channels(data: dict):
    try:
        with open(PAYPAY_CHANNEL_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"paypay_channel.json の保存に失敗: {e}")


def load_paypay_log() -> dict:
    try:
        if os.path.exists(PAYPAY_LOG_FILE):
            with open(PAYPAY_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"paypay_log.json の読み込みに失敗: {e}")
    return {}


def save_paypay_log(data: dict):
    try:
        with open(PAYPAY_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"paypay_log.json の保存に失敗: {e}")


def load_ticket_config() -> dict:
    try:
        if os.path.exists(TICKET_CONFIG_FILE):
            with open(TICKET_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"ticket_config.json の読み込みに失敗: {e}")
    return {}


def save_ticket_config(data: dict):
    try:
        with open(TICKET_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"ticket_config.json の保存に失敗: {e}")


def load_ticket_data() -> dict:
    try:
        if os.path.exists(TICKET_DATA_FILE):
            with open(TICKET_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"ticket_data.json の読み込みに失敗: {e}")
    return {}


def save_ticket_data(data: dict):
    try:
        with open(TICKET_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"ticket_data.json の保存に失敗: {e}")


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
paypay_log = load_paypay_log()
ticket_config = load_ticket_config()
ticket_data = load_ticket_data()

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

    guild_key = str(message.guild.id)
    setting = paypay_channels.get(guild_key)
    if not setting:
        return

    notify_channel = message.guild.get_channel(setting["channel_id"])
    if notify_channel is None:
        return

    timestamp = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    role_id = setting.get("role_id")
    mention_text = f"<@&{role_id}>" if role_id else None

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

        await notify_channel.send(content=mention_text, embed=embed)

        entry = {
            "user_id": message.author.id,
            "user_name": str(message.author),
            "channel_id": message.channel.id,
            "channel_name": message.channel.name,
            "link": link,
            "timestamp": timestamp,
            "message_url": message.jump_url
        }
        if guild_key not in paypay_log:
            paypay_log[guild_key] = []
        paypay_log[guild_key].append(entry)
        save_paypay_log(paypay_log)


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


# ── PayPayパネル ──────────────────────────────────────────────

class ChannelSelectMenu(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="📢 通知チャンネルを選択...",
            channel_types=[discord.ChannelType.text],
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        channel = self.values[0]
        guild_key = str(interaction.guild_id)
        existing = paypay_channels.get(guild_key, {})
        paypay_channels[guild_key] = {
            "channel_id": channel.id,
            "role_id": existing.get("role_id")
        }
        save_paypay_channels(paypay_channels)
        await interaction.response.send_message(f"✅ 通知チャンネルを {channel.mention} に設定しました。", ephemeral=True)


class RoleSelectMenu(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="🔔 メンションロールを選択（省略可）...",
            min_values=0,
            max_values=1,
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        existing = paypay_channels.get(guild_key, {})
        if not existing.get("channel_id"):
            await interaction.response.send_message("先に通知チャンネルを設定してください。", ephemeral=True)
            return
        role_id = self.values[0].id if self.values else None
        paypay_channels[guild_key]["role_id"] = role_id
        save_paypay_channels(paypay_channels)
        if role_id:
            await interaction.response.send_message(f"✅ メンションロールを <@&{role_id}> に設定しました。", ephemeral=True)
        else:
            await interaction.response.send_message("✅ メンションロールを解除しました。", ephemeral=True)


class PayPayPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ChannelSelectMenu())
        self.add_item(RoleSelectMenu())

    @discord.ui.button(label="検知をオフ", style=discord.ButtonStyle.danger, emoji="🔕", row=2)
    async def turn_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        key = str(interaction.guild_id)
        if key in paypay_channels:
            del paypay_channels[key]
            save_paypay_channels(paypay_channels)
            await interaction.response.send_message("🔕 PayPayリンク検知をオフにしました。", ephemeral=True)
        else:
            await interaction.response.send_message("このサーバーでは検知が設定されていません。", ephemeral=True)

    @discord.ui.button(label="ログを表示", style=discord.ButtonStyle.primary, emoji="📋", row=2)
    async def show_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        entries = paypay_log.get(guild_key, [])
        if not entries:
            await interaction.response.send_message("まだログがありません。", ephemeral=True)
            return
        reversed_entries = list(reversed(entries))
        await interaction.response.send_message(
            embed=build_log_embed(reversed_entries, 0, interaction.guild.name),
            view=LogView(reversed_entries, 0, interaction.guild.name),
            ephemeral=True
        )

    @discord.ui.button(label="現在の設定", style=discord.ButtonStyle.secondary, emoji="ℹ️", row=2)
    async def show_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        setting = paypay_channels.get(guild_key)
        embed = discord.Embed(title="⚙️ PayPay検知 現在の設定", color=discord.Color.blurple())
        if setting and setting.get("channel_id"):
            channel = interaction.guild.get_channel(setting["channel_id"])
            role_id = setting.get("role_id")
            embed.add_field(name="状態", value="🟢 オン", inline=False)
            embed.add_field(name="通知チャンネル", value=channel.mention if channel else "不明", inline=False)
            embed.add_field(name="メンションロール", value=f"<@&{role_id}>" if role_id else "なし", inline=False)
            log_count = len(paypay_log.get(guild_key, []))
            embed.add_field(name="検知ログ件数", value=f"{log_count}件", inline=False)
        else:
            embed.add_field(name="状態", value="🔴 オフ", inline=False)
            embed.add_field(name="設定方法", value="上のプルダウンでチャンネルを選択してください", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── ログページネーション ──────────────────────────────────────

LOG_PER_PAGE = 5


def build_log_embed(entries: list, page: int, guild_name: str) -> discord.Embed:
    total_pages = max(1, -(-len(entries) // LOG_PER_PAGE))
    start = page * LOG_PER_PAGE
    page_entries = entries[start:start + LOG_PER_PAGE]
    embed = discord.Embed(title="📋 PayPayリンク検知ログ", color=discord.Color.orange())
    for i, e in enumerate(page_entries):
        embed.add_field(
            name=f"#{len(entries) - start - i}  {e['timestamp']}",
            value=(
                f"送信者: <@{e['user_id']}> (`{e['user_name']}`)\n"
                f"チャンネル: #{e['channel_name']}\n"
                f"リンク: {e['link']}\n"
                f"[メッセージへ移動]({e['message_url']})"
            ),
            inline=False
        )
    embed.set_footer(text=f"{guild_name} | 合計: {len(entries)}件 | ページ {page + 1}/{total_pages}")
    return embed


class LogView(discord.ui.View):
    def __init__(self, entries: list, page: int, guild_name: str):
        super().__init__(timeout=120)
        self.entries = entries
        self.page = page
        self.guild_name = guild_name
        self.total_pages = max(1, -(-len(entries) // LOG_PER_PAGE))

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
        await interaction.response.edit_message(embed=build_log_embed(self.entries, self.page - 1, self.guild_name), view=LogView(self.entries, self.page - 1, self.guild_name))

    async def next_page(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_log_embed(self.entries, self.page + 1, self.guild_name), view=LogView(self.entries, self.page + 1, self.guild_name))


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
        await interaction.response.edit_message(embed=build_serverlist_embed(self.guilds, self.page - 1), view=ServerListView(self.guilds, self.page - 1))

    async def next_page(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_serverlist_embed(self.guilds, self.page + 1), view=ServerListView(self.guilds, self.page + 1))


# ── オートコンプリート ─────────────────────────────────────────

async def server_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=g.name, value=str(g.id))
        for g in client.guilds
        if current.lower() in g.name.lower()
    ][:25]


# ── コマンド ──────────────────────────────────────────────────

@tree.command(name="panel", description="ユーザー管理パネルを表示します（オーナー専用）")
async def panel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    embed = discord.Embed(title="ユーザー管理パネル", description="ボタンを押してBotの使用許可を管理できます。", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=ManagementPanel(), ephemeral=True)


@tree.command(name="paypay", description="PayPayリンク検知パネルを表示します（許可ユーザー専用）")
async def paypay(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    guild_key = str(interaction.guild_id)
    setting = paypay_channels.get(guild_key)
    if setting and setting.get("channel_id"):
        channel = interaction.guild.get_channel(setting["channel_id"])
        role_id = setting.get("role_id")
        status = (
            f"状態: 🟢 オン\n"
            f"通知チャンネル: {channel.mention if channel else '不明'}\n"
            f"メンションロール: {'<@&' + str(role_id) + '>' if role_id else 'なし'}"
        )
    else:
        status = "状態: 🔴 オフ"
    embed = discord.Embed(
        title="💰 PayPay検知パネル",
        description=f"{status}\n\nプルダウンでチャンネル・ロールを設定できます。",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed, view=PayPayPanel(), ephemeral=True)


@tree.command(name="serverlist", description="Botが導入されているサーバー一覧を表示します（許可ユーザー専用）")
async def serverlist(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    guilds = list(client.guilds)
    if not guilds:
        await interaction.response.send_message("導入済みサーバーがありません。", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_serverlist_embed(guilds, 0), view=ServerListView(guilds, 0), ephemeral=True)


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


# ── チケットシステム ──────────────────────────────────────────

def is_ticket_staff(interaction: discord.Interaction) -> bool:
    guild_key = str(interaction.guild_id)
    cfg = ticket_config.get(guild_key, {})
    staff_role_id = cfg.get("staff_role_id")
    if not staff_role_id:
        return False
    staff_role = interaction.guild.get_role(staff_role_id)
    return staff_role is not None and staff_role in interaction.user.roles


class TicketCategorySelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="📁 チケットカテゴリを選択...",
            channel_types=[discord.ChannelType.category],
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        if guild_key not in ticket_config:
            ticket_config[guild_key] = {}
        ticket_config[guild_key]["category_id"] = self.values[0].id
        save_ticket_config(ticket_config)
        await interaction.response.send_message(f"✅ チケットカテゴリを **{self.values[0].name}** に設定しました。", ephemeral=True)


class TicketStaffRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(
            placeholder="👥 スタッフロールを選択...",
            min_values=1,
            max_values=1,
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        if guild_key not in ticket_config:
            ticket_config[guild_key] = {}
        ticket_config[guild_key]["staff_role_id"] = self.values[0].id
        save_ticket_config(ticket_config)
        await interaction.response.send_message(f"✅ スタッフロールを {self.values[0].mention} に設定しました。", ephemeral=True)


class TicketLogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="📋 ログチャンネルを選択...",
            channel_types=[discord.ChannelType.text],
            row=2
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        if guild_key not in ticket_config:
            ticket_config[guild_key] = {}
        ticket_config[guild_key]["log_channel_id"] = self.values[0].id
        save_ticket_config(ticket_config)
        await interaction.response.send_message(f"✅ ログチャンネルを {self.values[0].mention} に設定しました。", ephemeral=True)


class TicketSetupPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(TicketCategorySelect())
        self.add_item(TicketStaffRoleSelect())
        self.add_item(TicketLogChannelSelect())

    @discord.ui.button(label="現在の設定を確認", style=discord.ButtonStyle.secondary, emoji="ℹ️", row=3)
    async def show_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        cfg = ticket_config.get(guild_key, {})
        cat_id = cfg.get("category_id")
        staff_id = cfg.get("staff_role_id")
        log_id = cfg.get("log_channel_id")
        cat = interaction.guild.get_channel(cat_id) if cat_id else None
        staff = interaction.guild.get_role(staff_id) if staff_id else None
        log_ch = interaction.guild.get_channel(log_id) if log_id else None
        embed = discord.Embed(title="🎫 チケット設定", color=discord.Color.blurple())
        embed.add_field(name="チケットカテゴリ", value=cat.name if cat else "未設定", inline=False)
        embed.add_field(name="スタッフロール", value=staff.mention if staff else "未設定", inline=False)
        embed.add_field(name="ログチャンネル", value=log_ch.mention if log_ch else "未設定", inline=False)
        embed.add_field(name="チケット総数", value=f"{cfg.get('ticket_count', 0)}枚", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AddUserToTicketModal(discord.ui.Modal, title="ユーザーをチケットに追加"):
    user_id = discord.ui.TextInput(label="ユーザーID", placeholder="例: 123456789012345678")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.response.send_message("無効なIDです。数字で入力してください。", ephemeral=True)
            return
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("そのユーザーはこのサーバーにいません。", ephemeral=True)
            return
        await interaction.channel.set_permissions(
            member,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True
        )
        await interaction.response.send_message(f"✅ {member.mention} をチケットに追加しました。")


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ch_id = str(interaction.channel_id)
        data = ticket_data.get(ch_id)
        if not data:
            await interaction.followup.send("チケット情報が見つかりません。")
            return

        guild_key = str(interaction.guild_id)
        cfg = ticket_config.get(guild_key, {})
        log_channel_id = cfg.get("log_channel_id")

        messages = []
        async for msg in interaction.channel.history(limit=500, oldest_first=True):
            ts = msg.created_at.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")
            content = msg.content or "[添付ファイルまたはEmbed]"
            messages.append(f"[{ts}] {msg.author.display_name} ({msg.author.id}): {content}")
        transcript_text = "\n".join(messages) if messages else "メッセージなし"

        data["status"] = "closed"
        data["closed_at"] = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
        data["closed_by"] = str(interaction.user.id)
        ticket_data[ch_id] = data
        save_ticket_data(ticket_data)

        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                embed = discord.Embed(
                    title=f"🔒 チケット #{data['ticket_number']:04d} クローズ",
                    color=discord.Color.red(),
                    timestamp=datetime.now(JST)
                )
                embed.add_field(name="作成者", value=f"<@{data['user_id']}>", inline=True)
                embed.add_field(name="クローズした人", value=interaction.user.mention, inline=True)
                embed.add_field(name="作成日時", value=data["created_at"], inline=False)
                embed.add_field(name="クローズ日時", value=data["closed_at"], inline=False)
                transcript_file = discord.File(
                    fp=io.StringIO(transcript_text),
                    filename=f"transcript-{data['ticket_number']:04d}.txt"
                )
                await log_channel.send(embed=embed, file=transcript_file)

        try:
            user = interaction.guild.get_member(int(data["user_id"]))
            if user:
                await interaction.channel.set_permissions(user, view_channel=False)
            await interaction.channel.edit(name=f"closed-{data['ticket_number']:04d}")
            close_embed = discord.Embed(
                title="🔒 チケットをクローズしました",
                description=f"{interaction.user.mention} がチケットをクローズしました。\n10秒後にチャンネルを削除します。",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=close_embed)
            await asyncio.sleep(10)
            del ticket_data[ch_id]
            save_ticket_data(ticket_data)
            await interaction.channel.delete(reason=f"チケット #{data['ticket_number']:04d} クローズ")
        except Exception as e:
            await interaction.followup.send(f"クローズ処理中にエラーが発生しました: {e}")

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="チケットを閉じる", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close", row=0)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch_id = str(interaction.channel_id)
        data = ticket_data.get(ch_id)
        if not data:
            await interaction.response.send_message("このチャンネルはチケットではありません。", ephemeral=True)
            return
        if interaction.user.id != int(data["user_id"]) and not is_ticket_staff(interaction) and not is_allowed(interaction):
            await interaction.response.send_message("チケット作成者またはスタッフのみ閉じることができます。", ephemeral=True)
            return
        await interaction.response.send_message("本当にチケットを閉じますか？", view=TicketCloseConfirmView())

    @discord.ui.button(label="ユーザーを追加", style=discord.ButtonStyle.secondary, emoji="➕", custom_id="ticket:adduser", row=0)
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_ticket_staff(interaction) and not is_allowed(interaction):
            await interaction.response.send_message("スタッフのみユーザーを追加できます。", ephemeral=True)
            return
        await interaction.response.send_modal(AddUserToTicketModal())

    @discord.ui.button(label="チケット情報", style=discord.ButtonStyle.primary, emoji="ℹ️", custom_id="ticket:info", row=0)
    async def ticket_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch_id = str(interaction.channel_id)
        data = ticket_data.get(ch_id)
        if not data:
            await interaction.response.send_message("チケット情報が見つかりません。", ephemeral=True)
            return
        embed = discord.Embed(title="🎫 チケット情報", color=discord.Color.blurple())
        embed.add_field(name="チケット番号", value=f"#{data['ticket_number']:04d}", inline=True)
        embed.add_field(name="作成者", value=f"<@{data['user_id']}>", inline=True)
        embed.add_field(name="作成日時", value=data["created_at"], inline=False)
        embed.add_field(name="ステータス", value="🟢 オープン" if data["status"] == "open" else "🔴 クローズ", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="チケットを作成", style=discord.ButtonStyle.success, emoji="🎫", custom_id="ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild_key = str(interaction.guild_id)
        cfg = ticket_config.get(guild_key, {})

        if not cfg.get("category_id"):
            await interaction.followup.send("チケットが設定されていません。管理者に連絡してください。", ephemeral=True)
            return

        user_id_str = str(interaction.user.id)
        for ch_id, tdata in list(ticket_data.items()):
            if tdata.get("guild_id") == guild_key and tdata.get("user_id") == user_id_str and tdata.get("status") == "open":
                existing_ch = interaction.guild.get_channel(int(ch_id))
                if existing_ch:
                    await interaction.followup.send(f"すでに開いているチケットがあります: {existing_ch.mention}", ephemeral=True)
                    return
                else:
                    del ticket_data[ch_id]
                    save_ticket_data(ticket_data)

        category = interaction.guild.get_channel(cfg["category_id"])
        if category is None:
            await interaction.followup.send("カテゴリが見つかりません。管理者に連絡してください。", ephemeral=True)
            return

        ticket_num = cfg.get("ticket_count", 0) + 1
        cfg["ticket_count"] = ticket_num
        ticket_config[guild_key] = cfg
        save_ticket_config(ticket_config)

        safe_name = re.sub(r'[^a-z0-9-]', '', interaction.user.name.lower())[:20] or "user"
        channel_name = f"ticket-{ticket_num:04d}-{safe_name}"

        staff_role_id = cfg.get("staff_role_id")
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                read_message_history=True, manage_messages=True
            ),
        }
        if staff_role_id:
            staff_role = interaction.guild.get_role(staff_role_id)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True,
                    read_message_history=True, manage_messages=True, attach_files=True
                )

        try:
            channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"チケット #{ticket_num:04d} - {interaction.user}"
            )
        except Exception as e:
            await interaction.followup.send(f"チャンネルの作成に失敗しました: {e}", ephemeral=True)
            return

        ticket_data[str(channel.id)] = {
            "guild_id": guild_key,
            "user_id": user_id_str,
            "ticket_number": ticket_num,
            "created_at": datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S"),
            "status": "open"
        }
        save_ticket_data(ticket_data)

        embed = discord.Embed(
            title=f"🎫 チケット #{ticket_num:04d}",
            description=f"{interaction.user.mention} さん、チケットを作成しました。\nスタッフが対応しますのでしばらくお待ちください。",
            color=discord.Color.green(),
            timestamp=datetime.now(JST)
        )
        embed.set_footer(text=f"作成者: {interaction.user} | {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} JST")

        staff_mention = f"<@&{staff_role_id}>" if staff_role_id else ""
        await channel.send(
            content=f"{interaction.user.mention} {staff_mention}".strip(),
            embed=embed,
            view=TicketControlView()
        )
        await interaction.followup.send(f"✅ チケットを作成しました: {channel.mention}", ephemeral=True)


@tree.command(name="ticketsetup", description="チケットシステムを設定します（許可ユーザー専用）")
async def ticketsetup(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    guild_key = str(interaction.guild_id)
    cfg = ticket_config.get(guild_key, {})
    cat_id = cfg.get("category_id")
    staff_id = cfg.get("staff_role_id")
    log_id = cfg.get("log_channel_id")
    cat = interaction.guild.get_channel(cat_id) if cat_id else None
    staff = interaction.guild.get_role(staff_id) if staff_id else None
    log_ch = interaction.guild.get_channel(log_id) if log_id else None
    embed = discord.Embed(
        title="🎫 チケットシステム設定",
        description="プルダウンから各項目を設定してください。",
        color=discord.Color.blurple()
    )
    embed.add_field(name="チケットカテゴリ", value=cat.name if cat else "未設定", inline=False)
    embed.add_field(name="スタッフロール", value=staff.mention if staff else "未設定", inline=False)
    embed.add_field(name="ログチャンネル", value=log_ch.mention if log_ch else "未設定", inline=False)
    await interaction.response.send_message(embed=embed, view=TicketSetupPanel(), ephemeral=True)


@tree.command(name="ticketpanel", description="チケット作成パネルを設置します（許可ユーザー専用）")
@app_commands.describe(title="パネルのタイトル", description="パネルの説明文")
async def ticketpanel(
    interaction: discord.Interaction,
    title: str = "サポートチケット",
    description: str = "ボタンを押してチケットを作成してください。"
):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"🎫 {title}",
        description=description,
        color=discord.Color.blurple()
    )
    embed.set_footer(text="チケットを作成するには下のボタンを押してください")
    await interaction.response.send_message("✅ パネルを設置しました。", ephemeral=True)
    await interaction.channel.send(embed=embed, view=TicketPanel())


@client.event
async def on_ready():
    await tree.sync()
    client.add_view(ManagementPanel())
    client.add_view(TicketPanel())
    client.add_view(TicketControlView())
    if not update_status.is_running():
        update_status.start()
    print(f"ログイン成功: {client.user} (ID: {client.user.id})")
    print("スラッシュコマンドを同期しました")


client.run(TOKEN)
