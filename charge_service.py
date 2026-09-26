"""チャージ処理の中核。

安全性の優先順位: 安全性 > データ整合性 > 二重処理防止 > 再起動復旧 > 安定性 > UX

重要な不変条件
--------------
* 受取処理関数の戻り値だけで成功と判断しない (履歴 / Wallet と突合する)
* 外部処理の結果が不明な場合、再受取せず MANUAL_REVIEW へ移行する
* 同一 Transaction では一度しか残高を付与しない (DB 側で保証)
* 同じ送金リンクは二度と受取対象にならない (link_hash / link_uuid の UNIQUE 制約)
* Discord 通知やランキング更新の失敗で、確定済みのチャージを巻き戻さない
* 全クエリに guild_id を含め、他サーバーの残高を参照しない
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import discord

import config
import kyash_service
import ui
import utils
from database import (
    AlreadyCredited,
    Database,
    GuildSettings,
    IllegalStateTransition,
    ShopError,
)

if TYPE_CHECKING:
    from main import ChargeBot

logger = logging.getLogger(config.LOGGER_CHARGE)
queue_logger = logging.getLogger(config.LOGGER_QUEUE)


class ChargeError(Exception):
    """利用者操作に対する想定内のエラー。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail

    @property
    def user_message(self) -> str:
        return config.USER_ERROR_MESSAGES.get(
            self.code, config.USER_ERROR_MESSAGES[config.ErrorCode.UNKNOWN_ERROR]
        )


