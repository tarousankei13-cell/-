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
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import discord

import config
import kyash_service
import ui
import utils
from database import AlreadyCredited, Database, GuildSettings, IllegalStateTransition

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

    # ==================================================================
    # 事前チェック
    # ==================================================================
    @property
    def processing_transaction_id(self) -> str | None:
        return self._processing_tx

    def check_button_rate_limit(self, user_id: int) -> None:
        if not self._button_rate_limiter.check(f"btn:{user_id}"):
            raise ChargeError(config.ErrorCode.RATE_LIMITED)

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
        if not self.kyash.is_usable:
            raise ChargeError(
                config.ErrorCode.KYASH_UNAVAILABLE,
                f"受取用Kyashアカウントの状態: {self.kyash.status}",
            )

    async def _check_limits(
        self, guild_id: int, user_id: int, amount: int, settings: GuildSettings
    ) -> None:
        """金額制限と日次上限の確認。"""
        if amount < settings.minimum_charge:
            raise ChargeError(config.ErrorCode.AMOUNT_BELOW_MIN)
        if amount > settings.maximum_charge:
            raise ChargeError(config.ErrorCode.AMOUNT_ABOVE_MAX)
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
            if await self.db.count_active_transactions(guild_id, user_id) > 0:
                raise ChargeError(config.ErrorCode.ACTIVE_TRANSACTION_EXISTS)
            await self._check_limits(guild_id, user_id, amount, settings)
            await self.db.ensure_user(guild_id, user_id)
            tx_id = await self.db.create_transaction(
                guild_id=guild_id,
                user_id=user_id,
                requested_amount=amount,
                charge_rate=settings.charge_rate,
                expires_at=utils.now_ts() + config.LINK_WAIT_SECONDS,
            )
        logger.info(
            "チャージを開始しました tx=%s guild=%s user=%s amount=%s rate=%s",
            tx_id, guild_id, user_id, amount, settings.charge_rate,
        )
        await self._safe(self.log_event(
            guild_id,
            "🟡 チャージ開始",
            fields=(
                ("利用者", f"<@{user_id}>", True),
                ("申請額", utils.fmt_yen(amount), True),
                ("取引ID", f"`{tx_id}`", True),
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
                raise ChargeError(config.ErrorCode.LINK_ALREADY_USED)

            await self.db.transition_status(
                tx_id, config.TxStatus.VALIDATING, expected=(config.TxStatus.WAITING_LINK,)
            )
            try:
                info = await self.kyash.link_check(canonical_url)
            except kyash_service.LinkIsClaimError as exc:
                await self._fail(tx_id, config.ErrorCode.LINK_IS_CLAIM, str(exc),
                                 expected=(config.TxStatus.VALIDATING,))
                raise ChargeError(config.ErrorCode.LINK_IS_CLAIM) from exc
            except kyash_service.LinkInvalidError as exc:
                await self._fail(tx_id, config.ErrorCode.INVALID_LINK, str(exc),
                                 expected=(config.TxStatus.VALIDATING,))
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

        try:
            await self.kyash.link_receive(link_uuid)
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
        # ここから先の失敗はチャージ結果に影響させない (すべて _safe 経由)
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
        guild = self.bot.get_guild(int(row["guild_id"]))
        guild_name = guild.name if guild else f"サーバー {row['guild_id']}"
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
        guild = self.bot.get_guild(int(row["guild_id"]))
        embed = ui.dm_review_embed(
            guild_name=guild.name if guild else f"サーバー {row['guild_id']}",
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
    ) -> discord.abc.Messageable | None:
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
        permissions = channel.permissions_for(guild.me) if isinstance(
            channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)
        ) else None
        if permissions is not None and not (permissions.send_messages and permissions.embed_links):
            logger.warning("チャンネルへの送信権限がありません guild=%s channel=%s",
                           guild_id, channel_id)
            return None
        return channel  # type: ignore[return-value]

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
        self, guild_id: int, settings: GuildSettings
    ) -> list[tuple[int, int, str]]:
        """ランキング表示用エントリを構築する。

        * Source of Truth は ``balances`` の現在値 (履歴の合計では計算しない)
        * Bot ユーザーは対象外 / 凍結ユーザーは DB 側で除外済み
        * 退会済みユーザーは取得できる範囲で表示 (設定により非表示)
        """
        limit = max(config.RANKING_LIMIT_MIN, min(config.RANKING_LIMIT_MAX, settings.ranking_limit))
        # Bot・退会ユーザーの除外で件数が減るため、多めに取得してから絞り込む
        rows = await self.db.get_ranking(guild_id, limit * 3 + 10)
        guild = self.bot.get_guild(guild_id)
        entries: list[tuple[int, int, str]] = []
        for row in rows:
            user_id = int(row["user_id"])
            balance = int(row["balance"])
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
        if settings.ranking_enabled:
            entries = await self.build_ranking_entries(guild_id, settings)
            signature = self.ranking_signature(entries)
            embed = ui.ranking_embed(guild, entries, settings, updated_at=utils.now_ts())
        else:
            signature = "DISABLED"
            embed = ui.ranking_disabled_embed()

        updated = 0
        for panel in panels:
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
    ) -> discord.abc.Messageable | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)  # type: ignore[assignment]
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return channel  # type: ignore[return-value]

    async def refresh_charge_panels(self, guild_id: int) -> int:
        """チャージパネルへ最新の設定値を反映する。"""
        panels = await self.db.list_panels(guild_id)
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
