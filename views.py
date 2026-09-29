"""Discord UI components: Views, Modals, Embeds, and DynamicItems."""

from __future__ import annotations

import io
import json
import logging
import re
import time
from typing import TYPE_CHECKING

import discord

import image_gen
from models import (
    DecodedOrderInfo,
    EmbedColor,
    OrderStatus,
    RateBreakdown,
    TransactionType,
    calculate_user_amount,
    next_rank,
    parse_iso,
    resolve_rank,
    utc_now,
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


# ── レートリミット（メモリ内） ───────────────────────────────


class RateLimiter:
    def __init__(self) -> None:
        self._last: dict[tuple[int, str], float] = {}

    def remaining(self, user_id: int, action: str, cooldown: int) -> float:
        if cooldown <= 0:
            return 0.0
        last = self._last.get((user_id, action))
        if last is None:
            return 0.0
        remain = cooldown - (time.monotonic() - last)
        return remain if remain > 0 else 0.0

    def mark(self, user_id: int, action: str) -> None:
        self._last[(user_id, action)] = time.monotonic()


rate_limiter = RateLimiter()


# ── 処理中カウンタ（Graceful Shutdown 用） ────────────────────


def _inflight_enter(client: discord.Client) -> None:
    try:
        client.inflight += 1  # type: ignore[attr-defined]
    except AttributeError:
        pass


def _inflight_exit(client: discord.Client) -> None:
    try:
        client.inflight = max(0, client.inflight - 1)  # type: ignore[attr-defined]
    except AttributeError:
        pass


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

    camp_rate = settings.get("campaign_rate", "")
    camp_end = parse_iso(settings.get("campaign_end", ""))
    if camp_rate and (camp_end is None or camp_end > utc_now()):
        try:
            camp_discount = 100 - int(camp_rate)
            note = f"🎉 キャンペーン実施中: **{camp_discount}% OFF**"
            if camp_end:
                note += f"\n終了: <t:{int(camp_end.timestamp())}:R>"
            embed.add_field(name="キャンペーン", value=note, inline=False)
        except ValueError:
            pass

    if settings.get("rank_enabled", "1") == "1":
        embed.add_field(
            name="ランク特典",
            value="ご利用回数に応じて割引率がアップします。",
            inline=False,
        )

    embed.set_footer(text="McDonald's Concierge")
    return embed


def format_rate_breakdown(br: RateBreakdown) -> str:
    lines = [f"基本 {100 - br.base}% OFF"]
    if br.vip:
        lines[0] = f"VIP優待 {100 - br.base}% OFF"
    if br.campaign:
        lines.append("🎉 キャンペーン適用")
    if br.rank and br.rank_bonus > 0:
        lines.append(f"{br.rank.emoji} {br.rank.name} +{br.rank_bonus}%")
    if br.coupon_code:
        lines.append(f"🎟 {br.coupon_code} +{br.coupon_bonus}%")
    lines.append(f"**合計 {br.discount}% OFF**")
    return "\n".join(lines)


def build_order_confirm_embed(
    decoded: DecodedOrderInfo,
    user_amount: int,
    subsidy: int,
    balance: int,
    breakdown: RateBreakdown | None = None,
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
    if len(products_text) > 1000:
        products_text = products_text[:990] + "\n  ..."

    after_balance = balance - user_amount

    embed = discord.Embed(
        title="注文内容の確認",
        description="以下の内容で注文します。",
        color=EmbedColor.INFO,
    )
    embed.add_field(name="店舗", value=store_display, inline=True)
    embed.add_field(
        name="受取方法", value=decoded.pickup_method or "不明", inline=True
    )
    embed.add_field(
        name="商品", value=f"```\n{products_text}\n```", inline=False
    )
    embed.add_field(name="定価", value=f"¥{decoded.total_amount:,}", inline=True)
    embed.add_field(name="代行負担", value=f"¥{subsidy:,}", inline=True)
    embed.add_field(name="お支払い", value=f"**¥{user_amount:,}**", inline=True)
    if breakdown:
        embed.add_field(
            name="適用割引", value=format_rate_breakdown(breakdown), inline=False
        )
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
    points_earned: int = 0,
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
        if points_earned > 0:
            embed.add_field(
                name="獲得ポイント", value=f"+{points_earned:,}pt", inline=True
            )
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


async def build_achievement_embed(db: "Database", order: dict) -> discord.Embed:
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
    embed.add_field(name="ご利用額", value=f"¥{order['user_amount']:,}", inline=True)
    embed.add_field(name="通常価格", value=f"¥{order['total_amount']:,}", inline=True)
    if order.get("receipt_number"):
        embed.add_field(
            name="受取番号", value=f"#{order['receipt_number']}", inline=True
        )
    embed.add_field(name="注文ID", value=f"#{order['id']:04d}", inline=True)
    embed.set_footer(text=f"Total Orders: {total_completed}")
    return embed


async def build_balance_embed(
    db: "Database", user: discord.User | discord.Member
) -> discord.Embed:
    balance = await db.get_balance(user.id)
    points = await db.get_points(user.id)
    completed = await db.get_user_completed_count(user.id)
    rank = resolve_rank(completed)
    nxt = next_rank(completed)

    embed = discord.Embed(title="残高情報", color=EmbedColor.PRIMARY)
    embed.add_field(name="ユーザー", value=user.display_name, inline=True)
    embed.add_field(name="現在残高", value=f"**¥{balance:,}**", inline=True)
    embed.add_field(name="ポイント", value=f"{points:,}pt", inline=True)
    if await db.get_setting("rank_enabled") == "1":
        rank_text = f"{rank.emoji} {rank.name}"
        if rank.bonus:
            rank_text += f" (+{rank.bonus}% OFF)"
        embed.add_field(name="ランク", value=rank_text, inline=True)
        embed.add_field(name="完了注文", value=f"{completed:,}回", inline=True)
        if nxt:
            embed.add_field(
                name="次のランク",
                value=f"{nxt.emoji} {nxt.name} まであと {nxt.threshold - completed}回",
                inline=True,
            )
    embed.set_footer(text="McDonald's Concierge")
    return embed


def build_history_embed(
    orders: list[dict], page: int, total_pages: int
) -> discord.Embed:
    embed = discord.Embed(
        title="注文履歴",
        description=f"ページ {page}/{total_pages}" if orders else "注文履歴はありません。",
        color=EmbedColor.DARK,
    )
    for o in orders:
        status = OrderStatus(o["status"])
        store = o.get("store_name") or o.get("store_id") or "不明"
        line = f"{status.emoji} ¥{o['user_amount']:,}  {store}"
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
        name="3. お気に入り",
        value=(
            "注文完了後に保存すると「⭐ お気に入り」から\n"
            "同じ内容をワンタップで再注文できます。"
        ),
        inline=False,
    )
    embed.add_field(
        name="4. 特典",
        value=(
            "ご利用回数に応じてランクが上がり割引率がアップします。\n"
            "`/profile` でランク・ポイントを確認できます。\n"
            "`/coupon` でクーポンを適用できます。"
        ),
        inline=False,
    )
    embed.set_footer(text="McDonald's Concierge")
    return embed


def build_deposit_info_embed(amount: int, instruction: str) -> discord.Embed:
    embed = discord.Embed(
        title="入金申請", description=instruction, color=EmbedColor.INFO
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
        description=f"ページ {page}/{total_pages}" if txs else "取引履歴はありません。",
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


# ── 画像・通知ヘルパー ──────────────────────────────────────


async def make_receipt_bytes(number: str) -> bytes | None:
    """注文番号を差し込んだ完了画像のバイト列を返す（生成不可なら None）。"""
    if not number or not image_gen.is_available():
        return None
    try:
        return await image_gen.render_order_complete(number)
    except Exception as exc:
        logger.error("Failed to render receipt image: %s", exc)
        return None


def _receipt_file(data: bytes) -> discord.File:
    return discord.File(io.BytesIO(data), filename="order.png")


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


async def notify_user(
    bot: discord.Client,
    user_id: int,
    embed: discord.Embed,
    image: bytes | None = None,
) -> bool:
    """ユーザーへDMで通知する（設定で無効化されていれば送らない）。"""
    db: "Database" = bot.db  # type: ignore[attr-defined]
    try:
        if await db.get_setting("dm_notify") != "1":
            return False
        user_row = await db.get_user(user_id)
        if not user_row.get("notify_dm", 1):
            return False
        user = await bot.fetch_user(user_id)
        if image:
            await user.send(embed=embed, file=_receipt_file(image))
        else:
            await user.send(embed=embed)
        return True
    except Exception:
        return False


async def send_achievement(bot: discord.Client, order: dict) -> bool:
    """実績Embedを実績チャンネルへ送信する（投稿済フラグは扱わない）。"""
    db: "Database" = bot.db  # type: ignore[attr-defined]
    channel_id = await db.get_setting("achievement_channel_id")
    if not channel_id:
        return False
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        embed = await build_achievement_embed(db, order)
        img = await make_receipt_bytes(order.get("receipt_number", ""))
        if img:
            embed.set_image(url="attachment://order.png")
            await channel.send(embed=embed, file=_receipt_file(img))  # type: ignore[union-attr]
        else:
            await channel.send(embed=embed)  # type: ignore[union-attr]
        return True
    except Exception as exc:
        logger.error("Failed to send achievement: %s", exc)
        return False


async def post_achievement(bot: discord.Client, order: dict) -> bool:
    """未投稿の実績のみ送信し、投稿済フラグを立てる。"""
    db: "Database" = bot.db  # type: ignore[attr-defined]
    if order.get("achievement_posted"):
        return False
    if await send_achievement(bot, order):
        await db.set_achievement_posted(order["id"])
        return True
    return False


# ── 注文フロー ─────────────────────────────────────────────


async def start_order_flow(
    interaction: discord.Interaction, hex_str: str
) -> None:
    """Hexを解析し、確認画面を表示する。interactionはdefer済みであること。"""
    db = _get_db(interaction)
    mcd = _get_mcd(interaction)
    uid = interaction.user.id

    async def fail(title: str, desc: str) -> None:
        await interaction.followup.send(
            embed=discord.Embed(
                title=title, description=desc, color=EmbedColor.ERROR
            ),
            ephemeral=True,
        )

    ok, err_msg = validate_hex(hex_str)
    if not ok:
        await fail("入力エラー", err_msg)
        return

    settings = await db.get_all_settings()

    # Hex重複チェック
    if settings.get("hex_reuse_check", "1") == "1":
        dup = await db.find_reused_hex(hex_str)
        if dup:
            await fail(
                "重複した注文",
                f"このHexデータは既に注文 #{dup['id']:04d} で使用されています。",
            )
            return

    # 1日あたりの注文上限
    tz_offset = int(settings.get("tz_offset", "9") or 9)
    daily_limit = int(settings.get("daily_order_limit", "0") or 0)
    if daily_limit > 0:
        today = await db.count_orders_today(uid, tz_offset)
        if today >= daily_limit:
            await fail(
                "本日の上限に到達",
                f"1日あたりの注文上限（{daily_limit}件）に達しています。",
            )
            return

    try:
        decoded = await mcd.decode_hex(hex_str)
    except Exception as exc:
        logger.error("Hex decode error: %s", exc)
        await fail("解析エラー", "Hexデータの解析に失敗しました。")
        return

    if decoded.total_amount <= 0:
        await fail("解析エラー", "金額情報を取得できませんでした。")
        return

    await db.resolve_product_names(decoded)

    minimum = int(settings.get("min_order_amount", "400") or 0)
    maximum = int(settings.get("max_order_amount", "0") or 0)

    if decoded.total_amount < minimum:
        await fail(
            "注文不可", f"最低注文額 ¥{minimum:,} 未満のため注文できません。"
        )
        return
    if maximum > 0 and decoded.total_amount > maximum:
        await fail(
            "注文不可", f"注文上限額 ¥{maximum:,} を超えています。"
        )
        return

    breakdown = await db.resolve_rate(uid)
    user_amount = calculate_user_amount(decoded.total_amount, breakdown.final)
    subsidy = decoded.total_amount - user_amount

    balance = await db.get_balance(uid)
    if balance < user_amount:
        shortage = user_amount - balance
        embed = discord.Embed(
            title="残高不足",
            description=(
                f"残高が不足しています。\n\n"
                f"必要額: **¥{user_amount:,}**\n"
                f"現在残高: ¥{balance:,}\n"
                f"不足額: **¥{shortage:,}**\n\n"
                "下のボタンから不足分を入金申請できます。"
            ),
            color=EmbedColor.ERROR,
        )
        await interaction.followup.send(
            embed=embed,
            view=TopUpView(uid, shortage),
            ephemeral=True,
        )
        return

    embed = build_order_confirm_embed(
        decoded, user_amount, subsidy, balance, breakdown
    )
    view = OrderConfirmView(
        user_id=uid,
        decoded=decoded,
        user_amount=user_amount,
        subsidy=subsidy,
        breakdown=breakdown,
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def guard_user(
    interaction: discord.Interaction, action: str
) -> bool:
    """ブラックリスト・メンテ・受付・レート制限をまとめて確認する。

    問題があればユーザーへ返信し False を返す。
    """
    db = _get_db(interaction)
    uid = interaction.user.id
    settings = await db.get_all_settings()

    blocked, reason = await db.is_blacklisted(uid)
    if blocked:
        desc = "ご利用が制限されています。"
        if reason:
            desc += f"\n理由: {reason}"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="利用制限", description=desc, color=EmbedColor.ERROR
            ),
            ephemeral=True,
        )
        return False

    if settings.get("maintenance", "0") == "1":
        await interaction.response.send_message(
            embed=discord.Embed(
                title="メンテナンス中",
                description="現在メンテナンス中です。しばらくお待ちください。",
                color=EmbedColor.WARNING,
            ),
            ephemeral=True,
        )
        return False

    if action == "order" and settings.get("accepting_orders", "1") != "1":
        await interaction.response.send_message(
            embed=discord.Embed(
                title="受付停止中",
                description="現在注文の受付を停止しています。",
                color=EmbedColor.WARNING,
            ),
            ephemeral=True,
        )
        return False

    cooldown_key = "order_cooldown" if action == "order" else "deposit_cooldown"
    cooldown = int(settings.get(cooldown_key, "0") or 0)
    remain = rate_limiter.remaining(uid, action, cooldown)
    if remain > 0:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="操作が早すぎます",
                description=f"あと {int(remain) + 1} 秒お待ちください。",
                color=EmbedColor.WARNING,
            ),
            ephemeral=True,
        )
        return False

    return True


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
        if not await guard_user(interaction, "order"):
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
        if not await guard_user(interaction, "deposit"):
            return
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
        embed = await build_balance_embed(db, interaction.user)
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
        label="お気に入り",
        emoji="⭐",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:favorites",
        row=1,
    )
    async def favorites_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        db = _get_db(interaction)
        favs = await db.get_favorites(interaction.user.id)
        if not favs:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="お気に入り",
                    description=(
                        "お気に入りは登録されていません。\n"
                        "注文完了後に「お気に入りに保存」から登録できます。"
                    ),
                    color=EmbedColor.DARK,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="お気に入り",
                description="再注文する項目を選択してください。",
                color=EmbedColor.PRIMARY,
            ),
            view=FavoritesView(interaction.user.id, favs),
            ephemeral=True,
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
        await interaction.response.send_message(
            embed=build_help_embed(), ephemeral=True
        )


