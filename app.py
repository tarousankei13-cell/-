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
import random
from datetime import timedelta, timezone, datetime


TOKEN = "ここに新しいトークンを貼り付け"
OWNER_ID = 1324938326741876758
ALLOWED_USERS_FILE = "allowed_users.json"
PAYPAY_CHANNEL_FILE = "paypay_channel.json"
PAYPAY_LOG_FILE = "paypay_log.json"
TICKET_CONFIG_FILE = "ticket_config.json"
TICKET_DATA_FILE = "ticket_data.json"
BAN_CONFIG_FILE = "ban_config.json"
TEMP_BANS_FILE = "temp_bans.json"
JISSEKI_CONFIG_FILE = "jisseki_config.json"
LOTTERY_DATA_FILE = "lottery_data.json"

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


def load_ban_config() -> dict:
    try:
        if os.path.exists(BAN_CONFIG_FILE):
            with open(BAN_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"ban_config.json の読み込みに失敗: {e}")
    return {}


def save_ban_config(data: dict):
    try:
        with open(BAN_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"ban_config.json の保存に失敗: {e}")


def load_temp_bans() -> dict:
    try:
        if os.path.exists(TEMP_BANS_FILE):
            with open(TEMP_BANS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"temp_bans.json の読み込みに失敗: {e}")
    return {}


def save_temp_bans(data: dict):
    try:
        with open(TEMP_BANS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"temp_bans.json の保存に失敗: {e}")


def load_jisseki_config() -> dict:
    try:
        if os.path.exists(JISSEKI_CONFIG_FILE):
            with open(JISSEKI_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"jisseki_config.json の読み込みに失敗: {e}")
    return {}


def save_jisseki_config(data: dict):
    try:
        with open(JISSEKI_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"jisseki_config.json の保存に失敗: {e}")


def load_lottery_data() -> dict:
    try:
        if os.path.exists(LOTTERY_DATA_FILE):
            with open(LOTTERY_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"lottery_data.json の読み込みに失敗: {e}")
    return {}


def save_lottery_data(data: dict):
    try:
        with open(LOTTERY_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"lottery_data.json の保存に失敗: {e}")


def parse_duration(duration_str: str) -> timedelta | None:
    matches = re.findall(r'(\d+)([smhdw])', duration_str.lower())
    if not matches:
        return None
    unit_map = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days', 'w': 'weeks'}
    total = timedelta()
    for amount, unit in matches:
        total += timedelta(**{unit_map[unit]: int(amount)})
    return total if total.total_seconds() > 0 else None


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
ban_config = load_ban_config()
temp_bans = load_temp_bans()
jisseki_config = load_jisseki_config()
lottery_data = load_lottery_data()

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


@tasks.loop(seconds=30)
async def check_temp_bans():
    now = datetime.now(timezone.utc).timestamp()
    expired = [(k, v) for k, v in temp_bans.items() if v["expires_at"] <= now]
    for key, data in expired:
        guild = client.get_guild(int(data["guild_id"]))
        if guild:
            try:
                user = await client.fetch_user(int(data["user_id"]))
                await guild.unban(user, reason="一時BANの期限切れ・自動解除")
                cfg = ban_config.get(data["guild_id"], {})
                log_ch = guild.get_channel(cfg.get("log_channel_id", 0))
                if log_ch:
                    embed = discord.Embed(
                        title="🔓 一時BAN 自動解除",
                        color=discord.Color.green(),
                        timestamp=datetime.now(JST)
                    )
                    embed.add_field(name="ユーザー", value=f"{user} (`{user.id}`)", inline=False)
                    embed.add_field(name="BAN理由", value=data.get("reason", "理由なし"), inline=False)
                    embed.set_thumbnail(url=user.display_avatar.url)
                    await log_ch.send(embed=embed)
            except Exception as e:
                print(f"一時BAN自動解除に失敗: {e}")
        temp_bans.pop(key, None)
    if expired:
        save_temp_bans(temp_bans)

@check_temp_bans.before_loop
async def before_check_temp_bans():
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
            await interaction.channel.edit(name=f"🔒・closed-{data['ticket_number']:04d}")
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
        channel_name = f"🎫・ticket-{ticket_num:04d}-{safe_name}"

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


# ── BAN システム ─────────────────────────────────────────────

BANS_PER_PAGE = 10


