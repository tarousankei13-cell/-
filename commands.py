"""スラッシュコマンド定義。

権限モデル
----------
* Bot Owner   : Bot 全体の管理 (サーバー許可 / Kyash アカウント / システム / DB)
* Server Admin: サーバー内の管理操作 (設定 / 残高操作 / 凍結 / パネル / 統計)
* 一般利用者  : パネル操作のみ (コマンドは使用しない)

すべての管理コマンドは Bot 内部で毎回権限を確認し、Discord の Administrator
権限だけに依存しない。危険操作には確認ボタンを付ける。
"""
from __future__ import annotations

import logging
import platform
from datetime import datetime
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import discord
from discord import app_commands

import chart
import config
import kyash_service
import price_service
import ui
import utils
from charge_service import ChargeError
from database import DatabaseError, RequestError

if TYPE_CHECKING:
    from main import ChargeBot

logger = logging.getLogger(config.LOGGER_BOT)
audit_logger = logging.getLogger(config.LOGGER_AUDIT)


# ---------------------------------------------------------------------------
# 権限チェック
# ---------------------------------------------------------------------------
class PermissionDenied(app_commands.CheckFailure):
    """権限不足 (利用者向けメッセージ付き)。"""

    def __init__(self, message: str = "この操作を行う権限がありません。") -> None:
        super().__init__(message)
        self.message = message


class GuildNotAllowed(app_commands.CheckFailure):
    """未許可サーバーでの実行。"""

    def __init__(self) -> None:
        super().__init__("このサーバーではBotの利用が許可されていません。")


def require_owner():
    """Bot Owner 専用。"""

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        if not bot.is_bot_owner(interaction.user):
            raise PermissionDenied("このコマンドは Bot Owner のみ実行できます。")
        return True

    return app_commands.check(predicate)


