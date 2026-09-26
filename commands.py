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
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

import config
import kyash_service
import ui
import utils
from charge_service import ChargeError

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
) -> bool:
    """確認ボタンを表示し、承認されたかどうかを返す。"""
    view = ui.ConfirmView(
        owner_id=interaction.user.id, confirm_label=confirm_label, stages=stages
    )
    embed = ui.info_embed(title, description, color=config.Color.DANGER)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.wait()
    return bool(view.value)


def _channel_writable(channel: discord.abc.GuildChannel, me: discord.Member) -> bool:
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
    embed = ui.charge_panel_embed(settings, kyash_ready=bot.kyash.is_usable)
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


@app_commands.command(name="charge_panels", description="設置済みチャージパネルの一覧 (管理者)")
@app_commands.describe(disable_message_id="無効化するパネルのメッセージID (省略時は一覧のみ)")
@app_commands.guild_only()
@require_admin()
async def charge_panels_command(
    interaction: discord.Interaction, disable_message_id: str | None = None
) -> None:
    """チャージパネルの一覧表示と無効化。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None

    if disable_message_id:
        if not disable_message_id.strip().isdigit():
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "メッセージIDは数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        message_id = int(disable_message_id.strip())
        panels = await bot.db.list_panels(guild.id, active_only=False)
        if not any(int(p["message_id"]) == message_id for p in panels):
            await interaction.followup.send(
                embed=ui.info_embed("見つかりません", "このサーバーに該当するパネルがありません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.db.deactivate_panel(message_id=message_id)
        await _audit(interaction, "PANEL_DISABLE", detail={"message_id": message_id})
        await interaction.followup.send(
            embed=ui.success_embed("✅ 無効化しました",
                                   f"メッセージID `{message_id}` のパネルを無効化しました。\n"
                                   "メッセージ自体は残るため、不要な場合は手動で削除してください。"),
            ephemeral=True,
        )
        return

    panels = await bot.db.list_panels(guild.id, active_only=False)
    if not panels:
        await interaction.followup.send(
            embed=ui.info_embed("チャージパネル", "まだ設置されていません。`/charge_panel` で設置できます。"),
            ephemeral=True,
        )
        return
    lines = [
        f"{'🟢' if p['active'] else '⚫'} <#{p['channel_id']}> / `{p['message_id']}` / "
        f"{utils.format_jst(p['created_at'])}"
        for p in panels
    ]
    await interaction.followup.send(
        embed=ui.info_embed("💰 チャージパネル一覧", f"{ui.SEPARATOR}\n" + "\n".join(lines[:25])),
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


@app_commands.command(name="ranking_panels", description="設置済みランキングパネルの一覧 (管理者)")
@app_commands.describe(disable_message_id="無効化するパネルのメッセージID (省略時は一覧のみ)")
@app_commands.guild_only()
@require_admin()
async def ranking_panels_command(
    interaction: discord.Interaction, disable_message_id: str | None = None
) -> None:
    """ランキングパネルの一覧表示と無効化 (チャージパネルとは別管理)。"""
    bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    assert guild is not None

    if disable_message_id:
        if not disable_message_id.strip().isdigit():
            await interaction.followup.send(
                embed=ui.info_embed("入力が不正です", "メッセージIDは数字で指定してください。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        message_id = int(disable_message_id.strip())
        panels = await bot.db.list_ranking_panels(guild.id, active_only=False)
        if not any(int(p["message_id"]) == message_id for p in panels):
            await interaction.followup.send(
                embed=ui.info_embed("見つかりません", "このサーバーに該当するパネルがありません。",
                                    color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        await bot.db.deactivate_ranking_panel(message_id=message_id)
        await _audit(interaction, "RANKING_PANEL_DISABLE", detail={"message_id": message_id})
        await interaction.followup.send(
            embed=ui.success_embed("✅ 無効化しました",
                                   f"メッセージID `{message_id}` のランキングパネルを無効化しました。"),
            ephemeral=True,
        )
        return

    panels = await bot.db.list_ranking_panels(guild.id, active_only=False)
    if not panels:
        await interaction.followup.send(
            embed=ui.info_embed("ランキングパネル", "まだ設置されていません。`/ranking_panel` で設置できます。"),
            ephemeral=True,
        )
        return
    lines = [
        f"{'🟢' if p['active'] else '⚫'} <#{p['channel_id']}> / `{p['message_id']}`\n"
        f"　{config.RANKING_TYPE_LABELS.get(str(p['ranking_type']), str(p['ranking_type']))} / "
        f"最終更新 {utils.format_jst(p['last_updated_at'])}"
        for p in panels
    ]
    await interaction.followup.send(
        embed=ui.info_embed("🏆 ランキングパネル一覧", f"{ui.SEPARATOR}\n" + "\n".join(lines[:25])),
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
        # 許可直後にコマンドを使えるよう、そのサーバーへ同期する
        synced = 0
        if guild is not None:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced = len(await bot.tree.sync(guild=guild))
            except discord.HTTPException as exc:
                logger.warning("ギルドコマンド同期に失敗しました guild=%s: %s",
                               target_id, utils.safe_error_text(exc))
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ サーバーを許可しました",
                f"対象: **{guild.name if guild else '未参加'}** (`{target_id}`)\n"
                f"コマンド同期: {synced} 件\n操作ID: `{op_id}`",
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

    @app_commands.command(name="sync", description="スラッシュコマンドを再同期します")
    @require_owner()
    async def sync(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        results: list[str] = []
        try:
            synced = await bot.tree.sync()
            results.append(f"グローバル: {len(synced)} 件")
        except discord.HTTPException as exc:
            results.append(f"グローバル: 失敗 ({utils.safe_error_text(exc, limit=120)})")
        for guild in bot.guilds:
            if not await bot.db.is_guild_allowed(guild.id):
                continue
            try:
                bot.tree.copy_global_to(guild=guild)
                guild_synced = await bot.tree.sync(guild=guild)
                results.append(f"`{guild.id}`: {len(guild_synced)} 件")
            except discord.HTTPException as exc:
                results.append(f"`{guild.id}`: 失敗 ({utils.safe_error_text(exc, limit=80)})")
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
class KyashGroup(app_commands.Group):
    """受取用 Kyash アカウントの管理。"""

    def __init__(self) -> None:
        super().__init__(name="kyash", description="受取用Kyashアカウントの管理")

    @app_commands.command(name="login", description="受取用Kyashアカウントへログインします (Bot Owner)")
    @require_owner()
    async def login(self, interaction: discord.Interaction) -> None:
        """認証情報は Ephemeral Modal で受け取り、ログにも DB にも平文で残さない。"""
        await interaction.response.send_modal(ui.KyashLoginModal())

    @app_commands.command(name="status", description="受取用Kyashアカウントの状態を表示します (管理者)")
    @require_admin()
    async def status(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = bot.kyash.status_snapshot()
        record = await bot.db.get_kyash_account()
        embed = ui.info_embed(
            "🔐 受取用Kyashアカウント",
            f"{ui.SEPARATOR}\n状態: **{config.KYASH_STATUS_LABELS.get(snapshot['status'], snapshot['status'])}**",
            color=config.Color.SUCCESS if snapshot["status"] == config.KyashAccountStatus.ACTIVE
            else config.Color.WARNING,
        )
        embed.add_field(name="セッション", value="保持中" if snapshot["logged_in"] else "なし", inline=True)
        embed.add_field(name="ユーザー名", value=f"`{snapshot['username'] or '-'}`", inline=True)
        embed.add_field(name="最終確認", value=utils.format_jst(snapshot["last_checked_at"]), inline=True)
        embed.add_field(
            name="保存情報",
            value=(
                f"トークン: {'保存済み' if record.access_token_enc else '未保存'}\n"
                f"端末情報: {'保存済み' if record.client_uuid and record.installation_uuid else '未保存'}\n"
                "※パスワードは保存していません"
            ),
            inline=False,
        )
        # 残高照会の可否のみ確認する (金額は Bot Owner にのみ表示)
        wallet_state = "-"
        if snapshot["logged_in"]:
            try:
                wallet = await bot.kyash.get_wallet()
                wallet_state = (
                    f"可能 (残高 {utils.fmt_yen(wallet.all_balance)})"
                    if bot.is_bot_owner(interaction.user) else "可能"
                )
            except kyash_service.KyashServiceError as exc:
                wallet_state = f"不可 ({utils.sanitize_for_log(exc, limit=100)})"
        embed.add_field(name="Wallet取得", value=wallet_state, inline=True)
        embed.add_field(name="モジュール", value=f"Kyasher {snapshot['module_version']}", inline=True)
        if snapshot["last_error"]:
            embed.add_field(
                name="最終エラー",
                value=utils.truncate(utils.sanitize_for_log(snapshot["last_error"]), 900),
                inline=False,
            )
        embed.set_footer(text="パスワード・トークン等の秘密情報は表示されません")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="threshold", description="受取用アカウントの残高しきい値を設定します (Bot Owner)"
    )
    @app_commands.describe(amount="しきい値 (円)。0で無効。到達すると新規チャージを停止します")
    @require_owner()
    async def threshold(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, 100_000_000],
    ) -> None:
        """Kyash 側の残高上限に達して受取が失敗する前に、新規チャージを止めるための設定。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.kyash.set_wallet_threshold(int(amount))
        await _audit(interaction, "KYASH_THRESHOLD", detail={"threshold": int(amount)})
        balance = bot.kyash.last_wallet_balance
        headroom = bot.kyash.wallet_headroom()
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 残高しきい値を設定しました",
                f"しきい値: **{utils.fmt_yen(int(amount)) if amount else '無効'}**\n"
                + (f"現在残高: {utils.fmt_yen(balance)}\n" if balance is not None else "")
                + (f"しきい値まで: {utils.fmt_yen(headroom)}" if headroom is not None else ""),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="logout", description="受取用Kyashアカウントをログアウトします (Bot Owner)")
    @require_owner()
    async def logout(self, interaction: discord.Interaction) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        approved = await _confirm(
            interaction,
            title="⚠️ Kyash アカウントをログアウトします",
            description=(
                "保存済みのアクセストークンと端末情報を削除します。\n"
                "ログアウト後は新規チャージを受け付けられません。\n"
                "受取待ちのチャージは保留され、再ログイン後に処理されます。"
            ),
            confirm_label="ログアウトする",
        )
        if not approved:
            return
        await bot.kyash.logout()
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
        if bot.kyash.status == config.KyashAccountStatus.UNCONFIGURED:
            status = await bot.kyash.restore_from_db()
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

    @app_commands.command(name="info", description="ユーザーの残高と変更履歴を表示します")
    @app_commands.describe(user="対象ユーザー")
    @app_commands.guild_only()
    @require_admin()
    async def info(self, interaction: discord.Interaction, user: discord.Member) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        balance = await bot.db.get_balance(guild_id, user.id)
        summary = await bot.db.get_user_charge_summary(guild_id, user.id)
        rank, _, total = await bot.db.get_user_rank(guild_id, user.id)
        rows, history_total = await bot.db.list_balance_history(guild_id, user.id, limit=10)
        user_row = await bot.db.get_user(guild_id, user.id)
        embed = ui.info_embed(
            f"💳 {user.display_name} の残高",
            f"{ui.SEPARATOR}\n現在残高: **{utils.fmt_int(balance)}**",
        )
        embed.add_field(name="順位", value=f"{rank}位 / {total}人" if rank else "対象外", inline=True)
        embed.add_field(name="累計チャージ", value=f"{summary['count']}回", inline=True)
        embed.add_field(name="累計獲得", value=utils.fmt_int(summary["credited"]), inline=True)
        embed.add_field(
            name="凍結状態",
            value="🧊 凍結中" if (user_row and user_row["frozen"]) else "🟢 通常",
            inline=True,
        )
        if rows:
            lines = [
                f"{utils.format_jst(r['created_at'])} / "
                f"{config.BALANCE_TYPE_LABELS.get(r['type'], r['type'])} / "
                f"{'+' if r['change_amount'] >= 0 else ''}{utils.fmt_int(r['change_amount'])} → "
                f"{utils.fmt_int(r['balance_after'])}"
                + (f" / {utils.truncate(str(r['reason']), 40)}" if r["reason"] else "")
                for r in rows
            ]
            embed.add_field(
                name=f"残高変更履歴 (最新{len(rows)}/{history_total}件)",
                value=utils.truncate("\n".join(lines), 1000),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


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

    @app_commands.command(name="info", description="ユーザーの状態を表示します")
    @app_commands.guild_only()
    @require_admin()
    async def info(self, interaction: discord.Interaction, user: discord.Member) -> None:
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        row = await bot.db.get_user(guild_id, user.id)
        balance = await bot.db.get_balance(guild_id, user.id)
        summary = await bot.db.get_user_charge_summary(guild_id, user.id)
        active = await bot.db.get_active_transaction(guild_id, user.id)
        embed = ui.info_embed(
            f"👤 {user.display_name}",
            f"{ui.SEPARATOR}\nユーザーID: `{user.id}`",
        )
        embed.add_field(name="現在残高", value=utils.fmt_int(balance), inline=True)
        embed.add_field(name="累計チャージ", value=f"{summary['count']}回", inline=True)
        embed.add_field(
            name="凍結", value="🧊 凍結中" if (row and row["frozen"]) else "🟢 通常", inline=True
        )
        if row and row["frozen"]:
            embed.add_field(name="凍結理由", value=utils.truncate(str(row["frozen_reason"] or "-"), 500),
                            inline=False)
            embed.add_field(name="凍結日時", value=utils.format_jst(row["frozen_at"]), inline=True)
        if active is not None:
            embed.add_field(
                name="進行中の取引",
                value=f"`{active['id']}` / {config.STATUS_LABELS.get(active['status'], active['status'])} / "
                      f"{utils.fmt_yen(int(active['requested_amount']))}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


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
        embed.add_field(name="種別", value=str(row["source"]), inline=True)
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
@app_commands.describe(global_scope="Bot全体の統計を表示 (Bot Owner のみ)")
@app_commands.guild_only()
@require_admin()
async def stats_command(
    interaction: discord.Interaction, global_scope: bool = False
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
        f"<@{r['actor_id']}>"
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
# /shop (内部残高でロールを販売)
# ---------------------------------------------------------------------------
class ShopGroup(app_commands.Group):
    """ロールショップの管理。"""

    def __init__(self) -> None:
        super().__init__(name="shop", description="ロールショップの管理 (管理者)")

    @app_commands.command(name="add", description="販売する商品 (ロール) を追加します")
    @app_commands.describe(
        role="付与するロール", name="商品名", price="価格 (内部残高)",
        duration_days="有効期間 (0で無期限)", stock="在庫 (-1で無制限)",
        purchase_limit="1人あたりの購入上限 (0で無制限)", description="説明",
        sort_order="並び順 (小さいほど先)",
    )
    @app_commands.guild_only()
    @require_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        name: str,
        price: app_commands.Range[int, config.SHOP_PRICE_MIN, config.SHOP_PRICE_MAX],
        duration_days: app_commands.Range[int, 0, config.SHOP_DURATION_MAX_DAYS] = 0,
        stock: app_commands.Range[int, -1, 1_000_000] = -1,
        purchase_limit: app_commands.Range[int, 0, 1000] = 1,
        description: str | None = None,
        sort_order: app_commands.Range[int, 0, 10_000] = 0,
    ) -> None:
        """商品を追加する。Bot がそのロールを付与できるか事前に検証する。"""
        bot: "ChargeBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        assert guild is not None
        problem = _role_assignable_problem(guild, role)
        if problem:
            await interaction.followup.send(
                embed=ui.info_embed("このロールは販売できません", problem, color=config.Color.DANGER),
                ephemeral=True,
            )
            return
        item_id = await bot.db.add_shop_item(
            guild_id=guild.id, role_id=role.id, name=name.strip()[:100], price=int(price),
            duration_days=int(duration_days), stock=int(stock),
            purchase_limit=int(purchase_limit),
            description=description.strip()[:500] if description else None,
            sort_order=int(sort_order), created_by=interaction.user.id,
        )
        op_id = await _audit(
            interaction, "SHOP_ITEM_ADD",
            detail={"item_id": item_id, "role_id": role.id, "price": int(price),
                    "duration_days": int(duration_days), "stock": int(stock)},
        )
        await bot.charge.refresh_shop_panels(guild.id)
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ 商品を追加しました",
                f"商品ID: `{item_id}`\n商品名: **{name}**\nロール: {role.mention}\n"
                f"価格: **{utils.fmt_int(int(price))}**\n"
                f"期間: {f'{duration_days}日' if duration_days else '無期限'}\n"
                f"在庫: {'無制限' if stock < 0 else stock}\n"
                f"購入上限: {'無制限' if purchase_limit == 0 else f'{purchase_limit}回'}\n"
                f"操作ID: `{op_id}`",
            ),
            ephemeral=True,
        )

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
            values["name"] = name.strip()[:100]
        if description is not None:
            values["description"] = description.strip()[:500] or None
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
        lines = []
        for item in items:
            duration = int(item["duration_days"])
            stock = int(item["stock"])
            lines.append(
                f"{'🟢' if item['active'] else '⚫'} `{item['id']}` **{item['name']}** "
                f"→ <@&{int(item['role_id'])}>\n"
                f"　{utils.fmt_int(int(item['price']))} / "
                f"{f'{duration}日' if duration else '無期限'} / "
                f"在庫{'∞' if stock < 0 else stock} / "
                f"上限{'∞' if int(item['purchase_limit']) == 0 else item['purchase_limit']}"
            )
        embed = ui.info_embed(
            "🛒 商品一覧",
            f"{ui.SEPARATOR}\nショップ: {'🟢 有効' if settings.shop_enabled else '⚫ 無効'}\n\n"
            + "\n".join(lines[:15]),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
            guild_id=guild.id, name=name.strip()[:100], inviter_reward=int(inviter_reward),
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
    account_age_days = (utils.now_ts() - int(user.created_at.timestamp())) / 86400

    # 同じ Kyash 送金者名を共有している他ユーザー (名義貸し・転売の兆候)
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
        ),
        inline=True,
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
# 登録
# ---------------------------------------------------------------------------
async def setup_commands(bot: "ChargeBot") -> None:
    """すべてのスラッシュコマンドをツリーへ登録する。"""
    tree = bot.tree
    for command in (
        setup_command, charge_panel_command, charge_panels_command,
        ranking_panel_command, ranking_panels_command, history_command,
        stats_command, queue_command, logs_command, backup_command,
        inspect_command, admin_panel_command,
    ):
        tree.add_command(command)
    for group in (
        ServerGroup(), KyashGroup(), SettingsGroup(), BalanceGroup(), UserGroup(),
        MaintenanceGroup(), EmergencyStopGroup(), AchievementGroup(), TransactionGroup(),
        DataGroup(), ConfigGroup(), SystemGroup(), RateGroup(), ShopGroup(),
        CampaignGroup(), ExportGroup(), GlobalGroup(),
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
