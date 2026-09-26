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
    panels = await bot.db.list_panels(guild.id)
    ranking_panels = await bot.db.list_ranking_panels(guild.id)
    kyash_ok = bot.kyash.status == config.KyashAccountStatus.ACTIVE

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
@app_commands.describe(channel="設置先チャンネル (省略時は現在のチャンネル)")
@app_commands.guild_only()
@require_admin()
async def ranking_panel_command(
    interaction: discord.Interaction, channel: discord.TextChannel | None = None
) -> None:
    """ランキングパネルを新規設置する (複数設置可能・既存は削除しない)。"""
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
    if settings.ranking_enabled:
        entries = await bot.charge.build_ranking_entries(guild.id, settings)
        embed = ui.ranking_embed(guild, entries, settings, updated_at=utils.now_ts())
        signature = bot.charge.ranking_signature(entries)
    else:
        embed = ui.ranking_disabled_embed()
        signature = "DISABLED"
    message = await target.send(embed=embed, view=ui.RankingPanelView())
    panel_id = await bot.db.add_ranking_panel(guild.id, target.id, message.id)
    await bot.db.update_ranking_panel_state(message.id, signature=signature)
    op_id = await _audit(
        interaction, "RANKING_PANEL_CREATE",
        detail={"panel_id": panel_id, "channel_id": target.id, "message_id": message.id},
    )
    await interaction.followup.send(
        embed=ui.success_embed(
            "✅ ランキングパネルを設置しました",
            f"チャンネル: {target.mention}\nメッセージID: `{message.id}`\n"
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
        f"{'🟢' if p['active'] else '⚫'} <#{p['channel_id']}> / `{p['message_id']}` / "
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


@app_commands.command(name="system", description="システム状態を表示します (管理者)")
@app_commands.guild_only()
@require_admin()
async def system_command(interaction: discord.Interaction) -> None:
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
    embed.add_field(
        name="バックグラウンドタスク",
        value="\n".join(f"{'🟢' if ok else '🔴'} {name}" for name, ok in task_status.items()),
        inline=False,
    )
    embed.set_footer(text="秘密情報は表示されません")
    await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="config", description="現在のサーバー設定を表示します (管理者)")
@app_commands.guild_only()
@require_admin()
async def config_command(interaction: discord.Interaction) -> None:
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
# 登録
# ---------------------------------------------------------------------------
async def setup_commands(bot: "ChargeBot") -> None:
    """すべてのスラッシュコマンドをツリーへ登録する。"""
    tree = bot.tree
    for command in (
        setup_command, charge_panel_command, charge_panels_command,
        ranking_panel_command, ranking_panels_command, history_command,
        stats_command, queue_command, system_command, config_command,
        logs_command, backup_command,
    ):
        tree.add_command(command)
    for group in (
        ServerGroup(), KyashGroup(), SettingsGroup(), BalanceGroup(), UserGroup(),
        MaintenanceGroup(), EmergencyStopGroup(), AchievementGroup(), TransactionGroup(),
        DataGroup(),
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