# ── Hex input modal ────────────────────────────────────────


class HexInputModal(discord.ui.Modal, title="注文データ入力"):
    hex_input = discord.ui.TextInput(
        label="Hex Stream",
        style=discord.TextStyle.paragraph,
        placeholder="Hexデータを貼り付けてください",
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rate_limiter.mark(interaction.user.id, "order")
        await start_order_flow(interaction, self.hex_input.value.strip())

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.error("HexInputModal error: %s", error, exc_info=True)
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


# ── Order confirm view ─────────────────────────────────────


class OrderConfirmView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        decoded: DecodedOrderInfo,
        user_amount: int,
        subsidy: int,
        breakdown: RateBreakdown | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.decoded = decoded
        self.user_amount = user_amount
        self.subsidy = subsidy
        self.breakdown = breakdown
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
        client = interaction.client

        products_data = [
            {
                "id": p.product_id,
                "name": p.display_name,
                "addons": [
                    {"id": a.product_id, "name": a.display_name} for a in p.addons
                ],
            }
            for p in decoded.products
        ]

        coupon_code = self.breakdown.coupon_code if self.breakdown else ""
        rate_used = self.breakdown.final if self.breakdown else 0

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
                rate_used=rate_used,
                coupon_code=coupon_code,
            )
        except ValueError as exc:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="注文失敗", description=str(exc), color=EmbedColor.ERROR
                ),
                view=None,
            )
            return

        if coupon_code:
            try:
                await db.consume_coupon(coupon_code, self.user_id, order_id)
            except Exception as exc:
                logger.error("Coupon consume failed: %s", exc)

        await interaction.response.edit_message(
            embed=build_processing_embed(order_id), view=None
        )

        _inflight_enter(client)
        try:
            await self._execute(interaction, db, mcd, order_id, decoded)
        finally:
            _inflight_exit(client)

    async def _execute(
        self,
        interaction: discord.Interaction,
        db: "Database",
        mcd: "MCDAdapter",
        order_id: int,
        decoded: DecodedOrderInfo,
    ) -> None:
        client = interaction.client

        if not mcd.is_enabled:
            await db.update_order_status(order_id, OrderStatus.PROCESSING)
            await db.update_order_status(
                order_id,
                OrderStatus.MANUAL_REVIEW,
                error_info="外部APIが未設定のため管理者による手動処理が必要です",
            )
            embed = build_order_result_embed(order_id, OrderStatus.MANUAL_REVIEW)
            await interaction.edit_original_response(embed=embed)
            await send_admin_log(
                client,
                "注文受付（手動処理）",
                f"注文 #{order_id:04d} を受け付けました。外部API未設定のため手動処理が必要です。",
                ユーザー=interaction.user.display_name,
                金額=f"¥{self.user_amount:,}",
            )
            return

        await db.update_order_status(order_id, OrderStatus.PROCESSING)
        max_attempts = await db.get_int_setting("retry_max_attempts", 3)
        result = await mcd.execute_order(decoded.raw_hex, max_attempts)
        attempts = result.get("attempts", 1)

        if result.get("success"):
            receipt = result.get("receipt_number", "")
            s_name = result.get("store_name", "") or decoded.store_name

            point_rate = await db.get_int_setting("point_rate", 0)
            points = (
                calculate_user_amount(self.user_amount, point_rate)
                if point_rate > 0
                else 0
            )

            await db.update_order_status(
                order_id,
                OrderStatus.COMPLETED,
                receipt_number=receipt,
                order_token=result.get("order_token", ""),
                order_group=result.get("order_group", ""),
                retry_count=attempts - 1,
                points_earned=points,
            )
            if points > 0:
                await db.add_points(self.user_id, points)

            embed = build_order_result_embed(
                order_id, OrderStatus.COMPLETED,
                receipt_number=receipt,
                store_name=s_name,
                user_amount=self.user_amount,
                points_earned=points,
            )
            img = await make_receipt_bytes(receipt)
            view = SaveFavoriteView(self.user_id, decoded.raw_hex)
            if img:
                embed.set_image(url="attachment://order.png")
                await interaction.edit_original_response(
                    embed=embed, attachments=[_receipt_file(img)], view=view
                )
            else:
                await interaction.edit_original_response(embed=embed, view=view)

            order = await db.get_order(order_id)
            if order:
                await post_achievement(client, order)
                await send_admin_log(
                    client,
                    "注文完了",
                    f"注文 #{order_id:04d} が完了しました。",
                    ユーザー=interaction.user.display_name,
                    金額=f"¥{self.user_amount:,}",
                    店舗=s_name or "不明",
                )

        elif result.get("unknown_state"):
            await db.update_order_status(
                order_id, OrderStatus.MANUAL_REVIEW,
                error_info=result.get("error", "タイムアウト"),
                retry_count=attempts - 1,
            )
            embed = build_order_result_embed(order_id, OrderStatus.MANUAL_REVIEW)
            await interaction.edit_original_response(embed=embed)
            await notify_user(client, self.user_id, embed)
            await send_admin_log(
                client,
                "要確認",
                f"注文 #{order_id:04d} は確認が必要です。",
                ユーザー=interaction.user.display_name,
                理由=result.get("error", "不明")[:200],
            )

        else:
            err = result.get("error", "不明なエラー")
            await db.update_order_status(
                order_id, OrderStatus.FAILED,
                error_info=err,
                retry_count=attempts - 1,
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
            await notify_user(client, self.user_id, embed)
            await send_admin_log(
                client,
                "注文失敗",
                f"注文 #{order_id:04d} が失敗しました。残高を返金しました。",
                ユーザー=interaction.user.display_name,
                エラー=err[:200],
                試行回数=str(attempts),
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


# ── お気に入り ──────────────────────────────────────────────


class SaveFavoriteView(discord.ui.View):
    def __init__(self, user_id: int, hex_data: str) -> None:
        super().__init__(timeout=600)
        self.user_id = user_id
        self.hex_data = hex_data

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="お気に入りに保存", emoji="⭐", style=discord.ButtonStyle.secondary
    )
    async def save(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            SaveFavoriteModal(self.hex_data)
        )
        self.stop()


class SaveFavoriteModal(discord.ui.Modal, title="お気に入りに保存"):
    name_input = discord.ui.TextInput(
        label="名前",
        placeholder="例: いつものセット",
        required=True,
        max_length=60,
    )

    def __init__(self, hex_data: str) -> None:
        super().__init__()
        self.hex_data = hex_data

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = _get_db(interaction)
        favs = await db.get_favorites(interaction.user.id, limit=100)
        if len(favs) >= 25:
            await interaction.response.send_message(
                "お気に入りの上限（25件）に達しています。", ephemeral=True
            )
            return
        await db.add_favorite(
            interaction.user.id, self.name_input.value.strip(), self.hex_data
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="保存しました",
                description=(
                    f"「{self.name_input.value.strip()}」をお気に入りに保存しました。\n"
                    "パネルの「⭐ お気に入り」から再注文できます。"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )


class FavoriteSelect(discord.ui.Select):
    def __init__(self, favorites: list[dict]) -> None:
        options = [
            discord.SelectOption(
                label=f["name"][:100],
                value=str(f["id"]),
                description=f"登録日: {f['created_at'][:10]}",
            )
            for f in favorites[:25]
        ]
        super().__init__(
            placeholder="再注文するお気に入りを選択",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = _get_db(interaction)
        fav = await db.get_favorite(int(self.values[0]), interaction.user.id)
        if not fav:
            await interaction.response.send_message(
                "お気に入りが見つかりません。", ephemeral=True
            )
            return

        settings = await db.get_all_settings()
        if settings.get("maintenance", "0") == "1":
            await interaction.response.send_message(
                "現在メンテナンス中です。", ephemeral=True
            )
            return
        if settings.get("accepting_orders", "1") != "1":
            await interaction.response.send_message(
                "現在注文の受付を停止しています。", ephemeral=True
            )
            return
        blocked, reason = await db.is_blacklisted(interaction.user.id)
        if blocked:
            await interaction.response.send_message(
                f"ご利用が制限されています。{(' 理由: ' + reason) if reason else ''}",
                ephemeral=True,
            )
            return
        cooldown = int(settings.get("order_cooldown", "0") or 0)
        remain = rate_limiter.remaining(interaction.user.id, "order", cooldown)
        if remain > 0:
            await interaction.response.send_message(
                f"操作が早すぎます。あと {int(remain) + 1} 秒お待ちください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        rate_limiter.mark(interaction.user.id, "order")
        await start_order_flow(interaction, fav["hex_data"])


class FavoritesView(discord.ui.View):
    def __init__(self, user_id: int, favorites: list[dict]) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.favorites = favorites
        self.add_item(FavoriteSelect(favorites))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="削除", emoji="🗑", style=discord.ButtonStyle.danger, row=1
    )
    async def delete(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(DeleteFavoriteModal())

    @discord.ui.button(
        label="閉じる", style=discord.ButtonStyle.secondary, row=1
    )
    async def close(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="閉じました。", embed=None, view=None
        )
        self.stop()


class DeleteFavoriteModal(discord.ui.Modal, title="お気に入り削除"):
    name_input = discord.ui.TextInput(
        label="削除するお気に入りの名前",
        placeholder="完全一致で入力してください",
        required=True,
        max_length=60,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = _get_db(interaction)
        target = self.name_input.value.strip()
        favs = await db.get_favorites(interaction.user.id, limit=100)
        match = next((f for f in favs if f["name"] == target), None)
        if not match:
            await interaction.response.send_message(
                f"「{target}」が見つかりません。", ephemeral=True
            )
            return
        await db.delete_favorite(match["id"], interaction.user.id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="削除しました",
                description=f"「{target}」をお気に入りから削除しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )


# ── 入金 ────────────────────────────────────────────────────


class TopUpView(discord.ui.View):
    """残高不足時の入金案内。"""

    def __init__(self, user_id: int, shortage: int) -> None:
        super().__init__(timeout=300)
        self.user_id = user_id
        self.shortage = shortage

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作は実行できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="不足分を入金", emoji="💳", style=discord.ButtonStyle.primary
    )
    async def topup(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            DepositAmountModal(default_amount=self.shortage)
        )
        self.stop()


class DepositAmountModal(discord.ui.Modal, title="入金申請"):
    amount_input = discord.ui.TextInput(
        label="入金額（円）",
        placeholder="例: 1000",
        required=True,
        max_length=10,
    )

    def __init__(self, default_amount: int | None = None) -> None:
        super().__init__()
        if default_amount and default_amount > 0:
            self.amount_input.default = str(default_amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount_input.value.strip().replace(",", "").replace("¥", "")
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

        db = _get_db(interaction)
        settings = await db.get_all_settings()
        min_dep = int(settings.get("min_deposit_amount", "0") or 0)
        max_dep = int(settings.get("max_deposit_amount", "0") or 0)

        if amount <= 0:
            msg = "金額は1円以上で入力してください。"
        elif min_dep > 0 and amount < min_dep:
            msg = f"最低入金額は ¥{min_dep:,} です。"
        elif max_dep > 0 and amount > max_dep:
            msg = f"1回あたりの入金上限は ¥{max_dep:,} です。"
        else:
            msg = ""

        if msg:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="入力エラー", description=msg, color=EmbedColor.ERROR
                ),
                ephemeral=True,
            )
            return

        instruction = settings.get("deposit_instruction", "")
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
        rate_limiter.mark(self.user_id, "deposit")

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
            await interaction.response.send_message(str(exc), ephemeral=True)
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

        await notify_user(
            interaction.client,
            user_id,
            discord.Embed(
                title="入金完了",
                description=(
                    f"入金 #{self.deposit_id} が承認されました。\n"
                    f"入金額: ¥{amount:,}\n"
                    f"現在残高: ¥{new_balance:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
        )

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

        desc = f"入金申請 #{self.deposit_id} (¥{amount:,}) は却下されました。"
        if reason:
            desc += f"\n理由: {reason}"
        await notify_user(
            interaction.client,
            user_id,
            discord.Embed(
                title="入金却下", description=desc, color=EmbedColor.ERROR
            ),
        )

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

    @discord.ui.button(
        label="ポイント交換", emoji="🎁", style=discord.ButtonStyle.secondary
    )
    async def redeem(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        db = _get_db(interaction)
        points = await db.get_points(self.user_id)
        if points <= 0:
            await interaction.response.send_message(
                "交換可能なポイントがありません。", ephemeral=True
            )
            return
        await interaction.response.send_modal(PointRedeemModal(points))


class PointRedeemModal(discord.ui.Modal, title="ポイント交換"):
    amount_input = discord.ui.TextInput(
        label="交換するポイント数（1pt = ¥1）",
        placeholder="例: 500",
        required=True,
        max_length=10,
    )

    def __init__(self, available: int) -> None:
        super().__init__()
        self.amount_input.default = str(available)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = _get_db(interaction)
        try:
            amount = int(self.amount_input.value.strip().replace(",", ""))
        except ValueError:
            await interaction.response.send_message(
                "数値で入力してください。", ephemeral=True
            )
            return
        try:
            remaining, new_balance = await db.redeem_points(
                interaction.user.id, amount
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="ポイント交換完了",
                description=(
                    f"{amount:,}pt を残高に交換しました。\n"
                    f"残ポイント: {remaining:,}pt\n"
                    f"現在残高: ¥{new_balance:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )


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