def require_admin(*, allow_unallowed_guild: bool = False):
    """Server Admin (または Bot Owner) 専用。"""

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        if interaction.guild is None:
            raise PermissionDenied("このコマンドはサーバー内でのみ実行できます。")
        if not allow_unallowed_guild and not bot.is_bot_owner(interaction.user):
            if not await bot.db.is_guild_allowed(interaction.guild.id):
                raise GuildNotAllowed()
        if not await bot.is_server_admin(interaction):
            raise PermissionDenied(
                "このコマンドはサーバー管理者のみ実行できます。\n"
                "管理者ロールは `/settings admin_role` で設定します。"
            )
        return True

    return app_commands.check(predicate)


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------
async def _audit(
    interaction: discord.Interaction,
    action: str,
    *,
    target_user_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """監査ログへ記録する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    operation_id = await bot.db.add_audit_log(
        actor_id=interaction.user.id,
        action=action,
        guild_id=interaction.guild.id if interaction.guild else None,
        target_user_id=target_user_id,
        detail=detail,
    )
    audit_logger.info(
        "action=%s actor=%s guild=%s target=%s op=%s detail=%s",
        action, interaction.user.id,
        interaction.guild.id if interaction.guild else None,
        target_user_id, operation_id, utils.safe_json_dumps(detail or {}, limit=500),
    )
    return operation_id


async def _confirm(
    interaction: discord.Interaction,
    *,
    title: str,
    description: str,
    confirm_label: str = "実行する",
    stages: int = 1,
    danger: bool = True,
) -> bool:
    """確認ボタンを表示し、承認されたかどうかを返す。

    Args:
        stages: 2 以上にすると「最終確認」を挟む (取り消せない操作向け)。
        danger: False にすると警告色ではなく通常色で表示する
            (キャンペーン開始など、危険ではない確認用)。
    """
    view = ui.ConfirmView(
        owner_id=interaction.user.id, confirm_label=confirm_label, stages=stages,
        danger=danger,
    )
    embed = ui.info_embed(
        title, description,
        color=config.Color.DANGER if danger else config.Color.ACCENT,
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.wait()
    return bool(view.value)


def _channel_writable(
    channel: discord.TextChannel | discord.Thread, me: discord.Member
) -> bool:
    """Bot がそのチャンネルへ Embed 付きメッセージを送れるか。"""
    perms = channel.permissions_for(me)
    return bool(perms.view_channel and perms.send_messages and perms.embed_links)


def _parse_date(value: str | None) -> int | None:
    """``YYYY-MM-DD`` を JST 基準の UNIX 秒へ変換する。"""
    if not value:
        return None
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=utils.JST)
    except ValueError:
        return None
    return int(dt.timestamp())


STATUS_CHOICES = [
    app_commands.Choice(name=f"{config.STATUS_LABELS[s]} ({s})", value=s)
    for s in (
        config.TxStatus.WAITING_LINK, config.TxStatus.QUEUED, config.TxStatus.PROCESSING,
        config.TxStatus.RECEIVED, config.TxStatus.CREDITING, config.TxStatus.COMPLETED,
        config.TxStatus.FAILED, config.TxStatus.CANCELLED, config.TxStatus.EXPIRED,
        config.TxStatus.MANUAL_REVIEW,
    )
]


# ---------------------------------------------------------------------------
# /setup
# ---------------------------------------------------------------------------
@app_commands.command(name="setup", description="初期設定の状態を確認します (管理者)")
@app_commands.guild_only()
@require_admin(allow_unallowed_guild=True)
async def setup_command(interaction: discord.Interaction) -> None:
    """初期設定チェックリストを表示する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None
    settings = await bot.db.get_settings(guild.id)
    allowed = await bot.db.is_guild_allowed(guild.id)
    panels = await bot.db.list_panels(guild.id, panel_type=config.PANEL_TYPE_CHARGE)
    ranking_panels = await bot.db.list_ranking_panels(guild.id)
    kyash_ok = bot.kyash.status == config.KyashAccountStatus.ACTIVE
    manage_guild = bool(guild.me and guild.me.guild_permissions.manage_guild)

    checks: list[tuple[bool, str, str]] = [
        (True, "データベース", f"`{config.DB_PATH.name}` / schema v{config.SCHEMA_VERSION}"),
        (allowed, "サーバー許可", "許可済み" if allowed else "未許可 (Bot Owner の `/server allow` が必要)"),
        (
            settings.admin_role_id is not None,
            "管理者ロール",
            f"<@&{settings.admin_role_id}>" if settings.admin_role_id
            else "未設定 (`/settings admin_role`) ※現在は manage_guild 権限で代替中",
        ),
        (
            settings.achievement_channel_id is not None,
            "実績チャンネル",
            f"<#{settings.achievement_channel_id}>" if settings.achievement_channel_id
            else "未設定 (`/settings achievement_channel`)",
        ),
        (
            settings.log_channel_id is not None,
            "ログチャンネル",
            f"<#{settings.log_channel_id}>" if settings.log_channel_id
            else "未設定 (`/settings log_channel`)",
        ),
        (True, "チャージ率", utils.fmt_rate(settings.charge_rate)),
        (
            settings.minimum_charge < settings.maximum_charge,
            "金額制限",
            f"{utils.fmt_yen(settings.minimum_charge)} 〜 {utils.fmt_yen(settings.maximum_charge)} "
            f"/ 日次 {utils.fmt_yen(settings.daily_limit)}",
        ),
        (
            kyash_ok,
            "受取用Kyashアカウント",
            config.KYASH_STATUS_LABELS.get(bot.kyash.status, bot.kyash.status)
            + ("" if kyash_ok else " (Bot Owner の `/kyash login` が必要)"),
        ),
        (
            bool(panels),
            "チャージパネル",
            f"{len(panels)} 件設置済み" if panels else "未設置 (`/charge_panel`)",
        ),
        (
            bool(ranking_panels),
            "ランキングパネル",
            f"{len(ranking_panels)} 件設置済み" if ranking_panels else "未設置 (`/ranking_panel`)",
        ),
        (
            settings.balance_log_channel_id is not None,
            "残高操作ログ",
            f"<#{settings.balance_log_channel_id}> "
            f"({'全変動' if settings.balance_log_scope == 'ALL' else '手動操作のみ'})"
            if settings.balance_log_channel_id
            else "未設定 (`/settings balance_log_channel`)",
        ),
        (
            manage_guild,
            "招待キャンペーン用の権限",
            "「サーバー管理」権限あり (招待者を特定できます)" if manage_guild
            else "「サーバー管理」権限なし → 招待キャンペーンでは招待者を特定できません",
        ),
    ]

    lines = [f"{'✅' if ok else '⚠️'} **{name}** — {detail}" for ok, name, detail in checks]
    pending = [name for ok, name, _ in checks if not ok]
    embed = ui.info_embed(
        "🧭 セットアップ状態",
        f"{ui.SEPARATOR}\n" + "\n".join(lines) + f"\n{ui.SEPARATOR}",
        color=config.Color.SUCCESS if not pending else config.Color.WARNING,
    )
    if pending:
        embed.add_field(name="未設定の項目", value="\n".join(f"・{name}" for name in pending), inline=False)
    else:
        embed.add_field(name="状態", value="すべての項目が設定されています。", inline=False)
    shop_items = await bot.db.list_shop_items(guild.id, active_only=False)
    campaign = await bot.db.get_active_campaign(guild.id)
    embed.add_field(
        name="任意機能",
        value=(
            f"ショップ: {len(shop_items)} 商品 (`/shop add` / `/shop panel`)\n"
            f"招待キャンペーン: "
            + (f"開催中 ({campaign['name']})" if campaign else "未開催 (`/campaign create`)")
            + f"\n日次サマリ: {'有効' if settings.summary_enabled else '無効'} "
              "(`/settings summary_channel`)\n"
              "管理ダッシュボード: `/admin_panel`"
        ),
        inline=False,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# チャージパネル
# ---------------------------------------------------------------------------
@app_commands.command(name="charge_panel", description="このチャンネルへチャージパネルを新規設置します (管理者)")
@app_commands.guild_only()
@require_admin()
async def charge_panel_command(interaction: discord.Interaction) -> None:
    """常設チャージパネルを設置する (既存パネルは削除しない)。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    channel = interaction.channel
    assert guild is not None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR,
                                 admin_detail="テキストチャンネルで実行してください。"),
            ephemeral=True,
        )
        return
    if guild.me is None or not _channel_writable(channel, guild.me):
        await interaction.followup.send(
            embed=ui.info_embed(
                "権限が不足しています",
                "このチャンネルへ「メッセージを送信」「埋め込みリンク」の権限が必要です。",
                color=config.Color.DANGER,
            ),
            ephemeral=True,
        )
        return

    settings = await bot.db.get_settings(guild.id)
    embed = ui.charge_panel_embed(
        settings,
        kyash_ready=bot.kyash.is_usable,
        providers=await bot.charge.provider_availability(guild.id, settings),
    )
    message = await channel.send(embed=embed, view=ui.ChargePanelView())
    panel_id = await bot.db.add_panel(guild.id, channel.id, message.id, config.PANEL_TYPE_CHARGE)
    op_id = await _audit(
        interaction, "PANEL_CREATE",
        detail={"panel_id": panel_id, "channel_id": channel.id, "message_id": message.id,
                "panel_type": config.PANEL_TYPE_CHARGE},
    )
    await interaction.followup.send(
        embed=ui.success_embed(
            "✅ チャージパネルを設置しました",
            f"チャンネル: {channel.mention}\nメッセージID: `{message.id}`\n操作ID: `{op_id}`",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# パネル一覧 / 無効化 (種類をまとめて扱う)
# ---------------------------------------------------------------------------
#: /panels で選べるパネルの種類。RANKING は別テーブルで管理している。
_PANEL_KINDS: Final[dict[str, str]] = {
    config.PANEL_TYPE_CHARGE: "💰 チャージパネル",
    "RANKING": "🏆 ランキングパネル",
    config.PANEL_TYPE_SHOP: "🛒 ショップパネル",
    config.PANEL_TYPE_INVITE: "🤝 招待パネル",
    config.PANEL_TYPE_ADMIN: "🛠 管理ダッシュボード",
    config.PANEL_TYPE_GOAL: "🎯 チャージ目標パネル",
}


@app_commands.command(name="panels", description="設置済みパネルの一覧と無効化 (管理者)")
@app_commands.describe(
    type="パネルの種類", disable_message_id="無効化するパネルのメッセージID",
)
@app_commands.choices(type=[
    app_commands.Choice(name=label, value=key) for key, label in _PANEL_KINDS.items()
])
@app_commands.guild_only()
@require_admin()
async def panels_command(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    disable_message_id: str | None = None,
) -> None:
    """パネルの一覧表示と無効化。

    チャージ / ランキング / ショップ / 招待 / 管理の 5 種類を 1 つの
    コマンドで扱う (種類ごとにコマンドを分けない)。
    """
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None
    kind = type.value
    is_ranking = kind == "RANKING"

    async def fetch() -> list[Any]:
        if is_ranking:
            return list(await bot.db.list_ranking_panels(guild.id, active_only=False))
        return list(await bot.db.list_panels(guild.id, panel_type=kind, active_only=False))

    if disable_message_id:
        raw = disable_message_id.strip()
        if not raw.isdigit():
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "メッセージIDは数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        message_id = int(raw)
        if not any(int(p["message_id"]) == message_id for p in await fetch()):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "見つかりません",
                    f"このサーバーに該当する{_PANEL_KINDS[kind]}がありません。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        if is_ranking:
            await bot.db.deactivate_ranking_panel(message_id=message_id)
        else:
            await bot.db.deactivate_panel(message_id=message_id)
        op_id = await _audit(
            interaction, "PANEL_DISABLE",
            detail={"message_id": message_id, "panel_type": kind},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 無効化しました",
                f"{_PANEL_KINDS[kind]} `{message_id}` を無効化しました。\n"
                f"操作ID: `{op_id}`\n"
                "メッセージ自体は残るため、不要な場合は手動で削除してください。",
            ),
            ephemeral=True,
        )
        return

    panels = await fetch()
    if not panels:
        setup_hint = {
            config.PANEL_TYPE_CHARGE: "`/charge_panel`",
            "RANKING": "`/ranking_panel`",
            config.PANEL_TYPE_SHOP: "`/shop panel`",
            config.PANEL_TYPE_INVITE: "`/campaign panel`",
            config.PANEL_TYPE_ADMIN: "`/admin_panel`",
        }[kind]
        await interaction.followup.send(
            embed=ui.info_embed(
                _PANEL_KINDS[kind], f"まだ設置されていません。{setup_hint} で設置できます。"
            ),
            ephemeral=True,
        )
        return
    lines = []
    for p in panels[:25]:
        extra = ""
        if is_ranking and "ranking_type" in p.keys():
            extra = f" / {config.RANKING_TYPE_LABELS.get(str(p['ranking_type']), '')}"
        lines.append(
            f"{'🟢' if p['active'] else '⚫'} <#{p['channel_id']}> / "
            f"`{p['message_id']}` / {utils.format_jst(p['created_at'])}{extra}"
        )
    await interaction.followup.send(
        embed=ui.info_embed(
            f"{_PANEL_KINDS[kind]} 一覧",
            f"{ui.SEPARATOR}\n" + "\n".join(lines)
            + f"\n{ui.SEPARATOR}\n無効化するには `disable_message_id` にIDを指定してください。",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# ランキングパネル (チャージパネルとは完全に独立)
# ---------------------------------------------------------------------------
@app_commands.command(name="ranking_panel", description="ランキング専用パネルを設置します (管理者)")
@app_commands.describe(
    channel="設置先チャンネル (省略時は現在のチャンネル)", type="集計方式 (既定: 残高)"
)
@app_commands.choices(type=[
    app_commands.Choice(name=label, value=key)
    for key, label in config.RANKING_TYPE_LABELS.items()
])
@app_commands.guild_only()
@require_admin()
async def ranking_panel_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    type: app_commands.Choice[str] | None = None,
) -> None:
    """ランキングパネルを新規設置する (複数設置可能・既存は削除しない)。

    集計方式はパネルごとに選べるため、残高・週間・月間・招待を同時に設置できる。
    """
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None
    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            embed=ui.info_embed("設置できません", "テキストチャンネルを指定してください。",
                                color=config.Color.DANGER),
            ephemeral=True,
        )
        return
    if guild.me is None or not _channel_writable(target, guild.me):
        await interaction.followup.send(
            embed=ui.info_embed(
                "権限が不足しています",
                f"{target.mention} へ「メッセージを送信」「埋め込みリンク」の権限が必要です。",
                color=config.Color.DANGER,
            ),
            ephemeral=True,
        )
        return

    settings = await bot.db.get_settings(guild.id)
    ranking_type = type.value if type else config.RankingType.BALANCE
    if settings.ranking_enabled:
        entries = await bot.charge.build_ranking_entries(guild.id, settings, ranking_type)
        embed = ui.ranking_embed(
            guild, entries, settings, updated_at=utils.now_ts(), ranking_type=ranking_type
        )
        signature = f"{ranking_type}|" + bot.charge.ranking_signature(entries)
    else:
        embed = ui.ranking_disabled_embed()
        signature = "DISABLED"
    message = await target.send(embed=embed, view=ui.RankingPanelView())
    panel_id = await bot.db.add_ranking_panel(guild.id, target.id, message.id, ranking_type)
    await bot.db.update_ranking_panel_state(message.id, signature=signature)
    op_id = await _audit(
        interaction, "RANKING_PANEL_CREATE",
        detail={"panel_id": panel_id, "channel_id": target.id, "message_id": message.id,
                "ranking_type": ranking_type},
    )
    await interaction.followup.send(
        embed=ui.success_embed(
            "✅ ランキングパネルを設置しました",
            f"チャンネル: {target.mention}\n"
            f"集計方式: **{config.RANKING_TYPE_LABELS.get(ranking_type, ranking_type)}**\n"
            f"メッセージID: `{message.id}`\n"
            f"表示件数: {settings.ranking_limit} / 更新間隔: {settings.ranking_interval}秒\n"
            f"操作ID: `{op_id}`",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /server (Bot Owner 専用)
# ---------------------------------------------------------------------------
class ServerGroup(app_commands.Group):
    """サーバー許可管理 (Bot Owner 専用)。"""

    def __init__(self) -> None:
        super().__init__(name="server", description="サーバー許可の管理 (Bot Owner)")

    @app_commands.command(name="allow", description="サーバーの利用を許可します")
    @app_commands.describe(guild_id="対象サーバーID (省略時は実行中のサーバー)", note="メモ")
    @require_owner()
    async def allow(
        self, interaction: discord.Interaction, guild_id: str | None = None, note: str | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        target_id = _resolve_guild_id(interaction, guild_id)
        if target_id is None:
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "サーバーIDを数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.db.set_guild_permission(target_id, "ALLOWED", interaction.user.id, note)
        await bot.db.get_settings(target_id)  # 設定行をデフォルト値で作成
        op_id = await _audit(interaction, "SERVER_ALLOW",
                             detail={"guild_id": target_id, "note": note})
        guild = bot.get_guild(target_id)
        # コマンドはグローバルにのみ登録する。ここでギルドへコピーすると
        # グローバル分と二重に表示されてしまうため、同期は行わない。
        # 許可の判定は実行時 (require_admin / ensure_usable_guild) で行うので、
        # 許可した時点で既に登録済みのコマンドがそのまま使える。
        removed = 0
        if guild is not None:
            # 旧バージョンが残したギルド単位のコマンドがあれば掃除する
            removed = await bot.clear_guild_commands(guild)
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ サーバーを許可しました",
                f"対象: **{guild.name if guild else '未参加'}** (`{target_id}`)\n"
                f"操作ID: `{op_id}`\n\n"
                "コマンドはグローバル登録のため、すぐに使えます。"
                + (f"\n重複していたギルド専用コマンド {removed} 件を削除しました。"
                   if removed else ""),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="deny", description="サーバーの利用を禁止します")
    @app_commands.describe(guild_id="対象サーバーID", reason="理由")
    @require_owner()
    async def deny(
        self, interaction: discord.Interaction, guild_id: str, reason: str | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        target_id = _resolve_guild_id(interaction, guild_id)
        if target_id is None:
            await interaction.response.send_message(
                embed=ui.info_embed("入力が不正です", "サーバーIDを数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        guild = bot.get_guild(target_id)
        approved = await _confirm(
            interaction,
            title="⚠️ サーバーの利用を禁止します",
            description=(
                f"対象: **{guild.name if guild else '未参加'}** (`{target_id}`)\n"
                "禁止すると新規チャージ・パネル操作ができなくなります。\n"
                "残高・履歴データは削除されません。"
            ),
            confirm_label="禁止する",
        )
        if not approved:
            return
        await bot.db.set_guild_permission(target_id, "DENIED", interaction.user.id, reason)
        op_id = await _audit(interaction, "SERVER_DENY",
                             detail={"guild_id": target_id, "reason": reason})
        await interaction.followup.send(
            embed=ui.success_embed(
                "🚫 サーバーの利用を禁止しました",
                f"対象: `{target_id}`\n理由: {reason or '-'}\n操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="suspend", description="許可に有効期限を設定します")
    @app_commands.describe(guild_id="対象サーバーID", until="失効日 (YYYY-MM-DD)。none で無期限に戻す")
    @require_owner()
    async def suspend(
        self, interaction: discord.Interaction, guild_id: str, until: str
    ) -> None:
        """期限付きの許可 (期限を過ぎると自動的に利用不可になる)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        target_id = _resolve_guild_id(interaction, guild_id)
        if target_id is None:
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "サーバーIDを数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if until.strip().lower() in ("none", "clear", "-"):
            expires: int | None = None
        else:
            expires = _parse_date(until)
            if expires is None:
                await interaction.followup.send(
                    embed=ui.info_embed(
                        "入力が不正です", "失効日は `YYYY-MM-DD` または `none` で指定してください。",
                        color=config.Color.DANGER,
                    ),
                    ephemeral=True,
                )
                return
        await bot.db.set_guild_permission_expiry(target_id, expires)
        op_id = await _audit(
            interaction, "SERVER_SUSPEND",
            detail={"guild_id": target_id, "expires_at": expires},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 有効期限を設定しました",
                f"対象: `{target_id}`\n"
                f"失効: {utils.format_jst(expires) if expires else '無期限 (解除)'}\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="許可状態の一覧を表示します")
    @require_owner()
    async def list_servers(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.list_guild_permissions()
        joined = {g.id: g for g in bot.guilds}
        lines: list[str] = []
        for row in rows[:40]:
            gid = int(row["guild_id"])
            guild = joined.get(gid)
            mark = "🟢" if row["status"] == "ALLOWED" else "🚫"
            state = "参加中" if guild else "未参加"
            lines.append(
                f"{mark} `{gid}` {guild.name if guild else '(不明)'} / {state} / "
                f"{utils.format_jst(row['allowed_at'])}"
            )
        unregistered = [g for g in bot.guilds if g.id not in {int(r["guild_id"]) for r in rows}]
        embed = ui.info_embed(
            "🗂 サーバー許可一覧",
            f"{ui.SEPARATOR}\n" + ("\n".join(lines) if lines else "登録されたサーバーはありません。"),
        )
        if unregistered:
            embed.add_field(
                name="未登録で参加中のサーバー",
                value="\n".join(f"`{g.id}` {g.name}" for g in unregistered[:10]),
                inline=False,
            )
        embed.set_footer(text=f"許可済み {await bot.db.count_allowed_guilds()} / 参加 {len(bot.guilds)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="sync", description="スラッシュコマンドを再同期します (Bot Owner)"
    )
    @app_commands.describe(
        cleanup="コマンドが二重に表示される場合に ON。ギルド専用の重複登録を削除します"
    )
    @require_owner()
    async def sync(self, interaction: discord.Interaction, cleanup: bool = False) -> None:
        """コマンドはグローバルにのみ登録する。

        グローバルとギルドの両方に同じコマンドを登録すると、Discord は
        それぞれを別枠で表示するため「コマンドが2つずつ見える」状態になる。
        ``cleanup`` はその原因となるギルド専用登録をすべて削除する。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        results: list[str] = []
        try:
            synced = await bot.tree.sync()
            results.append(f"グローバル: **{len(synced)} 件** を登録しました")
        except (discord.HTTPException, discord.ClientException) as exc:
            results.append(f"グローバル: 失敗 ({utils.safe_error_text(exc, limit=120)})")
        if cleanup:
            summary = await bot.cleanup_guild_commands(force=True)
            results.append(
                f"重複削除: **{summary['removed']} 件** "
                f"({summary['guilds']} サーバーを確認)"
            )
            if summary["failed"]:
                results.append(f"⚠️ {summary['failed']} サーバーで削除に失敗しました")
        else:
            results.append(
                "コマンドが二重に見える場合は `/server sync cleanup:True` を実行してください。"
            )
        await interaction.followup.send(
            embed=ui.info_embed("🔄 コマンド同期", "\n".join(results[:25])), ephemeral=True
        )


def _resolve_guild_id(interaction: discord.Interaction, raw: str | None) -> int | None:
    """サーバーID の解決 (省略時は実行中のサーバー)。"""
    if raw is None or not raw.strip():
        return interaction.guild.id if interaction.guild else None
    text = raw.strip()
    if not text.isdigit() or len(text) > 25:
        return None
    return int(text)


# ---------------------------------------------------------------------------
# /kyash (受取用アカウント / Bot Owner 専用。status のみ管理者も参照可)
# ---------------------------------------------------------------------------
def _resolve_kyash_slot(bot: "ChargeBot", account: str | None) -> Any:
    """識別名またはアカウントIDから受取用アカウントを引く。"""
    if not account:
        return bot.kyash.primary
    text = account.strip()
    if text.isdigit():
        slot = bot.kyash.get_slot(int(text))
        # get_slot は未知の ID でフォールバックするため、一致を確認する
        if slot is not None and slot.id == int(text):
            return slot
    return bot.kyash.find_slot_by_label(text)


class KyashGroup(app_commands.Group):
    """受取用 Kyash アカウントの管理。"""

    def __init__(self) -> None:
        super().__init__(name="kyash", description="受取用Kyashアカウントの管理")

    @app_commands.command(name="login", description="受取用Kyashアカウントへログインします (Bot Owner)")
    @app_commands.describe(account="ログイン先のアカウント (省略時は代表アカウント)")
    @require_owner()
    async def login(
        self, interaction: discord.Interaction, account: str | None = None
    ) -> None:
        """認証情報は Ephemeral Modal で受け取り、ログにも DB にも平文で残さない。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        slot = _resolve_kyash_slot(bot, account) if account else None
        if account and slot is None:
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed(
                    "見つかりません",
                    f"アカウント `{utils.truncate(account, 40)}` は登録されていません。"
                    "`/kyash add` で枠を追加してください。",
                    color=config.Color.DANGER),
            )
            return
        await ui.safe_send_modal(
            interaction, ui.KyashLoginModal(account_id=slot.id if slot else None)
        )

    @app_commands.command(name="status", description="受取用Kyashアカウントの状態を表示します (管理者)")
    @require_admin()
    async def status(self, interaction: discord.Interaction) -> None:
        """登録されている受取用アカウントをまとめて表示する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            embed=ui.kyash_accounts_embed(
                bot.kyash.status_snapshot(),
                show_balance=bot.is_bot_owner(interaction.user),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="add", description="受取用アカウントの枠を追加します (Bot Owner)")
    @app_commands.describe(
        label="識別名 (例: main / sub1)",
        threshold="残高しきい値 (円)。0で無制限",
        priority="使う順番 (小さいほど先に使う)",
    )
    @require_owner()
    async def add_account(
        self,
        interaction: discord.Interaction,
        label: str,
        threshold: app_commands.Range[int, 0, 100_000_000] = 0,
        priority: app_commands.Range[int, 0, 1000] = 0,
    ) -> None:
        """アカウント枠を追加する。ログインは `/kyash login` で行う。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            account_id = await bot.kyash.add_account(
                label, threshold=int(threshold), priority=int(priority)
            )
        except (kyash_service.KyashServiceError, DatabaseError) as exc:
            await interaction.followup.send(
                embed=ui.info_embed("追加できません", str(exc), color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        op_id = await _audit(
            interaction, "KYASH_ACCOUNT_ADD",
            detail={"account_id": account_id, "label": label,
                    "threshold": int(threshold), "priority": int(priority)},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ アカウント枠を追加しました",
                f"アカウントID: `{account_id}`\n識別名: **{label}**\n"
                f"しきい値: {utils.fmt_yen(int(threshold)) if threshold else '無制限'}\n"
                f"優先度: {priority}\n操作ID: `{op_id}`\n\n"
                f"続けて `/kyash login account:{label}` でログインしてください。",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="受取用アカウントを削除します (Bot Owner)")
    @app_commands.describe(account="識別名またはアカウントID")
    @require_owner()
    async def remove_account(self, interaction: discord.Interaction, account: str) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        slot = _resolve_kyash_slot(bot, account)
        if slot is None:
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed("見つかりません", "そのアカウントは登録されていません。",
                                    color=config.Color.DANGER),
            )
            return
        used = await bot.db.count_transactions_by_account(slot.id)
        approved = await _confirm(
            interaction,
            title=f"受取用アカウント「{slot.label}」を削除します",
            description=(
                "保存済みのトークンと端末情報も削除されます。\n"
                + (f"⚠️ このアカウントで受け取った取引が **{used} 件**あります。\n"
                   "取引の記録は残りますが、その取引の**受取確認ができなくなります**。\n"
                   if used else "")
                + "残りのアカウントが無くなると、Kyash のチャージを受け付けられません。"
            ),
            confirm_label="削除する",
        )
        if not approved:
            return
        removed = await bot.kyash.remove_account(slot.id)
        op_id = await _audit(
            interaction, "KYASH_ACCOUNT_REMOVE",
            detail={"account_id": slot.id, "label": slot.label, "transactions": used},
        )
        for guild in bot.guilds:
            if await bot.db.is_guild_allowed(guild.id):
                await bot.charge.refresh_charge_panels(guild.id)
        await interaction.followup.send(
            embed=ui.info_embed(
                "削除しました" if removed else "見つかりません",
                f"識別名: **{slot.label}**\n操作ID: `{op_id}`",
                color=config.Color.NEUTRAL,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="priority", description="受取用アカウントの使う順番と有効/無効を変えます (Bot Owner)"
    )
    @app_commands.describe(
        account="識別名またはアカウントID",
        priority="使う順番 (小さいほど先に使う)",
        enabled="受取に使うかどうか",
    )
    @require_owner()
    async def priority(
        self,
        interaction: discord.Interaction,
        account: str,
        priority: app_commands.Range[int, 0, 1000] | None = None,
        enabled: bool | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        slot = _resolve_kyash_slot(bot, account)
        if slot is None:
            await interaction.followup.send(
                embed=ui.info_embed("見つかりません", "そのアカウントは登録されていません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if priority is None and enabled is None:
            await interaction.followup.send(
                embed=ui.info_embed("変更内容がありません",
                                    "priority か enabled のどちらかを指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.kyash.set_account_options(
            slot.id,
            priority=int(priority) if priority is not None else None,
            enabled=enabled,
        )
        op_id = await _audit(
            interaction, "KYASH_ACCOUNT_OPTIONS",
            detail={"account_id": slot.id, "priority": priority, "enabled": enabled},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 設定を変更しました",
                f"識別名: **{slot.label}**\n"
                f"優先度: {slot.priority}\n"
                f"受取に使う: {'はい' if slot.enabled else 'いいえ'}\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="threshold", description="受取用アカウントの残高しきい値を設定します (Bot Owner)"
    )
    @app_commands.describe(
        amount="しきい値 (円)。0で無効。到達するとそのアカウントは使われません",
        account="対象アカウント (省略時は代表アカウント)",
    )
    @require_owner()
    async def threshold(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, 100_000_000],
        account: str | None = None,
    ) -> None:
        """Kyash 側の残高上限に達して受取が失敗する前に、そのアカウントの使用を止める設定。

        複数登録している場合、1つが上限に達しても他のアカウントで受取を続けられる。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        slot = _resolve_kyash_slot(bot, account) if account else bot.kyash.primary
        if slot is None:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "アカウントがありません",
                    "`/kyash add` で枠を追加してから設定してください。",
                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.kyash.set_wallet_threshold(int(amount), account_id=slot.id)
        await _audit(
            interaction, "KYASH_THRESHOLD",
            detail={"account_id": slot.id, "threshold": int(amount)},
        )
        headroom = slot.headroom()
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 残高しきい値を設定しました",
                f"アカウント: **{slot.label}**\n"
                f"しきい値: **{utils.fmt_yen(int(amount)) if amount else '無効 (無制限)'}**\n"
                + (f"現在残高: {utils.fmt_yen(int(slot.wallet_balance))}\n"
                   if slot.wallet_balance is not None else "")
                + (f"しきい値まで: {utils.fmt_yen(headroom)}" if headroom is not None else ""),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="logout", description="受取用Kyashアカウントをログアウトします (Bot Owner)")
    @app_commands.describe(account="対象アカウント (省略時はすべて)")
    @require_owner()
    async def logout(
        self, interaction: discord.Interaction, account: str | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        slot = _resolve_kyash_slot(bot, account) if account else None
        if account and slot is None:
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed("見つかりません", "そのアカウントは登録されていません。",
                                    color=config.Color.DANGER),
            )
            return
        target = f"**{slot.label}**" if slot else "**すべてのアカウント**"
        approved = await _confirm(
            interaction,
            title="⚠️ Kyash アカウントをログアウトします",
            description=(
                f"対象: {target}\n\n"
                "保存済みのアクセストークンと端末情報を削除します。\n"
                "使えるアカウントが無くなると新規チャージを受け付けられません。\n"
                "受取待ちのチャージは保留され、再ログイン後に処理されます。"
            ),
            confirm_label="ログアウトする",
        )
        if not approved:
            return
        await bot.kyash.logout(slot.id if slot else None)
        op_id = await _audit(interaction, "KYASH_LOGOUT")
        for guild in bot.guilds:
            if await bot.db.is_guild_allowed(guild.id):
                await bot.charge.refresh_charge_panels(guild.id)
        await interaction.followup.send(
            embed=ui.success_embed("👋 ログアウトしました", f"操作ID: `{op_id}`"), ephemeral=True
        )

    @app_commands.command(name="reconnect", description="保存済み情報でセッションを再確認します (Bot Owner)")
    @require_owner()
    async def reconnect(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        # 全アカウントを読み直して健康確認する
        if not bot.kyash.slots():
            status = await bot.kyash.load_accounts()
        else:
            status = await bot.kyash.health_check()
        await _audit(interaction, "KYASH_RECONNECT", detail={"status": status})
        if status == config.KyashAccountStatus.ACTIVE:
            bot.charge.queue_wakeup.set()
            for guild in bot.guilds:
                if await bot.db.is_guild_allowed(guild.id):
                    await bot.charge.refresh_charge_panels(guild.id)
            await interaction.followup.send(
                embed=ui.success_embed(
                    "✅ セッションは有効です",
                    f"受取用アカウント: `{bot.kyash.username or '不明'}`\n受取処理を再開しました。",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.info_embed(
                "⚠️ セッションを復元できませんでした",
                f"状態: **{config.KYASH_STATUS_LABELS.get(status, status)}**\n"
                f"詳細: {utils.truncate(utils.sanitize_for_log(bot.kyash.last_error or '-'), 400)}\n\n"
                "`/kyash login` で再ログインしてください。"
                "端末情報が保存されている場合は SMS 認証が不要になることがあります。",
                color=config.Color.WARNING,
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /settings (Server Admin)
# ---------------------------------------------------------------------------
class SettingsGroup(app_commands.Group):
    """サーバーごとの設定変更。"""

    def __init__(self) -> None:
        super().__init__(name="settings", description="サーバー設定の変更 (管理者)")

    async def _apply(
        self,
        interaction: discord.Interaction,
        *,
        field: str,
        value: Any,
        label: str,
        display: str,
        refresh_charge_panels: bool = False,
        refresh_ranking: bool = False,
    ) -> None:
        """設定を更新し、監査ログ・ログチャンネル・パネル表示へ反映する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        before = await bot.db.get_settings(guild.id)
        before_value = getattr(before, field, None)
        await bot.db.update_settings(guild.id, **{field: value})
        op_id = await _audit(
            interaction, f"SETTING_{field.upper()}",
            detail={"field": field, "before": str(before_value), "after": str(value)},
        )
        if refresh_charge_panels:
            await bot.charge.refresh_charge_panels(guild.id)
        if refresh_ranking:
            await bot.charge.refresh_ranking_panels(guild.id, force=True)
        await bot.charge.log_event(
            guild.id, f"⚙️ 設定変更: {label}",
            fields=(
                ("変更前", str(before_value), True),
                ("変更後", display, True),
                ("操作者", interaction.user.mention, True),
                ("操作ID", f"`{op_id}`", True),
            ),
            color=config.Color.ACCENT,
        )
        await interaction.followup.send(
            embed=ui.success_embed(f"✅ {label} を変更しました", f"{display}\n操作ID: `{op_id}`"),
            ephemeral=True,
        )

    @app_commands.command(name="charge_rate", description="チャージ率(%)を変更します")
    @app_commands.describe(rate="例: 130 / 130.5 (1〜1000)")
    @app_commands.guild_only()
    @require_admin()
    async def charge_rate(self, interaction: discord.Interaction, rate: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = utils.validate_charge_rate(rate)
        if parsed is None:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "入力が不正です",
                    f"チャージ率は {config.MIN_CHARGE_RATE}〜{config.MAX_CHARGE_RATE} の数値で指定してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await self._apply(
            interaction, field="charge_rate", value=utils.rate_to_db(parsed),
            label="チャージ率", display=utils.fmt_rate(parsed), refresh_charge_panels=True,
        )

    @app_commands.command(name="charge_min", description="最低チャージ額を変更します")
    @app_commands.describe(amount="円 (1以上)")
    @app_commands.guild_only()
    @require_admin()
    async def charge_min(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, config.AMOUNT_HARD_MIN, config.AMOUNT_HARD_MAX],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await bot.db.get_settings(interaction.guild.id)  # type: ignore[union-attr]
        if amount >= settings.maximum_charge:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "設定できません",
                    f"最低額は最大額 ({utils.fmt_yen(settings.maximum_charge)}) より小さい値にしてください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await self._apply(
            interaction, field="minimum_charge", value=int(amount), label="最低チャージ額",
            display=utils.fmt_yen(amount), refresh_charge_panels=True,
        )

    @app_commands.command(name="charge_max", description="最大チャージ額を変更します")
    @app_commands.describe(amount="円")
    @app_commands.guild_only()
    @require_admin()
    async def charge_max(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, config.AMOUNT_HARD_MIN, config.AMOUNT_HARD_MAX],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await bot.db.get_settings(interaction.guild.id)  # type: ignore[union-attr]
        if amount <= settings.minimum_charge:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "設定できません",
                    f"最大額は最低額 ({utils.fmt_yen(settings.minimum_charge)}) より大きい値にしてください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await self._apply(
            interaction, field="maximum_charge", value=int(amount), label="最大チャージ額",
            display=utils.fmt_yen(amount), refresh_charge_panels=True,
        )

    @app_commands.command(name="daily_limit", description="1ユーザーの日次チャージ上限を変更します")
    @app_commands.describe(amount="円 (0で無制限)")
    @app_commands.guild_only()
    @require_admin()
    async def daily_limit(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, 100_000_000],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="daily_limit", value=int(amount), label="日次チャージ上限 (ユーザー)",
            display=utils.fmt_yen(amount) if amount else "無制限",
        )

    @app_commands.command(name="guild_daily_limit", description="サーバー全体の日次チャージ上限を変更します")
    @app_commands.describe(amount="円 (0で無制限)")
    @app_commands.guild_only()
    @require_admin()
    async def guild_daily_limit(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, 1_000_000_000],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="guild_daily_limit", value=int(amount),
            label="日次チャージ上限 (サーバー全体)",
            display=utils.fmt_yen(amount) if amount else "無制限",
        )

    @app_commands.command(name="admin_role", description="管理者ロールを設定します")
    @app_commands.describe(role="管理者として扱うロール")
    @app_commands.guild_only()
    @require_admin()
    async def admin_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if role.is_default():
            await interaction.followup.send(
                embed=ui.info_embed("設定できません", "@everyone は管理者ロールに指定できません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await self._apply(
            interaction, field="admin_role_id", value=role.id, label="管理者ロール",
            display=role.mention,
        )

    @app_commands.command(name="achievement_channel", description="実績チャンネルを設定します")
    @app_commands.guild_only()
    @require_admin()
    async def achievement_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        if guild.me is None or not _channel_writable(channel, guild.me):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "権限が不足しています",
                    f"{channel.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await self._apply(
            interaction, field="achievement_channel_id", value=channel.id,
            label="実績チャンネル", display=channel.mention,
        )

    @app_commands.command(name="log_channel", description="ログチャンネルを設定します")
    @app_commands.guild_only()
    @require_admin()
    async def log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        if guild.me is None or not _channel_writable(channel, guild.me):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "権限が不足しています",
                    f"{channel.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await self._apply(
            interaction, field="log_channel_id", value=channel.id,
            label="ログチャンネル", display=channel.mention,
        )

    @app_commands.command(name="ranking_limit", description="ランキング表示件数を変更します")
    @app_commands.describe(count=f"{config.RANKING_LIMIT_MIN}〜{config.RANKING_LIMIT_MAX}")
    @app_commands.guild_only()
    @require_admin()
    async def ranking_limit(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, config.RANKING_LIMIT_MIN, config.RANKING_LIMIT_MAX],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="ranking_limit", value=int(count), label="ランキング表示件数",
            display=f"TOP {count}", refresh_ranking=True,
        )

    @app_commands.command(name="ranking_interval", description="ランキング自動更新間隔を変更します")
    @app_commands.describe(seconds=f"{config.RANKING_INTERVAL_MIN}〜{config.RANKING_INTERVAL_MAX} 秒")
    @app_commands.guild_only()
    @require_admin()
    async def ranking_interval(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, config.RANKING_INTERVAL_MIN, config.RANKING_INTERVAL_MAX],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="ranking_interval", value=int(seconds),
            label="ランキング更新間隔", display=f"{seconds} 秒",
        )

    @app_commands.command(name="ranking_enabled", description="ランキング表示のON/OFFを切り替えます")
    @app_commands.guild_only()
    @require_admin()
    async def ranking_enabled(self, interaction: discord.Interaction, enabled: bool) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="ranking_enabled", value=1 if enabled else 0,
            label="ランキング表示", display="有効" if enabled else "無効", refresh_ranking=True,
        )

    @app_commands.command(
        name="ranking_hide_absent", description="サーバーに存在しないユーザーをランキングから隠します"
    )
    @app_commands.guild_only()
    @require_admin()
    async def ranking_hide_absent(self, interaction: discord.Interaction, hide: bool) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="ranking_hide_absent", value=1 if hide else 0,
            label="退会ユーザーの非表示", display="非表示" if hide else "表示", refresh_ranking=True,
        )

    @app_commands.command(name="max_balance", description="1ユーザーが保持できる残高の上限を設定します")
    @app_commands.describe(amount="上限 (0で無制限)")
    @app_commands.guild_only()
    @require_admin()
    async def max_balance(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, 1_000_000_000],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="max_balance", value=int(amount), label="残高上限",
            display=utils.fmt_int(amount) if amount else "無制限",
        )

    @app_commands.command(
        name="manual_review_allow_new",
        description="確認中の取引がある利用者の新規チャージを許可します",
    )
    @app_commands.guild_only()
    @require_admin()
    async def manual_review_allow_new(
        self, interaction: discord.Interaction, allow: bool
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="manual_review_allow_new", value=1 if allow else 0,
            label="確認中でも新規チャージを許可",
            display="許可する (確認待ちで利用者をロックしない)" if allow else "許可しない (安全側)",
        )

    @app_commands.command(
        name="balance_log_channel", description="残高操作のログを送信するチャンネルを設定します"
    )
    @app_commands.guild_only()
    @require_admin()
    async def balance_log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        """残高変更を専用チャンネルへ記録する。

        他人の残高が見えるため、全員が閲覧できるチャンネルを指定した場合は確認を求める。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        if guild.me is None or not _channel_writable(channel, guild.me):
            await interaction.response.send_message(
                embed=ui.info_embed(
                    "権限が不足しています",
                    f"{channel.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        everyone_can_read = channel.permissions_for(guild.default_role).view_channel
        if everyone_can_read:
            approved = await _confirm(
                interaction,
                title="⚠️ このチャンネルは全員が閲覧できます",
                description=(
                    f"{channel.mention} は @everyone が閲覧可能です。\n"
                    "残高ログには**他の利用者の残高**が表示されます。\n"
                    "本当にこのチャンネルを指定しますか？"
                ),
                confirm_label="このまま設定する",
            )
            if not approved:
                return
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="balance_log_channel_id", value=channel.id,
            label="残高操作ログチャンネル", display=channel.mention,
        )
        await bot.charge.log_balance_change(
            guild.id, change_type=config.BalanceChangeType.RECONCILE,
            user_id=interaction.user.id, balance_before=0, balance_after=0, change=0,
            reason="残高ログチャンネルの設定テスト (この行はテスト送信です)",
            operator_id=interaction.user.id,
        )

    @app_commands.command(
        name="balance_log_scope", description="残高ログに含める範囲を設定します"
    )
    @app_commands.describe(scope="MANUAL=管理者の手動操作のみ / ALL=チャージ等の自動変動も含む")
    @app_commands.choices(scope=[
        app_commands.Choice(name="管理者の手動操作のみ (既定)", value="MANUAL"),
        app_commands.Choice(name="すべての残高変動", value="ALL"),
    ])
    @app_commands.guild_only()
    @require_admin()
    async def balance_log_scope(
        self, interaction: discord.Interaction, scope: app_commands.Choice[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="balance_log_scope", value=scope.value,
            label="残高ログの範囲", display=scope.name,
        )

    @app_commands.command(name="summary_channel", description="日次サマリの投稿先を設定します")
    @app_commands.guild_only()
    @require_admin()
    async def summary_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        if guild.me is None or not _channel_writable(channel, guild.me):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "権限が不足しています",
                    f"{channel.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await bot.db.update_settings(guild.id, summary_enabled=1)
        await self._apply(
            interaction, field="summary_channel_id", value=channel.id,
            label="日次サマリチャンネル",
            display=f"{channel.mention} (毎日 {config.SUMMARY_POST_HOUR:02d}:"
                    f"{config.SUMMARY_POST_MINUTE:02d} JST に前日分を投稿)",
        )

    @app_commands.command(name="summary_enabled", description="日次サマリの投稿を切り替えます")
    @app_commands.guild_only()
    @require_admin()
    async def summary_enabled(self, interaction: discord.Interaction, enabled: bool) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="summary_enabled", value=1 if enabled else 0,
            label="日次サマリ", display="有効" if enabled else "無効",
        )

    @app_commands.command(name="shop_enabled", description="ショップの有効/無効を切り替えます")
    @app_commands.guild_only()
    @require_admin()
    async def shop_enabled(self, interaction: discord.Interaction, enabled: bool) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._apply(
            interaction, field="shop_enabled", value=1 if enabled else 0,
            label="ショップ", display="有効" if enabled else "無効",
        )
        await bot.charge.refresh_shop_panels(interaction.guild.id)  # type: ignore[union-attr]

    @app_commands.command(name="panel_title", description="チャージパネルのタイトルを変更します")
    @app_commands.describe(title="空文字で既定値に戻します")
    @app_commands.guild_only()
    @require_admin()
    async def panel_title(self, interaction: discord.Interaction, title: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = title.strip()[:240] or None
        await self._apply(
            interaction, field="panel_title", value=value, label="パネルタイトル",
            display=value or "(既定値)", refresh_charge_panels=True,
        )

    @app_commands.command(
        name="panel_description", description="チャージパネルの説明文を変更します"
    )
    @app_commands.describe(description="空文字で既定値に戻します。\\n で改行できます")
    @app_commands.guild_only()
    @require_admin()
    async def panel_description(
        self, interaction: discord.Interaction, description: str
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = description.replace("\\n", "\n").strip()[:1500] or None
        await self._apply(
            interaction, field="panel_description", value=value, label="パネル説明文",
            display=utils.truncate(value or "(既定値)", 200), refresh_charge_panels=True,
        )

    @app_commands.command(name="accent_color", description="パネルの色を変更します")
    @app_commands.describe(color="16進数 (例: 1B1B1F)。default で既定値に戻します")
    @app_commands.guild_only()
    @require_admin()
    async def accent_color(self, interaction: discord.Interaction, color: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = color.strip().lstrip("#").lower()
        if text in ("default", "reset", ""):
            value: int | None = None
            display = "(既定値)"
        else:
            try:
                value = int(text, 16)
            except ValueError:
                await interaction.followup.send(
                    embed=ui.info_embed(
                        "入力が不正です", "`1B1B1F` のような16進数、または `default` を指定してください。",
                        color=config.Color.DANGER,
                    ),
                    ephemeral=True,
                )
                return
            if not 0 <= value <= 0xFFFFFF:
                await interaction.followup.send(
                    embed=ui.info_embed("入力が不正です", "000000〜FFFFFF の範囲で指定してください。",
                                        color=config.Color.DANGER),
                    ephemeral=True,
                )
                return
            display = f"#{value:06X}"
        await self._apply(
            interaction, field="accent_color", value=value, label="パネルの色",
            display=display, refresh_charge_panels=True,
        )


# ---------------------------------------------------------------------------
# /balance (Server Admin)
# ---------------------------------------------------------------------------
class BalanceGroup(app_commands.Group):
    """内部残高の管理操作。"""

    def __init__(self) -> None:
        super().__init__(name="balance", description="内部残高の管理 (管理者)")

    @app_commands.command(name="add", description="残高を加算します")
    @app_commands.describe(user="対象ユーザー", amount="加算する残高", reason="理由 (監査ログに記録)")
    @app_commands.guild_only()
    @require_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000_000],
        reason: str,
    ) -> None:
        await _do_balance_change(
            interaction, user, int(amount), reason, config.BalanceChangeType.ADMIN_ADD
        )

    @app_commands.command(name="remove", description="残高を減算します")
    @app_commands.describe(user="対象ユーザー", amount="減算する残高", reason="理由 (監査ログに記録)")
    @app_commands.guild_only()
    @require_admin()
    async def remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000_000],
        reason: str,
    ) -> None:
        await _do_balance_change(
            interaction, user, int(amount), reason, config.BalanceChangeType.ADMIN_REMOVE,
            confirm=True,
        )

    @app_commands.command(name="set", description="残高を指定値に設定します")
    @app_commands.describe(user="対象ユーザー", amount="設定する残高", reason="理由 (監査ログに記録)")
    @app_commands.guild_only()
    @require_admin()
    async def set_balance(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 0, 1_000_000_000],
        reason: str,
    ) -> None:
        await _do_balance_change(
            interaction, user, int(amount), reason, config.BalanceChangeType.ADMIN_SET,
            confirm=True,
        )

    @app_commands.command(name="move", description="残高を利用者間で付け替えます")
    @app_commands.describe(
        from_user="出金元", to_user="入金先", amount="付け替える残高", reason="理由 (監査ログに記録)"
    )
    @app_commands.guild_only()
    @require_admin()
    async def move(
        self,
        interaction: discord.Interaction,
        from_user: discord.Member,
        to_user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000_000],
        reason: str,
    ) -> None:
        """誤付与の是正などに使う (出金側の残高が不足していれば失敗する)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        if from_user.id == to_user.id:
            await interaction.response.send_message(
                embed=ui.info_embed("実行できません", "同一ユーザー間では付け替えできません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if to_user.bot:
            await interaction.response.send_message(
                embed=ui.info_embed("対象外です", "Bot へは付け替えできません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        src = await bot.db.get_balance(guild.id, from_user.id)
        dst = await bot.db.get_balance(guild.id, to_user.id)
        approved = await _confirm(
            interaction,
            title="⚠️ 残高の付け替えを実行します",
            description=(
                f"出金元: {from_user.mention} {utils.fmt_int(src)} → "
                f"{utils.fmt_int(max(0, src - int(amount)))}\n"
                f"入金先: {to_user.mention} {utils.fmt_int(dst)} → "
                f"{utils.fmt_int(dst + int(amount))}\n"
                f"金額: **{utils.fmt_int(int(amount))}**\n"
                f"理由: {utils.truncate(reason, 300)}"
            ),
            confirm_label="付け替える",
        )
        if not approved:
            return
        try:
            result = await bot.charge.move_balance(
                guild_id=guild.id, from_user_id=from_user.id, to_user_id=to_user.id,
                amount=int(amount), operator_id=interaction.user.id, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=ui.error_embed(
                    config.ErrorCode.DATABASE_ERROR, admin_detail=utils.safe_error_text(exc)
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 残高を付け替えました",
                f"出金元: {from_user.mention} "
                f"{utils.fmt_int(result['from_before'])} → **{utils.fmt_int(result['from_after'])}**\n"
                f"入金先: {to_user.mention} "
                f"{utils.fmt_int(result['to_before'])} → **{utils.fmt_int(result['to_after'])}**\n"
                f"操作ID: `{result['operation_id']}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="ledger", description="残高台帳を検索します")
    @app_commands.describe(
        user="対象ユーザー (省略時はサーバー全体)", type="変更種別",
        date="日付 (YYYY-MM-DD)", operator="操作者", page="ページ番号",
    )
    @app_commands.choices(type=[
        app_commands.Choice(name=label, value=key)
        for key, label in list(config.BALANCE_TYPE_LABELS.items())[:25]
    ])
    @app_commands.guild_only()
    @require_admin()
    async def ledger(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        type: app_commands.Choice[str] | None = None,
        date: str | None = None,
        operator: discord.Member | None = None,
        page: app_commands.Range[int, 1, 500] = 1,
    ) -> None:
        """残高変更履歴を種別・期間・操作者で絞り込んで表示する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        date_from = _parse_date(date)
        if date and date_from is None:
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "日付は `YYYY-MM-DD` 形式で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        per_page = 8
        rows, total = await bot.db.list_balance_history_filtered(
            guild_id,
            user_id=user.id if user else None,
            types=(type.value,) if type else None,
            operator_id=operator.id if operator else None,
            date_from=date_from,
            date_to=date_from + 86400 if date_from is not None else None,
            offset=(page - 1) * per_page,
            limit=per_page,
        )
        total_pages = max(1, -(-total // per_page))
        lines: list[str] = []
        for row in rows:
            change = int(row["change_amount"])
            sign = "+" if change > 0 else ""
            lines.append(
                f"`{row['id']}` {utils.format_jst(int(row['created_at']), with_seconds=True)} "
                f"[{config.BALANCE_TYPE_LABELS.get(str(row['type']), str(row['type']))}]\n"
                f"　<@{int(row['user_id'])}> **{sign}{utils.fmt_int(change)}** "
                f"({utils.fmt_int(int(row['balance_before']))} → {utils.fmt_int(int(row['balance_after']))})"
                + (f" / 操作者 <@{int(row['operator_id'])}>" if row["operator_id"] else "")
                + (f"\n　理由: {utils.truncate(str(row['reason']), 80)}" if row["reason"] else "")
                + (f"\n　取消済み → 履歴 `{row['undo_of']}` の逆仕訳" if row["undo_of"] else "")
            )
        embed = ui.info_embed(
            "📒 残高台帳",
            f"{ui.SEPARATOR}\n該当 **{utils.fmt_int(total)}** 件 / ページ {page}/{total_pages}\n\n"
            + ("\n".join(lines) if lines else "該当する履歴はありません。"),
        )
        embed.set_footer(text="`/balance undo <履歴ID>` で1件ずつ取り消せます")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="undo", description="残高変更履歴1件を取り消します")
    @app_commands.describe(history_id="履歴ID (/balance ledger で確認)", reason="理由")
    @app_commands.guild_only()
    @require_admin()
    async def undo(
        self, interaction: discord.Interaction,
        history_id: app_commands.Range[int, 1, 10_000_000_000], reason: str,
    ) -> None:
        """逆仕訳で取り消す (元の履歴は書き換えず、二重取消もできない)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        entry = await bot.db.get_balance_history_entry(int(history_id), guild_id)
        if entry is None:
            await interaction.response.send_message(
                embed=ui.info_embed("見つかりません", "指定された履歴IDはこのサーバーに存在しません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        change = int(entry["change_amount"])
        current = await bot.db.get_balance(guild_id, int(entry["user_id"]))
        approved = await _confirm(
            interaction,
            title="⚠️ 残高操作を取り消します",
            description=(
                f"履歴ID: `{history_id}`\n"
                f"対象: <@{int(entry['user_id'])}>\n"
                f"種別: {config.BALANCE_TYPE_LABELS.get(str(entry['type']), str(entry['type']))}\n"
                f"元の変動: **{'+' if change > 0 else ''}{utils.fmt_int(change)}**\n"
                f"現在残高: {utils.fmt_int(current)} → "
                f"**{utils.fmt_int(max(0, current - change))}** (予定)\n"
                f"理由: {utils.truncate(reason, 300)}"
            ),
            confirm_label="取り消す",
        )
        if not approved:
            return
        try:
            result = await bot.charge.undo_balance_change(
                guild_id=guild_id, history_id=int(history_id),
                operator_id=interaction.user.id, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=ui.info_embed("取り消せませんでした", utils.safe_error_text(exc, limit=400),
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 残高操作を取り消しました",
                f"対象: <@{result['user_id']}>\n"
                f"適用差分: **{utils.fmt_int(result['applied_change'])}**\n"
                f"残高: {utils.fmt_int(result['balance_before'])} → "
                f"**{utils.fmt_int(result['balance_after'])}**\n"
                f"取消の履歴ID: `{result['undo_history_id']}` / 操作ID: `{result['operation_id']}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="audit", description="残高と履歴合計を突合します")
    @app_commands.describe(user="対象ユーザー")
    @app_commands.guild_only()
    @require_admin()
    async def audit(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """残高 (balances) と履歴合計 (balance_history) の一致を確認する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        result = await bot.db.audit_balance(guild_id, user.id)
        consistent = result["diff"] == 0
        embed = ui.info_embed(
            "🧮 残高の突合結果",
            f"{ui.SEPARATOR}\n対象: {user.mention}\n"
            + ("🟢 一致しています" if consistent else "🔴 **不一致を検出しました**")
            + f"\n{ui.SEPARATOR}",
            color=config.Color.SUCCESS if consistent else config.Color.DANGER,
        )
        embed.add_field(name="現在残高 (balances)", value=utils.fmt_int(result["actual"]), inline=True)
        embed.add_field(name="履歴合計 (balance_history)", value=utils.fmt_int(result["expected"]), inline=True)
        embed.add_field(name="差分", value=f"**{utils.fmt_int(result['diff'])}**", inline=True)
        embed.add_field(name="履歴件数", value=f"{result['entries']}件", inline=True)
        if result["breakdown"]:
            embed.add_field(
                name="種別ごとの内訳",
                value="\n".join(
                    f"{config.BALANCE_TYPE_LABELS.get(b['type'], b['type'])}: "
                    f"{utils.fmt_int(b['total'])} ({b['count']}件)"
                    for b in result["breakdown"][:12]
                ),
                inline=False,
            )
        if not consistent:
            embed.add_field(
                name="修復方法",
                value=(
                    "`/balance repair user: mode:history` → 履歴に差分を追記 (残高はそのまま)\n"
                    "`/balance repair user: mode:balance` → 残高を履歴合計へ戻す"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="repair", description="残高と履歴合計の不一致を修復します")
    @app_commands.describe(
        user="対象ユーザー", mode="history=履歴を残高に合わせる / balance=残高を履歴に合わせる",
        reason="理由 (監査ログに記録)",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="history: 履歴へ差分を追記 (残高は変えない)", value="history"),
        app_commands.Choice(name="balance: 残高を履歴合計へ戻す", value="balance"),
    ])
    @app_commands.guild_only()
    @require_owner()
    async def repair(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        mode: app_commands.Choice[str],
        reason: str,
    ) -> None:
        """不一致の修復 (Bot Owner 限定)。元の履歴は書き換えない。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        audit = await bot.db.audit_balance(guild_id, user.id)
        if audit["diff"] == 0:
            await interaction.response.send_message(
                embed=ui.info_embed("修復は不要です", "残高と履歴合計は一致しています。"),
                ephemeral=True,
            )
            return
        approved = await _confirm(
            interaction,
            title="⚠️ 残高の突合修復を実行します",
            description=(
                f"対象: {user.mention}\n"
                f"現在残高: {utils.fmt_int(audit['actual'])}\n"
                f"履歴合計: {utils.fmt_int(audit['expected'])}\n"
                f"差分: **{utils.fmt_int(audit['diff'])}**\n"
                f"mode: `{mode.value}`\n\n"
                + (
                    "履歴へ差分行を追記します (残高は変わりません)。"
                    if mode.value == "history"
                    else f"**残高を {utils.fmt_int(audit['expected'])} へ変更します。**"
                )
            ),
            confirm_label="修復する",
            stages=2,
        )
        if not approved:
            return
        result = await bot.charge.repair_balance(
            guild_id=guild_id, user_id=user.id, operator_id=interaction.user.id,
            reason=reason, mode=mode.value,
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 修復しました",
                f"mode: `{result.get('mode')}`\n"
                f"残高 {utils.fmt_int(result['actual'])} / 履歴合計 "
                f"{utils.fmt_int(result['expected'])} (差分 {utils.fmt_int(result['diff'])})\n"
                f"操作ID: `{result.get('operation_id')}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="distribution", description="残高の分布を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def distribution(self, interaction: discord.Interaction) -> None:
        """管理者向けの残高分布 (凍結・残高0も含む実態)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        dist = await bot.db.get_balance_distribution(guild_id)
        embed = ui.info_embed(
            "📊 残高の分布",
            f"{ui.SEPARATOR}\n保有者 **{dist['count']}** 人 / 残高合計 "
            f"**{utils.fmt_int(dist['total'])}**\n{ui.SEPARATOR}",
        )
        embed.add_field(name="中位値", value=utils.fmt_int(dist["median"]), inline=True)
        embed.add_field(name="最大", value=utils.fmt_int(dist["max"]), inline=True)
        embed.add_field(name="残高0", value=f"{dist['zero']}人", inline=True)
        embed.add_field(name="上位10%の占有率", value=f"{dist['top10_share']:.1f}%", inline=True)
        embed.add_field(name="発行総額", value=utils.fmt_int(dist["issued"]), inline=True)
        embed.add_field(name="消費総額", value=utils.fmt_int(dist["spent"]), inline=True)
        embed.set_footer(text="発行総額 − 消費総額 = 現在の残高合計 (理論値)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="bulk", description="ロール保持者へ一括で残高を操作します")
    @app_commands.describe(
        role="対象ロール", operation="操作", amount="金額", reason="理由",
        dry_run="True で対象と総額のプレビューのみ (既定)",
    )
    @app_commands.choices(operation=[
        app_commands.Choice(name="加算", value=config.BalanceChangeType.ADMIN_ADD),
        app_commands.Choice(name="減算", value=config.BalanceChangeType.ADMIN_REMOVE),
        app_commands.Choice(name="設定", value=config.BalanceChangeType.ADMIN_SET),
    ])
    @app_commands.guild_only()
    @require_admin()
    async def bulk(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        operation: app_commands.Choice[str],
        amount: app_commands.Range[int, 0, 1_000_000_000],
        reason: str,
        dry_run: bool = True,
    ) -> None:
        """一括残高操作 (既定はドライラン。実行前に必ずプレビューを表示する)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        targets = [m for m in role.members if not m.bot]
        if not targets:
            await interaction.response.send_message(
                embed=ui.info_embed("対象がいません", f"{role.mention} を持つ利用者がいません。"),
                ephemeral=True,
            )
            return
        label = config.BALANCE_TYPE_LABELS.get(operation.value, operation.value)
        total = int(amount) * len(targets)
        preview = (
            f"ロール: {role.mention}\n"
            f"対象人数: **{len(targets)}人**\n"
            f"操作: **{label}** {utils.fmt_int(int(amount))}\n"
            + (f"総額 (概算): **{utils.fmt_int(total)}**\n"
               if operation.value != config.BalanceChangeType.ADMIN_SET else "")
            + f"理由: {utils.truncate(reason, 200)}"
        )
        if dry_run:
            await interaction.response.send_message(
                embed=ui.info_embed(
                    "🔍 ドライラン (実行していません)",
                    preview + "\n\n実行するには `dry_run: False` を指定してください。",
                    color=config.Color.WARNING,
                ),
                ephemeral=True,
            )
            return
        approved = await _confirm(
            interaction,
            title="⚠️ 一括残高操作を実行します",
            description=preview + "\n\n**この操作は多数の利用者の残高を変更します。**",
            confirm_label=f"{len(targets)}人に実行する",
            stages=2,
        )
        if not approved:
            return
        succeeded = 0
        failed = 0
        for member in targets:
            try:
                await bot.charge.admin_adjust_balance(
                    guild_id=guild.id, user_id=member.id, change_type=operation.value,
                    amount=int(amount), operator_id=interaction.user.id,
                    reason=f"[一括] {reason}",
                )
                succeeded += 1
            except Exception:  # noqa: BLE001 - 1人の失敗で全体を止めない
                logger.exception("一括残高操作に失敗しました user=%s", member.id)
                failed += 1
        await _audit(
            interaction, "BALANCE_BULK",
            detail={"role_id": role.id, "operation": operation.value, "amount": int(amount),
                    "targets": len(targets), "succeeded": succeeded, "failed": failed,
                    "reason": utils.truncate(reason, 300)},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 一括操作を実行しました",
                f"成功: **{succeeded}人** / 失敗: {failed}人\n"
                f"操作: {label} {utils.fmt_int(int(amount))}",
            ),
            ephemeral=True,
        )


async def _do_balance_change(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    reason: str,
    change_type: str,
    *,
    confirm: bool = False,
) -> None:
    """残高操作の共通処理 (危険操作は確認ボタンを表示)。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    guild = interaction.guild
    assert guild is not None
    if user.bot:
        await interaction.response.send_message(
            embed=ui.info_embed("対象外です", "Bot に残高を設定することはできません。",
                                color=config.Color.DANGER),
            ephemeral=True,
        )
        return
    current = await bot.db.get_balance(guild.id, user.id)
    label = config.BALANCE_TYPE_LABELS.get(change_type, change_type)
    if confirm:
        if change_type == config.BalanceChangeType.ADMIN_SET:
            after_preview = amount
        else:
            after_preview = max(0, current - amount)
        approved = await _confirm(
            interaction,
            title=f"⚠️ 残高{label}を実行します",
            description=(
                f"対象: {user.mention}\n"
                f"現在残高: **{utils.fmt_int(current)}**\n"
                f"実行後 (予定): **{utils.fmt_int(after_preview)}**\n"
                f"理由: {utils.truncate(reason, 300)}"
            ),
            confirm_label="実行する",
        )
        if not approved:
            return
    else:
        await interaction.response.defer(ephemeral=True, thinking=True)

    result = await bot.charge.admin_adjust_balance(
        guild_id=guild.id, user_id=user.id, change_type=change_type,
        amount=amount, operator_id=interaction.user.id, reason=reason,
    )
    await interaction.followup.send(
        embed=ui.success_embed(
            f"✅ 残高{label}を実行しました",
            f"対象: {user.mention}\n"
            f"残高: **{utils.fmt_int(result['balance_before'])}** → "
            f"**{utils.fmt_int(result['balance_after'])}** "
            f"({'+' if result['change'] >= 0 else ''}{utils.fmt_int(result['change'])})\n"
            f"理由: {utils.truncate(reason, 300)}",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /user (Server Admin)
# ---------------------------------------------------------------------------
class UserGroup(app_commands.Group):
    """利用者の凍結管理。"""

    def __init__(self) -> None:
        super().__init__(name="user", description="利用者の管理 (管理者)")

    @app_commands.command(name="freeze", description="ユーザーのチャージを停止します")
    @app_commands.describe(user="対象ユーザー", reason="理由")
    @app_commands.guild_only()
    @require_admin()
    async def freeze(
        self, interaction: discord.Interaction, user: discord.Member, reason: str
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        await bot.db.set_frozen(guild_id, user.id, True, interaction.user.id, reason)
        op_id = await _audit(interaction, "USER_FREEZE", target_user_id=user.id,
                             detail={"reason": reason})
        bot.charge.request_ranking_refresh(guild_id)  # 凍結ユーザーはランキング対象外
        await bot.charge.log_event(
            guild_id, "🧊 ユーザーを凍結しました",
            fields=(("対象", user.mention, True), ("操作者", interaction.user.mention, True),
                    ("理由", utils.truncate(reason, 200), False), ("操作ID", f"`{op_id}`", True)),
            color=config.Color.WARNING,
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "🧊 凍結しました",
                f"対象: {user.mention}\n理由: {utils.truncate(reason, 300)}\n"
                "残高確認はできますが、新規チャージはできません。\n操作ID: `" + op_id + "`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="unfreeze", description="ユーザーの凍結を解除します")
    @app_commands.describe(user="対象ユーザー", reason="理由")
    @app_commands.guild_only()
    @require_admin()
    async def unfreeze(
        self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        await bot.db.set_frozen(guild_id, user.id, False, interaction.user.id, reason)
        op_id = await _audit(interaction, "USER_UNFREEZE", target_user_id=user.id,
                             detail={"reason": reason})
        bot.charge.request_ranking_refresh(guild_id)
        await bot.charge.log_event(
            guild_id, "🟢 ユーザーの凍結を解除しました",
            fields=(("対象", user.mention, True), ("操作者", interaction.user.mention, True),
                    ("操作ID", f"`{op_id}`", True)),
            color=config.Color.SUCCESS,
        )
        await interaction.followup.send(
            embed=ui.success_embed("🟢 凍結を解除しました", f"対象: {user.mention}\n操作ID: `{op_id}`"),
            ephemeral=True,
        )

    @app_commands.command(name="cooldown", description="連続失敗によるクールダウンを解除します")
    @app_commands.describe(user="対象ユーザー")
    @app_commands.guild_only()
    @require_admin()
    async def cooldown(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """失敗が続いて一時制限された利用者の制限を解除する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        remaining = bot.charge.cooldown_remaining(guild_id, user.id)
        bot.charge.clear_cooldown(guild_id, user.id)
        await _audit(interaction, "USER_COOLDOWN_CLEAR", target_user_id=user.id,
                     detail={"remaining_seconds": remaining})
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ クールダウンを解除しました" if remaining else "クールダウンはありません",
                f"対象: {user.mention}\n"
                + (f"残っていた制限: {remaining}秒" if remaining else "制限はかかっていませんでした。"),
            ),
            ephemeral=True,
        )

# ---------------------------------------------------------------------------
# /maintenance, /emergency_stop (Server Admin)
# ---------------------------------------------------------------------------
class MaintenanceGroup(app_commands.Group):
    """メンテナンスモード。"""

    def __init__(self) -> None:
        super().__init__(name="maintenance", description="メンテナンスモードの切替 (管理者)")

    @app_commands.command(name="on", description="メンテナンスを開始します (新規チャージ停止)")
    @app_commands.guild_only()
    @require_admin()
    async def on(self, interaction: discord.Interaction) -> None:
        await _set_mode(interaction, field="maintenance", value=True,
                        label="メンテナンス", action="MAINTENANCE_ON")

    @app_commands.command(name="off", description="メンテナンスを終了します")
    @app_commands.guild_only()
    @require_admin()
    async def off(self, interaction: discord.Interaction) -> None:
        await _set_mode(interaction, field="maintenance", value=False,
                        label="メンテナンス", action="MAINTENANCE_OFF")


class EmergencyStopGroup(app_commands.Group):
    """緊急停止。"""

    def __init__(self) -> None:
        super().__init__(name="emergency_stop", description="緊急停止の切替 (管理者)")

    @app_commands.command(name="on", description="緊急停止します (新規チャージ・新規受取を停止)")
    @app_commands.guild_only()
    @require_admin()
    async def on(self, interaction: discord.Interaction) -> None:
        approved = await _confirm(
            interaction,
            title="🚨 緊急停止を実行します",
            description=(
                "・新規チャージを受け付けなくなります\n"
                "・受取キューの処理を停止します (キューは保持されます)\n"
                "・残高確認・ランキングなどの読み取り機能は継続します\n\n"
                "解除するまでチャージは行われません。"
            ),
            confirm_label="緊急停止する",
        )
        if not approved:
            return
        await _set_mode(interaction, field="emergency_stop", value=True,
                        label="緊急停止", action="EMERGENCY_STOP_ON", already_responded=True)

    @app_commands.command(name="off", description="緊急停止を解除します")
    @app_commands.guild_only()
    @require_admin()
    async def off(self, interaction: discord.Interaction) -> None:
        await _set_mode(interaction, field="emergency_stop", value=False,
                        label="緊急停止", action="EMERGENCY_STOP_OFF")


async def _set_mode(
    interaction: discord.Interaction,
    *,
    field: str,
    value: bool,
    label: str,
    action: str,
    already_responded: bool = False,
) -> None:
    """メンテナンス / 緊急停止の状態を更新する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    guild = interaction.guild
    assert guild is not None
    if not already_responded:
        await interaction.response.defer(ephemeral=True, thinking=True)
    await bot.db.update_settings(guild.id, **{field: 1 if value else 0})
    op_id = await _audit(interaction, action, detail={field: value})
    await bot.charge.refresh_charge_panels(guild.id)
    if not value:
        bot.charge.queue_wakeup.set()  # 解除時は保留中のキューを再開する
    await bot.charge.log_event(
        guild.id, f"{'🔴' if value else '🟢'} {label}を{'開始' if value else '解除'}しました",
        fields=(("操作者", interaction.user.mention, True), ("操作ID", f"`{op_id}`", True)),
        color=config.Color.DANGER if value else config.Color.SUCCESS,
    )
    await interaction.followup.send(
        embed=ui.success_embed(
            f"{'🔴' if value else '🟢'} {label}を{'開始' if value else '解除'}しました",
            f"操作ID: `{op_id}`",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /achievement (Server Admin)
# ---------------------------------------------------------------------------
class AchievementGroup(app_commands.Group):
    """実績の管理。"""

    def __init__(self) -> None:
        super().__init__(name="achievement", description="実績の管理 (管理者)")

    @app_commands.command(name="proxy", description="確認済みの取引について実績を代理投稿します")
    @app_commands.describe(
        user="対象ユーザー",
        amount="送金額 (円)",
        reason="理由 (監査ログに記録)",
        charge_rate="チャージ率 (省略時は現在の設定値)",
        credited_amount="獲得残高 (省略時はチャージ率から自動計算)",
    )
    @app_commands.guild_only()
    @require_admin()
    async def proxy(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, config.AMOUNT_HARD_MAX],
        reason: str,
        charge_rate: str | None = None,
        credited_amount: app_commands.Range[int, 0, 1_000_000_000] | None = None,
    ) -> None:
        """管理者が確認済みの正当な取引を実績として登録する。

        自動チャージと同じ見た目で投稿するが、DB 上は ``source='ADMIN_PROXY'``
        として区別し、監査ログへ操作者・対象・金額・理由・操作IDを記録する。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        if user.bot:
            await interaction.response.send_message(
                embed=ui.info_embed("対象外です", "Bot を対象にはできません。", color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        settings = await bot.db.get_settings(guild.id)
        rate = settings.charge_rate
        if charge_rate is not None:
            parsed = utils.validate_charge_rate(charge_rate)
            if parsed is None:
                await interaction.response.send_message(
                    embed=ui.info_embed(
                        "入力が不正です",
                        f"チャージ率は {config.MIN_CHARGE_RATE}〜{config.MAX_CHARGE_RATE} で指定してください。",
                        color=config.Color.DANGER,
                    ),
                    ephemeral=True,
                )
                return
            rate = parsed
        credited = (
            int(credited_amount) if credited_amount is not None
            else utils.calc_credited_amount(int(amount), rate)
        )
        approved = await _confirm(
            interaction,
            title="⚠️ 代理実績を登録します",
            description=(
                f"対象: {user.mention}\n"
                f"送金額: **{utils.fmt_yen(int(amount))}**\n"
                f"チャージ率: **{utils.fmt_rate(rate)}**\n"
                f"付与する残高: **{utils.fmt_int(credited)}**\n"
                f"理由: {utils.truncate(reason, 300)}\n\n"
                "この操作は残高を増加させ、実績チャンネルへ投稿されます。"
            ),
            confirm_label="登録する",
        )
        if not approved:
            return
        result = await bot.charge.create_proxy_achievement(
            guild_id=guild.id, user_id=user.id, amount=int(amount), charge_rate=rate,
            credited_amount=credited, operator_id=interaction.user.id, reason=reason,
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 代理実績を登録しました",
                f"対象: {user.mention}\n"
                f"取引ID: `{result['transaction_id']}`\n"
                f"残高: **{utils.fmt_int(result['balance_before'])}** → "
                f"**{utils.fmt_int(result['balance_after'])}**\n"
                f"操作ID: `{result['operation_id']}`",
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /transaction (Server Admin) — MANUAL_REVIEW の確定など
# ---------------------------------------------------------------------------
class TransactionGroup(app_commands.Group):
    """個別取引の確認と確定。"""

    def __init__(self) -> None:
        super().__init__(name="transaction", description="取引の確認と確定 (管理者)")

    @app_commands.command(name="info", description="取引の詳細を表示します")
    @app_commands.describe(transaction_id="取引ID (例: TX-XXXXXXXX)")
    @app_commands.guild_only()
    @require_admin()
    async def info(self, interaction: discord.Interaction, transaction_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await _fetch_transaction_for_guild(interaction, transaction_id)
        if row is None:
            return
        embed = ui.info_embed(
            f"🧾 取引 `{row['id']}`",
            f"{ui.SEPARATOR}\n状態: "
            f"{config.STATUS_EMOJI.get(row['status'], '⚪')} "
            f"**{config.STATUS_LABELS.get(row['status'], row['status'])}**",
        )
        embed.add_field(name="利用者", value=f"<@{row['user_id']}>", inline=True)
        embed.add_field(name="申請額", value=utils.fmt_yen(int(row["requested_amount"])), inline=True)
        embed.add_field(
            name="受取額",
            value=utils.fmt_yen(int(row["received_amount"])) if row["received_amount"] is not None else "-",
            inline=True,
        )
        embed.add_field(name="チャージ率", value=utils.fmt_rate(row["charge_rate"]), inline=True)
        embed.add_field(
            name="付与残高",
            value=utils.fmt_int(int(row["credited_amount"])) if row["credited_amount"] is not None else "-",
            inline=True,
        )
        source = str(row["source"] or "")
        embed.add_field(
            name="種別",
            value=config.TX_SOURCE_LABELS.get(source, source or "-"),
            inline=True,
        )
        embed.add_field(
            name="残高推移",
            value=(
                f"{utils.fmt_int(row['balance_before'])} → {utils.fmt_int(row['balance_after'])}"
                if row["balance_after"] is not None else "-"
            ),
            inline=True,
        )
        embed.add_field(name="リトライ回数", value=str(row["retry_count"]), inline=True)
        embed.add_field(
            name="リンク識別子",
            value=f"`{utils.mask_identifier(row['link_uuid'], keep=8)}`" if row["link_uuid"] else "-",
            inline=True,
        )
        embed.add_field(name="作成", value=utils.format_jst(row["created_at"], with_seconds=True), inline=True)
        embed.add_field(name="更新", value=utils.format_jst(row["updated_at"], with_seconds=True), inline=True)
        embed.add_field(
            name="完了",
            value=utils.format_jst(row["completed_at"], with_seconds=True) if row["completed_at"] else "-",
            inline=True,
        )
        if row["error_code"]:
            embed.add_field(
                name="エラー",
                value=f"`{row['error_code']}`\n{utils.truncate(str(row['error_message'] or '-'), 800)}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="verify", description="Kyash側の受取状態を再確認します")
    @app_commands.describe(transaction_id="取引ID")
    @app_commands.guild_only()
    @require_admin()
    async def verify(self, interaction: discord.Interaction, transaction_id: str) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await _fetch_transaction_for_guild(interaction, transaction_id)
        if row is None:
            return
        try:
            verification = await bot.charge.verify_transaction(str(row["id"]))
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        verdict_labels = {
            kyash_service.Verdict.CONFIRMED: "🟢 受取済みを確認",
            kyash_service.Verdict.NO_EVIDENCE: "🔴 受取の痕跡なし",
            kyash_service.Verdict.UNAVAILABLE: "🟠 判定不能 (情報取得不可)",
        }
        embed = ui.info_embed(
            "🔎 受取状態の確認結果",
            f"{ui.SEPARATOR}\n取引: `{row['id']}`\n"
            f"判定: **{verdict_labels.get(verification.verdict, verification.verdict)}**\n"
            f"詳細: {utils.truncate(utils.sanitize_for_log(verification.detail), 500)}",
            color=(
                config.Color.SUCCESS if verification.verdict == kyash_service.Verdict.CONFIRMED
                else config.Color.WARNING
            ),
        )
        if verification.verdict == kyash_service.Verdict.CONFIRMED and row["status"] in (
            config.TxStatus.MANUAL_REVIEW, config.TxStatus.RECEIVED, config.TxStatus.CREDITING
        ):
            embed.add_field(
                name="次の操作",
                value=f"`/transaction resolve transaction_id:{row['id']} complete:True` で完了できます。",
                inline=False,
            )
        elif verification.verdict == kyash_service.Verdict.NO_EVIDENCE and row["status"] == config.TxStatus.MANUAL_REVIEW:
            embed.add_field(
                name="次の操作",
                value=(
                    f"未受取のため `/transaction retry transaction_id:{row['id']}` で再受取、"
                    f"または `complete:False` で失敗確定できます。"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="resolve", description="確認中の取引を完了/失敗で確定します")
    @app_commands.describe(transaction_id="取引ID", complete="True=完了として残高付与 / False=失敗確定",
                           reason="理由 (監査ログに記録)")
    @app_commands.guild_only()
    @require_admin()
    async def resolve(
        self, interaction: discord.Interaction, transaction_id: str, complete: bool, reason: str
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        row = await _fetch_transaction_for_guild(interaction, transaction_id)
        if row is None:
            return
        approved = await _confirm(
            interaction,
            title=f"⚠️ 取引を{'完了' if complete else '失敗'}として確定します",
            description=(
                f"取引: `{row['id']}`\n利用者: <@{row['user_id']}>\n"
                f"受取額: {utils.fmt_yen(int(row['received_amount'] or row['requested_amount']))}\n\n"
                + (
                    "**Kyash アプリ等で実際に受け取れていることを必ず確認してから実行してください。**\n"
                    "完了にすると内部残高が付与されます。"
                    if complete else "失敗として確定し、利用者へ失敗通知が送られます。"
                )
            ),
            confirm_label="完了にする" if complete else "失敗にする",
        )
        if not approved:
            return
        try:
            result = await bot.charge.resolve_manual_review(
                str(row["id"]), complete=complete, operator_id=interaction.user.id, reason=reason
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                f"✅ 取引を{'完了' if complete else '失敗'}として確定しました",
                f"取引: `{row['id']}`\n操作ID: `{result['operation_id']}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="refund", description="完了済みチャージを取り消して残高を回収します")
    @app_commands.describe(transaction_id="取引ID", reason="理由 (監査ログに記録)")
    @app_commands.guild_only()
    @require_admin()
    async def refund(
        self, interaction: discord.Interaction, transaction_id: str, reason: str
    ) -> None:
        """完了済みチャージの取消 (原取引に紐づく逆仕訳として記録する)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        row = await _fetch_transaction_for_guild(interaction, transaction_id)
        if row is None:
            return
        if row["status"] != config.TxStatus.COMPLETED:
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed(
                    "取消できません",
                    f"完了済みの取引のみ取消できます (現在: "
                    f"{config.STATUS_LABELS.get(str(row['status']), str(row['status']))})。",
                    color=config.Color.DANGER,
                ),
            )
            return
        if row["refunded_at"]:
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed(
                    "既に取消済みです",
                    f"{utils.format_jst(int(row['refunded_at']))} に取消されています。",
                    color=config.Color.DANGER,
                ),
            )
            return
        credited = int(row["credited_amount"] or 0)
        current = await bot.db.get_balance(int(row["guild_id"]), int(row["user_id"]))
        approved = await _confirm(
            interaction,
            title="⚠️ チャージを取り消します",
            description=(
                f"取引ID: `{row['id']}`\n"
                f"対象: <@{int(row['user_id'])}>\n"
                f"送金額: {utils.fmt_yen(int(row['received_amount'] or 0))}\n"
                f"回収する残高: **{utils.fmt_int(credited)}**\n"
                f"残高: {utils.fmt_int(current)} → **{utils.fmt_int(max(0, current - credited))}**\n"
                f"理由: {utils.truncate(reason, 300)}\n\n"
                "利用者へは取消の通知が送られます。Kyash 側の受取は取り消されません。"
            ),
            confirm_label="取り消す",
        )
        if not approved:
            return
        try:
            result = await bot.charge.refund_charge_transaction(
                str(row["id"]), operator_id=interaction.user.id, reason=reason
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=ui.info_embed("取消できませんでした", utils.safe_error_text(exc, limit=400),
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ チャージを取り消しました",
                f"取引ID: `{row['id']}`\n"
                f"残高: {utils.fmt_int(result['balance_before'])} → "
                f"**{utils.fmt_int(result['balance_after'])}**\n"
                f"操作ID: `{result['operation_id']}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="retry", description="未受取を確認済みの取引を再度受取キューへ戻します")
    @app_commands.describe(transaction_id="取引ID")
    @app_commands.guild_only()
    @require_admin()
    async def retry(self, interaction: discord.Interaction, transaction_id: str) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await _fetch_transaction_for_guild(interaction, transaction_id)
        if row is None:
            return
        try:
            await bot.charge.requeue_manual_review(str(row["id"]), interaction.user.id)
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "🔁 受取キューへ戻しました",
                f"取引: `{row['id']}`\n未受取であることを確認したうえで再試行します。",
            ),
            ephemeral=True,
        )


async def _fetch_transaction_for_guild(
    interaction: discord.Interaction, transaction_id: str
) -> Any:
    """取引を取得する。他サーバーの取引は Bot Owner 以外参照できない。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    row = await bot.db.get_transaction(transaction_id.strip().upper())
    responder = interaction.followup.send if interaction.response.is_done() else None
    if row is None:
        embed = ui.info_embed("見つかりません", "指定された取引IDは存在しません。", color=config.Color.DANGER)
        if responder:
            await responder(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return None
    if interaction.guild is not None and int(row["guild_id"]) != interaction.guild.id:
        if not bot.is_bot_owner(interaction.user):
            embed = ui.info_embed(
                "参照できません", "この取引は別のサーバーのものです。", color=config.Color.DANGER
            )
            if responder:
                await responder(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            return None
    return row


# ---------------------------------------------------------------------------
# /history (管理者向け取引検索)
# ---------------------------------------------------------------------------
@app_commands.command(name="history", description="取引履歴を検索します (管理者)")
@app_commands.describe(
    user="対象ユーザー", transaction_id="取引ID", status="状態", date="日付 (YYYY-MM-DD)",
    amount_min="最小金額", amount_max="最大金額", page="ページ番号",
)
@app_commands.choices(status=STATUS_CHOICES)
@app_commands.guild_only()
@require_admin()
async def history_command(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    transaction_id: str | None = None,
    status: app_commands.Choice[str] | None = None,
    date: str | None = None,
    amount_min: app_commands.Range[int, 0, config.AMOUNT_HARD_MAX] | None = None,
    amount_max: app_commands.Range[int, 0, config.AMOUNT_HARD_MAX] | None = None,
    page: app_commands.Range[int, 1, 500] = 1,
) -> None:
    """管理者向けの全取引検索 (このサーバー内のみ / ページング)。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild_id = interaction.guild.id  # type: ignore[union-attr]
    date_from = _parse_date(date)
    if date and date_from is None:
        await interaction.followup.send(
            embed=ui.info_embed("入力が不正です", "日付は `YYYY-MM-DD` 形式で指定してください。",
                                color=config.Color.DANGER),
            ephemeral=True,
        )
        return
    date_to = date_from + 86400 if date_from is not None else None
    per_page = 5
    rows, total = await bot.db.search_transactions(
        guild_id=guild_id,
        user_id=user.id if user else None,
        tx_id=transaction_id.strip().upper() if transaction_id else None,
        status=status.value if status else None,
        date_from=date_from,
        date_to=date_to,
        amount_min=int(amount_min) if amount_min is not None else None,
        amount_max=int(amount_max) if amount_max is not None else None,
        offset=(page - 1) * per_page,
        limit=per_page,
    )
    total_pages = max(1, -(-total // per_page))
    embed = ui.info_embed(
        "🔍 取引検索",
        f"{ui.SEPARATOR}\n該当 **{utils.fmt_int(total)}** 件 / ページ {page}/{total_pages}",
    )
    if not rows:
        embed.add_field(name="結果", value="該当する取引はありません。", inline=False)
    for row in rows:
        embed.add_field(
            name=f"{config.STATUS_EMOJI.get(row['status'], '⚪')} `{row['id']}` "
                 f"({config.STATUS_LABELS.get(row['status'], row['status'])})",
            value=(
                f"利用者: <@{row['user_id']}>\n"
                f"申請 {utils.fmt_yen(int(row['requested_amount']))} / "
                f"受取 {utils.fmt_yen(int(row['received_amount'])) if row['received_amount'] is not None else '-'} / "
                f"付与 {utils.fmt_int(int(row['credited_amount'])) if row['credited_amount'] is not None else '-'}\n"
                f"率 {utils.fmt_rate(row['charge_rate'])} / {utils.format_jst(row['created_at'])}"
                + (f"\nエラー: `{row['error_code']}`" if row["error_code"] else "")
            ),
            inline=False,
        )
    embed.set_footer(text="page パラメータで次のページを表示できます")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /stats, /queue, /system, /config, /logs
# ---------------------------------------------------------------------------
@app_commands.command(name="stats", description="チャージ統計を表示します (管理者)")
@app_commands.describe(
    global_scope="Bot全体の統計を表示 (Bot Owner のみ)",
    days="日別推移グラフの日数 (0でグラフなし)",
)
@app_commands.guild_only()
@require_admin()
async def stats_command(
    interaction: discord.Interaction,
    global_scope: bool = False,
    days: app_commands.Range[int, 0, config.CHART_MAX_DAYS] = config.CHART_DEFAULT_DAYS,
) -> None:
    """統計情報を DB から集計して表示する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    if global_scope and not bot.is_bot_owner(interaction.user):
        await interaction.followup.send(
            embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED), ephemeral=True
        )
        return
    guild_id = None if global_scope else interaction.guild.id  # type: ignore[union-attr]
    stats = await bot.db.get_statistics(guild_id)
    queue_counts = await bot.db.count_queue()
    success_rate = (stats["success"] / stats["total"] * 100) if stats["total"] else 0.0
    uptime = utils.now_ts() - bot.started_at
    embed = ui.info_embed(
        "📊 チャージ統計" + (" (Bot全体)" if global_scope else ""),
        f"{ui.SEPARATOR}\n"
        f"総チャージ回数: **{utils.fmt_int(stats['total'])}** 回\n"
        f"総送金額: **{utils.fmt_yen(stats['sent'])}**\n"
        f"総付与残高: **{utils.fmt_int(stats['credited'])}**",
        color=config.Color.INFO,
    )
    embed.add_field(
        name="今日",
        value=f"{utils.fmt_int(stats['today_count'])}回 / {utils.fmt_yen(stats['today_sent'])}\n"
              f"付与 {utils.fmt_int(stats['today_credited'])}",
        inline=True,
    )
    embed.add_field(
        name="今月",
        value=f"{utils.fmt_int(stats['month_count'])}回 / {utils.fmt_yen(stats['month_sent'])}\n"
              f"付与 {utils.fmt_int(stats['month_credited'])}",
        inline=True,
    )
    embed.add_field(
        name="成功 / 失敗",
        value=f"✅ {utils.fmt_int(stats['success'])} / ❌ {utils.fmt_int(stats['failed'])}\n"
              f"成功率 {success_rate:.1f}%",
        inline=True,
    )
    embed.add_field(
        name="処理中",
        value=f"{utils.fmt_int(stats['in_progress'])} 件"
              + (f"\n🟠 確認中 {stats['manual_review']} 件" if stats["manual_review"] else ""),
        inline=True,
    )
    embed.add_field(
        name="キュー",
        value=f"待機 {queue_counts.get(config.TxStatus.QUEUED, 0)} / "
              f"処理中 {queue_counts.get(config.TxStatus.PROCESSING, 0)}",
        inline=True,
    )
    embed.add_field(
        name="登録ユーザー / 残高合計",
        value=f"{utils.fmt_int(stats['users'])} 人 / {utils.fmt_int(stats['total_balance'])}",
        inline=True,
    )
    embed.add_field(name="Bot 稼働時間", value=utils.format_duration(uptime), inline=True)
    embed.set_footer(text=f"v{bot.version} / 集計元: charge_transactions・balances")
    # 日別推移のグラフ (描けない環境では理由を添えて数字だけ出す)
    image = None
    if days > 0:
        image, totals = await bot.charge.build_daily_chart(guild_id, days=int(days))
        embed.add_field(
            name=f"直近 {totals['days']} 日",
            value=(
                f"合計 **{utils.fmt_yen(totals['amount'])}** / {totals['count']}回\n"
                f"付与 {utils.fmt_int(totals['credited'])}\n"
                f"チャージのあった日: {totals['active_days']} / {totals['days']}日\n"
                f"1日の最高: {utils.fmt_yen(totals['best_amount'])}"
            ),
            inline=True,
        )
        if image is not None:
            embed.set_image(url=f"attachment://{config.CHART_FILENAME}")
        else:
            embed.add_field(
                name="グラフを表示できません",
                value=chart.unavailable_reason() or "画像の生成に失敗しました。",
                inline=False,
            )
    if image is not None:
        await interaction.followup.send(embed=embed, file=image, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="queue", description="受取キューの状態を表示します (管理者)")
@app_commands.guild_only()
@require_admin()
async def queue_command(interaction: discord.Interaction) -> None:
    """受取キューと処理中の取引を表示する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    items = await bot.db.list_queue(limit=25)
    review_rows = await bot.db.list_transactions_by_status(
        [config.TxStatus.MANUAL_REVIEW], limit=10
    )
    now = utils.now_ts()
    processing: list[str] = []
    waiting: list[str] = []
    for item in items:
        line = (
            f"`{item['transaction_id']}` <@{item['user_id']}> "
            f"{utils.fmt_yen(int(item['requested_amount']))} / "
            f"待機 {utils.format_duration(now - int(item['enqueued_at']))} / "
            f"再試行 {item['attempts']}"
        )
        if item["tx_status"] == config.TxStatus.PROCESSING:
            processing.append(line)
        else:
            next_at = int(item["next_attempt_at"])
            suffix = f" / 次回 {utils.format_jst(next_at, with_seconds=True)}" if next_at > now else ""
            waiting.append(line + suffix)
    embed = ui.info_embed(
        "🗃 受取キュー",
        f"{ui.SEPARATOR}\n受取用アカウントは1つのため、常に1件ずつ直列処理します。",
        color=config.Color.INFO,
    )
    embed.add_field(
        name=f"処理中 ({len(processing)})",
        value=utils.truncate("\n".join(processing), 1000) if processing else "なし",
        inline=False,
    )
    embed.add_field(
        name=f"待機中 ({len(waiting)})",
        value=utils.truncate("\n".join(waiting), 1000) if waiting else "なし",
        inline=False,
    )
    if review_rows:
        lines = [
            f"`{r['id']}` <@{r['user_id']}> {utils.fmt_yen(int(r['received_amount'] or r['requested_amount']))} "
            f"/ `{r['error_code']}`"
            for r in review_rows
        ]
        embed.add_field(
            name=f"🟠 手動確認が必要 ({len(review_rows)})",
            value=utils.truncate("\n".join(lines), 1000)
            + "\n`/transaction verify` → `/transaction resolve` で確定してください。",
            inline=False,
        )
    embed.set_footer(text=f"現在処理中: {bot.charge.processing_transaction_id or 'なし'}")
    await interaction.followup.send(embed=embed, ephemeral=True)


class SystemGroup(app_commands.Group):
    """システム状態と運用設定。"""

    def __init__(self) -> None:
        super().__init__(name="system", description="システム状態と運用設定 (管理者)")

    @app_commands.command(name="status", description="システム状態を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def status(self, interaction: discord.Interaction) -> None:
        await _render_system(interaction)

    @app_commands.command(name="heartbeat", description="死活監視URLを設定します (Bot Owner)")
    @app_commands.describe(url="監視サービスのURL。none で無効化")
    @require_owner()
    async def heartbeat(self, interaction: discord.Interaction, url: str) -> None:
        """定期的に GET する URL を設定する (healthchecks.io 等)。

        Bot がクラッシュループに入ると Discord へも通知できないため、
        外部から停止を検知できるようにする。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = url.strip()
        if value.lower() in ("none", "off", "clear", "-"):
            await bot.db.set_system_value("heartbeat_url", "")
            await _audit(interaction, "SYSTEM_HEARTBEAT", detail={"enabled": False})
            await interaction.followup.send(
                embed=ui.success_embed("✅ 死活監視を無効化しました",
                                       f"`data/heartbeat` の更新は継続します "
                                       f"({config.TASK_HEARTBEAT_INTERVAL}秒ごと)。"),
                ephemeral=True,
            )
            return
        if not value.startswith("https://") and not value.startswith("http://"):
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "http(s):// から始まるURLを指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.db.set_system_value("heartbeat_url", value[:500])
        await _audit(interaction, "SYSTEM_HEARTBEAT", detail={"enabled": True})
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 死活監視URLを設定しました",
                f"{config.TASK_HEARTBEAT_INTERVAL} 秒ごとに通知します。\n"
                "一定時間通知が届かない場合にアラートが出るよう、監視サービス側で設定してください。",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="backup_remote", description="バックアップの外部保存方法を設定します (Bot Owner)"
    )
    @app_commands.describe(mode="保存方法", directory="SECONDARY_DIR の保存先パス")
    @app_commands.choices(mode=[
        app_commands.Choice(name="ローカルのみ (既定)", value=config.BackupRemote.NONE),
        app_commands.Choice(name="Bot Owner の DM へ送信", value=config.BackupRemote.OWNER_DM),
        app_commands.Choice(name="別ディレクトリへコピー", value=config.BackupRemote.SECONDARY_DIR),
    ])
    @require_owner()
    async def backup_remote(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        directory: str | None = None,
    ) -> None:
        """VPS 消失時にバックアップを失わないための外部保存設定。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if mode.value == config.BackupRemote.SECONDARY_DIR:
            if not directory:
                await interaction.followup.send(
                    embed=ui.info_embed("保存先が必要です", "directory に保存先パスを指定してください。",
                                        color=config.Color.DANGER),
                    ephemeral=True,
                )
                return
            try:
                from pathlib import Path

                target = Path(directory.strip())
                target.mkdir(parents=True, exist_ok=True)
                probe = target / ".write_test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                await interaction.followup.send(
                    embed=ui.info_embed("保存先へ書き込めません", utils.safe_error_text(exc, limit=300),
                                        color=config.Color.DANGER),
                    ephemeral=True,
                )
                return
            await bot.db.set_system_value("backup_secondary_dir", str(target))
        await bot.db.set_system_value("backup_remote", mode.value)
        await _audit(interaction, "SYSTEM_BACKUP_REMOTE",
                     detail={"mode": mode.value, "directory": directory})
        note = ""
        if mode.value == config.BackupRemote.OWNER_DM:
            note = (
                "\n⚠️ バックアップには残高台帳が含まれます (Kyash トークンは暗号化済み)。\n"
                "　リストアには `data/secret.key` も必要なので、別途保管してください。"
            )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 外部保存を設定しました", f"方法: **{mode.name}**{note}"
            ),
            ephemeral=True,
        )


async def _render_system(interaction: discord.Interaction) -> None:
    """Bot / DB / Kyash / キュー / タスクの状態を表示する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    settings = await bot.db.get_settings(interaction.guild.id)  # type: ignore[union-attr]
    queue_counts = await bot.db.count_queue()
    pending_notifications = await bot.db.count_pending_notifications()
    db_ok = True
    try:
        await bot.db.fetchone("SELECT 1")
    except Exception:  # noqa: BLE001
        db_ok = False
    db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    task_status = bot.tasks.status()
    embed = ui.info_embed(
        "🖥 システム状態",
        f"{ui.SEPARATOR}\nBot バージョン: **v{bot.version}** / schema v{config.SCHEMA_VERSION}",
        color=config.Color.INFO,
    )
    embed.add_field(name="稼働時間", value=utils.format_duration(utils.now_ts() - bot.started_at), inline=True)
    embed.add_field(name="Discord 遅延", value=f"{bot.latency * 1000:.0f} ms", inline=True)
    embed.add_field(
        name="DB", value=f"{'🟢 正常' if db_ok else '🔴 異常'} / {db_size / 1024:.0f} KB", inline=True
    )
    embed.add_field(
        name="Kyash",
        value=config.KYASH_STATUS_LABELS.get(bot.kyash.status, bot.kyash.status),
        inline=True,
    )
    embed.add_field(
        name="キュー",
        value=f"待機 {queue_counts.get(config.TxStatus.QUEUED, 0)} / "
              f"処理中 {queue_counts.get(config.TxStatus.PROCESSING, 0)}",
        inline=True,
    )
    embed.add_field(name="通知キュー", value=f"{pending_notifications} 件", inline=True)
    embed.add_field(
        name="このサーバーの状態",
        value=f"メンテナンス: {'🟠 ON' if settings.maintenance else '🟢 OFF'}\n"
              f"緊急停止: {'🔴 ON' if settings.emergency_stop else '🟢 OFF'}",
        inline=True,
    )
    embed.add_field(
        name="許可サーバー数",
        value=f"{await bot.db.count_allowed_guilds()} / 参加 {len(bot.guilds)}",
        inline=True,
    )
    embed.add_field(
        name="バージョン",
        value=f"Python {platform.python_version()}\ndiscord.py {discord.__version__}\n"
              f"Kyasher {kyash_service.KYASH_MODULE_VERSION}",
        inline=True,
    )
    snapshot = bot.kyash.status_snapshot()
    days_left = snapshot.get("token_days_left")
    embed.add_field(
        name="Kyash 詳細",
        value=(
            (f"トークン残り: {days_left:.1f} 日\n" if days_left is not None else "トークン: 未取得\n")
            + (
                f"残高しきい値: {utils.fmt_yen(snapshot['wallet_threshold'])} "
                f"(余裕 {utils.fmt_yen(snapshot['wallet_headroom'])})"
                if snapshot.get("wallet_threshold") else "残高しきい値: 未設定"
            )
        ),
        inline=True,
    )
    metrics = bot.charge.metrics_snapshot()
    embed.add_field(
        name="メトリクス (起動後)",
        value=(
            f"完了 {metrics['charges_completed']} / 失敗 {metrics['charges_failed']}\n"
            f"平均受取 {metrics['receive_avg_seconds']}秒 / 確認 {metrics['manual_reviews']}\n"
            f"購入 {metrics['purchases']} / 招待確定 {metrics['invites_confirmed']}\n"
            f"クールダウン中 {metrics['cooldowns']}人"
        ),
        inline=True,
    )
    embed.add_field(
        name="バックグラウンドタスク",
        value="\n".join(f"{'🟢' if ok else '🔴'} {name}" for name, ok in task_status.items()),
        inline=False,
    )
    embed.set_footer(text="秘密情報は表示されません")
    await interaction.followup.send(embed=embed, ephemeral=True)


class ConfigGroup(app_commands.Group):
    """設定の確認・入出力。"""

    def __init__(self) -> None:
        super().__init__(name="config", description="設定の確認と入出力 (管理者)")

    @app_commands.command(name="show", description="現在のサーバー設定を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def show(self, interaction: discord.Interaction) -> None:
        await _render_config(interaction)

    @app_commands.command(name="export", description="設定を JSON として出力します")
    @app_commands.guild_only()
    @require_admin()
    async def export(self, interaction: discord.Interaction) -> None:
        """設定を JSON ファイルで出力する (Kyash 認証情報は含まない)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        settings = await bot.db.get_settings(guild.id)
        rates = await bot.db.list_role_rates(guild.id)
        items = await bot.db.list_shop_items(guild.id, active_only=False)
        payload = {
            "version": config.BOT_VERSION,
            "exported_at": utils.format_jst(utils.now_ts(), with_seconds=True),
            "guild_id": guild.id,
            "settings": {
                "charge_rate": str(settings.charge_rate),
                "minimum_charge": settings.minimum_charge,
                "maximum_charge": settings.maximum_charge,
                "daily_limit": settings.daily_limit,
                "guild_daily_limit": settings.guild_daily_limit,
                "max_balance": settings.max_balance,
                "manual_review_allow_new": settings.manual_review_allow_new,
                "balance_log_scope": settings.balance_log_scope,
                "ranking_enabled": settings.ranking_enabled,
                "ranking_limit": settings.ranking_limit,
                "ranking_interval": settings.ranking_interval,
                "ranking_hide_absent": settings.ranking_hide_absent,
                "summary_enabled": settings.summary_enabled,
                "shop_enabled": settings.shop_enabled,
                "panel_title": settings.panel_title,
                "panel_description": settings.panel_description,
                "accent_color": settings.accent_color,
            },
            "role_rates": [
                {"role_id": int(r["role_id"]), "charge_rate": str(r["charge_rate"]),
                 "priority": int(r["priority"])}
                for r in rates
            ],
            "shop_items": [
                {"role_id": int(i["role_id"]), "name": str(i["name"]),
                 "description": i["description"], "price": int(i["price"]),
                 "duration_days": int(i["duration_days"]), "stock": int(i["stock"]),
                 "purchase_limit": int(i["purchase_limit"]),
                 "sort_order": int(i["sort_order"]), "active": bool(i["active"])}
                for i in items
            ],
        }
        import io
        import json

        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await _audit(interaction, "CONFIG_EXPORT", detail={"items": len(items),
                                                           "role_rates": len(rates)})
        await interaction.followup.send(
            embed=ui.success_embed(
                "📤 設定を出力しました",
                "チャンネルID・ロールIDはこのサーバー固有です。\n"
                "別サーバーへ取り込む場合は `/config import` を使用してください。\n"
                "※ Kyash の認証情報は含まれません。",
            ),
            file=discord.File(io.BytesIO(data), filename=f"config_{guild.id}.json"),
            ephemeral=True,
        )

    @app_commands.command(name="import", description="出力した JSON から設定を取り込みます")
    @app_commands.describe(
        file="/config export で出力した JSON", include_shop="ショップ商品も取り込む"
    )
    @app_commands.guild_only()
    @require_admin()
    async def import_config(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        include_shop: bool = False,
    ) -> None:
        """他サーバーからの設定取り込み (チャンネル・ロール設定は対象外)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        if file.size > 512 * 1024:
            await interaction.response.send_message(
                embed=ui.info_embed("ファイルが大きすぎます", "512KB 以下の JSON を指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        try:
            raw = await file.read()
            import json

            payload = json.loads(raw.decode("utf-8"))
            incoming = dict(payload.get("settings") or {})
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                embed=ui.info_embed("読み込めませんでした", utils.safe_error_text(exc, limit=200),
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        allowed_keys = {
            "charge_rate", "minimum_charge", "maximum_charge", "daily_limit",
            "guild_daily_limit", "max_balance", "manual_review_allow_new",
            "balance_log_scope", "ranking_enabled", "ranking_limit", "ranking_interval",
            "ranking_hide_absent", "summary_enabled", "shop_enabled", "panel_title",
            "panel_description", "accent_color",
        }
        values: dict[str, Any] = {}
        for key, value in incoming.items():
            if key not in allowed_keys:
                continue
            if key == "charge_rate":
                parsed = utils.validate_charge_rate(str(value))
                if parsed is None:
                    continue
                values[key] = utils.rate_to_db(parsed)
            elif key == "balance_log_scope":
                values[key] = "ALL" if str(value).upper() == "ALL" else "MANUAL"
            elif isinstance(value, bool):
                values[key] = 1 if value else 0
            elif isinstance(value, int) or value is None:
                values[key] = value
            elif isinstance(value, str):
                values[key] = value[:1500]
        shop_items = payload.get("shop_items") or [] if include_shop else []
        approved = await _confirm(
            interaction,
            title="⚠️ 設定を取り込みます",
            description=(
                f"取り込む項目: **{len(values)} 件**\n"
                + (f"ショップ商品: **{len(shop_items)} 件**を追加\n" if shop_items else "")
                + "現在の設定は上書きされます。\n"
                "チャンネル・ロール・管理者ロールの設定は取り込まれません。"
            ),
            confirm_label="取り込む",
        )
        if not approved:
            return
        if values:
            await bot.db.update_settings(guild.id, **values)
        added = 0
        for item in shop_items:
            try:
                role_id = int(item["role_id"])
                if guild.get_role(role_id) is None:
                    continue
                await bot.db.add_shop_item(
                    guild_id=guild.id, role_id=role_id, name=str(item["name"])[:100],
                    price=int(item["price"]), duration_days=int(item.get("duration_days") or 0),
                    stock=int(item.get("stock", -1)),
                    purchase_limit=int(item.get("purchase_limit") or 0),
                    description=(str(item["description"])[:500] if item.get("description") else None),
                    sort_order=int(item.get("sort_order") or 0), created_by=interaction.user.id,
                )
                added += 1
            except Exception:  # noqa: BLE001
                logger.exception("ショップ商品の取り込みに失敗しました")
        op_id = await _audit(
            interaction, "CONFIG_IMPORT",
            detail={"settings": len(values), "shop_items": added},
        )
        await bot.charge.refresh_charge_panels(guild.id)
        await bot.charge.refresh_shop_panels(guild.id)
        await interaction.followup.send(
            embed=ui.success_embed(
                "📥 設定を取り込みました",
                f"設定 {len(values)} 件 / ショップ商品 {added} 件\n操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="reset", description="サーバー設定を既定値へ戻します")
    @app_commands.guild_only()
    @require_admin()
    async def reset(self, interaction: discord.Interaction) -> None:
        """設定を既定値へ戻す (残高・履歴・パネルには影響しない)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        approved = await _confirm(
            interaction,
            title="⚠️ 設定を既定値へ戻します",
            description=(
                "チャージ率・金額制限・上限・ランキング・パネル文言などを既定値へ戻します。\n"
                "管理者ロール・チャンネル設定も解除されます。\n"
                "**残高・履歴・取引・パネルの設置状態は削除されません。**"
            ),
            confirm_label="既定値に戻す",
            stages=2,
        )
        if not approved:
            return
        await bot.db.update_settings(
            guild.id,
            charge_rate=config.DEFAULT_CHARGE_RATE,
            minimum_charge=config.DEFAULT_MINIMUM_CHARGE,
            maximum_charge=config.DEFAULT_MAXIMUM_CHARGE,
            daily_limit=config.DEFAULT_DAILY_LIMIT,
            guild_daily_limit=config.DEFAULT_GUILD_DAILY_LIMIT,
            max_balance=config.DEFAULT_MAX_BALANCE,
            manual_review_allow_new=0,
            admin_role_id=None,
            achievement_channel_id=None,
            log_channel_id=None,
            balance_log_channel_id=None,
            balance_log_scope=config.DEFAULT_BALANCE_LOG_SCOPE,
            summary_channel_id=None,
            summary_enabled=0,
            shop_enabled=1,
            ranking_enabled=1,
            ranking_limit=config.DEFAULT_RANKING_LIMIT,
            ranking_interval=config.DEFAULT_RANKING_INTERVAL,
            ranking_hide_absent=0,
            panel_title=None,
            panel_description=None,
            accent_color=None,
        )
        op_id = await _audit(interaction, "CONFIG_RESET")
        await bot.charge.refresh_charge_panels(guild.id)
        await interaction.followup.send(
            embed=ui.success_embed("♻️ 設定を既定値へ戻しました", f"操作ID: `{op_id}`"),
            ephemeral=True,
        )


async def _render_config(interaction: discord.Interaction) -> None:
    """サーバー設定の一覧 (チャージ関連とランキング関連を分けて表示)。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None
    settings = await bot.db.get_settings(guild.id)
    panels = await bot.db.list_panels(guild.id)
    ranking_panels = await bot.db.list_ranking_panels(guild.id)
    embed = ui.info_embed(
        f"⚙️ {guild.name} の設定",
        f"{ui.SEPARATOR}\nサーバー許可: "
        f"{'🟢 許可済み' if await bot.db.is_guild_allowed(guild.id) else '🚫 未許可'}",
        color=config.Color.ACCENT,
    )
    embed.add_field(
        name="チャージ設定",
        value=(
            f"チャージ率: **{utils.fmt_rate(settings.charge_rate)}**\n"
            f"最低額: {utils.fmt_yen(settings.minimum_charge)}\n"
            f"最高額: {utils.fmt_yen(settings.maximum_charge)}\n"
            f"日次上限 (ユーザー): {utils.fmt_yen(settings.daily_limit) if settings.daily_limit else '無制限'}\n"
            f"日次上限 (サーバー): {utils.fmt_yen(settings.guild_daily_limit) if settings.guild_daily_limit else '無制限'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="チャンネル / ロール",
        value=(
            f"管理者ロール: {f'<@&{settings.admin_role_id}>' if settings.admin_role_id else '未設定'}\n"
            f"実績: {f'<#{settings.achievement_channel_id}>' if settings.achievement_channel_id else '未設定'}\n"
            f"ログ: {f'<#{settings.log_channel_id}>' if settings.log_channel_id else '未設定'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="運用状態",
        value=(
            f"メンテナンス: {'🟠 ON' if settings.maintenance else '🟢 OFF'}\n"
            f"緊急停止: {'🔴 ON' if settings.emergency_stop else '🟢 OFF'}\n"
            f"チャージパネル: {len(panels)} 件"
        ),
        inline=False,
    )
    embed.add_field(
        name="ランキング設定 (チャージパネルとは独立)",
        value=(
            f"ランキング: {'🟢 有効' if settings.ranking_enabled else '⚫ 無効'}\n"
            f"表示件数: TOP {settings.ranking_limit}\n"
            f"更新間隔: {settings.ranking_interval} 秒\n"
            f"退会ユーザー: {'非表示' if settings.ranking_hide_absent else '表示'}\n"
            f"ランキングパネル: {len(ranking_panels)} 件"
        ),
        inline=False,
    )
    rates = await bot.db.list_role_rates(guild.id)
    items = await bot.db.list_shop_items(guild.id, active_only=False)
    campaign = await bot.db.get_active_campaign(guild.id)
    embed.add_field(
        name="残高・ログ",
        value=(
            f"残高上限: {utils.fmt_int(settings.max_balance) if settings.max_balance else '無制限'}\n"
            f"残高ログ: "
            f"{f'<#{settings.balance_log_channel_id}>' if settings.balance_log_channel_id else '未設定'}"
            f" ({'全変動' if settings.balance_log_scope == 'ALL' else '手動操作のみ'})\n"
            f"日次サマリ: "
            f"{f'<#{settings.summary_channel_id}>' if settings.summary_channel_id else '未設定'}"
            f" ({'有効' if settings.summary_enabled else '無効'})\n"
            f"確認中の新規チャージ: {'許可' if settings.manual_review_allow_new else '不可'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="ロール別レート / ショップ / 招待",
        value=(
            f"ロール別レート: {len(rates)} 件\n"
            f"ショップ: {'🟢 有効' if settings.shop_enabled else '⚫ 無効'} / 商品 {len(items)} 件\n"
            f"招待キャンペーン: "
            + (f"🟢 {campaign['name']}" if campaign else "なし")
        ),
        inline=False,
    )
    embed.set_footer(text=f"最終更新 {utils.format_jst(settings.updated_at)}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="logs", description="監査ログを表示します (管理者)")
@app_commands.describe(action="操作種別で絞り込み", actor="操作者で絞り込み", page="ページ番号")
@app_commands.guild_only()
@require_admin()
async def logs_command(
    interaction: discord.Interaction,
    action: str | None = None,
    actor: discord.Member | None = None,
    page: app_commands.Range[int, 1, 200] = 1,
) -> None:
    """監査ログ (管理操作の記録) を表示する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    per_page = 8
    rows, total = await bot.db.list_audit_logs(
        guild_id=interaction.guild.id,  # type: ignore[union-attr]
        action=action.strip().upper() if action else None,
        actor_id=actor.id if actor else None,
        offset=(page - 1) * per_page,
        limit=per_page,
    )
    total_pages = max(1, -(-total // per_page))
    lines = [
        f"{utils.format_jst(r['created_at'], with_seconds=True)} / `{r['action']}` / "
        # 自動処理 (SYSTEM_ACTOR_ID) はメンションにしない
        + ("🤖 自動処理" if int(r["actor_id"]) == config.SYSTEM_ACTOR_ID
           else f"<@{r['actor_id']}>")
        + (f" → <@{r['target_user_id']}>" if r["target_user_id"] else "")
        + (f"\n　`{utils.truncate(str(r['detail']), 150)}`" if r["detail"] else "")
        for r in rows
    ]
    embed = ui.info_embed(
        "📋 監査ログ",
        f"{ui.SEPARATOR}\n該当 **{utils.fmt_int(total)}** 件 / ページ {page}/{total_pages}\n\n"
        + ("\n".join(lines) if lines else "該当するログはありません。"),
    )
    embed.set_footer(text="page パラメータで次のページを表示できます")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /data, /backup (Bot Owner 専用)
# ---------------------------------------------------------------------------
class DataGroup(app_commands.Group):
    """データ管理 (Bot Owner 専用)。"""

    def __init__(self) -> None:
        super().__init__(name="data", description="データ管理 (Bot Owner)")

    @app_commands.command(name="delete", description="サーバーのデータを削除します (危険)")
    @app_commands.describe(guild_id="対象サーバーID (省略時は実行中のサーバー)")
    @require_owner()
    async def delete(self, interaction: discord.Interaction, guild_id: str | None = None) -> None:
        """サーバーデータを削除する (2段階確認 + 削除前バックアップ)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        target_id = _resolve_guild_id(interaction, guild_id)
        if target_id is None:
            await interaction.response.send_message(
                embed=ui.info_embed("入力が不正です", "サーバーIDを数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        users = await bot.db.count_guild_users(target_id)
        total_balance = await bot.db.sum_guild_balance(target_id)
        guild = bot.get_guild(target_id)
        approved = await _confirm(
            interaction,
            title="🚨 サーバーデータを完全に削除します",
            description=(
                f"対象: **{guild.name if guild else '未参加'}** (`{target_id}`)\n"
                f"削除対象: ユーザー {users} 人 / 残高合計 {utils.fmt_int(total_balance)}\n"
                "残高・履歴・取引・パネル・設定・監査ログがすべて削除されます。\n"
                "**この操作は取り消せません。** (削除前にDBバックアップを作成します)"
            ),
            confirm_label="削除する",
            stages=2,
        )
        if not approved:
            return
        backup_path = None
        try:
            backup_path = await bot.tasks.run_backup()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=ui.info_embed(
                    "中止しました",
                    f"削除前のバックアップに失敗したため中止しました。\n{utils.safe_error_text(exc, limit=300)}",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        counts = await bot.db.delete_guild_data(target_id)
        op_id = await _audit(
            interaction, "DATA_DELETE",
            detail={"guild_id": target_id, "counts": counts,
                    "backup": backup_path.name if backup_path else None},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "🗑 データを削除しました",
                f"対象: `{target_id}`\n"
                + "\n".join(f"・{table}: {count} 件" for table, count in counts.items() if count)
                + f"\nバックアップ: `{backup_path.name if backup_path else '-'}`\n操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )


@app_commands.command(name="backup", description="DBバックアップを手動実行します (Bot Owner)")
@require_owner()
async def backup_command(interaction: discord.Interaction) -> None:
    """DB のバックアップを即時実行する。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        path = await bot.tasks.run_backup()
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(
            embed=ui.error_embed(
                config.ErrorCode.DATABASE_ERROR, admin_detail=utils.safe_error_text(exc)
            ),
            ephemeral=True,
        )
        return
    await _audit(interaction, "BACKUP", detail={"file": path.name})
    size = path.stat().st_size
    await interaction.followup.send(
        embed=ui.success_embed(
            "💾 バックアップを作成しました",
            f"ファイル: `{path.name}`\nサイズ: {size / 1024:.0f} KB\n"
            f"保存先: `{config.BACKUP_DIR}`\n保持世代: {config.BACKUP_KEEP}",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /rate (ロール別チャージ率)
# ---------------------------------------------------------------------------
class RateGroup(app_commands.Group):
    """ロール別チャージ率 (VIP 優遇など)。"""

    def __init__(self) -> None:
        super().__init__(name="rate", description="ロール別チャージ率の管理 (管理者)")

    @app_commands.command(name="set", description="ロールに適用するチャージ率を設定します")
    @app_commands.describe(
        role="対象ロール", charge_rate="例: 140 / 145.5", priority="優先度 (大きいほど優先)"
    )
    @app_commands.guild_only()
    @require_admin()
    async def set_rate(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        charge_rate: str,
        priority: app_commands.Range[int, 0, 1000] = 0,
    ) -> None:
        """ロール保持者のチャージ率を上書きする。

        複数のロールに該当する場合は優先度が高いものを採用する。
        適用されたレートは Transaction 作成時に保存されるため、後の変更に影響されない。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = utils.validate_charge_rate(charge_rate)
        if parsed is None:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "入力が不正です",
                    f"チャージ率は {config.MIN_CHARGE_RATE}〜{config.MAX_CHARGE_RATE} で指定してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        if role.is_default():
            await interaction.followup.send(
                embed=ui.info_embed("設定できません", "@everyone は指定できません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        await bot.db.set_role_rate(guild_id, role.id, parsed, int(priority))
        op_id = await _audit(
            interaction, "ROLE_RATE_SET",
            detail={"role_id": role.id, "charge_rate": str(parsed), "priority": int(priority)},
        )
        await bot.charge.log_event(
            guild_id, "⚙️ ロール別チャージ率を設定",
            fields=(
                ("ロール", role.mention, True),
                ("チャージ率", utils.fmt_rate(parsed), True),
                ("優先度", str(priority), True),
                ("操作者", interaction.user.mention, True),
                ("操作ID", f"`{op_id}`", True),
            ),
            color=config.Color.ACCENT,
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ ロール別チャージ率を設定しました",
                f"{role.mention} → **{utils.fmt_rate(parsed)}** (優先度 {priority})\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="ロール別チャージ率を削除します")
    @app_commands.guild_only()
    @require_admin()
    async def remove_rate(self, interaction: discord.Interaction, role: discord.Role) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await bot.db.remove_role_rate(interaction.guild.id, role.id)  # type: ignore[union-attr]
        if not removed:
            await interaction.followup.send(
                embed=ui.info_embed("設定されていません", f"{role.mention} のレート設定はありません。"),
                ephemeral=True,
            )
            return
        await _audit(interaction, "ROLE_RATE_REMOVE", detail={"role_id": role.id})
        await interaction.followup.send(
            embed=ui.success_embed("✅ 削除しました", f"{role.mention} のレート設定を削除しました。"),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="ロール別チャージ率の一覧を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def list_rates(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        settings = await bot.db.get_settings(guild_id)
        rows = await bot.db.list_role_rates(guild_id)
        lines = [
            f"優先度 {row['priority']}: <@&{int(row['role_id'])}> → "
            f"**{utils.fmt_rate(row['charge_rate'])}**"
            for row in rows
        ]
        embed = ui.info_embed(
            "⚙️ ロール別チャージ率",
            f"{ui.SEPARATOR}\nサーバー既定: **{utils.fmt_rate(settings.charge_rate)}**\n"
            + ("\n".join(lines) if lines else "ロール別の設定はありません。")
            + f"\n{ui.SEPARATOR}",
        )
        embed.set_footer(text="複数該当する場合は優先度が高いレートを適用します")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /refund (返金申請) と /receipt (チャージの控え)
# ---------------------------------------------------------------------------
class RefundGroup(app_commands.Group):
    """返金 (チャージ取消) の申請と審査。"""

    def __init__(self) -> None:
        super().__init__(
            name="refund", description="返金の申請と確認 (request / list は利用者も実行可)"
        )

    @app_commands.command(name="request", description="チャージの返金を申請します")
    @app_commands.describe(
        transaction_id="取引ID (`/history` や控えで確認できます)",
        reason="返金を希望する理由",
    )
    @app_commands.guild_only()
    async def request(
        self, interaction: discord.Interaction, transaction_id: str, reason: str
    ) -> None:
        """自分のチャージについて返金を申請する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = reason.strip()[:900]
        if not text:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.ITEM_INPUT_INVALID), ephemeral=True
            )
            return
        try:
            created = await bot.charge.request_refund(
                guild.id, interaction.user.id, transaction_id.strip(), text
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, next_action=exc.detail or None),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.refund_request_embed(
                request_id=int(created["request_id"]),
                tx_id=str(created["transaction_id"]),
                amount=int(created["amount"]),
                received=int(created["received_amount"]),
                completed_at=int(created["completed_at"]),
                reason=text,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="cancel", description="自分の返金申請を取り下げます")
    @app_commands.describe(request_id="申請ID")
    @app_commands.guild_only()
    async def cancel(
        self, interaction: discord.Interaction,
        request_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await bot.charge.cancel_refund_request(
                int(request_id), user_id=interaction.user.id
            )
        except ChargeError as exc:
            await interaction.followup.send(embed=ui.error_embed(exc.code), ephemeral=True)
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "⚪ 返金申請を取り下げました",
                f"申請ID: `{request_id}`\n残高は変わっていません。",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="返金申請の一覧を表示します")
    @app_commands.describe(
        status="状態で絞り込み", user="利用者で絞り込み (管理者のみ)", page="ページ番号",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name=label, value=key)
        for key, label in config.REFUND_STATUS_LABELS.items()
    ])
    @app_commands.guild_only()
    async def list_requests(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        user: discord.User | None = None,
        page: app_commands.Range[int, 1, 200] = 1,
    ) -> None:
        """自分の申請を確認する (管理者は全員ぶんを見られる)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        is_admin = await bot.is_server_admin(interaction)
        if not is_admin:
            target_user: int | None = interaction.user.id
            title = "あなたの返金申請"
        elif user is not None:
            target_user = user.id
            title = f"{user.display_name} の返金申請"
        else:
            target_user = None
            title = "返金申請の一覧"
        per_page = 6
        rows, total = await bot.db.list_refund_requests(
            guild.id, status=status.value if status else None, user_id=target_user,
            offset=(page - 1) * per_page, limit=per_page,
        )
        await interaction.followup.send(
            embed=ui.refund_list_embed(
                rows, title=title, total=total, page=page,
                total_pages=max(1, -(-total // per_page)),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="approve", description="返金申請を承認します (管理者)")
    @app_commands.describe(request_id="申請ID")
    @app_commands.guild_only()
    @require_admin()
    async def approve(
        self, interaction: discord.Interaction,
        request_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        """承認して内部残高を取り消す (実際の送金は管理者が別途行う)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = await bot.db.get_refund_request(int(request_id), guild.id)
        if request is None:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.REQUEST_NOT_FOUND), ephemeral=True
            )
            return
        if not await bot.charge.can_review(guild.id, interaction.user):
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED), ephemeral=True
            )
            return
        view = ui.ConfirmView(
            owner_id=interaction.user.id, confirm_label="取消を実行する", danger=True
        )
        await interaction.followup.send(
            embed=ui.info_embed(
                "⚠️ 返金の承認",
                f"申請 `#{request_id}` を承認すると、取引 "
                f"`{request['transaction_id']}` を取り消し、"
                f"**{utils.fmt_int(int(request['amount'] or 0))}** を回収します。\n\n"
                f"{ui.REFUND_SCOPE_NOTE}",
                color=config.Color.DANGER,
            ),
            view=view, ephemeral=True,
        )
        await view.wait()
        if not view.value:
            return
        try:
            result = await bot.charge.approve_refund(
                int(request_id), operator_id=interaction.user.id
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail or None),
                ephemeral=True,
            )
            return
        op_id = await _audit(
            interaction, "REFUND_APPROVE_CMD",
            target_user_id=int(request["user_id"]),
            detail={"request_id": int(request_id),
                    "transaction_id": str(request["transaction_id"])},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 返金を承認しました",
                f"申請ID: `{request_id}`\n"
                f"回収した残高: **{utils.fmt_int(int(result['credited_amount']))}**\n"
                f"利用者の残高: {utils.fmt_int(int(result['balance_before']))} → "
                f"**{utils.fmt_int(int(result['balance_after']))}**\n"
                f"操作ID: `{op_id}`\n\n{ui.REFUND_SCOPE_NOTE}",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="reject", description="返金申請を却下します (管理者)")
    @app_commands.describe(request_id="申請ID", reason="却下の理由 (利用者へ通知します)")
    @app_commands.guild_only()
    @require_admin()
    async def reject(
        self, interaction: discord.Interaction,
        request_id: app_commands.Range[int, 1, 10_000_000],
        reason: str,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await bot.charge.can_review(guild.id, interaction.user):
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED), ephemeral=True
            )
            return
        request = await bot.db.get_refund_request(int(request_id), guild.id)
        if request is None:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.REQUEST_NOT_FOUND), ephemeral=True
            )
            return
        try:
            await bot.charge.reject_refund(
                int(request_id), operator_id=interaction.user.id,
                reason=reason.strip()[:400],
            )
        except ChargeError as exc:
            await interaction.followup.send(embed=ui.error_embed(exc.code), ephemeral=True)
            return
        op_id = await _audit(
            interaction, "REFUND_REJECT_CMD",
            target_user_id=int(request["user_id"]),
            detail={"request_id": int(request_id), "reason": reason[:300]},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "🔴 返金申請を却下しました",
                f"申請ID: `{request_id}`\n利用者へ DM で通知しました。\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )


class ReceiptGroup(app_commands.Group):
    """チャージの控え (署名つき)。"""

    def __init__(self) -> None:
        super().__init__(name="receipt", description="チャージの控えの発行と確認")

    @app_commands.command(name="show", description="自分のチャージの控えを発行します")
    @app_commands.describe(transaction_id="取引ID (省略すると直近のチャージ)")
    @app_commands.guild_only()
    async def show(
        self, interaction: discord.Interaction, transaction_id: str | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        tx_id = (transaction_id or "").strip()
        if not tx_id:
            rows, _ = await bot.db.search_transactions(
                guild_id=guild.id, user_id=interaction.user.id,
                status=config.TxStatus.COMPLETED, limit=1,
            )
            if not rows:
                await interaction.followup.send(
                    embed=ui.info_embed(
                        "控えを発行できる取引がありません",
                        "完了したチャージがまだありません。",
                        color=config.Color.WARNING,
                    ),
                    ephemeral=True,
                )
                return
            tx_id = str(rows[0]["id"])
        try:
            receipt = await bot.charge.issue_receipt(
                guild.id, interaction.user.id, tx_id
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, next_action=exc.detail or None),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.receipt_embed(
                code=str(receipt["code"]), tx_id=str(receipt["tx_id"]),
                received=int(receipt["received"]), credited=int(receipt["credited"]),
                completed_at=int(receipt["completed_at"]),
                provider=str(receipt["provider"]), refunded=bool(receipt["refunded"]),
                guild_name=guild.name,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="verify", description="控えのコードを確認します")
    @app_commands.describe(code="控えのコード (R1. で始まる文字列)")
    @app_commands.guild_only()
    async def verify(self, interaction: discord.Interaction, code: str) -> None:
        """控えが本物か、いまも有効かを確認する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await bot.charge.verify_receipt_code(code)
        payload = result.get("payload") or {}
        guild_name = guild.name
        if payload and int(payload.get("guild_id", 0)) != guild.id:
            guild_name = f"別のサーバー (`{payload.get('guild_id')}`)"
        await interaction.followup.send(
            embed=ui.receipt_verify_embed(result, guild_name=guild_name),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /fraud (不正検知の確認と処理)
# ---------------------------------------------------------------------------
class FraudGroup(app_commands.Group):
    """不正の兆候の確認と処理 (管理者)。"""

    def __init__(self) -> None:
        super().__init__(name="fraud", description="不正検知の確認と処理 (管理者)")

    @app_commands.command(name="list", description="検知の一覧を表示します")
    @app_commands.describe(
        status="状態で絞り込み (既定: 未処理)", kind="種別で絞り込み",
        user="利用者で絞り込み", page="ページ番号",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name=label, value=key)
            for key, label in config.FRAUD_STATUS_LABELS.items()
        ] + [app_commands.Choice(name="すべて", value="ALL")],
        kind=[
            app_commands.Choice(name=label, value=key)
            for key, label in config.FRAUD_KIND_LABELS.items()
        ],
    )
    @app_commands.guild_only()
    @require_admin()
    async def list_flags(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        kind: app_commands.Choice[str] | None = None,
        user: discord.User | None = None,
        page: app_commands.Range[int, 1, 200] = 1,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        per_page = 6
        chosen = status.value if status else config.FraudStatus.OPEN
        filter_status = None if chosen == "ALL" else chosen
        rows, total = await bot.db.list_fraud_flags(
            guild.id, status=filter_status, kind=kind.value if kind else None,
            user_id=user.id if user else None,
            offset=(page - 1) * per_page, limit=per_page,
        )
        total_pages = max(1, -(-total // per_page))
        await interaction.followup.send(
            embed=ui.fraud_list_embed(
                rows, guild_name=guild.name, total=total, page=page,
                total_pages=total_pages, status=filter_status,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="resolve", description="検知を対処済みにします"
    )
    @app_commands.describe(flag_id="検知ID", note="メモ (任意)")
    @app_commands.guild_only()
    @require_admin()
    async def resolve(
        self,
        interaction: discord.Interaction,
        flag_id: app_commands.Range[int, 1, 10_000_000],
        note: str | None = None,
    ) -> None:
        await _review_fraud(
            interaction, int(flag_id), status=config.FraudStatus.RESOLVED, note=note
        )

    @app_commands.command(
        name="ignore", description="検知を問題なしにします"
    )
    @app_commands.describe(flag_id="検知ID", note="メモ (任意)")
    @app_commands.guild_only()
    @require_admin()
    async def ignore(
        self,
        interaction: discord.Interaction,
        flag_id: app_commands.Range[int, 1, 10_000_000],
        note: str | None = None,
    ) -> None:
        await _review_fraud(
            interaction, int(flag_id), status=config.FraudStatus.IGNORED, note=note
        )

    @app_commands.command(
        name="scan", description="いますぐ検知を実行します (定期実行とは別に)"
    )
    @app_commands.guild_only()
    @require_admin()
    async def scan(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        outcome = await bot.charge.scan_guild_fraud(guild.id)
        open_count = await bot.db.count_open_fraud_flags(guild.id)
        op_id = await _audit(interaction, "FRAUD_SCAN", detail=outcome)
        await interaction.followup.send(
            embed=ui.success_embed(
                "🛡 検知を実行しました",
                f"新しく検知: **{outcome.get('flagged', 0)}** 件\n"
                f"既存の検知を更新: {outcome.get('updated', 0)} 件\n"
                f"未処理の検知: **{open_count}** 件\n"
                f"`/fraud list` で内容を確認できます。\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="thresholds", description="検知の条件を表示します"
    )
    @app_commands.guild_only()
    @require_admin()
    async def thresholds(self, interaction: discord.Interaction) -> None:
        """どの条件で検知しているかを明示する (誤検知の判断材料)。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = ui.info_embed(
            "🛡 検知の条件",
            f"{ui.SEPARATOR}\n"
            "検知は**目安**です。自動的な処分は一切行いません。\n"
            f"{ui.SEPARATOR}",
        )
        embed.add_field(
            name=config.FRAUD_KIND_LABELS[config.FraudKind.BURST_CHARGE],
            value=f"{config.FRAUD_BURST_WINDOW // 60} 分以内に "
                  f"{config.FRAUD_BURST_COUNT} 件以上のチャージ完了",
            inline=False,
        )
        embed.add_field(
            name=config.FRAUD_KIND_LABELS[config.FraudKind.SHARED_SENDER],
            value=f"同じ Kyash 送金者名を {config.FRAUD_SHARED_SENDER_USERS} 人以上が使用",
            inline=False,
        )
        embed.add_field(
            name=config.FRAUD_KIND_LABELS[config.FraudKind.DRAIN_AND_LEAVE],
            value=f"退出前 {config.FRAUD_DRAIN_WINDOW // 60} 分のチャージ分の "
                  f"{float(config.FRAUD_DRAIN_RATIO) * 100:.0f}% 以上を使って退出",
            inline=False,
        )
        embed.add_field(
            name=config.FRAUD_KIND_LABELS[config.FraudKind.INVITE_ONLY],
            value=f"確定招待が {config.FRAUD_INVITE_ONLY_COUNT} 件以上でチャージが0件",
            inline=False,
        )
        embed.add_field(
            name=config.FRAUD_KIND_LABELS[config.FraudKind.RAPID_REFUND],
            value=f"返金申請が {config.FRAUD_REFUND_COUNT} 件以上",
            inline=False,
        )
        embed.add_field(
            name="実行の間隔",
            value=f"{config.TASK_FRAUD_INTERVAL // 60} 分ごと "
                  "(`/fraud scan` で手動実行もできます)",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def _review_fraud(
    interaction: discord.Interaction, flag_id: int, *, status: str, note: str | None
) -> None:
    """``/fraud resolve`` と ``/fraud ignore`` の共通処理。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    guild = interaction.guild
    assert guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)
    flag = await bot.db.get_fraud_flag(flag_id, guild.id)
    if flag is None:
        await interaction.followup.send(
            embed=ui.error_embed(config.ErrorCode.FRAUD_FLAG_NOT_FOUND), ephemeral=True
        )
        return
    try:
        result = await bot.charge.review_fraud_flag(
            flag_id, status=status, reviewed_by=interaction.user.id,
            note=note.strip()[:400] if note else None,
        )
    except ChargeError as exc:
        await interaction.followup.send(
            embed=ui.error_embed(exc.code, admin_detail=exc.detail or None), ephemeral=True
        )
        return
    resolved = status == config.FraudStatus.RESOLVED
    await interaction.followup.send(
        embed=ui.success_embed(
            "✅ 対処済みにしました" if resolved else "⚪ 問題なしにしました",
            f"検知ID: `{flag_id}`\n対象: <@{result['user_id']}>\n"
            f"種別: {config.FRAUD_KIND_LABELS.get(str(result['kind']), str(result['kind']))}",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /goal (サーバー全体のチャージ目標)
# ---------------------------------------------------------------------------
class GoalGroup(app_commands.Group):
    """サーバー全体のチャージ目標。"""

    def __init__(self) -> None:
        super().__init__(
            name="goal", description="チャージ目標 (progress / list は利用者も実行可)"
        )

    @app_commands.command(name="create", description="チャージ目標を開始します (管理者)")
    @app_commands.describe(
        name="目標の名前", target_amount="目標の合計チャージ額 (円)",
        reward_amount="達成時に参加者へ配る残高 (0で配らない)",
        reward_role="達成時に参加者へ付けるロール",
        days="締切までの日数 (未指定なら期限なし)",
        channel="進捗パネルを置くチャンネル (未指定なら設置しない)",
    )
    @app_commands.guild_only()
    @require_admin()
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        target_amount: app_commands.Range[int, 1, 1_000_000_000],
        reward_amount: app_commands.Range[int, 0, 1_000_000] = 0,
        reward_role: discord.Role | None = None,
        days: app_commands.Range[float, 0.5, 365.0] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """目標を開始し、必要なら進捗パネルも設置する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        name = name.strip()[:config.SHOP_NAME_MAX_LEN]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if channel is not None and (
            guild.me is None or not _channel_writable(channel, guild.me)
        ):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "このチャンネルには置けません",
                    f"{channel.mention} へ Embed 付きメッセージを送れません。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        try:
            created = await bot.charge.create_goal(
                guild, name=name, target_amount=int(target_amount),
                reward_amount=int(reward_amount), reward_role=reward_role,
                days=float(days) if days else None, created_by=interaction.user.id,
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail or None),
                ephemeral=True,
            )
            return
        goal_id = int(created["goal_id"])
        panel_note = "なし (`/goal panel` で後から設置できます)"
        if channel is not None:
            goal = await bot.db.get_goal(goal_id, guild.id)
            progress = await bot.db.goal_progress(goal)  # type: ignore[arg-type]
            try:
                message = await channel.send(
                    embed=ui.goal_panel_embed(goal, progress), view=ui.GoalPanelView()
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                panel_note = f"設置に失敗しました ({utils.safe_error_text(exc)})"
            else:
                await bot.db.add_panel(
                    guild.id, channel.id, message.id, config.PANEL_TYPE_GOAL
                )
                panel_note = f"{channel.mention} (メッセージID `{message.id}`)"
        op_id = await _audit(
            interaction, "GOAL_CREATE_CMD",
            detail={"goal_id": goal_id, "target": int(target_amount),
                    "channel_id": channel.id if channel else None},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "🎯 チャージ目標を開始しました",
                f"目標ID: `{goal_id}`\n名前: **{name}**\n"
                f"目標額: **{utils.fmt_yen(int(target_amount))}**\n"
                f"報酬: {'残高 ' + utils.fmt_int(int(reward_amount)) if reward_amount else '残高なし'}"
                + (f" + {reward_role.mention}" if reward_role else "") + "\n"
                f"締切: {utils.format_jst(int(created['ends_at'])) if created['ends_at'] else '期限なし'}\n"
                f"パネル: {panel_note}\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="panel", description="進捗パネルを設置します (管理者)"
    )
    @app_commands.describe(channel="設置するチャンネル (未指定なら実行したチャンネル)")
    @app_commands.guild_only()
    @require_admin()
    async def panel(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "チャンネルを指定してください",
                    "テキストチャンネルを `channel` で指定してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        if guild.me is None or not _channel_writable(target, guild.me):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "権限が不足しています",
                    f"{target.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        goal = await bot.db.get_open_goal(guild.id)
        progress = await bot.db.goal_progress(goal) if goal is not None else None
        message = await target.send(
            embed=ui.goal_panel_embed(goal, progress), view=ui.GoalPanelView()
        )
        panel_id = await bot.db.add_panel(
            guild.id, target.id, message.id, config.PANEL_TYPE_GOAL
        )
        op_id = await _audit(
            interaction, "GOAL_PANEL_CREATE",
            detail={"panel_id": panel_id, "channel_id": target.id,
                    "message_id": message.id},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 進捗パネルを設置しました",
                f"チャンネル: {target.mention}\nメッセージID: `{message.id}`\n"
                + ("" if goal is not None else
                   "※ いま集計中の目標はありません。`/goal create` で開始すると自動で表示されます。\n")
                + f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="progress", description="いまの進捗を表示します")
    @app_commands.guild_only()
    async def progress(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        goal = await bot.db.get_open_goal(guild.id)
        if goal is None:
            await interaction.followup.send(
                embed=ui.goal_panel_embed(None, None), ephemeral=True
            )
            return
        progress = await bot.db.goal_progress(goal)
        participants = await bot.db.list_goal_participants(goal)
        contribution = next(
            (int(r["amount"]) for r in participants
             if int(r["user_id"]) == interaction.user.id),
            0,
        )
        await interaction.followup.send(
            embed=ui.goal_progress_embed(goal, progress, contribution=contribution),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="目標の一覧を表示します")
    @app_commands.guild_only()
    async def list_goals(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.list_goals(guild.id)
        await interaction.followup.send(
            embed=ui.goal_list_embed(rows, guild_name=guild.name), ephemeral=True
        )

    @app_commands.command(
        name="close", description="目標を締めます (達成していれば報酬を配る・管理者)"
    )
    @app_commands.describe(goal_id="目標ID")
    @app_commands.guild_only()
    @require_admin()
    async def close(
        self, interaction: discord.Interaction,
        goal_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        await _finish_goal(interaction, int(goal_id), cancel=False)

    @app_commands.command(
        name="cancel", description="目標を中止します (報酬は配らない・管理者)"
    )
    @app_commands.describe(goal_id="目標ID")
    @app_commands.guild_only()
    @require_admin()
    async def cancel(
        self, interaction: discord.Interaction,
        goal_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        await _finish_goal(interaction, int(goal_id), cancel=True)

    @app_commands.command(
        name="grants", description="配布した報酬の一覧を表示します (管理者)"
    )
    @app_commands.describe(goal_id="目標ID")
    @app_commands.guild_only()
    @require_admin()
    async def grants(
        self, interaction: discord.Interaction,
        goal_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        goal = await bot.db.get_goal(int(goal_id), guild.id)
        if goal is None:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.GOAL_NOT_FOUND), ephemeral=True
            )
            return
        rows = await bot.db.list_goal_grants(int(goal_id))
        people, total = await bot.db.count_goal_grants(int(goal_id))
        lines = [
            f"<@{int(r['user_id'])}> {utils.fmt_int(int(r['amount']))}"
            + (f" + <@&{int(r['role_id'])}>" if r["role_id"] else "")
            for r in rows[:25]
        ]
        await interaction.followup.send(
            embed=ui.info_embed(
                f"🎁 配布した報酬 (`{goal_id}` {goal['name']})",
                f"{ui.SEPARATOR}\n配布 {people}人 / 合計 {utils.fmt_int(total)}\n\n"
                + ("\n".join(lines) if lines else "まだ配布していません。"),
            ),
            ephemeral=True,
        )


async def _finish_goal(
    interaction: discord.Interaction, goal_id: int, *, cancel: bool
) -> None:
    """``/goal close`` と ``/goal cancel`` の共通処理。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    guild = interaction.guild
    assert guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await bot.charge.close_goal(
            guild.id, goal_id, operator_id=interaction.user.id, cancel=cancel
        )
    except ChargeError as exc:
        await interaction.followup.send(
            embed=ui.error_embed(exc.code, admin_detail=exc.detail or None),
            ephemeral=True,
        )
        return
    op_id = await _audit(
        interaction, "GOAL_CANCEL_CMD" if cancel else "GOAL_CLOSE_CMD",
        detail={"goal_id": goal_id, "status": result["status"],
                "total": result["total"]},
    )
    status = str(result["status"])
    if status == config.GoalStatus.ACHIEVED:
        title = "🎉 目標を達成として締めました"
        body = (
            f"到達額: **{utils.fmt_yen(int(result['total']))}**\n"
            f"報酬を配布した人数: **{result.get('granted', 0)}**人\n"
        )
    elif cancel:
        title = "⚫ 目標を中止しました"
        body = f"到達額: {utils.fmt_yen(int(result['total']))}\n報酬は配布していません。\n"
    else:
        title = "🏁 目標を未達で締めました"
        body = f"到達額: {utils.fmt_yen(int(result['total']))}\n報酬は配布していません。\n"
    await interaction.followup.send(
        embed=ui.success_embed(title, f"{body}操作ID: `{op_id}`"), ephemeral=True
    )


# ---------------------------------------------------------------------------
# /auction (内部残高でロールを競る)
# ---------------------------------------------------------------------------
class AuctionGroup(app_commands.Group):
    """オークションの開催と入札。"""

    def __init__(self) -> None:
        super().__init__(
            name="auction", description="オークション (bid / list は利用者も実行可)"
        )

    @app_commands.command(name="create", description="オークションを開始します (管理者)")
    @app_commands.describe(
        name="オークション名", role="景品のロール",
        start_price="開始価格 (最初の入札はこの額以上)",
        hours="開催時間 (時間単位・小数可)",
        min_increment="最低更新額 (現在額 + この額以上でないと入札できない)",
        duration_days="落札したロールの有効期間 (0で無期限)",
        description="説明",
        channel="パネルを置くチャンネル (未指定なら実行したチャンネル)",
    )
    @app_commands.guild_only()
    @require_admin()
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        role: discord.Role,
        start_price: app_commands.Range[int, 1, config.AUCTION_MAX_BID],
        hours: app_commands.Range[float, 0.1, float(config.AUCTION_MAX_DAYS * 24)],
        min_increment: app_commands.Range[int, 1, 1_000_000] = 100,
        duration_days: app_commands.Range[int, 0, config.SHOP_DURATION_MAX_DAYS] = 0,
        description: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """オークションを開始し、入札パネルを設置する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        name = name.strip()[:config.SHOP_NAME_MAX_LEN]
        description = (
            description.strip()[:config.SHOP_DESC_MAX_LEN] or None if description else None
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "チャンネルを指定してください",
                    "パネルを置けるテキストチャンネルを `channel` で指定してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        if guild.me is None or not _channel_writable(target, guild.me):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "このチャンネルには置けません",
                    f"{target.mention} へ Embed 付きメッセージを送れません。\n"
                    "「チャンネルを見る」「メッセージを送信」「埋め込みリンク」を許可してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        try:
            created = await bot.charge.create_auction(
                guild, name=name, role=role, start_price=int(start_price),
                min_increment=int(min_increment), hours=float(hours),
                duration_days=int(duration_days), description=description,
                created_by=interaction.user.id,
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail or None),
                ephemeral=True,
            )
            return
        auction_id = int(created["auction_id"])
        auction = await bot.db.get_auction(auction_id, guild.id)
        assert auction is not None
        try:
            message = await target.send(
                embed=ui.auction_panel_embed(auction), view=ui.AuctionView()
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            # パネルを出せないオークションは即座に中止する (入札できないため)
            await bot.charge.cancel_auction(
                guild.id, auction_id, operator_id=interaction.user.id,
                reason="パネルを設置できなかったため自動中止",
            )
            await interaction.followup.send(
                embed=ui.info_embed(
                    "パネルを設置できませんでした",
                    f"オークションは中止しました。\n詳細: {utils.safe_error_text(exc)}",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await bot.db.set_auction_message(
            auction_id, channel_id=target.id, message_id=message.id
        )
        op_id = await _audit(
            interaction, "AUCTION_PANEL",
            detail={"auction_id": auction_id, "channel_id": target.id,
                    "message_id": message.id},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ オークションを開始しました",
                f"オークションID: `{auction_id}`\n名前: **{name}**\n"
                f"景品: {role.mention}\n"
                f"開始価格: **{utils.fmt_int(int(start_price))}**\n"
                f"最低更新額: **{utils.fmt_int(int(min_increment))}**\n"
                f"締切: {utils.format_jst(int(created['ends_at']))}\n"
                f"パネル: {target.mention}\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="bid", description="オークションに入札します")
    @app_commands.describe(auction_id="オークションID", amount="入札額")
    @app_commands.guild_only()
    async def bid(
        self,
        interaction: discord.Interaction,
        auction_id: app_commands.Range[int, 1, 10_000_000],
        amount: app_commands.Range[int, 1, config.AUCTION_MAX_BID],
    ) -> None:
        """パネルを使わずに入札する (パネルが流れてしまった場合の代替)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await bot.charge.place_bid(member, int(auction_id), int(amount))
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, next_action=exc.detail or None),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.auction_bid_success_embed(
                name=str(result["name"]), amount=int(result["amount"]),
                balance_after=int(result["balance_after"]),
                ends_at=int(result["ends_at"]), extended=bool(result["extended"]),
                auction_id=int(auction_id),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="オークションの一覧を表示します")
    @app_commands.describe(status="状態で絞り込み")
    @app_commands.choices(status=[
        app_commands.Choice(name=label, value=key)
        for key, label in config.AUCTION_STATUS_LABELS.items()
    ])
    @app_commands.guild_only()
    async def list_auctions(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.list_auctions(
            guild.id, status=status.value if status else None
        )
        await interaction.followup.send(
            embed=ui.auction_list_embed(rows, guild_name=guild.name), ephemeral=True
        )

    @app_commands.command(
        name="close", description="オークションを締切ります (管理者)"
    )
    @app_commands.describe(auction_id="オークションID")
    @app_commands.guild_only()
    @require_admin()
    async def close(
        self,
        interaction: discord.Interaction,
        auction_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        """締切時刻より前に締める (現在の最高額が落札となる)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        auction = await bot.db.get_auction(int(auction_id), guild.id)
        if auction is None:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.AUCTION_NOT_OPEN), ephemeral=True
            )
            return
        outcome = await bot.charge.close_auction(
            int(auction_id), force=True, operator_id=interaction.user.id
        )
        if not outcome.get("closed"):
            await interaction.followup.send(
                embed=ui.error_embed(
                    config.ErrorCode.AUCTION_NOT_OPEN,
                    next_action="このオークションは既に終了しています。",
                ),
                ephemeral=True,
            )
            return
        op_id = await _audit(
            interaction, "AUCTION_FORCE_CLOSE",
            detail={"auction_id": int(auction_id), "reason": outcome.get("reason")},
        )
        if outcome["reason"] == "NO_BIDS":
            body = "入札がなかったため、落札者なしで終了しました。"
        elif outcome.get("delivered"):
            body = (
                f"落札者: <@{outcome['winner_id']}>\n"
                f"落札額: **{utils.fmt_int(int(outcome['winning_bid']))}**\n"
                f"景品: <@&{outcome['role_id']}> を付与しました。"
            )
        else:
            body = (
                "落札者へ景品を渡せなかったため、落札額を返金して中止扱いにしました。\n"
                "ロールの位置と Bot の権限を確認してください。"
            )
        await interaction.followup.send(
            embed=ui.success_embed(
                "🏁 オークションを締切りました", f"{body}\n操作ID: `{op_id}`"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="cancel", description="オークションを中止して全額返金します (管理者)"
    )
    @app_commands.describe(auction_id="オークションID", reason="中止の理由")
    @app_commands.guild_only()
    @require_admin()
    async def cancel(
        self,
        interaction: discord.Interaction,
        auction_id: app_commands.Range[int, 1, 10_000_000],
        reason: str,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await bot.charge.cancel_auction(
                guild.id, int(auction_id), operator_id=interaction.user.id,
                reason=reason.strip()[:300],
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail or None),
                ephemeral=True,
            )
            return
        refunded = result.get("refunded")
        op_id = await _audit(
            interaction, "AUCTION_CANCEL_CMD",
            detail={"auction_id": int(auction_id), "reason": reason[:300]},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "⚫ オークションを中止しました",
                f"オークション: **{result['name']}**\n"
                + (f"返金: <@{refunded['user_id']}> へ "
                   f"**{utils.fmt_int(int(refunded['amount']))}**\n"
                   if refunded else "返金: なし (入札がありませんでした)\n")
                + f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="bids", description="入札の履歴を表示します (管理者)")
    @app_commands.describe(auction_id="オークションID")
    @app_commands.guild_only()
    @require_admin()
    async def bids(
        self,
        interaction: discord.Interaction,
        auction_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        auction = await bot.db.get_auction(int(auction_id), guild.id)
        if auction is None:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.AUCTION_NOT_OPEN), ephemeral=True
            )
            return
        rows = await bot.db.list_auction_bids(int(auction_id), limit=20)
        counts = await bot.db.count_auction_bids(int(auction_id))
        lines = [
            f"{utils.format_jst(int(r['created_at']), with_seconds=True)} "
            f"<@{int(r['user_id'])}> **{utils.fmt_int(int(r['amount']))}**"
            + (" (返金済み)" if int(r["refunded"] or 0) else " (預かり中)")
            for r in rows
        ]
        await interaction.followup.send(
            embed=ui.info_embed(
                f"💸 入札履歴 (`{auction_id}` {auction['name']})",
                f"{ui.SEPARATOR}\n入札 {counts[0]}件 / {counts[1]}人\n\n"
                + ("\n".join(lines) if lines else "入札はまだありません。"),
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /shop (内部残高でロールを販売)
# ---------------------------------------------------------------------------
class ShopGroup(app_commands.Group):
    """ロールショップの管理。"""

    def __init__(self) -> None:
        super().__init__(
            name="shop", description="ショップの管理 (cancel のみ利用者も実行可)"
        )

    @app_commands.command(name="add", description="販売する商品を追加します")
    @app_commands.describe(
        name="商品名", price="価格 (内部残高)",
        item_type="商品の種類 (既定: ロール付与)",
        role="付与するロール (種類がロール付与のときだけ必要)",
        duration_days="有効期間 (0で無期限)",
        subscription="期限が来たら残高から自動で更新する (期間の指定が必要)",
        stock="在庫 (-1で無制限)",
        purchase_limit="1人あたりの購入上限 (0で無制限)", description="説明",
        sort_order="並び順 (小さいほど先)",
        bonus_rate="チャージ率ブーストの上げ幅 (%ポイント)",
        boost_hours="チャージ率ブーストの時間",
        role_color="カスタムロールの色を固定する (#RRGGBB・未指定なら購入者が選べる)",
        category="専用チャンネルを作るカテゴリ",
    )
    @app_commands.choices(item_type=[
        app_commands.Choice(
            name=f"{config.SHOP_ITEM_TYPE_EMOJI[t]} {config.SHOP_ITEM_TYPE_LABELS[t]}", value=t
        )
        for t in config.ALL_SHOP_ITEM_TYPES
    ])
    @app_commands.guild_only()
    @require_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        name: str,
        price: app_commands.Range[int, config.SHOP_PRICE_MIN, config.SHOP_PRICE_MAX],
        item_type: app_commands.Choice[str] | None = None,
        role: discord.Role | None = None,
        duration_days: app_commands.Range[int, 0, config.SHOP_DURATION_MAX_DAYS] = 0,
        subscription: bool = False,
        stock: app_commands.Range[int, -1, 1_000_000] = -1,
        purchase_limit: app_commands.Range[int, 0, 1000] = 1,
        description: str | None = None,
        sort_order: app_commands.Range[int, 0, 10_000] = 0,
        bonus_rate: str | None = None,
        boost_hours: app_commands.Range[int, 1, config.RATE_BOOST_MAX_HOURS] | None = None,
        role_color: str | None = None,
        category: discord.CategoryChannel | None = None,
    ) -> None:
        """商品を追加する。種類ごとに必要な設定が揃っているかを事前に検証する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        # 入口で長さを揃える。DB・Embed・ログで同じ値を使い、表示の食い違いを防ぐ。
        name = name.strip()[:config.SHOP_NAME_MAX_LEN]
        description = (description.strip()[:config.SHOP_DESC_MAX_LEN] or None) if description else None
        kind = item_type.value if item_type else config.ShopItemType.ROLE
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None

        payload: dict[str, Any] = {}
        problem: str | None = None
        if kind == config.ShopItemType.ROLE:
            if role is None:
                problem = "ロール付与の商品には `role` の指定が必要です。"
            else:
                problem = _role_assignable_problem(guild, role)
        elif kind == config.ShopItemType.RATE_BOOST:
            bonus = utils.to_decimal(bonus_rate)
            cap = utils.to_decimal(config.RATE_BOOST_MAX_BONUS)
            if bonus is None or bonus <= 0:
                problem = "チャージ率ブーストには `bonus_rate` (上げ幅) の指定が必要です。"
            elif cap is not None and bonus > cap:
                problem = f"上げ幅は {config.RATE_BOOST_MAX_BONUS} までにしてください。"
            elif boost_hours is None:
                problem = "チャージ率ブーストには `boost_hours` (時間) の指定が必要です。"
            else:
                payload = {"bonus_rate": str(bonus), "hours": int(boost_hours)}
        elif kind == config.ShopItemType.CUSTOM_ROLE:
            if role_color:
                color_value = utils.parse_color(role_color)
                if color_value is None:
                    problem = "色は `#RRGGBB` の形式で指定してください。"
                else:
                    payload = {"color": f"{color_value:06X}"}
        elif kind == config.ShopItemType.PRIVATE_CHANNEL:
            if category is not None:
                payload = {"category_id": category.id}
        if problem is None and subscription and int(duration_days) <= 0:
            problem = "自動更新にするには `duration_days` (更新の間隔) を指定してください。"
        if problem:
            await interaction.followup.send(
                embed=ui.info_embed("この設定では販売できません", problem,
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return

        item_id = await bot.db.add_shop_item(
            guild_id=guild.id, role_id=role.id if role else 0, name=name, price=int(price),
            duration_days=int(duration_days), stock=int(stock),
            purchase_limit=int(purchase_limit),
            description=description,
            sort_order=int(sort_order), created_by=interaction.user.id,
            item_type=kind, subscription=subscription, payload=payload or None,
        )
        # 追加後に Bot 側の権限も確認する (販売してから気付くのを避ける)
        saved = await bot.db.get_shop_item(item_id, guild.id)
        warning = bot.charge.shop_item_problem(guild, saved) if saved else None
        op_id = await _audit(
            interaction, "SHOP_ITEM_ADD",
            detail={"item_id": item_id, "item_type": kind,
                    "role_id": role.id if role else None, "price": int(price),
                    "duration_days": int(duration_days), "stock": int(stock),
                    "subscription": bool(subscription), "payload": payload},
        )
        await bot.charge.refresh_shop_panels(guild.id)
        embed = ui.success_embed(
            "✅ 商品を追加しました",
            f"商品ID: `{item_id}`\n商品名: **{name}**\n"
            f"種類: {config.SHOP_ITEM_TYPE_EMOJI.get(kind, '🎫')} "
            f"{config.SHOP_ITEM_TYPE_LABELS.get(kind, kind)}\n"
            + (f"ロール: {role.mention}\n" if role else "")
            + f"価格: **{utils.fmt_int(int(price))}**\n"
            + (f"期間: {duration_days}日"
               + (" ごとに自動更新" if subscription else "") + "\n"
               if duration_days else "期間: 無期限\n")
            + f"在庫: {'無制限' if stock < 0 else stock}\n"
            f"購入上限: {'無制限' if purchase_limit == 0 else f'{purchase_limit}回'}\n"
            + (f"追加設定: `{utils.safe_json_dumps(payload, limit=300)}`\n" if payload else "")
            + f"操作ID: `{op_id}`",
        )
        embed.add_field(
            name="この種類について",
            value=config.SHOP_ITEM_TYPE_DESCRIPTIONS.get(kind, "-")
            + ("\n利用者は購入時に内容を入力します。"
               if kind in config.SHOP_TYPES_NEED_INPUT else ""),
            inline=False,
        )
        if warning:
            embed.add_field(
                name="⚠️ 今のままでは購入できません",
                value=f"{warning}\n解消するまでこの商品は購入時に拒否されます。",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="edit", description="商品の設定を変更します")
    @app_commands.describe(
        item_id="商品ID", price="価格", stock="在庫 (-1で無制限)",
        duration_days="有効期間 (0で無期限)", purchase_limit="購入上限 (0で無制限)",
        active="販売中かどうか", name="商品名", description="説明",
    )
    @app_commands.guild_only()
    @require_admin()
    async def edit(
        self,
        interaction: discord.Interaction,
        item_id: app_commands.Range[int, 1, 10_000_000],
        price: app_commands.Range[int, config.SHOP_PRICE_MIN, config.SHOP_PRICE_MAX] | None = None,
        stock: app_commands.Range[int, -1, 1_000_000] | None = None,
        duration_days: app_commands.Range[int, 0, config.SHOP_DURATION_MAX_DAYS] | None = None,
        purchase_limit: app_commands.Range[int, 0, 1000] | None = None,
        active: bool | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        item = await bot.db.get_shop_item(int(item_id), guild_id)
        if item is None:
            await interaction.followup.send(
                embed=ui.info_embed("見つかりません", "この商品IDはこのサーバーに存在しません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        values: dict[str, Any] = {}
        if price is not None:
            values["price"] = int(price)
        if stock is not None:
            values["stock"] = int(stock)
        if duration_days is not None:
            values["duration_days"] = int(duration_days)
        if purchase_limit is not None:
            values["purchase_limit"] = int(purchase_limit)
        if active is not None:
            values["active"] = 1 if active else 0
        if name is not None:
            values["name"] = name.strip()[:config.SHOP_NAME_MAX_LEN]
        if description is not None:
            values["description"] = description.strip()[:config.SHOP_DESC_MAX_LEN] or None
        if not values:
            await interaction.followup.send(
                embed=ui.info_embed("変更項目がありません", "変更したい項目を指定してください。"),
                ephemeral=True,
            )
            return
        await bot.db.update_shop_item(int(item_id), guild_id, **values)
        op_id = await _audit(
            interaction, "SHOP_ITEM_EDIT", detail={"item_id": int(item_id), "changes": values}
        )
        await bot.charge.refresh_shop_panels(guild_id)
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 商品を更新しました",
                f"商品ID: `{item_id}`\n"
                + "\n".join(f"{k}: {v}" for k, v in values.items())
                + f"\n操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="商品を販売停止にします")
    @app_commands.describe(item_id="商品ID")
    @app_commands.guild_only()
    @require_admin()
    async def remove(
        self, interaction: discord.Interaction, item_id: app_commands.Range[int, 1, 10_000_000]
    ) -> None:
        """販売停止にする (購入履歴は保持する)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        item = await bot.db.get_shop_item(int(item_id), guild_id)
        if item is None:
            await interaction.followup.send(
                embed=ui.info_embed("見つかりません", "この商品IDはこのサーバーに存在しません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.db.update_shop_item(int(item_id), guild_id, active=0)
        await _audit(interaction, "SHOP_ITEM_REMOVE", detail={"item_id": int(item_id)})
        await bot.charge.refresh_shop_panels(guild_id)
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 販売を停止しました",
                f"商品ID `{item_id}` (**{item['name']}**) を非表示にしました。\n"
                "既に付与されたロールはそのまま維持されます。",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="商品一覧を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def list_items(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        items = await bot.db.list_shop_items(guild_id, active_only=False)
        settings = await bot.db.get_settings(guild_id)
        if not items:
            await interaction.followup.send(
                embed=ui.info_embed("商品がありません", "`/shop add` で商品を追加できます。"),
                ephemeral=True,
            )
            return
        guild = interaction.guild
        assert guild is not None
        lines = []
        for item in items:
            duration = int(item["duration_days"])
            stock = int(item["stock"])
            kind = str(item["item_type"] or config.ShopItemType.ROLE)
            subscription = bool(int(item["subscription"] or 0)) and duration > 0
            # 売っているつもりで買えない商品を見つけられるようにする
            problem = bot.charge.shop_item_problem(guild, item) if item["active"] else None
            lines.append(
                f"{'🟢' if item['active'] else '⚫'} `{item['id']}` "
                f"{config.SHOP_ITEM_TYPE_EMOJI.get(kind, '🎫')} **{item['name']}**"
                + (f" → <@&{int(item['role_id'])}>" if kind == config.ShopItemType.ROLE else "")
                + (" 🔁" if subscription else "")
                + f"\n　{utils.fmt_int(int(item['price']))} / "
                f"{f'{duration}日' if duration else '無期限'} / "
                f"在庫{'∞' if stock < 0 else stock} / "
                f"上限{'∞' if int(item['purchase_limit']) == 0 else item['purchase_limit']}"
                + (f"\n　⚠️ {problem}" if problem else "")
            )
        embed = ui.info_embed(
            "🛒 商品一覧",
            f"{ui.SEPARATOR}\nショップ: {'🟢 有効' if settings.shop_enabled else '⚫ 無効'}\n\n"
            + "\n".join(lines[:15]),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="cancel", description="自動更新 (サブスク) を停止します"
    )
    @app_commands.describe(
        purchase_id="購入ID (📦 購入履歴で確認できます)",
        user="他の人の自動更新を止める (管理者のみ)",
    )
    @app_commands.guild_only()
    async def cancel(
        self,
        interaction: discord.Interaction,
        purchase_id: app_commands.Range[int, 1, 10_000_000],
        user: discord.User | None = None,
    ) -> None:
        """自動更新を止める (期限までは使える)。

        自分の購入は誰でも止められる。他人の分を止めるのは管理者だけ。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        target_id: int | None = interaction.user.id
        if user is not None and user.id != interaction.user.id:
            if not await bot.is_server_admin(interaction):
                await interaction.followup.send(
                    embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED), ephemeral=True
                )
                return
            target_id = user.id
        try:
            result = await bot.charge.cancel_subscription(
                guild.id, int(purchase_id), user_id=target_id,
                operator_id=interaction.user.id,
            )
        except ChargeError as exc:
            await interaction.followup.send(embed=ui.error_embed(exc.code), ephemeral=True)
            return
        expires = result["expires_at"]
        await interaction.followup.send(
            embed=ui.success_embed(
                "🚫 自動更新を停止しました",
                f"商品: **{result['item_name']}**\n"
                f"購入ID: `{purchase_id}`\n"
                + (f"期限: {utils.format_jst(int(expires))} まで使えます\n"
                   if expires else "")
                + "次回以降の引き落としは行いません。",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="subscriptions", description="自動更新中の購入を一覧します (管理者)"
    )
    @app_commands.describe(user="特定の利用者に絞り込む")
    @app_commands.guild_only()
    @require_admin()
    async def subscriptions(
        self, interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.list_subscriptions(
            guild.id, user_id=user.id if user else None
        )
        await interaction.followup.send(
            embed=ui.subscription_list_embed(rows, guild_name=guild.name), ephemeral=True
        )

    @app_commands.command(name="log", description="購入履歴を表示します")
    @app_commands.describe(status="状態で絞り込み", page="ページ番号")
    @app_commands.choices(status=[
        app_commands.Choice(name=label, value=key)
        for key, label in config.PURCHASE_STATUS_LABELS.items()
    ])
    @app_commands.guild_only()
    @require_admin()
    async def log(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        page: app_commands.Range[int, 1, 200] = 1,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        per_page = 8
        rows, total = await bot.db.list_purchases(
            interaction.guild.id,  # type: ignore[union-attr]
            status=status.value if status else None,
            offset=(page - 1) * per_page, limit=per_page,
        )
        total_pages = max(1, -(-total // per_page))
        lines = [
            f"`{r['id']}` {config.PURCHASE_STATUS_LABELS.get(str(r['status']), str(r['status']))} "
            f"<@{int(r['user_id'])}> **{r['item_name']}** "
            f"{utils.fmt_int(int(r['price']))} / {utils.format_jst(int(r['created_at']))}"
            + (f"\n　期限 {utils.format_jst(r['expires_at'])}" if r["expires_at"] else "")
            for r in rows
        ]
        await interaction.followup.send(
            embed=ui.info_embed(
                "📦 購入履歴",
                f"{ui.SEPARATOR}\n該当 **{utils.fmt_int(total)}** 件 / "
                f"ページ {page}/{total_pages}\n\n"
                + ("\n".join(lines) if lines else "該当する購入はありません。"),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="refund", description="購入を返金してロールを剥奪します")
    @app_commands.describe(purchase_id="購入ID (/shop log で確認)", reason="理由")
    @app_commands.guild_only()
    @require_admin()
    async def refund(
        self,
        interaction: discord.Interaction,
        purchase_id: app_commands.Range[int, 1, 10_000_000],
        reason: str,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        purchase = await bot.db.get_purchase(int(purchase_id))
        if purchase is None or int(purchase["guild_id"]) != guild_id:
            await interaction.response.send_message(
                embed=ui.info_embed("見つかりません", "この購入IDはこのサーバーに存在しません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        approved = await _confirm(
            interaction,
            title="⚠️ 購入を返金します",
            description=(
                f"購入ID: `{purchase_id}`\n"
                f"対象: <@{int(purchase['user_id'])}>\n"
                f"商品: **{purchase['item_name']}**\n"
                f"返金額: **{utils.fmt_int(int(purchase['price']))}**\n"
                f"ロール <@&{int(purchase['role_id'])}> を剥奪します。\n"
                f"理由: {utils.truncate(reason, 300)}"
            ),
            confirm_label="返金する",
        )
        if not approved:
            return
        try:
            result = await bot.charge.refund_shop_purchase(
                int(purchase_id), operator_id=interaction.user.id, reason=reason
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        await bot.charge.refresh_shop_panels(guild_id)
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 返金しました",
                f"対象: <@{result['user_id']}>\n"
                f"残高: {utils.fmt_int(result['balance_before'])} → "
                f"**{utils.fmt_int(result['balance_after'])}**",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="panel", description="ショップパネルを設置します")
    @app_commands.describe(channel="設置先 (省略時は現在のチャンネル)")
    @app_commands.guild_only()
    @require_admin()
    async def panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        """常設ショップパネルを設置する (複数設置可・既存は削除しない)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                embed=ui.info_embed("設置できません", "テキストチャンネルを指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if guild.me is None or not _channel_writable(target, guild.me):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "権限が不足しています",
                    f"{target.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        settings = await bot.db.get_settings(guild.id)
        items = await bot.db.list_shop_items(guild.id)
        message = await target.send(
            embed=ui.shop_panel_embed(settings, items), view=ui.ShopPanelView()
        )
        panel_id = await bot.db.add_panel(
            guild.id, target.id, message.id, config.PANEL_TYPE_SHOP
        )
        op_id = await _audit(
            interaction, "SHOP_PANEL_CREATE",
            detail={"panel_id": panel_id, "channel_id": target.id, "message_id": message.id},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ ショップパネルを設置しました",
                f"チャンネル: {target.mention}\nメッセージID: `{message.id}`\n操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )


def _role_assignable_problem(guild: discord.Guild, role: discord.Role) -> str | None:
    """Bot がそのロールを付与できない理由を返す (問題なければ None)。"""
    me = guild.me
    if me is None:
        return "Bot のメンバー情報を取得できません。"
    if not me.guild_permissions.manage_roles:
        return "Bot に「ロールの管理」権限がありません。"
    if role.is_default():
        return "@everyone は指定できません。"
    if role.managed:
        return "連携により自動管理されているロールは付与できません。"
    if role >= me.top_role:
        return (
            f"{role.mention} は Bot の最上位ロール ({me.top_role.mention}) 以上の位置にあります。\n"
            "Bot のロールをこのロールより上へ移動してください。"
        )
    return None


# ---------------------------------------------------------------------------
# /campaign (招待キャンペーン)
# ---------------------------------------------------------------------------
class CampaignGroup(app_commands.Group):
    """招待キャンペーンの管理。"""

    def __init__(self) -> None:
        super().__init__(name="campaign", description="招待キャンペーンの管理 (管理者)")

    @app_commands.command(name="create", description="招待キャンペーンを開始します")
    @app_commands.describe(
        name="キャンペーン名",
        inviter_reward="招待した人への報酬 (内部残高)",
        invited_reward="招待された人への報酬 (内部残高)",
        preset="不正対策のしきい値プリセット",
        confirm_condition="報酬を確定させる条件",
        require_days="滞在日数の条件 (日。0で無効)",
        ends_at="終了日 (YYYY-MM-DD。省略で無期限)",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="標準 (作成7日以上/1日5人/累計50人)", value="STANDARD"),
            app_commands.Choice(name="厳格 (作成30日以上/1日3人/累計20人/全件レビュー)", value="STRICT"),
            app_commands.Choice(name="緩め (作成1日以上/1日10人/累計200人)", value="LOOSE"),
        ],
        confirm_condition=[
            app_commands.Choice(name="チャージ完了で確定 (推奨・不正に強い)", value="CHARGE"),
            app_commands.Choice(name="滞在日数で確定", value="DAYS"),
            app_commands.Choice(name="チャージ完了 かつ 滞在日数", value="BOTH"),
            app_commands.Choice(name="参加時点で確定 (非推奨)", value="JOIN"),
        ],
    )
    @app_commands.guild_only()
    @require_admin()
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        inviter_reward: app_commands.Range[int, 0, 1_000_000],
        invited_reward: app_commands.Range[int, 0, 1_000_000] = config.DEFAULT_INVITED_REWARD,
        preset: app_commands.Choice[str] | None = None,
        confirm_condition: app_commands.Choice[str] | None = None,
        require_days: app_commands.Range[int, 0, 365] = 0,
        ends_at: str | None = None,
    ) -> None:
        """キャンペーンを開始する (既存の開催中キャンペーンは自動終了)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        # 入口で長さを揃える (確認画面・ログ・DB で同じ値になるようにする)
        name = name.strip()[:config.CAMPAIGN_NAME_MAX_LEN]
        if not name:
            await interaction.response.send_message(
                embed=ui.info_embed("入力が不正です", "キャンペーン名を入力してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        preset_key = preset.value if preset else config.DEFAULT_CAMPAIGN_PRESET
        values = config.CAMPAIGN_PRESETS[preset_key]
        condition = confirm_condition.value if confirm_condition else "CHARGE"
        require_charge = condition in ("CHARGE", "BOTH")
        days = int(require_days) if condition in ("DAYS", "BOTH") else 0
        if condition in ("DAYS", "BOTH") and days <= 0:
            days = 7  # 日数条件を選んだ場合の既定値
        ends_ts = _parse_date(ends_at)
        if ends_at and ends_ts is None:
            await interaction.response.send_message(
                embed=ui.info_embed("入力が不正です", "終了日は `YYYY-MM-DD` 形式で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return

        # 招待者の特定に必要な権限を事前に確認する
        warnings: list[str] = []
        if guild.me is None or not guild.me.guild_permissions.manage_guild:
            warnings.append(
                "⚠️ Bot に「サーバー管理」権限がないため、**招待者を特定できません**。\n"
                "　権限を付与してから開始してください。"
            )
        if guild.me is not None and not guild.me.guild_permissions.create_instant_invite:
            warnings.append("⚠️ Bot に「招待を作成」権限がないため、招待リンクを発行できません。")

        conditions_text = {
            "CHARGE": "招待された人がチャージを1回完了したら確定",
            "DAYS": f"参加から {days} 日の滞在で確定",
            "BOTH": f"チャージ完了 かつ 参加から {days} 日の滞在で確定",
            "JOIN": "参加した時点で確定 (**自作自演を防げません**)",
        }[condition]
        approved = await _confirm(
            interaction,
            title="招待キャンペーンを開始します",
            description=(
                f"名称: **{name}**\n"
                f"報酬: 招待者 **{utils.fmt_int(int(inviter_reward))}** / "
                f"参加者 **{utils.fmt_int(int(invited_reward))}**\n"
                f"確定条件: {conditions_text}\n"
                f"アカウント作成: {values['min_account_age_days']}日以上\n"
                f"上限: 1日 {values['daily_limit']}人 / 累計 {values['total_limit']}人\n"
                f"全件レビュー: {'必要' if values['require_review'] else '不要'}\n"
                f"終了日: {utils.format_jst(ends_ts) if ends_ts else '無期限'}\n"
                + ("\n" + "\n".join(warnings) if warnings else "")
            ),
            confirm_label="開始する",
            danger=False,
        )
        if not approved:
            return
        campaign_id = await bot.db.create_campaign(
            guild_id=guild.id, name=name, inviter_reward=int(inviter_reward),
            invited_reward=int(invited_reward),
            min_account_age_days=int(values["min_account_age_days"]),
            daily_limit=int(values["daily_limit"]), total_limit=int(values["total_limit"]),
            require_charge=require_charge, require_days=days,
            require_review=bool(values["require_review"]),
            starts_at=utils.now_ts(), ends_at=ends_ts, created_by=interaction.user.id,
        )
        op_id = await _audit(
            interaction, "CAMPAIGN_CREATE",
            detail={"campaign_id": campaign_id, "name": name, "preset": preset_key,
                    "condition": condition, "inviter_reward": int(inviter_reward),
                    "invited_reward": int(invited_reward), "require_days": days},
        )
        # 招待キャッシュを初期化しておく (帰属判定の基準)
        await bot.charge.sync_invite_cache(guild)
        await bot.charge.refresh_invite_panels(guild.id)
        await bot.charge.log_event(
            guild.id, "🤝 招待キャンペーンを開始しました",
            fields=(
                ("名称", name, True),
                ("報酬", f"招待者 {utils.fmt_int(int(inviter_reward))} / "
                         f"参加者 {utils.fmt_int(int(invited_reward))}", True),
                ("確定条件", conditions_text, False),
                ("操作者", interaction.user.mention, True),
                ("操作ID", f"`{op_id}`", True),
            ),
            color=config.Color.ACCENT,
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ キャンペーンを開始しました",
                f"キャンペーンID: `{campaign_id}`\n"
                f"`/campaign panel` で招待パネルを設置してください。\n操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="edit", description="開催中のキャンペーン設定を変更します")
    @app_commands.describe(
        inviter_reward="招待者への報酬", invited_reward="参加者への報酬",
        daily_limit="1日の上限 (0で無制限)", total_limit="累計上限 (0で無制限)",
        min_account_age_days="アカウント作成からの最低日数", require_review="全件を管理者レビューにする",
    )
    @app_commands.guild_only()
    @require_admin()
    async def edit(
        self,
        interaction: discord.Interaction,
        inviter_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        invited_reward: app_commands.Range[int, 0, 1_000_000] | None = None,
        daily_limit: app_commands.Range[int, 0, 1000] | None = None,
        total_limit: app_commands.Range[int, 0, 100_000] | None = None,
        min_account_age_days: app_commands.Range[int, 0, 3650] | None = None,
        require_review: bool | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        campaign = await bot.db.get_active_campaign(guild_id)
        if campaign is None:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.CAMPAIGN_NOT_ACTIVE), ephemeral=True
            )
            return
        values: dict[str, Any] = {}
        if inviter_reward is not None:
            values["inviter_reward"] = int(inviter_reward)
        if invited_reward is not None:
            values["invited_reward"] = int(invited_reward)
        if daily_limit is not None:
            values["daily_limit"] = int(daily_limit)
        if total_limit is not None:
            values["total_limit"] = int(total_limit)
        if min_account_age_days is not None:
            values["min_account_age_days"] = int(min_account_age_days)
        if require_review is not None:
            values["require_review"] = 1 if require_review else 0
        if not values:
            await interaction.followup.send(
                embed=ui.info_embed("変更項目がありません", "変更したい項目を指定してください。"),
                ephemeral=True,
            )
            return
        await bot.db.update_campaign(int(campaign["id"]), **values)
        op_id = await _audit(
            interaction, "CAMPAIGN_EDIT",
            detail={"campaign_id": int(campaign["id"]), "changes": values},
        )
        await bot.charge.refresh_invite_panels(guild_id)
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ キャンペーンを更新しました",
                "\n".join(f"{k}: {v}" for k, v in values.items()) + f"\n操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="end", description="開催中のキャンペーンを終了します")
    @app_commands.guild_only()
    @require_admin()
    async def end(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        campaign = await bot.db.get_active_campaign(guild_id)
        if campaign is None:
            await interaction.response.send_message(
                embed=ui.error_embed(config.ErrorCode.CAMPAIGN_NOT_ACTIVE), ephemeral=True
            )
            return
        approved = await _confirm(
            interaction,
            title="⚠️ キャンペーンを終了します",
            description=(
                f"**{campaign['name']}** を終了します。\n"
                "保留中の招待は確定されなくなります (既に確定した報酬は維持されます)。"
            ),
            confirm_label="終了する",
        )
        if not approved:
            return
        await bot.db.end_campaign(guild_id)
        op_id = await _audit(
            interaction, "CAMPAIGN_END", detail={"campaign_id": int(campaign["id"])}
        )
        await bot.charge.refresh_invite_panels(guild_id)
        await interaction.followup.send(
            embed=ui.success_embed("✅ キャンペーンを終了しました", f"操作ID: `{op_id}`"),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="キャンペーンの一覧を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def list_campaigns(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.list_campaigns(interaction.guild.id)  # type: ignore[union-attr]
        if not rows:
            await interaction.followup.send(
                embed=ui.info_embed("キャンペーンがありません", "`/campaign create` で開始できます。"),
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for r in rows:
            mark = "🟢" if r["status"] == config.CampaignStatus.ACTIVE else "⚫"
            days = int(r["require_days"] or 0)
            parts: list[str] = []
            if r["require_charge"]:
                parts.append("チャージ")
            if days:
                parts.append(f"{days}日滞在")
            condition = " + ".join(parts) if parts else "参加のみ"
            lines.append(
                f"{mark} `{r['id']}` **{r['name']}**\n"
                f"　報酬 {utils.fmt_int(int(r['inviter_reward']))}/"
                f"{utils.fmt_int(int(r['invited_reward']))} / 条件 {condition} / "
                f"開始 {utils.format_jst(r['starts_at'])}"
            )
        await interaction.followup.send(
            embed=ui.info_embed("🤝 キャンペーン一覧", f"{ui.SEPARATOR}\n" + "\n".join(lines)),
            ephemeral=True,
        )

    @app_commands.command(name="review", description="保留中の招待を承認/却下します")
    @app_commands.describe(
        record_id="記録ID (省略時は保留一覧を表示)", approve="True=承認 / False=却下", reason="理由"
    )
    @app_commands.guild_only()
    @require_admin()
    async def review(
        self,
        interaction: discord.Interaction,
        record_id: app_commands.Range[int, 1, 10_000_000] | None = None,
        approve: bool | None = None,
        reason: str = "管理者確認",
    ) -> None:
        """不審と判定された招待を管理者が確認する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        if record_id is None or approve is None:
            rows, total = await bot.db.list_invite_records(
                guild_id, status=config.InviteStatus.HOLD, limit=10
            )
            lines = [
                f"`{r['id']}` <@{int(r['invited_id'])}> ← <@{r['inviter_id']}>\n"
                f"　理由: {config.INVITE_REASON_LABELS.get(str(r['reason']), str(r['reason'] or '-'))}"
                f" / 参加 {utils.format_jst(int(r['joined_at']))}"
                for r in rows
            ]
            await interaction.followup.send(
                embed=ui.info_embed(
                    f"🟠 確認待ちの招待 ({total} 件)",
                    f"{ui.SEPARATOR}\n"
                    + ("\n".join(lines) if lines else "確認待ちの招待はありません。")
                    + "\n\n`/campaign review record_id: approve:` で承認・却下できます。",
                ),
                ephemeral=True,
            )
            return
        record = await bot.db.get_invite_record(int(record_id))
        if record is None or int(record["guild_id"]) != guild_id:
            await interaction.followup.send(
                embed=ui.info_embed("見つかりません", "この記録IDはこのサーバーに存在しません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        try:
            result = await bot.charge.review_invite(
                int(record_id), approve=approve, operator_id=interaction.user.id, reason=reason
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                f"✅ 招待を{'承認' if approve else '却下'}しました",
                f"記録ID: `{record_id}`\n"
                + ("報酬を付与しました。" if result["confirmed"]
                   else "条件の判定を継続します。" if approve else "報酬は付与されません。")
                + f"\n操作ID: `{result['operation_id']}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="stats", description="招待キャンペーンの実績を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def stats(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        campaign = await bot.db.get_active_campaign(guild_id)
        counts: dict[str, int] = {}
        for status in (config.InviteStatus.CONFIRMED, config.InviteStatus.PENDING,
                       config.InviteStatus.HOLD, config.InviteStatus.REJECTED):
            _, total = await bot.db.list_invite_records(guild_id, status=status, limit=1)
            counts[status] = total
        top = await bot.db.get_invite_ranking(guild_id, limit=10)
        embed = ui.info_embed(
            "🤝 招待キャンペーンの実績",
            f"{ui.SEPARATOR}\n"
            + (f"開催中: **{campaign['name']}**" if campaign else "開催中のキャンペーンはありません")
            + f"\n{ui.SEPARATOR}",
        )
        for status, count in counts.items():
            embed.add_field(
                name=config.INVITE_STATUS_LABELS.get(status, status),
                value=f"{count}人", inline=True,
            )
        if top:
            embed.add_field(
                name="招待ランキング",
                value="\n".join(
                    f"{i}. <@{int(r['user_id'])}> {r['total']}人"
                    for i, r in enumerate(top, start=1)
                ),
                inline=False,
            )
        rejected_rows, _ = await bot.db.list_invite_records(
            guild_id, status=config.InviteStatus.REJECTED, limit=25
        )
        if rejected_rows:
            breakdown: dict[str, int] = {}
            for row in rejected_rows:
                key = str(row["reason"] or "-")
                breakdown[key] = breakdown.get(key, 0) + 1
            embed.add_field(
                name="無効の内訳 (直近25件)",
                value="\n".join(
                    f"{config.INVITE_REASON_LABELS.get(k, k)}: {v}件"
                    for k, v in sorted(breakdown.items(), key=lambda x: -x[1])
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="blacklist", description="招待報酬の対象外ユーザーを管理します")
    @app_commands.describe(action="操作", user="対象ユーザー", reason="理由")
    @app_commands.choices(action=[
        app_commands.Choice(name="追加", value="add"),
        app_commands.Choice(name="解除", value="remove"),
        app_commands.Choice(name="一覧", value="list"),
    ])
    @app_commands.guild_only()
    @require_admin()
    async def blacklist(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.Member | None = None,
        reason: str = "不正な招待",
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        if action.value == "list":
            rows = await bot.db.list_invite_blacklist(guild_id)
            lines = [
                f"<@{int(r['user_id'])}> — {utils.truncate(str(r['reason'] or '-'), 60)} "
                f"({utils.format_jst(int(r['created_at']))})"
                for r in rows
            ]
            await interaction.followup.send(
                embed=ui.info_embed(
                    "🚫 招待ブラックリスト",
                    f"{ui.SEPARATOR}\n" + ("\n".join(lines) if lines else "登録はありません。"),
                ),
                ephemeral=True,
            )
            return
        if user is None:
            await interaction.followup.send(
                embed=ui.info_embed("対象を指定してください", "user を指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if action.value == "add":
            await bot.db.add_invite_blacklist(guild_id, user.id, reason, interaction.user.id)
            await _audit(interaction, "INVITE_BLACKLIST_ADD", target_user_id=user.id,
                         detail={"reason": utils.truncate(reason, 300)})
            await interaction.followup.send(
                embed=ui.success_embed(
                    "🚫 ブラックリストへ追加しました",
                    f"{user.mention} は招待報酬の対象外になります。",
                ),
                ephemeral=True,
            )
        else:
            removed = await bot.db.remove_invite_blacklist(guild_id, user.id)
            await _audit(interaction, "INVITE_BLACKLIST_REMOVE", target_user_id=user.id)
            await interaction.followup.send(
                embed=ui.success_embed(
                    "✅ 解除しました" if removed else "登録がありません",
                    f"{user.mention} のブラックリスト登録を解除しました。" if removed
                    else f"{user.mention} は登録されていません。",
                ),
                ephemeral=True,
            )

    @app_commands.command(name="panel", description="招待キャンペーンのパネルを設置します")
    @app_commands.describe(channel="設置先 (省略時は現在のチャンネル)")
    @app_commands.guild_only()
    @require_admin()
    async def panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                embed=ui.info_embed("設置できません", "テキストチャンネルを指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if guild.me is None or not _channel_writable(target, guild.me):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "権限が不足しています",
                    f"{target.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        settings = await bot.db.get_settings(guild.id)
        campaign = await bot.db.get_active_campaign(guild.id)
        message = await target.send(
            embed=ui.invite_panel_embed(settings, campaign), view=ui.InvitePanelView()
        )
        panel_id = await bot.db.add_panel(
            guild.id, target.id, message.id, config.PANEL_TYPE_INVITE
        )
        op_id = await _audit(
            interaction, "INVITE_PANEL_CREATE",
            detail={"panel_id": panel_id, "channel_id": target.id, "message_id": message.id},
        )
        note = ""
        if guild.me is not None and not guild.me.guild_permissions.manage_guild:
            note = "\n⚠️ Bot に「サーバー管理」権限がないため招待者を特定できません。"
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 招待パネルを設置しました",
                f"チャンネル: {target.mention}\nメッセージID: `{message.id}`\n"
                f"操作ID: `{op_id}`{note}",
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /inspect (統合調査ビュー)
# ---------------------------------------------------------------------------
@app_commands.command(name="inspect", description="利用者の情報を1画面で確認します (管理者)")
@app_commands.describe(user="対象ユーザー")
@app_commands.guild_only()
@require_admin()
async def inspect_command(interaction: discord.Interaction, user: discord.Member) -> None:
    """残高・取引・招待・購入・不正の兆候をまとめて表示する (不正調査用)。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None
    guild_id = guild.id
    balance = await bot.db.get_balance(guild_id, user.id)
    summary = await bot.db.get_user_charge_summary(guild_id, user.id)
    rank, _, total = await bot.db.get_user_rank(guild_id, user.id)
    user_row = await bot.db.get_user(guild_id, user.id)
    active = await bot.db.get_active_transaction(guild_id, user.id)
    audit = await bot.db.audit_balance(guild_id, user.id)
    history = await bot.db.get_member_history(guild_id, user.id)
    purchases = await bot.db.list_user_purchases(guild_id, user.id, limit=5)
    invite_summary = await bot.db.get_invite_summary(guild_id, user.id)
    invited_record = await bot.db.get_invite_record_for_invited(guild_id, user.id)
    txs, tx_total = await bot.db.search_transactions(
        guild_id=guild_id, user_id=user.id, limit=5
    )
    failed_rows, failed_total = await bot.db.search_transactions(
        guild_id=guild_id, user_id=user.id, status=config.TxStatus.FAILED, limit=1
    )
    cooldown = bot.charge.cooldown_remaining(guild_id, user.id)
    charge_rate, rate_role = await bot.charge.resolve_charge_rate(
        guild_id, user.id, await bot.db.get_settings(guild_id)
    )
    charge_rate, rate_boost = await bot.charge.apply_rate_boost(
        guild_id, user.id, charge_rate
    )
    subscriptions = await bot.db.list_subscriptions(guild_id, user_id=user.id)
    account_age_days = (utils.now_ts() - int(user.created_at.timestamp())) / 86400
    fraud_rows, fraud_total = await bot.db.list_fraud_flags(
        guild_id, status=None, user_id=user.id, limit=5
    )
    fraud_open = sum(
        1 for f in fraud_rows if str(f["status"]) == config.FraudStatus.OPEN
    )

    embed = ui.info_embed(
        f"🔎 {user.display_name} の調査ビュー",
        f"{ui.SEPARATOR}\n{user.mention} (`{user.id}`)\n{ui.SEPARATOR}",
        color=config.Color.INFO,
    )
    rank_text = f"{rank}位 / {total}人" if rank else "対象外"
    diff_text = (
        "🟢 一致" if audit["diff"] == 0
        else f"🔴 差分 {utils.fmt_int(audit['diff'])}"
    )
    embed.add_field(
        name="残高",
        value=(
            f"現在: **{utils.fmt_int(balance)}**\n"
            f"順位: {rank_text}\n"
            f"突合: {diff_text}"
        ),
        inline=True,
    )
    embed.add_field(
        name="チャージ",
        value=(
            f"完了: {summary['count']}回\n"
            f"送金累計: {utils.fmt_yen(summary['sent'])}\n"
            f"獲得累計: {utils.fmt_int(summary['credited'])}\n"
            f"全取引: {tx_total}件 (失敗 {failed_total}件)"
        ),
        inline=True,
    )
    embed.add_field(
        name="状態",
        value=(
            f"凍結: {'🧊 凍結中' if (user_row and user_row['frozen']) else '🟢 通常'}\n"
            f"クールダウン: {f'{cooldown}秒' if cooldown else 'なし'}\n"
            f"適用レート: {utils.fmt_rate(charge_rate)}"
            + (f" (<@&{rate_role}>)" if rate_role else "")
            + (f"\n⚡ ブースト中: +{utils.fmt_rate(rate_boost)}" if rate_boost else "")
            + (f"\n🔁 自動更新: {len(subscriptions)}件" if subscriptions else "")
        ),
        inline=True,
    )
    if fraud_total:
        embed.add_field(
            name="🛡 不正検知",
            value=(
                f"検知 **{fraud_total}** 件 (未処理 **{fraud_open}** 件)\n"
                + "\n".join(
                    f"`{int(f['id'])}` "
                    f"{config.FRAUD_KIND_LABELS.get(str(f['kind']), str(f['kind']))} / "
                    f"{config.FRAUD_STATUS_LABELS.get(str(f['status']), str(f['status']))}"
                    for f in fraud_rows
                )
            ),
            inline=False,
        )
    embed.add_field(
        name="アカウント",
        value=(
            f"作成: {utils.format_jst(int(user.created_at.timestamp()))} "
            f"({account_age_days:.0f}日前)\n"
            f"参加: {utils.format_jst(int(user.joined_at.timestamp())) if user.joined_at else '-'}\n"
            + (
                f"参加回数: {history['join_count']}回 / 退出 {history['leave_count']}回"
                if history else "参加履歴: 記録なし"
            )
        ),
        inline=False,
    )
    if active is not None:
        embed.add_field(
            name="進行中の取引",
            value=(
                f"`{active['id']}` "
                f"{config.STATUS_LABELS.get(str(active['status']), str(active['status']))} / "
                f"{utils.fmt_yen(int(active['requested_amount']))}"
            ),
            inline=False,
        )
    if txs:
        embed.add_field(
            name="直近の取引",
            value="\n".join(
                f"{config.STATUS_EMOJI.get(str(t['status']), '⚪')} `{t['id']}` "
                f"{utils.fmt_yen(int(t['requested_amount']))} "
                f"{utils.format_jst(int(t['created_at']))}"
                + (f" `{t['error_code']}`" if t["error_code"] else "")
                for t in txs
            ),
            inline=False,
        )
    embed.add_field(
        name="招待",
        value=(
            f"招待した人数: 確定 {invite_summary['confirmed']} / 保留 {invite_summary['pending']} / "
            f"要確認 {invite_summary['hold']} / 無効 {invite_summary['rejected']}\n"
            f"獲得報酬: {utils.fmt_int(invite_summary['reward'])}\n"
            + (
                f"このユーザー自身の招待元: <@{invited_record['inviter_id']}> "
                f"({config.INVITE_STATUS_LABELS.get(str(invited_record['status']), '-')})"
                if invited_record and invited_record["inviter_id"] else "招待元: なし"
            )
        ),
        inline=False,
    )
    if purchases:
        embed.add_field(
            name="購入履歴 (直近)",
            value="\n".join(
                f"{config.PURCHASE_STATUS_LABELS.get(str(p['status']), str(p['status']))} "
                f"`{p['id']}` {p['item_name']} {utils.fmt_int(int(p['price']))}"
                for p in purchases
            ),
            inline=False,
        )
    flags: list[str] = []
    if account_age_days < 7:
        flags.append("アカウント作成から7日未満")
    if history and int(history["leave_count"]) > 0:
        flags.append(f"退出・再入場の履歴あり ({history['leave_count']}回)")
    if failed_total >= 3:
        flags.append(f"失敗した取引が多い ({failed_total}件)")
    if audit["diff"] != 0:
        flags.append("残高と履歴合計が不一致")
    if invite_summary["hold"]:
        flags.append(f"確認待ちの招待 {invite_summary['hold']}件")
    if flags:
        embed.add_field(
            name="⚠️ 注意すべき点",
            value="\n".join(f"・{f}" for f in flags),
            inline=False,
        )
    embed.set_footer(text="`/balance ledger user:` で残高台帳、`/history user:` で全取引を確認できます")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /admin_panel (管理ダッシュボード)
# ---------------------------------------------------------------------------
@app_commands.command(
    name="admin_panel", description="管理ダッシュボードを設置します (管理者)"
)
@app_commands.describe(channel="設置先 (省略時は現在のチャンネル)")
@app_commands.guild_only()
@require_admin()
async def admin_panel_command(
    interaction: discord.Interaction, channel: discord.TextChannel | None = None
) -> None:
    """常設の管理ダッシュボードを設置する (自動更新・ボタン操作つき)。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None
    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            embed=ui.info_embed("設置できません", "テキストチャンネルを指定してください。",
                                color=config.Color.DANGER),
            ephemeral=True,
        )
        return
    if guild.me is None or not _channel_writable(target, guild.me):
        await interaction.followup.send(
            embed=ui.info_embed(
                "権限が不足しています",
                f"{target.mention} へメッセージ送信・埋め込みリンクの権限が必要です。",
                color=config.Color.DANGER,
            ),
            ephemeral=True,
        )
        return
    if target.permissions_for(guild.default_role).view_channel:
        approved = await _confirm(
            interaction,
            title="⚠️ このチャンネルは全員が閲覧できます",
            description=(
                f"{target.mention} は @everyone が閲覧可能です。\n"
                "管理ダッシュボードには運用状況が表示されます。\n"
                "管理者専用チャンネルへの設置を推奨します。"
            ),
            confirm_label="このまま設置する",
        )
        if not approved:
            return
    embed = await bot.charge.build_admin_panel_embed(guild.id)
    message = await target.send(embed=embed, view=ui.AdminPanelView())
    panel_id = await bot.db.add_panel(guild.id, target.id, message.id, config.PANEL_TYPE_ADMIN)
    op_id = await _audit(
        interaction, "ADMIN_PANEL_CREATE",
        detail={"panel_id": panel_id, "channel_id": target.id, "message_id": message.id},
    )
    await interaction.followup.send(
        embed=ui.success_embed(
            "✅ 管理ダッシュボードを設置しました",
            f"チャンネル: {target.mention}\nメッセージID: `{message.id}`\n"
            f"{config.TASK_RANKING_INTERVAL} 秒ごとに自動更新します。\n操作ID: `{op_id}`",
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /export (CSV 出力)
# ---------------------------------------------------------------------------
class ExportGroup(app_commands.Group):
    """CSV 出力 (監査・経理用)。"""

    def __init__(self) -> None:
        super().__init__(name="export", description="CSV 出力 (管理者)")

    @staticmethod
    def _csv_file(rows: Sequence[Sequence[Any]], header: Sequence[str], name: str) -> discord.File:
        """行データから CSV ファイルを作る (Excel 互換の BOM 付き UTF-8)。"""
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        data = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        return discord.File(io.BytesIO(data), filename=name)

    @app_commands.command(name="transactions", description="取引履歴を CSV で出力します")
    @app_commands.describe(days="直近何日分か (既定30日)", status="状態で絞り込み")
    @app_commands.choices(status=STATUS_CHOICES)
    @app_commands.guild_only()
    @require_admin()
    async def transactions(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 30,
        status: app_commands.Choice[str] | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        rows, total = await bot.db.search_transactions(
            guild_id=guild_id, status=status.value if status else None,
            date_from=utils.now_ts() - int(days) * 86400, limit=5000,
        )
        data = [
            [
                r["id"], r["user_id"], r["requested_amount"], r["received_amount"],
                r["charge_rate"], r["credited_amount"], r["status"], r["source"],
                r["error_code"] or "", r["retry_count"],
                utils.format_jst(r["created_at"], with_seconds=True),
                utils.format_jst(r["completed_at"], with_seconds=True) if r["completed_at"] else "",
                utils.format_jst(r["refunded_at"], with_seconds=True) if r["refunded_at"] else "",
            ]
            for r in rows
        ]
        await _audit(interaction, "EXPORT_TRANSACTIONS", detail={"rows": len(data), "days": days})
        await interaction.followup.send(
            embed=ui.success_embed(
                "📤 取引履歴を出力しました",
                f"出力 **{len(data)}** 件 (該当 {total} 件 / 直近 {days} 日)\n"
                "※ 送金リンクは含まれません (保存していません)",
            ),
            file=self._csv_file(
                data,
                ["transaction_id", "user_id", "requested_amount", "received_amount",
                 "charge_rate", "credited_amount", "status", "source", "error_code",
                 "retry_count", "created_at", "completed_at", "refunded_at"],
                f"transactions_{guild_id}.csv",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="balances", description="残高一覧を CSV で出力します")
    @app_commands.guild_only()
    @require_admin()
    async def balances(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        rows = await bot.db.list_guild_balances(guild_id)
        guild = interaction.guild
        data = []
        for r in rows:
            member = guild.get_member(int(r["user_id"])) if guild else None
            data.append([
                r["user_id"], member.name if member else "", r["balance"],
                "1" if r["frozen"] else "0",
                utils.format_jst(r["updated_at"], with_seconds=True),
            ])
        await _audit(interaction, "EXPORT_BALANCES", detail={"rows": len(data)})
        await interaction.followup.send(
            embed=ui.success_embed("📤 残高一覧を出力しました", f"出力 **{len(data)}** 件"),
            file=self._csv_file(
                data, ["user_id", "user_name", "balance", "frozen", "updated_at"],
                f"balances_{guild_id}.csv",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="ledger", description="残高台帳を CSV で出力します")
    @app_commands.describe(days="直近何日分か (既定30日)", user="対象ユーザー")
    @app_commands.guild_only()
    @require_admin()
    async def ledger(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 30,
        user: discord.Member | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        rows, total = await bot.db.list_balance_history_filtered(
            guild_id, user_id=user.id if user else None,
            date_from=utils.now_ts() - int(days) * 86400, limit=5000,
        )
        data = [
            [
                r["id"], r["user_id"], r["type"], r["change_amount"], r["balance_before"],
                r["balance_after"], r["transaction_id"] or "", r["operator_id"] or "",
                (r["reason"] or "").replace("\n", " "), r["undo_of"] or "",
                utils.format_jst(r["created_at"], with_seconds=True),
            ]
            for r in rows
        ]
        await _audit(interaction, "EXPORT_LEDGER", detail={"rows": len(data), "days": days})
        await interaction.followup.send(
            embed=ui.success_embed(
                "📤 残高台帳を出力しました",
                f"出力 **{len(data)}** 件 (該当 {total} 件 / 直近 {days} 日)",
            ),
            file=self._csv_file(
                data,
                ["history_id", "user_id", "type", "change_amount", "balance_before",
                 "balance_after", "transaction_id", "operator_id", "reason", "undo_of",
                 "created_at"],
                f"ledger_{guild_id}.csv",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="audit", description="監査ログを CSV で出力します")
    @app_commands.describe(days="直近何日分か (既定90日)")
    @app_commands.guild_only()
    @require_admin()
    async def audit_log(
        self, interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 90,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        rows, total = await bot.db.list_audit_logs(guild_id=guild_id, limit=5000)
        cutoff = utils.now_ts() - int(days) * 86400
        data = [
            [
                r["id"], r["operation_id"] or "", r["action"], r["actor_id"],
                r["target_user_id"] or "", (r["detail"] or "").replace("\n", " "),
                utils.format_jst(r["created_at"], with_seconds=True),
            ]
            for r in rows if int(r["created_at"]) >= cutoff
        ]
        await _audit(interaction, "EXPORT_AUDIT", detail={"rows": len(data), "days": days})
        await interaction.followup.send(
            embed=ui.success_embed(
                "📤 監査ログを出力しました", f"出力 **{len(data)}** 件 (全 {total} 件)"
            ),
            file=self._csv_file(
                data,
                ["id", "operation_id", "action", "actor_id", "target_user_id", "detail",
                 "created_at"],
                f"audit_{guild_id}.csv",
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /global (Bot Owner 向け横断ビュー)
# ---------------------------------------------------------------------------
class GlobalGroup(app_commands.Group):
    """全サーバー横断の管理 (Bot Owner 専用)。"""

    def __init__(self) -> None:
        super().__init__(name="global", description="全サーバー横断の管理 (Bot Owner)")

    @app_commands.command(name="overview", description="全サーバーの状況を一覧表示します")
    @require_owner()
    async def overview(self, interaction: discord.Interaction) -> None:
        """サーバーごとのチャージ額・残高・要確認件数・最終稼働を一覧する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.get_global_overview()
        joined = {g.id: g for g in bot.guilds}
        lines: list[str] = []
        total_received = 0
        total_balance = 0
        for row in rows:
            guild_id = int(row["guild_id"])
            guild = joined.get(guild_id)
            total_received += int(row["received"] or 0)
            total_balance += int(row["balance"] or 0)
            mark = "🟢" if row["status"] == "ALLOWED" else "🚫"
            warn = ""
            if int(row["review"] or 0):
                warn += f" 🟠{row['review']}"
            if row["expires_at"]:
                warn += f" ⏰{utils.format_jst(int(row['expires_at']))}"
            lines.append(
                f"{mark} **{guild.name if guild else f'(未参加) {guild_id}'}**{warn}\n"
                f"　送金 {utils.fmt_yen(int(row['received'] or 0))} / "
                f"残高 {utils.fmt_int(int(row['balance'] or 0))} / "
                f"成功 {row['charges']} / 失敗 {row['failed']}\n"
                f"　最終稼働 {utils.format_jst(row['last_activity'])}"
            )
        embed = ui.info_embed(
            "🌐 全サーバーの状況",
            f"{ui.SEPARATOR}\n"
            f"許可済み {await bot.db.count_allowed_guilds()} / 参加 {len(bot.guilds)}\n"
            f"総送金額 **{utils.fmt_yen(total_received)}** / "
            f"総残高 **{utils.fmt_int(total_balance)}**\n{ui.SEPARATOR}\n"
            + ("\n".join(lines[:15]) if lines else "データがありません。"),
        )
        embed.set_footer(text="🟠=手動確認待ち / ⏰=許可の有効期限")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stats", description="Bot 全体の統計を表示します")
    @require_owner()
    async def stats(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        stats = await bot.db.get_statistics(None)
        metrics = bot.charge.metrics_snapshot()
        success_rate = (stats["success"] / stats["total"] * 100) if stats["total"] else 0.0
        embed = ui.info_embed(
            "🌐 Bot 全体の統計",
            f"{ui.SEPARATOR}\n"
            f"総チャージ **{utils.fmt_int(stats['total'])}** 回 / "
            f"成功率 {success_rate:.1f}%\n"
            f"総送金額 **{utils.fmt_yen(stats['sent'])}** / "
            f"総付与 **{utils.fmt_int(stats['credited'])}**\n{ui.SEPARATOR}",
        )
        embed.add_field(
            name="今日 / 今月",
            value=(
                f"{utils.fmt_int(stats['today_count'])}回 "
                f"({utils.fmt_yen(stats['today_sent'])})\n"
                f"{utils.fmt_int(stats['month_count'])}回 "
                f"({utils.fmt_yen(stats['month_sent'])})"
            ),
            inline=True,
        )
        embed.add_field(
            name="利用者 / 残高",
            value=(
                f"{utils.fmt_int(stats['users'])}人\n"
                f"{utils.fmt_int(stats['total_balance'])}"
            ),
            inline=True,
        )
        embed.add_field(
            name="処理中 / 要確認",
            value=f"{stats['in_progress']} / {stats['manual_review']}",
            inline=True,
        )
        embed.add_field(
            name="メトリクス (起動後)",
            value=(
                f"完了 {metrics['charges_completed']} / 失敗 {metrics['charges_failed']}\n"
                f"平均受取 {metrics['receive_avg_seconds']}秒\n"
                f"購入 {metrics['purchases']} / 招待確定 {metrics['invites_confirmed']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="稼働時間",
            value=utils.format_duration(utils.now_ts() - bot.started_at),
            inline=True,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /tier (累計チャージによる自動昇格)
# ---------------------------------------------------------------------------
class TierGroup(app_commands.Group):
    """累計チャージ額による段位 (自動昇格・降格なし)。"""

    def __init__(self) -> None:
        super().__init__(name="tier", description="累計チャージによる段位の管理 (管理者)")

    @app_commands.command(name="add", description="段位を追加します")
    @app_commands.describe(
        name="段位の名前 (例: ゴールド)",
        threshold="この累計チャージ額に達したら付与",
        role="付与するロール",
        description="特典の説明 (任意)",
    )
    @app_commands.guild_only()
    @require_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        name: str,
        threshold: app_commands.Range[int, 1, 1_000_000_000],
        role: discord.Role,
        description: str | None = None,
    ) -> None:
        """段位を追加する。

        チャージ率は付与したロールへ ``/rate set`` で設定する。
        レートの決まり方を1系統に保つため、段位自体はレートを持たない。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        label = name.strip()[:config.CUSTOM_NAME_MAX_LEN]
        if not label:
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "段位の名前を入力してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        problem = bot.charge.role_grant_problem(guild, role)
        if problem:
            await interaction.followup.send(
                embed=ui.info_embed("このロールは使えません", problem,
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        try:
            tier_id = await bot.db.add_tier(
                guild_id=guild.id, name=label, threshold=int(threshold), role_id=role.id,
                description=(description.strip()[:300] if description else None),
                created_by=interaction.user.id,
            )
        except DatabaseError as exc:
            await interaction.followup.send(
                embed=ui.info_embed("追加できません", str(exc), color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        op_id = await _audit(
            interaction, "TIER_ADD",
            detail={"tier_id": tier_id, "name": label, "threshold": int(threshold),
                    "role_id": role.id},
        )
        rates = {int(r["role_id"]): r for r in await bot.db.list_role_rates(guild.id)}
        hint = (
            f"このロールのチャージ率は **{utils.fmt_rate(rates[role.id]['charge_rate'])}** です。"
            if role.id in rates else
            f"チャージ率を上げるには `/rate set role:{role.name} charge_rate:140` を実行してください。"
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 段位を追加しました",
                f"段位ID: `{tier_id}`\n名前: **{label}**\n"
                f"しきい値: 累計 **{utils.fmt_yen(int(threshold))}**\n"
                f"ロール: {role.mention}\n操作ID: `{op_id}`\n\n{hint}\n"
                "※ 既にしきい値を超えている利用者へは、次回のチャージまたは定期判定で付与されます。",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="段位を削除します")
    @app_commands.describe(tier_id="段位ID (`/tier list` で確認)")
    @app_commands.guild_only()
    @require_admin()
    async def remove(
        self, interaction: discord.Interaction,
        tier_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        tier = await bot.db.get_tier(guild.id, int(tier_id))
        if tier is None:
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed("見つかりません", "その段位IDは存在しません。",
                                    color=config.Color.DANGER),
            )
            return
        approved = await _confirm(
            interaction,
            title=f"段位「{tier['name']}」を削除します",
            description=(
                "設定と付与記録を削除します。\n"
                "**既に付与したロールは外れません** (必要なら手動で外してください)。\n"
                "同じ段位を作り直すと、条件を満たす人へ再度付与されます。"
            ),
            confirm_label="削除する",
        )
        if not approved:
            return
        removed = await bot.db.remove_tier(guild.id, int(tier_id))
        op_id = await _audit(
            interaction, "TIER_REMOVE", detail={"tier_id": int(tier_id)}
        )
        await interaction.followup.send(
            embed=ui.info_embed(
                "削除しました" if removed else "見つかりません",
                f"段位ID: `{tier_id}`\n操作ID: `{op_id}`",
                color=config.Color.NEUTRAL,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="段位の一覧を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def list_tiers(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        tiers = await bot.db.list_tiers(guild.id)
        rates = {
            int(r["role_id"]): utils.fmt_rate(r["charge_rate"])
            for r in await bot.db.list_role_rates(guild.id)
        }
        await interaction.followup.send(
            embed=ui.tier_list_embed(tiers, guild_name=guild.name, rates=rates),
            ephemeral=True,
        )

    @app_commands.command(
        name="sweep", description="条件を満たす利用者へ段位をまとめて付与します"
    )
    @app_commands.guild_only()
    @require_admin()
    async def sweep(self, interaction: discord.Interaction) -> None:
        """しきい値を変更した後などに、取りこぼしを拾うための手動実行。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        promoted = 0
        for user_id in await bot.db.list_tier_candidates(guild.id, limit=500):
            promoted += len(await bot.charge.check_tiers(guild.id, user_id))
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 段位の判定を実行しました",
                f"新しく付与した段位: **{promoted} 件**\n"
                "既に付与済みの段位は変更していません。",
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /ranking_reward (ランキング上位への自動配布)
# ---------------------------------------------------------------------------
_RANKING_PERIOD_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, label in config.RANKING_PERIOD_LABELS.items()
]


class RankingRewardGroup(app_commands.Group):
    """締めた期間のランキング上位へ報酬を自動配布する。"""

    def __init__(self) -> None:
        super().__init__(
            name="ranking_reward", description="ランキング報酬の管理 (管理者)"
        )

    @app_commands.command(name="set", description="順位範囲への報酬を設定します")
    @app_commands.describe(
        period="集計期間", rank_from="開始順位", rank_to="終了順位",
        amount="配布する残高 (0 でロールのみ)", role="付与するロール (任意)",
    )
    @app_commands.choices(period=_RANKING_PERIOD_CHOICES)
    @app_commands.guild_only()
    @require_admin()
    async def set_reward(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str],
        rank_from: app_commands.Range[int, 1, config.MAX_REWARD_RANK],
        rank_to: app_commands.Range[int, 1, config.MAX_REWARD_RANK],
        amount: app_commands.Range[int, 0, 1_000_000_000] = 0,
        role: discord.Role | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        if int(rank_from) > int(rank_to):
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "開始順位が終了順位を超えています。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if int(amount) == 0 and role is None:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "入力が不正です", "残高かロールの少なくとも一方を指定してください。",
                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        if role is not None:
            problem = bot.charge.role_grant_problem(guild, role)
            if problem:
                await interaction.followup.send(
                    embed=ui.info_embed("このロールは使えません", problem,
                                        color=config.Color.DANGER),
                    ephemeral=True,
                )
                return
        reward_id = await bot.db.set_ranking_reward(
            guild_id=guild.id, ranking_type=period.value, rank_from=int(rank_from),
            rank_to=int(rank_to), amount=int(amount),
            role_id=role.id if role else None, created_by=interaction.user.id,
        )
        op_id = await _audit(
            interaction, "RANKING_REWARD_SET",
            detail={"reward_id": reward_id, "period": period.value,
                    "rank_from": int(rank_from), "rank_to": int(rank_to),
                    "amount": int(amount), "role_id": role.id if role else None},
        )
        rank_text = (f"{rank_from}位" if int(rank_from) == int(rank_to)
                     else f"{rank_from}〜{rank_to}位")
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ ランキング報酬を設定しました",
                f"期間: **{period.name}**\n対象: **{rank_text}**\n"
                + (f"残高: **{utils.fmt_int(int(amount))}**\n" if amount else "")
                + (f"ロール: {role.mention}\n" if role else "")
                + f"操作ID: `{op_id}`\n\n"
                + ("週間は毎週月曜 00:00 (JST)、月間は毎月1日 00:00 (JST) に"
                   "**締めた期間**を自動配布します。"),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="報酬の設定を削除します")
    @app_commands.describe(reward_id="設定ID (`/ranking_reward list` で確認)")
    @app_commands.guild_only()
    @require_admin()
    async def remove(
        self, interaction: discord.Interaction,
        reward_id: app_commands.Range[int, 1, 10_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        removed = await bot.db.remove_ranking_reward(guild.id, int(reward_id))
        op_id = await _audit(
            interaction, "RANKING_REWARD_REMOVE", detail={"reward_id": int(reward_id)}
        )
        await interaction.followup.send(
            embed=ui.info_embed(
                "削除しました" if removed else "見つかりません",
                f"設定ID: `{reward_id}`\n操作ID: `{op_id}`",
                color=config.Color.NEUTRAL if removed else config.Color.DANGER,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="list", description="報酬の設定と直近の配布を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def list_rewards(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        await interaction.followup.send(
            embed=ui.ranking_reward_list_embed(
                await bot.db.list_ranking_rewards(guild.id),
                guild_name=guild.name,
                grants=await bot.db.list_ranking_grants(guild.id, limit=10),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="run", description="報酬の配布を手動で実行します")
    @app_commands.describe(
        period="集計期間", current="進行中の期間で配布する (既定は締めた期間)",
    )
    @app_commands.choices(period=_RANKING_PERIOD_CHOICES)
    @app_commands.guild_only()
    @require_admin()
    async def run(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str],
        current: bool = False,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        start, end, period_key = (
            utils.current_period_bounds(period.value) if current
            else utils.period_bounds(period.value)
        )
        approved = await _confirm(
            interaction,
            title=f"{period.name}ランキングの報酬を配布します",
            description=(
                f"対象期間: **{period_key}**\n"
                f"{utils.format_jst(start)} 〜 {utils.format_jst(end)}\n"
                + ("⚠️ **進行中の期間**で配布します。以後この期間は自動配布されません。\n"
                   if current else "")
                + "\n配布済みの利用者へは二重に配布されません。"
            ),
            confirm_label="配布する",
            danger=False,
        )
        if not approved:
            return
        result = await bot.charge.distribute_ranking_rewards(
            guild.id, period.value, use_current=current, operator_id=interaction.user.id
        )
        await _audit(
            interaction, "RANKING_REWARD_RUN",
            detail={"period": period.value, "period_key": result.get("period_key"),
                    "granted": result.get("granted"), "current": current},
        )
        if result.get("already"):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "すでに配布済みです",
                    f"期間 **{result['period_key']}** の配布は完了しています。",
                    color=config.Color.NEUTRAL,
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "🏆 配布しました",
                f"期間: **{result.get('period_key')}**\n"
                f"配布: **{result.get('granted')} 人**\n"
                f"重複スキップ: {result.get('skipped')} 人\n\n"
                + ("報酬の設定がないか、期間中のチャージがありませんでした。"
                   if not result.get("granted") else "実績チャンネルへ結果を投稿しました。"),
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /provider (チャージ方式の設定)
# ---------------------------------------------------------------------------
# 管理コマンドでは Kyash を1つだけ並べる。
# 「送金リンク」「請求リンク」を別々に並べると設定が方式ごとに分かれてしまい、
# /provider kyash_mode で切り替えた瞬間にレートや上限が別の値へ化ける。
# 受け取り方は必ず /provider kyash_mode で決める。
_PROVIDER_CHOICES = [
    app_commands.Choice(name=config.family_label(p), value=p)
    for p in config.ADMIN_PROVIDERS
]
_MANUAL_PROVIDER_CHOICES = [
    app_commands.Choice(name=config.PROVIDER_LABELS[p], value=p)
    for p in config.MANUAL_PROVIDERS
]


class ProviderGroup(app_commands.Group):
    """チャージ方式 (Kyash / PayPay / LTC) の設定。

    入金先と審査チャンネルは Bot Owner が全サーバー共通で設定する。
    有効/無効・チャージ率・金額上下限はサーバーごとに管理者が設定する。
    """

    def __init__(self) -> None:
        super().__init__(name="provider", description="チャージ方式の設定")

    # --- 管理者 ---
    @app_commands.command(name="status", description="チャージ方式の状態を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def status(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        settings = await bot.db.get_settings(guild.id)
        entries = await bot.charge.provider_availability(guild.id, settings)
        price = await bot.price.status_snapshot()
        delegated = guild.id in await bot.charge.get_delegated_guilds()
        paypay = await bot.db.get_destination(config.ChargeProvider.PAYPAY)
        await interaction.followup.send(
            embed=ui.provider_status_embed(
                entries,
                guild_name=guild.name,
                review_channel_id=await bot.charge.get_review_channel_id(),
                price=price,
                delegated=delegated,
                settings=settings,
                paypay_link=str(paypay["claim_url"]) if paypay and paypay["claim_url"]
                else None,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="enable", description="チャージ方式の受付を切り替えます")
    @app_commands.describe(provider="対象の方式", enabled="受け付けるかどうか")
    @app_commands.choices(provider=_PROVIDER_CHOICES)
    @app_commands.guild_only()
    @require_admin()
    async def enable(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
        enabled: bool,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        await bot.db.set_provider_settings(
            guild.id, provider.value, enabled=enabled, updated_by=interaction.user.id
        )
        op_id = await _audit(
            interaction, "PROVIDER_ENABLE",
            detail={"provider": provider.value, "enabled": bool(enabled)},
        )
        await bot.charge.refresh_charge_panels(guild.id)
        await bot.charge.log_event(
            guild.id, "⚙️ チャージ方式の受付を変更",
            fields=(
                ("方式", provider.name, True),
                ("状態", "受付中" if enabled else "停止", True),
                ("操作ID", f"`{op_id}`", True),
            ),
        )
        embed = ui.success_embed(
            "✅ 受付状態を変更しました",
            f"{provider.name}: **{'受付中' if enabled else '停止'}**\n操作ID: `{op_id}`",
        )
        # Kyash は受け取り方が2つあるので、片方だけ止めたつもりにさせない
        family = config.provider_family(provider.value)
        methods = config.FAMILY_PROVIDERS.get(family, ())
        if len(methods) > 1:
            names = " / ".join(config.PROVIDER_LABELS.get(m, m) for m in methods)
            embed.add_field(
                name="対象",
                value=(
                    f"{names} の**両方**が対象です。\n"
                    + ("受け取り方を変えたいだけなら "
                       f"`{config.FAMILY_MODE_COMMANDS.get(family)}` を使ってください。"
                       if not enabled else "")
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="rate", description="方式ごとのチャージ率を設定します")
    @app_commands.describe(
        provider="対象の方式",
        charge_rate="例: 130 / 145.5。`clear` でサーバー既定に戻します",
    )
    @app_commands.choices(provider=_PROVIDER_CHOICES)
    @app_commands.guild_only()
    @require_admin()
    async def rate(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
        charge_rate: str,
    ) -> None:
        """方式別レートはサーバー既定を置き換える。

        ロール別レート (VIP 等) と併用した場合は**高い方**が適用される。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        clear = charge_rate.strip().lower() in ("clear", "reset", "既定", "なし")
        parsed = None if clear else utils.validate_charge_rate(charge_rate)
        if not clear and parsed is None:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "入力が不正です",
                    f"チャージ率は {config.MIN_CHARGE_RATE}〜{config.MAX_CHARGE_RATE} で"
                    "指定してください (`clear` で既定に戻します)。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await bot.db.set_provider_settings(
            guild.id, provider.value, charge_rate=parsed, clear_rate=clear,
            updated_by=interaction.user.id,
        )
        op_id = await _audit(
            interaction, "PROVIDER_RATE_SET",
            detail={"provider": provider.value,
                    "charge_rate": None if clear else str(parsed)},
        )
        await bot.charge.refresh_charge_panels(guild.id)
        settings = await bot.db.get_settings(guild.id)
        await bot.charge.log_event(
            guild.id, "⚙️ 方式別チャージ率を設定",
            fields=(
                ("方式", provider.name, True),
                ("チャージ率",
                 "サーバー既定に戻しました" if clear else utils.fmt_rate(parsed), True),
                ("操作ID", f"`{op_id}`", True),
            ),
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ チャージ率を設定しました",
                f"{provider.name}: "
                + ("**サーバー既定** "
                   f"({utils.fmt_rate(settings.charge_rate)}) に戻しました"
                   if clear else f"**{utils.fmt_rate(parsed)}**")
                + f"\n操作ID: `{op_id}`\n\n"
                "※ ロール別レート (`/rate set`) と併用した場合は**高い方**が適用されます。",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="limits", description="方式ごとの金額上下限を設定します")
    @app_commands.describe(
        provider="対象の方式",
        minimum="最低額 (0 でサーバー既定に戻す)",
        maximum="最大額 (0 でサーバー既定に戻す)",
    )
    @app_commands.choices(provider=_PROVIDER_CHOICES)
    @app_commands.guild_only()
    @require_admin()
    async def limits(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
        minimum: app_commands.Range[int, 0, 100_000_000],
        maximum: app_commands.Range[int, 0, 100_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        clear = int(minimum) == 0 and int(maximum) == 0
        if not clear and int(minimum) > 0 and int(maximum) > 0 and minimum > maximum:
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "最低額が最大額を超えています。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.db.set_provider_settings(
            guild.id, provider.value,
            minimum_charge=int(minimum) if not clear and minimum > 0 else None,
            maximum_charge=int(maximum) if not clear and maximum > 0 else None,
            clear_limits=clear, updated_by=interaction.user.id,
        )
        op_id = await _audit(
            interaction, "PROVIDER_LIMITS_SET",
            detail={"provider": provider.value, "minimum": int(minimum),
                    "maximum": int(maximum), "clear": clear},
        )
        settings = await bot.db.get_settings(guild.id)
        low, high = await bot.charge.provider_limits(guild.id, provider.value, settings)
        # パネルには受付範囲を出しているため、投稿済みのパネルも作り直す
        await bot.charge.refresh_charge_panels(guild.id)
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 金額の範囲を設定しました",
                f"{provider.name}: **{utils.fmt_yen(low)} 〜 {utils.fmt_yen(high)}**\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="kyash_mode", description="Kyash の受け取り方を選びます (管理者)"
    )
    @app_commands.describe(mode="どちらか一方だけを利用者に見せます")
    @app_commands.choices(mode=[
        app_commands.Choice(name=label, value=key)
        for key, label in config.KYASH_MODE_LABELS.items()
    ])
    @app_commands.guild_only()
    @require_admin()
    async def kyash_mode(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        """送金リンク方式 / 請求リンク方式 を切り替える。

        2つ同時に出すと利用者が迷うため、必ずどちらか一方だけを見せる。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.db.update_settings(guild.id, kyash_mode=mode.value)
        op_id = await _audit(
            interaction, "PROVIDER_KYASH_MODE", detail={"mode": mode.value}
        )
        provider = config.KYASH_MODE_PROVIDER[mode.value]
        await bot.charge.refresh_charge_panels(guild.id)
        embed = ui.success_embed(
            "✅ Kyash の受け取り方を変更しました",
            f"方式: **{mode.name}**\n"
            f"利用者に見せるのは **{config.PROVIDER_LABELS[provider]}** だけになります。\n"
            f"操作ID: `{op_id}`",
        )
        embed.add_field(
            name="この方式について",
            value=config.KYASH_MODE_DESCRIPTIONS[mode.value],
            inline=False,
        )
        if not bot.kyash.is_usable:
            embed.add_field(
                name="⚠️ まだ使えません",
                value="受取用 Kyash アカウントが未ログインです。`/kyash login` を先に実行してください。",
                inline=False,
            )
        # 受付停止のままだと、方式を変えても利用者には何も出ない
        row = await bot.db.get_provider_settings(guild.id, config.ChargeProvider.KYASH)
        if row is not None and not row["enabled"]:
            embed.add_field(
                name="⚠️ Kyash は受付停止中です",
                value=(
                    "いま Kyash は受付を止めているため、方式を変えても利用者には出ません。\n"
                    "`/provider enable provider:Kyash enabled:True` で再開してください。"
                ),
                inline=False,
            )
        # 前の受け取り方で進行中の取引は、そのまま続けられる (勝手に消さない)
        pending = await bot.db.count_transactions_by_provider(
            guild.id,
            statuses=(config.TxStatus.CREATED, config.TxStatus.WAITING_LINK,
                      config.TxStatus.WAITING_PAYMENT),
        )
        stale = sum(
            count for name, count in pending.items()
            if config.provider_family(name) == config.ChargeProvider.KYASH
            and name != provider
        )
        if stale:
            embed.add_field(
                name="進行中の取引について",
                value=(
                    f"前の受け取り方で進行中の取引が **{stale} 件** あります。\n"
                    "すでに送金されている可能性があるため取り消しません。"
                    "利用者は前の方式のままその取引を完了できます。"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="paypay_mode", description="PayPay の受け取り方を選びます (管理者)"
    )
    @app_commands.describe(mode="どちらか一方だけを利用者に見せます")
    @app_commands.choices(mode=[
        app_commands.Choice(name=label, value=key)
        for key, label in config.PAYPAY_MODE_LABELS.items()
    ])
    @app_commands.guild_only()
    @require_admin()
    async def paypay_mode(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        """ID方式 / 請求リンク方式 を切り替える。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.db.update_settings(guild.id, paypay_mode=mode.value)
        op_id = await _audit(
            interaction, "PROVIDER_PAYPAY_MODE", detail={"mode": mode.value}
        )
        destination = await bot.db.get_destination(config.ChargeProvider.PAYPAY)
        # パネルとヘルプの手順が受け取り方で変わるため、投稿済みのパネルも作り直す
        await bot.charge.refresh_charge_panels(guild.id)
        embed = ui.success_embed(
            "✅ PayPay の受け取り方を変更しました",
            f"方式: **{mode.name}**\n操作ID: `{op_id}`",
        )
        embed.add_field(
            name="この方式について",
            value=config.PAYPAY_MODE_DESCRIPTIONS[mode.value],
            inline=False,
        )
        if destination is None:
            embed.add_field(
                name="⚠️ まだ使えません",
                value="PayPay の入金先が未登録です。`/provider destination` を実行してください。",
                inline=False,
            )
        elif mode.value == config.PayPayMode.CLAIM_LINK and not str(
            destination["claim_url"] or ""
        ).strip():
            embed.add_field(
                name="⚠️ まだ使えません",
                value="請求リンクが未登録です。`/provider paypay_link` で登録してください。",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="paypay_link", description="PayPay の請求リンクを登録します (Bot Owner)"
    )
    @app_commands.describe(
        url="PayPay の請求リンク (https://... 形式)。空にすると削除します",
    )
    @require_owner()
    async def paypay_link(
        self, interaction: discord.Interaction, url: str | None = None
    ) -> None:
        """請求リンク方式で利用者へ見せるリンクを登録する。

        入金先の ID とは別に保存するため、方式を切り替えても両方が残る。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = (url or "").strip()
        if value and not utils.is_safe_link(value):
            await interaction.followup.send(
                embed=ui.info_embed(
                    "リンクの形式が正しくありません",
                    "`https://` で始まる PayPay のリンクを指定してください。\n"
                    "利用者にそのまま表示されるため、開けることを確認してから登録してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        ok = await bot.db.set_destination_claim_url(
            config.ChargeProvider.PAYPAY,
            claim_url=value or None,
            updated_by=interaction.user.id,
        )
        if not ok:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "先に入金先を登録してください",
                    "`/provider destination provider:PayPay address:<PayPay ID>` を"
                    "実行してから、請求リンクを登録してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        op_id = await _audit(
            interaction, "PROVIDER_PAYPAY_LINK",
            detail={"registered": bool(value)},
        )
        # 入金先は全サーバー共通なので、すべてのパネルの内容が変わる
        await bot.charge.refresh_all_charge_panels()
        if not value:
            await interaction.followup.send(
                embed=ui.success_embed(
                    "🗑 請求リンクを削除しました",
                    f"請求リンク方式は使えなくなります。\n操作ID: `{op_id}`",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ PayPay の請求リンクを登録しました",
                f"登録したリンク:\n{ui.copy_block(value)}\n"
                f"操作ID: `{op_id}`\n\n"
                "`/provider paypay_mode mode:請求リンク方式` で切り替えると、"
                "利用者にこのリンクが表示されます。\n"
                "**利用者にそのまま見せるため、開けることを確認してください。**",
            ),
            ephemeral=True,
        )

    # --- Bot Owner ---
    @app_commands.command(
        name="destination", description="入金先を登録します (Bot Owner)"
    )
    @app_commands.describe(
        provider="対象の方式", address="PayPay ID / LTC アドレス",
        label="表示名 (例: 受取用)", note="利用者へ見せる注意書き",
    )
    @app_commands.choices(provider=_MANUAL_PROVIDER_CHOICES)
    @require_owner()
    async def destination(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
        address: str,
        label: str | None = None,
        note: str | None = None,
    ) -> None:
        """入金先は全サーバー共通。承認も既定では Bot Owner のみが行う。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = address.strip()
        if not value or len(value) > 200:
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "入金先は 1〜200 文字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.db.set_destination(
            provider.value, address=value,
            label=(label.strip()[:60] if label else None),
            note=(note.strip()[:300] if note else None),
            updated_by=interaction.user.id,
        )
        op_id = await _audit(
            interaction, "PROVIDER_DESTINATION_SET",
            detail={"provider": provider.value,
                    "address": utils.mask_identifier(value, keep=6)},
        )
        # 入金先は全サーバー共通なので、すべてのパネルで使える方式が変わる
        await bot.charge.refresh_all_charge_panels()
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 入金先を登録しました",
                f"方式: **{provider.name}**\n"
                f"入金先: ```\n{value}\n```\n"
                f"操作ID: `{op_id}`\n\n"
                "利用者にはこの値がそのまま表示されます。**間違いがないか確認してください。**",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="destination_clear", description="入金先の登録を削除します (Bot Owner)"
    )
    @app_commands.describe(provider="対象の方式")
    @app_commands.choices(provider=_MANUAL_PROVIDER_CHOICES)
    @require_owner()
    async def destination_clear(
        self, interaction: discord.Interaction, provider: app_commands.Choice[str]
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        approved = await _confirm(
            interaction,
            title=f"{provider.name} の入金先を削除します",
            description="削除すると、この方式でのチャージは受け付けられなくなります。",
            confirm_label="削除する",
        )
        if not approved:
            return
        removed = await bot.db.delete_destination(provider.value)
        op_id = await _audit(
            interaction, "PROVIDER_DESTINATION_CLEAR", detail={"provider": provider.value}
        )
        await bot.charge.refresh_all_charge_panels()
        await interaction.followup.send(
            embed=ui.info_embed(
                "削除しました" if removed else "登録されていません",
                f"方式: {provider.name}\n操作ID: `{op_id}`",
                color=config.Color.NEUTRAL,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="review_channel", description="申請の審査チャンネルを設定します (Bot Owner)"
    )
    @app_commands.describe(channel="審査カードを投稿するチャンネル (未指定で解除)")
    @require_owner()
    async def review_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if channel is not None:
            me = channel.guild.me
            if me is None or not _channel_writable(channel, me):
                await interaction.followup.send(
                    embed=ui.info_embed(
                        "そのチャンネルには投稿できません",
                        "Bot に「メッセージを送信」と「埋め込みリンク」の権限が必要です。",
                        color=config.Color.DANGER,
                    ),
                    ephemeral=True,
                )
                return
        await bot.charge.set_review_channel_id(channel.id if channel else None)
        op_id = await _audit(
            interaction, "PROVIDER_REVIEW_CHANNEL",
            detail={"channel_id": channel.id if channel else None},
        )
        # 審査チャンネルの有無で PayPay / LTC が使えるかが変わる
        await bot.charge.refresh_all_charge_panels()
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 審査チャンネルを設定しました" if channel else "審査チャンネルを解除しました",
                (f"投稿先: {channel.mention}\n" if channel else "")
                + f"操作ID: `{op_id}`\n\n"
                + ("すべてのサーバーの申請がここへ集まります。" if channel
                   else "解除中は PayPay / LTC のチャージを受け付けません。"),
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="delegate",
        description="そのサーバーの管理者にも承認を許可します (Bot Owner)",
    )
    @app_commands.describe(guild_id="対象サーバーID (未指定で現在のサーバー)", enabled="許可するか")
    @require_owner()
    async def delegate(
        self, interaction: discord.Interaction, enabled: bool, guild_id: str | None = None
    ) -> None:
        """入金先は Owner のものなので、承認の既定は Owner だけ。

        信頼できるサーバーにだけ、Owner が明示的に承認を委任する。
        """
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = (guild_id or "").strip()
        if raw and not raw.isdigit():
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "サーバーIDは数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        target = int(raw) if raw else (interaction.guild.id if interaction.guild else 0)
        if not target:
            await interaction.followup.send(
                embed=ui.info_embed("対象が不明です", "サーバーIDを指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        current = await bot.charge.set_delegated(target, enabled)
        op_id = await _audit(
            interaction, "PROVIDER_DELEGATE",
            detail={"guild_id": target, "enabled": bool(enabled),
                    "delegated_count": len(current)},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 承認の委任を変更しました",
                f"サーバー: `{target}`\n"
                f"管理者による承認: **{'許可' if enabled else '不可'}**\n"
                f"委任中のサーバー数: {len(current)}\n操作ID: `{op_id}`\n\n"
                + ("⚠️ 入金先は Bot Owner のものです。委任したサーバーの管理者は、"
                   "入金を確認せずに残高を発行できてしまう点に注意してください。"
                   if enabled else ""),
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="price_source", description="LTC 価格の取得元を切り替えます (Bot Owner)"
    )
    @app_commands.describe(source="取得元")
    @app_commands.choices(source=[
        app_commands.Choice(name="CoinGecko API (自動)", value=config.PRICE_SOURCE_COINGECKO),
        app_commands.Choice(name="管理者が設定した固定価格", value=config.PRICE_SOURCE_MANUAL),
    ])
    @require_owner()
    async def price_source(
        self, interaction: discord.Interaction, source: app_commands.Choice[str]
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.price.set_source(source.value)
        op_id = await _audit(
            interaction, "PROVIDER_PRICE_SOURCE", detail={"source": source.value}
        )
        note = (
            "API から取得できない場合は、`/provider price` で設定した固定価格へ"
            "自動で切り替わります (どちらも無い場合は LTC チャージを受け付けません)。"
            if source.value == config.PRICE_SOURCE_COINGECKO
            else "`/provider price` で設定した価格のみを使います。相場との乖離に注意してください。"
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 価格の取得元を変更しました",
                f"取得元: **{source.name}**\n操作ID: `{op_id}`\n\n{note}",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="price", description="LTC の固定価格を設定します (Bot Owner)"
    )
    @app_commands.describe(jpy="1 LTC あたりの円 (0 で削除)")
    @require_owner()
    async def price(self, interaction: discord.Interaction, jpy: str) -> None:
        """API 障害時のフォールバック価格。`/provider price_source manual` で常用もできる。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if jpy.strip() in ("0", "clear", "reset"):
            await bot.db.set_system_value(price_service.KEY_MANUAL_PRICE, "")
            op_id = await _audit(interaction, "PROVIDER_PRICE_CLEAR", detail={})
            await interaction.followup.send(
                embed=ui.info_embed("固定価格を削除しました", f"操作ID: `{op_id}`",
                                    color=config.Color.NEUTRAL),
                ephemeral=True,
            )
            return
        parsed = utils.validate_price_jpy(jpy)
        if parsed is None:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "入力が不正です",
                    f"1 LTC あたりの円を {config.PRICE_MIN_JPY}〜{config.PRICE_MAX_JPY} "
                    "の範囲で指定してください。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        await bot.price.set_manual_price(parsed)
        op_id = await _audit(
            interaction, "PROVIDER_PRICE_SET", detail={"price_jpy": str(parsed)}
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 固定価格を設定しました",
                f"1 LTC = **{utils.fmt_yen(int(parsed))}**\n操作ID: `{op_id}`\n\n"
                "※ 相場が動いた場合、更新を忘れると差額を突かれる恐れがあります。"
                "`/provider status` で最終更新を確認できます。",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="price_check", description="LTC 価格を取得して確認します (Bot Owner)"
    )
    @require_owner()
    async def price_check(self, interaction: discord.Interaction) -> None:
        """実際に価格を取り、使える状態かを確かめる (設定は変えない)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            quote = await bot.price.get_price(force=True)
        except price_service.PriceError as exc:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "🔴 価格を取得できませんでした",
                    f"```\n{utils.truncate(utils.sanitize_for_log(exc.detail, limit=400), 900)}\n```\n"
                    "この状態では LTC チャージを受け付けません。\n"
                    "`/provider price` で固定価格を設定すると、API 障害時も受付を続けられます。",
                    color=config.Color.DANGER,
                ),
                ephemeral=True,
            )
            return
        sample = utils.asset_amount_for(1000, quote.price)
        await interaction.followup.send(
            embed=ui.success_embed(
                "🟢 価格を取得できました",
                f"1 LTC = **{utils.fmt_yen(int(quote.price))}**\n"
                f"取得元: {config.PRICE_SOURCE_LABELS.get(quote.source, quote.source)}"
                + ("  ⚠️ 代替値" if quote.stale else "") + "\n"
                f"取得時刻: {utils.discord_ts(quote.fetched_at)}\n\n"
                f"換算例: 1,000円 → **{utils.fmt_asset(sample)}**",
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /request (チャージ申請の審査)
# ---------------------------------------------------------------------------
_REQUEST_STATUS_CHOICES = [
    app_commands.Choice(name=label, value=value)
    for value, label in config.REQUEST_STATUS_LABELS.items()
]


class RequestGroup(app_commands.Group):
    """チャージ申請の確認・承認・却下。

    承認できるのは Bot Owner (または Owner が委任したサーバーの管理者) のみ。
    入金先が Owner のものであるため、確認できる人だけが承認する。
    """

    def __init__(self) -> None:
        super().__init__(name="request", description="チャージ申請の審査")

    async def _require_reviewer(self, interaction: discord.Interaction) -> bool:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        guild_id = interaction.guild.id if interaction.guild else 0
        if await bot.charge.can_review(guild_id, interaction.user):
            return True
        await ui.safe_respond(
            interaction,
            embed=ui.info_embed(
                "操作できません",
                "申請を処理できるのは Bot Owner "
                "(または Owner が承認を委任したサーバーの管理者) だけです。",
                color=config.Color.DANGER,
            ),
        )
        return False

    @app_commands.command(name="list", description="チャージ申請を一覧表示します")
    @app_commands.describe(
        status="状態で絞り込み", provider="方式で絞り込み",
        user="利用者で絞り込み", page="ページ",
    )
    # 申請 (requests) が作られるのは承認制の方式だけなので、Kyash は並べない
    @app_commands.choices(
        status=_REQUEST_STATUS_CHOICES, provider=_MANUAL_PROVIDER_CHOICES
    )
    @require_admin()
    async def list_requests(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        provider: app_commands.Choice[str] | None = None,
        user: discord.User | None = None,
        page: app_commands.Range[int, 1, 500] = 1,
    ) -> None:
        """一覧は管理者も見られる (承認はできない)。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        is_owner = bot.is_bot_owner(interaction.user)
        page_size = 10
        rows, total = await bot.db.list_requests(
            # Owner はすべてのサーバーを横断して見られる
            guild_id=None if is_owner and interaction.guild is None else (
                interaction.guild.id if interaction.guild else None
            ),
            user_id=user.id if user else None,
            provider=provider.value if provider else None,
            statuses=[status.value] if status else None,
            offset=(int(page) - 1) * page_size,
            limit=page_size,
        )
        total_pages = max(1, -(-total // page_size))
        await interaction.followup.send(
            embed=ui.request_list_embed(
                rows, page=int(page), total_pages=total_pages, total=total,
                title="📨 チャージ申請",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="show", description="申請の詳細を表示します")
    @app_commands.describe(request_id="申請ID")
    @require_admin()
    async def show(
        self, interaction: discord.Interaction,
        request_id: app_commands.Range[int, 1, 100_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        is_owner = bot.is_bot_owner(interaction.user)
        row = await bot.db.get_request(
            int(request_id),
            None if is_owner else (interaction.guild.id if interaction.guild else None),
        )
        if row is None:
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.REQUEST_NOT_FOUND), ephemeral=True
            )
            return
        history, _ = await bot.db.list_requests(
            guild_id=int(row["guild_id"]), user_id=int(row["user_id"]), limit=5
        )
        balance = await bot.db.get_balance(int(row["guild_id"]), int(row["user_id"]))
        await interaction.followup.send(
            embed=ui.review_detail_embed(request=row, history=history, balance=balance),
            ephemeral=True,
        )

    @app_commands.command(name="approve", description="申請を承認して残高を付与します")
    @app_commands.describe(
        request_id="申請ID", amount="付与額を変える場合に指定 (未指定で申請どおり)",
        note="金額を変える理由 (監査ログに残ります)",
    )
    @require_admin()
    async def approve(
        self,
        interaction: discord.Interaction,
        request_id: app_commands.Range[int, 1, 100_000_000],
        amount: app_commands.Range[int, 1, 1_000_000_000] | None = None,
        note: str | None = None,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        if not await self._require_reviewer(interaction):
            return
        row = await bot.db.get_request(int(request_id))
        if row is None:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.REQUEST_NOT_FOUND)
            )
            return
        if amount is not None and not (note or "").strip():
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed(
                    "理由が必要です",
                    "付与額を申請と変える場合は `note` に理由を入力してください。",
                    color=config.Color.DANGER,
                ),
            )
            return
        provider = str(row["provider"])
        credited = int(amount) if amount is not None else int(row["estimated_credit"])
        approved = await _confirm(
            interaction,
            title=f"申請 #{int(request_id)} を承認します",
            description=(
                f"方式: **{config.PROVIDER_LABELS.get(provider, provider)}**\n"
                f"利用者: <@{int(row['user_id'])}>\n"
                f"申請額: **{utils.fmt_yen(int(row['requested_amount']))}**\n"
                + (f"送金数量: **{utils.fmt_asset(row['asset_amount'])}**\n"
                   if row["asset_amount"] is not None else "")
                + f"付与: **{utils.fmt_int(credited)}**"
                + ("  (申請どおり)" if amount is None else "  ⚠️ 申請額から変更")
                + "\n\n**入金が実際に届いていることを確認しましたか？**"
            ),
            confirm_label="承認する",
            danger=False,
        )
        if not approved:
            return
        try:
            result = await bot.charge.approve_request(
                int(request_id), operator_id=interaction.user.id,
                credited_amount=int(amount) if amount is not None else None,
                note=note,
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "🟢 承認しました",
                f"申請ID: `#{request_id}`\n"
                f"付与: **{utils.fmt_int(int(result['credited_amount']))}**\n"
                f"取引ID: `{result['transaction_id']}`\n"
                f"操作ID: `{result['operation_id']}`\n"
                f"残高: {utils.fmt_int(int(result['balance_before']))} → "
                f"**{utils.fmt_int(int(result['balance_after']))}**",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="reject", description="申請を却下します (残高は動きません)")
    @app_commands.describe(request_id="申請ID", reason="却下理由 (利用者へ通知されます)")
    @require_admin()
    async def reject(
        self,
        interaction: discord.Interaction,
        request_id: app_commands.Range[int, 1, 100_000_000],
        reason: str,
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        if not await self._require_reviewer(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await bot.charge.reject_request(
                int(request_id), operator_id=interaction.user.id, reason=reason
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.info_embed(
                "🔴 却下しました",
                f"申請ID: `#{request_id}`\n操作ID: `{result['operation_id']}`\n"
                "残高は変更していません。利用者へ理由を DM で通知しました。",
                color=config.Color.DANGER,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="pending", description="未処理の申請をまとめて表示します")
    @require_admin()
    async def pending(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.list_pending_requests(limit=25)
        if not bot.is_bot_owner(interaction.user) and interaction.guild is not None:
            rows = [r for r in rows if int(r["guild_id"]) == interaction.guild.id]
        counts = await bot.db.count_requests_by_status()
        embed = ui.request_list_embed(
            rows, page=1, total_pages=1, total=len(rows), title="🟡 承認待ちの申請"
        )
        embed.add_field(
            name="全体の集計",
            value="\n".join(
                f"{config.REQUEST_STATUS_LABELS.get(k, k)}: {v}件"
                for k, v in sorted(counts.items())
            ) or "なし",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="cancel", description="申請を取り消します (管理者)")
    @app_commands.describe(request_id="申請ID")
    @require_admin()
    async def cancel(
        self, interaction: discord.Interaction,
        request_id: app_commands.Range[int, 1, 100_000_000],
    ) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        if not await self._require_reviewer(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await bot.db.cancel_request(int(request_id))
        except RequestError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        op_id = await _audit(
            interaction, "REQUEST_CANCEL",
            target_user_id=int(row["user_id"]), detail={"request_id": int(request_id)},
        )
        await bot.charge.update_review_card(int(request_id))
        await interaction.followup.send(
            embed=ui.info_embed(
                "取り消しました",
                f"申請ID: `#{request_id}`\n操作ID: `{op_id}`",
                color=config.Color.NEUTRAL,
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# 登録
# ---------------------------------------------------------------------------
async def setup_commands(bot: "ChargeBot") -> None:
    """すべてのスラッシュコマンドをツリーへ登録する。"""
    tree = bot.tree
    #: app_commands.Command は多相なため、まとめて回すときは Any で受ける。
    singles: tuple[Any, ...] = (
        setup_command, charge_panel_command, panels_command,
        ranking_panel_command, history_command,
        stats_command, queue_command, logs_command, backup_command,
        inspect_command, admin_panel_command,
    )
    for command in singles:
        tree.add_command(command)
    for group in (
        ServerGroup(), KyashGroup(), SettingsGroup(), BalanceGroup(), UserGroup(),
        MaintenanceGroup(), EmergencyStopGroup(), AchievementGroup(), TransactionGroup(),
        DataGroup(), ConfigGroup(), SystemGroup(), RateGroup(), ShopGroup(),
        AuctionGroup(), GoalGroup(), FraudGroup(), RefundGroup(), ReceiptGroup(),
        CampaignGroup(), ExportGroup(), GlobalGroup(),
        ProviderGroup(), RequestGroup(), TierGroup(), RankingRewardGroup(),
    ):
        tree.add_command(group)

    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """コマンドエラーを利用者には安全なメッセージで返す。"""
        if isinstance(error, GuildNotAllowed):
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.GUILD_DISABLED))
            return
        if isinstance(error, PermissionDenied):
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed("⛔ 権限がありません", error.message, color=config.Color.DANGER),
            )
            return
        if isinstance(error, app_commands.CheckFailure):
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED))
            return
        if isinstance(error, app_commands.CommandOnCooldown):
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.RATE_LIMITED))
            return
        original = getattr(error, "original", error)
        logger.exception(
            "コマンド実行でエラーが発生しました command=%s user=%s",
            interaction.command.qualified_name if interaction.command else "?",
            interaction.user.id,
            exc_info=original,
        )
        is_admin = bot.is_bot_owner(interaction.user)
        await ui.safe_respond(
            interaction,
            embed=ui.error_embed(
                config.ErrorCode.UNKNOWN_ERROR,
                admin_detail=utils.safe_error_text(original) if is_admin else None,
            ),
        )

    tree.on_error = on_app_command_error  # type: ignore[assignment]
    logger.info("スラッシュコマンドを登録しました")
