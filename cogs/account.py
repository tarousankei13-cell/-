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

from config import JST
from discord import app_commands
from discord.ext import commands

import emoji as E
from core import audit
from core import crypto
from core import proxy as core_proxy
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
            # ⚠️ ここでもカードを見る。以前は「`/mcd card` を実行して
            #    ください」と案内するだけで、やり忘れるとそのアカウントは
            #    注文に選ばれないまま放置されていた。
            cards = []
            try:
                cards = await client.get_cards()
            except McdError as e:
                log.info("カード一覧を取得できませんでした: %s", e)
            await client.aclose()
            await interaction.edit_original_response(
                embed=embeds.info(f"{E.OK} 登録しました。"), view=None)
            await _after_mcd_added(
                interaction, account_id, label, cards,
                note="認証コードは求められませんでした。",
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

        await _after_mcd_added(interaction, account_id, p.label, cards)



class McdBulkModal(discord.ui.Modal, title="アカウントをまとめて登録"):
    """自分で作ったアカウントを、順に取り込む。

    ⚠️ **新しくアカウントを作るものではない。** すでにマクドナルドに
       登録してあるアカウントを、このBOTへ取り込むだけ。

    ⚠️ 認証コードを求められたものは、ここでは進められない。
       コードは本人が受け取るものなので、まとめ処理の中で待てない。
       その分は `/mcd add` で1件ずつ入れてもらう。
    """

    lines = discord.ui.TextInput(
        label="メールアドレスとパスワード（1行に1件）",
        style=discord.TextStyle.paragraph,
        required=True, max_length=2000,
        placeholder=("a@example.com,ぱすわーど\n"
                     "b@example.com,ぱすわーど\n"
                     "（カンマ・コロン・スペース・タブ区切り）"),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.lines.value)
        entries = _parse_bulk(raw)
        if not entries:
            await interaction.response.send_message(
                embed=embeds.error(
                    "読み取れる行がありませんでした。\n"
                    "`メールアドレス,パスワード` の形で1行に1件、"
                    "入れてください。"
                ),
                ephemeral=True,
            )
            return
        if len(entries) > 20:
            await interaction.response.send_message(
                embed=embeds.error(
                    f"一度に登録できるのは20件までです（{len(entries)}件ありました）。"
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=embeds.info(
                f"{E.LOADING} {len(entries)} 件を順に登録しています…\n"
                f"{E.INFO} 1件ずつログインするので少し時間がかかります。"
            ),
            ephemeral=True,
        )

        added, otp_needed, failed = [], [], []
        for email, password, label in entries:
            fp = Fingerprint.generate(*mcd_accounts.random_home_location())
            client = McdClient(fp)
            try:
                result = await client.login(email, password)
                if result.needs_otp:
                    # ⚠️ ここでは進めない。コードは本人が受け取るもので、
                    #    まとめ処理の中で待つことはできない。
                    otp_needed.append(email)
                    await client.aclose()
                    continue
                account_id = await _save_mcd(client, fp, email, label)
                cards = []
                try:
                    cards = await client.get_cards()
                except McdError:
                    pass
                await client.aclose()
                # カードが1枚だけなら、ここで設定してしまう
                usable = [c for c in cards if c.get("card_id")]
                card_name = ""
                if len(usable) == 1:
                    async with session_scope() as s:
                        acc = await s.get(McdAccount, account_id)
                        if acc:
                            acc.card_id = usable[0]["card_id"]
                    card_name = (usable[0].get("masked") or "")[:32]
                added.append((account_id, label, card_name, len(usable)))
            except McdError as e:
                failed.append((email, str(e)[:80]))
                await client.aclose()
            except Exception as e:
                log.exception("まとめ登録に失敗しました: %s", email)
                failed.append((email, f"{type(e).__name__}: {e}"[:80]))
                try:
                    await client.aclose()
                except Exception:
                    pass

        parts = []
        if added:
            rows = []
            for aid, label, card, n in added:
                mark = (f"カード {card or '設定済み'}" if n == 1
                        else (f"{E.WARN} カード{n}枚 → `/mcd card {aid}`" if n > 1
                              else f"{E.NG} カード無し"))
                rows.append(f"・`#{aid}` **{label}**　{mark}")
            parts.append(f"{E.OK} **登録できました（{len(added)}件）**\n"
                         + "\n".join(rows))
        if otp_needed:
            parts.append(
                f"{E.KEY} **認証コードが必要です（{len(otp_needed)}件）**\n"
                + "\n".join(f"・{_mask_mail(m)}" for m in otp_needed)
                + "\n→ `/mcd add` で1件ずつ登録してください。"
            )
        if failed:
            parts.append(
                f"{E.NG} **登録できませんでした（{len(failed)}件）**\n"
                + "\n".join(f"・{_mask_mail(m)}　{why}" for m, why in failed)
            )

        no_card = [a for a in added if a[3] == 0]
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.PEOPLE} まとめ登録の結果",
                description="\n\n".join(parts)[:4000],
                color=embeds.GREEN if added else embeds.RED,
            ),
            view=(CardScanView(interaction.user.id, [a[0] for a in no_card])
                  if no_card else None),
            ephemeral=True,
        )