class ChargeService:
    """チャージのライフサイクル管理・残高操作・通知・ランキング更新。"""

    def __init__(self, bot: "ChargeBot", db: Database, kyash: kyash_service.KyashService) -> None:
        self.bot = bot
        self.db = db
        self.kyash = kyash
        self.accepting_new = True
        self.queue_wakeup = asyncio.Event()
        self._charge_rate_limiter = utils.RateLimiter(
            config.RATE_LIMIT_CHARGE_COUNT, config.RATE_LIMIT_CHARGE_WINDOW
        )
        self._button_rate_limiter = utils.RateLimiter(
            config.RATE_LIMIT_BUTTON_COUNT, config.RATE_LIMIT_BUTTON_WINDOW
        )
        self._link_locks = utils.KeyedLocks()
        self._user_locks = utils.KeyedLocks()
        self._ranking_tasks: dict[int, asyncio.Task[None]] = {}
        self._processing_tx: str | None = None
        #: 連続失敗によるクールダウン {(guild_id, user_id): [失敗時刻, ...]}
        self._failures: dict[tuple[int, int], list[float]] = {}
        self._cooldowns: dict[tuple[int, int], float] = {}
        #: 招待の帰属判定用キャッシュ {guild_id: {code: uses}}
        self._invite_cache: dict[int, dict[str, int]] = {}
        #: 運用メトリクス (/system で参照。永続化しない)
        self.metrics: dict[str, float] = {
            "charges_completed": 0, "charges_failed": 0, "manual_reviews": 0,
            "receive_count": 0, "receive_seconds": 0.0, "purchases": 0,
            "purchase_refunds": 0, "invites_confirmed": 0, "invites_rejected": 0,
            "invites_hold": 0,
        }

    # ==================================================================
    # 事前チェック
    # ==================================================================
    @property
    def processing_transaction_id(self) -> str | None:
        return self._processing_tx

    def guild_name(self, guild_id: int) -> str:
        """通知文に使うサーバー名 (キャッシュに無い場合は ID を表示)。"""
        guild = self.bot.get_guild(guild_id)
        return guild.name if guild else f"サーバー {guild_id}"

    def check_button_rate_limit(self, user_id: int) -> None:
        if not self._button_rate_limiter.check(f"btn:{user_id}"):
            raise ChargeError(config.ErrorCode.RATE_LIMITED)

    # ------------------------------------------------------------------
    # 連続失敗によるクールダウン (不正探索・いたずら対策)
    # ------------------------------------------------------------------
    def record_failure(self, guild_id: int, user_id: int) -> bool:
        """失敗を記録し、クールダウンへ入ったかどうかを返す。"""
        key = (guild_id, user_id)
        now = time.monotonic()
        history = [t for t in self._failures.get(key, [])
                   if now - t <= config.FAILURE_COOLDOWN_WINDOW]
        history.append(now)
        self._failures[key] = history
        if len(history) >= config.FAILURE_COOLDOWN_THRESHOLD:
            self._cooldowns[key] = now + config.FAILURE_COOLDOWN_SECONDS
            self._failures[key] = []
            logger.warning(
                "連続失敗によりクールダウンを適用しました guild=%s user=%s (%s秒)",
                guild_id, user_id, config.FAILURE_COOLDOWN_SECONDS,
            )
            return True
        return False

    def clear_failures(self, guild_id: int, user_id: int) -> None:
        """成功時に失敗カウントを消去する。"""
        self._failures.pop((guild_id, user_id), None)

    def cooldown_remaining(self, guild_id: int, user_id: int) -> int:
        """クールダウンの残り秒数 (0なら制限なし)。"""
        until = self._cooldowns.get((guild_id, user_id))
        if until is None:
            return 0
        remaining = int(until - time.monotonic())
        if remaining <= 0:
            self._cooldowns.pop((guild_id, user_id), None)
            return 0
        return remaining

    def clear_cooldown(self, guild_id: int, user_id: int) -> None:
        """管理者によるクールダウン解除。"""
        self._cooldowns.pop((guild_id, user_id), None)
        self._failures.pop((guild_id, user_id), None)

    async def ensure_usable_guild(self, guild_id: int) -> GuildSettings:
        """サーバーが許可されているか確認し、設定を返す。"""
        if not await self.db.is_guild_allowed(guild_id):
            raise ChargeError(config.ErrorCode.GUILD_DISABLED)
        return await self.db.get_settings(guild_id)

    async def preflight(self, guild_id: int, user_id: int, settings: GuildSettings) -> None:
        """チャージ開始前の総合チェック (安全側へ倒す)。"""
        if not self.accepting_new:
            raise ChargeError(config.ErrorCode.MAINTENANCE, "Bot が終了処理中です")
        if settings.emergency_stop:
            raise ChargeError(config.ErrorCode.EMERGENCY_STOP)
        if settings.maintenance:
            raise ChargeError(config.ErrorCode.MAINTENANCE)
        if await self.db.is_frozen(guild_id, user_id):
            raise ChargeError(config.ErrorCode.USER_FROZEN)
        remaining = self.cooldown_remaining(guild_id, user_id)
        if remaining > 0:
            raise ChargeError(
                config.ErrorCode.COOLDOWN, f"クールダウン中です (残り {remaining} 秒)"
            )
        if not self.kyash.is_usable:
            raise ChargeError(
                config.ErrorCode.KYASH_UNAVAILABLE,
                f"受取用Kyashアカウントの状態: {self.kyash.status}",
            )
        if self.kyash.wallet_limit_reached:
            raise ChargeError(
                config.ErrorCode.WALLET_LIMIT,
                f"受取用アカウントの残高しきい値に到達 (しきい値 {self.kyash.wallet_threshold})",
            )

    async def _check_limits(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        settings: GuildSettings,
        *,
        charge_rate: Decimal | None = None,
    ) -> None:
        """金額制限・日次上限・残高上限・受取用アカウントの余裕を確認する。"""
        if amount < settings.minimum_charge:
            raise ChargeError(config.ErrorCode.AMOUNT_BELOW_MIN)
        if amount > settings.maximum_charge:
            raise ChargeError(config.ErrorCode.AMOUNT_ABOVE_MAX)
        if settings.max_balance > 0:
            current = await self.db.get_balance(guild_id, user_id)
            expected = utils.calc_credited_amount(amount, charge_rate or settings.charge_rate)
            if current + expected > settings.max_balance:
                raise ChargeError(
                    config.ErrorCode.MAX_BALANCE_EXCEEDED,
                    f"残高上限 {settings.max_balance} に対し {current} + {expected} となります",
                )
        try:
            # 受取用アカウントの残高しきい値を超えないか (受取前に止める)
            self.kyash.check_wallet_capacity(amount)
        except kyash_service.WalletLimitError as exc:
            raise ChargeError(config.ErrorCode.WALLET_LIMIT, str(exc)) from exc
        day_start = utils.jst_day_start()
        if settings.daily_limit > 0:
            used = await self.db.sum_daily_charge(guild_id, user_id, day_start)
            if used + amount > settings.daily_limit:
                raise ChargeError(
                    config.ErrorCode.DAILY_LIMIT_EXCEEDED,
                    f"本日の利用額 {used} + {amount} > 上限 {settings.daily_limit}",
                )
        if settings.guild_daily_limit > 0:
            guild_used = await self.db.sum_daily_charge(guild_id, None, day_start)
            if guild_used + amount > settings.guild_daily_limit:
                raise ChargeError(
                    config.ErrorCode.GUILD_DAILY_LIMIT_EXCEEDED,
                    f"サーバー本日の利用額 {guild_used} + {amount} > 上限 {settings.guild_daily_limit}",
                )

    # ==================================================================
    # チャージ開始
    # ==================================================================
    async def resolve_charge_rate(
        self, guild_id: int, user_id: int, settings: GuildSettings
    ) -> tuple[Decimal, int | None]:
        """利用者に適用するチャージ率を解決する。

        ロール別レート (VIP 等) が設定されていて、利用者がそのロールを持つ場合は
        優先度の高いレートを採用する。該当がなければサーバー既定のレートを使う。

        Returns:
            ``(charge_rate, role_id or None)``
        """
        rows = await self.db.list_role_rates(guild_id)
        if not rows:
            return settings.charge_rate, None
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is None:
            return settings.charge_rate, None
        member_role_ids = {role.id for role in getattr(member, "roles", [])}
        for row in rows:  # 優先度降順に評価
            role_id = int(row["role_id"])
            if role_id in member_role_ids:
                rate = utils.to_decimal(row["charge_rate"])
                if rate is not None:
                    return rate, role_id
        return settings.charge_rate, None

    async def start_charge(
        self, guild_id: int, user_id: int, raw_amount: str
    ) -> tuple[str, int, GuildSettings]:
        """金額を検証して Transaction を作成する。

        Returns:
            ``(transaction_id, amount, settings)``
        """
        settings = await self.ensure_usable_guild(guild_id)
        # 入力検証を先に行い、書式エラーでレート制限を消費しない
        # (パネル操作側の _button_rate_limiter で連打は別途抑止している)
        amount = utils.parse_user_amount(raw_amount)
        if amount is None:
            raise ChargeError(config.ErrorCode.INVALID_AMOUNT)
        if not self._charge_rate_limiter.check(f"charge:{guild_id}:{user_id}"):
            raise ChargeError(config.ErrorCode.RATE_LIMITED)

        async with self._user_locks.acquire(f"{guild_id}:{user_id}"):
            await self.preflight(guild_id, user_id, settings)
            active = await self.db.count_active_transactions(
                guild_id, user_id, exclude_manual_review=settings.manual_review_allow_new
            )
            if active > 0:
                raise ChargeError(config.ErrorCode.ACTIVE_TRANSACTION_EXISTS)
            charge_rate, role_id = await self.resolve_charge_rate(guild_id, user_id, settings)
            await self._check_limits(
                guild_id, user_id, amount, settings, charge_rate=charge_rate
            )
            await self.db.ensure_user(guild_id, user_id)
            tx_id = await self.db.create_transaction(
                guild_id=guild_id,
                user_id=user_id,
                requested_amount=amount,
                charge_rate=charge_rate,
                expires_at=utils.now_ts() + config.LINK_WAIT_SECONDS,
            )
        logger.info(
            "チャージを開始しました tx=%s guild=%s user=%s amount=%s rate=%s role=%s",
            tx_id, guild_id, user_id, amount, charge_rate, role_id,
        )
        await self._safe(self.log_event(
            guild_id,
            "🟡 チャージ開始",
            fields=(
                ("利用者", f"<@{user_id}>", True),
                ("申請額", utils.fmt_yen(amount), True),
                ("取引ID", f"`{tx_id}`", True),
                (
                    "適用レート",
                    utils.fmt_rate(charge_rate) + (f" (<@&{role_id}>)" if role_id else ""),
                    True,
                ),
            ),
            color=config.Color.WARNING,
        ), context="開始ログ")
        return tx_id, amount, settings

    async def get_resumable_transaction(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        """リンク入力待ちのまま残っている Transaction を返す (再開用)。"""
        row = await self.db.get_active_transaction(guild_id, user_id)
        if row is None:
            return None
        if row["status"] != config.TxStatus.WAITING_LINK:
            return row
        if row["expires_at"] and row["expires_at"] < utils.now_ts():
            return row
        return row

    async def cancel_transaction(self, tx_id: str, user_id: int) -> None:
        """利用者によるキャンセル (リンク入力前のみ)。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "取引が見つかりません")
        if int(row["user_id"]) != user_id:
            raise ChargeError(config.ErrorCode.NOT_ALLOWED)
        if row["status"] not in (config.TxStatus.CREATED, config.TxStatus.WAITING_LINK):
            raise ChargeError(
                config.ErrorCode.UNKNOWN_ERROR, "この取引はもうキャンセルできません"
            )
        await self.db.transition_status(
            tx_id,
            config.TxStatus.CANCELLED,
            expected=(config.TxStatus.CREATED, config.TxStatus.WAITING_LINK),
            error_code=None,
            error_message="利用者によるキャンセル",
        )
        logger.info("チャージをキャンセルしました tx=%s", tx_id)

    # ==================================================================
    # 送金リンクの検証 → キュー投入
    # ==================================================================
    async def submit_link(self, tx_id: str, raw_link: str, user_id: int) -> dict[str, Any]:
        """送金リンクを検証し、受取キューへ登録する。

        リンクの完全なURLは DB に保存せず、ハッシュとリンクUUIDのみを保存する。
        """
        row = await self.db.get_transaction(tx_id)
        if row is None:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "取引が見つかりません")
        if int(row["user_id"]) != user_id:
            raise ChargeError(config.ErrorCode.NOT_ALLOWED)
        guild_id = int(row["guild_id"])
        if row["status"] != config.TxStatus.WAITING_LINK:
            if row["status"] == config.TxStatus.EXPIRED:
                raise ChargeError(config.ErrorCode.TRANSACTION_EXPIRED)
            raise ChargeError(
                config.ErrorCode.UNKNOWN_ERROR,
                f"この取引は現在 {config.STATUS_LABELS.get(row['status'], row['status'])} です",
            )
        if row["expires_at"] and row["expires_at"] < utils.now_ts():
            await self._fail(tx_id, config.ErrorCode.TRANSACTION_EXPIRED, "入力期限切れ",
                             expected=(config.TxStatus.WAITING_LINK,), status=config.TxStatus.EXPIRED)
            raise ChargeError(config.ErrorCode.TRANSACTION_EXPIRED)

        settings = await self.ensure_usable_guild(guild_id)
        await self.preflight(guild_id, user_id, settings)

        try:
            canonical_url, link_id = utils.normalize_kyash_link(raw_link)
        except utils.LinkParseError as exc:
            self._note_user_failure(guild_id, user_id, config.ErrorCode.INVALID_LINK)
            raise ChargeError(config.ErrorCode.INVALID_LINK, str(exc)) from exc
        finally:
            raw_link = ""  # 入力値の参照を破棄

        link_hash_value = utils.link_hash(link_id)

        # 同一リンクの同時処理を防ぐ (プロセス内ロック + DB UNIQUE 制約)
        async with self._link_locks.acquire(link_hash_value):
            duplicate = await self.db.find_transaction_by_link_hash(link_hash_value)
            if duplicate is not None:
                logger.warning(
                    "既に使用されたリンクが再送信されました tx=%s 既存tx=%s",
                    tx_id, duplicate["id"],
                )
                await self._fail(tx_id, config.ErrorCode.LINK_ALREADY_USED,
                                 f"既存取引 {duplicate['id']} と同じリンク",
                                 expected=(config.TxStatus.WAITING_LINK,))
                self._note_user_failure(guild_id, user_id, config.ErrorCode.LINK_ALREADY_USED)
                raise ChargeError(config.ErrorCode.LINK_ALREADY_USED)

            await self.db.transition_status(
                tx_id, config.TxStatus.VALIDATING, expected=(config.TxStatus.WAITING_LINK,)
            )
            try:
                info = await self.kyash.link_check(canonical_url)
            except kyash_service.LinkIsClaimError as exc:
                await self._fail(tx_id, config.ErrorCode.LINK_IS_CLAIM, str(exc),
                                 expected=(config.TxStatus.VALIDATING,))
                self._note_user_failure(guild_id, user_id, config.ErrorCode.LINK_IS_CLAIM)
                raise ChargeError(config.ErrorCode.LINK_IS_CLAIM) from exc
            except kyash_service.LinkInvalidError as exc:
                await self._fail(tx_id, config.ErrorCode.INVALID_LINK, str(exc),
                                 expected=(config.TxStatus.VALIDATING,))
                self._note_user_failure(guild_id, user_id, config.ErrorCode.INVALID_LINK)
                raise ChargeError(config.ErrorCode.INVALID_LINK) from exc
            except kyash_service.KyashAuthError as exc:
                # セッション異常: 受取は行われていないため、再入力できる状態へ戻す
                await self.kyash.deactivate(str(exc))
                await self._back_to_waiting(tx_id)
                await self.alert_admins(
                    guild_id, "Kyash セッション異常", "リンク検証中に認証エラーが発生しました。"
                    "`/kyash login` で再ログインしてください。"
                )
                raise ChargeError(config.ErrorCode.KYASH_AUTH_ERROR, str(exc)) from exc
            except kyash_service.KyashServiceError as exc:
                # 一時的な障害 → Transaction は維持して再入力を促す
                await self._back_to_waiting(tx_id)
                raise ChargeError(exc.error_code, str(exc)) from exc
            finally:
                canonical_url = ""  # URL の参照を破棄

            # リンクUUID による重複確認 (別のURL表記で同じリンクが来た場合)
            duplicate_uuid = await self.db.find_transaction_by_link_uuid(info.uuid)
            if duplicate_uuid is not None:
                logger.warning(
                    "同一リンクUUIDの再送信を検出しました tx=%s 既存tx=%s uuid=%s",
                    tx_id, duplicate_uuid["id"], utils.mask_identifier(info.uuid),
                )
                await self._fail(tx_id, config.ErrorCode.LINK_ALREADY_USED,
                                 f"既存取引 {duplicate_uuid['id']} と同じリンクUUID",
                                 expected=(config.TxStatus.VALIDATING,))
                raise ChargeError(config.ErrorCode.LINK_ALREADY_USED)

            requested = int(row["requested_amount"])
            if info.amount != requested:
                logger.warning(
                    "金額不一致 tx=%s 申請=%s リンク=%s", tx_id, requested, info.amount
                )
                await self._fail(
                    tx_id, config.ErrorCode.AMOUNT_MISMATCH,
                    f"申請額 {requested} / リンク金額 {info.amount}",
                    expected=(config.TxStatus.VALIDATING,),
                )
                await self._safe(self.notify_result(tx_id), context="金額不一致DM")
                self._note_user_failure(guild_id, user_id, config.ErrorCode.AMOUNT_MISMATCH)
                raise ChargeError(config.ErrorCode.AMOUNT_MISMATCH)

            try:
                await self.db.attach_link(
                    tx_id,
                    link_hash_value=link_hash_value,
                    link_uuid=info.uuid,
                    received_amount=info.amount,
                )
            except sqlite3.IntegrityError as exc:
                # UNIQUE 制約違反 = 同じリンクが並行して登録された
                logger.warning("リンク登録の競合を検出しました tx=%s: %s", tx_id, exc)
                await self._fail(tx_id, config.ErrorCode.LINK_ALREADY_USED, "リンク登録が競合しました",
                                 expected=(config.TxStatus.VALIDATING,))
                raise ChargeError(config.ErrorCode.LINK_ALREADY_USED) from exc

        logger.info(
            "リンク検証を完了しキューへ登録しました tx=%s amount=%s uuid=%s",
            tx_id, info.amount, utils.mask_identifier(info.uuid),
        )
        await self._safe(self.post_achievement(tx_id), context="実績投稿")
        await self._safe(self.log_event(
            guild_id,
            "🟡 受取キューへ登録",
            fields=(
                ("利用者", f"<@{user_id}>", True),
                ("金額", utils.fmt_yen(info.amount), True),
                ("取引ID", f"`{tx_id}`", True),
            ),
            color=config.Color.WARNING,
        ), context="キュー登録ログ")
        self.queue_wakeup.set()
        return {"tx_id": tx_id, "amount": info.amount}

    async def _back_to_waiting(self, tx_id: str) -> None:
        """VALIDATING から WAITING_LINK へ戻す (再入力を許可する)。"""
        try:
            await self.db.transition_status(
                tx_id, config.TxStatus.WAITING_LINK, expected=(config.TxStatus.VALIDATING,)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("WAITING_LINK への復帰に失敗しました tx=%s: %s", tx_id,
                           utils.safe_error_text(exc))

    def _note_user_failure(self, guild_id: int, user_id: int, code: str) -> None:
        """利用者起因の失敗を記録する (連続失敗でクールダウン)。"""
        if code in (
            config.ErrorCode.INVALID_LINK, config.ErrorCode.LINK_IS_CLAIM,
            config.ErrorCode.AMOUNT_MISMATCH, config.ErrorCode.LINK_ALREADY_USED,
            config.ErrorCode.LINK_EXPIRED,
        ):
            self.record_failure(guild_id, user_id)

    async def _fail(
        self,
        tx_id: str,
        code: str,
        detail: str,
        *,
        expected: tuple[str, ...] | None = None,
        status: str = config.TxStatus.FAILED,
    ) -> None:
        """Transaction を失敗 (または期限切れ) 状態にする。"""
        try:
            await self.db.transition_status(
                tx_id, status, expected=expected, error_code=code,
                error_message=utils.sanitize_for_log(detail),
            )
            if status == config.TxStatus.FAILED:
                self.metrics["charges_failed"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("失敗状態への更新に失敗しました tx=%s: %s", tx_id, utils.safe_error_text(exc))

    # ==================================================================
    # キュー処理 (受取用アカウントへの操作は常に1件ずつ)
    # ==================================================================
    async def process_queue_once(self) -> bool:
        """キューから1件だけ処理する。

        Returns:
            処理対象があった場合 True。
        """
        item = await self.db.fetch_next_queue_item()
        if item is None:
            return False
        tx_id = str(item["transaction_id"])
        attempts = int(item["attempts"])
        self._processing_tx = tx_id
        try:
            await self._process_transaction(tx_id, attempts)
        except Exception as exc:  # noqa: BLE001 - ワーカーを止めない
            queue_logger.exception("キュー処理で予期しない例外が発生しました tx=%s", tx_id)
            await self._handle_unknown_failure(tx_id, attempts, utils.safe_error_text(exc))
        finally:
            self._processing_tx = None
        return True

    async def _process_transaction(self, tx_id: str, attempts: int) -> None:
        """1件の Transaction を受取 → 確認 → 残高付与まで進める。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            await self.db.remove_from_queue(tx_id)
            return
        if row["status"] != config.TxStatus.QUEUED:
            queue_logger.info("状態が QUEUED でないためスキップします tx=%s status=%s",
                              tx_id, row["status"])
            if row["status"] in config.TERMINAL_STATUSES:
                await self.db.remove_from_queue(tx_id)
            return

        guild_id = int(row["guild_id"])
        link_uuid = row["link_uuid"]
        expected_amount = int(row["received_amount"] or row["requested_amount"])
        if not link_uuid:
            await self._fail(tx_id, config.ErrorCode.INVALID_LINK, "リンクUUIDが保存されていません",
                             expected=(config.TxStatus.QUEUED,))
            await self._safe(self.notify_result(tx_id), context="失敗DM")
            return

        # 緊急停止中は新しい受取処理を行わない (キューには残す)
        settings = await self.db.get_settings(guild_id)
        if settings.emergency_stop:
            queue_logger.warning("緊急停止中のため受取を保留します tx=%s", tx_id)
            await self._defer(tx_id, attempts, 60, "緊急停止中")
            return
        if not await self.db.is_guild_allowed(guild_id):
            # まだ受け取っていないため、ここで失敗にしても金銭は動かない
            await self._fail(tx_id, config.ErrorCode.GUILD_DISABLED, "サーバーが許可されていません",
                             expected=(config.TxStatus.QUEUED,))
            await self._safe(self.notify_result(tx_id), context="失敗DM")
            return
        if not self.kyash.is_usable:
            queue_logger.warning("Kyash セッションが利用不可のため保留します tx=%s status=%s",
                                 tx_id, self.kyash.status)
            await self._defer(tx_id, attempts, 60, f"Kyash状態: {self.kyash.status}")
            return

        # 受取前の残高を記録 (受取確認の基準になる)
        wallet_before: int | None = None
        try:
            wallet = await self.kyash.get_wallet()
            wallet_before = wallet.all_balance
        except kyash_service.KyashServiceError as exc:
            queue_logger.warning("受取前の残高照会に失敗しました tx=%s: %s", tx_id, exc)

        await self.db.transition_status(
            tx_id,
            config.TxStatus.PROCESSING,
            expected=(config.TxStatus.QUEUED,),
            processing_started_at=utils.now_ts(),
            wallet_before=wallet_before,
        )
        queue_logger.info("受取処理を開始します tx=%s uuid=%s amount=%s",
                          tx_id, utils.mask_identifier(link_uuid), expected_amount)

        receive_started = time.monotonic()
        try:
            await self.kyash.link_receive(link_uuid)
            self.metrics["receive_count"] += 1
            self.metrics["receive_seconds"] += time.monotonic() - receive_started
        except kyash_service.KyashRejectedError as exc:
            # Kyash が受取を拒否 (使用済み・無効など)。実状態を確認してから判断する。
            await self._resolve_after_rejection(
                tx_id, link_uuid, expected_amount, wallet_before, str(exc)
            )
            return
        except kyash_service.KyashAuthError as exc:
            await self.kyash.deactivate(str(exc))
            await self.alert_admins(
                guild_id, "Kyash 認証切れ",
                "受取処理中に認証エラーが発生しました。`/kyash login` で再ログインしてください。",
            )
            # 認証エラーではリクエストが処理されないため、受取は発生していない
            await self._retry_or_review(
                tx_id, attempts, config.ErrorCode.KYASH_AUTH_ERROR, str(exc)
            )
            return
        except (kyash_service.KyashTimeoutError, kyash_service.KyashNetworkError) as exc:
            # 結果不明: 必ず実状態を確認し、成功していれば再受取しない
            await self._resolve_unknown_outcome(
                tx_id, link_uuid, expected_amount, wallet_before, attempts,
                exc.error_code, str(exc),
            )
            return
        except kyash_service.KyashServiceError as exc:
            await self._resolve_unknown_outcome(
                tx_id, link_uuid, expected_amount, wallet_before, attempts,
                exc.error_code, str(exc),
            )
            return

        # 受取APIは成功を返した。実際に受け取れたかを確認する。
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid, amount=expected_amount, wallet_before=wallet_before
        )
        if verification.verdict == kyash_service.Verdict.NO_EVIDENCE:
            await asyncio.sleep(config.KYASH_RECEIPT_RECHECK_DELAY)
            verification = await self.kyash.verify_receipt(
                link_uuid=link_uuid, amount=expected_amount, wallet_before=wallet_before
            )
        if verification.verdict == kyash_service.Verdict.NO_EVIDENCE:
            queue_logger.error(
                "受取APIは成功を返したが受取の痕跡が確認できません tx=%s (%s)",
                tx_id, verification.detail,
            )
            await self._to_manual_review(
                tx_id, config.ErrorCode.MANUAL_REVIEW,
                f"受取成功応答だが確認できず: {verification.detail}",
            )
            return
        if verification.verdict == kyash_service.Verdict.UNAVAILABLE:
            queue_logger.warning(
                "受取確認情報を取得できませんでした。受取応答を採用します tx=%s (%s)",
                tx_id, verification.detail,
            )
        await self._mark_received_and_credit(tx_id, expected_amount)

    async def _resolve_after_rejection(
        self, tx_id: str, link_uuid: str, amount: int, wallet_before: int | None, message: str
    ) -> None:
        """受取拒否時の判定 (既に自分で受け取っていた可能性を排除する)。"""
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid, amount=amount, wallet_before=wallet_before
        )
        if verification.verdict == kyash_service.Verdict.CONFIRMED:
            queue_logger.warning(
                "受取拒否応答だが受取済みを確認したため完了処理を継続します tx=%s", tx_id
            )
            await self._mark_received_and_credit(tx_id, amount)
            return
        if verification.verdict == kyash_service.Verdict.NO_EVIDENCE:
            queue_logger.info("受取できないリンクとして失敗にします tx=%s (%s)", tx_id, message)
            await self._fail(
                tx_id, config.ErrorCode.LINK_ALREADY_USED, message,
                expected=(config.TxStatus.PROCESSING,),
            )
            await self._safe(self.notify_result(tx_id), context="失敗DM")
            return
        await self._to_manual_review(
            tx_id, config.ErrorCode.MANUAL_REVIEW,
            f"受取拒否だが実状態を確認できません: {message} / {verification.detail}",
        )

    async def _resolve_unknown_outcome(
        self,
        tx_id: str,
        link_uuid: str,
        amount: int,
        wallet_before: int | None,
        attempts: int,
        error_code: str,
        message: str,
    ) -> None:
        """タイムアウト等で結果が不明な場合の判定。"""
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid, amount=amount, wallet_before=wallet_before
        )
        if verification.verdict == kyash_service.Verdict.CONFIRMED:
            queue_logger.warning("通信エラー後に受取済みを確認しました tx=%s", tx_id)
            await self._mark_received_and_credit(tx_id, amount)
            return
        if verification.verdict == kyash_service.Verdict.NO_EVIDENCE:
            # 受け取れていないことを確認できたので、安全に再試行できる
            await self._retry_or_review(tx_id, attempts, error_code, message)
            return
        await self._to_manual_review(
            tx_id, config.ErrorCode.MANUAL_REVIEW,
            f"結果不明: {message} / {verification.detail}",
        )

    async def _retry_or_review(
        self, tx_id: str, attempts: int, error_code: str, message: str
    ) -> None:
        """指数バックオフで再試行し、上限に達したら MANUAL_REVIEW へ移す。"""
        next_attempts = attempts + 1
        if next_attempts >= config.MAX_RETRY:
            queue_logger.error("再試行上限に達しました tx=%s (%s)", tx_id, message)
            await self._to_manual_review(
                tx_id, error_code, f"再試行{next_attempts}回失敗: {message}"
            )
            return
        delay = min(
            config.RETRY_BACKOFF_MAX, config.RETRY_BACKOFF_BASE * (2 ** attempts)
        )
        await self.db.transition_status(
            tx_id,
            config.TxStatus.QUEUED,
            expected=(config.TxStatus.PROCESSING,),
            retry_count=next_attempts,
            error_code=error_code,
            error_message=utils.sanitize_for_log(message),
        )
        await self.db.mark_queue_attempt(
            tx_id, attempts=next_attempts,
            next_attempt_at=utils.now_ts() + delay, last_error=message,
        )
        queue_logger.warning(
            "%s 秒後に再試行します tx=%s (%s回目)", delay, tx_id, next_attempts
        )
        await self.log_event(
            None, "🔁 チャージ再試行",
            description=f"取引 `{tx_id}` を {delay} 秒後に再試行します。",
            fields=(("エラー", error_code, True), ("試行回数", str(next_attempts), True)),
            color=config.Color.WARNING,
            tx_id=tx_id,
        )

    async def _defer(self, tx_id: str, attempts: int, delay: int, reason: str) -> None:
        """受取を保留する (状態は QUEUED のまま)。"""
        await self.db.mark_queue_attempt(
            tx_id, attempts=attempts, next_attempt_at=utils.now_ts() + delay, last_error=reason
        )

    async def _to_manual_review(self, tx_id: str, code: str, detail: str) -> None:
        """MANUAL_REVIEW へ移行する (勝手に再受取しない)。"""
        self.metrics["manual_reviews"] += 1
        try:
            await self.db.transition_status(
                tx_id, config.TxStatus.MANUAL_REVIEW,
                error_code=code, error_message=utils.sanitize_for_log(detail),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("MANUAL_REVIEW への移行に失敗しました tx=%s: %s",
                         tx_id, utils.safe_error_text(exc))
            return
        await self.db.remove_from_queue(tx_id)
        row = await self.db.get_transaction(tx_id)
        guild_id = int(row["guild_id"]) if row else None
        logger.error("手動確認が必要な取引を検出しました tx=%s: %s", tx_id,
                     utils.sanitize_for_log(detail))
        await self.alert_admins(
            guild_id, "🟠 手動確認が必要です",
            f"取引 `{tx_id}` の受取結果を確認できませんでした。\n"
            f"`/transaction verify {tx_id}` で再確認、"
            f"`/transaction resolve` で完了/失敗を確定できます。\n"
            f"詳細: {utils.sanitize_for_log(detail, limit=300)}",
        )
        await self._safe(self.update_achievement(tx_id), context="実績更新")
        await self._safe(self.notify_review(tx_id), context="確認中DM")

    async def _handle_unknown_failure(self, tx_id: str, attempts: int, detail: str) -> None:
        """想定外の例外発生時は即 FAILED にせず安全側へ倒す。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            await self.db.remove_from_queue(tx_id)
            return
        status = row["status"]
        if status in config.TERMINAL_STATUSES:
            await self.db.remove_from_queue(tx_id)
            return
        if status == config.TxStatus.PROCESSING:
            # 受取処理に入っていた可能性があるため、確認できるまで MANUAL_REVIEW
            await self._to_manual_review(tx_id, config.ErrorCode.UNKNOWN_ERROR, detail)
            return
        await self._defer(tx_id, attempts, 30, detail)

    async def _mark_received_and_credit(self, tx_id: str, received_amount: int) -> None:
        """RECEIVED → CREDITING → COMPLETED まで進める。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            return
        if row["status"] in (config.TxStatus.PROCESSING, config.TxStatus.MANUAL_REVIEW):
            await self.db.transition_status(
                tx_id, config.TxStatus.RECEIVED,
                expected=(config.TxStatus.PROCESSING, config.TxStatus.MANUAL_REVIEW),
                received_amount=received_amount,
            )
        await self.credit_transaction(tx_id)

    async def credit_transaction(self, tx_id: str) -> dict[str, Any] | None:
        """チャージ率を適用して内部残高を付与する (冪等)。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            return None
        if row["status"] == config.TxStatus.COMPLETED:
            return None
        if row["status"] not in (
            config.TxStatus.RECEIVED, config.TxStatus.CREDITING, config.TxStatus.MANUAL_REVIEW
        ):
            logger.warning("残高付与できない状態です tx=%s status=%s", tx_id, row["status"])
            return None

        received = int(row["received_amount"] or 0)
        if received <= 0:
            await self._to_manual_review(
                tx_id, config.ErrorCode.UNKNOWN_ERROR, "受取額が確定していません"
            )
            return None
        # チャージ率は Transaction 作成時点の値を使用する (現在設定で再計算しない)
        credited = utils.calc_credited_amount(received, row["charge_rate"])

        if row["status"] != config.TxStatus.CREDITING:
            try:
                await self.db.transition_status(
                    tx_id, config.TxStatus.CREDITING,
                    expected=(config.TxStatus.RECEIVED, config.TxStatus.MANUAL_REVIEW),
                )
            except IllegalStateTransition as exc:
                logger.warning("CREDITING へ遷移できませんでした tx=%s: %s", tx_id, exc)

        try:
            result = await self.db.credit_transaction(tx_id, credited)
        except (AlreadyCredited, IllegalStateTransition) as exc:
            logger.warning("残高付与をスキップしました tx=%s: %s", tx_id, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.exception("残高付与に失敗しました tx=%s", tx_id)
            await self._to_manual_review(
                tx_id, config.ErrorCode.DATABASE_ERROR, utils.safe_error_text(exc)
            )
            return None

        if result["already_credited"]:
            logger.info("既に付与済みのため残高は変更しませんでした tx=%s", tx_id)
        else:
            logger.info(
                "チャージ完了 tx=%s guild=%s user=%s 受取=%s 付与=%s 残高=%s→%s",
                tx_id, result["guild_id"], result["user_id"], received,
                result["credited_amount"], result["balance_before"], result["balance_after"],
            )
        self.metrics["charges_completed"] += 1
        self.clear_failures(int(result["guild_id"]), int(result["user_id"]))
        # ここから先の失敗はチャージ結果に影響させない (すべて _safe 経由)
        await self._safe(
            self.log_balance_change(
                int(result["guild_id"]),
                change_type=config.BalanceChangeType.CHARGE,
                user_id=int(result["user_id"]),
                balance_before=int(result["balance_before"]),
                balance_after=int(result["balance_after"]),
                change=int(result["credited_amount"]),
                reason=f"チャージ完了 (送金 {received}円 / 率 {utils.fmt_rate(row['charge_rate'])})",
                transaction_id=tx_id,
            ),
            context="残高ログ",
        )
        # 招待キャンペーン: 被招待者の初回チャージで報酬を確定する
        await self._safe(
            self.confirm_invite_after_charge(int(result["guild_id"]), int(result["user_id"])),
            context="招待確定",
        )
        await self._safe(self.notify_result(tx_id), context="完了DM")
        await self._safe(self.update_achievement(tx_id), context="実績更新")
        try:
            self.request_ranking_refresh(int(result["guild_id"]))
        except Exception:  # noqa: BLE001
            logger.exception("ランキング更新要求に失敗しました")
        await self._safe(self.log_event(
            int(result["guild_id"]),
            "🟢 チャージ成功",
            fields=(
                ("利用者", f"<@{result['user_id']}>", True),
                ("送金額", utils.fmt_yen(received), True),
                ("付与", utils.fmt_int(result["credited_amount"]), True),
                ("残高", f"{utils.fmt_int(result['balance_before'])} → {utils.fmt_int(result['balance_after'])}", True),
                ("取引ID", f"`{tx_id}`", True),
            ),
            color=config.Color.SUCCESS,
        ), context="成功ログ")
        return result

    # ==================================================================
    # 再起動復旧 / 異常検知
    # ==================================================================
    async def recover_pending_transactions(self) -> dict[str, int]:
        """起動時に未完了 Transaction を現実の状態と突合して復旧する。

        受取成功の可能性がある場合、決して同じリンクを再受取しない。
        """
        summary = {"requeued": 0, "credited": 0, "manual_review": 0, "expired": 0}
        await self.db.cleanup_orphan_queue_items()
        expired = await self.db.expire_stale_transactions()
        summary["expired"] = len(expired)
        for row in expired:
            await self._safe(self.notify_result(str(row["id"])), context="期限切れDM")

        # 1) QUEUED: 受取は未実行 (PROCESSING になる前に落ちた) → そのまま再投入
        for row in await self.db.list_transactions_by_status([config.TxStatus.QUEUED]):
            await self.db.requeue_transaction(str(row["id"]), next_attempt_at=utils.now_ts())
            summary["requeued"] += 1

        # 2) RECEIVED / CREDITING: 受取は確認済み → 冪等な付与処理を実行
        for row in await self.db.list_transactions_by_status(
            [config.TxStatus.RECEIVED, config.TxStatus.CREDITING]
        ):
            logger.info("受取済み取引の残高付与を再実行します tx=%s", row["id"])
            await self.credit_transaction(str(row["id"]))
            summary["credited"] += 1

        # 3) PROCESSING: 受取したかどうか不明 → 実状態を確認し、不明なら MANUAL_REVIEW
        for row in await self.db.list_transactions_by_status([config.TxStatus.PROCESSING]):
            if await self._recover_processing(row):
                summary["credited"] += 1
            else:
                summary["manual_review"] += 1

        logger.info("未完了取引の復旧が完了しました: %s", summary)
        return summary

    async def _recover_processing(self, row: sqlite3.Row) -> bool:
        """PROCESSING のまま残った Transaction を安全に判定する。

        Returns:
            受取済みを確認して残高付与へ進めた場合 True、
            MANUAL_REVIEW へ移行した場合 False。
        """
        tx_id = str(row["id"])
        link_uuid = row["link_uuid"]
        amount = int(row["received_amount"] or row["requested_amount"])
        if not link_uuid:
            await self._to_manual_review(
                tx_id, config.ErrorCode.UNKNOWN_ERROR, "リンクUUIDが無いため判定できません"
            )
            return False
        if not self.kyash.is_usable:
            await self._to_manual_review(
                tx_id, config.ErrorCode.KYASH_UNAVAILABLE,
                "Kyash セッションが利用できないため受取状態を確認できません",
            )
            return False
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid, amount=amount,
            wallet_before=row["wallet_before"] if row["wallet_before"] is not None else None,
        )
        if verification.verdict == kyash_service.Verdict.CONFIRMED:
            logger.warning("再起動前に受取が成功していたことを確認しました tx=%s", tx_id)
            await self._mark_received_and_credit(tx_id, amount)
            return True
        # NO_EVIDENCE でも自動で再受取はしない (二重受取の可能性を残さない)
        await self._to_manual_review(
            tx_id, config.ErrorCode.MANUAL_REVIEW,
            f"再起動時に処理中だった取引です: {verification.detail}",
        )
        return False

    async def check_stuck_transactions(self) -> int:
        """一定時間進まない Transaction を検知して安全に処理する。"""
        threshold = utils.now_ts() - config.STUCK_PROCESSING_SECONDS
        handled = 0
        for row in await self.db.find_stuck_transactions(threshold):
            tx_id = str(row["id"])
            if tx_id == self._processing_tx:
                continue  # 現在処理中
            status = row["status"]
            logger.warning("停滞している取引を検出しました tx=%s status=%s", tx_id, status)
            if status in (config.TxStatus.RECEIVED, config.TxStatus.CREDITING):
                await self.credit_transaction(tx_id)
            elif status == config.TxStatus.PROCESSING:
                await self._recover_processing(row)
            handled += 1
        abandoned = await self.db.expire_abandoned_validating(
            utils.now_ts() - config.STUCK_PROCESSING_SECONDS
        )
        for row in abandoned:
            await self._safe(self.notify_result(str(row["id"])), context="期限切れDM")
        return handled + len(abandoned)

    async def expire_transactions(self) -> int:
        """期限切れの Transaction を EXPIRED にして利用者へ通知する。"""
        rows = await self.db.expire_stale_transactions()
        for row in rows:
            await self._safe(self.notify_result(str(row["id"])), context="期限切れDM")
        if rows:
            logger.info("%s 件の取引を期限切れにしました", len(rows))
        return len(rows)

    # ==================================================================
    # 管理者操作
    # ==================================================================
    async def admin_adjust_balance(
        self,
        *,
        guild_id: int,
        user_id: int,
        change_type: str,
        amount: int,
        operator_id: int,
        reason: str,
    ) -> dict[str, int]:
        """管理者による残高操作 (監査ログ + ランキング即時反映)。"""
        await self.db.ensure_user(guild_id, user_id)
        result = await self.db.adjust_balance(
            guild_id=guild_id, user_id=user_id, change_type=change_type,
            amount=amount, operator_id=operator_id, reason=reason,
        )
        op_id = await self.db.add_audit_log(
            actor_id=operator_id,
            action=change_type,
            guild_id=guild_id,
            target_user_id=user_id,
            detail={
                "amount": amount,
                "balance_before": result["balance_before"],
                "balance_after": result["balance_after"],
                "reason": utils.truncate(reason, 300),
            },
        )
        self.request_ranking_refresh(guild_id)
        await self._log_balance_from_history(
            guild_id, user_id, change_type=change_type, fallback=result,
            operator_id=operator_id, reason=reason, operation_id=op_id,
        )
        await self.log_event(
            guild_id,
            f"🛠 残高操作 ({config.BALANCE_TYPE_LABELS.get(change_type, change_type)})",
            fields=(
                ("対象", f"<@{user_id}>", True),
                ("操作者", f"<@{operator_id}>", True),
                ("変更", utils.fmt_int(result["change"]), True),
                ("残高", f"{utils.fmt_int(result['balance_before'])} → {utils.fmt_int(result['balance_after'])}", True),
                ("理由", utils.truncate(reason, 200), False),
                ("操作ID", f"`{op_id}`", True),
            ),
            color=config.Color.ACCENT,
        )
        logger.info(
            "管理者残高操作 guild=%s user=%s type=%s %s→%s operator=%s",
            guild_id, user_id, change_type, result["balance_before"],
            result["balance_after"], operator_id,
        )
        return result

    async def create_proxy_achievement(
        self,
        *,
        guild_id: int,
        user_id: int,
        amount: int,
        charge_rate: Decimal,
        credited_amount: int | None,
        operator_id: int,
        reason: str,
    ) -> dict[str, Any]:
        """管理者による代理実績の登録 (確認済みの正当な取引のみ)。"""
        credited = (
            int(credited_amount)
            if credited_amount is not None
            else utils.calc_credited_amount(amount, charge_rate)
        )
        await self.db.ensure_user(guild_id, user_id)
        tx_id = await self.db.create_proxy_transaction(
            guild_id=guild_id, user_id=user_id, requested_amount=amount,
            received_amount=amount, charge_rate=charge_rate,
        )
        result = await self.db.credit_transaction(
            tx_id, credited,
            change_type=config.BalanceChangeType.PROXY_ACHIEVEMENT,
            reason=f"代理実績: {utils.truncate(reason, 200)}",
            operator_id=operator_id,
        )
        op_id = await self.db.add_audit_log(
            actor_id=operator_id,
            action="ACHIEVEMENT_PROXY",
            guild_id=guild_id,
            target_user_id=user_id,
            detail={
                "transaction_id": tx_id,
                "amount": amount,
                "charge_rate": str(charge_rate),
                "credited_amount": credited,
                "balance_before": result["balance_before"],
                "balance_after": result["balance_after"],
                "reason": utils.truncate(reason, 300),
            },
        )
        self.request_ranking_refresh(guild_id)
        await self.post_achievement(tx_id)
        await self.log_event(
            guild_id,
            "🛠 代理実績を登録しました",
            fields=(
                ("対象", f"<@{user_id}>", True),
                ("操作者", f"<@{operator_id}>", True),
                ("送金額", utils.fmt_yen(amount), True),
                ("付与", utils.fmt_int(credited), True),
                ("取引ID", f"`{tx_id}`", True),
                ("操作ID", f"`{op_id}`", True),
                ("理由", utils.truncate(reason, 200), False),
            ),
            color=config.Color.ACCENT,
        )
        return {"transaction_id": tx_id, "operation_id": op_id, "credited_amount": credited, **result}

    async def resolve_manual_review(
        self, tx_id: str, *, complete: bool, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """MANUAL_REVIEW の取引を管理者判断で確定する。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "取引が見つかりません")
        if row["status"] != config.TxStatus.MANUAL_REVIEW:
            raise ChargeError(
                config.ErrorCode.UNKNOWN_ERROR,
                f"状態が MANUAL_REVIEW ではありません (現在: {row['status']})",
            )
        guild_id = int(row["guild_id"])
        if complete:
            result = await self.credit_transaction(tx_id)
            action = "TX_RESOLVE_COMPLETE"
        else:
            await self._fail(tx_id, config.ErrorCode.MANUAL_REVIEW,
                             f"管理者判断で失敗確定: {reason}",
                             expected=(config.TxStatus.MANUAL_REVIEW,))
            await self._safe(self.notify_result(tx_id), context="失敗DM")
            await self._safe(self.update_achievement(tx_id), context="実績更新")
            result = None
            action = "TX_RESOLVE_FAIL"
        op_id = await self.db.add_audit_log(
            actor_id=operator_id, action=action, guild_id=guild_id,
            target_user_id=int(row["user_id"]),
            detail={"transaction_id": tx_id, "reason": utils.truncate(reason, 300)},
        )
        return {"operation_id": op_id, "result": result}

    async def verify_transaction(self, tx_id: str) -> kyash_service.VerificationResult:
        """取引の受取状態を Kyash 側の情報で再確認する。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "取引が見つかりません")
        if not row["link_uuid"]:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "リンクUUIDが保存されていません")
        if not self.kyash.is_usable:
            raise ChargeError(config.ErrorCode.KYASH_UNAVAILABLE)
        amount = int(row["received_amount"] or row["requested_amount"])
        return await self.kyash.verify_receipt(
            link_uuid=str(row["link_uuid"]), amount=amount,
            wallet_before=row["wallet_before"] if row["wallet_before"] is not None else None,
        )

    async def requeue_manual_review(self, tx_id: str, operator_id: int) -> None:
        """受取されていないことを確認済みの取引を、再度受取キューへ戻す。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "取引が見つかりません")
        if row["status"] != config.TxStatus.MANUAL_REVIEW:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "MANUAL_REVIEW の取引ではありません")
        verification = await self.verify_transaction(tx_id)
        if verification.verdict != kyash_service.Verdict.NO_EVIDENCE:
            raise ChargeError(
                config.ErrorCode.MANUAL_REVIEW,
                f"未受取であることを確認できないため再試行できません (判定: {verification.verdict})",
            )
        await self.db.transition_status(
            tx_id, config.TxStatus.QUEUED, expected=(config.TxStatus.MANUAL_REVIEW,),
            retry_count=0,
        )
        await self.db.requeue_transaction(tx_id, next_attempt_at=utils.now_ts())
        await self.db.add_audit_log(
            actor_id=operator_id, action="TX_REQUEUE", guild_id=int(row["guild_id"]),
            target_user_id=int(row["user_id"]),
            detail={"transaction_id": tx_id, "verdict": verification.verdict},
        )
        self.queue_wakeup.set()

    # ==================================================================
    # 通知 (失敗してもチャージ結果に影響させない)
    # ==================================================================
    async def _safe(self, coro: Any, *, context: str) -> None:
        """通知系の処理を実行し、失敗しても呼び出し側へ例外を伝播させない。

        確定済みのチャージ (DB commit 済み) を Discord 側の失敗で巻き戻さないため、
        通知・ランキング更新はすべてこのラッパ経由で実行する。
        """
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("通知処理に失敗しました (%s)", context)

    async def notify_result(self, tx_id: str) -> None:
        """利用者へ DM で結果を通知する。失敗時は通知キューへ積む。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            return
        status = row["status"]
        if status not in (
            config.TxStatus.COMPLETED, config.TxStatus.FAILED,
            config.TxStatus.EXPIRED, config.TxStatus.CANCELLED,
        ):
            return
        if status == config.TxStatus.CANCELLED:
            return  # 利用者自身の操作のため DM しない
        guild_name = self.guild_name(int(row["guild_id"]))
        if status == config.TxStatus.COMPLETED:
            embed = ui.dm_success_embed(
                guild_name=guild_name,
                received_amount=int(row["received_amount"] or 0),
                charge_rate=row["charge_rate"],
                credited_amount=int(row["credited_amount"] or 0),
                balance_after=int(row["balance_after"] or 0),
                tx_id=tx_id,
                timestamp=int(row["completed_at"] or row["updated_at"]),
            )
        else:
            embed = ui.dm_failure_embed(
                guild_name=guild_name,
                tx_id=tx_id,
                error_code=row["error_code"] or config.ErrorCode.UNKNOWN_ERROR,
                timestamp=int(row["updated_at"]),
                requested_amount=int(row["requested_amount"]),
            )
        await self._send_dm(int(row["user_id"]), embed, tx_id=tx_id)

    async def notify_review(self, tx_id: str) -> None:
        """確認中であることを利用者へ通知する。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            return
        embed = ui.dm_review_embed(
            guild_name=self.guild_name(int(row["guild_id"])),
            tx_id=tx_id,
            timestamp=utils.now_ts(),
        )
        await self._send_dm(int(row["user_id"]), embed, tx_id=tx_id, queue_on_failure=False)

    async def _send_dm(
        self, user_id: int, embed: discord.Embed, *, tx_id: str | None = None,
        queue_on_failure: bool = True,
    ) -> bool:
        """DM を送信する。DM 拒否ユーザーでもチャージ結果には影響させない。"""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            logger.info("DM が拒否されているため通知できませんでした user=%s tx=%s", user_id, tx_id)
            return False
        except Exception as exc:  # noqa: BLE001 - DM 失敗はチャージ結果に影響させない
            logger.warning("DM 送信に失敗しました user=%s tx=%s: %s",
                           user_id, tx_id, utils.safe_error_text(exc))
            if queue_on_failure and tx_id:
                await self.db.enqueue_notification(
                    kind="DM_RESULT", payload={"tx_id": tx_id},
                    user_id=user_id, transaction_id=tx_id, delay=60,
                )
            return False

    async def post_achievement(self, tx_id: str) -> None:
        """実績チャンネルへ投稿する (処理中は 🟡 で先に投稿)。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            return
        guild_id = int(row["guild_id"])
        settings = await self.db.get_settings(guild_id)
        if not settings.achievement_channel_id:
            return
        if row["achievement_message_id"]:
            await self.update_achievement(tx_id)
            return
        channel = await self._resolve_channel(guild_id, settings.achievement_channel_id, "achievement_channel_id")
        if channel is None:
            return
        embed = self._build_achievement_embed(row)
        try:
            message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("実績の投稿に失敗しました tx=%s: %s", tx_id, utils.safe_error_text(exc))
            await self.db.enqueue_notification(
                kind="ACHIEVEMENT", payload={"tx_id": tx_id}, guild_id=guild_id,
                channel_id=settings.achievement_channel_id, transaction_id=tx_id, delay=60,
            )
            return
        try:
            await self.db.transition_status(
                tx_id, row["status"],
                achievement_channel_id=channel.id, achievement_message_id=message.id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("実績メッセージIDの保存に失敗しました tx=%s: %s",
                           tx_id, utils.safe_error_text(exc))

    async def update_achievement(self, tx_id: str) -> None:
        """既に投稿した実績 Embed を現在の状態へ更新する。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            return
        channel_id = row["achievement_channel_id"]
        message_id = row["achievement_message_id"]
        if not channel_id or not message_id:
            if row["status"] == config.TxStatus.COMPLETED:
                await self.post_achievement(tx_id)
            return
        guild_id = int(row["guild_id"])
        channel = await self._resolve_channel(guild_id, int(channel_id), "achievement_channel_id")
        if channel is None:
            return
        embed = self._build_achievement_embed(row)
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=embed)
        except discord.NotFound:
            logger.info("実績メッセージが存在しないため再投稿します tx=%s", tx_id)
            await self.db.transition_status(
                tx_id, row["status"], achievement_message_id=None
            )
            await self.post_achievement(tx_id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("実績の更新に失敗しました tx=%s: %s", tx_id, utils.safe_error_text(exc))
            await self.db.enqueue_notification(
                kind="ACHIEVEMENT", payload={"tx_id": tx_id}, guild_id=guild_id,
                channel_id=int(channel_id), transaction_id=tx_id, delay=60,
            )

    def _build_achievement_embed(self, row: sqlite3.Row) -> discord.Embed:
        return ui.achievement_embed(
            user_mention=f"<@{int(row['user_id'])}>",
            received_amount=int(row["received_amount"] or row["requested_amount"]),
            charge_rate=row["charge_rate"],
            credited_amount=(
                int(row["credited_amount"]) if row["credited_amount"] is not None else None
            ),
            status=row["status"],
            tx_id=str(row["id"]),
            timestamp=int(row["completed_at"] or row["created_at"]),
            proxy=row["source"] == config.TxSource.ADMIN_PROXY,
        )

    async def post_generic_achievement(
        self, guild_id: int, embed: discord.Embed
    ) -> tuple[int, int] | None:
        """実績チャンネルへ任意の実績を投稿する。

        Returns:
            ``(channel_id, message_id)``。投稿できなかった場合は None。
        """
        settings = await self.db.get_settings(guild_id)
        if not settings.achievement_channel_id:
            return None
        channel = await self._resolve_channel(
            guild_id, settings.achievement_channel_id, "achievement_channel_id"
        )
        if channel is None:
            return None
        try:
            message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("実績の投稿に失敗しました guild=%s: %s",
                           guild_id, utils.safe_error_text(exc))
            return None
        return channel.id, message.id

    async def update_generic_achievement(
        self, guild_id: int, channel_id: int | None, message_id: int | None,
        embed: discord.Embed,
    ) -> bool:
        """投稿済みの実績 Embed を更新する (状態が変わったとき)。"""
        if not channel_id or not message_id:
            return False
        channel = await self._resolve_channel(guild_id, int(channel_id), "achievement_channel_id")
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=embed)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("実績の更新に失敗しました guild=%s message=%s: %s",
                           guild_id, message_id, utils.safe_error_text(exc))
            return False

    async def post_shop_achievement(
        self, purchase_id: int, *, post_if_missing: bool = True
    ) -> None:
        """ショップ購入の実績を投稿・更新する。"""
        purchase = await self.db.get_purchase(purchase_id)
        if purchase is None:
            return
        guild_id = int(purchase["guild_id"])
        balance = await self.db.get_balance(guild_id, int(purchase["user_id"]))
        embed = ui.shop_achievement_embed(
            user_mention=f"<@{int(purchase['user_id'])}>",
            item_name=str(purchase["item_name"]),
            role_id=int(purchase["role_id"]),
            price=int(purchase["price"]),
            balance_after=balance,
            expires_at=purchase["expires_at"],
            purchase_id=purchase_id,
            timestamp=int(purchase["created_at"]),
            status=str(purchase["status"]),
        )
        if purchase["achievement_message_id"]:
            if await self.update_generic_achievement(
                guild_id, purchase["achievement_channel_id"],
                purchase["achievement_message_id"], embed,
            ):
                return
            if not post_if_missing:
                return
        result = await self.post_generic_achievement(guild_id, embed)
        if result is not None:
            channel_id, message_id = result
            await self.db.set_purchase_achievement(
                purchase_id, channel_id=channel_id, message_id=message_id
            )

    async def log_event(
        self,
        guild_id: int | None,
        title: str,
        description: str = "",
        *,
        fields: tuple[tuple[str, str, bool], ...] = (),
        color: int = config.Color.NEUTRAL,
        tx_id: str | None = None,
    ) -> None:
        """ログチャンネルへ送信する (秘密情報は含めない)。"""
        if guild_id is None and tx_id:
            row = await self.db.get_transaction(tx_id)
            if row is None:
                return
            guild_id = int(row["guild_id"])
        if guild_id is None:
            return
        settings = await self.db.get_settings(guild_id)
        if not settings.log_channel_id:
            return
        channel = await self._resolve_channel(guild_id, settings.log_channel_id, "log_channel_id")
        if channel is None:
            return
        embed = ui.log_embed(title, description, color=color, fields=fields)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("ログ送信に失敗しました guild=%s: %s", guild_id, utils.safe_error_text(exc))

    async def alert_admins(self, guild_id: int | None, title: str, description: str) -> None:
        """重大障害を管理者 (ログチャンネル) と Bot Owner へ通知する。"""
        logger.error("管理者通知: %s / %s", title, utils.sanitize_for_log(description, limit=300))
        if guild_id is not None:
            await self._safe(
                self.log_event(guild_id, f"🚨 {title}", description, color=config.Color.DANGER),
                context="管理者ログ",
            )
        await self._safe(self.bot.alert_owner(f"**{title}**\n{description}"), context="Owner通知")

    async def _resolve_channel(
        self, guild_id: int, channel_id: int, setting_name: str
    ) -> discord.TextChannel | discord.Thread | None:
        """チャンネルを解決する。削除済みなら設定を無効化して警告する。"""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)  # type: ignore[assignment]
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if channel is None:
            logger.warning("設定されたチャンネルが見つかりません guild=%s channel=%s (%s)",
                           guild_id, channel_id, setting_name)
            await self.db.update_settings(guild_id, **{setting_name: None})
            await self.bot.alert_owner(
                f"サーバー `{guild_id}` の `{setting_name}` に設定されたチャンネル "
                f"`{channel_id}` が見つからないため、設定を解除しました。"
            )
            return None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning("テキストチャンネルではないため使用しません guild=%s channel=%s",
                           guild_id, channel_id)
            return None
        permissions = channel.permissions_for(guild.me) if guild.me else None
        if permissions is not None and not (permissions.send_messages and permissions.embed_links):
            logger.warning("チャンネルへの送信権限がありません guild=%s channel=%s",
                           guild_id, channel_id)
            return None
        return channel

    # ==================================================================
    # ランキング (チャージパネルとは完全に独立)
    # ==================================================================
    def request_ranking_refresh(self, guild_id: int) -> None:
        """残高変更後のランキング更新要求 (短時間の変更をまとめる)。"""
        existing = self._ranking_tasks.get(guild_id)
        if existing is not None and not existing.done():
            return  # 既に予約済み → まとめて1回で更新する
        self._ranking_tasks[guild_id] = asyncio.create_task(
            self._debounced_refresh(guild_id), name=f"ranking-refresh-{guild_id}"
        )

    async def _debounced_refresh(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(config.RANKING_DEBOUNCE_SECONDS)
            await self.refresh_ranking_panels(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - ランキング更新失敗はチャージに影響させない
            logger.exception("ランキング更新に失敗しました guild=%s", guild_id)
        finally:
            self._ranking_tasks.pop(guild_id, None)

    async def build_ranking_entries(
        self,
        guild_id: int,
        settings: GuildSettings,
        ranking_type: str = config.RankingType.BALANCE,
    ) -> list[tuple[int, int, str]]:
        """ランキング表示用エントリを構築する。

        * 残高ランキングの Source of Truth は ``balances`` の現在値
          (履歴の合計では計算しない)
        * 週間・月間は ``charge_transactions`` の完了分 (取消済みは除外) を集計
        * 招待ランキングは確定した招待数を集計
        * Bot ユーザーは対象外 / 凍結ユーザーは DB 側で除外済み
        * 退会済みユーザーは取得できる範囲で表示 (設定により非表示)
        """
        limit = max(config.RANKING_LIMIT_MIN, min(config.RANKING_LIMIT_MAX, settings.ranking_limit))
        # Bot・退会ユーザーの除外で件数が減るため、多めに取得してから絞り込む
        fetch = limit * 3 + 10
        if ranking_type == config.RankingType.WEEKLY:
            rows = await self.db.get_charge_ranking(
                guild_id, since=utils.now_ts() - 7 * 86400, limit=fetch
            )
        elif ranking_type == config.RankingType.MONTHLY:
            rows = await self.db.get_charge_ranking(
                guild_id, since=utils.jst_month_start(), limit=fetch
            )
        elif ranking_type == config.RankingType.INVITE:
            rows = await self.db.get_invite_ranking(guild_id, limit=fetch)
        else:
            rows = await self.db.get_ranking(guild_id, fetch)
        guild = self.bot.get_guild(guild_id)
        entries: list[tuple[int, int, str]] = []
        keys = rows[0].keys() if rows else []
        value_key = "balance" if "balance" in keys else "total"
        for row in rows:
            user_id = int(row["user_id"])
            balance = int(row[value_key])
            member = guild.get_member(user_id) if guild else None
            if member is not None:
                if member.bot:
                    continue
                display = member.mention
            else:
                if settings.ranking_hide_absent:
                    continue
                user = self.bot.get_user(user_id)
                if user is not None and user.bot:
                    continue
                display = f"`{user.name}`" if user else f"`退会ユーザー ({user_id})`"
            entries.append((user_id, balance, display))
            if len(entries) >= limit:
                break
        return entries

    @staticmethod
    def ranking_signature(entries: list[tuple[int, int, str]]) -> str:
        """表示内容が変化したかを判定するための署名。"""
        return "|".join(f"{uid}:{bal}:{disp}" for uid, bal, disp in entries)

    async def refresh_ranking_panels(self, guild_id: int, *, force: bool = False) -> int:
        """ランキングパネルを必要な場合のみ編集する。

        Returns:
            実際に編集したパネル数。
        """
        panels = await self.db.list_ranking_panels(guild_id)
        if not panels:
            return 0
        settings = await self.db.get_settings(guild_id)
        guild = self.bot.get_guild(guild_id)
        # 集計方式ごとに1回だけ計算する
        cache: dict[str, tuple[str, discord.Embed]] = {}

        async def render(ranking_type: str) -> tuple[str, discord.Embed]:
            if ranking_type in cache:
                return cache[ranking_type]
            if not settings.ranking_enabled:
                result = ("DISABLED", ui.ranking_disabled_embed())
            else:
                entries = await self.build_ranking_entries(guild_id, settings, ranking_type)
                result = (
                    f"{ranking_type}|" + self.ranking_signature(entries),
                    ui.ranking_embed(
                        guild, entries, settings, updated_at=utils.now_ts(),
                        ranking_type=ranking_type,
                    ),
                )
            cache[ranking_type] = result
            return result

        updated = 0
        for panel in panels:
            panel_type = str(panel["ranking_type"] or config.RankingType.BALANCE)
            signature, embed = await render(panel_type)
            if not force and panel["last_signature"] == signature:
                continue  # 内容が変わっていないので Discord API を呼ばない
            channel = await self._resolve_message_channel(guild_id, int(panel["channel_id"]))
            if channel is None:
                await self.db.deactivate_ranking_panel(message_id=int(panel["message_id"]))
                continue
            try:
                message = await channel.fetch_message(int(panel["message_id"]))
                await message.edit(embed=embed, view=ui.RankingPanelView())
                await self.db.update_ranking_panel_state(
                    int(panel["message_id"]), signature=signature
                )
                updated += 1
            except discord.NotFound:
                logger.info("ランキングパネルが削除されていたため無効化します message=%s",
                            panel["message_id"])
                await self.db.deactivate_ranking_panel(message_id=int(panel["message_id"]))
                await self.bot.alert_owner(
                    f"サーバー `{guild_id}` のランキングパネル (message `{panel['message_id']}`) が"
                    "見つからないため無効化しました。`/ranking_panel` で再設置できます。"
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("ランキングパネルの更新に失敗しました message=%s: %s",
                               panel["message_id"], utils.safe_error_text(exc))
        return updated

    async def _resolve_message_channel(
        self, guild_id: int, channel_id: int
    ) -> discord.TextChannel | discord.Thread | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)  # type: ignore[assignment]
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        return channel

    async def refresh_charge_panels(self, guild_id: int) -> int:
        """チャージパネルへ最新の設定値を反映する。"""
        panels = await self.db.list_panels(guild_id, panel_type=config.PANEL_TYPE_CHARGE)
        if not panels:
            return 0
        settings = await self.db.get_settings(guild_id)
        embed = ui.charge_panel_embed(settings, kyash_ready=self.kyash.is_usable)
        updated = 0
        for panel in panels:
            channel = await self._resolve_message_channel(guild_id, int(panel["channel_id"]))
            if channel is None:
                await self.db.deactivate_panel(message_id=int(panel["message_id"]))
                continue
            try:
                message = await channel.fetch_message(int(panel["message_id"]))
                await message.edit(embed=embed, view=ui.ChargePanelView())
                updated += 1
            except discord.NotFound:
                logger.info("チャージパネルが削除されていたため無効化します message=%s",
                            panel["message_id"])
                await self.db.deactivate_panel(message_id=int(panel["message_id"]))
                await self.bot.alert_owner(
                    f"サーバー `{guild_id}` のチャージパネル (message `{panel['message_id']}`) が"
                    "見つからないため無効化しました。`/charge_panel` で再設置できます。"
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("チャージパネルの更新に失敗しました message=%s: %s",
                               panel["message_id"], utils.safe_error_text(exc))
        return updated

    # ==================================================================
    # 終了処理
    # ==================================================================
    async def shutdown(self) -> None:
        """新規受付を止め、保留中のランキング更新タスクを破棄する。

        DB を閉じる前に呼び出すことで、終了後にデバウンス待ちのタスクが
        走って例外になるのを防ぐ。
        """
        self.accepting_new = False
        pending = [task for task in self._ranking_tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._ranking_tasks.clear()
        if pending:
            logger.info("保留中のランキング更新 %s 件を破棄しました", len(pending))

    # ==================================================================
    # 残高操作ログ (指定チャンネルへ送信)
    # ==================================================================
    async def log_balance_change(
        self,
        guild_id: int,
        *,
        change_type: str,
        user_id: int,
        balance_before: int,
        balance_after: int,
        change: int,
        reason: str = "",
        operator_id: int | None = None,
        operation_id: str | None = None,
        transaction_id: str | None = None,
        history_id: int | None = None,
        audit_hash: str | None = None,
    ) -> None:
        """残高の変動を専用チャンネルへ記録する。

        ``balance_log_scope`` が ``MANUAL`` の場合は管理者の手動操作のみを送信し、
        ``ALL`` の場合はチャージ・招待報酬・ショップ購入などの自動変動も送信する。
        送信に失敗しても残高操作そのものには影響させない。
        """
        settings = await self.db.get_settings(guild_id)
        if not settings.balance_log_channel_id:
            return
        if settings.balance_log_scope != "ALL" and change_type not in config.MANUAL_BALANCE_TYPES:
            return
        channel = await self._resolve_channel(
            guild_id, settings.balance_log_channel_id, "balance_log_channel_id"
        )
        if channel is None:
            return
        label = config.BALANCE_TYPE_LABELS.get(change_type, change_type)
        sign = "+" if change > 0 else ""
        color = (
            config.Color.SUCCESS if change > 0
            else config.Color.DANGER if change < 0 else config.Color.NEUTRAL
        )
        fields: list[tuple[str, str, bool]] = [
            ("対象", f"<@{user_id}> (`{user_id}`)", True),
            ("種別", label, True),
            ("変動", f"**{sign}{utils.fmt_int(change)}**", True),
            ("変更前 → 変更後",
             f"{utils.fmt_int(balance_before)} → **{utils.fmt_int(balance_after)}**", True),
        ]
        if operator_id:
            fields.append(("操作者", f"<@{operator_id}> (`{operator_id}`)", True))
        if transaction_id:
            fields.append(("取引ID", f"`{transaction_id}`", True))
        if history_id is not None:
            fields.append(("履歴ID", f"`{history_id}`", True))
        if operation_id:
            fields.append(("操作ID", f"`{operation_id}`", True))
        if reason:
            fields.append(("理由", utils.truncate(reason, 400), False))
        if audit_hash:
            fields.append(("監査ハッシュ", f"`{audit_hash}`", False))
        embed = ui.log_embed(
            f"💳 残高変更: {label}", color=color, fields=tuple(fields)
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("残高ログの送信に失敗しました guild=%s: %s",
                           guild_id, utils.safe_error_text(exc))

    async def _log_balance_from_history(
        self, guild_id: int, user_id: int, *, change_type: str, fallback: dict[str, Any],
        operator_id: int | None = None, reason: str = "", operation_id: str | None = None,
        transaction_id: str | None = None,
    ) -> None:
        """直近の履歴行を引いて残高ログを送る (履歴IDを併記するため)。"""
        history_id = None
        try:
            rows, _ = await self.db.list_balance_history_filtered(
                guild_id, user_id=user_id, types=(change_type,), limit=1
            )
            if rows:
                history_id = int(rows[0]["id"])
        except Exception:  # noqa: BLE001
            history_id = None
        await self._safe(
            self.log_balance_change(
                guild_id,
                change_type=change_type,
                user_id=user_id,
                balance_before=int(fallback.get("balance_before", 0)),
                balance_after=int(fallback.get("balance_after", 0)),
                change=int(fallback.get("change", fallback.get("balance_after", 0)
                                        - fallback.get("balance_before", 0))),
                reason=reason,
                operator_id=operator_id,
                operation_id=operation_id,
                transaction_id=transaction_id,
                history_id=history_id,
            ),
            context="残高ログ",
        )

    # ==================================================================
    # ショップ (内部残高でロールを購入)
    # ==================================================================
    async def purchase_shop_item(
        self, member: discord.Member, item_id: int
    ) -> dict[str, Any]:
        """内部残高で商品 (ロール) を購入する。

        残高の引き落としは単一トランザクションで確定させ、その後ロールを付与する。
        ロール付与に失敗した場合は自動で返金し、利用者へ明示する。
        """
        guild = member.guild
        settings = await self.ensure_usable_guild(guild.id)
        if not settings.shop_enabled:
            raise ChargeError(config.ErrorCode.SHOP_ITEM_UNAVAILABLE, "ショップが無効です")
        if settings.emergency_stop:
            raise ChargeError(config.ErrorCode.EMERGENCY_STOP)
        if await self.db.is_frozen(guild.id, member.id):
            raise ChargeError(config.ErrorCode.USER_FROZEN)

        item = await self.db.get_shop_item(item_id, guild.id)
        if item is None or not item["active"]:
            raise ChargeError(config.ErrorCode.SHOP_ITEM_UNAVAILABLE)
        role = guild.get_role(int(item["role_id"]))
        if role is None:
            raise ChargeError(
                config.ErrorCode.SHOP_ITEM_UNAVAILABLE, "商品のロールが存在しません"
            )
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles or role >= me.top_role \
                or role.managed or role.is_default():
            raise ChargeError(
                config.ErrorCode.ROLE_ASSIGN_FAILED,
                "Bot がこのロールを付与できません (ロールの位置・権限を確認してください)",
            )
        duration = int(item["duration_days"])
        if duration == 0 and role in member.roles:
            raise ChargeError(config.ErrorCode.SHOP_ALREADY_OWNED)

        async with self._user_locks.acquire(f"shop:{guild.id}:{member.id}"):
            try:
                result = await self.db.purchase_shop_item(
                    guild_id=guild.id, user_id=member.id, item_id=item_id
                )
            except ShopError as exc:
                raise ChargeError(exc.code, exc.detail) from exc

            purchase_id = int(result["purchase_id"])
            try:
                await member.add_roles(
                    role, reason=f"ショップ購入 #{purchase_id} ({item['name']})"
                )
            except Exception as exc:  # noqa: BLE001 - 付与失敗時は必ず返金する
                logger.error(
                    "ロール付与に失敗したため返金します purchase=%s: %s",
                    purchase_id, utils.safe_error_text(exc),
                )
                try:
                    refund = await self.db.refund_purchase(
                        purchase_id, operator_id=None,
                        reason="ロール付与に失敗したため自動返金",
                        status=config.PurchaseStatus.FAILED,
                    )
                    await self._log_balance_from_history(
                        guild.id, member.id,
                        change_type=config.BalanceChangeType.SPEND_REFUND,
                        fallback=refund, reason="ロール付与失敗による自動返金",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("自動返金に失敗しました purchase=%s", purchase_id)
                    await self.alert_admins(
                        guild.id, "ショップの自動返金に失敗",
                        f"購入 `#{purchase_id}` のロール付与と返金の両方に失敗しました。"
                        "手動で `/shop refund` を実行してください。",
                    )
                raise ChargeError(config.ErrorCode.ROLE_ASSIGN_FAILED) from exc

            await self.db.activate_purchase(purchase_id)

        self.metrics["purchases"] += 1
        await self._safe(self.post_shop_achievement(purchase_id), context="購入実績")
        await self.db.add_audit_log(
            actor_id=member.id, action="SHOP_PURCHASE", guild_id=guild.id,
            target_user_id=member.id,
            detail={
                "purchase_id": purchase_id, "item_id": item_id, "item": item["name"],
                "price": result["price"], "role_id": role.id,
                "expires_at": result["expires_at"],
            },
        )
        await self._log_balance_from_history(
            guild.id, member.id, change_type=config.BalanceChangeType.SPEND,
            fallback=result, reason=f"ショップ購入: {item['name']}",
            transaction_id=f"SHOP-{purchase_id}",
        )
        await self._safe(self.log_event(
            guild.id, "🛒 ショップ購入",
            fields=(
                ("利用者", member.mention, True),
                ("商品", str(item["name"]), True),
                ("価格", utils.fmt_int(result["price"]), True),
                ("ロール", role.mention, True),
                ("期限", utils.format_jst(result["expires_at"]) if result["expires_at"] else "無期限", True),
                ("残高", f"{utils.fmt_int(result['balance_before'])} → "
                         f"{utils.fmt_int(result['balance_after'])}", True),
            ),
            color=config.Color.ACCENT,
        ), context="購入ログ")
        self.request_ranking_refresh(guild.id)
        return {**result, "role_id": role.id, "role_name": role.name}

    async def refund_shop_purchase(
        self, purchase_id: int, *, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """購入を返金し、付与したロールを剥奪する。"""
        purchase = await self.db.get_purchase(purchase_id)
        if purchase is None:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "購入記録が見つかりません")
        try:
            result = await self.db.refund_purchase(
                purchase_id, operator_id=operator_id, reason=reason
            )
        except ShopError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        guild_id = int(result["guild_id"])
        user_id = int(result["user_id"])
        await self._remove_purchase_role(guild_id, user_id, int(result["role_id"]),
                                         reason=f"購入返金 #{purchase_id}")
        self.metrics["purchase_refunds"] += 1
        await self._safe(
            self.post_shop_achievement(purchase_id, post_if_missing=False),
            context="返金実績の更新",
        )
        await self.db.add_audit_log(
            actor_id=operator_id, action="SHOP_REFUND", guild_id=guild_id,
            target_user_id=user_id,
            detail={"purchase_id": purchase_id, "price": result["price"],
                    "reason": utils.truncate(reason, 300)},
        )
        await self._log_balance_from_history(
            guild_id, user_id, change_type=config.BalanceChangeType.SPEND_REFUND,
            fallback=result, operator_id=operator_id, reason=f"購入返金: {reason}",
            transaction_id=f"SHOP-{purchase_id}",
        )
        await self._safe(self.log_event(
            guild_id, "↩️ ショップ返金",
            fields=(
                ("対象", f"<@{user_id}>", True),
                ("商品", str(result["item_name"]), True),
                ("返金額", utils.fmt_int(result["price"]), True),
                ("操作者", f"<@{operator_id}>", True),
                ("理由", utils.truncate(reason, 200), False),
            ),
            color=config.Color.WARNING,
        ), context="返金ログ")
        self.request_ranking_refresh(guild_id)
        return result

    async def _remove_purchase_role(
        self, guild_id: int, user_id: int, role_id: int, *, reason: str
    ) -> bool:
        """購入で付与したロールを剥奪する (他の有効な購入が残る場合は剥奪しない)。"""
        remaining = await self.db.list_active_purchases_for_role(guild_id, user_id, role_id)
        if remaining:
            logger.info(
                "他に有効な購入が残っているためロールを維持します guild=%s user=%s role=%s",
                guild_id, user_id, role_id,
            )
            return False
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        member = guild.get_member(user_id)
        role = guild.get_role(role_id)
        if member is None or role is None:
            return False
        if role not in member.roles:
            return True
        try:
            await member.remove_roles(role, reason=utils.truncate(reason, 400))
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("ロール剥奪に失敗しました guild=%s user=%s role=%s: %s",
                           guild_id, user_id, role_id, utils.safe_error_text(exc))
            await self.alert_admins(
                guild_id, "ロールの剥奪に失敗",
                f"<@{user_id}> の <@&{role_id}> を剥奪できませんでした。手動で外してください。",
            )
            return False

    async def expire_shop_purchases(self) -> int:
        """期限切れの購入ロールを剥奪する。"""
        rows = await self.db.list_expired_purchases()
        handled = 0
        for row in rows:
            purchase_id = int(row["id"])
            await self.db.mark_purchase_expired(purchase_id)
            await self._safe(
                self.post_shop_achievement(purchase_id, post_if_missing=False),
                context="期限切れ実績の更新",
            )
            await self._remove_purchase_role(
                int(row["guild_id"]), int(row["user_id"]), int(row["role_id"]),
                reason=f"購入期限切れ #{purchase_id}",
            )
            await self._safe(self.log_event(
                int(row["guild_id"]), "⌛ ロールの有効期限が切れました",
                fields=(
                    ("対象", f"<@{row['user_id']}>", True),
                    ("商品", str(row["item_name"]), True),
                    ("ロール", f"<@&{row['role_id']}>", True),
                ),
                color=config.Color.NEUTRAL,
            ), context="期限切れログ")
            handled += 1
        if handled:
            logger.info("期限切れの購入 %s 件を処理しました", handled)
        return handled

    # ==================================================================
    # 招待キャンペーン
    # ==================================================================
    async def sync_invite_cache(self, guild: discord.Guild) -> bool:
        """招待の使用回数をキャッシュする (帰属判定の基準)。

        Returns:
            取得できた場合 True。「サーバー管理」権限がない場合は False。
        """
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            logger.info(
                "招待一覧を取得できません (サーバー管理権限が必要) guild=%s", guild.id
            )
            return False
        except discord.HTTPException as exc:
            logger.warning("招待一覧の取得に失敗しました guild=%s: %s",
                           guild.id, utils.safe_error_text(exc))
            return False
        self._invite_cache[guild.id] = {
            invite.code: int(invite.uses or 0) for invite in invites
        }
        return True

    async def detect_used_invite(self, guild: discord.Guild) -> tuple[str | None, bool]:
        """参加時に使われた招待コードを特定する。

        Returns:
            ``(code, ambiguous)``。``code`` が None のときは特定できなかったことを表し、
            ``ambiguous`` が True なら複数候補があり判定を保留すべきことを表す。

        バニティURL・サーバー発見経由の参加は Discord の仕様上特定できない。
        """
        before = self._invite_cache.get(guild.id, {})
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None, False
        after = {invite.code: int(invite.uses or 0) for invite in invites}
        self._invite_cache[guild.id] = after
        increased = [code for code, uses in after.items() if uses > before.get(code, 0)]
        if len(increased) == 1:
            return increased[0], False
        if len(increased) > 1:
            return None, True
        # 使い切りで削除された招待を推定する
        disappeared = [code for code in before if code not in after]
        if len(disappeared) == 1:
            return disappeared[0], False
        return None, len(disappeared) > 1

    async def issue_invite_code(self, member: discord.Member) -> dict[str, Any]:
        """利用者専用の招待リンクを発行する (既存があれば再利用)。"""
        guild = member.guild
        await self.ensure_usable_guild(guild.id)
        campaign = await self.db.get_active_campaign(guild.id)
        if campaign is None:
            raise ChargeError(config.ErrorCode.CAMPAIGN_NOT_ACTIVE)
        if await self.db.is_invite_blacklisted(guild.id, member.id):
            raise ChargeError(config.ErrorCode.NOT_ALLOWED, "招待の利用が制限されています")

        existing = await self.db.get_invite_code_for_user(guild.id, member.id)
        if existing is not None and existing["url"]:
            # 招待が Discord 側で削除されていないか確認する
            code_alive = True
            try:
                invites = await guild.invites()
                code_alive = any(inv.code == existing["code"] for inv in invites)
            except (discord.Forbidden, discord.HTTPException):
                code_alive = True  # 確認できない場合は既存を返す
            if code_alive:
                summary = await self.db.get_invite_summary(guild.id, member.id)
                return {"code": existing["code"], "url": existing["url"],
                        "created": False, "summary": summary, "campaign": campaign}

        channel = self._pick_invite_channel(guild)
        if channel is None:
            raise ChargeError(
                config.ErrorCode.INVITE_NOT_AVAILABLE,
                "招待を作成できるチャンネルがありません (Bot に「招待を作成」権限が必要です)",
            )
        try:
            invite = await channel.create_invite(
                max_age=0, max_uses=0, unique=True,
                reason=f"招待キャンペーン: {member} ({member.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("招待リンクの作成に失敗しました guild=%s: %s",
                           guild.id, utils.safe_error_text(exc))
            raise ChargeError(config.ErrorCode.INVITE_NOT_AVAILABLE) from exc

        await self.db.save_invite_code(guild.id, member.id, invite.code, invite.url)
        # 発行直後にキャッシュへ反映し、初回の参加を取りこぼさない
        self._invite_cache.setdefault(guild.id, {})[invite.code] = 0
        summary = await self.db.get_invite_summary(guild.id, member.id)
        logger.info("招待リンクを発行しました guild=%s user=%s", guild.id, member.id)
        return {"code": invite.code, "url": invite.url, "created": True,
                "summary": summary, "campaign": campaign}

    def _pick_invite_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """招待を作成できるチャンネルを選ぶ。"""
        me = guild.me
        if me is None:
            return None
        candidates: list[discord.TextChannel] = []
        if isinstance(guild.rules_channel, discord.TextChannel):
            candidates.append(guild.rules_channel)
        if isinstance(guild.system_channel, discord.TextChannel):
            candidates.append(guild.system_channel)
        candidates.extend(guild.text_channels)
        for channel in candidates:
            perms = channel.permissions_for(me)
            if perms.create_instant_invite and perms.view_channel:
                return channel
        return None

    async def handle_member_join(self, member: discord.Member) -> None:
        """参加イベントを処理し、招待の帰属と不正判定を行う。"""
        guild = member.guild
        if not await self.db.is_guild_allowed(guild.id):
            return
        history = await self.db.record_member_join(guild.id, member.id)
        code, ambiguous = await self.detect_used_invite(guild)
        inviter_id = (
            await self.db.get_invite_code_owner(guild.id, code) if code else None
        )
        if code:
            await self.db.increment_invite_code_uses(guild.id, code)

        campaign = await self.db.get_active_campaign(guild.id)
        if campaign is None:
            return
        status, reason = await self._judge_invite(
            member, campaign, history, inviter_id, ambiguous
        )
        record_id, created = await self.db.record_invite(
            guild_id=guild.id,
            campaign_id=int(campaign["id"]),
            inviter_id=inviter_id,
            invited_id=member.id,
            code=code,
            status=status,
            reason=reason,
        )
        if not created:
            logger.info(
                "既に招待記録が存在するためスキップします guild=%s user=%s record=%s",
                guild.id, member.id, record_id,
            )
            return
        if status == config.InviteStatus.REJECTED:
            self.metrics["invites_rejected"] += 1
        elif status == config.InviteStatus.HOLD:
            self.metrics["invites_hold"] += 1

        reason_label = config.INVITE_REASON_LABELS.get(reason or "", reason or "-")
        await self._safe(self.log_event(
            guild.id,
            f"🤝 招待を記録しました ({config.INVITE_STATUS_LABELS.get(status, status)})",
            fields=(
                ("参加者", f"{member.mention} (`{member.id}`)", True),
                ("招待者", f"<@{inviter_id}>" if inviter_id else "不明", True),
                ("アカウント作成", utils.format_jst(int(member.created_at.timestamp())), True),
                ("判定理由", reason_label, True),
                ("記録ID", f"`{record_id}`", True),
            ),
            color=(
                config.Color.SUCCESS if status == config.InviteStatus.PENDING
                else config.Color.WARNING if status == config.InviteStatus.HOLD
                else config.Color.NEUTRAL
            ),
        ), context="招待ログ")

        if status == config.InviteStatus.HOLD:
            await self.alert_admins(
                guild.id, "招待の確認が必要です",
                f"参加者 <@{member.id}> / 招待者 <@{inviter_id}> の招待を保留しました。\n"
                f"理由: {reason_label}\n"
                f"`/campaign review` で承認または却下できます (記録ID `{record_id}`)。",
            )
        elif status == config.InviteStatus.PENDING and not campaign["require_charge"] \
                and int(campaign["require_days"] or 0) == 0:
            # 条件がない設定では即時確定する
            await self._confirm_invite(record_id)

    async def _judge_invite(
        self,
        member: discord.Member,
        campaign: sqlite3.Row,
        history: dict[str, Any],
        inviter_id: int | None,
        ambiguous: bool,
    ) -> tuple[str, str | None]:
        """招待の有効性を判定する。

        Returns:
            ``(status, reason)``
        """
        guild_id = member.guild.id
        if member.bot:
            return config.InviteStatus.REJECTED, config.InviteRejectReason.BOT_ACCOUNT
        if history.get("rejoin"):
            # 退出→再入場による報酬の周回を防ぐ
            return config.InviteStatus.REJECTED, config.InviteRejectReason.REJOIN
        if inviter_id is None:
            return config.InviteStatus.REJECTED, (
                config.InviteRejectReason.AMBIGUOUS if ambiguous
                else config.InviteRejectReason.UNKNOWN_INVITER
            )
        if inviter_id == member.id:
            return config.InviteStatus.REJECTED, config.InviteRejectReason.SELF_INVITE
        if await self.db.is_invite_blacklisted(guild_id, inviter_id):
            return config.InviteStatus.REJECTED, config.InviteRejectReason.BLACKLISTED

        now = utils.now_ts()
        account_age_days = (now - int(member.created_at.timestamp())) / 86400
        if account_age_days < int(campaign["min_account_age_days"] or 0):
            return config.InviteStatus.REJECTED, config.InviteRejectReason.ACCOUNT_TOO_NEW

        daily_limit = int(campaign["daily_limit"] or 0)
        if daily_limit > 0:
            today = await self.db.count_invites(
                guild_id, inviter_id, since=utils.jst_day_start()
            )
            if today >= daily_limit:
                return config.InviteStatus.REJECTED, config.InviteRejectReason.DAILY_LIMIT
        total_limit = int(campaign["total_limit"] or 0)
        if total_limit > 0:
            total = await self.db.count_invites(guild_id, inviter_id)
            if total >= total_limit:
                return config.InviteStatus.REJECTED, config.InviteRejectReason.TOTAL_LIMIT

        # --- 不審パターンは却下せず保留にして管理者が判断する ---
        recent = await self.db.count_recent_invites_by_inviter(
            guild_id, inviter_id, now - config.INVITE_BURST_WINDOW
        )
        if recent >= config.INVITE_BURST_COUNT:
            return config.InviteStatus.HOLD, config.InviteRejectReason.SUSPICIOUS_BURST

        inviter = member.guild.get_member(inviter_id)
        if inviter is not None:
            delta_days = abs(
                int(member.created_at.timestamp()) - int(inviter.created_at.timestamp())
            ) / 86400
            if delta_days <= config.INVITE_AGE_PROXIMITY_DAYS:
                return config.InviteStatus.HOLD, config.InviteRejectReason.SUSPICIOUS_AGE

        if campaign["require_review"]:
            return config.InviteStatus.HOLD, None
        return config.InviteStatus.PENDING, None

    async def handle_member_leave(self, guild_id: int, user_id: int) -> None:
        """退出を記録する (再入場の検知に使う)。"""
        try:
            await self.db.record_member_leave(guild_id, user_id)
        except Exception:  # noqa: BLE001
            logger.exception("退出の記録に失敗しました guild=%s user=%s", guild_id, user_id)

    async def confirm_invite_after_charge(self, guild_id: int, user_id: int) -> None:
        """チャージ完了をトリガーに招待報酬を確定する。"""
        record = await self.db.list_pending_invites_for_user(guild_id, user_id)
        if record is None:
            return
        campaign = await self.db.get_campaign(int(record["campaign_id"] or 0))
        if campaign is None or campaign["status"] != config.CampaignStatus.ACTIVE:
            return
        if not campaign["require_charge"]:
            return
        require_days = int(campaign["require_days"] or 0)
        if require_days > 0:
            elapsed = (utils.now_ts() - int(record["joined_at"])) / 86400
            if elapsed < require_days:
                logger.info(
                    "滞在日数の条件を満たしていないため確定を保留します record=%s", record["id"]
                )
                return
        await self._confirm_invite(int(record["id"]))

    async def confirm_invites_by_days(self) -> int:
        """滞在日数の条件を満たした招待を確定する (チャージ条件なしの設定)。"""
        rows = await self.db.list_invites_awaiting_days()
        confirmed = 0
        now = utils.now_ts()
        for row in rows:
            require_days = int(row["require_days"] or 0)
            if require_days <= 0:
                continue
            if (now - int(row["joined_at"])) / 86400 < require_days:
                continue
            guild = self.bot.get_guild(int(row["guild_id"]))
            if guild is not None and guild.get_member(int(row["invited_id"])) is None:
                # 既に退出している場合は確定しない
                await self.db.set_invite_status(
                    int(row["id"]), config.InviteStatus.REJECTED,
                    reason=config.InviteRejectReason.REJOIN,
                )
                continue
            if await self._confirm_invite(int(row["id"])):
                confirmed += 1
        return confirmed

    async def _confirm_invite(self, record_id: int) -> bool:
        """招待を確定して報酬を付与し、関係者へ通知する。"""
        try:
            result = await self.db.confirm_invite_and_reward(record_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("招待の確定に失敗しました record=%s", record_id)
            await self.alert_admins(
                None, "招待報酬の付与に失敗",
                f"記録ID `{record_id}` の確定に失敗しました: {utils.safe_error_text(exc, limit=200)}",
            )
            return False
        if result.get("already_confirmed"):
            return False
        guild_id = int(result["guild_id"])
        inviter_id = result.get("inviter_id")
        invited_id = int(result["invited_id"])
        self.metrics["invites_confirmed"] += 1
        logger.info(
            "招待報酬を付与しました record=%s guild=%s inviter=%s invited=%s",
            record_id, guild_id, inviter_id, invited_id,
        )
        for user_id, amount, label in (
            (inviter_id, int(result["reward_inviter"]), "招待報酬"),
            (invited_id, int(result["reward_invited"]), "参加ボーナス"),
        ):
            if not user_id or amount <= 0:
                continue
            balance = await self.db.get_balance(guild_id, user_id)
            await self._safe(
                self.log_balance_change(
                    guild_id,
                    change_type=config.BalanceChangeType.INVITE_REWARD,
                    user_id=user_id,
                    balance_before=balance - amount,
                    balance_after=balance,
                    change=amount,
                    reason=f"{label} (記録ID {record_id})",
                ),
                context="招待報酬の残高ログ",
            )
            await self._safe(
                self._send_dm(
                    user_id,
                    ui.invite_reward_embed(
                        guild_name=self.guild_name(guild_id),
                        label=label, amount=amount, balance_after=balance,
                        campaign_name=str(result.get("campaign_name") or ""),
                    ),
                ),
                context="招待報酬DM",
            )
        await self._safe(
            self.post_generic_achievement(
                guild_id,
                ui.invite_achievement_embed(
                    inviter_mention=f"<@{inviter_id}>" if inviter_id else None,
                    invited_mention=f"<@{invited_id}>",
                    inviter_reward=int(result["reward_inviter"]),
                    invited_reward=int(result["reward_invited"]),
                    campaign_name=str(result.get("campaign_name") or "招待キャンペーン"),
                    record_id=record_id,
                    timestamp=utils.now_ts(),
                ),
            ),
            context="招待実績",
        )
        await self._safe(self.log_event(
            guild_id, "🎉 招待報酬を付与しました",
            fields=(
                ("招待者", f"<@{inviter_id}>" if inviter_id else "-", True),
                ("参加者", f"<@{invited_id}>", True),
                ("招待者報酬", utils.fmt_int(result["reward_inviter"]), True),
                ("参加者報酬", utils.fmt_int(result["reward_invited"]), True),
                ("記録ID", f"`{record_id}`", True),
            ),
            color=config.Color.SUCCESS,
        ), context="招待確定ログ")
        self.request_ranking_refresh(guild_id)
        return True

    async def review_invite(
        self, record_id: int, *, approve: bool, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """保留中の招待を管理者が承認・却下する。"""
        record = await self.db.get_invite_record(record_id)
        if record is None:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, "招待記録が見つかりません")
        if record["status"] not in (config.InviteStatus.HOLD, config.InviteStatus.PENDING):
            raise ChargeError(
                config.ErrorCode.UNKNOWN_ERROR,
                f"確認できない状態です ({record['status']})",
            )
        guild_id = int(record["guild_id"])
        if approve:
            campaign = await self.db.get_campaign(int(record["campaign_id"] or 0))
            requires_more = bool(
                campaign and campaign["require_charge"]
                and record["status"] == config.InviteStatus.HOLD
            )
            if requires_more:
                # 承認後もチャージ条件の判定を継続させる
                await self.db.set_invite_status(
                    record_id, config.InviteStatus.PENDING,
                    reason="管理者承認 (チャージ条件の判定を継続)", reviewed_by=operator_id,
                )
                confirmed = False
            else:
                confirmed = await self._confirm_invite(record_id)
        else:
            await self.db.set_invite_status(
                record_id, config.InviteStatus.REJECTED,
                reason=utils.truncate(f"管理者却下: {reason}", 300), reviewed_by=operator_id,
            )
            confirmed = False
        op_id = await self.db.add_audit_log(
            actor_id=operator_id,
            action="INVITE_APPROVE" if approve else "INVITE_REJECT",
            guild_id=guild_id,
            target_user_id=int(record["invited_id"]),
            detail={"record_id": record_id, "inviter_id": record["inviter_id"],
                    "reason": utils.truncate(reason, 300)},
        )
        return {"operation_id": op_id, "confirmed": confirmed}

    # ==================================================================
    # 残高の詳細操作 (管理者向けラッパ)
    # ==================================================================
    async def move_balance(
        self,
        *,
        guild_id: int,
        from_user_id: int,
        to_user_id: int,
        amount: int,
        operator_id: int,
        reason: str,
    ) -> dict[str, Any]:
        """残高を利用者間で付け替える。"""
        result = await self.db.move_balance(
            guild_id=guild_id, from_user_id=from_user_id, to_user_id=to_user_id,
            amount=amount, operator_id=operator_id, reason=reason,
        )
        await self.db.add_audit_log(
            actor_id=operator_id, action="BALANCE_MOVE", guild_id=guild_id,
            target_user_id=to_user_id,
            detail={"from": from_user_id, "to": to_user_id, "amount": amount,
                    "operation_id": result["operation_id"],
                    "reason": utils.truncate(reason, 300)},
        )
        for user_id, before, after, change, change_type in (
            (from_user_id, result["from_before"], result["from_after"], -amount,
             config.BalanceChangeType.ADMIN_MOVE_OUT),
            (to_user_id, result["to_before"], result["to_after"], amount,
             config.BalanceChangeType.ADMIN_MOVE_IN),
        ):
            await self._safe(
                self.log_balance_change(
                    guild_id, change_type=change_type, user_id=user_id,
                    balance_before=before, balance_after=after, change=change,
                    reason=reason, operator_id=operator_id,
                    operation_id=result["operation_id"],
                ),
                context="付け替えの残高ログ",
            )
        self.request_ranking_refresh(guild_id)
        await self._safe(self.log_event(
            guild_id, "🔀 残高の付け替え",
            fields=(
                ("出金元", f"<@{from_user_id}>", True),
                ("入金先", f"<@{to_user_id}>", True),
                ("金額", utils.fmt_int(amount), True),
                ("操作者", f"<@{operator_id}>", True),
                ("理由", utils.truncate(reason, 200), False),
                ("操作ID", f"`{result['operation_id']}`", True),
            ),
            color=config.Color.ACCENT,
        ), context="付け替えログ")
        return result

    async def undo_balance_change(
        self, *, guild_id: int, history_id: int, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """残高変更履歴1件を逆仕訳で取り消す。"""
        result = await self.db.undo_balance_history(
            history_id, guild_id=guild_id, operator_id=operator_id, reason=reason
        )
        op_id = await self.db.add_audit_log(
            actor_id=operator_id, action="BALANCE_UNDO", guild_id=guild_id,
            target_user_id=int(result["user_id"]),
            detail={"history_id": history_id, "original_type": result["original_type"],
                    "original_change": result["original_change"],
                    "applied_change": result["applied_change"],
                    "reason": utils.truncate(reason, 300)},
        )
        await self._safe(
            self.log_balance_change(
                guild_id, change_type=config.BalanceChangeType.UNDO,
                user_id=int(result["user_id"]),
                balance_before=int(result["balance_before"]),
                balance_after=int(result["balance_after"]),
                change=int(result["applied_change"]),
                reason=f"履歴ID {history_id} の取消: {reason}",
                operator_id=operator_id, operation_id=op_id,
                history_id=int(result["undo_history_id"]),
            ),
            context="取消の残高ログ",
        )
        self.request_ranking_refresh(guild_id)
        await self._safe(self.log_event(
            guild_id, "↩️ 残高操作の取消",
            fields=(
                ("対象", f"<@{result['user_id']}>", True),
                ("取消した履歴", f"`{history_id}` ({result['original_type']})", True),
                ("適用差分", utils.fmt_int(result["applied_change"]), True),
                ("残高", f"{utils.fmt_int(result['balance_before'])} → "
                         f"{utils.fmt_int(result['balance_after'])}", True),
                ("操作者", f"<@{operator_id}>", True),
                ("理由", utils.truncate(reason, 200), False),
            ),
            color=config.Color.WARNING,
        ), context="取消ログ")
        return {**result, "operation_id": op_id}

    async def repair_balance(
        self, *, guild_id: int, user_id: int, operator_id: int, reason: str, mode: str
    ) -> dict[str, Any]:
        """残高と履歴合計の不一致を修復する。"""
        result = await self.db.repair_balance(
            guild_id=guild_id, user_id=user_id, operator_id=operator_id,
            reason=reason, mode=mode,
        )
        if not result.get("repaired"):
            return result
        op_id = await self.db.add_audit_log(
            actor_id=operator_id, action="BALANCE_REPAIR", guild_id=guild_id,
            target_user_id=user_id,
            detail={"mode": mode, "actual": result["actual"], "expected": result["expected"],
                    "diff": result["diff"], "reason": utils.truncate(reason, 300)},
        )
        await self._safe(
            self.log_balance_change(
                guild_id, change_type=config.BalanceChangeType.RECONCILE, user_id=user_id,
                balance_before=int(result["actual"]),
                balance_after=int(result["expected"]) if mode == "balance"
                else int(result["actual"]),
                change=0 if mode == "balance" else int(result["diff"]),
                reason=f"突合修復 (mode={mode}): {reason}",
                operator_id=operator_id, operation_id=op_id,
            ),
            context="修復の残高ログ",
        )
        if mode == "balance":
            self.request_ranking_refresh(guild_id)
        await self.alert_admins(
            guild_id, "残高の突合修復を実行しました",
            f"対象: <@{user_id}>\nmode: `{mode}`\n"
            f"残高 {result['actual']} / 履歴合計 {result['expected']} (差分 {result['diff']})\n"
            f"操作者: <@{operator_id}> / 操作ID `{op_id}`",
        )
        return {**result, "operation_id": op_id}

    async def refund_charge_transaction(
        self, tx_id: str, *, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """完了済みチャージを取り消して残高を回収する。"""
        result = await self.db.refund_transaction(
            tx_id, operator_id=operator_id, reason=reason
        )
        guild_id = int(result["guild_id"])
        user_id = int(result["user_id"])
        await self.db.add_audit_log(
            actor_id=operator_id, action="TX_REFUND", guild_id=guild_id,
            target_user_id=user_id,
            detail={"transaction_id": tx_id, "credited_amount": result["credited_amount"],
                    "applied_change": result["applied_change"],
                    "operation_id": result["operation_id"],
                    "reason": utils.truncate(reason, 300)},
        )
        await self._safe(
            self.log_balance_change(
                guild_id, change_type=config.BalanceChangeType.REVERSAL, user_id=user_id,
                balance_before=int(result["balance_before"]),
                balance_after=int(result["balance_after"]),
                change=int(result["applied_change"]),
                reason=f"チャージ取消: {reason}", operator_id=operator_id,
                operation_id=result["operation_id"], transaction_id=tx_id,
            ),
            context="取消の残高ログ",
        )
        self.request_ranking_refresh(guild_id)
        await self._safe(self.update_achievement(tx_id), context="実績更新")
        await self._safe(self.log_event(
            guild_id, "🚫 チャージの取消",
            fields=(
                ("対象", f"<@{user_id}>", True),
                ("取引ID", f"`{tx_id}`", True),
                ("回収額", utils.fmt_int(result["credited_amount"]), True),
                ("残高", f"{utils.fmt_int(result['balance_before'])} → "
                         f"{utils.fmt_int(result['balance_after'])}", True),
                ("操作者", f"<@{operator_id}>", True),
                ("理由", utils.truncate(reason, 200), False),
            ),
            color=config.Color.DANGER,
        ), context="取消ログ")
        await self._safe(
            self._send_dm(
                user_id,
                ui.refund_notice_embed(
                    guild_name=self.guild_name(guild_id),
                    tx_id=tx_id, amount=int(result["credited_amount"]),
                    balance_after=int(result["balance_after"]), reason=reason,
                ),
            ),
            context="取消DM",
        )
        return result

    # ==================================================================
    # MANUAL_REVIEW のエスカレーション
    # ==================================================================
    async def escalate_manual_reviews(self) -> int:
        """長時間解決されない MANUAL_REVIEW を管理者へ再通知する。"""
        threshold = utils.now_ts() - config.MANUAL_REVIEW_ESCALATION_SECONDS
        rows = await self.db.list_stale_manual_reviews(threshold)
        if not rows:
            return 0
        by_guild: dict[int, list[Any]] = {}
        for row in rows:
            by_guild.setdefault(int(row["guild_id"]), []).append(row)
        for guild_id, items in by_guild.items():
            lines = [
                f"・`{r['id']}` <@{r['user_id']}> "
                f"{utils.fmt_yen(int(r['received_amount'] or r['requested_amount']))} "
                f"({utils.format_jst(int(r['updated_at']))} から未解決)"
                for r in items[:10]
            ]
            await self.alert_admins(
                guild_id, f"手動確認が {len(items)} 件 未解決です",
                "\n".join(lines) + "\n`/transaction verify` → `/transaction resolve` で確定してください。",
            )
            # 再通知の間隔を空けるため updated_at を更新する
            for row in items:
                await self.db.execute(
                    "UPDATE charge_transactions SET updated_at=? WHERE id=? AND status=?",
                    (utils.now_ts(), str(row["id"]), config.TxStatus.MANUAL_REVIEW),
                )
        return len(rows)

    # ==================================================================
    # 日次サマリ
    # ==================================================================
    async def post_daily_summary(self, guild_id: int, *, start: int, end: int) -> bool:
        """前日の実績サマリをサマリチャンネルへ投稿する。"""
        settings = await self.db.get_settings(guild_id)
        if not settings.summary_enabled or not settings.summary_channel_id:
            return False
        channel = await self._resolve_channel(
            guild_id, settings.summary_channel_id, "summary_channel_id"
        )
        if channel is None:
            return False
        summary = await self.db.get_period_summary(guild_id, start=start, end=end)
        distribution = await self.db.get_balance_distribution(guild_id)
        embed = ui.daily_summary_embed(
            guild_name=self.guild_name(guild_id),
            start=start, end=end, summary=summary, distribution=distribution,
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("日次サマリの投稿に失敗しました guild=%s: %s",
                           guild_id, utils.safe_error_text(exc))
            return False
        return True

    def metrics_snapshot(self) -> dict[str, Any]:
        """運用メトリクスのスナップショット (/system 表示用)。"""
        receives = self.metrics.get("receive_count", 0)
        avg = (self.metrics.get("receive_seconds", 0.0) / receives) if receives else 0.0
        return {
            **{k: int(v) if isinstance(v, (int, float)) and k != "receive_seconds" else v
               for k, v in self.metrics.items()},
            "receive_avg_seconds": round(avg, 2),
            "cooldowns": len(self._cooldowns),
        }

    async def _update_panels(
        self,
        guild_id: int,
        panel_type: str,
        embed: discord.Embed,
        view: discord.ui.View,
    ) -> int:
        """指定種類のパネルメッセージを一括更新する。"""
        panels = await self.db.list_panels(guild_id, panel_type=panel_type)
        updated = 0
        for panel in panels:
            channel = await self._resolve_message_channel(guild_id, int(panel["channel_id"]))
            if channel is None:
                await self.db.deactivate_panel(message_id=int(panel["message_id"]))
                continue
            try:
                message = await channel.fetch_message(int(panel["message_id"]))
                await message.edit(embed=embed, view=view)
                updated += 1
            except discord.NotFound:
                logger.info("パネルが削除されていたため無効化します type=%s message=%s",
                            panel_type, panel["message_id"])
                await self.db.deactivate_panel(message_id=int(panel["message_id"]))
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("パネル更新に失敗しました type=%s message=%s: %s",
                               panel_type, panel["message_id"], utils.safe_error_text(exc))
        return updated

    async def refresh_shop_panels(self, guild_id: int) -> int:
        """ショップパネルへ最新の商品情報を反映する。"""
        settings = await self.db.get_settings(guild_id)
        items = await self.db.list_shop_items(guild_id)
        return await self._update_panels(
            guild_id, config.PANEL_TYPE_SHOP,
            ui.shop_panel_embed(settings, items), ui.ShopPanelView(),
        )

    async def refresh_invite_panels(self, guild_id: int) -> int:
        """招待パネルへ最新のキャンペーン情報を反映する。"""
        settings = await self.db.get_settings(guild_id)
        campaign = await self.db.get_active_campaign(guild_id)
        return await self._update_panels(
            guild_id, config.PANEL_TYPE_INVITE,
            ui.invite_panel_embed(settings, campaign), ui.InvitePanelView(),
        )

    async def build_admin_panel_embed(self, guild_id: int) -> discord.Embed:
        """管理ダッシュボードの Embed を構築する。"""
        settings = await self.db.get_settings(guild_id)
        stats = await self.db.get_statistics(guild_id)
        queue = await self.db.count_queue()
        reviews = await self.db.list_transactions_by_status(
            [config.TxStatus.MANUAL_REVIEW], limit=100
        )
        return ui.admin_panel_embed(
            guild_name=self.guild_name(guild_id),
            settings=settings,
            kyash=self.kyash.status_snapshot(),
            queue=queue,
            stats=stats,
            metrics=self.metrics_snapshot(),
            review_count=len([r for r in reviews if int(r["guild_id"]) == guild_id]),
            updated_at=utils.now_ts(),
        )

    async def refresh_admin_panels(self, guild_id: int) -> int:
        """管理ダッシュボードを更新する。"""
        panels = await self.db.list_panels(guild_id, panel_type=config.PANEL_TYPE_ADMIN)
        if not panels:
            return 0
        embed = await self.build_admin_panel_embed(guild_id)
        return await self._update_panels(
            guild_id, config.PANEL_TYPE_ADMIN, embed, ui.AdminPanelView()
        )

    # ==================================================================
    # 通知キューの再送
    # ==================================================================
    async def process_notification_queue(self) -> int:
        """DM / 実績投稿の再送を試みる。"""
        rows = await self.db.fetch_due_notifications()
        processed = 0
        for row in rows:
            notification_id = int(row["id"])
            attempts = int(row["attempts"]) + 1
            kind = row["kind"]
            tx_id = row["transaction_id"]
            try:
                if kind == "DM_RESULT" and tx_id:
                    await self.notify_result(str(tx_id))
                elif kind == "ACHIEVEMENT" and tx_id:
                    await self.post_achievement(str(tx_id))
                else:
                    await self.db.finish_notification(
                        notification_id, status="SKIPPED", error="未対応の通知種別"
                    )
                    continue
                await self.db.finish_notification(notification_id, status="SENT")
                processed += 1
            except Exception as exc:  # noqa: BLE001
                message = utils.safe_error_text(exc)
                if attempts >= config.NOTIFICATION_MAX_ATTEMPTS:
                    await self.db.finish_notification(
                        notification_id, status="FAILED", error=message
                    )
                    logger.warning("通知の再送を諦めました id=%s: %s", notification_id, message)
                else:
                    await self.db.retry_notification(
                        notification_id, attempts=attempts,
                        next_attempt_at=utils.now_ts() + 60 * attempts, error=message,
                    )
        return processed
