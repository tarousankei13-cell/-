"""Discord UI components: Views, Modals, Embeds, and DynamicItems."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import discord

from models import (
    DecodedOrderInfo,
    EmbedColor,
    OrderStatus,
    TransactionType,
    calculate_user_amount,
    validate_hex,
)

if TYPE_CHECKING:
    from db import Database
    from mcd_adapter import MCDAdapter

logger = logging.getLogger("bot.views")

ORDERS_PER_PAGE = 5
TX_PER_PAGE = 8


def _get_db(interaction: discord.Interaction) -> "Database":
    return interaction.client.db  # type: ignore[attr-defined]


def _get_mcd(interaction: discord.Interaction) -> "MCDAdapter":
    return interaction.client.mcd  # type: ignore[attr-defined]


# ── Embed builders ─────────────────────────────────────────


async def build_panel_embed(db: "Database") -> discord.Embed:
    settings = await db.get_all_settings()
    rate = int(settings.get("user_rate", "60"))
    discount = 100 - rate
    minimum = int(settings.get("min_order_amount", "400"))
    maintenance = settings.get("maintenance", "0") == "1"
    accepting = settings.get("accepting_orders", "1") == "1"

    if maintenance:
        status_text = "🔴 メンテナンス中"
    elif accepting:
        status_text = "🟢 受付中"
    else:
        status_text = "🔴 受付停止中"

    embed = discord.Embed(
        title="McDonald's Concierge",
        description=(
            "モバイルオーダー代行サービス\n\n"
            "定価の一部を当サービスが負担し、\n"
            "お得にご注文いただけます。"
        ),
        color=EmbedColor.PRIMARY,
    )
    embed.add_field(name="割引率", value=f"**{discount}% OFF**", inline=True)
    embed.add_field(name="最低注文額", value=f"**¥{minimum:,}**", inline=True)
    embed.add_field(name="受付状況", value=status_text, inline=True)
    embed.set_footer(text="McDonald's Concierge")
    return embed


def build_order_confirm_embed(
    decoded: DecodedOrderInfo,
    user_amount: int,
    subsidy: int,
    balance: int,
) -> discord.Embed:
    store_display = decoded.store_name or decoded.store_id or "不明"
    if decoded.store_name and decoded.store_id:
        store_display = f"{decoded.store_name} ({decoded.store_id})"

    product_lines: list[str] = []
    for p in decoded.products:
        name = p.display_name or p.product_id
        product_lines.append(f"  {name}")
        for addon in p.addons:
            addon_name = addon.display_name or addon.product_id
            product_lines.append(f"    + {addon_name}")
    products_text = "\n".join(product_lines) if product_lines else "  (商品情報なし)"

    after_balance = balance - user_amount

    embed = discord.Embed(
        title="注文内容の確認",
        description="以下の内容で注文します。",
        color=EmbedColor.INFO,
    )
    embed.add_field(
        name="店舗",
        value=store_display,
        inline=True,
    )
    embed.add_field(
        name="受取方法",
        value=decoded.pickup_method or "不明",
        inline=True,
    )
    embed.add_field(
        name="商品",
        value=f"```\n{products_text}\n```",
        inline=False,
    )
    embed.add_field(name="定価", value=f"¥{decoded.total_amount:,}", inline=True)
    embed.add_field(name="代行負担", value=f"¥{subsidy:,}", inline=True)
    embed.add_field(name="お支払い", value=f"**¥{user_amount:,}**", inline=True)
    embed.add_field(name="現在残高", value=f"¥{balance:,}", inline=True)
    embed.add_field(name="注文後残高", value=f"¥{after_balance:,}", inline=True)
    embed.set_footer(text="確定ボタンを押すと残高から引き落とされます")
    return embed


def build_processing_embed(order_id: int) -> discord.Embed:
    return discord.Embed(
        title="⚙ 注文処理中",
        description=f"注文 #{order_id:04d} を処理しています...\nしばらくお待ちください。",
        color=EmbedColor.WARNING,
    )


def build_order_result_embed(
    order_id: int,
    status: OrderStatus,
    receipt_number: str = "",
    store_name: str = "",
    user_amount: int = 0,
    error_info: str = "",
) -> discord.Embed:
    if status == OrderStatus.COMPLETED:
        embed = discord.Embed(
            title="✅ 注文完了",
            description="ご注文が完了しました。",
            color=EmbedColor.SUCCESS,
        )
        if receipt_number:
            embed.add_field(
                name="受取番号", value=f"**#{receipt_number}**", inline=True
            )
        if store_name:
            embed.add_field(name="店舗", value=store_name, inline=True)
        embed.add_field(name="お支払い", value=f"¥{user_amount:,}", inline=True)
        embed.add_field(name="注文ID", value=f"#{order_id:04d}", inline=True)
    elif status == OrderStatus.MANUAL_REVIEW:
        embed = discord.Embed(
            title="🔍 確認待ち",
            description=(
                f"注文 #{order_id:04d} は管理者の確認が必要です。\n"
                "残高は確保済みです。結果は追ってお知らせします。"
            ),
            color=EmbedColor.WARNING,
        )
    elif status == OrderStatus.FAILED:
        embed = discord.Embed(
            title="❌ 注文失敗",
            description=(
                f"注文 #{order_id:04d} の処理に失敗しました。\n"
                "残高は返金されました。"
            ),
            color=EmbedColor.ERROR,
        )
        if error_info:
            embed.add_field(name="エラー", value=error_info[:200], inline=False)
    else:
        embed = discord.Embed(
            title=f"{status.emoji} {status.display}",
            description=f"注文 #{order_id:04d}",
            color=EmbedColor.DARK,
        )
    embed.set_footer(text="McDonald's Concierge")
    return embed


async def build_achievement_embed(
    db: "Database", order: dict
) -> discord.Embed:
    total_completed = await db.get_completed_order_count()
    embed = discord.Embed(
        title="ORDER COMPLETED",
        description="ご注文ありがとうございました。",
        color=EmbedColor.PRIMARY,
    )
    embed.add_field(
        name="店舗",
        value=order.get("store_name") or order.get("store_id", "不明"),
        inline=True,
    )
    embed.add_field(
        name="ご利用額",
        value=f"¥{order['user_amount']:,}",
        inline=True,
    )
    embed.add_field(
        name="通常価格",
        value=f"¥{order['total_amount']:,}",
        inline=True,
    )
    if order.get("receipt_number"):
        embed.add_field(
            name="受取番号",
            value=f"#{order['receipt_number']}",
            inline=True,
        )
    embed.add_field(
        name="注文ID", value=f"#{order['id']:04d}", inline=True
    )
    embed.set_footer(text=f"Total Orders: {total_completed}")
    return embed


def build_balance_embed(
    user: discord.User | discord.Member, balance: int
) -> discord.Embed:
    embed = discord.Embed(
        title="残高情報",
        color=EmbedColor.PRIMARY,
    )
    embed.add_field(name="ユーザー", value=user.display_name, inline=True)
    embed.add_field(name="現在残高", value=f"**¥{balance:,}**", inline=True)
    embed.set_footer(text="McDonald's Concierge")
    return embed


def build_history_embed(
    orders: list[dict], page: int, total_pages: int
) -> discord.Embed:
    embed = discord.Embed(
        title="注文履歴",
        description=f"ページ {page}/{total_pages}" if total_pages > 0 else "注文履歴はありません。",
        color=EmbedColor.DARK,
    )
    for o in orders:
        status = OrderStatus(o["status"])
        store = o.get("store_name") or o.get("store_id") or "不明"
        line = (
            f"{status.emoji} ¥{o['user_amount']:,}  {store}"
        )
        if o.get("receipt_number"):
            line += f"  #{o['receipt_number']}"
        embed.add_field(
            name=f"#{o['id']:04d}  {o['created_at'][:10]}",
            value=line,
            inline=False,
        )
    if not orders:
        embed.add_field(name="​", value="注文履歴はありません。", inline=False)
    embed.set_footer(text="McDonald's Concierge")
    return embed


def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="使い方",
        description="McDonald's Concierge の使い方",
        color=EmbedColor.INFO,
    )
    embed.add_field(
        name="1. 入金",
        value="「💳 入金」から入金申請を行い、管理者の承認を待ちます。",
        inline=False,
    )
    embed.add_field(
        name="2. 注文",
        value=(
            "「🍔 注文する」からHexデータを入力します。\n"
            "内容を確認し、確定ボタンで注文が実行されます。"
        ),
        inline=False,
    )
    embed.add_field(
        name="3. 確認",
        value="「💰 残高」で残高、「📋 注文履歴」で過去の注文を確認できます。",
        inline=False,
    )
    embed.set_footer(text="McDonald's Concierge")
    return embed


def build_deposit_info_embed(
    amount: int, instruction: str
) -> discord.Embed:
    embed = discord.Embed(
        title="入金申請",
        description=instruction,
        color=EmbedColor.INFO,
    )
    embed.add_field(name="入金額", value=f"**¥{amount:,}**", inline=False)
    embed.set_footer(text="確認ボタンを押すと入金申請が送信されます")
    return embed


def build_admin_deposit_embed(
    user: discord.User | discord.Member | None,
    user_id: int,
    amount: int,
    deposit_id: int,
) -> discord.Embed:
    name = user.display_name if user else f"ID:{user_id}"
    embed = discord.Embed(
        title="入金申請",
        description=f"**{name}** から入金申請があります。",
        color=EmbedColor.WARNING,
    )
    embed.add_field(name="申請ID", value=f"#{deposit_id}", inline=True)
    embed.add_field(name="金額", value=f"¥{amount:,}", inline=True)
    embed.add_field(name="ユーザーID", value=str(user_id), inline=True)
    return embed


def build_tx_history_embed(
    txs: list[dict], page: int, total_pages: int
) -> discord.Embed:
    embed = discord.Embed(
        title="取引履歴",
        description=f"ページ {page}/{total_pages}" if total_pages > 0 else "取引履歴はありません。",
        color=EmbedColor.DARK,
    )
    for tx in txs:
        try:
            tx_type = TransactionType(tx["type"]).display
        except ValueError:
            tx_type = tx["type"]
        sign = "+" if tx["amount"] >= 0 else ""
        embed.add_field(
            name=f"{tx['created_at'][:16]}",
            value=(
                f"{tx_type}  {sign}¥{tx['amount']:,}\n"
                f"残高: ¥{tx['balance_after']:,}"
                + (f"\n{tx['reason']}" if tx.get("reason") else "")
            ),
            inline=False,
        )
    if not txs:
        embed.add_field(name="​", value="取引履歴はありません。", inline=False)
    embed.set_footer(text="McDonald's Concierge")
    return embed


async def send_admin_log(
    bot: discord.Client, title: str, description: str, **fields: str
) -> None:
    db: "Database" = bot.db  # type: ignore[attr-defined]
    channel_id = await db.get_setting("admin_log_channel_id")
    if not channel_id:
        return
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        embed = discord.Embed(
            title=title, description=description, color=EmbedColor.DARK
        )
        for k, v in fields.items():
            embed.add_field(name=k, value=v, inline=True)
        embed.set_footer(text="McDonald's Concierge Admin Log")
        await channel.send(embed=embed)  # type: ignore[union-attr]
    except Exception as exc:
        logger.error("Failed to send admin log: %s", exc)


async def post_achievement(bot: discord.Client, order: dict) -> bool:
    db: "Database" = bot.db  # type: ignore[attr-defined]
    if order.get("achievement_posted"):
        return False
    channel_id = await db.get_setting("achievement_channel_id")
    if not channel_id:
        return False
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        embed = await build_achievement_embed(db, order)
        await channel.send(embed=embed)  # type: ignore[union-attr]
        await db.set_achievement_posted(order["id"])
        return True
    except Exception as exc:
        logger.error("Failed to post achievement for order #%s: %s", order["id"], exc)
        return False


# ── Persistent panel view ──────────────────────────────────


class PanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,  # type: ignore[type-arg]
    ) -> None:
        logger.error("PanelView error: %s", error, exc_info=True)
        embed = discord.Embed(
            title="エラー",
            description="処理中にエラーが発生しました。",
            color=EmbedColor.ERROR,
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(
        label="注文する",
        emoji="🍔",
        style=discord.ButtonStyle.success,
        custom_id="panel:order",
        row=0,
    )
    async def order_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        db = _get_db(interaction)
        if await db.get_setting("maintenance") == "1":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="メンテナンス中",
                    description="現在メンテナンス中です。しばらくお待ちください。",
                    color=EmbedColor.WARNING,
                ),
                ephemeral=True,
            )
            return
        if await db.get_setting("accepting_orders") != "1":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="受付停止中",
                    description="現在注文の受付を停止しています。",
                    color=EmbedColor.WARNING,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(HexInputModal())

    @discord.ui.button(
        label="入金",
        emoji="💳",
        style=discord.ButtonStyle.primary,
        custom_id="panel:deposit",
        row=0,
    )
    async def deposit_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(DepositAmountModal())

    @discord.ui.button(
        label="残高",
        emoji="💰",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:balance",
        row=0,
    )
    async def balance_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        db = _get_db(interaction)
        balance = await db.get_balance(interaction.user.id)
        embed = build_balance_embed(interaction.user, balance)
        view = BalanceDetailView(interaction.user.id)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    @discord.ui.button(
        label="注文履歴",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:history",
        row=1,
    )
    async def history_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        db = _get_db(interaction)
        uid = interaction.user.id
        total = await db.get_user_order_count(uid)
        total_pages = max(1, (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
        orders = await db.get_user_orders(uid, limit=ORDERS_PER_PAGE, offset=0)
        embed = build_history_embed(orders, 1, total_pages)
        view = HistoryView(uid, 1, total_pages)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    @discord.ui.button(
        label="使い方",
        emoji="❓",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:help",
        row=1,
    )
    async def help_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        embed = build_help_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Hex input modal ────────────────────────────────────────


class HexInputModal(discord.ui.Modal, title="注文データ入力"):
    hex_input = discord.ui.TextInput(
        label="Hex Stream",
        style=discord.TextStyle.paragraph,
        placeholder="Hexデータを貼り付けてください",
        required=True,
        max_length=10000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        db = _get_db(interaction)
        mcd = _get_mcd(interaction)
        hex_str = self.hex_input.value.strip()

        ok, err_msg = validate_hex(hex_str)
        if not ok:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="入力エラー", description=err_msg, color=EmbedColor.ERROR
                ),
                ephemeral=True,
            )
            return

        try:
            decoded = await mcd.decode_hex(hex_str)
        except Exception as exc:
            logger.error("Hex decode error: %s", exc)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="解析エラー",
                    description="Hexデータの解析に失敗しました。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return

        if decoded.total_amount <= 0:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="解析エラー",
                    description="金額情報を取得できませんでした。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return

        settings = await db.get_all_settings()
        rate = int(settings.get("user_rate", "60"))
        minimum = int(settings.get("min_order_amount", "400"))

        user_amount = calculate_user_amount(decoded.total_amount, rate)
        subsidy = decoded.total_amount - user_amount

        if decoded.total_amount < minimum:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="注文不可",
                    description=f"最低注文額 ¥{minimum:,} 未満のため注文できません。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return

        balance = await db.get_balance(interaction.user.id)
        if balance < user_amount:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="残高不足",
                    description=(
                        f"残高が不足しています。\n"
                        f"必要額: ¥{user_amount:,}\n"
                        f"現在残高: ¥{balance:,}\n"
                        f"不足額: ¥{user_amount - balance:,}"
                    ),
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = build_order_confirm_embed(decoded, user_amount, subsidy, balance)
        view = OrderConfirmView(
            user_id=interaction.user.id,
            decoded=decoded,
            user_amount=user_amount,
            subsidy=subsidy,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.error("HexInputModal error: %s", error, exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="エラー",
                        description="処理中にエラーが発生しました。",
                        color=EmbedColor.ERROR,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="エラー",
                        description="処理中にエラーが発生しました。",
                        color=EmbedColor.ERROR,
                    ),
                    ephemeral=True,
                )
        except Exception:
            pass


# ── Order confirm view ─────────────────────────────────────


class OrderConfirmView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        decoded: DecodedOrderInfo,
        user_amount: int,
        subsidy: int,
    ) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.decoded = decoded
        self.user_amount = user_amount
        self.subsidy = subsidy
        self._processed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="注文を確定", emoji="✅", style=discord.ButtonStyle.success
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._processed:
            await interaction.response.send_message(
                "この注文は既に処理済みです。", ephemeral=True
            )
            return
        self._processed = True
        self.stop()

        db = _get_db(interaction)
        mcd = _get_mcd(interaction)
        decoded = self.decoded

        products_data = [
            {
                "id": p.product_id,
                "name": p.display_name,
                "addons": [{"id": a.product_id, "name": a.display_name} for a in p.addons],
            }
            for p in decoded.products
        ]

        try:
            order_id = await db.create_order_with_payment(
                user_id=self.user_id,
                store_id=decoded.store_id,
                store_name=decoded.store_name,
                pickup_method=decoded.pickup_method,
                total_amount=decoded.total_amount,
                user_amount=self.user_amount,
                subsidy_amount=self.subsidy,
                hex_data=decoded.raw_hex,
                products_json=json.dumps(products_data, ensure_ascii=False),
            )
        except ValueError as exc:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="注文失敗", description=str(exc), color=EmbedColor.ERROR
                ),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=build_processing_embed(order_id), view=None
        )

        if mcd.is_enabled:
            await db.update_order_status(order_id, OrderStatus.PROCESSING)
            result = await mcd.execute_order(decoded.raw_hex)

            if result.get("success"):
                receipt = result.get("receipt_number", "")
                s_name = result.get("store_name", "") or decoded.store_name
                await db.update_order_status(
                    order_id,
                    OrderStatus.COMPLETED,
                    receipt_number=receipt,
                    order_token=result.get("order_token", ""),
                    order_group=result.get("order_group", ""),
                )
                embed = build_order_result_embed(
                    order_id, OrderStatus.COMPLETED,
                    receipt_number=receipt,
                    store_name=s_name,
                    user_amount=self.user_amount,
                )
                await interaction.edit_original_response(embed=embed)

                order = await db.get_order(order_id)
                if order:
                    await post_achievement(interaction.client, order)
                    await send_admin_log(
                        interaction.client,
                        "注文完了",
                        f"注文 #{order_id:04d} が完了しました。",
                        ユーザー=interaction.user.display_name,
                        金額=f"¥{self.user_amount:,}",
                        店舗=s_name,
                    )
            elif result.get("unknown_state"):
                await db.update_order_status(
                    order_id, OrderStatus.MANUAL_REVIEW,
                    error_info=result.get("error", "タイムアウト"),
                )
                embed = build_order_result_embed(
                    order_id, OrderStatus.MANUAL_REVIEW
                )
                await interaction.edit_original_response(embed=embed)
                await send_admin_log(
                    interaction.client,
                    "要確認",
                    f"注文 #{order_id:04d} は確認が必要です。",
                    ユーザー=interaction.user.display_name,
                    理由=result.get("error", "不明")[:200],
                )
            else:
                err = result.get("error", "不明なエラー")
                await db.update_order_status(
                    order_id, OrderStatus.FAILED, error_info=err
                )
                await db.add_balance(
                    self.user_id,
                    self.user_amount,
                    TransactionType.REFUND,
                    reason=f"注文 #{order_id} 失敗返金",
                    order_id=order_id,
                )
                embed = build_order_result_embed(
                    order_id, OrderStatus.FAILED, error_info=err
                )
                await interaction.edit_original_response(embed=embed)
                await send_admin_log(
                    interaction.client,
                    "注文失敗",
                    f"注文 #{order_id:04d} が失敗しました。残高を返金しました。",
                    ユーザー=interaction.user.display_name,
                    エラー=err[:200],
                )
        else:
            await db.update_order_status(
                order_id,
                OrderStatus.PROCESSING,
            )
            await db.update_order_status(
                order_id,
                OrderStatus.MANUAL_REVIEW,
                error_info="外部APIが未設定のため管理者による手動処理が必要です",
            )
            embed = build_order_result_embed(
                order_id, OrderStatus.MANUAL_REVIEW
            )
            await interaction.edit_original_response(embed=embed)
            await send_admin_log(
                interaction.client,
                "注文受付（手動処理）",
                f"注文 #{order_id:04d} を受け付けました。外部API未設定のため手動処理が必要です。",
                ユーザー=interaction.user.display_name,
                金額=f"¥{self.user_amount:,}",
            )

    @discord.ui.button(
        label="キャンセル", emoji="✕", style=discord.ButtonStyle.danger
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="キャンセル",
                description="注文をキャンセルしました。",
                color=EmbedColor.DARK,
            ),
            view=None,
        )


# ── Deposit modals & views ─────────────────────────────────


class DepositAmountModal(discord.ui.Modal, title="入金申請"):
    amount_input = discord.ui.TextInput(
        label="入金額（円）",
        placeholder="例: 1000",
        required=True,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount_input.value.strip()
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="入力エラー",
                    description="金額は数値で入力してください。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="入力エラー",
                    description="金額は1円以上で入力してください。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return

        db = _get_db(interaction)
        instruction = await db.get_setting("deposit_instruction")
        embed = build_deposit_info_embed(amount, instruction)
        view = DepositConfirmView(interaction.user.id, amount)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.error("DepositAmountModal error: %s", error, exc_info=True)
        try:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="エラー",
                    description="処理中にエラーが発生しました。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
        except Exception:
            pass


class DepositConfirmView(discord.ui.View):
    def __init__(self, user_id: int, amount: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.amount = amount
        self._processed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="入金申請", emoji="✅", style=discord.ButtonStyle.success
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._processed:
            await interaction.response.send_message(
                "この申請は既に処理済みです。", ephemeral=True
            )
            return
        self._processed = True
        self.stop()

        db = _get_db(interaction)
        deposit_id = await db.create_deposit(self.user_id, self.amount)

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="入金申請完了",
                description=(
                    f"入金申請 #{deposit_id} を受け付けました。\n"
                    f"金額: ¥{self.amount:,}\n\n"
                    "管理者の承認をお待ちください。"
                ),
                color=EmbedColor.SUCCESS,
            ),
            view=None,
        )

        admin_ch_id = await db.get_setting("admin_log_channel_id")
        if admin_ch_id:
            try:
                ch = interaction.client.get_channel(int(admin_ch_id))
                if ch is None:
                    ch = await interaction.client.fetch_channel(int(admin_ch_id))
                embed = build_admin_deposit_embed(
                    interaction.user, self.user_id, self.amount, deposit_id
                )
                dep_view = discord.ui.View(timeout=None)
                dep_view.add_item(DepositApproveButton(deposit_id))
                dep_view.add_item(DepositRejectButton(deposit_id))
                msg = await ch.send(embed=embed, view=dep_view)  # type: ignore[union-attr]
                await db.update_deposit_message(deposit_id, msg.id, ch.id)  # type: ignore[union-attr]
            except Exception as exc:
                logger.error("Failed to send deposit notification: %s", exc)

    @discord.ui.button(
        label="キャンセル", emoji="✕", style=discord.ButtonStyle.danger
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="キャンセル",
                description="入金申請をキャンセルしました。",
                color=EmbedColor.DARK,
            ),
            view=None,
        )


# ── Deposit dynamic items (persistent across restart) ──────


class DepositApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"deposit:approve:(?P<id>[0-9]+)",
):
    def __init__(self, deposit_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="承認",
                custom_id=f"deposit:approve:{deposit_id}",
            )
        )
        self.deposit_id = deposit_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "DepositApproveButton":
        return cls(deposit_id=int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await interaction.client.is_owner(interaction.user):  # type: ignore[arg-type]
            await interaction.response.send_message(
                "管理者のみ操作できます。", ephemeral=True
            )
            return
        db = _get_db(interaction)
        try:
            user_id, amount, new_balance = await db.approve_deposit(
                self.deposit_id, interaction.user.id
            )
        except ValueError as exc:
            await interaction.response.send_message(
                str(exc), ephemeral=True
            )
            return

        embed = discord.Embed(
            title="✅ 入金承認済",
            description=(
                f"入金 #{self.deposit_id} を承認しました。\n"
                f"ユーザーID: {user_id}\n"
                f"金額: ¥{amount:,}\n"
                f"新残高: ¥{new_balance:,}"
            ),
            color=EmbedColor.SUCCESS,
        )
        await interaction.response.edit_message(embed=embed, view=None)

        try:
            user = await interaction.client.fetch_user(user_id)
            await user.send(
                embed=discord.Embed(
                    title="入金完了",
                    description=(
                        f"入金 #{self.deposit_id} が承認されました。\n"
                        f"入金額: ¥{amount:,}\n"
                        f"現在残高: ¥{new_balance:,}"
                    ),
                    color=EmbedColor.SUCCESS,
                )
            )
        except Exception:
            pass

        await send_admin_log(
            interaction.client,
            "入金承認",
            f"入金 #{self.deposit_id} を承認しました。",
            ユーザーID=str(user_id),
            金額=f"¥{amount:,}",
            承認者=interaction.user.display_name,
        )


class DepositRejectButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"deposit:reject:(?P<id>[0-9]+)",
):
    def __init__(self, deposit_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="却下",
                custom_id=f"deposit:reject:{deposit_id}",
            )
        )
        self.deposit_id = deposit_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "DepositRejectButton":
        return cls(deposit_id=int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await interaction.client.is_owner(interaction.user):  # type: ignore[arg-type]
            await interaction.response.send_message(
                "管理者のみ操作できます。", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            DepositRejectReasonModal(self.deposit_id)
        )


class DepositRejectReasonModal(discord.ui.Modal, title="入金却下"):
    reason_input = discord.ui.TextInput(
        label="却下理由",
        placeholder="理由を入力してください（任意）",
        required=False,
        max_length=500,
    )

    def __init__(self, deposit_id: int) -> None:
        super().__init__()
        self.deposit_id = deposit_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = _get_db(interaction)
        reason = self.reason_input.value.strip()
        try:
            user_id, amount = await db.reject_deposit(
                self.deposit_id, interaction.user.id, reason
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        dep = await db.get_deposit(self.deposit_id)
        if dep and dep.get("message_id") and dep.get("channel_id"):
            try:
                ch = interaction.client.get_channel(dep["channel_id"])
                if ch is None:
                    ch = await interaction.client.fetch_channel(dep["channel_id"])
                msg = await ch.fetch_message(dep["message_id"])  # type: ignore[union-attr]
                embed = discord.Embed(
                    title="❌ 入金却下済",
                    description=(
                        f"入金 #{self.deposit_id} を却下しました。\n"
                        f"ユーザーID: {user_id}\n"
                        f"金額: ¥{amount:,}"
                        + (f"\n理由: {reason}" if reason else "")
                    ),
                    color=EmbedColor.ERROR,
                )
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass

        await interaction.response.send_message(
            embed=discord.Embed(
                title="却下完了",
                description=f"入金 #{self.deposit_id} を却下しました。",
                color=EmbedColor.DARK,
            ),
            ephemeral=True,
        )

        try:
            user = await interaction.client.fetch_user(user_id)
            desc = f"入金申請 #{self.deposit_id} (¥{amount:,}) は却下されました。"
            if reason:
                desc += f"\n理由: {reason}"
            await user.send(
                embed=discord.Embed(
                    title="入金却下", description=desc, color=EmbedColor.ERROR
                )
            )
        except Exception:
            pass

        await send_admin_log(
            interaction.client,
            "入金却下",
            f"入金 #{self.deposit_id} を却下しました。",
            ユーザーID=str(user_id),
            金額=f"¥{amount:,}",
            理由=reason or "(なし)",
            操作者=interaction.user.display_name,
        )


# ── History view (paginated) ──────────────────────────────


class HistoryView(discord.ui.View):
    def __init__(self, user_id: int, page: int, total_pages: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.page = page
        self.total_pages = total_pages
        self.prev_btn.disabled = page <= 1
        self.next_btn.disabled = page >= total_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    async def _load_page(
        self, interaction: discord.Interaction, page: int
    ) -> None:
        db = _get_db(interaction)
        offset = (page - 1) * ORDERS_PER_PAGE
        orders = await db.get_user_orders(
            self.user_id, limit=ORDERS_PER_PAGE, offset=offset
        )
        embed = build_history_embed(orders, page, self.total_pages)
        view = HistoryView(self.user_id, page, self.total_pages)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._load_page(interaction, self.page - 1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._load_page(interaction, self.page + 1)

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.danger)
    async def close_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="閉じました。", embed=None, view=None
        )
        self.stop()


# ── Balance detail view ────────────────────────────────────


class BalanceDetailView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="取引履歴", emoji="📋", style=discord.ButtonStyle.secondary
    )
    async def tx_history(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        db = _get_db(interaction)
        total = await db.get_transaction_count(self.user_id)
        total_pages = max(1, (total + TX_PER_PAGE - 1) // TX_PER_PAGE)
        txs = await db.get_transactions(self.user_id, limit=TX_PER_PAGE, offset=0)
        embed = build_tx_history_embed(txs, 1, total_pages)
        view = TxHistoryView(self.user_id, 1, total_pages)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Transaction history view (paginated) ───────────────────


class TxHistoryView(discord.ui.View):
    def __init__(self, user_id: int, page: int, total_pages: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.page = page
        self.total_pages = total_pages
        self.prev_btn.disabled = page <= 1
        self.next_btn.disabled = page >= total_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    async def _load_page(
        self, interaction: discord.Interaction, page: int
    ) -> None:
        db = _get_db(interaction)
        offset = (page - 1) * TX_PER_PAGE
        txs = await db.get_transactions(
            self.user_id, limit=TX_PER_PAGE, offset=offset
        )
        embed = build_tx_history_embed(txs, page, self.total_pages)
        view = TxHistoryView(self.user_id, page, self.total_pages)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._load_page(interaction, self.page - 1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._load_page(interaction, self.page + 1)

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.danger)
    async def close_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="閉じました。", embed=None, view=None
        )
        self.stop()
