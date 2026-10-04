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
from core import audit
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

async def _save_mcd(client, fp, email: str, label: str) -> int:
    """
    ログイン済みのマクドナルドアカウントを保存して、IDを返す。

    ⚠️ 認証コードを求められた場合と求められなかった場合の**両方**が
       ここを通る。別々に書くと、片方だけ列が増えて食い違う。
    """
    refresh = client.tokens.refresh_token
    if not refresh:
        raise RuntimeError("リフレッシュトークンがありません")

    cipher = get_cipher()
    async with session_scope() as s:
        acc = McdAccount(
            label=label,
            email_enc=cipher.encrypt(email),
            refresh_token_enc=cipher.encrypt(refresh),
            device_uid=fp.device_uid,
            wmop_device_id=fp.wmop_device_id,
            fb_instance_id=fp.fb_instance_id,
            home_lat=fp.latitude, home_lng=fp.longitude,
        )
        s.add(acc)
        await s.flush()
        return acc.id


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
            result = await client.login(email, password)
        except McdError as e:
            await client.aclose()
            await interaction.edit_original_response(embed=embeds.error(str(e)))
            return

        # 認証コードを求められなかったときは、ここで終わり。
        # 届かないコードの入力画面を出しても進めない。
        if not result.needs_otp:
            try:
                account_id = await _save_mcd(client, fp, email, label)
            except Exception:
                log.exception("マクドナルドアカウントの保存に失敗しました")
                await client.aclose()
                await interaction.edit_original_response(
                    embed=embeds.error("アカウントの保存に失敗しました。")
                )
                return
            await client.aclose()
            await interaction.edit_original_response(
                embed=embeds.ok(
                    f"マクドナルドアカウント `#{account_id}` **{label}** を登録しました。\n"
                    f"{E.INFO} 認証コードは求められませんでした。\n"
                    f"続けて `/mcd card {account_id}` で決済カードを選んでください。"
                ),
                view=None,
            )
            return

        await interaction.edit_original_response(
            embed=embeds.info(
                f"{E.KEY} 認証コードを送信しました。\n"
                "SMS またはメールに届いた **6桁** を、下のボタンから入力してください。"
            ),
            view=McdOtpView(
                interaction.user.id, client, fp, result.mfa_token, email, label
            ),
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
            await p.client.login_with_mfa(p.mfa_token, str(self.otp.value).strip())
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

        account_id = await _save_mcd(p.client, p.fp, p.email, p.label)

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

# ⚠️ PayPay の登録はSMSのURLを挟むので、途中の状態を持っておく必要がある。
#    BOTを再起動すると消えるが、やり直せばよいだけなので保存はしない
#    （パスワードをディスクに残さないほうが安全）。
_PENDING_PAYPAY: dict[int, object] = {}


class AccountCog(commands.Cog):
    """マクドナルド／Kyash アカウントの管理"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    mcd = app_commands.Group(name="mcd", description="マクドナルドアカウント（管理者用）")
    kyash = app_commands.Group(name="kyash", description="Kyashアカウント（管理者用）")
    paypay = app_commands.Group(name="paypay", description="PayPayアカウント（管理者用）")

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
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.remove", target=f"mcd:{account_id}", before=label,
        )
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

    # -- PayPay --------------------------------------------------

    @paypay.command(name="add", description="PayPayアカウントを追加します")
    @app_commands.describe(
        label="見分けるための名前", phone="電話番号", password="パスワード",
        proxy="このアカウント専用のプロキシ（省略可）",
    )
    @admin_only()
    async def paypay_add(
        self, interaction: discord.Interaction, label: str, phone: str,
        password: str, proxy: str | None = None,
    ) -> None:
        """
        ⚠️ ログインは2段階。ここを実行すると SMS で **URL** が届くので、
           `/paypay confirm` にそのURLを貼ること（数字のOTPではない）。
        """
        from core.crypto import get_cipher
        from db.models import PayPayAccount
        from services.paypay import accounts as pp_accounts
        from services.paypay.client import PayPayClient, PayPayError

        await interaction.response.defer(ephemeral=True, thinking=True)
        cipher = get_cipher()
        client = PayPayClient(proxy=proxy or pp_accounts.config_proxy())
        try:
            started = await client.start_login(phone, password)
        except PayPayError as e:
            await client.aclose()
            await interaction.followup.send(
                embed=embeds.error(f"ログインを始められませんでした。\n{e}"),
                ephemeral=True,
            )
            return

        async with session_scope() as s:
            acc = PayPayAccount(
                label=label,
                phone_enc=cipher.encrypt(phone),
                password_enc=cipher.encrypt(password),
                device_uuid=client.session.device_uuid,
                client_uuid=client.session.client_uuid,
                proxy_url=proxy,
                status=pp_accounts.STATUS_DEGRADED,   # 確認が済むまでは使わせない
            )
            s.add(acc)
            await s.flush()
            account_id = acc.id

        # 端末が登録済みなら、SMSなしでそのまま入れている
        if started.get("done"):
            await pp_accounts.save_session(account_id, started["session"])
            note = ""
            try:
                bal = await client.get_balance()
                note = f"\n{E.WALLET} いまの残高 **{embeds.yen(bal.all_balance)}**"
            except PayPayError:
                pass
            finally:
                await client.aclose()
            await interaction.followup.send(
                embed=embeds.ok(
                    f"**#{account_id} {label}** を登録し、ログインまで完了しました。"
                    f"{note}\n\n"
                    f"{E.INFO} SMSの確認は不要でした。\n"
                    f"{E.INFO} トークンは約90日もちます。"
                ),
                ephemeral=True,
            )
            return

        # ⚠️ 確認の手順で同じ検証子（PKCE）と Cookie が要る。
        #    クライアントごと預かっておく（作り直すと通らない）。
        _PENDING_PAYPAY[account_id] = client

        await interaction.followup.send(
            embed=embeds.ok(
                f"**#{account_id} {label}** を登録しました。\n\n"
                f"{E.INFO} SMS に **URL** が届きます（数字ではありません）。\n"
                f"`/paypay confirm account_id:{account_id} url:<届いたURL>` "
                "を実行してください。\n\n"
                f"{E.WARN} ログインに3回失敗するとアカウントが一時ロックされます。"
            ),
            ephemeral=True,
        )

    @paypay.command(name="confirm", description="SMSで届いたURLを貼って確定します")
    @app_commands.describe(account_id="アカウントID", url="SMSで届いたURL")
    @admin_only()
    async def paypay_confirm(
        self, interaction: discord.Interaction, account_id: int, url: str
    ) -> None:
        from services.paypay import accounts as pp_accounts
        from services.paypay.client import PayPayError

        await interaction.response.defer(ephemeral=True, thinking=True)
        client = _PENDING_PAYPAY.get(account_id)
        if client is None:
            await interaction.followup.send(
                embed=embeds.error(
                    "この登録の続きが見つかりませんでした。\n"
                    "`/paypay add` からやり直してください。\n"
                    f"{E.INFO} BOTを再起動すると、やりかけの登録は消えます。"
                ),
                ephemeral=True,
            )
            return
        try:
            session = await client.login_confirm(url)
        except PayPayError as e:
            await interaction.followup.send(
                embed=embeds.error(f"確認できませんでした。\n{e}"), ephemeral=True
            )
            return
        finally:
            pass

        await pp_accounts.save_session(account_id, session)
        _PENDING_PAYPAY.pop(account_id, None)
        try:
            balance = await client.get_balance()
            note = f"\n{E.WALLET} いまの残高 **{embeds.yen(balance.all_balance)}**"
        except PayPayError:
            note = ""
        finally:
            await client.aclose()

        await interaction.followup.send(
            embed=embeds.ok(
                f"**#{account_id}** のログインが完了しました。{note}\n\n"
                f"{E.INFO} トークンは約90日もちます。"
                "期限が近づくと管理者へお知らせします。"
            ),
            ephemeral=True,
        )

    @paypay.command(name="methods", description="チャージに使える決済を選びます")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Kyash と PayPay の両方（推奨）", value="both"),
        app_commands.Choice(name="Kyash のみ", value="kyash"),
        app_commands.Choice(name="PayPay のみ", value="paypay"),
    ])
    @admin_only()
    async def paypay_methods(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        from core import settings

        await settings.set_value(
            "charge_methods", mode.value, updated_by=interaction.user.id
        )
        body = f"チャージに使える決済を **{mode.name}** にしました。\n"
        if mode.value in ("both", "paypay"):
            body += (
                f"\n{E.WARN} PayPay は **日本からしかアクセスできません**。"
                "国外で動かす場合は `/paypay proxy` でプロキシをご指定ください。\n"
                f"{E.INFO} `/paypay add` で口座を登録してください。"
            )
        body += f"\n{E.INFO} チャージパネルの文面も変わります（`/panel refresh`）。"
        await interaction.response.send_message(embed=embeds.ok(body), ephemeral=True)

    @paypay.command(name="proxy", description="PayPay用のプロキシ（全口座の既定）")
    @app_commands.describe(url="http://user:pass@host:port 形式。空で解除")
    @admin_only()
    async def paypay_proxy(
        self, interaction: discord.Interaction, url: str = ""
    ) -> None:
        """
        ⚠️ 口座ごとに指定があれば、そちらが優先される。
           ここは「指定が無い口座の既定」。
        """
        from core import settings

        await settings.set_value(
            "paypay_proxy", url.strip(), updated_by=interaction.user.id
        )
        await interaction.response.send_message(
            embed=embeds.ok(
                "PayPay用のプロキシを設定しました。\n"
                f"{E.INFO} 口座ごとに指定がある場合は、そちらが優先されます。"
                if url.strip() else "PayPay用のプロキシを解除しました。"
            ),
            ephemeral=True,
        )

    @paypay.command(name="list", description="登録済みPayPayアカウントの一覧")
    @admin_only()
    async def paypay_list(self, interaction: discord.Interaction) -> None:
        from sqlalchemy import select
        from db.models import PayPayAccount
        from services.paypay import accounts as pp_accounts

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            rows = (await s.execute(select(PayPayAccount))).scalars().all()
            items = [
                (a.id, a.label, a.status, pp_accounts.token_days_left(a),
                 a.last_balance, a.proxy_url, a.last_error)
                for a in rows
            ]
        if not items:
            await interaction.followup.send(
                embed=embeds.info(
                    "まだ登録がありません。`/paypay add` から追加してください。"
                ),
                ephemeral=True,
            )
            return
        lines = []
        for aid, label, status, left, bal, proxy, err in items:
            mark = {"ACTIVE": E.GREEN, "DEGRADED": E.YELLOW}.get(status, E.RED)
            days = f"残り{int(left)}日" if left is not None else "未ログイン"
            lines.append(
                f"{mark} `#{aid}` **{label}**　{days}"
                + (f"　残高 {embeds.yen(int(bal))}" if bal is not None else "")
                + (f"　{E.INFO}プロキシ有" if proxy else "")
                + (f"\n　　{E.WARN} {err[:60]}" if err else "")
            )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.YEN} PayPayアカウント",
                description="\n".join(lines), color=embeds.BLUE,
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
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.remove", target=f"kyash:{account_id}", before=label,
        )
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
