"""管理コマンド。すべてオーナー限定。

アカウントの登録はスラッシュコマンドの引数ではなく Modal で受ける。
引数に書くとコマンド履歴にトークンが残るため。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from mcd.rates import active_campaigns

from ._shared import BAD, INFO, MONEY, OK, WARN, deny, embed, reply, yen

log = logging.getLogger("bot.manage")


async def owner_gate(interaction: discord.Interaction) -> bool:
    if await interaction.client.is_owner(interaction.user):
        return True
    await deny(interaction, "オーナー限定のコマンドです")
    return False


# ====================================================== アカウント登録モーダル


class McdTokenModal(discord.ui.Modal, title="マクドナルドアカウントの登録"):
    label = discord.ui.TextInput(label="表示名", placeholder="メイン", max_length=40)
    refresh_token = discord.ui.TextInput(
        label="refresh_token", style=discord.TextStyle.paragraph, max_length=2000
    )
    card_id = discord.ui.TextInput(
        label="card_id（空欄なら既定のカードを自動選択）", required=False, max_length=120
    )

    def __init__(self, cog: "Manage"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.register_mcd(
            interaction, str(self.label.value), str(self.refresh_token.value),
            str(self.card_id.value or ""),
        )


class McdLoginModal(discord.ui.Modal, title="マクドナルドにログイン"):
    label = discord.ui.TextInput(label="表示名", placeholder="メイン", max_length=40)
    email = discord.ui.TextInput(label="メールアドレス", max_length=200)
    password = discord.ui.TextInput(label="パスワード", max_length=200)

    def __init__(self, cog: "Manage"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.mcd_login_step1(
            interaction, str(self.label.value), str(self.email.value), str(self.password.value)
        )


class OtpModal(discord.ui.Modal, title="ワンタイムパスワード"):
    otp = discord.ui.TextInput(label="SMS等に届いた確認コード", max_length=12)

    def __init__(self, cog: "Manage", kind: str, label: str):
        super().__init__()
        self.cog = cog
        self.kind = kind
        self.label_value = label

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.kind == "mcd":
            await self.cog.mcd_login_step2(interaction, self.label_value, str(self.otp.value))
        else:
            await self.cog.kyash_login_step2(interaction, self.label_value, str(self.otp.value))


class KyashTokenModal(discord.ui.Modal, title="Kyash アカウントの登録"):
    label = discord.ui.TextInput(label="表示名", placeholder="受取用1", max_length=40)
    access_token = discord.ui.TextInput(
        label="access_token", style=discord.TextStyle.paragraph, max_length=2000
    )
    client_uuid = discord.ui.TextInput(label="client_uuid（任意）", required=False, max_length=80)
    installation_uuid = discord.ui.TextInput(
        label="installation_uuid（任意）", required=False, max_length=80
    )

    def __init__(self, cog: "Manage"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.register_kyash(
            interaction, str(self.label.value), str(self.access_token.value),
            "", str(self.client_uuid.value or ""), str(self.installation_uuid.value or ""),
        )


class KyashLoginModal(discord.ui.Modal, title="Kyash にログイン"):
    label = discord.ui.TextInput(label="表示名", placeholder="受取用1", max_length=40)
    email = discord.ui.TextInput(label="メールアドレス", max_length=200)
    password = discord.ui.TextInput(label="パスワード", max_length=200)

    def __init__(self, cog: "Manage"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.kyash_login_step1(
            interaction, str(self.label.value), str(self.email.value), str(self.password.value)
        )


class OtpPromptView(discord.ui.View):
    def __init__(self, cog: "Manage", kind: str, label: str, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.kind = kind
        self.label_value = label
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="確認コードを入力", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(OtpModal(self.cog, self.kind, self.label_value))


# ====================================================================== cog


class Manage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_mcd: dict[int, tuple[str, object]] = {}
        self._pending_kyash: dict[int, object] = {}

    # ------------------------------------------------------------ /mcd 群

    mcd_group = app_commands.Group(name="mcd", description="マクドナルド側の管理（オーナー限定）")
    kyash_group = app_commands.Group(name="kyash", description="Kyash 側の管理（オーナー限定）")

    # ---- アカウント登録 --------------------------------------------------

    @mcd_group.command(name="login", description="メールとパスワードでマクドナルドにログインします")
    async def mcd_login(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        await interaction.response.send_modal(McdLoginModal(self))

    @mcd_group.command(name="token", description="refresh_token を直接登録します")
    async def mcd_token(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        await interaction.response.send_modal(McdTokenModal(self))

    async def mcd_login_step1(
        self, interaction: discord.Interaction, label: str, email: str, password: str
    ) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        from vendor.HATTIMCD.main import MCD

        client = MCD()
        try:
            mfa_token = await asyncio.to_thread(client.login, email, password)
        except Exception as exc:
            await reply(interaction, embed(f"{cfg.E_NG} ログインに失敗しました", str(exc)[:300], BAD))
            return

        self._pending_mcd[interaction.user.id] = (mfa_token, client)
        await reply(
            interaction,
            embed(
                f"{cfg.E_KEY} 確認コードを入力してください",
                "SMS 等に届いたコードを入力してください。",
                INFO,
            ),
            view=OtpPromptView(self, "mcd", label, interaction.user.id),
        )

    async def mcd_login_step2(
        self, interaction: discord.Interaction, label: str, otp: str
    ) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        pending = self._pending_mcd.pop(interaction.user.id, None)
        if pending is None:
            await reply(interaction, embed(f"{cfg.E_NG} セッションが切れました", "やり直してください。", BAD))
            return

        mfa_token, client = pending
        try:
            tokens = await asyncio.to_thread(client.login_with_mfa, mfa_token, otp)
        except Exception as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 認証に失敗しました", str(exc)[:300], BAD))
            return

        await self.register_mcd(interaction, label, tokens.refresh_token, "")

    async def register_mcd(
        self, interaction: discord.Interaction, label: str, refresh_token: str, card_id: str
    ) -> None:
        cfg = self.bot.cfg
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            account_id = await asyncio.to_thread(
                self.bot.store.add_mcd_account, label.strip(), refresh_token.strip(), card_id.strip()
            )
        except Exception as exc:
            await reply(
                interaction,
                embed(f"{cfg.E_NG} 登録できませんでした", f"{exc}\n表示名が重複していませんか？", BAD),
            )
            return

        ok, detail = await self.bot.mcd.health_check(account_id)
        await asyncio.to_thread(
            self.bot.store.audit, "mcd.account.added", interaction.user.id,
            {"account": account_id, "label": label},
        )
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} マクドナルドアカウントを登録しました" if ok
                else f"{cfg.E_WARN} 登録しましたが認証に失敗しています",
                f"#{account_id} {label}\n認証チェック: {detail}",
                OK if ok else WARN,
            ),
        )

    @kyash_group.command(name="login", description="メールとパスワードで Kyash にログインします")
    async def kyash_login(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        await interaction.response.send_modal(KyashLoginModal(self))

    @kyash_group.command(name="token", description="access_token を直接登録します")
    async def kyash_token(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        await interaction.response.send_modal(KyashTokenModal(self))

    async def kyash_login_step1(
        self, interaction: discord.Interaction, label: str, email: str, password: str
    ) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        from vendor.Kyasher.main import Kyash

        try:
            client = await asyncio.to_thread(Kyash, email, password)
        except Exception as exc:
            await reply(interaction, embed(f"{cfg.E_NG} ログインに失敗しました", str(exc)[:300], BAD))
            return

        self._pending_kyash[interaction.user.id] = client
        await reply(
            interaction,
            embed(
                f"{cfg.E_KEY} 確認コードを入力してください",
                "SMS に届いた 6 桁のコードを入力してください。",
                INFO,
            ),
            view=OtpPromptView(self, "kyash", label, interaction.user.id),
        )

    async def kyash_login_step2(
        self, interaction: discord.Interaction, label: str, otp: str
    ) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = self._pending_kyash.pop(interaction.user.id, None)
        if client is None:
            await reply(interaction, embed(f"{cfg.E_NG} セッションが切れました", "やり直してください。", BAD))
            return

        try:
            await asyncio.to_thread(client.login, otp)
        except Exception as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 認証に失敗しました", str(exc)[:300], BAD))
            return

        await self.register_kyash(
            interaction, label, client.access_token, client.email or "",
            client.client_uuid, client.installation_uuid,
        )

    async def register_kyash(
        self, interaction: discord.Interaction, label: str, access_token: str,
        email: str, client_uuid: str, installation_uuid: str,
    ) -> None:
        cfg = self.bot.cfg
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            account_id = await asyncio.to_thread(
                self.bot.store.add_kyash_account, label.strip(), access_token.strip(),
                email, client_uuid, installation_uuid,
            )
        except Exception as exc:
            await reply(
                interaction,
                embed(f"{cfg.E_NG} 登録できませんでした", f"{exc}\n表示名が重複していませんか？", BAD),
            )
            return

        ok, detail = await self.bot.kyash.health_check(account_id)
        await asyncio.to_thread(
            self.bot.store.audit, "kyash.account.added", interaction.user.id,
            {"account": account_id, "label": label},
        )
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} Kyash アカウントを登録しました" if ok
                else f"{cfg.E_WARN} 登録しましたが疎通に失敗しています",
                f"#{account_id} {label}\n疎通チェック: {detail}\n"
                f"アクセストークンの有効期限は約 30 日です。",
                OK if ok else WARN,
            ),
        )

    # ---- アカウント一覧・操作 --------------------------------------------

    @mcd_group.command(name="accounts", description="マクドナルドアカウントの一覧")
    async def mcd_accounts(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(self.bot.store.list_mcd_accounts)
        if not rows:
            await reply(
                interaction,
                embed(f"{cfg.E_WARN} 登録がありません", "`/mcd login` または `/mcd token` で登録してください。", WARN),
            )
            return
        lines = []
        for row in rows:
            mark = cfg.E_OK if int(row["enabled"]) else cfg.E_NG
            lines.append(
                f"{mark} `#{row['id']}` **{row['label']}** ・ 失敗 {row['fail_count']} 回"
                + (f"\n　└ カード `{row['card_id']}`" if row["card_id"] else "")
                + (f"\n　└ {str(row['last_error'])[:90]}" if row["last_error"] else "")
            )
        await reply(
            interaction,
            embed(f"{cfg.E_FOOD} マクドナルドアカウント {len(rows)} 件", "\n".join(lines)[:3800], INFO),
        )

    @kyash_group.command(name="accounts", description="Kyash アカウントの一覧と期限")
    async def kyash_accounts(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        status = await asyncio.to_thread(self.bot.kyash.token_status)
        if not status:
            await reply(
                interaction,
                embed(f"{cfg.E_WARN} 登録がありません", "`/kyash login` で登録してください。", WARN),
            )
            return
        lines = []
        for item in status:
            mark = cfg.E_OK if item["enabled"] else cfg.E_NG
            days = item["days_left"]
            expiry = "不明" if days is None else (
                f"あと {days} 日" if days > cfg.KYASH_TOKEN_WARN_DAYS
                else f"{cfg.E_WARN} あと {days} 日"
            )
            lines.append(
                f"{mark} `#{item['id']}` **{item['label']}** ・ 期限 {expiry} ・ 失敗 {item['fail_count']} 回"
                + (f"\n　└ {str(item['last_error'])[:90]}" if item["last_error"] else "")
            )
        await reply(
            interaction,
            embed(f"{cfg.E_MONEY} Kyash アカウント {len(status)} 件", "\n".join(lines)[:3800], INFO),
        )

    @mcd_group.command(name="account", description="アカウントの有効化・無効化・削除")
    @app_commands.describe(account_id="対象のID", action="操作")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="有効にする", value="enable"),
            app_commands.Choice(name="無効にする", value="disable"),
            app_commands.Choice(name="削除する", value="delete"),
            app_commands.Choice(name="接続を確認する", value="check"),
        ]
    )
    async def mcd_account(
        self, interaction: discord.Interaction, account_id: int, action: app_commands.Choice[str]
    ) -> None:
        if not await owner_gate(interaction):
            return
        await self._account_action(interaction, "mcd", account_id, action.value)

    @kyash_group.command(name="account", description="アカウントの有効化・無効化・削除")
    @app_commands.describe(account_id="対象のID", action="操作")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="有効にする", value="enable"),
            app_commands.Choice(name="無効にする", value="disable"),
            app_commands.Choice(name="削除する", value="delete"),
            app_commands.Choice(name="接続を確認する", value="check"),
        ]
    )
    async def kyash_account(
        self, interaction: discord.Interaction, account_id: int, action: app_commands.Choice[str]
    ) -> None:
        if not await owner_gate(interaction):
            return
        await self._account_action(interaction, "kyash", account_id, action.value)

    async def _account_action(
        self, interaction: discord.Interaction, kind: str, account_id: int, action: str
    ) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        store = self.bot.store
        update = store.update_mcd_account if kind == "mcd" else store.update_kyash_account
        delete = store.delete_mcd_account if kind == "mcd" else store.delete_kyash_account
        pool = self.bot.mcd if kind == "mcd" else self.bot.kyash

        if action == "check":
            ok, detail = await pool.health_check(account_id)
            await reply(
                interaction,
                embed(
                    f"{cfg.E_OK} 接続できました" if ok else f"{cfg.E_NG} 接続できません",
                    f"#{account_id}: {detail}",
                    OK if ok else BAD,
                ),
            )
            return

        if action == "delete":
            await asyncio.to_thread(delete, account_id)
            message = "削除しました"
        else:
            await asyncio.to_thread(
                update, account_id, enabled=1 if action == "enable" else 0, fail_count=0
            )
            message = "有効にしました" if action == "enable" else "無効にしました"

        await asyncio.to_thread(
            store.audit, f"{kind}.account.{action}", interaction.user.id, {"account": account_id}
        )
        await reply(interaction, embed(f"{cfg.E_OK} {message}", f"#{account_id}", OK))

    # ---- 残高操作 --------------------------------------------------------

    @mcd_group.command(name="adjust", description="利用者の残高を手動で増減します")
    @app_commands.describe(user="対象", amount="増減額（マイナス可）", memo="台帳に残すメモ")
    async def adjust(
        self, interaction: discord.Interaction, user: discord.User, amount: int, memo: str = ""
    ) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        balance = await asyncio.to_thread(
            self.bot.store.adjust, user.id, amount, memo or f"オーナー調整 by {interaction.user}"
        )
        await asyncio.to_thread(
            self.bot.store.audit, "balance.adjusted", interaction.user.id,
            {"user": user.id, "amount": amount, "memo": memo},
        )
        await reply(
            interaction,
            embed(
                f"{cfg.E_MONEY} 残高を調整しました",
                f"{user.mention}: {amount:+,} 円 → 残高 {yen(balance)}",
                MONEY,
            ),
        )
        await self.bot.send_log(
            cfg.LOG_MONEY_CHANNEL_ID,
            embed(
                f"{cfg.E_KEY} 手動調整",
                f"{user.mention} {amount:+,} 円 → {yen(balance)}\n"
                f"実行: {interaction.user.mention}\nメモ: {memo or '-'}",
                WARN,
            ),
        )

    @mcd_group.command(name="unlock", description="感想の未提出による注文ロックを解除します")
    @app_commands.describe(user="対象")
    async def unlock(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        blocking = await asyncio.to_thread(self.bot.store.blocking_order, user.id)
        if blocking is None:
            await reply(interaction, embed(f"{cfg.E_OK} ロックされていません", "", OK))
            return
        await asyncio.to_thread(
            self.bot.store.set_report_status, int(blocking["id"]), "approved"
        )
        await asyncio.to_thread(
            self.bot.store.audit, "report.unlocked", interaction.user.id,
            {"user": user.id, "order": int(blocking["id"])},
        )
        await reply(
            interaction,
            embed(f"{cfg.E_OK} ロックを解除しました", f"{user.mention} ・ 注文 #{blocking['id']}", OK),
        )

    @mcd_group.command(name="ban", description="利用者の利用を停止／再開します")
    @app_commands.describe(user="対象", banned="True で停止、False で再開")
    async def ban(self, interaction: discord.Interaction, user: discord.User, banned: bool) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        await asyncio.to_thread(self.bot.store.set_banned, user.id, banned)
        await asyncio.to_thread(
            self.bot.store.audit, "user.banned" if banned else "user.unbanned",
            interaction.user.id, {"user": user.id},
        )
        await reply(
            interaction,
            embed(
                f"{cfg.E_LOCK} 利用を停止しました" if banned else f"{cfg.E_OK} 利用を再開しました",
                user.mention,
                BAD if banned else OK,
            ),
        )

    # ---- 監査・ログ ------------------------------------------------------

    @mcd_group.command(name="audit", description="台帳と残高の整合性を確認します")
    async def audit(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
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
            embed(
                f"{cfg.E_WARN} 不整合が {len(mismatches)} 件あります",
                "\n".join(lines),
                BAD,
            ),
        )

    @mcd_group.command(name="logs", description="直近の監査ログを表示します")
    @app_commands.describe(count="表示件数", keyword="イベント名の絞り込み")
    async def logs(
        self, interaction: discord.Interaction, count: int = 15, keyword: str = ""
    ) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(
            self.bot.store.recent_audit, max(1, min(count, 40)), keyword
        )
        if not rows:
            await reply(interaction, embed("該当するログがありません", "", INFO))
            return
        lines = [
            f"`{r['created_at'][5:16]}` **{r['event']}** "
            + (f"<@{r['actor_id']}> " if r["actor_id"] else "")
            + (str(r["detail"])[:110] if r["detail"] else "")
            for r in rows
        ]
        await reply(
            interaction,
            embed(f"{cfg.E_CHART} 監査ログ", "\n".join(lines)[:3800], INFO),
        )

    @mcd_group.command(name="flags", description="未解決の不正フラグを表示します")
    async def flags(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(self.bot.store.open_fraud_flags)
        if not rows:
            await reply(interaction, embed(f"{cfg.E_OK} 未解決のフラグはありません", "", OK))
            return
        lines = [
            f"`#{r['id']}` <@{r['user_id']}> **{r['kind']}** ({r['score']})\n　└ {str(r['detail'])[:120]}"
            for r in rows[:15]
        ]
        await reply(
            interaction,
            embed(f"{cfg.E_FLAG} 未解決のフラグ {len(rows)} 件", "\n".join(lines)[:3800], WARN),
        )

    @mcd_group.command(name="resolve", description="不正フラグを解決済みにします")
    @app_commands.describe(flag_id="フラグのID")
    async def resolve(self, interaction: discord.Interaction, flag_id: int) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        await asyncio.to_thread(self.bot.store.resolve_fraud_flag, flag_id)
        await reply(interaction, embed(f"{cfg.E_OK} 解決済みにしました", f"#{flag_id}", OK))

    # ---- ダッシュボード --------------------------------------------------

    @app_commands.command(name="dashboard", description="全体の状況を表示します（オーナー限定）")
    async def dashboard(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
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
            value=f"マック {self.bot.mcd.available_count()} 件 / Kyash {self.bot.kyash.available_count()} 件",
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
            value=(
                "\n".join(f"{c.name}: 負担 {c.rate}%" for c in running) if running else "なし"
            ),
            inline=False,
        )
        await reply(interaction, e)

    # 「自分の負担率」と「ランキング」は利用者向けなので、
    # スラッシュコマンドではなく panel.py のボタンから開く。


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Manage(bot))
