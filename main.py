#!/usr/bin/env python3
"""高性能 Kyash 自動チャージ Discord Bot / エントリポイント。

Discord サーバーごとに独立した内部残高を管理するチャージ Bot。
利用者は常設チャージパネルから金額を入力し、Kyash の送金リンクを提出する。
Bot は管理者が登録した受取用 Kyash アカウントでリンクを検証・受取し、
サーバーごとのチャージ率を適用して内部残高を付与する。

内部残高は Discord サーバー内の数値であり、現金化・出金には対応しない。

起動方法:
    python3 main.py
"""
from __future__ import annotations

# =============================================================================
# ここだけ直接書き換えて使用する (それ以外の設定は Discord コマンドから変更可能)
# =============================================================================
#: Discord Bot Token (.env へは逃がさず、このファイルへ直接記述する)
DISCORD_BOT_TOKEN = "PUT_YOUR_DISCORD_BOT_TOKEN_HERE"

#: Bot Owner の Discord ユーザーID (Bot 全体の管理権限を持つ)
BOT_OWNER_ID = 1324938326741876758

#: 最初に利用を許可するサーバーID (0 の場合は /server allow で追加する)
BOOTSTRAP_GUILD_ID = 0
# =============================================================================

import asyncio
import logging
import signal
import sqlite3
import sys
from typing import Any

import discord
from discord.ext import commands

import config
import kyash_service
import ui
import utils
from charge_service import ChargeError, ChargeService
from database import Database
from tasks import BackgroundTasks

logger = logging.getLogger(config.LOGGER_BOT)

#: Token 未設定の判定に使うプレースホルダ
_TOKEN_PLACEHOLDER = "PUT_YOUR_DISCORD_BOT_TOKEN_HERE"


