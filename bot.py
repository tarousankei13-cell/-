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
import uuid

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "admin")

SESSION_DIR = "sessions"
DATA_DIR = "data"
os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

ITEMS_FILE = f"{DATA_DIR}/items.json"
PURCHASES_FILE = f"{DATA_DIR}/purchases.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


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


class ShopManager:
    def __init__(self):
        self.items = {}
        self.purchases = {}
        self.pending_payments = {}  # user_id -> {item_id, purchase_id}
        self.load_data()

    def load_data(self):
        try:
            if os.path.exists(ITEMS_FILE):
                with open(ITEMS_FILE, "r", encoding="utf-8") as f:
                    self.items = json.load(f)
            if os.path.exists(PURCHASES_FILE):
                with open(PURCHASES_FILE, "r", encoding="utf-8") as f:
                    self.purchases = json.load(f)
        except Exception as e:
            print(f"Error loading shop data: {e}")

    def save_items(self):
        with open(ITEMS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=2)

    def save_purchases(self):
        with open(PURCHASES_FILE, "w", encoding="utf-8") as f:
            json.dump(self.purchases, f, ensure_ascii=False, indent=2)

    def add_item(self, name, price, description, content, stock=-1):
        item_id = str(len(self.items) + 1)
        # 既存IDと被らないよう調整
        while item_id in self.items:
            item_id = str(int(item_id) + 1)
        self.items[item_id] = {
            "name": name,
            "price": int(price),
            "description": description,
            "content": content,
            "stock": stock,
            "created_at": time.time()
        }
        self.save_items()
        return item_id

    def remove_item(self, item_id):
        if str(item_id) in self.items:
            del self.items[str(item_id)]
            self.save_items()
            return True
        return False

    def get_item(self, item_id):
        return self.items.get(str(item_id))

    def get_all_items(self):
        return self.items

    def is_in_stock(self, item_id):
        item = self.get_item(item_id)
        if not item:
            return False
        return item["stock"] == -1 or item["stock"] > 0

    def create_purchase(self, user_id, item_id, link):
        purchase_id = str(uuid.uuid4())[:8].upper()
        self.purchases[purchase_id] = {
            "user_id": str(user_id),
            "item_id": str(item_id),
            "link": link,
            "status": "pending",
            "created_at": time.time()
        }
        self.save_purchases()
        return purchase_id

    def complete_purchase(self, purchase_id):
        if purchase_id not in self.purchases:
            return False
        self.purchases[purchase_id]["status"] = "completed"
        self.purchases[purchase_id]["completed_at"] = time.time()
        item_id = self.purchases[purchase_id]["item_id"]
        if item_id in self.items and self.items[item_id]["stock"] > 0:
            self.items[item_id]["stock"] -= 1
            self.save_items()
        self.save_purchases()
        return True


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

    def _get_any_client(self):
        for user_id, existing_client in self.clients.items():
            if existing_client:
                return existing_client
        for user_id, session in self.session_manager.sessions.items():
            if session.get("access_token"):
                try:
                    client = PayPay(access_token=session["access_token"])
                    self.clients[user_id] = client
                    return client
                except:
                    continue
        return None

    async def check_link(self, link_url):
        try:
            client = self._get_any_client()
            if not client:
                return False, None, "ログインしてね。"

            link_info = client.link_check(link_url)

            if hasattr(link_info, 'money') and hasattr(link_info, 'money_light'):
                total_amount = link_info.money + link_info.money_light
                has_password = getattr(link_info, 'has_password', False)
                return True, total_amount, f"金額: {total_amount}円 {'(パスワードあり)' if has_password else ''}"
            else:
                return False, None, f"リンク情報の取得に失敗: {link_info}"

        except Exception as e:
            print(f"Check link error: {e}")
            return False, None, f"リンク確認エラー: {e}"

    async def receive_link(self, user_id, link_url):
        try:
            client = await self.get_client(user_id)
            if not client:
                return False, "ログインしてね。"
            client.link_receive(link_url)
            return True, "受け取り完了"
        except Exception as e:
            print(f"Receive link error: {e}")
            return False, f"受け取りエラー: {e}"

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
                return False, "先に `/pay_login 電話番号 パスワード` で認証を行ってください。"

            if user_id not in self.pending_auth:
                return False, "認証セッションが見つかりません。もう一度 `/pay_login` からやり直してください。"

            client.login(otp)

            self.session_manager.update_user_tokens(
                user_id,
                client.access_token,
                client.refresh_token,
                client.device_uuid
            )

            del self.pending_auth[user_id]
            return True, "loginが完了しました！"
        except Exception as e:
            print(f"OTP verification error: {e}")
            return False, f"OTP認証エラー: {e}"

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

            balance_info = (
                f"【PayPay残高情報】\n"
                f"総残高: {all_balance}円\n"
                f"利用可能残高: {useable_balance}円\n"
                f"マネー: {money}円\n"
                f"マネーライト: {money_light}円\n"
                f"ポイント: {points}円"
            )
            return True, balance_info
        except Exception as e:
            print(f"Get balance error: {e}")
            return False, f"残高確認エラー: {e}"

    def _validate_phone(self, phone):
        pattern = r'^(0[5-9]0[0-9]{8}|0[5-9]0-[0-9]{4}-[0-9]{4})$'
        return re.match(pattern, phone) is not None


bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)
session_manager = SessionManager()
paypay_manager = PayPayManager(session_manager)
shop_manager = ShopManager()


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return any(r.name == ADMIN_ROLE_NAME for r in interaction.user.roles)


# ─── 管理者コマンド ────────────────────────────────────────────

@bot.tree.command(name="add_item", description="【管理者】商品を追加")
@discord.app_commands.describe(
    name="商品名",
    price="価格（円）",
    description="商品の説明",
    content="購入後に送る内容（URLやテキストなど）",
    stock="在庫数（-1で無制限）"
)
async def add_item_command(interaction: discord.Interaction, name: str, price: int, description: str, content: str, stock: int = -1):
    if not is_admin(interaction):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    item_id = shop_manager.add_item(name, price, description, content, stock)
    stock_text = "無制限" if stock == -1 else f"{stock}個"
    await interaction.followup.send(
        f"商品を追加しました！\nID: `{item_id}` | {name} | {price}円 | 在庫: {stock_text}",
        ephemeral=True
    )


@bot.tree.command(name="remove_item", description="【管理者】商品を削除")
@discord.app_commands.describe(item_id="削除する商品ID")
async def remove_item_command(interaction: discord.Interaction, item_id: str):
    if not is_admin(interaction):
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if shop_manager.remove_item(item_id):
        await interaction.followup.send(f"商品 `{item_id}` を削除しました。", ephemeral=True)
    else:
        await interaction.followup.send(f"商品 `{item_id}` が見つかりません。", ephemeral=True)


@bot.tree.command(name="pay_login", description="【管理者】PayPayアカウントで認証を開始（DMのみ）")
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
    except discord.errors.NotFound:
        pass
    except Exception as e:
        print(traceback.format_exc())


