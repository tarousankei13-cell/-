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
from decimal import Decimal
from typing import Any

import discord
from discord.ext import commands

import config
import kyash_service
import price_service
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
        self.price = price_service.PriceService(self.db, self)
        self.charge = ChargeService(self, self.db, self.kyash, self.price)
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
        self.add_view(ui.ShopPanelView())
        self.add_view(ui.InvitePanelView())
        self.add_view(ui.AdminPanelView())
        self.add_view(ui.ReviewCardView())
        charge_panels = await self.db.list_panels(panel_type=config.PANEL_TYPE_CHARGE)
        shop_panels = await self.db.list_panels(panel_type=config.PANEL_TYPE_SHOP)
        invite_panels = await self.db.list_panels(panel_type=config.PANEL_TYPE_INVITE)
        admin_panels = await self.db.list_panels(panel_type=config.PANEL_TYPE_ADMIN)
        ranking_panels = await self.db.list_ranking_panels()
        logger.info(
            "Persistent View を登録しました (チャージ %s / ランキング %s / ショップ %s / "
            "招待 %s / 管理 %s)",
            len(charge_panels), len(ranking_panels), len(shop_panels),
            len(invite_panels), len(admin_panels),
        )

        # 10) Kyash セッションの復元と状態確認
        status = await self.kyash.restore_from_db()
        logger.info("Kyash アカウント状態: %s", status)

        # 8-9) キュー復旧 / 停滞 Transaction の確認
        self._recovery_summary = await self.charge.recover_pending_transactions()

        # コマンド登録
        from commands import setup_commands

        await setup_commands(self)
        # コマンドはグローバルにのみ登録する。
        # グローバルとギルドの両方に同じコマンドを登録すると、Discord は
        # それぞれを別枠で表示するため「コマンドが2つずつ見える」状態になる。
        try:
            synced = await self.tree.sync()
            logger.info("スラッシュコマンドを同期しました (%s 件・グローバル)", len(synced))
        except (discord.HTTPException, discord.ClientException) as exc:
            logger.error("コマンド同期に失敗しました: %s", utils.safe_error_text(exc))

        # 11) バックグラウンドタスク開始
        self.tasks.start_all()

    #: ギルド専用コマンドの掃除を実施したバージョンを記録するキー
    GUILD_COMMAND_CLEANUP_KEY = "guild_command_cleanup_version"

    async def clear_guild_commands(self, guild: discord.abc.Snowflake) -> int:
        """そのサーバーに登録されたギルド専用コマンドを削除する。

        このBotはコマンドをグローバルにのみ登録する。過去のバージョンは
        サーバー許可時にギルドへもコピーしていたため、グローバル分と合わせて
        すべてのコマンドが二重に表示されていた。その残骸を消す。

        Returns:
            削除した件数。もともと無ければ 0 (API への書き込みもしない)。
        """
        try:
            existing = await self.tree.fetch_commands(guild=guild)
        except (discord.HTTPException, discord.ClientException) as exc:
            logger.warning("ギルドコマンドの取得に失敗しました guild=%s: %s",
                           getattr(guild, "id", "?"), utils.safe_error_text(exc))
            return 0
        if not existing:
            return 0
        self.tree.clear_commands(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except (discord.HTTPException, discord.ClientException) as exc:
            logger.warning("ギルドコマンドの削除に失敗しました guild=%s: %s",
                           getattr(guild, "id", "?"), utils.safe_error_text(exc))
            return 0
        logger.info("ギルド専用コマンド %s 件を削除しました guild=%s",
                    len(existing), getattr(guild, "id", "?"))
        return len(existing)

    async def cleanup_guild_commands(self, *, force: bool = False) -> dict[str, int]:
        """参加中の全サーバーからギルド専用コマンドを削除する。

        起動ごとに全サーバーへ問い合わせるのは無駄なので、実施したバージョンを
        記録して一度だけ走らせる。``force=True`` (``/server sync cleanup:True``)
        では記録を無視して必ず実行する。

        Returns:
            ``{"guilds": 確認数, "removed": 削除した総数, "failed": 失敗数}``
        """
        if not force:
            done = await self.db.get_system_value(self.GUILD_COMMAND_CLEANUP_KEY)
            if done == config.BOT_VERSION:
                return {"guilds": 0, "removed": 0, "failed": 0}
        removed = 0
        failed = 0
        guilds = list(self.guilds)
        for guild in guilds:
            try:
                removed += await self.clear_guild_commands(guild)
            except Exception:  # noqa: BLE001
                failed += 1
                logger.exception("ギルドコマンドの掃除に失敗しました guild=%s", guild.id)
        await self.db.set_system_value(self.GUILD_COMMAND_CLEANUP_KEY, config.BOT_VERSION)
        if removed:
            logger.warning(
                "重複していたギルド専用コマンドを %s 件削除しました "
                "(%s サーバーを確認)。コマンドはグローバル登録のみになります。",
                removed, len(guilds),
            )
        return {"guilds": len(guilds), "removed": removed, "failed": failed}

    async def on_ready(self) -> None:
        """Discord 接続完了 (再接続時にも呼ばれるため冪等に保つ)。"""
        logger.info(
            "ログインしました: %s (ID: %s) / 参加サーバー数 %s",
            self.user, self.user.id if self.user else "?", len(self.guilds),
        )
        # 旧バージョンが残した「ギルド専用コマンド」を掃除する。
        # これが残っているとグローバル分と合わせて全コマンドが二重に見える。
        try:
            cleanup = await self.cleanup_guild_commands()
            if cleanup["removed"]:
                await self.alert_owner(
                    f"**コマンドの重複を解消しました**\n"
                    f"ギルド専用に登録されていたコマンド {cleanup['removed']} 件を削除しました "
                    f"({cleanup['guilds']} サーバーを確認)。\n"
                    "以後コマンドはグローバル登録のみになります。Discord の表示が"
                    "更新されるまで少し時間がかかることがあります。"
                )
        except Exception:  # noqa: BLE001
            logger.exception("ギルドコマンドの掃除に失敗しました")
        for guild in self.guilds:
            allowed = await self.db.is_guild_allowed(guild.id)
            logger.info("  - %s (%s) 許可=%s", guild.name, guild.id, allowed)
            if allowed:
                # 招待キャンペーンの帰属判定に使う使用回数をキャッシュする
                if await self.charge.sync_invite_cache(guild):
                    logger.info("    招待キャッシュを同期しました")
                elif await self.db.get_active_campaign(guild.id) is not None:
                    logger.warning(
                        "    招待一覧を取得できないため招待者を特定できません "
                        "(Bot に「サーバー管理」権限が必要です)"
                    )
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

    async def on_member_join(self, member: discord.Member) -> None:
        """参加イベント: 招待の帰属判定と不正チェックを行う。"""
        try:
            await self.charge.handle_member_join(member)
        except Exception:  # noqa: BLE001 - 参加処理で Bot を落とさない
            logger.exception("参加処理に失敗しました guild=%s user=%s",
                             member.guild.id, member.id)

    async def on_member_remove(self, member: discord.Member) -> None:
        """退出イベント: 再入場の検知のため記録する。"""
        try:
            await self.charge.handle_member_leave(member.guild.id, member.id)
        except Exception:  # noqa: BLE001
            logger.exception("退出処理に失敗しました guild=%s user=%s",
                             member.guild.id, member.id)

    async def on_invite_create(self, invite: discord.Invite) -> None:
        """招待が作成されたらキャッシュへ反映する (取りこぼし防止)。"""
        if invite.guild is None:
            return
        cache = self.charge._invite_cache.setdefault(invite.guild.id, {})
        cache[invite.code] = int(invite.uses or 0)

    async def on_invite_delete(self, invite: discord.Invite) -> None:
        """招待が削除されたらキャッシュから除く。"""
        if invite.guild is None:
            return
        self.charge._invite_cache.get(invite.guild.id, {}).pop(invite.code, None)

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
        # ロール別チャージ率・ショップ商品からも取り除く
        removed_rate = await self.db.remove_role_rate(role.guild.id, role.id)
        items = [
            item for item in await self.db.list_shop_items(role.guild.id, active_only=False)
            if int(item["role_id"]) == role.id
        ]
        for item in items:
            await self.db.update_shop_item(int(item["id"]), role.guild.id, active=0)
        if removed_rate or items:
            await self.charge.log_event(
                role.guild.id, "⚠️ ロールが削除されました",
                f"`{role.name}` の削除に伴い、ロール別チャージ率 {removed_rate} 件と"
                f"ショップ商品 {len(items)} 件を無効化しました。",
                color=config.Color.WARNING,
            )
            await self.charge.refresh_shop_panels(role.guild.id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """実績 / ログチャンネルが削除された場合は設定を無効化する。"""
        settings = await self.db.get_settings(channel.guild.id)
        updates: dict[str, Any] = {}
        if settings.achievement_channel_id == channel.id:
            updates["achievement_channel_id"] = None
        if settings.log_channel_id == channel.id:
            updates["log_channel_id"] = None
        if settings.balance_log_channel_id == channel.id:
            updates["balance_log_channel_id"] = None
        if settings.summary_channel_id == channel.id:
            updates["summary_channel_id"] = None
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

    async def send_backup_to_owner(self, path: Any) -> bool:
        """バックアップファイルを Bot Owner の DM へ送信する。

        残高台帳と暗号化済みの Kyash トークンを含むため、送信先は Owner の DM のみ。
        """
        try:
            owner = self.get_user(BOT_OWNER_ID) or await self.fetch_user(BOT_OWNER_ID)
            await owner.send(
                embed=ui.log_embed(
                    "💾 DB バックアップ",
                    f"`{path.name}`\n"
                    "残高台帳を含みます。取り扱いに注意してください。\n"
                    "※ Kyash のアクセストークンは暗号化されており、`data/secret.key` が"
                    "ないと復号できません。リストアには鍵も必要です。",
                    color=config.Color.INFO,
                ),
                file=discord.File(str(path), filename=path.name),
            )
            logger.info("バックアップを Owner へ送信しました: %s", path.name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("バックアップの送信に失敗しました: %s", utils.safe_error_text(exc))
            return False

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
        """パネル操作の共通チェック。問題があれば応答して None を返す。

        戻り値が None でない場合、``interaction.guild`` は必ず存在する
        (呼び出し側は ``_require_guild`` で取り出す)。
        """
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

    @staticmethod
    def _member_of(interaction: discord.Interaction) -> discord.Member | None:
        """Interaction の実行者を Member として取得する。

        サーバー内の Interaction では ``interaction.user`` は Member だが、
        キャッシュ状況によっては User のことがあるため、その場合は
        サーバーから解決する。
        """
        if isinstance(interaction.user, discord.Member):
            return interaction.user
        if interaction.guild is None:
            return None
        return interaction.guild.get_member(interaction.user.id)

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        """ガード済みの Interaction からサーバーを取り出す。

        ``_guard_user_action`` / ``_guard_admin_action`` を通過した後に使う。
        """
        guild = interaction.guild
        if guild is None:  # pragma: no cover - ガード済みのため到達しない
            raise RuntimeError("サーバー外の操作です")
        return guild

    async def on_charge_button(self, interaction: discord.Interaction) -> None:
        """💰 チャージ → 方式選択 または 金額入力 Modal。

        使える方式が Kyash だけのときは選択画面を挟まず、従来どおり
        そのまま金額入力へ進む (余計な操作を増やさない)。
        """
        settings = await self._guard_user_action(interaction)
        if settings is None:
            return
        guild = self._require_guild(interaction)
        providers = await self.charge.usable_providers(guild.id, settings)
        only_kyash = (
            len(providers) == 1
            and providers[0]["provider"] == config.ChargeProvider.KYASH
        )
        if not only_kyash:
            entries = await self.charge.provider_availability(guild.id, settings)
            await interaction.response.send_message(
                embed=ui.provider_select_embed(entries),
                view=ui.ProviderSelectView(entries),
                ephemeral=True,
            )
            return
        await self._begin_kyash_charge(interaction, settings)

    async def on_provider_selected(
        self, interaction: discord.Interaction, provider: str
    ) -> None:
        """方式選択メニューの確定 (ステップ 1/4 → 2/4)。"""
        settings = await self._guard_user_action(interaction)
        if settings is None:
            return
        guild = self._require_guild(interaction)
        available = {
            p["provider"] for p in await self.charge.usable_providers(guild.id, settings)
        }
        if provider not in available:
            entries = await self.charge.provider_availability(guild.id, settings)
            blocked = next((e for e in entries if e["provider"] == provider), None)
            code = (
                str(blocked["error_code"]) if blocked and blocked["error_code"]
                else config.ErrorCode.PROVIDER_DISABLED
            )
            await ui.safe_respond(interaction, embed=ui.error_embed(code))
            return
        if provider == config.ChargeProvider.KYASH:
            await self._begin_kyash_charge(interaction, settings)
            return
        if provider == config.ChargeProvider.KYASH_CLAIM:
            active = await self.db.get_active_transaction(guild.id, interaction.user.id)
            if active is not None and str(active["status"]) == config.TxStatus.WAITING_PAYMENT:
                await self._show_claim_link(interaction, active, resumed=True)
                return
            await interaction.response.send_modal(
                ui.ManualAmountModal(provider, settings, await self.charge.provider_limits(
                    guild.id, provider, settings
                ))
            )
            return
        await interaction.response.send_modal(
            ui.ManualAmountModal(provider, settings, await self.charge.provider_limits(
                guild.id, provider, settings
            ))
        )

    async def _begin_kyash_charge(
        self, interaction: discord.Interaction, settings: Any
    ) -> None:
        """Kyash (自動) のチャージを開始する (進行中の取引があれば再開)。"""
        try:
            await self.charge.preflight(
                self._require_guild(interaction).id, interaction.user.id, settings
            )
        except ChargeError as exc:
            await ui.safe_respond(interaction, embed=ui.error_embed(exc.code))
            return
        guild = self._require_guild(interaction)
        active = await self.db.get_active_transaction(guild.id, interaction.user.id)
        if active is not None:
            # 請求リンクの支払い待ちなら、そのリンクを出し直す
            if str(active["status"]) == config.TxStatus.WAITING_PAYMENT:
                await self._show_claim_link(interaction, active, resumed=True)
                return
            if active["status"] == config.TxStatus.WAITING_LINK and (
                not active["expires_at"] or active["expires_at"] > utils.now_ts()
            ):
                remaining = max(30, int(active["expires_at"] or 0) - utils.now_ts())
                amount = int(active["requested_amount"])
                rate = utils.to_decimal(active["charge_rate"]) or settings.charge_rate
                await interaction.response.send_message(
                    embed=ui.link_wait_embed(
                        tx_id=str(active["id"]),
                        amount=amount,
                        rate=rate,
                        credited=utils.calc_credited_amount(amount, rate),
                        expires_at=int(active["expires_at"] or 0) or None,
                        resumed=True,
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
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tx_id, amount, settings = await self.charge.start_charge(
                guild.id, interaction.user.id, raw_amount
            )
        except ChargeError as exc:
            detail = None
            if exc.code in (
                config.ErrorCode.AMOUNT_BELOW_MIN, config.ErrorCode.AMOUNT_ABOVE_MAX
            ):
                guild_settings = await self.db.get_settings(guild.id)
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

        embed = ui.link_wait_embed(
            tx_id=tx_id,
            amount=amount,
            rate=settings.charge_rate,
            credited=utils.calc_credited_amount(amount, settings.charge_rate),
            expires_at=utils.now_ts() + config.LINK_WAIT_SECONDS,
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
            embed=ui.link_accepted_embed(
                tx_id=str(result["tx_id"]),
                amount=int(result["amount"]),
                waiting=max(0, waiting - 1),
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

    # ------------------------------------------------------------------
    # PayPay / LTC (申請 → 管理者承認)
    # ------------------------------------------------------------------
    async def handle_manual_amount_submit(
        self, interaction: discord.Interaction, provider: str, raw_amount: str
    ) -> None:
        """金額確定 → 入金先の案内 (ステップ 2/4 → 3/4)。"""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if provider == config.ChargeProvider.KYASH_CLAIM:
            await self._start_claim_charge(interaction, guild.id, raw_amount)
            return
        try:
            quote = await self.charge.start_manual_charge(
                guild.id, interaction.user.id, provider, raw_amount
            )
        except ChargeError as exc:
            embed = ui.error_embed(exc.code)
            if exc.code in (
                config.ErrorCode.AMOUNT_BELOW_MIN, config.ErrorCode.AMOUNT_ABOVE_MAX
            ):
                settings = await self.db.get_settings(guild.id)
                low, high = await self.charge.provider_limits(guild.id, provider, settings)
                embed.add_field(
                    name="ご案内",
                    value=f"受付範囲: {utils.fmt_yen(low)} 〜 {utils.fmt_yen(high)}",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            logger.exception("チャージ申請の作成で予期しない例外が発生しました provider=%s",
                             provider)
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.deposit_embed(quote),
            view=ui.DepositView(
                int(quote["request_id"]), provider,
                owner_id=interaction.user.id,
                timeout=float(config.QUOTE_WAIT_SECONDS),
            ),
            ephemeral=True,
        )

    async def _start_claim_charge(
        self, interaction: discord.Interaction, guild_id: int, raw_amount: str
    ) -> None:
        """請求リンクを発行して案内する (ステップ 2/2)。"""
        try:
            quote = await self.charge.start_claim_charge(
                guild_id, interaction.user.id, raw_amount
            )
        except ChargeError as exc:
            embed = ui.error_embed(exc.code)
            if exc.code in (
                config.ErrorCode.AMOUNT_BELOW_MIN, config.ErrorCode.AMOUNT_ABOVE_MAX
            ):
                settings = await self.db.get_settings(guild_id)
                low, high = await self.charge.provider_limits(
                    guild_id, config.ChargeProvider.KYASH_CLAIM, settings
                )
                embed.add_field(
                    name="ご案内",
                    value=f"受付範囲: {utils.fmt_yen(low)} 〜 {utils.fmt_yen(high)}",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            logger.exception("請求リンクの発行で予期しない例外が発生しました")
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.claim_link_embed(quote),
            view=ui.ClaimPaymentView(
                str(quote["tx_id"]), owner_id=interaction.user.id,
                timeout=float(config.CLAIM_WAIT_SECONDS),
            ),
            ephemeral=True,
        )

    async def _show_claim_link(
        self, interaction: discord.Interaction, row: Any, *, resumed: bool
    ) -> None:
        """発行済みの請求リンクを再表示する (中断からの再開)。"""
        link_id = str(row["claim_link_id"] or "")
        if not link_id:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.ACTIVE_TRANSACTION_EXISTS)
            )
            return
        amount = int(row["requested_amount"])
        rate = utils.to_decimal(row["charge_rate"]) or Decimal(config.DEFAULT_CHARGE_RATE)
        remaining = max(30, int(row["expires_at"] or 0) - utils.now_ts())
        quote = {
            "tx_id": str(row["id"]),
            "amount": amount,
            "charge_rate": rate,
            "role_id": None,
            "credited": utils.calc_credited_amount(amount, rate),
            "url": utils.kyash_link_url(link_id),
            "expires_at": row["expires_at"],
        }
        await interaction.response.send_message(
            embed=ui.claim_link_embed(quote, resumed=resumed),
            view=ui.ClaimPaymentView(
                str(row["id"]), owner_id=interaction.user.id, timeout=float(remaining)
            ),
            ephemeral=True,
        )

    async def on_claim_check(self, interaction: discord.Interaction, tx_id: str) -> None:
        """🔄 支払いを確認。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.charge.check_claim_payment(
                tx_id, user_id=interaction.user.id
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=None), ephemeral=True
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("請求リンクの確認で予期しない例外が発生しました tx=%s", tx_id)
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return
        row = await self.db.get_transaction(tx_id)
        if result in ("CREDITED", "DONE") and row is not None:
            await interaction.followup.send(
                embed=ui.success_embed(
                    "🟢 支払いを確認しました",
                    f"付与: **{utils.fmt_int(int(row['credited_amount'] or 0))}**\n"
                    f"残高: **{utils.fmt_int(int(row['balance_after'] or 0))}**\n"
                    f"取引ID: `{tx_id}`",
                ),
                ephemeral=True,
            )
            return
        if result == "REVIEW":
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.MANUAL_REVIEW), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.claim_pending_embed(
                tx_id=tx_id,
                amount=int(row["requested_amount"]) if row else 0,
            ),
            ephemeral=True,
        )

    async def on_claim_cancel(self, interaction: discord.Interaction, tx_id: str) -> None:
        """請求リンクの取り消し。"""
        try:
            await self.charge.cancel_claim_transaction(tx_id, interaction.user.id)
        except ChargeError as exc:
            await ui.safe_respond(interaction, embed=ui.error_embed(exc.code))
            return
        await ui.safe_respond(
            interaction,
            embed=ui.info_embed(
                "キャンセルしました",
                "請求リンクを無効にしました。まだ支払っていない場合は支払わないでください。\n"
                "すでに支払ってしまった場合はサーバーの管理者へお問い合わせください。",
                color=config.Color.NEUTRAL,
            ),
        )

    async def handle_request_submit(
        self,
        interaction: discord.Interaction,
        request_id: int,
        raw_proof: str,
        raw_asset: str | None,
    ) -> None:
        """証拠の提出 → 承認待ちへ (ステップ 3/4 → 4/4)。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.charge.submit_request(
                request_id, interaction.user.id, raw_proof, raw_asset
            )
        except ChargeError as exc:
            logger.info("申請の受付に失敗しました request=%s code=%s", request_id, exc.code)
            await interaction.followup.send(
                embed=ui.error_embed(exc.code), ephemeral=True
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("申請の受付で予期しない例外が発生しました request=%s", request_id)
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.request_submitted_embed(result), ephemeral=True
        )

    async def on_request_cancel(
        self, interaction: discord.Interaction, request_id: int
    ) -> None:
        """利用者が入金前の申請をやめる。"""
        try:
            await self.charge.cancel_own_request(request_id, interaction.user.id)
        except ChargeError as exc:
            await ui.safe_respond(interaction, embed=ui.error_embed(exc.code))
            return
        await ui.safe_respond(
            interaction,
            embed=ui.info_embed(
                "キャンセルしました",
                "申請を取り消しました。まだ送金していない場合は、送金しないでください。\n"
                "送金してしまった場合はサーバーの管理者へお問い合わせください。",
                color=config.Color.NEUTRAL,
            ),
        )

    # --- 審査カードのボタン ------------------------------------------
    async def _review_target(
        self, interaction: discord.Interaction
    ) -> sqlite3.Row | None:
        """審査カードのボタンから申請を引き、権限を確認する。"""
        if interaction.message is None:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.REQUEST_NOT_FOUND)
            )
            return None
        row = await self.db.get_request_by_message(interaction.message.id)
        if row is None:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.REQUEST_NOT_FOUND)
            )
            return None
        if not await self.charge.can_review(int(row["guild_id"]), interaction.user):
            logger.info("権限のない承認操作を拒否しました user=%s request=%s",
                        interaction.user.id, int(row["id"]))
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed(
                    "操作できません",
                    "この申請を処理できるのは Bot Owner "
                    "(または Owner が承認を委任したサーバーの管理者) だけです。",
                    color=config.Color.DANGER,
                ),
            )
            return None
        if row["status"] != config.RequestStatus.PENDING:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.REQUEST_ALREADY_HANDLED)
            )
            await self.charge.update_review_card(int(row["id"]))
            return None
        return row

    async def on_review_approve(self, interaction: discord.Interaction) -> None:
        """🟢 承認 (表示どおりの額を付与)。"""
        row = await self._review_target(interaction)
        if row is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._run_approve(interaction, int(row["id"]), None, None)

    async def on_review_edit_approve(self, interaction: discord.Interaction) -> None:
        """✏️ 金額を直して承認。"""
        row = await self._review_target(interaction)
        if row is None:
            return
        await interaction.response.send_modal(
            ui.ApproveAmountModal(int(row["id"]), int(row["estimated_credit"]))
        )

    async def handle_review_edit_approve(
        self, interaction: discord.Interaction, request_id: int, raw_amount: str, note: str
    ) -> None:
        amount = utils.parse_user_amount(raw_amount)
        if amount is None or amount <= 0:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.INVALID_AMOUNT)
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._run_approve(interaction, request_id, amount, note)

    async def _run_approve(
        self,
        interaction: discord.Interaction,
        request_id: int,
        credited: int | None,
        note: str | None,
    ) -> None:
        try:
            result = await self.charge.approve_request(
                request_id, operator_id=interaction.user.id,
                credited_amount=credited, note=note,
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("申請の承認で予期しない例外が発生しました request=%s", request_id)
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.success_embed(
                "🟢 承認しました",
                f"申請ID: `#{request_id}`\n"
                f"付与: **{utils.fmt_int(int(result['credited_amount']))}**\n"
                f"取引ID: `{result['transaction_id']}`\n"
                f"操作ID: `{result['operation_id']}`\n"
                f"利用者の残高: {utils.fmt_int(int(result['balance_before']))} → "
                f"**{utils.fmt_int(int(result['balance_after']))}**",
            ),
            ephemeral=True,
        )

    async def on_review_reject(self, interaction: discord.Interaction) -> None:
        """🔴 却下 (理由を入力)。"""
        row = await self._review_target(interaction)
        if row is None:
            return
        await interaction.response.send_modal(ui.RejectReasonModal(int(row["id"])))

    async def handle_review_reject(
        self, interaction: discord.Interaction, request_id: int, reason: str
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.charge.reject_request(
                request_id, operator_id=interaction.user.id, reason=reason
            )
        except ChargeError as exc:
            await interaction.followup.send(
                embed=ui.error_embed(exc.code, admin_detail=exc.detail), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.info_embed(
                "🔴 却下しました",
                f"申請ID: `#{request_id}`\n"
                f"操作ID: `{result['operation_id']}`\n"
                "残高は変更していません。利用者へ理由を DM で通知しました。",
                color=config.Color.DANGER,
            ),
            ephemeral=True,
        )

    async def on_review_detail(self, interaction: discord.Interaction) -> None:
        """🔍 詳細 (照合に必要な情報をコピーしやすい形で出す)。"""
        if interaction.message is None:
            return
        row = await self.db.get_request_by_message(interaction.message.id)
        if row is None:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.REQUEST_NOT_FOUND)
            )
            return
        if not await self.charge.can_review(int(row["guild_id"]), interaction.user):
            await ui.safe_respond(
                interaction,
                embed=ui.info_embed("操作できません", "この申請を参照する権限がありません。",
                                    color=config.Color.DANGER),
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        history, _ = await self.db.list_requests(
            guild_id=int(row["guild_id"]), user_id=int(row["user_id"]), limit=5
        )
        balance = await self.db.get_balance(int(row["guild_id"]), int(row["user_id"]))
        await interaction.followup.send(
            embed=ui.review_detail_embed(request=row, history=history, balance=balance),
            ephemeral=True,
        )

    async def on_balance_button(self, interaction: discord.Interaction) -> None:
        """💳 残高 (Ephemeral)。"""
        if await self._guard_user_action(interaction) is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = self._require_guild(interaction)
        balance = await self.db.get_balance(guild.id, interaction.user.id)
        summary = await self.db.get_user_charge_summary(guild.id, interaction.user.id)
        rank, _rank_balance, rank_total = await self.db.get_user_rank(
            guild.id, interaction.user.id
        )
        spent = await self.db.get_user_spend_total(guild.id, interaction.user.id)
        settings = await self.db.get_settings(guild.id)
        shop_available = bool(
            settings.shop_enabled and await self.db.list_shop_items(guild.id)
        )
        await interaction.followup.send(
            embed=ui.balance_embed(
                interaction.user,
                balance,
                summary,
                rank=rank,
                rank_total=rank_total,
                shop_available=shop_available,
                spent=spent,
            ),
            ephemeral=True,
        )

    async def on_history_button(self, interaction: discord.Interaction) -> None:
        """📜 履歴 (Ephemeral / ページング)。"""
        if await self._guard_user_action(interaction) is None:
            return
        view = ui.HistoryView(
            owner_id=interaction.user.id, guild_id=self._require_guild(interaction).id
        )
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
        open_requests, _ = await self.db.list_requests(
            guild_id=view.guild_id,
            user_id=interaction.user.id,
            statuses=[config.RequestStatus.QUOTED, config.RequestStatus.PENDING],
            limit=5,
        )
        embed = ui.history_embed(
            rows, page=view.page, total_pages=total_pages, total=total,
            open_requests=open_requests if view.page == 1 else (),
        )
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
        guild = self._require_guild(interaction)
        # DB を読むため、3秒の応答期限に間に合うよう先に defer する
        await interaction.response.defer(ephemeral=True, thinking=True)
        shop_available = bool(
            settings.shop_enabled and await self.db.list_shop_items(guild.id)
        )
        campaign = await self.db.get_active_campaign(guild.id)
        await interaction.followup.send(
            embed=ui.help_embed(
                settings,
                shop_available=shop_available,
                campaign_name=str(campaign["name"]) if campaign else None,
                providers=await self.charge.provider_availability(guild.id, settings),
            ),
            ephemeral=True,
        )

    async def on_panel_refresh_button(self, interaction: discord.Interaction) -> None:
        """🔄 チャージパネルの表示を最新化する。"""
        settings = await self._guard_user_action(interaction)
        if settings is None:
            return
        guild = self._require_guild(interaction)
        embed = ui.charge_panel_embed(
            settings,
            kyash_ready=self.kyash.is_usable,
            providers=await self.charge.provider_availability(guild.id, settings),
        )
        try:
            await interaction.response.edit_message(embed=embed, view=ui.ChargePanelView())
        except discord.HTTPException:
            await ui.safe_respond(
                interaction, embed=ui.info_embed("更新できませんでした", "もう一度お試しください。")
            )

    async def on_ranking_refresh_button(self, interaction: discord.Interaction) -> None:
        """🔄 ランキングを DB から再取得して更新する。"""
        guild = interaction.guild
        if guild is None:
            return
        try:
            self.charge.check_button_rate_limit(interaction.user.id)
            settings = await self.charge.ensure_usable_guild(guild.id)
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
        updated = await self.charge.refresh_ranking_panels(guild.id, force=True)
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
            await self.charge.ensure_usable_guild(interaction.guild.id)  # noqa: F841
        except ChargeError as exc:
            await ui.safe_respond(interaction, embed=ui.error_embed(exc.code))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = self._require_guild(interaction)
        rank, balance, total = await self.db.get_user_rank(guild.id, interaction.user.id)
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
    # ショップ操作ハンドラ
    # ------------------------------------------------------------------
    async def on_shop_open_button(self, interaction: discord.Interaction) -> None:
        """🛒 ショップを開く → 商品選択 (Ephemeral)。"""
        settings = await self._guard_user_action(interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not settings.shop_enabled:
            await interaction.followup.send(
                embed=ui.info_embed("ショップは停止中です", "現在は購入できません。"),
                ephemeral=True,
            )
            return
        guild = self._require_guild(interaction)
        items = await self.db.list_shop_items(guild.id)
        balance = await self.db.get_balance(guild.id, interaction.user.id)
        embed = ui.shop_panel_embed(settings, items)
        embed.add_field(name="あなたの残高", value=f"**{utils.fmt_int(balance)}**", inline=False)
        await interaction.followup.send(
            embed=embed,
            view=ui.ShopSelectView(items, owner_id=interaction.user.id),
            ephemeral=True,
        )

    async def on_shop_select(self, interaction: discord.Interaction, item_id: int) -> None:
        """商品を選択 → 確認 → 購入。"""
        member = self._member_of(interaction)
        if interaction.guild is None or member is None:
            return
        item = await self.db.get_shop_item(item_id, interaction.guild.id)
        if item is None or not item["active"]:
            await ui.safe_respond(
                interaction, embed=ui.error_embed(config.ErrorCode.SHOP_ITEM_UNAVAILABLE)
            )
            return
        balance = await self.db.get_balance(interaction.guild.id, member.id)
        owned = await self.db.count_user_purchases(interaction.guild.id, member.id, item_id)
        role = interaction.guild.get_role(int(item["role_id"]))
        embed, blocker = ui.purchase_confirm_embed(
            item=item,
            balance=balance,
            owned=owned,
            already_has_role=bool(role is not None and role in member.roles),
        )
        # 購入できない理由があるときは確認ボタンを出さず、理由だけを示す
        if blocker is not None:
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        view = ui.ConfirmView(
            owner_id=interaction.user.id, confirm_label="購入する", danger=False, timeout=90
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            return
        try:
            result = await self.charge.purchase_shop_item(member, item_id)
        except ChargeError as exc:
            logger.info("購入を拒否しました user=%s code=%s", interaction.user.id, exc.code)
            await interaction.followup.send(
                embed=ui.error_embed(exc.code), ephemeral=True
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("購入処理で予期しない例外が発生しました item=%s", item_id)
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.UNKNOWN_ERROR), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.purchase_success_embed(
                item_name=str(result["item_name"]), role_id=int(result["role_id"]),
                price=int(result["price"]), balance_after=int(result["balance_after"]),
                expires_at=result["expires_at"], purchase_id=int(result["purchase_id"]),
            ),
            ephemeral=True,
        )
        await self.charge.refresh_shop_panels(interaction.guild.id)

    async def on_shop_myitems_button(self, interaction: discord.Interaction) -> None:
        """📦 購入履歴 (Ephemeral)。"""
        if await self._guard_user_action(interaction) is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self.db.list_user_purchases(
            self._require_guild(interaction).id, interaction.user.id
        )
        await interaction.followup.send(embed=ui.my_items_embed(rows), ephemeral=True)

    # ------------------------------------------------------------------
    # 招待キャンペーン操作ハンドラ
    # ------------------------------------------------------------------
    async def on_invite_get_button(self, interaction: discord.Interaction) -> None:
        """🔗 個人専用の招待リンクを取得 (Ephemeral)。"""
        if await self._guard_user_action(interaction) is None:
            return
        member = self._member_of(interaction)
        if member is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.charge.issue_invite_code(member)
        except ChargeError as exc:
            await interaction.followup.send(embed=ui.error_embed(exc.code), ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            logger.exception("招待リンクの発行で例外が発生しました user=%s", interaction.user.id)
            await interaction.followup.send(
                embed=ui.error_embed(config.ErrorCode.INVITE_NOT_AVAILABLE), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=ui.invite_link_embed(
                url=result["url"], code=result["code"],
                summary=result["summary"], created=result["created"],
            ),
            ephemeral=True,
        )

    async def on_invite_status_button(self, interaction: discord.Interaction) -> None:
        """📊 自分の招待状況 (Ephemeral)。"""
        if await self._guard_user_action(interaction) is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = self._require_guild(interaction).id
        summary = await self.db.get_invite_summary(guild_id, interaction.user.id)
        rank, _, total = await self.db.get_invite_rank(guild_id, interaction.user.id)
        records, _ = await self.db.list_invite_records(
            guild_id, inviter_id=interaction.user.id, limit=10
        )
        await interaction.followup.send(
            embed=ui.invite_status_embed(
                summary=summary, rank=rank, total=total, records=records
            ),
            ephemeral=True,
        )

    async def on_invite_rank_button(self, interaction: discord.Interaction) -> None:
        """🏆 招待ランキング (Ephemeral)。"""
        settings = await self._guard_user_action(interaction)
        if settings is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = self._require_guild(interaction)
        entries = await self.charge.build_ranking_entries(
            guild.id, settings, config.RankingType.INVITE
        )
        await interaction.followup.send(
            embed=ui.ranking_embed(
                guild, entries, settings, updated_at=utils.now_ts(),
                ranking_type=config.RankingType.INVITE,
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # 管理ダッシュボード操作ハンドラ (押下時に毎回権限を確認)
    # ------------------------------------------------------------------
    async def _guard_admin_action(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        if not await self.is_server_admin(interaction):
            await ui.safe_respond(interaction, embed=ui.error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    async def on_admin_refresh_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard_admin_action(interaction):
            return
        embed = await self.charge.build_admin_panel_embed(self._require_guild(interaction).id)
        try:
            await interaction.response.edit_message(embed=embed, view=ui.AdminPanelView())
        except discord.HTTPException:
            await ui.safe_respond(interaction, embed=embed)

    async def on_admin_maintenance_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard_admin_action(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = self._require_guild(interaction).id
        settings = await self.db.get_settings(guild_id)
        new_value = not settings.maintenance
        await self.db.update_settings(guild_id, maintenance=1 if new_value else 0)
        await self.db.add_audit_log(
            actor_id=interaction.user.id,
            action="MAINTENANCE_ON" if new_value else "MAINTENANCE_OFF",
            guild_id=guild_id, detail={"source": "admin_panel"},
        )
        if not new_value:
            self.charge.queue_wakeup.set()
        await self.charge.refresh_charge_panels(guild_id)
        await self.charge.refresh_admin_panels(guild_id)
        await interaction.followup.send(
            embed=ui.success_embed(
                f"{'🟠' if new_value else '🟢'} メンテナンスを{'開始' if new_value else '解除'}しました",
                "チャージパネルの表示も更新しました。",
            ),
            ephemeral=True,
        )

    async def on_admin_queue_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard_admin_action(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        items = await self.db.list_queue(limit=15)
        now = utils.now_ts()
        lines = [
            f"{config.STATUS_EMOJI.get(str(i['tx_status']), '⚪')} `{i['transaction_id']}` "
            f"<@{i['user_id']}> {utils.fmt_yen(int(i['requested_amount']))} "
            f"/ 待機 {utils.format_duration(now - int(i['enqueued_at']))} / 再試行 {i['attempts']}"
            for i in items
        ]
        await interaction.followup.send(
            embed=ui.info_embed(
                "🗃 受取キュー",
                f"{ui.SEPARATOR}\n" + ("\n".join(lines) if lines else "キューは空です。"),
            ),
            ephemeral=True,
        )

    async def on_admin_review_button(self, interaction: discord.Interaction) -> None:
        if not await self._guard_admin_action(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = self._require_guild(interaction).id
        rows, total = await self.db.search_transactions(
            guild_id=guild_id, status=config.TxStatus.MANUAL_REVIEW, limit=10
        )
        lines = [
            f"`{r['id']}` <@{r['user_id']}> "
            f"{utils.fmt_yen(int(r['received_amount'] or r['requested_amount']))} "
            f"/ `{r['error_code']}` / {utils.format_jst(int(r['updated_at']))}"
            for r in rows
        ]
        invite_holds, hold_total = await self.db.list_invite_records(
            guild_id, status=config.InviteStatus.HOLD, limit=5
        )
        embed = ui.info_embed(
            f"🟠 要確認 ({total} 件)",
            f"{ui.SEPARATOR}\n" + ("\n".join(lines) if lines else "手動確認が必要な取引はありません。"),
            color=config.Color.WARNING if total else config.Color.SUCCESS,
        )
        if invite_holds:
            embed.add_field(
                name=f"招待の確認待ち ({hold_total} 件)",
                value="\n".join(
                    f"`{r['id']}` <@{r['invited_id']}> ← <@{r['inviter_id']}> "
                    f"({config.INVITE_REASON_LABELS.get(str(r['reason']), str(r['reason'] or '-'))})"
                    for r in invite_holds
                ),
                inline=False,
            )
        embed.add_field(
            name="操作",
            value="`/transaction verify` → `/transaction resolve` / `/campaign review`",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
            await self.price.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("価格取得サービス停止で例外が発生しました")
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
