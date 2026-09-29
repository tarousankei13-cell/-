"""
アカウント管理（管理者用）

マクドナルド・Kyash とも、メールとパスワードを入力したあと SMS 等で届く
6桁の認証コードが必要になる。

Discord のモーダルは「送信から3秒以内に応答」が必要で、その間に
ログイン通信を挟むと間に合わないことがある。そのため
  ① 認証情報のモーダル → すぐ「認証コードを入力」ボタンを表示
  ② 裏でログイン処理
  ③ ボタンを押して認証コードのモーダル
という二段構えにしている。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import emoji as E
from core.crypto import get_cipher
from db.models import KyashAccount, McdAccount, McdToken, utcnow
from db.session import session_scope
from cogs._checks import admin_only, handle_check_failure
from services.kyash.client import KyashClient, KyashError
from services.mcd import accounts as mcd_accounts
from services.mcd.client import Fingerprint, McdClient, McdError
from ui import embeds

log = logging.getLogger("bot.cogs.account")


# ============================================================
#  マクドナルド
# ============================================================

class McdCredModal(discord.ui.Modal, title="マクドナルドアカウントを追加"):
    label = discord.ui.TextInput(label="表示名（任意）", required=False, max_length=32,
                                 placeholder="例: メイン")
    email = discord.ui.TextInput(label="メールアドレス", required=True, max_length=128)
    password = discord.ui.TextInput(
        label="パスワード", required=True, max_length=128,
        placeholder="この入力はあなたにしか見えません",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        email = str(self.email.value).strip()
        password = str(self.password.value)
        label = str(self.label.value).strip() or email.split("@")[0]

        await interaction.response.send_message(
            embed=embeds.info(f"{E.LOADING} ログインしています…"), ephemeral=True
        )

        fp = Fingerprint.generate(*mcd_accounts.random_home_location())
        client = McdClient(fp)
        try:
            mfa_token = await client.login(email, password)
        except McdError as e:
            await client.aclose()
            await interaction.edit_original_response(embed=embeds.error(str(e)))
            return

        await interaction.edit_original_response(
            embed=embeds.info(
                f"{E.KEY} 認証コードを送信しました。\n"
                "SMS またはメールに届いた **6桁** を、下のボタンから入力してください。"
            ),
            view=McdOtpView(interaction.user.id, client, fp, mfa_token, email, label),
        )


class McdOtpView(discord.ui.View):
    def __init__(self, owner_id, client, fp, mfa_token, email, label) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.client = client
        self.fp = fp
        self.mfa_token = mfa_token
        self.email = email
        self.label = label
        self.attempts = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_timeout(self) -> None:
        await self.client.aclose()

    @discord.ui.button(label="認証コードを入力", emoji=E.KEY, style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(McdOtpModal(self))


class McdOtpModal(discord.ui.Modal, title="認証コードの入力"):
    otp = discord.ui.TextInput(label="6桁の認証コード", required=True, min_length=4, max_length=8)

    def __init__(self, parent: McdOtpView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = self.parent
        try:
            tokens = await p.client.login_with_mfa(p.mfa_token, str(self.otp.value).strip())
        except McdError as e:
            p.attempts += 1
            if p.attempts >= 3:
                await p.client.aclose()
                await interaction.followup.send(
                    embed=embeds.error("認証に3回失敗しました。最初からやり直してください。"),
                    ephemeral=True,
                )
                p.stop()
                return
            await interaction.followup.send(
                embed=embeds.error(f"{e}\nもう一度お試しください（残り {3 - p.attempts} 回）。"),
                ephemeral=True,
            )
            return

        cipher = get_cipher()
        lat, lng = p.fp.latitude, p.fp.longitude
        async with session_scope() as s:
            acc = McdAccount(
                label=p.label,
                email_enc=cipher.encrypt(p.email),
                refresh_token_enc=cipher.encrypt(tokens.refresh_token),
                device_uid=p.fp.device_uid,
                wmop_device_id=p.fp.wmop_device_id,
                fb_instance_id=p.fp.fb_instance_id,
                home_lat=lat, home_lng=lng,
            )
            s.add(acc)
            await s.flush()
            account_id = acc.id

        # カードを取得して選ばせる
        cards = []
        try:
            cards = await p.client.get_cards()
        except McdError as e:
            log.info("カード一覧を取得できませんでした: %s", e)
        await p.client.aclose()
        p.stop()

        if not cards:
            await interaction.followup.send(
                embed=embeds.warn(
                    f"アカウント `#{account_id}` **{p.label}** を登録しました。\n\n"
                    f"{E.WARN} 決済カードが見つかりませんでした。\n"
                    "マクドナルド公式アプリでカードを登録してから "
                    f"`/mcd card {account_id}` を実行してください。"
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=embeds.ok(
                f"アカウント `#{account_id}` **{p.label}** を登録しました。\n"
                "決済に使うカードを選んでください。"
            ),
            view=CardSelectView(interaction.user.id, account_id, cards),
            ephemeral=True,
        )


class CardSelectView(discord.ui.View):
    def __init__(self, owner_id: int, account_id: int, cards: list[dict]) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.account_id = account_id
        options = [
            discord.SelectOption(
                label=(c.get("masked") or c.get("card_id", ""))[:100],
                value=c["card_id"],
                description=f"{c.get('name', '')} {c.get('expiry', '')}".strip()[:100] or None,
            )
            for c in cards if c.get("card_id")
        ][:25]
        sel = discord.ui.Select(placeholder="決済カードを選んでください", options=options)
        sel.callback = self._on_pick
        self.add_item(sel)
        self._sel = sel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        card_id = self._sel.values[0]
        async with session_scope() as s:
            acc = await s.get(McdAccount, self.account_id)
            if acc:
                acc.card_id = card_id
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.ok(
                f"{E.CARD} 決済カードを設定しました。\n"
                f"アカウント `#{self.account_id}` は注文に使用できます。"
            ),
            view=None,
        )


# ============================================================
#  Kyash
# ============================================================

class KyashCredModal(discord.ui.Modal, title="Kyashアカウントを追加"):
    label = discord.ui.TextInput(label="表示名（任意）", required=False, max_length=32)
    email = discord.ui.TextInput(label="メールアドレス", required=True, max_length=128)
    password = discord.ui.TextInput(label="パスワード", required=True, max_length=128)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        email = str(self.email.value).strip()
        password = str(self.password.value)
        label = str(self.label.value).strip() or email.split("@")[0]

        await interaction.response.send_message(
            embed=embeds.info(f"{E.LOADING} ログインしています…"), ephemeral=True
        )

        client = KyashClient()
        try:
            need_otp = await client.start_login(email, password)
        except KyashError as e:
            await client.aclose()
            await interaction.edit_original_response(embed=embeds.error(str(e)))
            return

        if not need_otp:
            account_id = await _save_kyash(client, email, password, label)
            await client.aclose()
            await interaction.edit_original_response(
                embed=embeds.ok(f"Kyashアカウント `#{account_id}` **{label}** を登録しました。")
            )
            return

        await interaction.edit_original_response(
            embed=embeds.info(
                f"{E.KEY} 認証コードを送信しました。\n"
                "SMS に届いた **6桁** を、下のボタンから入力してください。"
            ),
            view=KyashOtpView(interaction.user.id, client, email, password, label),
        )


class KyashOtpView(discord.ui.View):
    def __init__(self, owner_id, client, email, password, label) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.client = client
        self.email = email
        self.password = password
        self.label = label
        self.attempts = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_timeout(self) -> None:
        await self.client.aclose()

    @discord.ui.button(label="認証コードを入力", emoji=E.KEY, style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(KyashOtpModal(self))


class KyashOtpModal(discord.ui.Modal, title="認証コードの入力"):
    otp = discord.ui.TextInput(label="6桁の認証コード", required=True, min_length=4, max_length=8)

    def __init__(self, parent: KyashOtpView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = self.parent
        try:
            await p.client.verify_otp(str(self.otp.value).strip(), p.email)
        except KyashError as e:
            p.attempts += 1
            if p.attempts >= 3:
                await p.client.aclose()
                p.stop()
                await interaction.followup.send(
                    embed=embeds.error("認証に3回失敗しました。最初からやり直してください。"),
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                embed=embeds.error(f"{e}\nもう一度お試しください（残り {3 - p.attempts} 回）。"),
                ephemeral=True,
            )
            return

        account_id = await _save_kyash(p.client, p.email, p.password, p.label)
        await p.client.aclose()
        p.stop()
        await interaction.followup.send(
            embed=embeds.ok(
                f"Kyashアカウント `#{account_id}` **{p.label}** を登録しました。\n"
                f"{E.INFO} 次回以降は認証コードなしで再ログインできます。"
            ),
            ephemeral=True,
        )


async def _save_kyash(client: KyashClient, email: str, password: str, label: str) -> int:
    cipher = get_cipher()
    is_kyc = None
    balance = None
    try:
        is_kyc = (await client.get_profile()).is_kyc
        balance = (await client.get_wallet()).all_balance
    except KyashError:
        pass

    async with session_scope() as s:
        acc = KyashAccount(
            label=label,
            email_enc=cipher.encrypt(email),
            password_enc=cipher.encrypt(password),
            client_uuid=client.session.client_uuid,
            installation_uuid=client.session.installation_uuid,
            access_token_enc=cipher.encrypt(client.session.access_token),
            token_obtained_at=utcnow(),
            is_kyc=is_kyc,
            last_balance=balance,
        )
        s.add(acc)
        await s.flush()
        return acc.id


# ============================================================
#  コマンド
# ============================================================

class AccountCog(commands.Cog):
    """マクドナルド／Kyash アカウントの管理"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    mcd = app_commands.Group(name="mcd", description="マクドナルドアカウント（管理者用）")
    kyash = app_commands.Group(name="kyash", description="Kyashアカウント（管理者用）")

    # -- マクドナルド -------------------------------------------

    @mcd.command(name="add", description="マクドナルドアカウントを追加します")
    @admin_only()
    async def mcd_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(McdCredModal())

    @mcd.command(name="list", description="登録済みアカウントの一覧")
    @admin_only()
    async def mcd_list(self, interaction: discord.Interaction) -> None:
        from ui import admin_flows

        await admin_flows.show_accounts(interaction)

    @mcd.command(name="card", description="決済に使うカードを選び直します")
    @app_commands.describe(account_id="アカウントID")
    @admin_only()
    async def mcd_card(self, interaction: discord.Interaction, account_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        handle = None
        try:
            handle = await mcd_accounts.open_account(account_id)
            await handle.client.ensure_auth()
            cards = await handle.client.get_cards()
        except McdError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        finally:
            if handle:
                await handle.aclose()

        if not cards:
            await interaction.followup.send(
                embed=embeds.warn("登録されているカードがありません。"), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=embeds.info("決済に使うカードを選んでください。"),
            view=CardSelectView(interaction.user.id, account_id, cards),
            ephemeral=True,
        )

    @mcd.command(name="enable", description="アカウントを再び使えるようにします")
    @admin_only()
    async def mcd_enable(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            acc.status = "ACTIVE"
            acc.consecutive_failures = 0
            label = acc.label
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を有効にしました。"), ephemeral=True
        )

    @mcd.command(name="disable", description="アカウントを一時的に使わないようにします")
    @admin_only()
    async def mcd_disable(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            acc.status = "BANNED"
            label = acc.label
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を無効にしました。"), ephemeral=True
        )

    @mcd.command(name="remove", description="アカウントを削除します")
    @admin_only()
    async def mcd_remove(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            label = acc.label
            token = await s.get(McdToken, account_id)
            if token:
                await s.delete(token)
            await s.delete(acc)
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を削除しました。"), ephemeral=True
        )

    @mcd.command(name="health", description="全アカウントの状態を確認します")
    @admin_only()
    async def mcd_health(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        results = await mcd_accounts.healthcheck_all()
        if not results:
            await interaction.followup.send(
                embed=embeds.info("登録済みのアカウントがありません。"), ephemeral=True
            )
            return
        lines = [
            f"{E.OK if alive else E.NG} `#{aid}` **{label}**"
            for aid, label, alive in results
        ]
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.KEY} アカウントの状態", description="\n".join(lines), color=embeds.BLUE
            ),
            ephemeral=True,
        )

    # -- Kyash ---------------------------------------------------

    @kyash.command(name="add", description="Kyashアカウントを追加します")
    @admin_only()
    async def kyash_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(KyashCredModal())

    @kyash.command(name="list", description="登録済みKyashアカウントの一覧")
    @admin_only()
    async def kyash_list(self, interaction: discord.Interaction) -> None:
        from ui import admin_flows

        await admin_flows.show_accounts(interaction)

    @kyash.command(name="relogin", description="Kyashへ再ログインします")
    @app_commands.describe(account_id="アカウントID")
    @admin_only()
    async def kyash_relogin(self, interaction: discord.Interaction, account_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        cipher = get_cipher()
        async with session_scope() as s:
            acc = await s.get(KyashAccount, account_id)
            if acc is None:
                await interaction.followup.send(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            email = cipher.decrypt(acc.email_enc) or ""
            password = cipher.decrypt(acc.password_enc) or ""
            label = acc.label
            client_uuid = acc.client_uuid or ""
            installation_uuid = acc.installation_uuid or ""

        if not password:
            await interaction.followup.send(
                embed=embeds.error(
                    "パスワードが保存されていないため自動で再ログインできません。"
                    "`/kyash add` で登録し直してください。"
                ),
                ephemeral=True,
            )
            return

        from services.kyash.client import KyashSession

        client = KyashClient(
            KyashSession(client_uuid=client_uuid, installation_uuid=installation_uuid)
        )
        try:
            need_otp = await client.start_login(email, password)
        except KyashError as e:
            await client.aclose()
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return

        if not need_otp:
            async with session_scope() as s:
                acc = await s.get(KyashAccount, account_id)
                acc.access_token_enc = cipher.encrypt(client.session.access_token)
                acc.token_obtained_at = utcnow()
                acc.status = "ACTIVE"
                acc.last_error = None
            await client.aclose()
            await interaction.followup.send(
                embed=embeds.ok(f"`#{account_id}` **{label}** に再ログインしました。"),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=embeds.info(
                f"{E.KEY} 認証コードを送信しました。下のボタンから入力してください。"
            ),
            view=KyashOtpView(interaction.user.id, client, email, password, label),
            ephemeral=True,
        )

    @kyash.command(name="remove", description="Kyashアカウントを削除します")
    @admin_only()
    async def kyash_remove(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(KyashAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            label = acc.label
            await s.delete(acc)
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を削除しました。"), ephemeral=True
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("account コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AccountCog(bot))
