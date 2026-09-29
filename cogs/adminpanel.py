"""管理者パネル。

スラッシュコマンドと同じ操作をボタンからできるようにする。
どのボタンも押した時点でオーナー判定を行うので、他の人に見えていても操作はできない。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from mcd.rates import active_campaigns
from mcd.settings import GROUPS

from ._shared import BAD, INFO, MONEY, OK, WARN, deny, embed, reply, yen

log = logging.getLogger("bot.adminpanel")


async def owner_only(interaction: discord.Interaction) -> bool:
    if await interaction.client.is_owner(interaction.user):
        return True
    await interaction.response.send_message(
        embed=embed("権限がありません", "このパネルはオーナー専用です。", BAD), ephemeral=True
    )
    return False


# ==================================================================== モーダル


class AdjustModal(discord.ui.Modal, title="残高の手動調整"):
    user_id = discord.ui.TextInput(label="利用者ID", placeholder="123456789012345678", max_length=25)
    amount = discord.ui.TextInput(label="増減額（マイナス可）", placeholder="-500", max_length=12)
    memo = discord.ui.TextInput(
        label="台帳に残すメモ", required=False, max_length=200,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, cog: "AdminPanel"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_adjust(
            interaction, str(self.user_id.value), str(self.amount.value), str(self.memo.value or "")
        )


class UserIdModal(discord.ui.Modal):
    """利用者IDだけを受け取る汎用モーダル。"""

    user_id = discord.ui.TextInput(label="利用者ID", placeholder="123456789012345678", max_length=25)

    def __init__(self, cog: "AdminPanel", action: str, title: str):
        super().__init__(title=title)
        self.cog = cog
        self.action = action

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_user_action(interaction, self.action, str(self.user_id.value))


# ================================================================== パネル本体


class AdminPanelView(discord.ui.View):
    """常設の管理者パネル。再起動後も押せるよう timeout=None + 固定 custom_id。"""

    def __init__(self, cog: "AdminPanel"):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await owner_only(interaction)

    # ---- 1段目: 状況の確認 ----

    @discord.ui.button(
        label="ダッシュボード", style=discord.ButtonStyle.primary,
        custom_id="admin:dashboard", row=0,
    )
    async def dashboard(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_dashboard(interaction)

    @discord.ui.button(
        label="承認待ち", style=discord.ButtonStyle.primary, custom_id="admin:pending", row=0
    )
    async def pending(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_pending(interaction)

    @discord.ui.button(
        label="不正フラグ", style=discord.ButtonStyle.primary, custom_id="admin:flags", row=0
    )
    async def flags(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_flags(interaction)

    # ---- 2段目: 状態の点検 ----

    @discord.ui.button(
        label="アカウント状態", style=discord.ButtonStyle.secondary,
        custom_id="admin:accounts", row=1,
    )
    async def accounts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_accounts(interaction)

    @discord.ui.button(
        label="設定一覧", style=discord.ButtonStyle.secondary, custom_id="admin:settings", row=1
    )
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_settings(interaction)

    @discord.ui.button(
        label="台帳監査", style=discord.ButtonStyle.secondary, custom_id="admin:audit", row=1
    )
    async def audit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_audit(interaction)

    @discord.ui.button(
        label="監査ログ", style=discord.ButtonStyle.secondary, custom_id="admin:logs", row=1
    )
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_logs(interaction)

    # ---- 3段目: 利用者への操作 ----

    @discord.ui.button(
        label="残高を調整", style=discord.ButtonStyle.success, custom_id="admin:adjust", row=2
    )
    async def adjust(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AdjustModal(self.cog))

    @discord.ui.button(
        label="ロック解除", style=discord.ButtonStyle.success, custom_id="admin:unlock", row=2
    )
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            UserIdModal(self.cog, "unlock", "注文ロックの解除")
        )

    @discord.ui.button(
        label="利用停止", style=discord.ButtonStyle.danger, custom_id="admin:ban", row=2
    )
    async def ban(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(UserIdModal(self.cog, "ban", "利用を停止する"))

    @discord.ui.button(
        label="停止を解除", style=discord.ButtonStyle.secondary, custom_id="admin:unban", row=2
    )
    async def unban(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(UserIdModal(self.cog, "unban", "停止を解除する"))

    @discord.ui.button(
        label="利用者の状況", style=discord.ButtonStyle.secondary, custom_id="admin:whois", row=2
    )
    async def whois(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(UserIdModal(self.cog, "whois", "利用者の状況を見る"))


# ======================================================================= cog


class AdminPanel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(AdminPanelView(self))

    def panel_embed(self) -> discord.Embed:
        cfg = self.bot.cfg
        e = embed(
            f"{cfg.E_KEY} 管理パネル",
            "スラッシュコマンドと同じ操作をここから行えます。\n"
            "ボタンはオーナーだけが使えます。",
            INFO,
            footer=cfg.BRAND_NAME,
        )
        e.add_field(
            name="状況",
            value="ダッシュボード / 承認待ち / 不正フラグ",
            inline=False,
        )
        e.add_field(
            name="点検",
            value="アカウント状態 / 設定一覧 / 台帳監査 / 監査ログ",
            inline=False,
        )
        e.add_field(
            name="利用者への操作",
            value="残高を調整 / ロック解除 / 利用停止 / 停止を解除 / 利用者の状況",
            inline=False,
        )
        e.add_field(
            name="ここに無い操作",
            value=(
                "アカウント登録は `/mcd login` `/kyash login`、\n"
                "設定の変更は `/config` 系を使ってください。"
            ),
            inline=False,
        )
        return e

    @app_commands.command(name="adminpanel", description="管理パネルを設置します（オーナー限定）")
    async def adminpanel_cmd(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await deny(interaction, "オーナー限定のコマンドです")
            return
        await interaction.response.send_message(
            embed=self.panel_embed(), view=AdminPanelView(self)
        )
        await asyncio.to_thread(
            self.bot.store.audit, "adminpanel.posted", interaction.user.id,
            {"channel": interaction.channel_id},
        )

    # ------------------------------------------------------------ 状況

    async def show_dashboard(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        totals = await asyncio.to_thread(self.bot.store.dashboard_totals)
        try:
            kyash_balance = await self.bot.kyash.total_balance()
        except Exception:
            kyash_balance = -1

        e = embed(f"{cfg.E_CHART} ダッシュボード", None, INFO, footer=cfg.BRAND_NAME)
        e.add_field(
            name="預かり残高",
            value=f"**{yen(totals['held_balance'])}**\n利用者 {totals['users']} 人",
            inline=True,
        )
        e.add_field(
            name="Kyash 残高",
            value=("取得できません" if kyash_balance < 0 else f"**{yen(kyash_balance)}**"),
            inline=True,
        )
        e.add_field(
            name="使用可能アカウント",
            value=(
                f"マック {self.bot.mcd.available_count()} 件 / "
                f"Kyash {self.bot.kyash.available_count()} 件"
            ),
            inline=True,
        )
        for name, data in (("今日", totals["today"]), ("今月", totals["month"])):
            e.add_field(
                name=f"{name}の注文",
                value=(
                    f"{data['count']} 件\n"
                    f"定価 {yen(data['face'])}\n"
                    f"利用者負担 {yen(data['paid'])}\n"
                    f"**持ち出し {yen(data['burden'])}**"
                ),
                inline=True,
            )
        e.add_field(name="​", value="​", inline=True)
        e.add_field(
            name="対応待ち",
            value=(
                f"商品レポートの承認 {totals['pending_reports']} 件\n"
                f"未解決の不正フラグ {totals['open_flags']} 件"
            ),
            inline=False,
        )
        running = active_campaigns(cfg.campaigns())
        e.add_field(
            name="適用中のキャンペーン",
            value="\n".join(f"{c.name}: 負担 {c.rate}%" for c in running) if running else "なし",
            inline=False,
        )
        await reply(interaction, e)

    async def show_pending(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        pending = await asyncio.to_thread(self.bot.store.pending_reports)
        if not pending:
            await reply(interaction, embed(f"{cfg.E_OK} 承認待ちはありません", "", OK))
            return
        lines = [
            f"`#{r['id']}` <@{r['user_id']}> ・ 画像 {r['image_count']} 枚 ・ {r['created_at'][5:16]}"
            for r in pending[:25]
        ]
        await reply(
            interaction,
            embed(
                f"{cfg.E_MEMO} 承認待ちの商品レポート {len(pending)} 件",
                "\n".join(lines)[:3800]
                + "\n\n承認・却下は承認チャンネルの投稿にあるボタンから行います。",
                WARN,
            ),
        )

    async def show_flags(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(self.bot.store.open_fraud_flags)
        if not rows:
            await reply(interaction, embed(f"{cfg.E_OK} 未解決のフラグはありません", "", OK))
            return
        lines = [
            f"`#{r['id']}` <@{r['user_id']}> **{r['kind']}** ({r['score']})\n　└ {str(r['detail'])[:110]}"
            for r in rows[:15]
        ]
        await reply(
            interaction,
            embed(
                f"{cfg.E_FLAG} 未解決のフラグ {len(rows)} 件",
                "\n".join(lines)[:3800] + "\n\n解決済みにするには `/mcd resolve <ID>`",
                WARN,
            ),
        )

    # ------------------------------------------------------------ 点検

    async def show_accounts(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        mcd_rows = await asyncio.to_thread(self.bot.store.list_mcd_accounts)
        kyash_status = await asyncio.to_thread(self.bot.kyash.token_status)

        e = embed(f"{cfg.E_SHOP} アカウントの状態", None, INFO, footer=cfg.BRAND_NAME)

        if mcd_rows:
            lines = [
                f"{cfg.E_OK if int(r['enabled']) else cfg.E_NG} `#{r['id']}` {r['label']}"
                f" ・ 失敗 {r['fail_count']} 回"
                for r in mcd_rows
            ]
            e.add_field(name="マクドナルド", value="\n".join(lines)[:1020], inline=False)
        else:
            e.add_field(
                name="マクドナルド", value="未登録。`/mcd login` で登録してください。", inline=False
            )

        if kyash_status:
            lines = []
            for item in kyash_status:
                days = item["days_left"]
                expiry = (
                    "期限不明" if days is None
                    else (f"あと {days} 日" if days > cfg.KYASH_TOKEN_WARN_DAYS
                          else f"{cfg.E_WARN} あと {days} 日")
                )
                lines.append(
                    f"{cfg.E_OK if item['enabled'] else cfg.E_NG} `#{item['id']}` "
                    f"{item['label']} ・ {expiry}"
                )
            e.add_field(name="Kyash", value="\n".join(lines)[:1020], inline=False)
        else:
            e.add_field(
                name="Kyash", value="未登録。`/kyash login` で登録してください。", inline=False
            )

        e.add_field(
            name="待ち行列",
            value=(
                f"処理中 {self.bot.mcd.inflight} 件 / 順番待ち {self.bot.mcd.waiting} 人"
            ),
            inline=False,
        )
        await reply(interaction, e)

    async def show_settings(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        from .config import fmt_value

        e = embed(f"{cfg.E_KEY} 現在の設定", None, INFO)
        for name in GROUPS:
            lines = []
            for spec, value in cfg.all_of(name):
                mark = "" if cfg.is_default(spec.key) else " *"
                lines.append(f"`{spec.key}`{mark} {fmt_value(spec, value)}")
            if lines:
                e.add_field(name=name, value="\n".join(lines)[:1020], inline=False)

        missing = cfg.missing_required()
        if missing:
            e.add_field(
                name=f"{cfg.E_WARN} 未設定",
                value="\n".join(f"・{s.label}" for s in missing)[:1020],
                inline=False,
            )
        e.set_footer(text="* は既定値から変更済み ・ 変更は /config set")
        await reply(interaction, e)

    async def show_audit(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        mismatches = await asyncio.to_thread(self.bot.store.audit_balances)
        if not mismatches:
            await reply(
                interaction,
                embed(
                    f"{cfg.E_OK} 整合しています",
                    "すべての利用者で「台帳の合計 = 残高」が一致しました。",
                    OK,
                ),
            )
            return
        lines = [
            f"<@{m['user_id']}> 残高 {m['balance']:,} / 台帳 {m['ledger_sum']:,} "
            f"(差 {m['balance'] - m['ledger_sum']:+,})"
            for m in mismatches[:20]
        ]
        await reply(
            interaction,
            embed(f"{cfg.E_WARN} 不整合が {len(mismatches)} 件あります", "\n".join(lines), BAD),
        )

    async def show_logs(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(self.bot.store.recent_audit, 15, "")
        if not rows:
            await reply(interaction, embed("ログがありません", "", INFO))
            return
        lines = [
            f"`{r['created_at'][5:16]}` **{r['event']}** "
            + (f"<@{r['actor_id']}> " if r["actor_id"] else "")
            + (str(r["detail"])[:90] if r["detail"] else "")
            for r in rows
        ]
        await reply(
            interaction,
            embed(
                f"{cfg.E_CHART} 監査ログ（直近15件）",
                "\n".join(lines)[:3800] + "\n\n絞り込みは `/mcd logs`",
                INFO,
            ),
        )

    # -------------------------------------------------- 利用者への操作

    @staticmethod
    def _parse_user_id(raw: str) -> Optional[int]:
        text = "".join(ch for ch in (raw or "") if ch.isdigit())
        return int(text) if text else None

    async def do_adjust(
        self, interaction: discord.Interaction, raw_user: str, raw_amount: str, memo: str
    ) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        uid = self._parse_user_id(raw_user)
        if uid is None:
            await reply(interaction, embed(f"{cfg.E_NG} 利用者IDが不正です", "", BAD))
            return
        try:
            amount = int(str(raw_amount).replace(",", "").strip())
        except ValueError:
            await reply(
                interaction,
                embed(f"{cfg.E_NG} 金額が不正です", "`-500` のように入力してください。", BAD),
            )
            return
        if amount == 0:
            await reply(interaction, embed(f"{cfg.E_NG} 0 は指定できません", "", BAD))
            return

        balance = await asyncio.to_thread(
            self.bot.store.adjust, uid, amount, memo or f"管理パネルから調整 by {interaction.user}"
        )
        await asyncio.to_thread(
            self.bot.store.audit, "balance.adjusted", interaction.user.id,
            {"user": uid, "amount": amount, "memo": memo, "via": "adminpanel"},
        )
        await reply(
            interaction,
            embed(
                f"{cfg.E_MONEY} 残高を調整しました",
                f"<@{uid}>: {amount:+,} 円 → 残高 {yen(balance)}",
                MONEY,
            ),
        )
        await self.bot.send_log(
            cfg.LOG_MONEY_CHANNEL_ID,
            embed(
                f"{cfg.E_KEY} 手動調整（管理パネル）",
                f"<@{uid}> {amount:+,} 円 → {yen(balance)}\n"
                f"実行: {interaction.user.mention}\nメモ: {memo or '-'}",
                WARN,
            ),
        )

    async def do_user_action(
        self, interaction: discord.Interaction, action: str, raw_user: str
    ) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        uid = self._parse_user_id(raw_user)
        if uid is None:
            await reply(interaction, embed(f"{cfg.E_NG} 利用者IDが不正です", "", BAD))
            return

        if action == "whois":
            await self._show_user(interaction, uid)
            return

        if action == "unlock":
            blocking = await asyncio.to_thread(self.bot.store.blocking_order, uid)
            if blocking is None:
                await reply(interaction, embed(f"{cfg.E_OK} ロックされていません", "", OK))
                return
            await asyncio.to_thread(
                self.bot.store.set_report_status, int(blocking["id"]), "approved"
            )
            await asyncio.to_thread(
                self.bot.store.audit, "report.unlocked", interaction.user.id,
                {"user": uid, "order": int(blocking["id"]), "via": "adminpanel"},
            )
            await reply(
                interaction,
                embed(
                    f"{cfg.E_OK} ロックを解除しました",
                    f"<@{uid}> ・ 注文 #{blocking['id']}",
                    OK,
                ),
            )
            return

        banned = action == "ban"
        await asyncio.to_thread(self.bot.store.set_banned, uid, banned)
        await asyncio.to_thread(
            self.bot.store.audit, "user.banned" if banned else "user.unbanned",
            interaction.user.id, {"user": uid, "via": "adminpanel"},
        )
        await reply(
            interaction,
            embed(
                f"{cfg.E_LOCK} 利用を停止しました" if banned else f"{cfg.E_OK} 利用を再開しました",
                f"<@{uid}>",
                BAD if banned else OK,
            ),
        )
        await self.bot.send_log(
            cfg.LOG_ADMIN_CHANNEL_ID,
            embed(
                f"{cfg.E_LOCK} 利用停止" if banned else f"{cfg.E_OK} 停止解除",
                f"<@{uid}>\n実行: {interaction.user.mention}",
                BAD if banned else OK,
            ),
        )

    async def _show_user(self, interaction: discord.Interaction, uid: int) -> None:
        cfg = self.bot.cfg
        row = await asyncio.to_thread(self.bot.store.get_user, uid)
        if row is None:
            await reply(
                interaction, embed(f"{cfg.E_NG} 利用実績がありません", f"<@{uid}>", BAD)
            )
            return

        orders = await asyncio.to_thread(self.bot.store.orders_of, uid, 5)
        ledger = await asyncio.to_thread(self.bot.store.ledger_of, uid, 5)
        blocking = await asyncio.to_thread(self.bot.store.blocking_order, uid)
        flags = await asyncio.to_thread(self.bot.store.open_fraud_flags, uid)

        e = embed(f"{cfg.E_CHART} 利用者の状況", f"<@{uid}>", INFO, footer=f"ID {uid}")
        e.add_field(name="残高", value=f"**{yen(row['balance'])}**", inline=True)
        e.add_field(name="累計注文", value=f"{row['total_orders']} 回", inline=True)
        e.add_field(
            name="割引の累計",
            value=yen(int(row["total_face"]) - int(row["total_paid"])),
            inline=True,
        )
        e.add_field(
            name="状態",
            value=(
                (f"{cfg.E_LOCK} 利用停止中\n" if int(row["banned"]) else "")
                + (f"{cfg.E_CLOCK} 感想が未完了（注文 #{blocking['id']}）\n" if blocking else "")
                + (f"{cfg.E_FLAG} 未解決のフラグ {len(flags)} 件\n" if flags else "")
                or f"{cfg.E_OK} 問題なし"
            ),
            inline=False,
        )
        e.add_field(
            name="規約同意",
            value=(
                f"版 {row['terms_version']}（{row['terms_agreed_at']}）"
                if row["terms_version"] else "未同意"
            ),
            inline=False,
        )
        if row["referred_by"]:
            e.add_field(name="紹介元", value=f"<@{int(row['referred_by'])}>", inline=True)
        e.add_field(name="紹介コード", value=f"`{row['referral_code']}`", inline=True)

        if ledger:
            e.add_field(
                name="残高の動き",
                value="\n".join(
                    f"`{x['created_at'][5:16]}` {x['kind']} {int(x['amount']):+,} "
                    f"→ {int(x['balance_after']):,}"
                    for x in ledger
                )[:1020],
                inline=False,
            )
        if orders:
            e.add_field(
                name="注文履歴",
                value="\n".join(
                    f"`{o['created_at'][5:16]}` {o['status']} / {yen(o['paid_amount'])} "
                    f"/ 番号 {o['receipt_number'] or '-'}"
                    for o in orders
                )[:1020],
                inline=False,
            )
        await reply(interaction, e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminPanel(bot))
