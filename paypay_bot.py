import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from PayPaython_mobile import PayPay
import re
import json
import time
import asyncio
import traceback

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "ここにトークンを貼り付け")

SESSION_DIR = "sessions"
VENDING_DATA_FILE = f"{SESSION_DIR}/vending_data.json"
os.makedirs(SESSION_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


# ── セッション管理 ────────────────────────────────────────────

class SessionManager:
    def __init__(self):
        self.sessions = {}
        self.load_sessions()

    def load_sessions(self):
        try:
            if os.path.exists(f"{SESSION_DIR}/sessions.json"):
                with open(f"{SESSION_DIR}/sessions.json", "r") as f:
                    self.sessions = json.load(f)
        except Exception as e:
            print(f"Error loading sessions: {e}")
            self.sessions = {}

    def save_sessions(self):
        try:
            with open(f"{SESSION_DIR}/sessions.json", "w") as f:
                json.dump(self.sessions, f)
        except Exception as e:
            print(f"Error saving sessions: {e}")

    def get_session(self, user_id):
        return self.sessions.get(str(user_id))

    def save_user_session(self, user_id, phone, password, access_token=None, refresh_token=None, device_uuid=None):
        self.sessions[str(user_id)] = {
            "phone": phone,
            "password": password,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "device_uuid": device_uuid,
            "last_updated": time.time()
        }
        self.save_sessions()

    def update_user_tokens(self, user_id, access_token, refresh_token=None, device_uuid=None):
        user_id = str(user_id)
        if user_id in self.sessions:
            self.sessions[user_id]["access_token"] = access_token
            if refresh_token:
                self.sessions[user_id]["refresh_token"] = refresh_token
            if device_uuid:
                self.sessions[user_id]["device_uuid"] = device_uuid
            self.sessions[user_id]["last_updated"] = time.time()
            self.save_sessions()


# ── PayPay管理 ────────────────────────────────────────────────

class PayPayManager:
    def __init__(self, session_manager):
        self.session_manager = session_manager
        self.clients = {}
        self.pending_auth = {}

    async def get_client(self, user_id):
        user_id = str(user_id)
        if user_id in self.clients:
            return self.clients[user_id]

        session = self.session_manager.get_session(user_id)
        if not session:
            return None

        try:
            if session.get("access_token"):
                client = PayPay(access_token=session["access_token"])
                self.clients[user_id] = client
                return client

            client = PayPay(session["phone"], session["password"], device_uuid=session.get("device_uuid"))
            self.clients[user_id] = client
            self.session_manager.update_user_tokens(
                user_id,
                client.access_token,
                client.refresh_token,
                client.device_uuid
            )
            return client
        except Exception as e:
            print(f"Error getting PayPay client: {e}")
            return None

    async def check_link(self, link_url):
        try:
            client = None
            for uid, existing_client in self.clients.items():
                if existing_client:
                    client = existing_client
                    break

            if not client:
                for uid, session in self.session_manager.sessions.items():
                    if session.get("access_token"):
                        try:
                            client = PayPay(access_token=session["access_token"])
                            break
                        except:
                            continue

            if not client:
                return False, "ログインしてね。"

            link_info = client.link_check(link_url)

            if hasattr(link_info, 'money') and hasattr(link_info, 'money_light'):
                total_amount = link_info.money + link_info.money_light
                has_password = getattr(link_info, 'has_password', False)
                password_text = "パスワードあり" if has_password else "パスワードなし"
                return True, f"リンク情報:\n金額: {total_amount}円\n{password_text}"
            else:
                return True, f"リンク情報: {link_info}"

        except Exception as e:
            print(f"Check link error: {e}")
            return False, f"リンク確認エラー: {e}"

    async def authenticate(self, user_id, phone, password):
        user_id = str(user_id)
        try:
            if not self._validate_phone(phone):
                return False, "電話番号の形式がなんか違うよ"

            client = PayPay(phone, password)
            self.clients[user_id] = client
            self.pending_auth[user_id] = True

            self.session_manager.save_user_session(user_id, phone, password)
            return True, "`/otp コード` で入力してね。"
        except Exception as e:
            print(f"Authentication error: {e}")
            return False, f"認証エラー: {e}"

    async def verify_otp(self, user_id, otp):
        user_id = str(user_id)
        try:
            client = self.clients.get(user_id)
            if not client:
                return False, "先に `/auth 電話番号 パスワード` で認証を行ってください。"

            if user_id not in self.pending_auth:
                return False, "認証セッションが見つかりません。もう一度 `/auth` からやり直してください。"

            client.login(otp)
            self.session_manager.update_user_tokens(
                user_id,
                client.access_token,
                client.refresh_token,
                client.device_uuid
            )
            if user_id in self.pending_auth:
                del self.pending_auth[user_id]

            return True, "loginが完了しました！"
        except Exception as e:
            print(f"OTP verification error: {e}")
            return False, f"OTP認証エラー: {e}"

    async def create_send_link(self, user_id, amount):
        try:
            client = await self.get_client(user_id)
            if not client:
                return False, "先にloginしてね。"

            if not amount.isdigit() or int(amount) <= 0:
                return False, "有効な金額を入力してください。"

            link = client.create_link(int(amount))
            return True, f"送金リンクを作成しました: {link}"
        except Exception as e:
            print(f"Create send link error: {e}")
            return False, f"送金リンク作成エラー: {e}"

    async def create_p2p_link(self, user_id):
        try:
            client = await self.get_client(user_id)
            if not client:
                return False, "先に認証を行ってください。"

            p2p_result = client.create_p2pcode()
            p2p_code = getattr(p2p_result, 'p2pcode', None)

            if p2p_code:
                return True, f"受け取りリンクを作成しました: {p2p_code}"
            else:
                return False, "受け取りコードの取得に失敗しました。"

        except Exception as e:
            print(f"Create P2P link error: {e}")
            print(traceback.format_exc())
            return False, f"受け取りリンク作成エラー: {e}"

    async def get_balance(self, user_id):
        try:
            client = await self.get_client(user_id)
            if not client:
                return False, "先に認証を行ってください。"

            get_balance = client.get_balance()
            all_balance = getattr(get_balance, 'all_balance', 0)
            useable_balance = getattr(get_balance, 'useable_balance', 0)
            money = getattr(get_balance, 'money', 0)
            money_light = getattr(get_balance, 'money_light', 0)
            points = getattr(get_balance, 'points', 0)

            balance_info = "【PayPay残高情報】\n"
            balance_info += f"総残高: {all_balance}円\n"
            balance_info += f"利用可能残高: {useable_balance}円\n"
            balance_info += f"マネー: {money}円\n"
            balance_info += f"マネーライト: {money_light}円\n"
            balance_info += f"ポイント: {points}円"

            return True, balance_info
        except Exception as e:
            print(f"Get balance error: {e}")
            print(traceback.format_exc())
            return False, f"残高確認エラー: {e}"

    def _validate_phone(self, phone):
        pattern = r'^(0[5-9]0[0-9]{8}|0[5-9]0-[0-9]{4}-[0-9]{4})$'
        return re.match(pattern, phone) is not None


# ── 自販機データ管理 ──────────────────────────────────────────

def load_vending_data() -> dict:
    try:
        if os.path.exists(VENDING_DATA_FILE):
            with open(VENDING_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"vending_data.json の読み込みに失敗: {e}")
    return {"items": {}, "next_id": 1, "admin_ids": [], "sales_log": [], "receiver_id": None}


def save_vending_data(data: dict):
    try:
        with open(VENDING_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"vending_data.json の保存に失敗: {e}")


vending_data = load_vending_data()


def is_vending_admin(user_id: int) -> bool:
    return user_id in vending_data.get("admin_ids", [])


# ── 自販機 Discord UI ─────────────────────────────────────────

def build_vending_embed(items: list) -> discord.Embed:
    embed = discord.Embed(
        title="🏪 PayPay 自販機",
        description="購入したい商品をプルダウンから選んでください。",
        color=0x00b900
    )
    if not items:
        embed.description = "現在販売中の商品はありません。"
    else:
        for item in items[:10]:
            stock_count = len(item["stock"])
            embed.add_field(
                name=f"ID: {item['id']}  ¥{item['price']}  {item['name']}",
                value=f"{item['description']}\n在庫: {stock_count}個",
                inline=False
            )
    embed.set_footer(text="プルダウンで商品を選択すると購入手順が表示されます")
    return embed


class VendingItemSelect(discord.ui.Select):
    def __init__(self, items: list):
        options = [
            discord.SelectOption(
                label=f"¥{item['price']} | {item['name']}"[:100],
                value=item["id"],
                description=f"{item['description']} (在庫: {len(item['stock'])}個)"[:100]
            )
            for item in items[:25]
        ]
        super().__init__(placeholder="🛒 商品を選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = self.values[0]
        item = vending_data["items"].get(item_id)
        if not item:
            await interaction.response.send_message("商品が見つかりません。", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🛒 {item['name']}",
            description=item["description"],
            color=0x00b900
        )
        embed.add_field(name="💴 価格", value=f"¥{item['price']}", inline=True)
        embed.add_field(name="📦 在庫", value=f"{len(item['stock'])}個", inline=True)
        embed.add_field(
            name="📋 購入手順",
            value=(
                f"1. PayPayアプリで **¥{item['price']}** の送金リンクを作成\n"
                "2. 以下のコマンドを実行（DM推奨）:\n"
                f"```/vending_buy {item['id']} [作成したリンクURL]```"
            ),
            inline=False
        )
        embed.set_footer(text=f"商品ID: {item['id']}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class VendingListView(discord.ui.View):
    def __init__(self, items: list):
        super().__init__(timeout=180)
        if items:
            self.add_item(VendingItemSelect(items))

    @discord.ui.button(label="🔄 更新", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = [
            item for item in vending_data["items"].values()
            if item.get("enabled") and len(item.get("stock", [])) > 0
        ]
        embed = build_vending_embed(items)
        await interaction.response.edit_message(embed=embed, view=VendingListView(items))


# ── 自販機 管理モーダル ───────────────────────────────────────

class VendingAddModal(discord.ui.Modal, title="商品を追加"):
    item_name = discord.ui.TextInput(
        label="商品名",
        placeholder="例: Netflixアカウント"
    )
    item_description = discord.ui.TextInput(
        label="説明（省略可）",
        placeholder="商品の説明",
        required=False
    )
    item_price = discord.ui.TextInput(
        label="価格（円）",
        placeholder="例: 500"
    )
    item_contents = discord.ui.TextInput(
        label="商品内容（1行1個）",
        style=discord.TextStyle.paragraph,
        placeholder="例:\nmail@example.com:password123\nmail2@example.com:password456"
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.item_price.value.strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("価格は正の整数で入力してください。", ephemeral=True)
            return

        contents = [c.strip() for c in self.item_contents.value.strip().split("\n") if c.strip()]
        if not contents:
            await interaction.response.send_message("商品内容を1つ以上入力してください。", ephemeral=True)
            return

        item_id = str(vending_data["next_id"])
        vending_data["next_id"] += 1
        vending_data["items"][item_id] = {
            "id": item_id,
            "name": self.item_name.value.strip(),
            "description": self.item_description.value.strip() or "説明なし",
            "price": price,
            "stock": contents,
            "total_sold": 0,
            "enabled": True
        }
        save_vending_data(vending_data)

        await interaction.response.send_message(
            f"✅ 商品を追加しました\n"
            f"**{self.item_name.value}** | ¥{price} | 在庫: {len(contents)}個\n"
            f"商品ID: `{item_id}`",
            ephemeral=True
        )


class VendingStockModal(discord.ui.Modal, title="在庫を追加"):
    item_id_input = discord.ui.TextInput(label="商品ID", placeholder="例: 1")
    item_contents = discord.ui.TextInput(
        label="追加する商品内容（1行1個）",
        style=discord.TextStyle.paragraph,
        placeholder="例:\nmail@example.com:password123"
    )

    async def on_submit(self, interaction: discord.Interaction):
        item_id = self.item_id_input.value.strip()
        item = vending_data["items"].get(item_id)
        if not item:
            await interaction.response.send_message(f"商品ID `{item_id}` が見つかりません。", ephemeral=True)
            return

        contents = [c.strip() for c in self.item_contents.value.strip().split("\n") if c.strip()]
        if not contents:
            await interaction.response.send_message("内容を1つ以上入力してください。", ephemeral=True)
            return

        item["stock"].extend(contents)
        save_vending_data(vending_data)

        await interaction.response.send_message(
            f"✅ **{item['name']}** に {len(contents)}個の在庫を追加しました。\n"
            f"現在の在庫: {len(item['stock'])}個",
            ephemeral=True
        )


# ── Bot初期化 ─────────────────────────────────────────────────

bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)
session_manager = SessionManager()
paypay_manager = PayPayManager(session_manager)


@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Error in event {event}: {traceback.format_exc()}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    try:
        print("Syncing slash commands...")
        await bot.tree.sync()
        print("Slash commands synced!")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# ── PayPay認証コマンド ─────────────────────────────────────────

@bot.tree.command(name="pay_login", description="PayPayアカウントで認証を開始")
@discord.app_commands.describe(
    phone="電話番号（例: 080-1234-5678）",
    password="PayPayのパスワード"
)
async def auth_command(interaction: discord.Interaction, phone: str, password: str):
    try:
        if not isinstance(interaction.channel, discord.DMChannel):
            await interaction.response.send_message("このコマンドはDMのみで使用可能です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.authenticate(interaction.user.id, phone, password)
        await interaction.followup.send(msg)
    except Exception as e:
        print(f"Error in auth command: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"エラーが発生しました: {e}")
        except:
            pass


@bot.tree.command(name="vefiry", description="SMSで受け取ったOTPコードを入力")
@discord.app_commands.describe(code="SMSで受け取ったOTPコード")
async def otp_command(interaction: discord.Interaction, code: str):
    try:
        if not isinstance(interaction.channel, discord.DMChannel):
            await interaction.response.send_message("このコマンドはDMのみで使用可能です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.verify_otp(interaction.user.id, code)
        await interaction.followup.send(msg)
    except Exception as e:
        print(f"Error in otp command: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"エラーが発生しました: {e}")
        except:
            pass


@bot.tree.command(name="send_link", description="指定した金額の送金リンクを作成")
@discord.app_commands.describe(amount="送金する金額（円）")
async def send_link_command(interaction: discord.Interaction, amount: str):
    try:
        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.create_send_link(interaction.user.id, amount)
        await interaction.followup.send(msg)
    except Exception as e:
        print(f"Error in send_link command: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"エラーが発生しました: {e}")
        except:
            pass


@bot.tree.command(name="receive_link", description="受け取りリンクを作成")
async def p2plink_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.create_p2p_link(interaction.user.id)
        await interaction.followup.send(msg)
    except Exception as e:
        print(f"Error in p2plink command: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"エラーが発生しました: {e}")
        except:
            pass


@bot.tree.command(name="check_link", description="PayPay送金リンクの内容を確認")
@discord.app_commands.describe(link="確認するPayPay送金リンク")
async def check_command(interaction: discord.Interaction, link: str):
    try:
        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.check_link(link)
        await interaction.followup.send(msg)
    except Exception as e:
        print(f"Error in check command: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"エラーが発生しました: {e}")
        except:
            pass


@bot.tree.command(name="get_balance", description="現在の残高を確認")
async def balance_command(interaction: discord.Interaction):
    try:
        if not isinstance(interaction.channel, discord.DMChannel):
            await interaction.response.send_message("このコマンドはDMのみで使用可能です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.get_balance(interaction.user.id)
        await interaction.followup.send(msg)
    except Exception as e:
        print(f"Error in balance command: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"エラーが発生しました: {e}")
        except:
            pass


# ── 自販機コマンド（管理者用） ────────────────────────────────

@bot.tree.command(name="vending_setup", description="自販機の管理者を登録します（初回セットアップ）")
@discord.app_commands.describe(
    receiver_login_id="PayPayリンクを受け取る際に使うログイン済みDiscord UserID（省略で自分）"
)
async def vending_setup_command(
    interaction: discord.Interaction,
    receiver_login_id: str = None
):
    try:
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id

        # 管理者が既にいる場合は既存の管理者のみ実行可能
        if vending_data["admin_ids"] and user_id not in vending_data["admin_ids"]:
            await interaction.followup.send("このコマンドは管理者のみ実行できます。", ephemeral=True)
            return

        if user_id not in vending_data["admin_ids"]:
            vending_data["admin_ids"].append(user_id)

        # 受け取り用PayPayアカウントのDiscord IDを設定
        if receiver_login_id:
            try:
                rid = int(receiver_login_id)
                vending_data["receiver_id"] = rid
            except ValueError:
                await interaction.followup.send("receiver_login_id は数字のDiscord IDで入力してください。", ephemeral=True)
                return
        else:
            vending_data["receiver_id"] = user_id

        save_vending_data(vending_data)

        receiver_id = vending_data["receiver_id"]
        embed = discord.Embed(title="✅ 自販機セットアップ完了", color=discord.Color.green())
        embed.add_field(name="管理者", value=f"<@{user_id}>", inline=True)
        embed.add_field(name="受け取りPayPayアカウント", value=f"<@{receiver_id}> のセッション", inline=True)
        embed.add_field(
            name="次のステップ",
            value=(
                "1. 受け取り用アカウントで `/pay_login` でログインしておく\n"
                "2. `/vending_add` で商品を追加する\n"
                "3. `/vending_list` で自販機を公開する"
            ),
            inline=False
        )
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"Error in vending_setup: {e}")
        print(traceback.format_exc())
        await interaction.followup.send(f"エラー: {e}", ephemeral=True)


@bot.tree.command(name="vending_add", description="自販機に商品を追加します（管理者専用）")
async def vending_add_command(interaction: discord.Interaction):
    if not is_vending_admin(interaction.user.id):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return
    await interaction.response.send_modal(VendingAddModal())


@bot.tree.command(name="vending_stock", description="既存商品に在庫を追加します（管理者専用）")
async def vending_stock_command(interaction: discord.Interaction):
    if not is_vending_admin(interaction.user.id):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return
    await interaction.response.send_modal(VendingStockModal())


@bot.tree.command(name="vending_remove", description="商品を削除します（管理者専用）")
@discord.app_commands.describe(item_id="削除する商品ID")
async def vending_remove_command(interaction: discord.Interaction, item_id: str):
    if not is_vending_admin(interaction.user.id):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    item = vending_data["items"].get(item_id)
    if not item:
        await interaction.followup.send(f"商品ID `{item_id}` が見つかりません。")
        return

    name = item["name"]
    del vending_data["items"][item_id]
    save_vending_data(vending_data)
    await interaction.followup.send(f"✅ **{name}**（ID: {item_id}）を削除しました。")


@bot.tree.command(name="vending_items", description="全商品リスト（在庫切れ含む）を表示します（管理者専用）")
async def vending_items_command(interaction: discord.Interaction):
    if not is_vending_admin(interaction.user.id):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    items = list(vending_data["items"].values())
    if not items:
        await interaction.followup.send("商品が登録されていません。")
        return

    embed = discord.Embed(title="📦 全商品一覧（管理者用）", color=discord.Color.blurple())
    for item in items:
        status = "✅ 販売中" if item.get("enabled") else "⏸️ 停止中"
        embed.add_field(
            name=f"ID: {item['id']} | {item['name']}",
            value=(
                f"価格: ¥{item['price']}\n"
                f"在庫: {len(item['stock'])}個 | 販売済: {item['total_sold']}個\n"
                f"状態: {status}"
            ),
            inline=False
        )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="vending_toggle", description="商品の販売を停止/再開します（管理者専用）")
@discord.app_commands.describe(item_id="対象の商品ID")
async def vending_toggle_command(interaction: discord.Interaction, item_id: str):
    if not is_vending_admin(interaction.user.id):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    item = vending_data["items"].get(item_id)
    if not item:
        await interaction.followup.send(f"商品ID `{item_id}` が見つかりません。")
        return

    item["enabled"] = not item.get("enabled", True)
    save_vending_data(vending_data)
    status = "✅ 販売再開" if item["enabled"] else "⏸️ 販売停止"
    await interaction.followup.send(f"**{item['name']}** を {status} しました。")


@bot.tree.command(name="vending_sales", description="販売履歴を表示します（管理者専用）")
async def vending_sales_command(interaction: discord.Interaction):
    if not is_vending_admin(interaction.user.id):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    logs = vending_data.get("sales_log", [])
    if not logs:
        await interaction.followup.send("まだ販売履歴がありません。")
        return

    recent = list(reversed(logs))[:10]
    total_revenue = sum(entry.get("price", 0) for entry in logs)

    embed = discord.Embed(
        title="📊 販売履歴（直近10件）",
        color=discord.Color.gold()
    )
    embed.add_field(name="総売上", value=f"¥{total_revenue}", inline=True)
    embed.add_field(name="総販売数", value=f"{len(logs)}件", inline=True)

    for entry in recent:
        from datetime import datetime, timezone, timedelta
        JST = timezone(timedelta(hours=9))
        ts = datetime.fromtimestamp(entry.get("timestamp", 0), JST).strftime("%m/%d %H:%M")
        embed.add_field(
            name=f"{ts} | {entry.get('item_name', '不明')}",
            value=f"購入者: <@{entry.get('buyer_id', '?')}> | ¥{entry.get('price', '?')}",
            inline=False
        )
    await interaction.followup.send(embed=embed)


# ── 自販機コマンド（ユーザー用） ──────────────────────────────

@bot.tree.command(name="vending_list", description="販売中の商品一覧を表示します")
async def vending_list_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=False)

        items = [
            item for item in vending_data["items"].values()
            if item.get("enabled") and len(item.get("stock", [])) > 0
        ]
        embed = build_vending_embed(items)
        await interaction.followup.send(embed=embed, view=VendingListView(items))

    except Exception as e:
        print(f"Error in vending_list: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"エラー: {e}", ephemeral=True)
        except:
            pass


@bot.tree.command(name="vending_buy", description="商品を購入します（PayPay送金リンクが必要）")
@discord.app_commands.describe(
    item_id="購入する商品ID（/vending_list で確認）",
    link="自分が作成したPayPay送金リンク（価格ぴったりで作成してください）"
)
async def vending_buy_command(interaction: discord.Interaction, item_id: str, link: str):
    try:
        await interaction.response.defer(ephemeral=True)

        # 商品確認
        item = vending_data["items"].get(item_id)
        if not item:
            await interaction.followup.send(f"❌ 商品ID `{item_id}` が見つかりません。`/vending_list` で確認してください。")
            return

        if not item.get("enabled"):
            await interaction.followup.send("❌ この商品は現在販売停止中です。")
            return

        if not item.get("stock"):
            await interaction.followup.send("❌ この商品は在庫切れです。")
            return

        # 受け取り用PayPayアカウント取得
        receiver_id = vending_data.get("receiver_id")
        if not receiver_id:
            await interaction.followup.send("❌ 自販機が正しくセットアップされていません。管理者に連絡してください。")
            return

        receiver_client = await paypay_manager.get_client(receiver_id)
        if not receiver_client:
            await interaction.followup.send("❌ 受け取り用アカウントにログインしていません。管理者に連絡してください。")
            return

        # リンクの金額を確認
        try:
            link_info = receiver_client.link_check(link)

            if hasattr(link_info, 'money') and hasattr(link_info, 'money_light'):
                link_amount = link_info.money + link_info.money_light
            elif hasattr(link_info, 'total'):
                link_amount = link_info.total
            elif hasattr(link_info, 'amount'):
                link_amount = link_info.amount
            else:
                await interaction.followup.send(f"❌ リンク情報の取得に失敗しました。有効なPayPayリンクを入力してください。")
                return

        except Exception as e:
            await interaction.followup.send(f"❌ リンクの確認に失敗しました: {e}")
            return

        # 金額チェック
        if link_amount < item["price"]:
            await interaction.followup.send(
                f"❌ 金額が不足しています。\n"
                f"必要金額: **¥{item['price']}** / リンク金額: **¥{link_amount}**"
            )
            return

        # リンクを受け取る
        try:
            receiver_client.receive_link(link, link_amount, True)
        except Exception:
            try:
                receiver_client.accept_link(link)
            except Exception as e:
                print(f"Link receive error (non-fatal): {e}")

        # コンテンツを取り出す
        content = item["stock"].pop(0)
        item["total_sold"] += 1

        # 販売ログ記録
        if "sales_log" not in vending_data:
            vending_data["sales_log"] = []
        vending_data["sales_log"].append({
            "buyer_id": str(interaction.user.id),
            "item_id": item_id,
            "item_name": item["name"],
            "price": link_amount,
            "timestamp": time.time()
        })
        save_vending_data(vending_data)

        # 購入者にDMで商品を届ける
        delivered_via_dm = False
        try:
            dm_embed = discord.Embed(
                title="✅ 購入完了！",
                description=f"**{item['name']}** をご購入いただきありがとうございます。",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="💴 支払金額", value=f"¥{link_amount}", inline=True)
            dm_embed.add_field(name="📦 商品内容", value=f"||{content}||", inline=False)
            dm_embed.set_footer(text="内容をクリックすると表示されます")
            await interaction.user.send(embed=dm_embed)
            delivered_via_dm = True
        except discord.Forbidden:
            pass

        # コマンドのレスポンス
        if delivered_via_dm:
            await interaction.followup.send(
                f"✅ 購入完了！\nDMに商品内容をお送りしました。\n残り在庫: {len(item['stock'])}個"
            )
        else:
            result_embed = discord.Embed(
                title="✅ 購入完了！",
                description=f"**{item['name']}** の購入ありがとうございます。",
                color=discord.Color.green()
            )
            result_embed.add_field(name="📦 商品内容", value=f"||{content}||", inline=False)
            result_embed.set_footer(text="※ DMが無効のためここに表示しています。内容は自分にしか見えません。")
            await interaction.followup.send(embed=result_embed, ephemeral=True)

    except Exception as e:
        print(f"Error in vending_buy: {e}")
        print(traceback.format_exc())
        try:
            await interaction.followup.send(f"❌ 購入処理中にエラーが発生しました: {e}", ephemeral=True)
        except:
            pass


@bot.tree.command(name="paypay_help", description="PayPayボットのコマンド一覧と使い方を表示")
async def help_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=False)

        embed = discord.Embed(
            title="📖 PayPay Bot コマンド一覧",
            color=0x00b900
        )
        embed.add_field(
            name="🔐 認証（DMのみ）",
            value=(
                "`/pay_login 電話番号 パスワード` — 認証開始\n"
                "`/vefiry コード` — OTP入力して認証完了\n"
                "`/get_balance` — 残高確認"
            ),
            inline=False
        )
        embed.add_field(
            name="💸 送金",
            value=(
                "`/send_link 金額` — 送金リンク作成\n"
                "`/receive_link` — 受け取りリンク作成\n"
                "`/check_link リンク` — リンク金額を確認"
            ),
            inline=False
        )
        embed.add_field(
            name="🏪 自販機（購入）",
            value=(
                "`/vending_list` — 販売中商品の一覧\n"
                "`/vending_buy 商品ID リンク` — 商品を購入（PayPay送金リンクが必要）"
            ),
            inline=False
        )
        embed.add_field(
            name="⚙️ 自販機（管理者用）",
            value=(
                "`/vending_setup` — 初回セットアップ\n"
                "`/vending_add` — 商品追加\n"
                "`/vending_stock` — 在庫追加\n"
                "`/vending_remove 商品ID` — 商品削除\n"
                "`/vending_toggle 商品ID` — 販売停止/再開\n"
                "`/vending_items` — 全商品確認（管理者）\n"
                "`/vending_sales` — 販売履歴"
            ),
            inline=False
        )
        embed.set_footer(text="認証・残高確認はセキュリティのためDMのみで使用できます")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"Error in help command: {e}")
        try:
            await interaction.followup.send(f"エラー: {e}")
        except:
            pass


# ── プレフィックスコマンド（既存互換） ───────────────────────

@bot.command(name="pay_login")
async def auth_prefix(ctx, phone: str, password: str):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("このコマンドはDMのみで使用可能です。")
        return
    await ctx.send("認証処理を開始します...")
    success, msg = await paypay_manager.authenticate(ctx.author.id, phone, password)
    await ctx.send(msg)


@bot.command(name="verify")
async def otp_prefix(ctx, code: str):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("このコマンドはDMのみで使用可能です。")
        return
    await ctx.send("OTP認証を処理中...")
    success, msg = await paypay_manager.verify_otp(ctx.author.id, code)
    await ctx.send(msg)


@bot.command(name="send_link")
async def send_link_prefix(ctx, amount: str):
    await ctx.send("送金リンクを作成中...")
    success, msg = await paypay_manager.create_send_link(ctx.author.id, amount)
    await ctx.send(msg)


@bot.command(name="receive_link")
async def p2plink_prefix(ctx):
    await ctx.send("受け取りリンクを作成中...")
    success, msg = await paypay_manager.create_p2p_link(ctx.author.id)
    await ctx.send(msg)


@bot.command(name="check_link")
async def check_prefix(ctx, link: str):
    await ctx.send("リンク情報を確認中...")
    success, msg = await paypay_manager.check_link(link)
    await ctx.send(msg)


@bot.command(name="get_balance")
async def balance_prefix(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("このコマンドはDM専用です。")
        return
    await ctx.send("残高を確認中...")
    success, msg = await paypay_manager.get_balance(ctx.author.id)
    await ctx.send(msg)


@bot.command(name="help", aliases=["paypay_help"])
async def help_prefix(ctx):
    await ctx.send(
        "**PayPay Bot コマンド一覧**\n"
        "`/pay_login 電話番号 パスワード` — 認証開始（DMのみ）\n"
        "`/verify コード` — OTP入力（DMのみ）\n"
        "`/send_link 金額` — 送金リンク作成\n"
        "`/receive_link` — 受け取りリンク作成\n"
        "`/check_link リンク` — リンク確認\n"
        "`/get_balance` — 残高確認（DMのみ）\n"
        "**自販機**\n"
        "`/vending_list` — 商品一覧\n"
        "`/vending_buy 商品ID リンク` — 購入"
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("引数が足りません。`/help` でコマンドの使い方を確認してください。")
    else:
        print(f"Command error: {error}")
        print(traceback.format_exc())
        await ctx.send(f"エラーが発生しました: {error}")


def main():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
