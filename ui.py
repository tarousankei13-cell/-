"""Discord UI (Embed / Persistent View / Modal)。

デザイン方針: 黒・ダーク・シンプル・高級感。スマートフォンでの視認性を最優先とし、
紫一色にはしない。利用者の個人情報・処理状況は原則 Ephemeral で表示する。

チャージパネルとランキングパネルは完全に独立したパネルとして実装する
(チャージパネルにランキング機能は入れない)。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Sequence

import discord

import config
import utils

if TYPE_CHECKING:  # 実行時の循環 import を避ける
    from database import GuildSettings

logger = logging.getLogger(config.LOGGER_BOT)

SEPARATOR = "───────────────────────"


# ---------------------------------------------------------------------------
# Embed ビルダー
# ---------------------------------------------------------------------------

def charge_panel_embed(settings: "GuildSettings", *, kyash_ready: bool) -> discord.Embed:
    """常設チャージパネルの Embed。"""
    if settings.emergency_stop:
        state = "🔴 緊急停止中"
    elif settings.maintenance:
        state = "🟠 メンテナンス中"
    elif not kyash_ready:
        state = "🟠 一時停止中"
    else:
        state = "🟢 通常受付中"

    embed = discord.Embed(
        title="💰 チャージシステム",
        description=(
            f"{SEPARATOR}\n"
            "下のボタンからチャージを開始できます。\n"
            "金額を入力したあと、Kyashの**送金リンク**を送信してください。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.BASE,
    )
    embed.add_field(name="現在のチャージ率", value=f"**{utils.fmt_rate(settings.charge_rate)}**", inline=True)
    embed.add_field(name="最低チャージ額", value=f"**{utils.fmt_yen(settings.minimum_charge)}**", inline=True)
    embed.add_field(name="最大チャージ額", value=f"**{utils.fmt_yen(settings.maximum_charge)}**", inline=True)
    embed.add_field(name="現在の状態", value=state, inline=False)
    embed.set_footer(text="サーバー内部残高システム / 現金化・出金には対応していません")
    return embed


def ranking_embed(
    guild: discord.Guild | None,
    entries: Sequence[tuple[int, int, str]],
    settings: "GuildSettings",
    *,
    updated_at: int,
) -> discord.Embed:
    """ランキングパネルの Embed。

    Args:
        entries: ``(user_id, balance, display)`` の並び (既に順位順)。
    """
    lines: list[str] = [SEPARATOR]
    if not entries:
        lines.append("まだランキングデータがありません。")
    else:
        for index, (user_id, balance, display) in enumerate(entries, start=1):
            medal = config.RANK_MEDALS.get(index, f"**{index}位**")
            lines.append(f"{medal}　{display}")
            lines.append(f"　　`{utils.fmt_int(balance)}`")
    lines.append(SEPARATOR)

    embed = discord.Embed(
        title="🏆 SERVER BALANCE RANKING",
        description="\n".join(lines)[:4000],
        color=config.Color.RANKING,
    )
    embed.set_footer(
        text=f"現在の内部残高 TOP {settings.ranking_limit} / 最終更新 {utils.format_jst(updated_at)}"
    )
    if guild is not None and guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def ranking_disabled_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🏆 SERVER BALANCE RANKING",
        description=f"{SEPARATOR}\nこのサーバーではランキング表示が無効になっています。\n{SEPARATOR}",
        color=config.Color.NEUTRAL,
    )
    return embed


def balance_embed(user: discord.abc.User, balance: int, summary: dict[str, int]) -> discord.Embed:
    """残高確認 (Ephemeral)。"""
    embed = discord.Embed(
        title="💳 あなたの残高",
        description=SEPARATOR,
        color=config.Color.INFO,
    )
    embed.add_field(name="現在残高", value=f"**{utils.fmt_int(balance)}**", inline=False)
    embed.add_field(name="累計チャージ", value=f"{utils.fmt_int(summary['count'])}回", inline=True)
    embed.add_field(name="累計送金額", value=utils.fmt_yen(summary["sent"]), inline=True)
    embed.add_field(name="累計獲得", value=utils.fmt_int(summary["credited"]), inline=True)
    embed.set_footer(text=f"{user.display_name} / このサーバー内の残高です")
    return embed


def history_embed(
    rows: Sequence[Any], *, page: int, total_pages: int, total: int
) -> discord.Embed:
    """チャージ履歴 (Ephemeral / ページング)。"""
    embed = discord.Embed(
        title="📜 チャージ履歴",
        description=f"{SEPARATOR}\n全 {utils.fmt_int(total)} 件" if total else f"{SEPARATOR}\n履歴はまだありません。",
        color=config.Color.NEUTRAL,
    )
    for row in rows:
        status = row["status"]
        emoji = config.STATUS_EMOJI.get(status, "⚪")
        label = config.STATUS_LABELS.get(status, status)
        received = row["received_amount"] if row["received_amount"] is not None else row["requested_amount"]
        credited = row["credited_amount"]
        value_lines = [
            f"送金額: {utils.fmt_yen(received)}",
            f"チャージ率: {utils.fmt_rate(row['charge_rate'])}",
            f"獲得残高: {utils.fmt_int(credited) if credited is not None else '-'}",
            f"状態: {emoji} {label}",
            f"ID: `{row['id']}`",
        ]
        embed.add_field(
            name=utils.format_jst(row["created_at"]),
            value="\n".join(value_lines),
            inline=False,
        )
    embed.set_footer(text=f"ページ {page}/{max(1, total_pages)}")
    return embed


def help_embed(settings: "GuildSettings") -> discord.Embed:
    """ヘルプ (Ephemeral)。"""
    embed = discord.Embed(
        title="❓ ヘルプ",
        description=(
            f"{SEPARATOR}\n"
            "このBotは、このサーバー内だけで使える**内部残高**を管理します。\n"
            "現金や外部サービスとの交換・出金には対応していません。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    embed.add_field(
        name="チャージのしかた",
        value=(
            "1. `💰 チャージ` を押す\n"
            "2. チャージしたい金額を入力する\n"
            "3. Kyashアプリで**同じ金額**の送金リンクを作る\n"
            "4. `🔗 送金リンクを送信` を押してリンクを貼る\n"
            "5. 自動で受け取り、残高が加算されます"
        ),
        inline=False,
    )
    embed.add_field(name="チャージ率", value=utils.fmt_rate(settings.charge_rate), inline=True)
    embed.add_field(name="最低 / 最大", value=f"{utils.fmt_yen(settings.minimum_charge)} / {utils.fmt_yen(settings.maximum_charge)}", inline=True)
    embed.add_field(name="1日の上限", value=utils.fmt_yen(settings.daily_limit), inline=True)
    embed.add_field(
        name="注意事項",
        value=(
            "・入力した金額と送金リンクの金額が**一致**していないと受け取れません\n"
            "・請求リンクではなく**送金リンク**を作成してください\n"
            "・リンクの入力期限は約15分です\n"
            "・処理の目安は通常10〜60秒程度です (混雑時は順番待ちになります)"
        ),
        inline=False,
    )
    embed.add_field(
        name="うまくいかないとき",
        value="結果はDMでお知らせします。解決しない場合はサーバーの管理者へお問い合わせください。",
        inline=False,
    )
    embed.set_footer(text="送金リンクは公開チャンネルに貼らないでください")
    return embed


def achievement_embed(
    *,
    user_mention: str,
    received_amount: int,
    charge_rate: Decimal | str,
    credited_amount: int | None,
    status: str,
    tx_id: str,
    timestamp: int,
    proxy: bool = False,
) -> discord.Embed:
    """実績チャンネルへ投稿する Embed。

    代理実績でも利用者から見た見た目は通常実績と同一にする
    (DB 上の ``source`` で区別する)。
    """
    if status == config.TxStatus.COMPLETED:
        state_text, color = "🟢 チャージ完了", config.Color.SUCCESS
    elif status in (config.TxStatus.FAILED, config.TxStatus.CANCELLED, config.TxStatus.EXPIRED):
        state_text, color = "🔴 チャージ失敗", config.Color.DANGER
    elif status == config.TxStatus.MANUAL_REVIEW:
        state_text, color = "🟠 確認中", config.Color.WARNING
    else:
        state_text, color = "🟡 チャージ処理中", config.Color.WARNING

    embed = discord.Embed(title="🏆 チャージ実績", description=SEPARATOR, color=color)
    embed.add_field(name="ユーザー", value=user_mention, inline=False)
    embed.add_field(name="送金額", value=utils.fmt_yen(received_amount), inline=True)
    embed.add_field(name="チャージ率", value=utils.fmt_rate(charge_rate), inline=True)
    embed.add_field(
        name="獲得残高",
        value=f"**{utils.fmt_int(credited_amount)}**" if credited_amount is not None else "-",
        inline=True,
    )
    embed.add_field(name="状態", value=state_text, inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.set_footer(text="内部残高システム")
    return embed


def dm_success_embed(
    *,
    guild_name: str,
    received_amount: int,
    charge_rate: Decimal | str,
    credited_amount: int,
    balance_after: int,
    tx_id: str,
    timestamp: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ チャージ完了",
        description=f"{SEPARATOR}\n**{guild_name}** でのチャージが完了しました。\n{SEPARATOR}",
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="送金額", value=utils.fmt_yen(received_amount), inline=True)
    embed.add_field(name="チャージ率", value=utils.fmt_rate(charge_rate), inline=True)
    embed.add_field(name="獲得残高", value=f"**{utils.fmt_int(credited_amount)}**", inline=True)
    embed.add_field(name="チャージ後残高", value=f"**{utils.fmt_int(balance_after)}**", inline=False)
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    return embed


def dm_failure_embed(
    *, guild_name: str, tx_id: str, error_code: str, timestamp: int, requested_amount: int | None = None
) -> discord.Embed:
    """失敗通知 (利用者には安全な一般向けメッセージのみ表示する)。"""
    message = config.USER_ERROR_MESSAGES.get(error_code, config.USER_ERROR_MESSAGES[config.ErrorCode.UNKNOWN_ERROR])
    embed = discord.Embed(
        title="⚠️ チャージを完了できませんでした",
        description=f"{SEPARATOR}\n**{guild_name}**\n{message}\n{SEPARATOR}",
        color=config.Color.DANGER,
    )
    if requested_amount is not None:
        embed.add_field(name="申請額", value=utils.fmt_yen(requested_amount), inline=True)
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    embed.set_footer(text="送金リンクが未使用の場合は、Kyashアプリからキャンセルできます")
    return embed


def dm_review_embed(*, guild_name: str, tx_id: str, timestamp: int) -> discord.Embed:
    embed = discord.Embed(
        title="🔎 処理結果を確認しています",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            "現在、受け取り結果の確認を行っています。\n"
            "確認が完了ししだい結果をお知らせしますので、そのままお待ちください。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    return embed


def log_embed(
    title: str, description: str = "", *, color: int = config.Color.NEUTRAL,
    fields: Sequence[tuple[str, str, bool]] = (),
) -> discord.Embed:
    """ログチャンネル向け Embed (秘密情報は渡さないこと)。"""
    embed = discord.Embed(
        title=title, description=utils.truncate(description, 3800), color=color
    )
    for name, value, inline in fields:
        embed.add_field(name=utils.truncate(name, 250), value=utils.truncate(str(value), 1000), inline=inline)
    embed.timestamp = discord.utils.utcnow()
    return embed


def error_embed(error_code: str, *, admin_detail: str | None = None) -> discord.Embed:
    """利用者向けエラー表示 (内部詳細は含めない)。"""
    embed = discord.Embed(
        title="⚠️ 実行できませんでした",
        description=config.USER_ERROR_MESSAGES.get(
            error_code, config.USER_ERROR_MESSAGES[config.ErrorCode.UNKNOWN_ERROR]
        ),
        color=config.Color.DANGER,
    )
    if admin_detail:
        embed.add_field(name="詳細 (管理者向け)", value=utils.truncate(admin_detail, 900), inline=False)
    return embed


def info_embed(title: str, description: str, *, color: int = config.Color.INFO) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def success_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=config.Color.SUCCESS)


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------

async def safe_respond(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    """応答済みかどうかに応じて response / followup を使い分ける。"""
    kwargs: dict[str, Any] = {}
    if embed is not None:
        kwargs["embed"] = embed
    if content is not None:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            await interaction.followup.send(ephemeral=ephemeral, **kwargs)
        else:
            await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
    except discord.HTTPException as exc:
        logger.warning("Interaction への応答に失敗しました: %s", utils.safe_error_text(exc))


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------

class AmountModal(discord.ui.Modal, title="チャージ金額の入力"):
    """チャージ金額を入力する Modal。"""

    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="チャージ金額 (円)",
        placeholder="例: 1000",
        required=True,
        min_length=1,
        max_length=config.AMOUNT_INPUT_MAX_LEN,
    )

    def __init__(self, settings: "GuildSettings") -> None:
        super().__init__(timeout=300)
        self.amount.placeholder = (
            f"{settings.minimum_charge}〜{settings.maximum_charge} の範囲で入力"
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.client.handle_amount_submit(interaction, str(self.amount.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("金額入力Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class LinkModal(discord.ui.Modal, title="送金リンクの送信"):
    """Kyash 送金リンクを入力する Modal (公開チャンネルへ平文を出さない)。"""

    link: discord.ui.TextInput = discord.ui.TextInput(
        label="Kyash 送金リンク",
        placeholder="https://kyash.me/payments/xxxxxxxx",
        required=True,
        min_length=3,
        max_length=300,
    )

    def __init__(self, tx_id: str) -> None:
        super().__init__(timeout=config.LINK_WAIT_SECONDS)
        self._tx_id = tx_id

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        raw = str(self.link.value)
        # 入力値はできるだけ早く破棄する
        self.link.default = None
        await interaction.client.handle_link_submit(interaction, self._tx_id, raw)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("リンク入力Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class KyashLoginModal(discord.ui.Modal, title="受取用Kyashアカウントのログイン"):
    """管理者のみが使用する Kyash ログイン Modal (Ephemeral 応答)。"""

    email: discord.ui.TextInput = discord.ui.TextInput(
        label="メールアドレス", placeholder="Kyash に登録したメールアドレス", required=True, max_length=200
    )
    password: discord.ui.TextInput = discord.ui.TextInput(
        label="パスワード", placeholder="保存されません", required=True, max_length=200
    )

    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        email = str(self.email.value).strip()
        password = str(self.password.value)
        self.password.default = None
        await interaction.client.handle_kyash_login(interaction, email, password)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("Kyashログの入力でエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class KyashOtpModal(discord.ui.Modal, title="SMS認証コードの入力"):
    """OTP 入力 Modal。OTP はログにも DB にも保存しない。"""

    otp: discord.ui.TextInput = discord.ui.TextInput(
        label="認証コード (6桁)", placeholder="SMSに届いた番号", required=True, min_length=4, max_length=10
    )

    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        code = str(self.otp.value).strip()
        self.otp.default = None
        await interaction.client.handle_kyash_otp(interaction, code)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("OTP入力でエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

class ChargePanelView(discord.ui.View):
    """常設チャージパネル (Persistent View)。

    ランキング機能はこのパネルには含めない (ランキングは専用パネル)。
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="チャージ", emoji="💰", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.CHARGE_START, row=0,
    )
    async def charge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_charge_button(interaction)

    @discord.ui.button(
        label="残高", emoji="💳", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_BALANCE, row=0,
    )
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_balance_button(interaction)

    @discord.ui.button(
        label="履歴", emoji="📜", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_HISTORY, row=1,
    )
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_history_button(interaction)

    @discord.ui.button(
        label="ヘルプ", emoji="❓", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_HELP, row=1,
    )
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_help_button(interaction)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_REFRESH, row=1,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_panel_refresh_button(interaction)


