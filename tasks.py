"""バックグラウンドタスク。

* 受取キューワーカー (受取用アカウントへの操作を直列化する唯一のワーカー)
* 期限切れ / 停滞 Transaction の検知
* 通知の再送
* Kyash セッションの健康確認
* ランキングパネルの定期更新
* DB バックアップ
* 整合性チェック

すべて二重起動しないよう起動状態を確認してから開始する。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from discord.ext import tasks

import config
import utils

if TYPE_CHECKING:
    from charge_service import ChargeService
    from database import Database
    from kyash_service import AccountSlot, KyashService
    from main import ChargeBot

logger = logging.getLogger(config.LOGGER_TASKS)


def _copy_file(source: Path, destination: Path) -> None:
    """バックアップのコピー (専用スレッドで実行する)。"""
    import shutil

    shutil.copy2(source, destination)


class BackgroundTasks:
    """Bot のバックグラウンド処理をまとめて管理する。"""

    def __init__(
        self, bot: "ChargeBot", db: "Database", charge: "ChargeService", kyash: "KyashService"
    ) -> None:
        self.bot = bot
        self.db = db
        self.charge = charge
        self.kyash = kyash
        self._closing = False
        self._queue_task: asyncio.Task[None] | None = None
        # アカウントごとの通知状態 (同じ異常を何度も通知しないため)
        self._kyash_alerts: dict[int, str] = {}
        self._token_alert_days: dict[int, str] = {}
        self._wallet_alerted: set[int] = set()
        self._all_limit_alerted: bool = False
        self._last_heartbeat_error: str | None = None

    # ------------------------------------------------------------------
    # 起動 / 停止
    # ------------------------------------------------------------------
    def start_all(self) -> None:
        """すべてのタスクを開始する (既に起動済みなら何もしない)。"""
        if self._queue_task is None or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._queue_worker(), name="queue-worker")
            logger.info("受取キューワーカーを開始しました")
        for loop in self._loops():
            if not loop.is_running():
                loop.start()
        logger.info("バックグラウンドタスクを開始しました")

    async def stop_all(self) -> None:
        """タスクを安全に停止する。"""
        self._closing = True
        for loop in self._loops():
            if loop.is_running():
                loop.cancel()
        if self._queue_task is not None and not self._queue_task.done():
            self.charge.queue_wakeup.set()
            self._queue_task.cancel()
            try:
                await self._queue_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        logger.info("バックグラウンドタスクを停止しました")

    def _loops(self) -> tuple[tasks.Loop, ...]:
        return (
            self.expire_checker,
            self.stuck_checker,
            self.notification_retry,
            self.kyash_health,
            self.ranking_refresher,
            self.backup_task,
            self.integrity_task,
            self.shop_expiry,
            self.subscription_task,
            self.auction_task,
            self.goal_task,
            self.fraud_task,
            self.panel_cache_task,
            self.request_task,
            self.claim_task,
            self.tier_task,
            self.ranking_reward_task,
            self.heartbeat,
            self.summary_task,
            self.campaign_task,
        )

    def ensure_running(self) -> list[str]:
        """停止してしまったタスクを再開する (長期運用時の自己復旧)。

        タスク本体は try/except で保護しているが、``before_loop`` の失敗や
        想定外の BaseException で停止した場合に備え、定期タスクから相互に監視する。

        Returns:
            再開したタスク名の一覧。
        """
        if self._closing:
            return []
        revived: list[str] = []
        if self._queue_task is None or self._queue_task.done():
            self._queue_task = asyncio.create_task(self._queue_worker(), name="queue-worker")
            revived.append("queue_worker")
        for loop in self._loops():
            if not loop.is_running():
                try:
                    loop.start()
                    revived.append(loop.coro.__name__)
                except RuntimeError:  # 既に起動していた場合
                    pass
        if revived:
            logger.warning("停止していたタスクを再開しました: %s", ", ".join(revived))
        return revived

    def status(self) -> dict[str, bool]:
        """各タスクの稼働状況 (``/system`` 表示用)。"""
        state = {
            "queue_worker": bool(self._queue_task and not self._queue_task.done()),
        }
        for loop in self._loops():
            state[loop.coro.__name__] = loop.is_running()
        return state

    # ------------------------------------------------------------------
    # 受取キューワーカー
    # ------------------------------------------------------------------
    async def _wait_ready(self) -> None:
        """Discord の準備完了を待つ (失敗してもタスクを停止させない)。

        ``before_loop`` で例外が出るとそのタスクは停止してしまうため、
        ここで必ず吸収する。停止した場合は ``ensure_running`` が再開する。
        """
        try:
            await self.bot.wait_until_ready()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Discord の準備完了を待機できませんでした: %s",
                           utils.safe_error_text(exc))

    async def _queue_worker(self) -> None:
        """キューを1件ずつ処理する。並列受取は行わない。"""
        await self._wait_ready()
        logger.info("キューワーカーの待機を開始します")
        while not self._closing:
            try:
                processed = await self.charge.process_queue_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ワーカーは落とさない
                logger.exception("キューワーカーで予期しない例外が発生しました")
                processed = False
                await asyncio.sleep(5)
            if processed:
                continue  # 連続処理 (滞留を早く解消する)
            self.charge.queue_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self.charge.queue_wakeup.wait(), timeout=config.QUEUE_IDLE_SLEEP
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # 定期タスク
    # ------------------------------------------------------------------
    @tasks.loop(seconds=config.TASK_EXPIRE_INTERVAL)
    async def expire_checker(self) -> None:
        """送金リンク入力待ちの期限切れを処理する (併せて他タスクの稼働を監視)。"""
        try:
            self.ensure_running()
        except Exception:  # noqa: BLE001
            logger.exception("タスク監視に失敗しました")
        try:
            await self.charge.expire_transactions()
        except Exception:  # noqa: BLE001
            logger.exception("期限切れ処理に失敗しました")

    @expire_checker.before_loop
    async def _before_expire(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_STUCK_INTERVAL)
    async def stuck_checker(self) -> None:
        """停滞 Transaction を検知して安全に処理する (併せて他タスクの稼働を監視)。"""
        try:
            self.ensure_running()
        except Exception:  # noqa: BLE001
            logger.exception("タスク監視に失敗しました")
        try:
            handled = await self.charge.check_stuck_transactions()
            if handled:
                logger.info("停滞していた取引 %s 件を処理しました", handled)
        except Exception:  # noqa: BLE001
            logger.exception("停滞取引の確認に失敗しました")
        try:
            await self.charge.escalate_manual_reviews()
        except Exception:  # noqa: BLE001
            logger.exception("手動確認の再通知に失敗しました")

    @stuck_checker.before_loop
    async def _before_stuck(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_NOTIFICATION_INTERVAL)
    async def notification_retry(self) -> None:
        """DM / 実績投稿の再送。"""
        try:
            await self.charge.process_notification_queue()
        except Exception:  # noqa: BLE001
            logger.exception("通知の再送に失敗しました")

    @notification_retry.before_loop
    async def _before_notification(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.KYASH_HEALTH_INTERVAL)
    async def kyash_health(self) -> None:
        """受取用 Kyash アカウントの健康確認。異常時は管理者へ通知する。

        複数アカウントに対応しているため、確認と通知はアカウント単位で行う。
        1台が落ちても他が生きていればチャージは継続できるので、
        「全台が使えない」ときだけ停止として扱う。
        """
        self.kyash.cleanup_pending_logins()
        slots = self.kyash.slots()
        if not slots:
            return
        for slot in slots:
            if slot.status == config.KyashAccountStatus.UNCONFIGURED:
                continue
            await self._check_slot_health(slot)
        await self._check_token_expiry()
        await self._check_wallet_threshold()

    async def _check_slot_health(self, slot: "AccountSlot") -> None:
        """1つのアカウントの状態を確認し、変化があれば通知する。"""
        label = slot.label
        try:
            status = await self.kyash.health_check(account_id=slot.id)
        except Exception:  # noqa: BLE001
            logger.exception("Kyash 健康確認に失敗しました (%s)", label)
            return
        if status == config.KyashAccountStatus.ACTIVE:
            if self._kyash_alerts.pop(slot.id, None) is not None:
                await self.bot.alert_owner(
                    f"✅ Kyash アカウント **{label}** のセッションが正常に復帰しました。"
                )
            return
        if self._kyash_alerts.get(slot.id) == status:
            return  # 同じ異常を繰り返し通知しない
        self._kyash_alerts[slot.id] = status
        usable = len(self.kyash.usable_slots())
        if usable:
            impact = (
                f"他に使えるアカウントが **{usable} 件** あるため、チャージは継続しています。"
            )
        else:
            impact = "使えるアカウントが無いため、**新規チャージは停止しています**。"
        await self.bot.alert_owner(
            f"🚨 Kyash アカウント **{label}** に異常があります (状態: {status})。\n"
            f"{impact}\n"
            f"`/kyash login account:{label}` で再ログインしてください。\n"
            f"詳細: {utils.sanitize_for_log(slot.last_error or '不明', limit=300)}"
        )

    @kyash_health.before_loop
    async def _before_kyash(self) -> None:
        await self._wait_ready()

    async def _check_token_expiry(self) -> None:
        """アクセストークンの失効が近いアカウントを事前警告する。

        上流仕様ではトークンの有効期間は発行から1ヶ月。更新用のエンドポイントは
        添付モジュールに存在しないため、自動更新はせず管理者へ再ログインを促す。
        """
        today = utils.format_jst(utils.now_ts())[:10]
        expiring_ids: set[int] = set()
        for slot in self.kyash.slots():
            if not slot.enabled:
                continue
            days_left = slot.token_days_left
            if days_left is None or days_left > config.KYASH_TOKEN_WARN_DAYS:
                continue
            expiring_ids.add(slot.id)
            if self._token_alert_days.get(slot.id) == today:
                continue  # 1日1回だけ通知する
            self._token_alert_days[slot.id] = today
            if days_left <= 0:
                message = (
                    f"🚨 Kyash アカウント **{slot.label}** の"
                    "アクセストークンの有効期限が切れている見込みです。\n"
                    f"`/kyash login account:{slot.label}` で再ログインしてください。"
                )
            else:
                message = (
                    f"⚠️ Kyash アカウント **{slot.label}** の"
                    f"アクセストークン残り期間が **{days_left:.1f} 日** です。\n"
                    f"失効前に `/kyash login account:{slot.label}` で再ログインしてください "
                    "(端末情報が保存されていれば SMS 認証は不要な場合があります)。"
                )
            logger.warning(
                "トークン期限の警告を通知しました (%s / 残り %.1f 日)", slot.label, days_left
            )
            await self.bot.alert_owner(message)
        # 期限が延びた (再ログインされた) アカウントの記録は消しておく
        for account_id in list(self._token_alert_days):
            if account_id not in expiring_ids:
                self._token_alert_days.pop(account_id, None)

    async def _check_wallet_threshold(self) -> None:
        """残高しきい値に達したアカウントを通知する。

        1台だけ到達した場合はフェイルオーバーで継続できるため警告にとどめ、
        使える全アカウントが到達したときだけ「停止」として通知する。
        """
        usable = self.kyash.usable_slots()
        for slot in usable:
            headroom = slot.headroom()
            if headroom is None:
                self._wallet_alerted.discard(slot.id)
                continue
            if headroom > 0:
                # しきい値の10%以上の余裕が戻ったら通知状態を解除する (ばたつき防止)
                if slot.id in self._wallet_alerted and headroom > slot.threshold * 0.1:
                    self._wallet_alerted.discard(slot.id)
                continue
            if slot.id in self._wallet_alerted:
                continue
            self._wallet_alerted.add(slot.id)
            balance = slot.wallet_balance or 0
            logger.error(
                "受取用アカウントの残高しきい値に到達しました (%s: %s / %s)",
                slot.label, balance, slot.threshold,
            )
            others = [s for s in usable if s.id != slot.id and not s.limit_reached]
            if others:
                impact = (
                    f"他のアカウント ({', '.join(s.label for s in others)}) で"
                    "受け取りを継続します。"
                )
            else:
                impact = "**新規チャージは停止しています。**"
            await self.bot.alert_owner(
                f"🚨 受取用Kyashアカウント **{slot.label}** の残高がしきい値に到達しました。\n"
                f"現在残高: {utils.fmt_yen(balance)} / "
                f"しきい値: {utils.fmt_yen(slot.threshold)}\n"
                f"{impact}\n"
                f"出金するか `/kyash threshold account:{slot.label}` を調整してください。"
            )
        # 全台が到達した場合は個別通知に加えて全体停止を明示する
        if usable and self.kyash.wallet_limit_reached:
            if not self._all_limit_alerted:
                self._all_limit_alerted = True
                await self.bot.alert_owner(
                    f"🛑 受取用Kyashアカウント **{len(usable)} 件すべて**が"
                    "残高しきい値に到達しました。\n"
                    "新規チャージは停止しています。出金または `/kyash add` で"
                    "受取アカウントを追加してください。"
                )
        else:
            self._all_limit_alerted = False

    @tasks.loop(seconds=config.TASK_RANKING_INTERVAL)
    async def ranking_refresher(self) -> None:
        """ランキングパネルの定期更新 (内容が変わらない場合は編集しない)。"""
        now = utils.now_ts()
        try:
            guild_ids = await self.db.guilds_with_ranking_panels()
        except Exception:  # noqa: BLE001
            logger.exception("ランキングパネル一覧の取得に失敗しました")
            return
        for guild_id in guild_ids:
            try:
                settings = await self.db.get_settings(guild_id)
                panels = await self.db.list_ranking_panels(guild_id)
                if not panels:
                    continue
                interval = max(
                    config.RANKING_INTERVAL_MIN,
                    min(config.RANKING_INTERVAL_MAX, settings.ranking_interval),
                )
                oldest = min(int(p["last_updated_at"] or 0) for p in panels)
                if now - oldest < interval:
                    continue
                await self.charge.refresh_ranking_panels(guild_id)
            except Exception:  # noqa: BLE001 - 1サーバーの失敗で全体を止めない
                logger.exception("ランキング更新に失敗しました guild=%s", guild_id)
        # 管理ダッシュボードも定期更新する
        try:
            for guild_id in await self.db.list_panel_guilds(config.PANEL_TYPE_ADMIN):
                try:
                    await self.charge.refresh_admin_panels(guild_id)
                except Exception:  # noqa: BLE001
                    logger.exception("管理パネルの更新に失敗しました guild=%s", guild_id)
        except Exception:  # noqa: BLE001
            logger.exception("管理パネル一覧の取得に失敗しました")

    @ranking_refresher.before_loop
    async def _before_ranking(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_BACKUP_INTERVAL)
    async def backup_task(self) -> None:
        """DB を定期バックアップする (最低1日1回・古い世代は削除)。"""
        try:
            last_raw = await self.db.get_system_value("last_backup_at")
            last = int(last_raw) if last_raw and last_raw.isdigit() else 0
            if utils.now_ts() - last < config.BACKUP_MIN_INTERVAL:
                return
            await self.run_backup()
        except Exception:  # noqa: BLE001
            logger.exception("バックアップ処理に失敗しました")

    @backup_task.before_loop
    async def _before_backup(self) -> None:
        await self._wait_ready()

    async def run_backup(self) -> Path:
        """バックアップを実行して古い世代を削除する。"""
        from datetime import datetime

        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(utils.now_ts(), utils.JST).strftime("%Y%m%d_%H%M%S")
        destination = config.BACKUP_DIR / f"charge_bot_{stamp}.db"
        try:
            await self.db.backup(destination)
        except Exception as exc:
            logger.error("バックアップに失敗しました: %s", utils.safe_error_text(exc))
            await self.bot.alert_owner(
                f"🚨 DB バックアップに失敗しました: {utils.safe_error_text(exc, limit=200)}"
            )
            raise
        await self.db.set_system_value("last_backup_at", str(utils.now_ts()))
        size = destination.stat().st_size
        logger.info("バックアップを作成しました: %s (%s bytes)", destination.name, size)
        self._prune_backups()
        await self._send_backup_remote(destination, size)
        return destination

    async def _send_backup_remote(self, path: Path, size: int) -> None:
        """設定に応じてバックアップを外部へ保存する (VPS 消失対策)。"""
        mode = await self.db.get_system_value("backup_remote") or config.BackupRemote.NONE
        if mode == config.BackupRemote.NONE:
            return
        if mode == config.BackupRemote.SECONDARY_DIR:
            target_dir = await self.db.get_system_value("backup_secondary_dir")
            if not target_dir:
                logger.warning("副バックアップ先が未設定のためコピーしません")
                return
            try:
                destination = Path(target_dir)
                destination.mkdir(parents=True, exist_ok=True)
                copied = destination / path.name
                await asyncio.to_thread(_copy_file, path, copied)
                try:
                    copied.chmod(0o600)
                except OSError:
                    pass
                logger.info("バックアップを副保存先へコピーしました: %s", copied)
            except Exception as exc:  # noqa: BLE001
                logger.error("副保存先へのコピーに失敗しました: %s", utils.safe_error_text(exc))
                await self.bot.alert_owner(
                    f"🚨 バックアップの副保存に失敗しました: {utils.safe_error_text(exc, limit=200)}"
                )
            return
        if mode == config.BackupRemote.OWNER_DM:
            if size > config.BACKUP_DM_MAX_BYTES:
                logger.warning("バックアップが大きすぎるため DM 送信をスキップします (%s bytes)", size)
                await self.bot.alert_owner(
                    f"⚠️ バックアップ `{path.name}` ({size / 1024 / 1024:.1f} MB) は "
                    "Discord の添付上限を超えるため送信しませんでした。"
                    "副保存先 (`/system backup_remote`) の利用を検討してください。"
                )
                return
            await self.bot.send_backup_to_owner(path)

    def _prune_backups(self) -> None:
        """保持世代数を超えた古いバックアップを削除する。"""
        files = sorted(
            config.BACKUP_DIR.glob("charge_bot_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in files[config.BACKUP_KEEP :]:
            try:
                path.unlink()
                logger.info("古いバックアップを削除しました: %s", path.name)
            except OSError as exc:
                logger.warning("バックアップの削除に失敗しました %s: %s", path.name, exc)

    @tasks.loop(seconds=config.TASK_SHOP_EXPIRY_INTERVAL)
    async def shop_expiry(self) -> None:
        """期限切れの購入ロールを剥奪する。"""
        try:
            await self.charge.expire_shop_purchases()
        except Exception:  # noqa: BLE001
            logger.exception("購入ロールの期限処理に失敗しました")

    @shop_expiry.before_loop
    async def _before_shop(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_SUBSCRIPTION_INTERVAL)
    async def subscription_task(self) -> None:
        """サブスクの更新予告と自動更新を処理する。"""
        try:
            await self.charge.run_subscriptions()
        except Exception:  # noqa: BLE001
            logger.exception("サブスクの処理に失敗しました")
        try:
            # 失効したブーストの行を溜め込まないよう、ここで一緒に掃除する
            await self.db.purge_expired_rate_boosts()
        except Exception:  # noqa: BLE001
            logger.exception("失効したチャージ率ブーストの掃除に失敗しました")

    @subscription_task.before_loop
    async def _before_subscription(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_AUCTION_INTERVAL)
    async def auction_task(self) -> None:
        """締切を過ぎたオークションを確定し、落札ロールの期限も見る。"""
        try:
            await self.charge.close_due_auctions()
        except Exception:  # noqa: BLE001
            logger.exception("オークションの締切処理に失敗しました")
        try:
            await self.charge.expire_auction_roles()
        except Exception:  # noqa: BLE001
            logger.exception("落札ロールの期限処理に失敗しました")

    @auction_task.before_loop
    async def _before_auction(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.PANEL_CACHE_TTL)
    async def panel_cache_task(self) -> None:
        """パネル表示用のキャッシュを温め続ける。

        ボタンの最初の応答を await ゼロで返すための土台。ここが動いていれば、
        DB が重い処理で塞がっていてもボタンは3秒以内に応答できる。
        """
        try:
            await self.charge.warm_panel_views()
        except Exception:  # noqa: BLE001
            logger.exception("パネル表示キャッシュの更新に失敗しました")

    @panel_cache_task.before_loop
    async def _before_panel_cache(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_GOAL_INTERVAL)
    async def goal_task(self) -> None:
        """チャージ目標の進捗を確認し、達成・期限切れを処理する。"""
        try:
            await self.charge.check_goals()
        except Exception:  # noqa: BLE001
            logger.exception("チャージ目標の確認に失敗しました")

    @goal_task.before_loop
    async def _before_goal(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_FRAUD_INTERVAL)
    async def fraud_task(self) -> None:
        """不正の兆候を洗い出して管理者へ知らせる (自動処分はしない)。"""
        try:
            await self.charge.run_fraud_scan()
        except Exception:  # noqa: BLE001
            logger.exception("不正検知に失敗しました")

    @fraud_task.before_loop
    async def _before_fraud(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_TIER_INTERVAL)
    async def tier_task(self) -> None:
        """累計チャージによる段位の取りこぼしを拾う。"""
        try:
            await self.charge.sweep_tiers()
        except Exception:  # noqa: BLE001
            logger.exception("段位の判定に失敗しました")

    @tier_task.before_loop
    async def _before_tier(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_RANKING_REWARD_INTERVAL)
    async def ranking_reward_task(self) -> None:
        """締めた期間のランキング報酬を配布する。"""
        try:
            await self.charge.run_ranking_rewards()
        except Exception:  # noqa: BLE001
            logger.exception("ランキング報酬の配布に失敗しました")

    @ranking_reward_task.before_loop
    async def _before_ranking_reward(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_CLAIM_INTERVAL)
    async def claim_task(self) -> None:
        """請求リンクの支払いを自動で確認し、期限切れを閉じる。"""
        try:
            await self.charge.check_waiting_payments()
        except Exception:  # noqa: BLE001
            logger.exception("請求リンクの支払い確認に失敗しました")
        try:
            await self.charge.expire_claim_transactions()
        except Exception:  # noqa: BLE001
            logger.exception("請求リンクの期限処理に失敗しました")

    @claim_task.before_loop
    async def _before_claim(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_REQUEST_INTERVAL)
    async def request_task(self) -> None:
        """チャージ申請の期限切れ処理と、未処理申請の催促。"""
        try:
            await self.charge.expire_stale_requests()
        except Exception:  # noqa: BLE001
            logger.exception("チャージ申請の期限処理に失敗しました")
        try:
            await self.charge.remind_pending_requests()
        except Exception:  # noqa: BLE001
            logger.exception("チャージ申請の催促に失敗しました")

    @request_task.before_loop
    async def _before_request(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_HEARTBEAT_INTERVAL)
    async def heartbeat(self) -> None:
        """死活監視。ファイルの更新時刻を進め、設定があれば外部URLへ通知する。

        systemd は再起動してくれるが、クラッシュループに入ると Discord へも
        出られず気づけないため、外部から検知できる手段を用意する。
        """
        path = config.DATA_DIR / "heartbeat"
        try:
            path.write_text(str(utils.now_ts()), encoding="utf-8")
        except OSError as exc:
            logger.warning("ハートビートファイルを更新できません: %s", exc)
        url = await self.db.get_system_value("heartbeat_url")
        if not url:
            return
        try:
            import requests

            def _ping() -> int:
                response = requests.get(url, timeout=10)
                return response.status_code

            status = await asyncio.to_thread(_ping)
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
            self._last_heartbeat_error = None
        except Exception as exc:  # noqa: BLE001 - 監視通知の失敗で Bot を止めない
            message = utils.safe_error_text(exc, limit=200)
            if message != self._last_heartbeat_error:
                self._last_heartbeat_error = message
                logger.warning("死活監視URLへの通知に失敗しました: %s", message)

    @heartbeat.before_loop
    async def _before_heartbeat(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_SUMMARY_INTERVAL)
    async def summary_task(self) -> None:
        """日次サマリを JST の指定時刻に1回だけ投稿する。"""
        from datetime import datetime

        now = datetime.fromtimestamp(utils.now_ts(), utils.JST)
        target_minutes = config.SUMMARY_POST_HOUR * 60 + config.SUMMARY_POST_MINUTE
        if now.hour * 60 + now.minute < target_minutes:
            return
        today = now.strftime("%Y-%m-%d")
        try:
            last = await self.db.get_system_value("last_summary_date")
            if last == today:
                return
            start = utils.jst_day_start() - 86400   # 前日 00:00
            end = utils.jst_day_start()             # 当日 00:00
            posted = 0
            for guild_id in await self.db.list_guilds_with_settings():
                if not await self.db.is_guild_allowed(guild_id):
                    continue
                try:
                    if await self.charge.post_daily_summary(guild_id, start=start, end=end):
                        posted += 1
                except Exception:  # noqa: BLE001
                    logger.exception("日次サマリの投稿に失敗しました guild=%s", guild_id)
            await self.db.set_system_value("last_summary_date", today)
            if posted:
                logger.info("日次サマリを %s サーバーへ投稿しました", posted)
        except Exception:  # noqa: BLE001
            logger.exception("日次サマリ処理に失敗しました")

    @summary_task.before_loop
    async def _before_summary(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_CAMPAIGN_INTERVAL)
    async def campaign_task(self) -> None:
        """招待キャンペーンの期限判定とサーバー許可の失効処理。"""
        try:
            confirmed = await self.charge.confirm_invites_by_days()
            if confirmed:
                logger.info("滞在日数の条件を満たした招待 %s 件を確定しました", confirmed)
        except Exception:  # noqa: BLE001
            logger.exception("招待の確定処理に失敗しました")
        try:
            expired = await self.db.expire_guild_permissions()
            for guild_id in expired:
                logger.warning("サーバー許可が期限切れになりました guild=%s", guild_id)
                await self.bot.alert_owner(
                    f"⏰ サーバー `{guild_id}` の利用許可が期限切れになりました。\n"
                    f"継続する場合は `/server allow guild_id:{guild_id}` を実行してください。"
                )
        except Exception:  # noqa: BLE001
            logger.exception("サーバー許可の失効処理に失敗しました")
        # 終了したキャンペーンの招待パネルを更新する
        try:
            for guild_id in await self.db.list_panel_guilds(config.PANEL_TYPE_INVITE):
                try:
                    await self.charge.refresh_invite_panels(guild_id)
                except Exception:  # noqa: BLE001
                    logger.exception("招待パネルの更新に失敗しました guild=%s", guild_id)
        except Exception:  # noqa: BLE001
            logger.exception("招待パネル一覧の取得に失敗しました")

    @campaign_task.before_loop
    async def _before_campaign(self) -> None:
        await self._wait_ready()

    @tasks.loop(seconds=config.TASK_INTEGRITY_INTERVAL)
    async def integrity_task(self) -> None:
        """DB 整合性チェック (自動修復は安全なものだけ)。"""
        try:
            result = await self.db.integrity_check()
        except Exception:  # noqa: BLE001
            logger.exception("整合性チェックに失敗しました")
            return
        orphans = result.get("orphan_queue") or []
        if orphans:
            removed = await self.db.cleanup_orphan_queue_items()
            logger.info("終了済み取引のキュー項目 %s 件を削除しました", removed)
        problems: list[str] = []
        if result.get("pragma") != "ok":
            problems.append(f"PRAGMA quick_check: {result.get('pragma')}")
        if result.get("completed_without_history"):
            problems.append(
                "残高履歴のない完了取引: " + ", ".join(result["completed_without_history"][:10])
            )
        if result.get("balance_mismatch"):
            problems.append(f"残高と履歴合計の不一致: {len(result['balance_mismatch'])} 件")
        if result.get("negative_balance"):
            problems.append(f"負の残高: {len(result['negative_balance'])} 件")
        if problems:
            logger.error("整合性の問題を検出しました: %s", problems)
            await self.bot.alert_owner(
                "🚨 DB 整合性チェックで問題を検出しました (自動修復していません)\n"
                + "\n".join(f"・{p}" for p in problems[:10])
            )

    @integrity_task.before_loop
    async def _before_integrity(self) -> None:
        await self._wait_ready()