def build_banlist_embed(bans: list, page: int, guild_name: str, guild_id_str: str) -> discord.Embed:
    total_pages = max(1, -(-len(bans) // BANS_PER_PAGE))
    start = page * BANS_PER_PAGE
    page_bans = bans[start:start + BANS_PER_PAGE]
    embed = discord.Embed(title="🔨 BANリスト", color=discord.Color.red())
    for i, entry in enumerate(page_bans):
        user = entry.user
        reason = entry.reason or "理由なし"
        ban_key = f"{guild_id_str}_{user.id}"
        tdata = temp_bans.get(ban_key)
        if tdata:
            expires = datetime.fromtimestamp(tdata["expires_at"], JST).strftime("%Y/%m/%d %H:%M")
            ban_type = f"⏰ 一時BAN（解除: {expires} JST）"
        else:
            ban_type = "🔨 永久BAN"
        embed.add_field(
            name=f"{start + i + 1}. {user}",
            value=f"ID: `{user.id}` | {ban_type}\n理由: {reason}",
            inline=False
        )
    embed.set_footer(text=f"{guild_name} | 合計: {len(bans)}人 | ページ {page + 1}/{total_pages}")
    return embed


class BanListView(discord.ui.View):
    def __init__(self, bans: list, page: int, guild_name: str, guild_id_str: str):
        super().__init__(timeout=120)
        self.bans = bans
        self.page = page
        self.guild_name = guild_name
        self.guild_id_str = guild_id_str
        self.total_pages = max(1, -(-len(bans) // BANS_PER_PAGE))

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
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_banlist_embed(self.bans, self.page - 1, self.guild_name, self.guild_id_str),
            view=BanListView(self.bans, self.page - 1, self.guild_name, self.guild_id_str)
        )

    async def next_page(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_banlist_embed(self.bans, self.page + 1, self.guild_name, self.guild_id_str),
            view=BanListView(self.bans, self.page + 1, self.guild_name, self.guild_id_str)
        )


async def _execute_ban(interaction: discord.Interaction, uid: int, reason: str, del_days: int, duration_str: str):
    if uid == interaction.user.id:
        await interaction.followup.send("自分自身をBANすることはできません。", ephemeral=True)
        return
    if uid == OWNER_ID:
        await interaction.followup.send("このユーザーをBANすることはできません。", ephemeral=True)
        return

    temp_duration = None
    expires_at = None
    if duration_str:
        temp_duration = parse_duration(duration_str)
        if temp_duration is None:
            await interaction.followup.send("無効な期間指定です。例: `30m` `2h` `7d` `1w`", ephemeral=True)
            return
        expires_at = (datetime.now(timezone.utc) + temp_duration).timestamp()

    ban_type = f"一時BAN ({duration_str})" if temp_duration else "永久BAN"

    try:
        user = await client.fetch_user(uid)
    except Exception:
        await interaction.followup.send("ユーザーが見つかりません。", ephemeral=True)
        return

    try:
        dm_embed = discord.Embed(
            title=f"🔨 {interaction.guild.name} からBANされました",
            color=discord.Color.red(),
            timestamp=datetime.now(JST)
        )
        dm_embed.add_field(name="種別", value=ban_type, inline=True)
        dm_embed.add_field(name="理由", value=reason, inline=False)
        if expires_at:
            unban_time = datetime.fromtimestamp(expires_at, JST).strftime("%Y/%m/%d %H:%M:%S")
            dm_embed.add_field(name="解除予定", value=f"{unban_time} (JST)", inline=False)
        await user.send(embed=dm_embed)
        dm_sent = True
    except Exception:
        dm_sent = False

    try:
        await interaction.guild.ban(user, reason=f"{reason} (実行者: {interaction.user})", delete_message_days=del_days)
    except discord.Forbidden:
        await interaction.followup.send("BANの実行権限がありません。", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"BANに失敗しました: {e}", ephemeral=True)
        return

    if temp_duration and expires_at:
        ban_key = f"{interaction.guild_id}_{uid}"
        temp_bans[ban_key] = {
            "guild_id": str(interaction.guild_id),
            "user_id": str(uid),
            "reason": reason,
            "expires_at": expires_at,
            "banned_by": str(interaction.user.id)
        }
        save_temp_bans(temp_bans)

    guild_key = str(interaction.guild_id)
    cfg = ban_config.get(guild_key, {})
    log_ch = interaction.guild.get_channel(cfg.get("log_channel_id", 0))
    if log_ch:
        log_embed = discord.Embed(title="🔨 ユーザーをBANしました", color=discord.Color.red(), timestamp=datetime.now(JST))
        log_embed.add_field(name="対象ユーザー", value=f"{user.mention} (`{user.id}`)", inline=False)
        log_embed.add_field(name="実行者", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="種別", value=ban_type, inline=True)
        log_embed.add_field(name="理由", value=reason, inline=False)
        log_embed.add_field(name="メッセージ削除", value=f"{del_days}日分", inline=True)
        log_embed.add_field(name="DM通知", value="✅ 送信済み" if dm_sent else "❌ 送信失敗", inline=True)
        if expires_at:
            unban_time = datetime.fromtimestamp(expires_at, JST).strftime("%Y/%m/%d %H:%M:%S")
            log_embed.add_field(name="解除予定", value=f"{unban_time} (JST)", inline=False)
        log_embed.set_thumbnail(url=user.display_avatar.url)
        await log_ch.send(embed=log_embed)

    result = discord.Embed(title="✅ BANしました", color=discord.Color.green())
    result.add_field(name="ユーザー", value=f"{user} (`{user.id}`)", inline=False)
    result.add_field(name="種別", value=ban_type, inline=True)
    result.add_field(name="理由", value=reason, inline=True)
    result.add_field(name="DM通知", value="✅ 送信済み" if dm_sent else "❌ 送信失敗（DM無効）", inline=False)
    await interaction.followup.send(embed=result, ephemeral=True)


async def _execute_unban(interaction: discord.Interaction, uid: int, reason: str):
    try:
        user = await client.fetch_user(uid)
    except Exception:
        await interaction.followup.send("ユーザーが見つかりません。", ephemeral=True)
        return

    try:
        await interaction.guild.unban(user, reason=f"{reason} (実行者: {interaction.user})")
    except discord.NotFound:
        await interaction.followup.send("そのユーザーはBANされていません。", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.followup.send("BAN解除の実行権限がありません。", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"BAN解除に失敗しました: {e}", ephemeral=True)
        return

    ban_key = f"{interaction.guild_id}_{uid}"
    if ban_key in temp_bans:
        del temp_bans[ban_key]
        save_temp_bans(temp_bans)

    guild_key = str(interaction.guild_id)
    cfg = ban_config.get(guild_key, {})
    log_ch = interaction.guild.get_channel(cfg.get("log_channel_id", 0))
    if log_ch:
        log_embed = discord.Embed(title="🔓 BANを解除しました", color=discord.Color.green(), timestamp=datetime.now(JST))
        log_embed.add_field(name="対象ユーザー", value=f"{user.mention} (`{user.id}`)", inline=False)
        log_embed.add_field(name="実行者", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="理由", value=reason, inline=False)
        log_embed.set_thumbnail(url=user.display_avatar.url)
        await log_ch.send(embed=log_embed)

    result = discord.Embed(title="✅ BAN解除しました", color=discord.Color.green())
    result.add_field(name="ユーザー", value=f"{user} (`{user.id}`)", inline=False)
    result.add_field(name="理由", value=reason, inline=False)
    await interaction.followup.send(embed=result, ephemeral=True)


class BanModal(discord.ui.Modal, title="ユーザーをBAN"):
    user_id = discord.ui.TextInput(label="ユーザーID", placeholder="例: 123456789012345678")
    reason = discord.ui.TextInput(label="理由", default="理由なし", required=False)
    duration = discord.ui.TextInput(label="一時BAN期間（省略で永久）", placeholder="例: 30m / 2h / 7d / 1w", required=False)
    delete_days = discord.ui.TextInput(label="メッセージ削除日数 (0〜7)", default="0", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.followup.send("無効なユーザーIDです。数字で入力してください。", ephemeral=True)
            return
        try:
            del_days = max(0, min(7, int(self.delete_days.value.strip() or "0")))
        except ValueError:
            del_days = 0
        reason = self.reason.value.strip() or "理由なし"
        duration_str = self.duration.value.strip()
        await _execute_ban(interaction, uid, reason, del_days, duration_str)


class UnbanModal(discord.ui.Modal, title="BANを解除"):
    user_id = discord.ui.TextInput(label="ユーザーID", placeholder="例: 123456789012345678")
    reason = discord.ui.TextInput(label="解除理由", default="理由なし", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.followup.send("無効なユーザーIDです。数字で入力してください。", ephemeral=True)
            return
        reason = self.reason.value.strip() or "理由なし"
        await _execute_unban(interaction, uid, reason)


class BanLogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="📋 BANログチャンネルを選択...",
            channel_types=[discord.ChannelType.text],
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        if guild_key not in ban_config:
            ban_config[guild_key] = {}
        ban_config[guild_key]["log_channel_id"] = self.values[0].id
        save_ban_config(ban_config)
        await interaction.response.send_message(f"✅ BANログチャンネルを {self.values[0].mention} に設定しました。", ephemeral=True)


class BanPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(BanLogChannelSelect())

    @discord.ui.button(label="BANする", style=discord.ButtonStyle.danger, emoji="🔨", row=1)
    async def do_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(BanModal())

    @discord.ui.button(label="BAN解除", style=discord.ButtonStyle.success, emoji="🔓", row=1)
    async def do_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(UnbanModal())

    @discord.ui.button(label="BANリスト", style=discord.ButtonStyle.primary, emoji="📋", row=1)
    async def show_banlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            bans = [entry async for entry in interaction.guild.bans()]
        except discord.Forbidden:
            await interaction.followup.send("BANリストの取得権限がありません。", ephemeral=True)
            return
        if not bans:
            await interaction.followup.send("BANされているユーザーはいません。", ephemeral=True)
            return
        guild_id_str = str(interaction.guild_id)
        await interaction.followup.send(
            embed=build_banlist_embed(bans, 0, interaction.guild.name, guild_id_str),
            view=BanListView(bans, 0, interaction.guild.name, guild_id_str),
            ephemeral=True
        )

    @discord.ui.button(label="現在の設定", style=discord.ButtonStyle.secondary, emoji="ℹ️", row=1)
    async def show_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        cfg = ban_config.get(guild_key, {})
        log_ch = interaction.guild.get_channel(cfg.get("log_channel_id", 0))
        temp_count = sum(1 for v in temp_bans.values() if v.get("guild_id") == guild_key)
        embed = discord.Embed(title="🔨 BAN設定", color=discord.Color.red())
        embed.add_field(name="ログチャンネル", value=log_ch.mention if log_ch else "未設定", inline=False)
        embed.add_field(name="一時BAN中のユーザー", value=f"{temp_count}人", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── 実績パネル ───────────────────────────────────────────────

class JissekiModal(discord.ui.Modal, title="実績を報告する"):
    product = discord.ui.TextInput(
        label="🛍️ 商品名",
        placeholder="商品名を入力してください"
    )
    rating = discord.ui.TextInput(
        label="⭐ 評価 (1〜5)",
        placeholder="1〜5の数字で入力してください",
        max_length=1
    )
    comment = discord.ui.TextInput(
        label="📋 コメント",
        style=discord.TextStyle.paragraph,
        placeholder="コメントを入力してください",
        required=False
    )
    quantity = discord.ui.TextInput(
        label="📦 個数",
        placeholder="数字のみ入力してください（例: 1）",
        default="1",
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            rating_num = int(self.rating.value.strip())
            if not 1 <= rating_num <= 5:
                raise ValueError
        except ValueError:
            await interaction.followup.send("評価は1〜5の数字で入力してください。", ephemeral=True)
            return

        try:
            quantity_num = int(self.quantity.value.strip())
            if quantity_num < 1:
                raise ValueError
        except ValueError:
            await interaction.followup.send("個数は1以上の数字で入力してください。", ephemeral=True)
            return

        output_channel = await _send_jisseki_embed(
            interaction.guild, interaction.user,
            self.product.value, rating_num, quantity_num, self.comment.value
        )
        if not output_channel:
            await interaction.followup.send("送信先チャンネルが見つかりません。", ephemeral=True)
            return
        await interaction.followup.send(f"✅ 実績を {output_channel.mention} に送信しました！", ephemeral=True)


class JissekiPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="実績を報告する", style=discord.ButtonStyle.success, emoji="📦", custom_id="jisseki:report")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(JissekiModal())


async def _send_jisseki_embed(
    guild: discord.Guild,
    target_user: discord.User | discord.Member,
    product: str,
    rating_num: int,
    quantity_num: int,
    comment: str
) -> discord.TextChannel | None:
    guild_key = str(guild.id)
    cfg = jisseki_config.get(guild_key, {})
    output_channel_id = cfg.get("output_channel_id")
    if not output_channel_id:
        return None
    output_channel = guild.get_channel(output_channel_id)
    if not output_channel:
        return None
    stars = "★" * rating_num + "☆" * (5 - rating_num)
    embed = discord.Embed(title="📦 実績報告", color=0x2b2d31)
    embed.add_field(name="👤 記入者", value=target_user.mention, inline=False)
    embed.add_field(name="🛍️ 商品名", value=product, inline=False)
    embed.add_field(name="⭐ 評価", value=f"{stars} ({rating_num})", inline=False)
    embed.add_field(name="📋 コメント", value=comment or "なし", inline=False)
    embed.add_field(name="📦 個数", value=f"{quantity_num}個", inline=False)
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.set_footer(text=client.user.name, icon_url=client.user.display_avatar.url)
    await output_channel.send(embed=embed)
    return output_channel


@tree.command(name="trackrecord-v2", description="代理で実績を送信します（許可ユーザー専用）")
@app_commands.describe(
    user="実績を送信するユーザー",
    product="商品名",
    rating="評価 (1〜5)",
    quantity="個数",
    comment="コメント（省略可）"
)
async def jisseki_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    product: str,
    rating: app_commands.Range[int, 1, 5],
    quantity: app_commands.Range[int, 1, None],
    comment: str = "なし"
):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    output_channel = await _send_jisseki_embed(
        interaction.guild, user, product, rating, quantity, comment
    )
    if not output_channel:
        await interaction.followup.send("実績送信チャンネルが設定されていません。先に `/jissekipanel` を実行してください。", ephemeral=True)
        return
    await interaction.followup.send(f"✅ {user.mention} の実績を {output_channel.mention} に送信しました！", ephemeral=True)


@tree.command(name="jissekipanel", description="実績報告パネルを設置します（許可ユーザー専用）")
@app_commands.describe(
    channel="実績を送信するチャンネル",
    title="パネルのタイトル",
    description="パネルの説明文"
)
async def jissekipanel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str = "実績報告パネル",
    description: str = "ボタンを押して実績を報告してください。"
):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return

    guild_key = str(interaction.guild_id)
    if guild_key not in jisseki_config:
        jisseki_config[guild_key] = {}
    jisseki_config[guild_key]["output_channel_id"] = channel.id
    save_jisseki_config(jisseki_config)

    embed = discord.Embed(
        title=f"📦 {title}",
        description=description,
        color=0x2b2d31
    )
    embed.set_footer(text="ボタンを押して実績を報告してください")
    await interaction.response.send_message(f"✅ パネルを設置しました。実績は {channel.mention} に送信されます。", ephemeral=True)
    await interaction.channel.send(embed=embed, view=JissekiPanel())


@tree.command(name="ban", description="BAN管理パネルを表示します（許可ユーザー専用）")
async def ban_cmd(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    guild_key = str(interaction.guild_id)
    cfg = ban_config.get(guild_key, {})
    log_ch = interaction.guild.get_channel(cfg.get("log_channel_id", 0))
    temp_count = sum(1 for v in temp_bans.values() if v.get("guild_id") == guild_key)
    embed = discord.Embed(
        title="🔨 BAN管理パネル",
        description="ログチャンネルをプルダウンで設定し、ボタンで操作してください。",
        color=discord.Color.red()
    )
    embed.add_field(name="ログチャンネル", value=log_ch.mention if log_ch else "未設定", inline=True)
    embed.add_field(name="一時BAN中", value=f"{temp_count}人", inline=True)
    await interaction.response.send_message(embed=embed, view=BanPanel(), ephemeral=True)


# ── 抽選システム ─────────────────────────────────────────────

def build_lottery_embed(data: dict) -> discord.Embed:
    status = data["status"]
    if status == "active":
        color = discord.Color.blurple()
        title_prefix = "🎉"
    elif status == "ended":
        color = discord.Color.gold()
        title_prefix = "🔒"
    else:
        color = discord.Color.dark_gray()
        title_prefix = "❌"

    embed = discord.Embed(
        title=f"{title_prefix} {data['title']}",
        description=data.get("description") or None,
        color=color
    )
    embed.add_field(name="🎁 賞品", value=data["prize"], inline=True)
    embed.add_field(name="🏆 当選人数", value=f"{data['winner_count']}人", inline=True)
    embed.add_field(name="👥 参加者数", value=f"{len(data['participants'])}人", inline=True)

    if status == "active":
        embed.add_field(name="⏰ 終了", value=f"<t:{int(data['ends_at'])}:R>", inline=False)
    elif status == "ended" and data.get("winners"):
        winner_mentions = " ".join(f"<@{w}>" for w in data["winners"])
        embed.add_field(name="🏆 当選者", value=winner_mentions, inline=False)
    elif status == "cancelled":
        embed.add_field(name="状態", value="❌ キャンセル済み", inline=False)

    embed.add_field(name="🎙️ 主催", value=f"<@{data['host_id']}>", inline=False)
    if status == "active":
        embed.set_footer(text="🎉 ボタンを押して応募 | 再度押すと取り消し")
    else:
        embed.set_footer(text="抽選終了")
    return embed


class LotteryEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎉 応募する", style=discord.ButtonStyle.success, custom_id="lottery:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        lottery_id = str(interaction.message.id)
        data = lottery_data.get(lottery_id)
        if not data:
            await interaction.response.send_message("この抽選は見つかりません。", ephemeral=True)
            return
        if data["status"] != "active":
            await interaction.response.send_message("この抽選はすでに終了しています。", ephemeral=True)
            return
        if datetime.now(timezone.utc).timestamp() > data["ends_at"]:
            await interaction.response.send_message("この抽選の応募期間は終了しています。", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id in data["participants"]:
            data["participants"].remove(user_id)
            lottery_data[lottery_id] = data
            save_lottery_data(lottery_data)
            await interaction.response.edit_message(embed=build_lottery_embed(data))
            await interaction.followup.send("🚫 抽選への応募を取り消しました。", ephemeral=True)
        else:
            data["participants"].append(user_id)
            lottery_data[lottery_id] = data
            save_lottery_data(lottery_data)
            await interaction.response.edit_message(embed=build_lottery_embed(data))
            await interaction.followup.send("✅ 抽選に応募しました！\n再度ボタンを押すと取り消せます。", ephemeral=True)


class LotteryResultView(discord.ui.View):
    def __init__(self, lottery_id: str):
        super().__init__(timeout=None)
        self.lottery_id = lottery_id

    @discord.ui.button(label="🔄 再抽選", style=discord.ButtonStyle.primary)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        data = lottery_data.get(self.lottery_id)
        if not data:
            await interaction.response.send_message("抽選データが見つかりません。", ephemeral=True)
            return
        participants = data["participants"]
        if not participants:
            await interaction.response.send_message("参加者がいません。", ephemeral=True)
            return
        winner_count = min(data["winner_count"], len(participants))
        new_winners = random.sample(participants, winner_count)
        data["winners"] = new_winners
        lottery_data[self.lottery_id] = data
        save_lottery_data(lottery_data)
        winner_mentions = " ".join(f"<@{w}>" for w in new_winners)
        embed = discord.Embed(
            title="🔄 再抽選結果",
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        )
        embed.add_field(name="📌 抽選", value=data["title"], inline=False)
        embed.add_field(name="🎁 賞品", value=data["prize"], inline=False)
        embed.add_field(name="🏆 新当選者", value=winner_mentions, inline=False)
        await interaction.response.send_message(content=winner_mentions, embed=embed)

    @discord.ui.button(label="👥 参加者一覧", style=discord.ButtonStyle.secondary)
    async def participants_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        data = lottery_data.get(self.lottery_id)
        if not data:
            await interaction.response.send_message("抽選データが見つかりません。", ephemeral=True)
            return
        participants = data["participants"]
        if not participants:
            await interaction.response.send_message("参加者がいません。", ephemeral=True)
            return
        lines = [f"{i + 1}. <@{uid}>" for i, uid in enumerate(participants)]
        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n..."
        embed = discord.Embed(title="👥 参加者一覧", description=text, color=discord.Color.blurple())
        embed.set_footer(text=f"合計: {len(participants)}人")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _do_draw(lottery_id: str):
    data = lottery_data.get(lottery_id)
    if not data or data["status"] != "active":
        return

    participants = data["participants"]
    winner_count = min(data["winner_count"], len(participants))
    winners = random.sample(participants, winner_count) if participants else []

    data["winners"] = winners
    data["status"] = "ended"
    lottery_data[lottery_id] = data
    save_lottery_data(lottery_data)

    guild = client.get_guild(int(data["guild_id"]))
    if not guild:
        return
    channel = guild.get_channel(int(data["channel_id"]))
    if not channel:
        return

    try:
        message = await channel.fetch_message(int(data["message_id"]))
        await message.edit(embed=build_lottery_embed(data), view=None)
    except Exception:
        pass

    if winners:
        winner_mentions = " ".join(f"<@{w}>" for w in winners)
        result_embed = discord.Embed(
            title="🎉 抽選結果発表！",
            color=discord.Color.gold(),
            timestamp=datetime.now(JST)
        )
        result_embed.add_field(name="📌 抽選タイトル", value=data["title"], inline=False)
        result_embed.add_field(name="🎁 賞品", value=data["prize"], inline=False)
        result_embed.add_field(name="🏆 当選者", value=winner_mentions, inline=False)
        result_embed.add_field(name="👥 参加者数", value=f"{len(participants)}人", inline=True)
        result_embed.set_footer(text="おめでとうございます！")
        await channel.send(content=winner_mentions, embed=result_embed, view=LotteryResultView(lottery_id))
    else:
        result_embed = discord.Embed(
            title="抽選終了",
            description=f"**{data['title']}** が終了しましたが、参加者がいませんでした。",
            color=discord.Color.gray()
        )
        await channel.send(embed=result_embed)


@tasks.loop(seconds=30)
async def check_lotteries():
    now = datetime.now(timezone.utc).timestamp()
    to_draw = [
        lid for lid, d in list(lottery_data.items())
        if d["status"] == "active" and d["ends_at"] <= now
    ]
    for lottery_id in to_draw:
        await _do_draw(lottery_id)

@check_lotteries.before_loop
async def before_check_lotteries():
    await client.wait_until_ready()


class LotteryCreateModal(discord.ui.Modal, title="抽選を作成"):
    lot_title = discord.ui.TextInput(label="🎉 抽選タイトル", placeholder="例: Nitroプレゼント抽選")
    description = discord.ui.TextInput(
        label="📝 説明",
        style=discord.TextStyle.paragraph,
        placeholder="抽選の詳細を入力（省略可）",
        required=False
    )
    prize = discord.ui.TextInput(label="🎁 賞品", placeholder="例: Discord Nitro 1ヶ月")
    winner_count = discord.ui.TextInput(label="🏆 当選人数 (1〜20)", placeholder="例: 1", default="1", max_length=2)
    duration = discord.ui.TextInput(label="⏰ 開催時間", placeholder="例: 30m / 1h / 1d / 7d")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            wcount = int(self.winner_count.value.strip())
            if not 1 <= wcount <= 20:
                raise ValueError
        except ValueError:
            await interaction.followup.send("当選人数は1〜20の整数で入力してください。", ephemeral=True)
            return
        dur = parse_duration(self.duration.value.strip())
        if dur is None:
            await interaction.followup.send("無効な時間指定です。例: `30m` `1h` `1d`", ephemeral=True)
            return
        ends_at = (datetime.now(timezone.utc) + dur).timestamp()
        data_obj = {
            "guild_id": str(interaction.guild_id),
            "channel_id": str(interaction.channel_id),
            "message_id": None,
            "title": self.lot_title.value,
            "description": self.description.value or "",
            "prize": self.prize.value,
            "winner_count": wcount,
            "ends_at": ends_at,
            "host_id": str(interaction.user.id),
            "participants": [],
            "winners": [],
            "status": "active",
            "created_at": datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
        }
        msg = await interaction.channel.send(embed=build_lottery_embed(data_obj), view=LotteryEntryView())
        data_obj["message_id"] = str(msg.id)
        lottery_data[str(msg.id)] = data_obj
        save_lottery_data(lottery_data)
        await interaction.followup.send("✅ 抽選を作成しました！", ephemeral=True)


def _get_active_lotteries(guild_id: str) -> list[tuple[str, dict]]:
    return [
        (lid, d) for lid, d in lottery_data.items()
        if d.get("guild_id") == guild_id and d["status"] == "active"
    ]


class LotteryDrawSelect(discord.ui.StringSelect):
    def __init__(self, lotteries: list[tuple]):
        options = [
            discord.SelectOption(
                label=d["title"][:100],
                value=lid,
                description=f"参加者: {len(d['participants'])}人 | 終了: {datetime.fromtimestamp(d['ends_at'], JST).strftime('%m/%d %H:%M')}"
            )
            for lid, d in lotteries[:25]
        ]
        super().__init__(placeholder="今すぐ抽選する抽選を選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lottery_id = self.values[0]
        data = lottery_data.get(lottery_id)
        if not data or data["status"] != "active":
            await interaction.followup.send("この抽選はすでに終了しています。", ephemeral=True)
            return
        await _do_draw(lottery_id)
        await interaction.followup.send("✅ 抽選を実行しました！", ephemeral=True)


class LotteryCancelSelect(discord.ui.StringSelect):
    def __init__(self, lotteries: list[tuple]):
        options = [
            discord.SelectOption(
                label=d["title"][:100],
                value=lid,
                description=f"参加者: {len(d['participants'])}人 | 終了: {datetime.fromtimestamp(d['ends_at'], JST).strftime('%m/%d %H:%M')}"
            )
            for lid, d in lotteries[:25]
        ]
        super().__init__(placeholder="キャンセルする抽選を選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lottery_id = self.values[0]
        data = lottery_data.get(lottery_id)
        if not data or data["status"] != "active":
            await interaction.followup.send("この抽選はすでに終了しています。", ephemeral=True)
            return
        data["status"] = "cancelled"
        lottery_data[lottery_id] = data
        save_lottery_data(lottery_data)
        channel = interaction.guild.get_channel(int(data["channel_id"]))
        if channel:
            try:
                msg = await channel.fetch_message(int(data["message_id"]))
                await msg.edit(embed=build_lottery_embed(data), view=None)
            except Exception:
                pass
        await interaction.followup.send(f"✅ **{data['title']}** をキャンセルしました。", ephemeral=True)


class LotterySelectView(discord.ui.View):
    def __init__(self, select: discord.ui.Select):
        super().__init__(timeout=60)
        self.add_item(select)


class LotteryManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="抽選を作成", style=discord.ButtonStyle.success, emoji="🎉", row=0)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(LotteryCreateModal())

    @discord.ui.button(label="今すぐ抽選", style=discord.ButtonStyle.primary, emoji="🎲", row=0)
    async def draw_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        lotteries = _get_active_lotteries(str(interaction.guild_id))
        if not lotteries:
            await interaction.response.send_message("現在アクティブな抽選はありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "抽選を選択してください：",
            view=LotterySelectView(LotteryDrawSelect(lotteries)),
            ephemeral=True
        )

    @discord.ui.button(label="抽選をキャンセル", style=discord.ButtonStyle.danger, emoji="❌", row=0)
    async def cancel_lottery(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        lotteries = _get_active_lotteries(str(interaction.guild_id))
        if not lotteries:
            await interaction.response.send_message("現在アクティブな抽選はありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "キャンセルする抽選を選択してください：",
            view=LotterySelectView(LotteryCancelSelect(lotteries)),
            ephemeral=True
        )

    @discord.ui.button(label="抽選一覧", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    async def list_lotteries(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        guild_key = str(interaction.guild_id)
        active = [d for d in lottery_data.values() if d.get("guild_id") == guild_key and d["status"] == "active"]
        if not active:
            await interaction.response.send_message("現在アクティブな抽選はありません。", ephemeral=True)
            return
        embed = discord.Embed(title="📋 アクティブな抽選一覧", color=discord.Color.blurple())
        for d in active[:10]:
            embed.add_field(
                name=f"🎉 {d['title']}",
                value=(
                    f"賞品: {d['prize']}\n"
                    f"当選人数: {d['winner_count']}人 | 参加者: {len(d['participants'])}人\n"
                    f"終了: <t:{int(d['ends_at'])}:R>\n"
                    f"[メッセージへ移動](https://discord.com/channels/{d['guild_id']}/{d['channel_id']}/{d['message_id']})"
                ),
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="lottery", description="抽選管理パネルを表示します（許可ユーザー専用）")
async def lottery_cmd(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return
    guild_key = str(interaction.guild_id)
    active_count = sum(1 for d in lottery_data.values() if d.get("guild_id") == guild_key and d["status"] == "active")
    embed = discord.Embed(
        title="🎉 抽選管理パネル",
        description="ボタンを押して抽選を作成・管理できます。",
        color=discord.Color.blurple()
    )
    embed.add_field(name="アクティブな抽選", value=f"{active_count}件", inline=True)
    await interaction.response.send_message(embed=embed, view=LotteryManageView(), ephemeral=True)


@client.event
async def on_ready():
    await tree.sync()
    client.add_view(ManagementPanel())
    client.add_view(TicketPanel())
    client.add_view(TicketControlView())
    client.add_view(JissekiPanel())
    client.add_view(LotteryEntryView())
    if not update_status.is_running():
        update_status.start()
    if not check_temp_bans.is_running():
        check_temp_bans.start()
    if not check_lotteries.is_running():
        check_lotteries.start()
    print(f"ログイン成功: {client.user} (ID: {client.user.id})")
    print("スラッシュコマンドを同期しました")


client.run(TOKEN)
