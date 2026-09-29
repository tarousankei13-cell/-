"""定期タスク"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config
import emoji as E
from core import saga, settings
from db.session import session_scope
from services import tasks as jobs
from services.mcd import store_sync
from ui import embeds

log = logging.getLogger("bot.cogs.tasks")


class TasksCog(commands.Cog):
    """メニュー同期・健全性チェック・バックアップなど"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_month: int | None = None
        self._last_daily_reset_day: int | None = None
        self._last_menu_force_date = None
        self._last_backup_day: int | None = None
        self._started = False

    def _start_loops(self) -> None:
        """
        ループの開始は on_ready 以降に行う。

        cog_load の時点ではまだログインしていないため、ループ内の
        wait_until_ready() が例外を投げてループが静かに死ぬことがある。
        """
        for loop in (self.menu_sync, self.store_index_sync, self.health_check,
                     self.token_warm, self.hourly_checks):
            if not loop.is_running():
                loop.start()

    async def cog_unload(self) -> None:
        self.menu_sync.cancel()
        self.store_index_sync.cancel()
        self.health_check.cancel()
        self.token_warm.cancel()
        self.hourly_checks.cancel()

    async def _wait_ready(self) -> None:
        """準備完了を待つ。まだ接続していない場合でもループを殺さない。"""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            pass

    # -- 通知 ---------------------------------------------------

    async def notify_admin(self, embed: discord.Embed) -> None:
        channel_id = settings.get("channel_admin")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("管理者通知の送信に失敗しました")

    # -- メニュー同期 -------------------------------------------

    @tasks.loop(minutes=config.MENU_SYNC_INTERVAL_MINUTES)
    async def menu_sync(self) -> None:
        """
        商品・価格・提供時間帯を取り込む。

        ETag を使うので、変更が無ければ 304 が返り通信量はゼロ。
        そのため短い間隔で回しても負荷が小さい。
        """
        if not self._started:
            return
        interval = int(
            settings.get("menu_sync_interval_minutes", config.MENU_SYNC_INTERVAL_MINUTES)
        )
        if self.menu_sync.minutes != interval:
            self.menu_sync.change_interval(minutes=interval)

        # 提供時間帯は日付ごとに定義されているため、日付が変わったら
        # ETag を無視して必ず取り直す。
        today = datetime.now(timezone.utc).date()
        force = self._last_menu_force_date != today
        if force:
            self._last_menu_force_date = today
            log.info("日付が変わったため、メニューを強制的に取り直します")

        diffs = await jobs.sync_all_menus(force=force)
        if not settings.get("menu_notify_diff", True):
            return

        from db.models import StoreCache

        blocks = []
        async with session_scope() as s:
            for store_id, diff in diffs.items():
                row = await s.get(StoreCache, store_id)
                text = jobs.format_menu_diff(store_id, row.store_name if row else "", diff)
                if text:
                    blocks.append(text)
        if blocks:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.CHART} メニューが更新されました",
                    description="\n\n".join(blocks)[:4000],
                    color=embeds.BLUE,
                )
            )

    @menu_sync.before_loop
    async def before_menu_sync(self) -> None:
        await self._wait_ready()

    # -- 店舗一覧の同期 -----------------------------------------

    @tasks.loop(minutes=config.STORE_INDEX_SYNC_MINUTES)
    async def store_index_sync(self) -> None:
        """
        店舗一覧を最新に保つ。

        新店舗の開店・閉店・店名変更・モバイルオーダー対応の切り替えを
        自動で反映する。各店舗は ETag を使って取り直すので、
        変わっていなければ 304（0バイト）で済む。
        """
        if not self._started:
            return
        interval = int(
            settings.get("store_index_sync_minutes", config.STORE_INDEX_SYNC_MINUTES)
        )
        if self.store_index_sync.minutes != interval:
            self.store_index_sync.change_interval(minutes=interval)

        try:
            report = await store_sync.sync()
        except Exception:
            log.exception("店舗一覧の同期に失敗しました")
            return

        if report.error:
            log.warning("店舗一覧の同期: %s", report.error)
        if not settings.get("store_notify_diff", True):
            return
        text = store_sync.format_report(report)
        if text:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.STORE} 店舗一覧が更新されました",
                    description=text[:4000],
                    color=embeds.BLUE,
                )
            )

    @store_index_sync.before_loop
    async def before_store_index_sync(self) -> None:
        await self._wait_ready()

    # -- アカウントの健全性 -------------------------------------

    @tasks.loop(hours=config.MCD_HEALTHCHECK_INTERVAL_HOURS)
    async def health_check(self) -> None:
        if not self._started:
            return
        results = await jobs.mcd_accounts.healthcheck_all()
        dead = [(i, l) for i, l, alive in results if not alive]
        if dead:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.RED} 応答しないアカウントがあります",
                    description="\n".join(f"`#{i}` **{l}**" for i, l in dead),
                    color=embeds.RED,
                )
            )

    @health_check.before_loop
    async def before_health_check(self) -> None:
        await self._wait_ready()

    # -- トークンの事前更新 -------------------------------------

    @tasks.loop(minutes=config.TOKEN_WARM_INTERVAL_MINUTES)
    async def token_warm(self) -> None:
        if not self._started:
            return
        warmed = await jobs.warm_tokens()
        log.debug("トークンを事前更新しました: %d件", warmed)

    @token_warm.before_loop
    async def before_token_warm(self) -> None:
        await self._wait_ready()

    # -- 毎時の点検 ---------------------------------------------

    @tasks.loop(hours=1)
    async def hourly_checks(self) -> None:
        if not self._started:
            return
        now = datetime.now(timezone.utc)

        # 元帳の整合性
        ok, message = await jobs.check_ledger()
        if not ok:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.NG} 元帳の不整合を検出しました",
                    description=message[:4000],
                    color=embeds.RED,
                )
            )

        # Kyash トークンの期限
        warnings = await jobs.kyash_token_warnings()
        if warnings and now.hour == 9:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.KEY} Kyashトークンの期限が近づいています",
                    description="\n".join(warnings) + "\n\n`/kyash relogin <ID>` で更新できます。",
                    color=embeds.YELLOW,
                )
            )

        # 日次リセット（バックアップとは別のフラグで管理する）
        if now.hour == 0 and self._last_daily_reset_day != now.day:
            self._last_daily_reset_day = now.day
            await jobs.daily_reset()

        # 月次リセット
        self._last_month = await jobs.monthly_reset_if_needed(self._last_month)

        # バックアップ
        backup_hour = int(settings.get("backup_hour", 4))
        if (
            settings.get("backup_enabled", True)
            and now.hour == backup_hour
            and self._last_backup_day != now.day
        ):
            self._last_backup_day = now.day
            await self._send_backup()

    @hourly_checks.before_loop
    async def before_hourly(self) -> None:
        await self._wait_ready()

    async def _send_backup(self) -> None:
        import io

        channel_id = settings.get("channel_admin")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            name, data = await jobs.make_backup()
        except Exception as e:
            log.warning("バックアップを作成できませんでした: %s", e)
            return
        try:
            await channel.send(
                content=(
                    f"{E.OK} 定期バックアップ（{len(data) / 1024:.0f} KB）\n"
                    f"{E.INFO} 残高・注文履歴はこのファイルだけで復元できます。\n"
                    f"　　登録済みアカウントも戻すには、サーバーの "
                    f"`data/encryption_key.txt` も必要です"
                ),
                file=discord.File(io.BytesIO(data), filename=name),
            )
            log.info("バックアップを送信しました: %s", name)
        except discord.HTTPException:
            log.exception("バックアップの送信に失敗しました")

    # -- 起動時 -------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._started:
            return
        self._started = True

        await settings.load_all()
        self._start_loops()

        # 未完了の注文とチャージを復旧する
        try:
            results = await saga.recover_pending()
            if results:
                await self.notify_admin(
                    discord.Embed(
                        title=f"{E.SYNC} 未完了の注文を復旧しました",
                        description="\n".join(
                            f"`{r.order_id[:8]}` → {r.state}" for r in results
                        )[:4000],
                        color=embeds.ORANGE,
                    )
                )
        except Exception:
            log.exception("注文の復旧に失敗しました")

        try:
            from services.kyash.charge import recover_pending as recover_charges

            count = await recover_charges()
            if count:
                await self.notify_admin(
                    discord.Embed(
                        title=f"{E.WARN} 未完了のチャージがあります",
                        description=(
                            f"{count} 件を「要確認」にしました。\n"
                            "Kyashの受取履歴と照合してください。"
                        ),
                        color=embeds.ORANGE,
                    )
                )
        except Exception:
            log.exception("チャージの復旧に失敗しました")

        # レシート画像が作れるか確認
        from services.receipt import self_check

        err = self_check()
        if err:
            log.error("レシート画像の準備に問題があります: %s", err)
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.WARN} レシート画像を生成できません",
                    description=err,
                    color=embeds.RED,
                )
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TasksCog(bot))
