"""Admin commands for McDonald's Concierge Bot."""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from models import (
    EmbedColor,
    OrderStatus,
    TransactionType,
)
from views import (
    PanelView,
    _receipt_file,
    build_order_result_embed,
    build_panel_embed,
    make_receipt_bytes,
    post_achievement,
    send_admin_log,
)

if TYPE_CHECKING:
    from db import Database

logger = logging.getLogger("bot.admin")


def _is_owner_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)
    return app_commands.check(predicate)


class OwnerGroup(app_commands.Group):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await interaction.client.is_owner(interaction.user):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="権限エラー",
                    description="この操作は管理者のみ実行できます。",
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )
            return False
        return True


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self) -> "Database":
        return self.bot.db  # type: ignore[attr-defined]

    # ── /setup_panel ─────────────────────────────────────────

    @app_commands.command(name="setup_panel", description="パネルを設置します")
    @_is_owner_check()
    async def setup_panel(self, interaction: discord.Interaction) -> None:
        embed = await build_panel_embed(self.db)
        view = PanelView()
        msg = await interaction.channel.send(embed=embed, view=view)  # type: ignore[union-attr]
        await self.db.save_panel(
            interaction.guild_id, interaction.channel_id, msg.id  # type: ignore[arg-type]
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="パネル設置完了",
                description="パネルを設置しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    # ── /restart ─────────────────────────────────────────────

    @app_commands.command(name="restart", description="BOTを再起動します")
    @_is_owner_check()
    async def restart(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="再起動中...",
                description="BOTを再起動しています。",
                color=EmbedColor.WARNING,
            )
        )
        logger.info("Restart requested by %s", interaction.user)
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── /admin group ─────────────────────────────────────────

    admin_group = OwnerGroup(name="admin", description="管理者コマンド")

    # ── balance subgroup ─────────────────────────────────────

    balance_group = OwnerGroup(
        name="balance", description="残高管理", parent=admin_group
    )

    @balance_group.command(name="add", description="残高を加算します")
    @app_commands.describe(user="対象ユーザー", amount="加算額", reason="理由")
    async def balance_add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: int,
        reason: str = "",
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                "金額は1以上で指定してください。", ephemeral=True
            )
            return
        new = await self.db.add_balance(
            user.id, amount, TransactionType.ADMIN_ADD,
            reason=reason or "管理者加算",
            operator_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高加算",
                description=(
                    f"**{user.display_name}** に ¥{amount:,} を加算しました。\n"
                    f"新残高: ¥{new:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await send_admin_log(
            self.bot, "残高加算",
            f"管理者 {interaction.user.display_name} が残高を加算しました。",
            対象=user.display_name, 金額=f"+¥{amount:,}",
            新残高=f"¥{new:,}", 理由=reason or "(なし)",
        )

    @balance_group.command(name="remove", description="残高を減算します")
    @app_commands.describe(user="対象ユーザー", amount="減算額", reason="理由")
    async def balance_remove(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: int,
        reason: str = "",
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                "金額は1以上で指定してください。", ephemeral=True
            )
            return
        try:
            new = await self.db.deduct_balance(
                user.id, amount, TransactionType.ADMIN_REMOVE,
                reason=reason or "管理者減算",
                operator_id=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高減算",
                description=(
                    f"**{user.display_name}** から ¥{amount:,} を減算しました。\n"
                    f"新残高: ¥{new:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await send_admin_log(
            self.bot, "残高減算",
            f"管理者 {interaction.user.display_name} が残高を減算しました。",
            対象=user.display_name, 金額=f"-¥{amount:,}",
            新残高=f"¥{new:,}", 理由=reason or "(なし)",
        )

    @balance_group.command(name="set", description="残高を設定します")
    @app_commands.describe(user="対象ユーザー", amount="設定額", reason="理由")
    async def balance_set(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: int,
        reason: str = "",
    ) -> None:
        if amount < 0:
            await interaction.response.send_message(
                "金額は0以上で指定してください。", ephemeral=True
            )
            return
        new = await self.db.set_balance(
            user.id, amount,
            reason=reason or "管理者設定",
            operator_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高設定",
                description=(
                    f"**{user.display_name}** の残高を ¥{new:,} に設定しました。"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await send_admin_log(
            self.bot, "残高設定",
            f"管理者 {interaction.user.display_name} が残高を設定しました。",
            対象=user.display_name, 新残高=f"¥{new:,}",
            理由=reason or "(なし)",
        )

    @balance_group.command(name="view", description="ユーザーの残高を確認します")
    @app_commands.describe(user="対象ユーザー")
    async def balance_view(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        balance = await self.db.get_balance(user.id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="残高確認",
                description=(
                    f"**{user.display_name}**\n"
                    f"残高: **¥{balance:,}**\n"
                    f"ID: {user.id}"
                ),
                color=EmbedColor.INFO,
            ),
            ephemeral=True,
        )

    @balance_group.command(name="history", description="取引履歴を確認します")
    @app_commands.describe(user="対象ユーザー", limit="表示件数")
    async def balance_history(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        limit: int = 10,
    ) -> None:
        txs = await self.db.get_transactions(user.id, limit=min(limit, 25))
        if not txs:
            await interaction.response.send_message(
                f"{user.display_name} の取引履歴はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"{user.display_name} の取引履歴",
            color=EmbedColor.DARK,
        )
        for tx in txs:
            try:
                tx_type = TransactionType(tx["type"]).display
            except ValueError:
                tx_type = tx["type"]
            sign = "+" if tx["amount"] >= 0 else ""
            embed.add_field(
                name=tx["created_at"][:16],
                value=(
                    f"{tx_type}  {sign}¥{tx['amount']:,}\n"
                    f"残高: ¥{tx['balance_after']:,}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @balance_group.command(name="top", description="残高ランキングを表示します")
    @app_commands.describe(limit="表示件数")
    async def balance_top(
        self, interaction: discord.Interaction, limit: int = 10
    ) -> None:
        rows = await self.db.get_all_balances(limit=min(limit, 25))
        if not rows:
            await interaction.response.send_message(
                "データがありません。", ephemeral=True
            )
            return
        lines: list[str] = []
        for i, (uid, bal) in enumerate(rows, 1):
            try:
                u = await self.bot.fetch_user(uid)
                name = u.display_name
            except Exception:
                name = str(uid)
            lines.append(f"**{i}.** {name} — ¥{bal:,}")
        embed = discord.Embed(
            title="残高ランキング",
            description="\n".join(lines),
            color=EmbedColor.PRIMARY,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── order subgroup ───────────────────────────────────────

    order_group = OwnerGroup(
        name="order", description="注文管理", parent=admin_group
    )

    @order_group.command(name="view", description="注文詳細を確認します")
    @app_commands.describe(order_id="注文ID")
    async def order_view(
        self, interaction: discord.Interaction, order_id: int
    ) -> None:
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.response.send_message(
                "注文が見つかりません。", ephemeral=True
            )
            return
        status = OrderStatus(order["status"])
        embed = discord.Embed(
            title=f"注文 #{order_id:04d}",
            color=EmbedColor.INFO,
        )
        embed.add_field(name="ステータス", value=f"{status.emoji} {status.display}", inline=True)
        embed.add_field(name="ユーザーID", value=str(order["user_id"]), inline=True)
        embed.add_field(name="店舗", value=order.get("store_name") or order.get("store_id") or "不明", inline=True)
        embed.add_field(name="定価", value=f"¥{order['total_amount']:,}", inline=True)
        embed.add_field(name="ユーザー支払", value=f"¥{order['user_amount']:,}", inline=True)
        embed.add_field(name="負担額", value=f"¥{order['subsidy_amount']:,}", inline=True)
        if order.get("receipt_number"):
            embed.add_field(name="受取番号", value=order["receipt_number"], inline=True)
        embed.add_field(name="受取方法", value=order.get("pickup_method") or "不明", inline=True)
        embed.add_field(name="作成日時", value=order["created_at"][:16], inline=True)
        if order.get("error_info"):
            embed.add_field(name="エラー情報", value=order["error_info"][:200], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @order_group.command(name="search", description="注文を検索します")
    @app_commands.describe(user="ユーザー", status="ステータス", limit="表示件数")
    @app_commands.choices(status=[
        app_commands.Choice(name="処理待ち", value="pending"),
        app_commands.Choice(name="処理中", value="processing"),
        app_commands.Choice(name="完了", value="completed"),
        app_commands.Choice(name="失敗", value="failed"),
        app_commands.Choice(name="要確認", value="manual_review"),
        app_commands.Choice(name="返金済", value="refunded"),
        app_commands.Choice(name="キャンセル", value="cancelled"),
    ])
    async def order_search(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        status: str | None = None,
        limit: int = 10,
    ) -> None:
        orders = await self.db.search_orders(
            user_id=user.id if user else None,
            status=status,
            limit=min(limit, 25),
        )
        if not orders:
            await interaction.response.send_message(
                "条件に合う注文はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="注文検索結果",
            description=f"{len(orders)}件",
            color=EmbedColor.DARK,
        )
        for o in orders[:15]:
            s = OrderStatus(o["status"])
            embed.add_field(
                name=f"#{o['id']:04d}  {o['created_at'][:10]}",
                value=(
                    f"{s.emoji} {s.display} | UID:{o['user_id']} | "
                    f"¥{o['user_amount']:,}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @order_group.command(name="complete", description="注文を手動で完了にします")
    @app_commands.describe(order_id="注文ID", receipt_number="受取番号")
    async def order_complete(
        self,
        interaction: discord.Interaction,
        order_id: int,
        receipt_number: str = "",
    ) -> None:
        try:
            await self.db.update_order_status(
                order_id, OrderStatus.COMPLETED,
                receipt_number=receipt_number,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        order = await self.db.get_order(order_id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="注文完了",
                description=f"注文 #{order_id:04d} を完了にしました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        if order:
            await post_achievement(self.bot, order)
            try:
                user = await self.bot.fetch_user(order["user_id"])
                embed = build_order_result_embed(
                    order_id, OrderStatus.COMPLETED,
                    receipt_number=receipt_number,
                    store_name=order.get("store_name", ""),
                    user_amount=order["user_amount"],
                )
                img = await make_receipt_bytes(receipt_number)
                if img:
                    embed.set_image(url="attachment://order.png")
                    await user.send(embed=embed, file=_receipt_file(img))
                else:
                    await user.send(embed=embed)
            except Exception:
                pass
        await send_admin_log(
            self.bot, "注文手動完了",
            f"管理者 {interaction.user.display_name} が注文を完了にしました。",
            注文ID=f"#{order_id:04d}",
            受取番号=receipt_number or "(なし)",
        )

    @order_group.command(name="refund", description="注文を返金します")
    @app_commands.describe(order_id="注文ID")
    async def order_refund(
        self, interaction: discord.Interaction, order_id: int
    ) -> None:
        try:
            amount = await self.db.refund_order(order_id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        order = await self.db.get_order(order_id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="返金完了",
                description=(
                    f"注文 #{order_id:04d} を返金しました。\n"
                    f"返金額: ¥{amount:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        if order:
            try:
                user = await self.bot.fetch_user(order["user_id"])
                await user.send(
                    embed=discord.Embed(
                        title="返金のお知らせ",
                        description=(
                            f"注文 #{order_id:04d} が返金されました。\n"
                            f"返金額: ¥{amount:,}"
                        ),
                        color=EmbedColor.INFO,
                    )
                )
            except Exception:
                pass
        await send_admin_log(
            self.bot, "注文返金",
            f"管理者 {interaction.user.display_name} が注文を返金しました。",
            注文ID=f"#{order_id:04d}",
            返金額=f"¥{amount:,}",
        )

    @order_group.command(name="review", description="要確認注文を一覧表示します")
    async def order_review(self, interaction: discord.Interaction) -> None:
        orders = await self.db.search_orders(status="manual_review", limit=20)
        if not orders:
            await interaction.response.send_message(
                "要確認の注文はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="要確認注文一覧",
            description=f"{len(orders)}件",
            color=EmbedColor.WARNING,
        )
        for o in orders:
            embed.add_field(
                name=f"#{o['id']:04d}  {o['created_at'][:10]}",
                value=(
                    f"UID:{o['user_id']} | ¥{o['user_amount']:,}\n"
                    f"{o.get('error_info', '')[:80]}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @order_group.command(name="retry", description="失敗した注文を再処理します")
    @app_commands.describe(order_id="注文ID")
    async def order_retry(
        self, interaction: discord.Interaction, order_id: int
    ) -> None:
        order = await self.db.get_order(order_id)
        if not order:
            await interaction.response.send_message(
                "注文が見つかりません。", ephemeral=True
            )
            return
        if order["status"] != OrderStatus.FAILED.value:
            await interaction.response.send_message(
                "失敗した注文のみ再処理できます。", ephemeral=True
            )
            return
        mcd = self.bot.mcd  # type: ignore[attr-defined]
        if not mcd.is_enabled:
            await interaction.response.send_message(
                "外部APIが設定されていません。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.db.update_order_status(order_id, OrderStatus.PROCESSING)
        result = await mcd.execute_order(order["hex_data"])
        if result.get("success"):
            receipt = result.get("receipt_number", "")
            await self.db.update_order_status(
                order_id, OrderStatus.COMPLETED,
                receipt_number=receipt,
                order_token=result.get("order_token", ""),
                order_group=result.get("order_group", ""),
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="再処理完了",
                    description=f"注文 #{order_id:04d} が完了しました。",
                    color=EmbedColor.SUCCESS,
                ),
                ephemeral=True,
            )
            refreshed = await self.db.get_order(order_id)
            if refreshed:
                await post_achievement(self.bot, refreshed)
        elif result.get("unknown_state"):
            await self.db.update_order_status(
                order_id, OrderStatus.MANUAL_REVIEW,
                error_info=result.get("error", ""),
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="要確認",
                    description=f"注文 #{order_id:04d} は確認が必要です。",
                    color=EmbedColor.WARNING,
                ),
                ephemeral=True,
            )
        else:
            await self.db.update_order_status(
                order_id, OrderStatus.FAILED,
                error_info=result.get("error", ""),
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="再処理失敗",
                    description=(
                        f"注文 #{order_id:04d} の再処理に失敗しました。\n"
                        f"{result.get('error', '')[:200]}"
                    ),
                    color=EmbedColor.ERROR,
                ),
                ephemeral=True,
            )

    # ── deposit subgroup ─────────────────────────────────────

    deposit_group = OwnerGroup(
        name="deposits", description="入金管理", parent=admin_group
    )

    @deposit_group.command(name="list", description="未処理の入金申請を表示します")
    async def deposit_list(self, interaction: discord.Interaction) -> None:
        deps = await self.db.get_pending_deposits()
        if not deps:
            await interaction.response.send_message(
                "未処理の入金申請はありません。", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="未処理入金申請",
            description=f"{len(deps)}件",
            color=EmbedColor.WARNING,
        )
        for d in deps:
            embed.add_field(
                name=f"#{d['id']}  {d['created_at'][:10]}",
                value=f"UID:{d['user_id']} | ¥{d['amount']:,}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @deposit_group.command(name="approve", description="入金を承認します")
    @app_commands.describe(deposit_id="入金ID")
    async def deposit_approve(
        self, interaction: discord.Interaction, deposit_id: int
    ) -> None:
        try:
            user_id, amount, new_balance = await self.db.approve_deposit(
                deposit_id, interaction.user.id
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="入金承認",
                description=(
                    f"入金 #{deposit_id} を承認しました。\n"
                    f"UID: {user_id} | ¥{amount:,} | 新残高: ¥{new_balance:,}"
                ),
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(
                embed=discord.Embed(
                    title="入金完了",
                    description=(
                        f"入金 #{deposit_id} が承認されました。\n"
                        f"入金額: ¥{amount:,}\n"
                        f"現在残高: ¥{new_balance:,}"
                    ),
                    color=EmbedColor.SUCCESS,
                )
            )
        except Exception:
            pass

    @deposit_group.command(name="reject", description="入金を却下します")
    @app_commands.describe(deposit_id="入金ID", reason="理由")
    async def deposit_reject(
        self,
        interaction: discord.Interaction,
        deposit_id: int,
        reason: str = "",
    ) -> None:
        try:
            user_id, amount = await self.db.reject_deposit(
                deposit_id, interaction.user.id, reason
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="入金却下",
                description=f"入金 #{deposit_id} を却下しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        try:
            user = await self.bot.fetch_user(user_id)
            desc = f"入金申請 #{deposit_id} (¥{amount:,}) は却下されました。"
            if reason:
                desc += f"\n理由: {reason}"
            await user.send(
                embed=discord.Embed(
                    title="入金却下", description=desc, color=EmbedColor.ERROR
                )
            )
        except Exception:
            pass

    # ── channel subgroup ─────────────────────────────────────

    channel_group = OwnerGroup(
        name="channel", description="チャンネル設定", parent=admin_group
    )

    @channel_group.command(
        name="achievement", description="実績投稿チャンネルを設定します"
    )
    @app_commands.describe(channel="チャンネル")
    async def channel_achievement(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await self.db.set_setting("achievement_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"実績チャンネルを {channel.mention} に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @channel_group.command(
        name="admin_log", description="管理者ログチャンネルを設定します"
    )
    @app_commands.describe(channel="チャンネル")
    async def channel_admin_log(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await self.db.set_setting("admin_log_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"管理者ログチャンネルを {channel.mention} に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    # ── settings commands ────────────────────────────────────

    @admin_group.command(name="rate", description="ユーザー負担率を設定します")
    @app_commands.describe(rate="負担率（1-100）")
    async def admin_rate(
        self, interaction: discord.Interaction, rate: int
    ) -> None:
        if rate < 1 or rate > 100:
            await interaction.response.send_message(
                "負担率は1〜100の範囲で指定してください。", ephemeral=True
            )
            return
        await self.db.set_setting("user_rate", str(rate))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"ユーザー負担率を **{rate}%** に設定しました。（割引 {100 - rate}%）",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @admin_group.command(name="minimum", description="最低注文額を設定します")
    @app_commands.describe(amount="最低注文額")
    async def admin_minimum(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        if amount < 0:
            await interaction.response.send_message(
                "金額は0以上で指定してください。", ephemeral=True
            )
            return
        await self.db.set_setting("min_order_amount", str(amount))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="設定完了",
                description=f"最低注文額を **¥{amount:,}** に設定しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @admin_group.command(name="maintenance", description="メンテナンスモードを切り替えます")
    @app_commands.describe(enabled="有効にする")
    async def admin_maintenance(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.db.set_setting("maintenance", "1" if enabled else "0")
        state = "有効" if enabled else "無効"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="メンテナンスモード",
                description=f"メンテナンスモードを **{state}** にしました。",
                color=EmbedColor.WARNING if enabled else EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    @admin_group.command(name="accepting", description="注文受付を切り替えます")
    @app_commands.describe(enabled="受付する")
    async def admin_accepting(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await self.db.set_setting("accepting_orders", "1" if enabled else "0")
        state = "開始" if enabled else "停止"
        await interaction.response.send_message(
            embed=discord.Embed(
                title="注文受付",
                description=f"注文受付を **{state}** しました。",
                color=EmbedColor.SUCCESS if enabled else EmbedColor.WARNING,
            ),
            ephemeral=True,
        )
        await self._refresh_panels()

    # ── stats ────────────────────────────────────────────────

    @admin_group.command(name="stats", description="統計情報を表示します")
    async def admin_stats(self, interaction: discord.Interaction) -> None:
        s = await self.db.get_stats()
        embed = discord.Embed(title="統計情報", color=EmbedColor.PRIMARY)
        embed.add_field(name="ユーザー数", value=f"{s['user_count']:,}", inline=True)
        embed.add_field(name="総注文数", value=f"{s['total_orders']:,}", inline=True)
        embed.add_field(name="完了注文", value=f"{s['completed_orders']:,}", inline=True)
        embed.add_field(name="総売上", value=f"¥{s['total_revenue']:,}", inline=True)
        embed.add_field(name="総負担額", value=f"¥{s['total_subsidy']:,}", inline=True)
        embed.add_field(name="総残高", value=f"¥{s['total_balance']:,}", inline=True)
        embed.add_field(name="未処理入金", value=f"{s['pending_deposits']:,}", inline=True)
        embed.add_field(name="要確認注文", value=f"{s['review_orders']:,}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── user info ────────────────────────────────────────────

    @admin_group.command(name="user", description="ユーザー情報を表示します")
    @app_commands.describe(user="対象ユーザー")
    async def admin_user(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        balance = await self.db.get_balance(user.id)
        order_count = await self.db.get_user_order_count(user.id)
        tx_count = await self.db.get_transaction_count(user.id)
        embed = discord.Embed(
            title=f"ユーザー情報: {user.display_name}",
            color=EmbedColor.INFO,
        )
        embed.add_field(name="ID", value=str(user.id), inline=True)
        embed.add_field(name="残高", value=f"¥{balance:,}", inline=True)
        embed.add_field(name="注文数", value=f"{order_count:,}", inline=True)
        embed.add_field(name="取引数", value=f"{tx_count:,}", inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── export ───────────────────────────────────────────────

    @admin_group.command(name="export", description="データをエクスポートします")
    @app_commands.describe(target="対象")
    @app_commands.choices(target=[
        app_commands.Choice(name="注文", value="orders"),
        app_commands.Choice(name="取引", value="transactions"),
    ])
    async def admin_export(
        self, interaction: discord.Interaction, target: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if target == "orders":
            data = await self.db.export_orders()
        else:
            data = await self.db.export_transactions()
        if not data:
            await interaction.followup.send("データがありません。", ephemeral=True)
            return
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
        writer.writeheader()
        writer.writerows(data)
        buf.seek(0)
        file = discord.File(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")),
            filename=f"{target}.csv",
        )
        await interaction.followup.send(file=file, ephemeral=True)

    # ── panel management ─────────────────────────────────────

    panel_group = OwnerGroup(
        name="panel", description="パネル管理", parent=admin_group
    )

    @panel_group.command(name="refresh", description="パネルを更新します")
    async def panel_refresh(self, interaction: discord.Interaction) -> None:
        await self._refresh_panels()
        await interaction.response.send_message(
            embed=discord.Embed(
                title="パネル更新",
                description="パネルを更新しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    @panel_group.command(name="delete", description="パネルを削除します")
    async def panel_delete(self, interaction: discord.Interaction) -> None:
        panel = await self.db.get_panel(interaction.guild_id)  # type: ignore[arg-type]
        if not panel:
            await interaction.response.send_message(
                "このサーバーにパネルはありません。", ephemeral=True
            )
            return
        try:
            ch = self.bot.get_channel(panel["channel_id"])
            if ch is None:
                ch = await self.bot.fetch_channel(panel["channel_id"])
            msg = await ch.fetch_message(panel["message_id"])  # type: ignore[union-attr]
            await msg.delete()
        except Exception:
            pass
        await self.db.delete_panel(interaction.guild_id)  # type: ignore[arg-type]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="パネル削除",
                description="パネルを削除しました。",
                color=EmbedColor.SUCCESS,
            ),
            ephemeral=True,
        )

    # ── helpers ──────────────────────────────────────────────

    async def _refresh_panels(self) -> None:
        panels = await self.db.get_all_panels()
        embed = await build_panel_embed(self.db)
        for p in panels:
            try:
                ch = self.bot.get_channel(p["channel_id"])
                if ch is None:
                    ch = await self.bot.fetch_channel(p["channel_id"])
                msg = await ch.fetch_message(p["message_id"])  # type: ignore[union-attr]
                await msg.edit(embed=embed, view=PanelView())
            except Exception as exc:
                logger.warning("Failed to refresh panel %s: %s", p["id"], exc)


async def setup(bot: commands.Bot) -> None:
    cog = AdminCog(bot)
    cog.__cog_app_commands__.append(cog.admin_group)
    await bot.add_cog(cog)