# ---------------------------------------------------------------------------
# ロギング
# ---------------------------------------------------------------------------
class SanitizingFormatter(logging.Formatter):
    """出力直前に秘密情報をマスキングする Formatter。

    各呼び出し側でもマスキングしているが、想定外の例外の traceback などに
    完全な送金リンクや認証情報が混入しないよう、最終防壁として全出力を通す。
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return utils.sanitize_for_log(text, limit=20_000)


def setup_logging(level: int = logging.INFO) -> None:
    """カテゴリ別に追跡しやすいログ設定を行う。

    秘密情報 (Token / パスワード / OTP / Cookie / 完全な送金リンク等) は
    ``SanitizingFormatter`` と ``utils.sanitize_for_log`` の二重でマスクする。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SanitizingFormatter(
            fmt="%(asctime)s [%(levelname)-8s] %(name)-14s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in (
        config.LOGGER_BOT, config.LOGGER_DB, config.LOGGER_CHARGE, config.LOGGER_KYASH,
        config.LOGGER_QUEUE, config.LOGGER_DISCORD_EVENTS, config.LOGGER_AUDIT,
        config.LOGGER_TASKS,
    ):
        logging.getLogger(name).setLevel(level)
    # discord.py 自体のログは警告以上のみ
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 起動時 Validation
# ---------------------------------------------------------------------------
def validate_environment() -> list[str]:
    """起動前チェック。異常があれば原因の一覧を返す。"""
    errors: list[str] = []

    if sys.version_info < (3, 11):
        errors.append(
            f"Python 3.11 以上が必要です (現在: {sys.version_info.major}.{sys.version_info.minor})"
        )
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN == _TOKEN_PLACEHOLDER:
        errors.append("DISCORD_BOT_TOKEN が未設定です (main.py の先頭を書き換えてください)")
    if not isinstance(BOT_OWNER_ID, int) or BOT_OWNER_ID <= 0:
        errors.append("BOT_OWNER_ID が未設定です (main.py の先頭を書き換えてください)")
    if not isinstance(BOOTSTRAP_GUILD_ID, int) or BOOTSTRAP_GUILD_ID < 0:
        errors.append("BOOTSTRAP_GUILD_ID が不正です")

    try:
        major = int(discord.__version__.split(".")[0])
        if major < 2:
            errors.append(f"discord.py 2.x が必要です (現在: {discord.__version__})")
    except (ValueError, IndexError):
        errors.append(f"discord.py のバージョンを判定できません: {discord.__version__}")

    for module_name, package in (("requests", "requests"), ("bs4", "beautifulsoup4")):
        try:
            __import__(module_name)
        except ImportError:
            errors.append(f"必須パッケージがありません: {package}")

    try:
        import importlib

        importlib.import_module("Kyasher")  # vendor/ 配下の添付モジュール
    except ImportError as exc:
        errors.append(f"Kyash モジュール (vendor/Kyasher) を import できません: {exc}")

    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.DATA_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        errors.append(f"データディレクトリへ書き込めません ({config.DATA_DIR}): {exc}")

    try:
        connection = sqlite3.connect(str(config.DB_PATH))
        connection.execute("SELECT 1")
        connection.close()
    except sqlite3.Error as exc:
        errors.append(f"SQLite データベースを開けません ({config.DB_PATH}): {exc}")

    return errors


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class ChargeBot(commands.Bot):
    """チャージ Bot 本体。"""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        # ランキングでの Bot 除外・表示名解決に必要 (Developer Portal で
        # SERVER MEMBERS INTENT を有効にする)。message_content は使用しない。
        intents.members = True
        intents.message_content = False
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            owner_id=BOT_OWNER_ID,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=False, replied_user=False
            ),
        )
        self.version = config.BOT_VERSION
        self.started_at = utils.now_ts()
        self.cipher = utils.TokenCipher(config.SECRET_KEY_PATH)
        self.db = Database(config.DB_PATH)
        self.kyash = kyash_service.KyashService(self.db, self.cipher)
        self.charge = ChargeService(self, self.db, self.kyash)
        self.tasks = BackgroundTasks(self, self.db, self.charge, self.kyash)
        self._shutdown_started = False
        self._recovery_summary: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 起動シーケンス
    # ------------------------------------------------------------------
    async def setup_hook(self) -> None:
        """Discord 接続前の初期化 (DB → 復旧 → View 再登録 → タスク開始)。"""
        logger.info("=" * 70)
        logger.info("Kyash チャージ Bot v%s / schema v%s を起動します",
                    config.BOT_VERSION, config.SCHEMA_VERSION)
        logger.info("Python %s / discord.py %s / Kyasher %s",
                    sys.version.split()[0], discord.__version__,
                    kyash_service.KYASH_MODULE_VERSION)
        logger.info("=" * 70)

        # 1-3) DB 初期化 / テーブル確認 / マイグレーション
        await self.db.connect()

        if not self.cipher.available:
            logger.warning(
                "アクセストークンの暗号化が利用できません (%s)。"
                "Kyash セッションは再起動後に再ログインが必要になります。",
                self.cipher.init_error,
            )

        # 4) 許可サーバーの読込 (Bootstrap Guild を自動登録)
        if BOOTSTRAP_GUILD_ID:
            if not await self.db.is_guild_allowed(BOOTSTRAP_GUILD_ID):
                await self.db.set_guild_permission(
                    BOOTSTRAP_GUILD_ID, "ALLOWED", BOT_OWNER_ID, note="bootstrap"
                )
                logger.info("Bootstrap サーバーを許可しました: %s", BOOTSTRAP_GUILD_ID)
        allowed = await self.db.count_allowed_guilds()
        logger.info("許可済みサーバー数: %s", allowed)

        # 5-7) Persistent View の再登録 (チャージパネル / ランキングパネル)
        self.add_view(ui.ChargePanelView())
        self.add_view(ui.RankingPanelView())
        charge_panels = await self.db.list_panels()
        ranking_panels = await self.db.list_ranking_panels()
        logger.info(
            "Persistent View を登録しました (チャージパネル %s 件 / ランキングパネル %s 件)",
            len(charge_panels), len(ranking_panels),
        )

        # 10) Kyash セッションの復元と状態確認
        status = await self.kyash.restore_from_db()
        logger.info("Kyash アカウント状態: %s", status)

        # 8-9) キュー復旧 / 停滞 Transaction の確認
        self._recovery_summary = await self.charge.recover_pending_transactions()

        # コマンド登録
        from commands import setup_commands

        await setup_commands(self)
        try:
            synced = await self.tree.sync()
            logger.info("スラッシュコマンドを同期しました (%s 件)", len(synced))
        except discord.HTTPException as exc:
            logger.error("コマンド同期に失敗しました: %s", utils.safe_error_text(exc))

        # 11) バックグラウンドタスク開始
        self.tasks.start_all()

    async def on_ready(self) -> None:
        """Discord 接続完了 (再接続時にも呼ばれるため冪等に保つ)。"""
        logger.info(
            "ログインしました: %s (ID: %s) / 参加サーバー数 %s",
            self.user, self.user.id if self.user else "?", len(self.guilds),
        )
        for guild in self.guilds:
            allowed = await self.db.is_guild_allowed(guild.id)
            logger.info("  - %s (%s) 許可=%s", guild.name, guild.id, allowed)
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name="チャージパネル"
                ),
                status=discord.Status.online,
            )
        except discord.HTTPException:
            pass
        if self._recovery_summary:
            summary = self._recovery_summary
            if any(summary.values()):
                await self.alert_owner(
                    "♻️ 起動時の復旧処理が完了しました\n"
                    f"・キュー再投入: {summary.get('requeued', 0)} 件\n"
                    f"・残高付与の再実行: {summary.get('credited', 0)} 件\n"
                    f"・手動確認へ移行: {summary.get('manual_review', 0)} 件\n"
                    f"・期限切れ: {summary.get('expired', 0)} 件"
                )
            self._recovery_summary = {}

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """導入されただけでは利用できない (Bot Owner の許可が必要)。"""
        allowed = await self.db.is_guild_allowed(guild.id)
        logger.info("サーバーへ参加しました: %s (%s) 許可=%s", guild.name, guild.id, allowed)
        if not allowed:
            await self.alert_owner(
                f"🆕 未許可のサーバーへ参加しました: **{guild.name}** (`{guild.id}`)\n"
                f"利用を許可する場合は `/server allow guild_id:{guild.id}` を実行してください。"
            )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """退出してもデータは削除しない (残高・履歴を保持)。"""
        logger.info("サーバーから退出しました: %s (%s) / データは保持します", guild.name, guild.id)
        await self.alert_owner(
            f"👋 サーバーから退出しました: **{guild.name}** (`{guild.id}`)\n"
            "残高・履歴は保持されています。削除する場合は `/data delete` を使用してください。"
        )

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """管理者ロールが削除された場合は設定を解除して警告する。"""
        settings = await self.db.get_settings(role.guild.id)
        if settings.admin_role_id == role.id:
            await self.db.update_settings(role.guild.id, admin_role_id=None)
            logger.warning("管理者ロールが削除されました guild=%s role=%s", role.guild.id, role.id)
            await self.alert_owner(
                f"⚠️ サーバー **{role.guild.name}** (`{role.guild.id}`) の管理者ロール "
                f"`{role.name}` が削除されたため設定を解除しました。\n"
                "`/settings admin_role` で新しいロールを設定してください。"
            )
            await self.charge.log_event(
                role.guild.id, "⚠️ 管理者ロールが削除されました",
                "管理者ロール設定を解除しました。`/settings admin_role` で再設定してください。",
                color=config.Color.DANGER,
            )

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """実績 / ログチャンネルが削除された場合は設定を無効化する。"""
        settings = await self.db.get_settings(channel.guild.id)
        updates: dict[str, Any] = {}
        if settings.achievement_channel_id == channel.id:
            updates["achievement_channel_id"] = None
        if settings.log_channel_id == channel.id:
            updates["log_channel_id"] = None
        if updates:
            await self.db.update_settings(channel.guild.id, **updates)
            logger.warning("設定チャンネルが削除されました guild=%s channel=%s",
                           channel.guild.id, channel.id)
            await self.alert_owner(
                f"⚠️ サーバー **{channel.guild.name}** のチャンネル `{channel.name}` が削除され、"
                f"設定 ({', '.join(updates)}) を解除しました。"
            )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """パネルメッセージが削除された場合は DB 上で無効化する。

        パネルは古いメッセージでキャッシュに載らないため raw イベントで処理する。
        削除イベントごとの書き込みを避けるため、まず読み取りで判定する。
        """
        if payload.guild_id is None:
            return
        await self._handle_deleted_message(payload.guild_id, payload.message_id)

    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        """一括削除 (purge) でパネルが消えた場合にも対応する。"""
        if payload.guild_id is None:
            return
        for message_id in payload.message_ids:
            await self._handle_deleted_message(payload.guild_id, message_id)

    async def _handle_deleted_message(self, guild_id: int, message_id: int) -> None:
        """削除されたメッセージがパネルなら無効化し、管理者へ知らせる。"""
        try:
            kind = await self.db.find_panel_by_message(message_id)
        except Exception:  # noqa: BLE001 - 削除イベントで Bot 全体を壊さない
            logger.exception("パネル判定に失敗しました message=%s", message_id)
            return
        if kind is None:
            return
        if kind == "CHARGE":
            await self.db.deactivate_panel(message_id=message_id)
            label, command = "チャージパネル", "/charge_panel"
        else:
            await self.db.deactivate_ranking_panel(message_id=message_id)
            label, command = "ランキングパネル", "/ranking_panel"
        logger.info("%sが削除されたため無効化しました message=%s", label, message_id)
        await self.charge.log_event(
            guild_id, f"⚠️ {label}が削除されました",
            f"メッセージ `{message_id}` を無効化しました。`{command}` で再設置できます。",
            color=config.Color.WARNING,
        )

    # ------------------------------------------------------------------
    # 権限
    # ------------------------------------------------------------------
    def is_bot_owner(self, user: discord.abc.User) -> bool:
        return user.id == BOT_OWNER_ID

    async def is_server_admin(self, interaction: discord.Interaction) -> bool:
        """サーバー管理者かどうかを毎回確認する。

        Discord の Administrator 権限だけに依存せず、設定された管理者ロールを
        主たる判定材料とする。管理者ロールが未設定の場合のみ、初期設定を
        可能にするため ``manage_guild`` 権限を持つメンバーを許可する。
        """
        if self.is_bot_owner(interaction.user):
            return True
        if interaction.guild is None:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        settings = await self.db.get_settings(interaction.guild.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            member = interaction.guild.get_member(interaction.user.id)
        if settings.admin_role_id:
            if member is None:
                return False
            return any(role.id == settings.admin_role_id for role in member.roles)
        if member is None:
            return False
        return bool(member.guild_permissions.manage_guild)

    async def alert_owner(self, message: str) -> None:
        """Bot Owner へ DM で通知する (秘密情報は含めない)。"""
        try:
            owner = self.get_user(BOT_OWNER_ID) or await self.fetch_user(BOT_OWNER_ID)
            await owner.send(
                embed=ui.log_embed("🔔 Bot 通知", message, color=config.Color.WARNING)
            )
        except (discord.HTTPException, discord.NotFound, discord.Forbidden) as exc:
            logger.warning("Owner への通知に失敗しました: %s", utils.safe_error_text(exc))

    # ------------------------------------------------------------------
    # パネル操作ハンドラ (Persistent View から呼ばれる)
    # ------------------------------------------------------------------
    async def _guard_user_action(
        self, interaction: discord.Interaction, *, need_charge: bool = False
    ) -> Any:
        """パネル操作の共通チェック。問題があれば応答して None を返す。"""
        if interaction.guild is None:
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED))
            return None
        try:
            self.charge.check_button_rate_limit(interaction.user.id)
            settings = await self.charge.ensure_usable_guild(interaction.guild.id)
            if need_charge:
                await self.charge.preflight(interaction.guild.id, interaction.user.id, settings)
            return settings
        except ChargeError as exc:
            logger.info("パネル操作を拒否しました user=%s code=%s", interaction.user.id, exc.code)
            await ui.safe_respond(interaction, embed=ui.error_embed(exc.code))
            return None

    async def on_charge_button(self, interaction: discord.Interaction) -> None:
        """💰 チャージ → 金額入力 Modal (進行中の取引があれば再開)。"""
        settings = await self._guard_user_action(interaction, need_charge=True)
        if settings is None:
            return
        active = await self.db.get_active_transaction(interaction.guild.id, interaction.user.id)
        if active is not None:
            if active["status"] == config.TxStatus.WAITING_LINK and (
                not active["expires_at"] or active["expires_at"] > utils.now_ts()
            ):
                remaining = max(30, int(active["expires_at"] or 0) - utils.now_ts())
                await interaction.response.send_message(
                    embed=ui.info_embed(
                        "⌛ 送金リンクの送信をお待ちしています",
                        f"申請額: **{utils.fmt_yen(int(active['requested_amount']))}**\n"
                        f"取引ID: `{active['id']}`\n\n"
                        "Kyash で同じ金額の**送金リンク**を作成し、下のボタンから送信してください。",
                        color=config.Color.WARNING,
                    ),
                    view=ui.LinkSubmitView(
                        str(active["id"]), owner_id=interaction.user.id, timeout=remaining
                    ),
                    ephemeral=True,
                )
                return
            await ui.safe_respond(
                interaction,
                embed=ui.error_embed(
                    config.ErrorCode.ACTIVE_TRANSACTION_EXISTS,
                ),
            )
            return
        await interaction.response.send_modal(ui.AmountModal(settings))

    async def handle_amount_submit(self, interaction: discord.Interaction, raw_amount: str) -> None:
        """金額入力の確定 → Transaction 作成 → リンク送信を案内。"""
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tx_id, amount, settings = await self.charge.start_charge(
                interaction.guild.id, interaction.user.id, raw_amount
            )
        except ChargeError as exc:
            detail = None
            if exc.code in (
                config.ErrorCode.AMOUNT_BELOW_MIN, config.ErrorCode.AMOUNT_ABOVE_MAX
            ):
                guild_settings = await self.db.get_settings(interaction.guild.id)
                detail = (
                    f"受付範囲: {utils.fmt_yen(guild_settings.minimum_charge)} 〜 "
                    f"{utils.fmt_yen(guild_settings.maximum_charge)}"
                )
            embed = ui.error_embed(exc.code)
            if detail:
                embed.add_field(name="ご案内", value=detail, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            logger.exception("チャージ開始で予期しない例外が発生しました")
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return

        embed = ui.info_embed(
            "🔗 送金リンクを送信してください",
            f"{ui.SEPARATOR}\n"
            f"申請額: **{utils.fmt_yen(amount)}**\n"
            f"チャージ率: **{utils.fmt_rate(settings.charge_rate)}**\n"
            f"獲得予定: **{utils.fmt_int(utils.calc_credited_amount(amount, settings.charge_rate))}**\n"
            f"取引ID: `{tx_id}`\n{ui.SEPARATOR}\n"
            f"Kyash アプリで **{utils.fmt_yen(amount)}** の送金リンクを作成し、"
            "下のボタンから送信してください。\n"
            f"※ 有効期限は約 {config.LINK_WAIT_SECONDS // 60} 分です。\n"
            "※ 送金リンクは公開チャンネルへ貼らないでください。",
            color=config.Color.ACCENT,
        )
        await interaction.followup.send(
            embed=embed,
            view=ui.LinkSubmitView(
                tx_id, owner_id=interaction.user.id, timeout=float(config.LINK_WAIT_SECONDS)
            ),
            ephemeral=True,
        )

    async def handle_link_submit(
        self, interaction: discord.Interaction, tx_id: str, raw_link: str
    ) -> None:
        """送金リンクの検証 → 受取キューへ登録。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.charge.submit_link(tx_id, raw_link, interaction.user.id)
        except ChargeError as exc:
            logger.info("リンク検証に失敗しました tx=%s code=%s detail=%s",
                        tx_id, exc.code, utils.sanitize_for_log(exc.detail or "", limit=200))
            embed = ui.error_embed(exc.code)
            if exc.code == config.ErrorCode.AMOUNT_MISMATCH:
                embed.add_field(
                    name="ご案内",
                    value="申請した金額と同じ金額の送金リンクを作成し、最初からやり直してください。",
                    inline=False,
                )
            elif exc.code in (
                config.ErrorCode.KYASH_NETWORK_ERROR, config.ErrorCode.KYASH_TIMEOUT,
                config.ErrorCode.KYASH_AUTH_ERROR,
            ):
                embed.add_field(
                    name="ご案内",
                    value="この取引はまだ有効です。少し待ってから同じリンクを再送信できます。",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            logger.exception("リンク検証で予期しない例外が発生しました tx=%s", tx_id)
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return
        finally:
            raw_link = ""  # メモリ上の参照を破棄

        queue_counts = await self.db.count_queue()
        waiting = queue_counts.get(config.TxStatus.QUEUED, 0)
        await interaction.followup.send(
            embed=ui.info_embed(
                "✅ 送金リンクを受け付けました",
                f"{ui.SEPARATOR}\n"
                f"金額: **{utils.fmt_yen(result['amount'])}**\n"
                f"取引ID: `{result['tx_id']}`\n"
                f"順番待ち: **{max(0, waiting - 1)} 件**\n{ui.SEPARATOR}\n"
                "自動で受け取り処理を行います。完了後に DM でお知らせします。",
                color=config.Color.SUCCESS,
            ),
            ephemeral=True,
        )

    async def handle_charge_cancel(self, interaction: discord.Interaction, tx_id: str) -> None:
        """利用者によるチャージキャンセル。"""
        try:
            await self.charge.cancel_transaction(tx_id, interaction.user.id)
        except ChargeError as exc:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(exc.code, admin_detail=None)
            )
            return
        await ui.safe_respond(
            interaction,
            embed=ui.info_embed(
                "キャンセルしました",
                "チャージをキャンセルしました。送金リンクを作成済みの場合は Kyash アプリから"
                "キャンセルしてください。",
                color=config.Color.NEUTRAL,
            ),
        )

    async def on_balance_button(self, interaction: discord.Interaction) -> None:
        """💳 残高 (Ephemeral)。"""
        if await self._guard_user_action(interaction) is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        balance = await self.db.get_balance(interaction.guild.id, interaction.user.id)
        summary = await self.db.get_user_charge_summary(interaction.guild.id, interaction.user.id)
        await interaction.followup.send(
            embed=ui.balance_embed(interaction.user, balance, summary), ephemeral=True
        )

    async def on_history_button(self, interaction: discord.Interaction) -> None:
        """📜 履歴 (Ephemeral / ページング)。"""
        if await self._guard_user_action(interaction) is None:
            return
        view = ui.HistoryView(owner_id=interaction.user.id, guild_id=interaction.guild.id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.render_history(interaction, view, edit=False)

    async def render_history(
        self, interaction: discord.Interaction, view: "ui.HistoryView", *, edit: bool
    ) -> None:
        """履歴ページを描画する。"""
        offset = (view.page - 1) * view.PAGE_SIZE
        rows, total = await self.db.list_user_transactions(
            view.guild_id, interaction.user.id, offset=offset, limit=view.PAGE_SIZE
        )
        total_pages = max(1, -(-total // view.PAGE_SIZE))
        if view.page > total_pages:
            view.page = total_pages
            offset = (view.page - 1) * view.PAGE_SIZE
            rows, total = await self.db.list_user_transactions(
                view.guild_id, interaction.user.id, offset=offset, limit=view.PAGE_SIZE
            )
        embed = ui.history_embed(rows, page=view.page, total_pages=total_pages, total=total)
        view.previous.disabled = view.page <= 1  # type: ignore[attr-defined]
        view.next.disabled = view.page >= total_pages  # type: ignore[attr-defined]
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def on_help_button(self, interaction: discord.Interaction) -> None:
        """❓ ヘルプ (Ephemeral)。"""
        settings = await self._guard_user_action(interaction)
        if settings is None:
            return
        await interaction.response.send_message(
            embed=ui.help_embed(settings), ephemeral=True
        )

    async def on_panel_refresh_button(self, interaction: discord.Interaction) -> None:
        """🔄 チャージパネルの表示を最新化する。"""
        settings = await self._guard_user_action(interaction)
        if settings is None:
            return
        embed = ui.charge_panel_embed(settings, kyash_ready=self.kyash.is_usable)
        try:
            await interaction.response.edit_message(embed=embed, view=ui.ChargePanelView())
        except discord.HTTPException:
            await ui.safe_respond(
                interaction, embed=ui.info_embed("更新できませんでした", "もう一度お試しください。")
            )

    async def on_ranking_refresh_button(self, interaction: discord.Interaction) -> None:
        """🔄 ランキングを DB から再取得して更新する。"""
        if interaction.guild is None:
            return
        try:
            self.charge.check_button_rate_limit(interaction.user.id)
            settings = await self.charge.ensure_usable_guild(interaction.guild.id)
        except ChargeError as exc:
            await ui.safe_respond(interaction, embed=ui.error_embed(exc.code))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not settings.ranking_enabled:
            await interaction.followup.send(
                embed=ui.info_embed("ランキングは無効です", "このサーバーではランキングを無効にしています。"),
                ephemeral=True,
            )
            return
        updated = await self.charge.refresh_ranking_panels(interaction.guild.id, force=True)
        await interaction.followup.send(
            embed=ui.info_embed(
                "🔄 ランキングを更新しました",
                f"最新の残高を反映しました (更新パネル数: {updated})。",
                color=config.Color.SUCCESS,
            ),
            ephemeral=True,
        )

    async def on_my_rank_button(self, interaction: discord.Interaction) -> None:
        """📜 自分の順位 (Ephemeral)。"""
        if interaction.guild is None:
            return
        try:
            self.charge.check_button_rate_limit(interaction.user.id)
            await self.charge.ensure_usable_guild(interaction.guild.id)
        except ChargeError as exc:
            await ui.safe_respond(interaction, embed=ui.error_embed(exc.code))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rank, balance, total = await self.db.get_user_rank(
            interaction.guild.id, interaction.user.id
        )
        if rank is None:
            description = (
                f"現在残高: **{utils.fmt_int(balance)}**\n"
                "残高が 0 のため、またはランキング対象外のため順位はありません。"
            )
        else:
            description = (
                f"順位: **{rank}位** / {utils.fmt_int(total)}人\n"
                f"現在残高: **{utils.fmt_int(balance)}**"
            )
        await interaction.followup.send(
            embed=ui.info_embed("📜 あなたの順位", description, color=config.Color.RANKING),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Kyash ログインハンドラ (Bot Owner 専用)
    # ------------------------------------------------------------------
    async def handle_kyash_login(
        self, interaction: discord.Interaction, email: str, password: str
    ) -> None:
        """受取用 Kyash アカウントのログイン (Ephemeral)。"""
        if not self.is_bot_owner(interaction.user):
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            needs_otp = await self.kyash.begin_login(interaction.user.id, email, password)
        except kyash_service.KyashServiceError as exc:
            logger.error("Kyash ログインに失敗しました: %s", utils.sanitize_for_log(exc, limit=200))
            await interaction.followup.send(
                embed=ui.error_embed(
                    exc.error_code, admin_detail=utils.sanitize_for_log(exc, limit=400)
                ),
                ephemeral=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Kyash ログインで予期しない例外が発生しました")
            await interaction.followup.send(
                embed=ui.error_embed(
                    config.ErrorCode.UNKNOWN_ERROR, admin_detail=utils.safe_error_text(exc)
                ),
                ephemeral=True,
            )
            return
        finally:
            password = ""  # 参照を破棄 (保存もログ出力もしない)

        if needs_otp:
            await interaction.followup.send(
                embed=ui.info_embed(
                    "📱 SMS認証コードを入力してください",
                    "Kyash から届いた認証コードを、下のボタンから入力してください。\n"
                    "※ 認証コードはログにも DB にも保存されません。",
                    color=config.Color.WARNING,
                ),
                view=ui.KyashLoginStartView(owner_id=interaction.user.id),
                ephemeral=True,
            )
            return
        await self.db.add_audit_log(
            actor_id=interaction.user.id, action="KYASH_LOGIN",
            detail={"method": "uuid_reuse", "username": self.kyash.username},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ ログインしました",
                f"受取用アカウント: `{self.kyash.username or '不明'}`\n"
                "登録済み端末情報を利用したため SMS 認証は不要でした。",
            ),
            ephemeral=True,
        )
        await self._after_kyash_login()

    async def handle_kyash_otp(self, interaction: discord.Interaction, otp: str) -> None:
        """OTP 検証によるログイン完了 (Ephemeral)。"""
        if not self.is_bot_owner(interaction.user):
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.kyash.complete_login(interaction.user.id, otp)
        except kyash_service.KyashServiceError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(
                    exc.error_code, admin_detail=utils.sanitize_for_log(exc, limit=400)
                ),
                ephemeral=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("OTP 検証で予期しない例外が発生しました")
            await interaction.followup.send(
                embed=ui.error_embed(
                    config.ErrorCode.UNKNOWN_ERROR, admin_detail=utils.safe_error_text(exc)
                ),
                ephemeral=True,
            )
            return
        finally:
            otp = ""  # 参照を破棄

        await self.db.add_audit_log(
            actor_id=interaction.user.id, action="KYASH_LOGIN",
            detail={"method": "otp", "username": self.kyash.username},
        )
        await interaction.followup.send(
            embed=ui.success_embed(
                "✅ ログインしました",
                f"受取用アカウント: `{self.kyash.username or '不明'}`\n"
                "チャージの受付が可能になりました。",
            ),
            ephemeral=True,
        )
        await self._after_kyash_login()

    async def _after_kyash_login(self) -> None:
        """ログイン後にパネル表示を更新し、キューを再開する。"""
        for guild in self.guilds:
            if await self.db.is_guild_allowed(guild.id):
                await self.charge.refresh_charge_panels(guild.id)
        self.charge.queue_wakeup.set()

    # ------------------------------------------------------------------
    # 終了処理
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Graceful shutdown。

        新規チャージ受付停止 → タスク停止 → セッション破棄 → DB commit/close
        → Discord 切断 の順に実行する。処理中 Transaction は DB 上の状態を保つ。
        """
        if self._shutdown_started:
            await super().close()
            return
        self._shutdown_started = True
        logger.info("終了処理を開始します")
        self.charge.accepting_new = False
        try:
            await self.tasks.stop_all()
        except Exception:  # noqa: BLE001
            logger.exception("タスク停止で例外が発生しました")
        try:
            await self.charge.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("チャージサービス停止で例外が発生しました")
        try:
            await self.kyash.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Kyash セッション停止で例外が発生しました")
        try:
            await self.db.close()
        except Exception:  # noqa: BLE001
            logger.exception("DB クローズで例外が発生しました")
        await super().close()
        logger.info("終了処理が完了しました")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
async def _run() -> int:
    bot = ChargeBot()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown(signal_name: str) -> None:
        logger.warning("%s を受信しました。終了処理を行います", signal_name)
        bot.charge.accepting_new = False
        stop_event.set()

    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        try:
            loop.add_signal_handler(sig, _request_shutdown, name)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            logger.warning("%s のシグナルハンドラを登録できませんでした", name)

    start_task = asyncio.create_task(bot.start(DISCORD_BOT_TOKEN), name="bot-start")
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-wait")
    done, _pending = await asyncio.wait(
        {start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    exit_code = 0
    if start_task in done:
        stop_task.cancel()
        try:
            start_task.result()
        except discord.LoginFailure:
            logger.critical("Discord Token が正しくありません。main.py の設定を確認してください。")
            exit_code = 1
        except discord.PrivilegedIntentsRequired:
            logger.critical(
                "SERVER MEMBERS INTENT が無効です。Discord Developer Portal → Bot → "
                "Privileged Gateway Intents で有効にしてください。"
            )
            exit_code = 1
        except Exception:  # noqa: BLE001
            logger.exception("Bot が異常終了しました")
            exit_code = 1
    else:
        start_task.cancel()
    if not bot.is_closed():
        await bot.close()
    return exit_code


def main() -> None:
    setup_logging()
    errors = validate_environment()
    if errors:
        logger.critical("起動前チェックで問題が見つかりました:")
        for error in errors:
            logger.critical("  ✗ %s", error)
        logger.critical("上記を解決してから再度起動してください。")
        sys.exit(1)
    if not BOOTSTRAP_GUILD_ID:
        logger.warning(
            "BOOTSTRAP_GUILD_ID が 0 です。起動後に `/server allow` でサーバーを許可してください。"
        )
    try:
        exit_code = asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        logger.warning("キーボード割り込みで終了します")
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