@bot.tree.command(name="verify", description="【管理者】SMSで受け取ったOTPコードを入力（DMのみ）")
@discord.app_commands.describe(code="SMSで受け取ったOTPコード")
async def otp_command(interaction: discord.Interaction, code: str):
    try:
        if not isinstance(interaction.channel, discord.DMChannel):
            await interaction.response.send_message("このコマンドはDMのみで使用可能です。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.verify_otp(interaction.user.id, code)
        await interaction.followup.send(msg)
    except discord.errors.NotFound:
        pass
    except Exception as e:
        print(traceback.format_exc())


@bot.tree.command(name="get_balance", description="【管理者】現在の残高を確認（DMのみ）")
async def balance_command(interaction: discord.Interaction):
    try:
        if not isinstance(interaction.channel, discord.DMChannel):
            await interaction.response.send_message("このコマンドはDMのみで使用可能です。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        success, msg = await paypay_manager.get_balance(interaction.user.id)
        await interaction.followup.send(msg)
    except discord.errors.NotFound:
        pass
    except Exception as e:
        print(traceback.format_exc())


# ─── ユーザーコマンド ──────────────────────────────────────────

@bot.tree.command(name="shop", description="商品一覧を表示")
async def shop_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    items = shop_manager.get_all_items()
    if not items:
        await interaction.followup.send("現在販売中の商品はありません。")
        return

    embed = discord.Embed(
        title="🏪 自販機ショップ",
        description="購入は `/buy <商品ID>` で！",
        color=0x00bfff
    )

    for item_id, item in items.items():
        stock_text = "無制限" if item["stock"] == -1 else (f"{item['stock']}個" if item["stock"] > 0 else "売り切れ")
        embed.add_field(
            name=f"[{item_id}] {item['name']} — {item['price']}円",
            value=f"{item['description']}\n在庫: {stock_text}",
            inline=False
        )

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="buy", description="商品を購入する")
@discord.app_commands.describe(item_id="購入する商品のID")
async def buy_command(interaction: discord.Interaction, item_id: str):
    await interaction.response.defer(ephemeral=True)

    item = shop_manager.get_item(item_id)
    if not item:
        await interaction.followup.send(f"商品 `{item_id}` が見つかりません。`/shop` で一覧を確認してください。", ephemeral=True)
        return

    if not shop_manager.is_in_stock(item_id):
        await interaction.followup.send("この商品は売り切れです。", ephemeral=True)
        return

    shop_manager.pending_payments[str(interaction.user.id)] = {"item_id": item_id}

    try:
        await interaction.user.send(
            f"**【購入手続き】{item['name']} — {item['price']}円**\n\n"
            f"以下の手順で購入してください：\n"
            f"1. PayPayアプリで **{item['price']}円** の送金リンクを作成\n"
            f"2. 作成したリンクを `/submit_payment <リンク>` で送信\n\n"
            f"※ 金額が一致しない場合は受け付けられません。\n"
            f"※ このセッションは30分間有効です。"
        )
        await interaction.followup.send("DMに購入手続きの案内を送りました！", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            f"DMを送れませんでした。DMを許可してから再度お試しください。\n\n"
            f"購入手順：PayPayで **{item['price']}円** の送金リンクを作成し、`/submit_payment <リンク>` で送信してください。",
            ephemeral=True
        )


@bot.tree.command(name="submit_payment", description="PayPayリンクを送って購入を完了する")
@discord.app_commands.describe(link="PayPayの送金リンク")
async def submit_payment_command(interaction: discord.Interaction, link: str):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    pending = shop_manager.pending_payments.get(user_id)

    if not pending:
        await interaction.followup.send(
            "購入待ちの商品がありません。先に `/buy <商品ID>` を実行してください。",
            ephemeral=True
        )
        return

    item_id = pending["item_id"]
    item = shop_manager.get_item(item_id)
    if not item:
        del shop_manager.pending_payments[user_id]
        await interaction.followup.send("商品が見つかりません。", ephemeral=True)
        return

    if not shop_manager.is_in_stock(item_id):
        del shop_manager.pending_payments[user_id]
        await interaction.followup.send("申し訳ありません。この商品は売り切れになりました。", ephemeral=True)
        return

    # リンクの金額確認
    ok, amount, msg = await paypay_manager.check_link(link)
    if not ok:
        await interaction.followup.send(f"リンクの確認に失敗しました: {msg}", ephemeral=True)
        return

    if amount != item["price"]:
        await interaction.followup.send(
            f"金額が一致しません。\n必要金額: **{item['price']}円** / リンクの金額: **{amount}円**\n正しい金額で再度お試しください。",
            ephemeral=True
        )
        return

    # 受け取り処理
    receive_ok, receive_msg = await paypay_manager.receive_link(user_id, link)
    if not receive_ok:
        # 受け取り失敗でも管理者に通知してログ保存
        print(f"[WARN] receive_link failed for user {user_id}: {receive_msg}")

    # 購入完了
    purchase_id = shop_manager.create_purchase(user_id, item_id, link)
    shop_manager.complete_purchase(purchase_id)
    del shop_manager.pending_payments[user_id]

    # 商品を納品
    try:
        await interaction.user.send(
            f"**✅ 購入完了！ありがとうございます！**\n\n"
            f"**{item['name']}** の内容をお届けします：\n\n"
            f"```\n{item['content']}\n```\n\n"
            f"購入ID: `{purchase_id}`"
        )
        await interaction.followup.send("購入が完了しました！商品をDMで送りました。", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            f"**✅ 購入完了！**\n\n"
            f"DMが送れませんでした。以下が商品内容です：\n\n"
            f"```\n{item['content']}\n```\n\n"
            f"購入ID: `{purchase_id}`",
            ephemeral=True
        )


@bot.tree.command(name="vending_help", description="自販機botのコマンド一覧を表示")
async def vending_help_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    embed = discord.Embed(
        title="🏪 自販機Bot — コマンド一覧",
        color=0x00bfff
    )
    embed.add_field(
        name="ショッピング",
        value=(
            "`/shop` — 商品一覧を見る\n"
            "`/buy <ID>` — 商品を購入する\n"
            "`/submit_payment <リンク>` — PayPayリンクを送って購入完了"
        ),
        inline=False
    )
    embed.add_field(
        name="管理者専用",
        value=(
            "`/add_item` — 商品を追加\n"
            "`/remove_item <ID>` — 商品を削除\n"
            "`/pay_login` — PayPay認証開始（DM）\n"
            "`/verify` — OTP入力（DM）\n"
            "`/get_balance` — 残高確認（DM）"
        ),
        inline=False
    )
    embed.set_footer(text="セキュリティ上、認証・残高確認はDMのみ使用可能です。")
    await interaction.followup.send(embed=embed)


# ─── イベント ─────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    try:
        await bot.tree.sync()
        print("Slash commands synced!")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Error in event {event}: {traceback.format_exc()}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("引数が足りません。`/vending_help` でコマンドの使い方を確認してください。")
    else:
        print(f"Command error: {error}")
        print(traceback.format_exc())


def main():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