def _mask_mail(email: str) -> str:
    """記録や画面にメールをそのまま出さない。"""
    name, _, domain = email.partition("@")
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}" if domain else f"{head}***"


def _parse_bulk(raw: str) -> list[tuple[str, str, str]]:
    """貼られた文字列から (メール, パスワード, 表示名) を取り出す。

    ⚠️ 区切り文字を決め打ちしない。カンマで貼る人もコロンで貼る人も
       タブで貼る人もいる。パスワードに区切り文字が入ることもあるので、
       **最初の1つだけ**で切る。
    """
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        email = password = ""
        for sep in (",", ":", "\t", " "):
            if sep in line:
                email, _, password = line.partition(sep)
                break
        email, password = email.strip(), password.strip()
        if not email or not password or "@" not in email:
            continue
        key = email.lower()
        if key in seen:      # 同じものを2回登録しない
            continue
        seen.add(key)
        out.append((email, password, email.split("@")[0][:32]))
    return out


async def _after_mcd_added(
    interaction: discord.Interaction, account_id: int, label: str,
    cards: list[dict], *, note: str = "",
) -> None:
    """アカウントを登録したあとの案内。

    ⚠️ カードが**1枚だけなら自動で設定する**。毎回 `/mcd card` を
       打たせると手間なうえ、設定し忘れたアカウントは注文に選ばれず、
       使えるアカウントが静かに減っていく。

    ⚠️ **2枚以上なら自動で決めない。** どれで決済するかはお金の話で、
       勝手に選んでよいものではない。
    """
    usable = [c for c in cards if c.get("card_id")]
    head = f"アカウント `#{account_id}` **{label}** を登録しました。"
    if note:
        head += f"\n{E.INFO} {note}"

    if len(usable) == 1:
        card = usable[0]
        name = (card.get("masked") or card.get("name") or "")[:64]
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc:
                acc.card_id = card["card_id"]
        await interaction.followup.send(
            embed=embeds.ok(
                f"{head}\n\n{E.CARD} 決済カードも自動で設定しました"
                f"{f'（{name}）' if name else ''}。\n"
                f"{E.INFO} 変えたいときは `/mcd card {account_id}`。"
            ),
            ephemeral=True,
        )
        return

    if not usable:
        await interaction.followup.send(
            embed=embeds.warn(
                f"{head}\n\n"
                f"{E.WARN} **決済カードがありません。このままでは注文に使われません。**\n\n"
                "① 公式アプリ／サイトでこのアカウントにログイン\n"
                "② カードを登録（3Dセキュアの確認があります）\n"
                "③ 下の「カードを探す」を押す"
            ),
            view=CardScanView(interaction.user.id, [account_id]),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=embeds.ok(f"{head}\n決済に使うカードを選んでください。"),
        view=CardSelectView(interaction.user.id, account_id, usable),
        ephemeral=True,
    )