class RankingPanelView(discord.ui.View):
    """ランキングパネル (Persistent View)。チャージパネルとは完全に独立。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.RANKING_REFRESH, row=0,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_ranking_refresh_button(interaction)

    @discord.ui.button(
        label="自分の順位", emoji="📜", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.RANKING_MYRANK, row=0,
    )
    async def my_rank(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_my_rank_button(interaction)


class LinkSubmitView(discord.ui.View):
    """金額確定後に送金リンクを受け付ける Ephemeral View。"""

    def __init__(self, tx_id: str, *, owner_id: int, timeout: float) -> None:
        super().__init__(timeout=max(30.0, timeout))
        self._tx_id = tx_id
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="送金リンクを送信", emoji="🔗", style=discord.ButtonStyle.primary)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(LinkModal(self._tx_id))

    @discord.ui.button(label="キャンセル", emoji="✖️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_charge_cancel(interaction, self._tx_id)
        self.stop()


class HistoryView(discord.ui.View):
    """履歴のページング (Ephemeral)。"""

    PAGE_SIZE = 5

    def __init__(self, *, owner_id: int, guild_id: int, page: int = 1) -> None:
        super().__init__(timeout=300)
        self._owner_id = owner_id
        self.guild_id = guild_id
        self.page = page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(1, self.page - 1)
        await interaction.client.render_history(interaction, self, edit=True)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        await interaction.client.render_history(interaction, self, edit=True)


class ConfirmView(discord.ui.View):
    """危険操作の確認 (2段階確認にも対応)。"""

    def __init__(
        self,
        *,
        owner_id: int,
        confirm_label: str = "実行する",
        danger: bool = True,
        stages: int = 1,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.value: bool | None = None
        self._owner_id = owner_id
        self._stages = max(1, stages)
        self._stage = 0
        self._confirm_label = confirm_label
        self.confirm.label = confirm_label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._stage += 1
        if self._stage < self._stages:
            remaining = self._stages - self._stage
            button.label = f"本当に実行する (残り{remaining}回)"
            await interaction.response.edit_message(
                embed=info_embed(
                    "⚠️ 最終確認",
                    "この操作は取り消せません。実行する場合はもう一度ボタンを押してください。",
                    color=config.Color.DANGER,
                ),
                view=self,
            )
            return
        self.value = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=info_embed("⏳ 実行中", "処理を実行しています…", color=config.Color.WARNING),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=info_embed("キャンセルしました", "操作は実行されていません。", color=config.Color.NEUTRAL),
            view=self,
        )
        self.stop()


class KyashLoginStartView(discord.ui.View):
    """OTP 入力を促す Ephemeral View。"""

    def __init__(self, *, owner_id: int) -> None:
        super().__init__(timeout=config.KYASH_LOGIN_PENDING_TTL)
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="認証コードを入力", emoji="🔑", style=discord.ButtonStyle.primary)
    async def enter_otp(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(KyashOtpModal())