class CardScanView(discord.ui.View):
    """カード未設定のアカウントを、まとめて探し直す画面。

    ⚠️ 公式アプリで登録した直後に押してもらう想定。こちらから
       カードを登録する手段は無い（公式のAPIにその呼び出しが無く、
       3-Dセキュアの本人確認を通す必要があるため）。
    """

    def __init__(self, owner_id: int, account_ids: list[int]) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.account_ids = list(account_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="カードを探す", emoji=E.SYNC,
                       style=discord.ButtonStyle.primary)
    async def scan(self, interaction: discord.Interaction,
                   btn: discord.ui.Button) -> None:
        # ⚠️ 1件ずつ通信するので時間がかかる。先に応答しておかないと
        #    「BOTは時間内に応答しませんでした」になる。
        btn.disabled = True
        await interaction.response.edit_message(
            embed=embeds.info(f"{E.LOADING} カードを探しています…"), view=self)

        done, several, none_, failed = [], [], [], []
        for aid in self.account_ids:
            try:
                ok_, count, name = await mcd_accounts.auto_pick_card(aid)
            except Exception:
                log.exception("カードの確認に失敗しました: %s", aid)
                failed.append(aid)
                continue
            if ok_:
                done.append((aid, name))
            elif count > 1:
                several.append((aid, count))
            elif count == 0:
                none_.append(aid)

        parts = []
        if done:
            parts.append(f"{E.OK} **設定しました**\n" + "\n".join(
                f"・`#{a}`　{n}" for a, n in done))
        if several:
            # ⚠️ 2枚以上あるときは選ばない。どれで決済するかはお金の話で、
            #    勝手に決めてよいものではない。
            parts.append(f"{E.WARN} **複数あるので選んでください**\n" + "\n".join(
                f"・`#{a}`　{c}枚 → `/mcd card {a}`" for a, c in several))
        if none_:
            parts.append(f"{E.NG} **まだカードがありません**\n" + "\n".join(
                f"・`#{a}`" for a in none_)
                + "\n公式アプリで登録してから、もう一度お試しください。")
        if failed:
            parts.append(f"{E.NG} **確認できませんでした**（ログインできない等）\n"
                         + "\n".join(f"・`#{a}`　→ `/mcd history {a}`" for a in failed))

        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title=f"{E.CARD} カードを探しました",
                description="\n\n".join(parts)[:4000] or "変化はありませんでした。",
                color=embeds.GREEN if done else embeds.YELLOW,
            ),
            view=None,
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

    @mcd.command(name="bulk", description="自分のアカウントをまとめて登録します")
    @admin_only()
    async def mcd_bulk(self, interaction: discord.Interaction) -> None:
        """⚠️ すでに持っているアカウントを取り込むもの。
           新しくアカウントを作る機能ではない。
        """
        await interaction.response.send_modal(McdBulkModal())

    @mcd.command(name="cards", description="全アカウントの決済カードの状況を表示します")
    @admin_only()
    async def mcd_cards(self, interaction: discord.Interaction) -> None:
        """どのアカウントにカードが付いているかを一目で見る。

        ⚠️ カードが無いアカウントは**注文に選ばれない**。気づかないと
           使えるアカウントが静かに減り、同時注文がさばけなくなる。
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await mcd_accounts.card_overview()
        if not rows:
            await interaction.followup.send(
                embed=embeds.info("登録されているアカウントがありません。"),
                ephemeral=True)
            return

        lines, missing = [], []
        for r in rows:
            if r["has_card"]:
                lines.append(f"{E.OK} `#{r['id']}` **{r['label']}**　カードあり")
            else:
                lines.append(f"{E.NG} `#{r['id']}` **{r['label']}**　"
                             f"**カード未設定（注文に使われません）**")
                missing.append(r["id"])

        e = discord.Embed(
            title=f"{E.CARD} 決済カードの状況",
            description="\n".join(lines)[:4000],
            color=embeds.YELLOW if missing else embeds.GREEN,
        )
        e.add_field(
            name="使える数",
            value=f"**{len(rows) - len(missing)}** / {len(rows)} アカウント",
            inline=True,
        )
        if missing:
            e.add_field(
                name=f"{E.WARN} カードの付け方",
                value=(
                    "① 公式アプリ／サイトでそのアカウントにログイン\n"
                    "② カードを登録（3Dセキュアの確認があります）\n"
                    "③ 下の「カードを探す」を押す\n\n"
                    f"{E.INFO} 1枚だけなら自動で設定します。"
                    "複数ある場合は選んでいただきます。"
                ),
                inline=False,
            )
        await interaction.followup.send(
            embed=e,
            view=(CardScanView(interaction.user.id, missing) if missing else None),
            ephemeral=True,
        )

    @mcd.command(name="history", description="アカウントに起きたことの履歴を表示します")
    @app_commands.describe(account_id="アカウントID", count="表示する件数")
    @admin_only()
    async def mcd_history(
        self, interaction: discord.Interaction, account_id: int,
        count: app_commands.Range[int, 1, 30] = 10,
    ) -> None:
        """なぜ止まったのかを後から調べるための窓口。

        ⚠️ `last_error` は最後の1件しか残らない。止まった理由は
           止まった**後**に調べるものなので、そのときには上書き
           されている。だから経緯を別に残してある。
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        from db.models import McdAccount

        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            label = acc.label if acc else ""
            status = acc.status if acc else ""
        if not label:
            await interaction.followup.send(
                embed=embeds.error("そのアカウントが見つかりません。"), ephemeral=True)
            return

        events = await mcd_accounts.account_events(account_id, limit=count)
        if not events:
            await interaction.followup.send(
                embed=embeds.info(
                    f"**{label}** には、まだ記録がありません。\n"
                    f"{E.INFO} 失敗・隔離・復帰が起きると、ここに残ります。"
                ),
                ephemeral=True,
            )
            return

        MARK = {
            "failure": E.WARN, "degrade": E.YELLOW,
            "quarantine": E.NG, "recover": E.OK,
        }
        lines = []
        for e in events:
            when = e.created_at.astimezone(JST)
            kind = f"`{e.kind}`" if e.kind else ""
            head = f"{MARK.get(e.action, E.INFO)} **{when:%m/%d %H:%M}**　{kind}"
            body = (e.message or "").strip().replace("\n", " ")[:90]
            lines.append(head + (f"\n　{body}" if body else ""))

        # ⚠️ 何が多いかを数えて先に見せる。1件ずつ読ませると、
        #    「たまたま1回」と「ずっと同じ理由」の区別が付かない。
        from collections import Counter
        kinds = Counter(e.kind or "不明" for e in events if e.action != "recover")

        e_ = discord.Embed(
            title=f"{E.HISTORY} {label} の履歴",
            description="\n".join(lines)[:4000],
            color=embeds.BLUE,
        )
        e_.add_field(name="いまの状態", value=f"`{status}`", inline=True)
        if kinds:
            e_.add_field(
                name="理由の内訳",
                value="　".join(f"{k} **{n}**" for k, n in kinds.most_common(5)),
                inline=True,
            )
        e_.set_footer(
            text="原因が分からないときは、生の応答が残っています（管理者にご相談ください）"
        )
        await interaction.followup.send(embed=e_, ephemeral=True)

    @mcd.command(name="enable", description="アカウントを再び使えるようにします")
    @app_commands.describe(account_id="対象のアカウントID（/mcd list で確認）")
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
    @app_commands.describe(account_id="対象のアカウントID（/mcd list で確認）")
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
    @app_commands.describe(account_id="削除するアカウントID（/mcd list で確認）")
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
        self, interaction: discord.Interaction,
        label: app_commands.Range[str, 1, 50],
        phone: app_commands.Range[str, 1, 20],
        password: app_commands.Range[str, 1, 200],
        proxy: app_commands.Range[str, 1, 300] | None = None,
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

        # ⚠️ プロキシの形を**使う前に**確かめる。httpx は知らない形を渡されると
        #    ValueError を投げるので、そのままだと生のエラーで落ちる。
        #    `/config proxy set` では確かめているのに、ここだけ素通りだった。
        if proxy:
            why = core_proxy.problem(proxy.strip())
            if why:
                await interaction.followup.send(
                    embed=embeds.error(f"プロキシの指定が正しくありません。\n\n{why}"),
                    ephemeral=True,
                )
                return

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
        self, interaction: discord.Interaction, account_id: int,
        url: app_commands.Range[str, 1, 1000],
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
    @app_commands.describe(mode="利用者が使えるチャージ方法")
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
        self, interaction: discord.Interaction,
        url: app_commands.Range[str, 0, 300] = "",
    ) -> None:
        """
        ⚠️ 口座ごとに指定があれば、そちらが優先される。
           ここは「指定が無い口座の既定」。
        """
        from core import proxy as proxy_mod
        from core import settings

        # ⚠️ ここでも形を確かめる。確かめないと、使えない値が入ったまま
        #    「設定できた」と見えてしまい、実際には素のIPで出ていく。
        why = proxy_mod.problem(url.strip())
        if why:
            await interaction.response.send_message(
                embed=embeds.error(f"この値では設定できません。\n{why}"),
                ephemeral=True,
            )
            return

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
            # ⚠️ 鍵が合わないと復号で落ちる。生の例外を利用者に見せない。
            #    バックアップを鍵なしで戻すと、全アカウントがこうなる。
            if crypto.is_unreadable(acc.email_enc) or crypto.is_unreadable(
                acc.password_enc
            ):
                await interaction.followup.send(
                    embed=embeds.error(
                        "このアカウントの登録内容を、いまの鍵では読み取れませんでした。\n\n"
                        f"{E.WARN} `data/encryption_key.txt` が、登録したときと"
                        "違うものになっています。バックアップを鍵なしで戻した場合や、"
                        "`ENCRYPTION_KEY` を変えた場合に起きます。\n\n"
                        "元の鍵を戻すか、登録し直してください。"
                    ),
                    ephemeral=True,
                )
                return
            email = crypto.try_decrypt(acc.email_enc) or ""
            password = crypto.try_decrypt(acc.password_enc) or ""
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
    @app_commands.describe(account_id="削除するアカウントID（/kyash list で確認）")
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
