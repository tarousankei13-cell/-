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
import io
import logging
import sqlite3
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import discord

import chart
import config
import kyash_service
import price_service
import ui
import utils
from database import (
    AlreadyCredited,
    AuctionError,
    Database,
    GoalError,
    RefundError,
    GuildSettings,
    IllegalStateTransition,
    RequestError,
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

    def __init__(
        self,
        bot: "ChargeBot",
        db: Database,
        kyash: kyash_service.KyashService,
        price: "price_service.PriceService",
    ) -> None:
        self.bot = bot
        self.db = db
        self.kyash = kyash
        self.price = price
        self.accepting_new = True
        self.queue_wakeup = asyncio.Event()
        self._charge_rate_limiter = utils.RateLimiter(
            config.RATE_LIMIT_CHARGE_COUNT, config.RATE_LIMIT_CHARGE_WINDOW
        )
        self._button_rate_limiter = utils.RateLimiter(
            config.RATE_LIMIT_BUTTON_COUNT, config.RATE_LIMIT_BUTTON_WINDOW
        )
        # 返金申請の連打を抑える (審査する人の負担を守る)。
        # 本来の歯止めは「審査待ちの同時件数」なので、ここは連投だけを止める。
        self._request_rate_limiter = utils.RateLimiter(
            config.RATE_LIMIT_REFUND_COUNT, config.RATE_LIMIT_REFUND_WINDOW
        )
        #: レシートの署名 (鍵はトークン暗号鍵とは別ファイル)
        self.receipts = utils.ReceiptSigner(config.RECEIPT_KEY_PATH)
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
            "requests_created": 0, "requests_submitted": 0,
            "requests_approved": 0, "requests_rejected": 0, "requests_expired": 0,
            "subscription_renewals": 0, "subscription_stops": 0,
            "auctions_closed": 0, "bids": 0, "goal_rewards": 0,
            "fraud_flags": 0, "refund_requests": 0,
            "refunds_approved": 0, "refunds_rejected": 0,
        }
        # 目標パネルの前回の内容 (変わらないときは編集しない)
        self._goal_signatures: dict[int, str] = {}
        #: パネル表示用のキャッシュ {guild_id: (失効時刻(monotonic), 内容)}
        self._panel_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        # 設定が変わったら表示用キャッシュを捨てる (古い内容でボタンを出さない)
        self.db.on_settings_changed = self.invalidate_panel_view

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

    def ensure_open_for_charge(self, settings: GuildSettings) -> None:
        """受付中かどうかだけを **DB を触らずに** 確認する。

        ボタンの最初の応答は3秒以内に返す必要があるため、キャッシュ済みの
        設定だけで判断できるものはここで弾く。利用者ごとの確認 (凍結・
        クールダウン) は :meth:`preflight` が行う。
        """
        if not self.accepting_new:
            raise ChargeError(config.ErrorCode.MAINTENANCE, "Bot が終了処理中です")
        if settings.emergency_stop:
            raise ChargeError(config.ErrorCode.EMERGENCY_STOP)
        if settings.maintenance:
            raise ChargeError(config.ErrorCode.MAINTENANCE)

    async def preflight(
        self,
        guild_id: int,
        user_id: int,
        settings: GuildSettings,
        *,
        require_kyash: bool = True,
    ) -> None:
        """チャージ開始前の総合チェック (安全側へ倒す)。

        Args:
            require_kyash: 受取用 Kyash アカウントが使える状態かを要求するか。
                PayPay / LTC は Kyash を一切経由しないため False を渡す。
                Kyash が未設定でも、これらのチャージは受け付けられる。
        """
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
        if require_kyash:
            if not self.kyash.is_usable:
                raise ChargeError(
                    config.ErrorCode.KYASH_UNAVAILABLE,
                    f"受取用Kyashアカウントの状態: {self.kyash.status}",
                )
            if self.kyash.wallet_limit_reached:
                raise ChargeError(
                    config.ErrorCode.WALLET_LIMIT,
                    f"受取用アカウントの残高しきい値に到達 "
                    f"(しきい値 {self.kyash.wallet_threshold})",
                )

    async def _check_limits(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        settings: GuildSettings,
        *,
        charge_rate: Decimal | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
        check_wallet: bool = True,
    ) -> None:
        """金額制限・日次上限・残高上限・受取用アカウントの余裕を確認する。

        Args:
            minimum/maximum: 方式ごとの上下限。None ならサーバー設定を使う。
            check_wallet: Kyash 受取用アカウントの余裕を見るか。
                PayPay / LTC は Kyash を経由しないため False を渡す。
        """
        low = settings.minimum_charge if minimum is None else minimum
        high = settings.maximum_charge if maximum is None else maximum
        if amount < low:
            raise ChargeError(
                config.ErrorCode.AMOUNT_BELOW_MIN, f"最低 {low} / 入力 {amount}"
            )
        if amount > high:
            raise ChargeError(
                config.ErrorCode.AMOUNT_ABOVE_MAX, f"最大 {high} / 入力 {amount}"
            )
        if settings.max_balance > 0:
            current = await self.db.get_balance(guild_id, user_id)
            expected = utils.calc_credited_amount(amount, charge_rate or settings.charge_rate)
            if current + expected > settings.max_balance:
                raise ChargeError(
                    config.ErrorCode.MAX_BALANCE_EXCEEDED,
                    f"残高上限 {settings.max_balance} に対し {current} + {expected} となります",
                )
        if check_wallet:
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
            # ブーストは最後に足す。ここで確定した率を取引に保存するので、
            # ブーストが切れても進行中の取引には影響しない。
            charge_rate, boost = await self.apply_rate_boost(guild_id, user_id, charge_rate)
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
                    utils.fmt_rate(charge_rate)
                    + (f" (<@&{role_id}>)" if role_id else "")
                    + (f" ⚡+{utils.fmt_rate(boost)}" if boost else ""),
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
                    # 送金者名は不正検知 (名義貸し・転売) の判断材料として残す
                    sender_name=info.sender_name,
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

        # 受け取るアカウントを先に決める。複数登録されている場合は
        # しきい値に余裕のあるものが選ばれる。以後の確認は必ず同じ
        # アカウントに対して行う (他のアカウントの残高で誤判定しないため)。
        try:
            account = self.kyash.pick_slot(expected_amount)
        except kyash_service.NoCapacityError as exc:
            queue_logger.warning("受取可能なアカウントがありません tx=%s: %s", tx_id, exc)
            await self._defer(tx_id, attempts, 120, str(exc))
            await self._safe(self.bot.alert_owner(
                f"**受取用アカウントに余裕がありません**\n{exc}\n"
                "`/kyash list` で状態を確認し、必要なら `/kyash add` で追加してください。"
            ), context="Owner通知")
            return
        account_id = account.id

        # 受取前の残高を記録 (受取確認の基準になる)
        wallet_before: int | None = None
        try:
            wallet = await self.kyash.get_wallet(account_id)
            wallet_before = wallet.all_balance
        except kyash_service.KyashServiceError as exc:
            queue_logger.warning("受取前の残高照会に失敗しました tx=%s: %s", tx_id, exc)

        await self.db.transition_status(
            tx_id,
            config.TxStatus.PROCESSING,
            expected=(config.TxStatus.QUEUED,),
            processing_started_at=utils.now_ts(),
            wallet_before=wallet_before,
            kyash_account_id=account_id,
        )
        queue_logger.info("受取処理を開始します tx=%s uuid=%s amount=%s account=%s",
                          tx_id, utils.mask_identifier(link_uuid), expected_amount,
                          account.label)

        receive_started = time.monotonic()
        try:
            await self.kyash.link_receive(
                link_uuid, amount=expected_amount, account_id=account_id
            )
            self.metrics["receive_count"] += 1
            self.metrics["receive_seconds"] += time.monotonic() - receive_started
        except kyash_service.KyashRejectedError as exc:
            # Kyash が受取を拒否 (使用済み・無効など)。実状態を確認してから判断する。
            await self._resolve_after_rejection(
                tx_id, link_uuid, expected_amount, wallet_before, str(exc),
                account_id=account_id,
            )
            return
        except kyash_service.KyashAuthError as exc:
            await self.kyash.deactivate(str(exc), account_id=account_id)
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
                exc.error_code, str(exc), account_id=account_id,
            )
            return
        except kyash_service.KyashServiceError as exc:
            await self._resolve_unknown_outcome(
                tx_id, link_uuid, expected_amount, wallet_before, attempts,
                exc.error_code, str(exc), account_id=account_id,
            )
            return

        # 受取APIは成功を返した。実際に受け取れたかを確認する。
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid, amount=expected_amount, wallet_before=wallet_before,
            account_id=account_id,
        )
        if verification.verdict == kyash_service.Verdict.NO_EVIDENCE:
            await asyncio.sleep(config.KYASH_RECEIPT_RECHECK_DELAY)
            verification = await self.kyash.verify_receipt(
                link_uuid=link_uuid, amount=expected_amount, wallet_before=wallet_before,
                account_id=account_id,
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
        self, tx_id: str, link_uuid: str, amount: int, wallet_before: int | None,
        message: str, *, account_id: int | None = None,
    ) -> None:
        """受取拒否時の判定 (既に自分で受け取っていた可能性を排除する)。"""
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid, amount=amount, wallet_before=wallet_before,
            account_id=account_id,
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
        *,
        account_id: int | None = None,
    ) -> None:
        """タイムアウト等で結果が不明な場合の判定。"""
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid, amount=amount, wallet_before=wallet_before,
            account_id=account_id,
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
        if error_code in config.NON_RETRYABLE_ERRORS:
            # 何度試しても同じ結果になる種類 (金額不一致・使用済みリンク等)。
            # 再試行しても Kyash を無駄に叩くだけで、利用者への通知も遅れる。
            queue_logger.info(
                "再試行しても結果が変わらないため確定させます tx=%s code=%s",
                tx_id, error_code,
            )
            await self._fail(tx_id, error_code, message,
                             expected=(config.TxStatus.PROCESSING,))
            await self._safe(self.notify_result(tx_id), context="確定通知")
            return
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
        # 累計チャージ額による段位の自動昇格
        await self._safe(
            self.check_tiers(int(result["guild_id"]), int(result["user_id"])),
            context="段位判定",
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
            # 受け取ったアカウントで確認する (他のアカウントの残高では判定できない)
            account_id=row["kyash_account_id"],
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
    # 累計チャージによる段位 (自動昇格・降格なし)
    # ==================================================================
    async def check_tiers(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        """累計チャージ額を見て、到達した段位のロールを付与する。

        しきい値を超えた段位はすべて付与する (途中の段位を飛ばさない)。
        一度付与した段位は記録され、**降格はしない**。

        Returns:
            新しく付与した段位の行。
        """
        tiers = await self.db.list_tiers(guild_id)
        if not tiers:
            return []
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return []
        member = guild.get_member(user_id)
        if member is None:
            return []
        total = await self.db.get_total_charged(guild_id, user_id)
        granted_ids = await self.db.list_granted_tier_ids(guild_id, user_id)
        promoted: list[sqlite3.Row] = []
        for tier in tiers:
            tier_id = int(tier["id"])
            if tier_id in granted_ids:
                continue
            if total < int(tier["threshold"]):
                continue
            role = guild.get_role(int(tier["role_id"]))
            if role is None:
                logger.warning("段位のロールが存在しません guild=%s tier=%s role=%s",
                               guild_id, tier_id, tier["role_id"])
                continue
            problem = self.role_grant_problem(guild, role)
            if problem:
                logger.warning("段位のロールを付与できません guild=%s tier=%s: %s",
                               guild_id, tier_id, problem)
                await self._safe(self.bot.alert_owner(
                    f"サーバー `{guild_id}` の段位 **{tier['name']}** のロールを"
                    f"付与できません: {problem}"
                ), context="Owner通知")
                continue
            # 記録を先に取る。ロール付与が失敗しても二重に通知しないため。
            if not await self.db.record_tier_grant(
                guild_id=guild_id, user_id=user_id, tier_id=tier_id, total=total
            ):
                continue
            try:
                if role not in getattr(member, "roles", []):
                    await member.add_roles(role, reason=f"段位到達: {tier['name']}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("段位ロールの付与に失敗しました guild=%s user=%s: %s",
                               guild_id, user_id, utils.safe_error_text(exc))
                continue
            promoted.append(tier)
            logger.info("段位に到達しました guild=%s user=%s tier=%s total=%s",
                        guild_id, user_id, tier["name"], total)

        for tier in promoted:
            await self._safe(
                self._announce_tier(guild_id, user_id, tier, total), context="段位通知"
            )
        return promoted

    async def _announce_tier(
        self, guild_id: int, user_id: int, tier: sqlite3.Row, total: int
    ) -> None:
        """段位到達を実績チャンネルへ投稿し、本人へ DM する。"""
        embed = ui.tier_achievement_embed(
            user_mention=f"<@{user_id}>",
            tier=tier,
            total_charged=total,
            timestamp=utils.now_ts(),
        )
        await self.post_generic_achievement(guild_id, embed)
        await self._send_dm(
            user_id,
            ui.tier_dm_embed(
                guild_name=self.guild_name(guild_id), tier=tier, total_charged=total
            ),
            queue_on_failure=True,
        )
        await self.log_event(
            guild_id,
            "🎖 段位に到達しました",
            fields=(
                ("利用者", f"<@{user_id}>", True),
                ("段位", str(tier["name"]), True),
                ("ロール", f"<@&{int(tier['role_id'])}>", True),
                ("累計チャージ", utils.fmt_yen(total), True),
            ),
            color=config.Color.SUCCESS,
        )

    def role_grant_problem(
        self, guild: discord.Guild, role: discord.Role
    ) -> str | None:
        """Bot がそのロールを付与できない理由 (問題なければ None)。"""
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return "Bot に「ロールの管理」権限がありません"
        if role.managed:
            return "連携により管理されているロールです"
        if role.is_default():
            return "@everyone は指定できません"
        if role >= me.top_role:
            return "Bot のロールより上位のため付与できません"
        return None

    async def sweep_tiers(self, *, limit: int = 200) -> int:
        """段位の取りこぼしをまとめて拾う (しきい値変更後などの追いつき)。"""
        total_promoted = 0
        for guild in list(self.bot.guilds):
            if not await self.db.is_guild_allowed(guild.id):
                continue
            if not await self.db.list_tiers(guild.id):
                continue
            for user_id in await self.db.list_tier_candidates(guild.id, limit=limit):
                try:
                    total_promoted += len(await self.check_tiers(guild.id, user_id))
                except Exception:  # noqa: BLE001
                    logger.exception("段位判定に失敗しました guild=%s user=%s",
                                     guild.id, user_id)
        if total_promoted:
            logger.info("段位を %d 件付与しました (追いつき処理)", total_promoted)
        return total_promoted

    # ==================================================================
    # ランキング報酬 (締めた期間の上位へ自動配布)
    # ==================================================================
    async def distribute_ranking_rewards(
        self,
        guild_id: int,
        period: str,
        *,
        use_current: bool = False,
        operator_id: int | None = None,
    ) -> dict[str, Any]:
        """締めた期間のランキング上位へ報酬を配布する。

        ``ranking_reward_grants`` の UNIQUE 制約により、同じ期間・同じ利用者へ
        二重に配布されることはない。

        Args:
            use_current: True の場合、進行中の期間で配布する (手動実行用)。
            operator_id: 手動実行した管理者 (監査ログ用)。
        """
        rewards = await self.db.list_ranking_rewards(guild_id, period)
        if not rewards:
            return {"granted": 0, "skipped": 0, "period_key": None, "entries": []}
        if use_current:
            start, end, period_key = utils.current_period_bounds(period)
        else:
            start, end, period_key = utils.period_bounds(period)
            if await self.db.has_ranking_grants(guild_id, period, period_key):
                return {
                    "granted": 0, "skipped": 0, "period_key": period_key,
                    "entries": [], "already": True,
                }
        rows = await self.db.get_period_ranking(
            guild_id, start=start, end=end, limit=config.MAX_REWARD_RANK
        )
        guild = self.bot.get_guild(guild_id)
        granted = 0
        skipped = 0
        entries: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            reward = next(
                (r for r in rewards
                 if int(r["rank_from"]) <= index <= int(r["rank_to"])),
                None,
            )
            if reward is None:
                continue
            user_id = int(row["user_id"])
            amount = int(reward["amount"])
            role_id = reward["role_id"]
            if not await self.db.record_ranking_grant(
                guild_id=guild_id, ranking_type=period, period_key=period_key,
                user_id=user_id, rank=index, score=int(row["score"]),
                amount=amount, role_id=role_id,
            ):
                skipped += 1
                continue
            if amount > 0:
                try:
                    await self.admin_adjust_balance(
                        guild_id=guild_id, user_id=user_id,
                        change_type=config.BalanceChangeType.RANKING_REWARD,
                        amount=amount, operator_id=operator_id or 0,
                        reason=f"{config.RANKING_PERIOD_LABELS.get(period, period)}"
                               f"ランキング {index}位 ({period_key})",
                    )
                except ChargeError as exc:
                    logger.warning("ランキング報酬の付与に失敗しました guild=%s user=%s: %s",
                                   guild_id, user_id, exc.code)
            if role_id and guild is not None:
                member = guild.get_member(user_id)
                role = guild.get_role(int(role_id))
                if member is not None and role is not None and not self.role_grant_problem(
                    guild, role
                ):
                    try:
                        await member.add_roles(
                            role, reason=f"ランキング報酬 {index}位 ({period_key})"
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.warning("ランキング報酬のロール付与に失敗しました: %s",
                                       utils.safe_error_text(exc))
            granted += 1
            entries.append({
                "rank": index, "user_id": user_id, "score": int(row["score"]),
                "amount": amount, "role_id": role_id,
            })
        result = {
            "granted": granted, "skipped": skipped, "period_key": period_key,
            "entries": entries, "start": start, "end": end, "period": period,
        }
        if granted:
            logger.info("ランキング報酬を配布しました guild=%s %s %s → %d件",
                        guild_id, period, period_key, granted)
            await self._safe(
                self.post_generic_achievement(
                    guild_id, ui.ranking_reward_embed(guild_name=self.guild_name(guild_id),
                                                      result=result)
                ),
                context="ランキング報酬の実績投稿",
            )
            await self._safe(self.log_event(
                guild_id,
                f"🏆 {config.RANKING_PERIOD_LABELS.get(period, period)}ランキング報酬を配布",
                fields=(
                    ("期間", period_key, True),
                    ("配布", f"{granted} 人", True),
                    ("重複スキップ", f"{skipped} 人", True),
                ),
                color=config.Color.SUCCESS,
            ), context="ランキング報酬ログ")
        return result

    async def run_ranking_rewards(self) -> int:
        """報酬設定のある全サーバーについて、締めた期間の配布を試す。"""
        total = 0
        for guild_id in await self.db.list_reward_guilds():
            if not await self.db.is_guild_allowed(guild_id):
                continue
            for period in (config.RankingPeriod.WEEKLY, config.RankingPeriod.MONTHLY):
                try:
                    result = await self.distribute_ranking_rewards(guild_id, period)
                    total += int(result["granted"])
                except Exception:  # noqa: BLE001
                    logger.exception("ランキング報酬の配布に失敗しました guild=%s period=%s",
                                     guild_id, period)
        return total

    # ==================================================================
    # Kyash 請求リンク (Bot が発行 → 利用者が支払う → 自動で反映)
    # ==================================================================
    async def start_claim_charge(
        self, guild_id: int, user_id: int, raw_amount: str
    ) -> dict[str, Any]:
        """請求リンクを発行して支払いを待つ取引を作る。

        Bot が金額を指定して発行するため、送金リンク方式と違い
        **金額不一致が原理的に起きない**。管理者の承認も不要。

        Returns:
            表示に必要な情報 (tx_id / url / amount / rate / 期限 など)。
        """
        settings = await self.ensure_usable_guild(guild_id)
        # 入力検証を先に行い、書式エラーでレート制限を消費しない
        amount = utils.parse_user_amount(raw_amount)
        if amount is None:
            raise ChargeError(config.ErrorCode.INVALID_AMOUNT)
        if not self._charge_rate_limiter.check(f"claim:{guild_id}:{user_id}"):
            raise ChargeError(config.ErrorCode.RATE_LIMITED)
        # 請求リンク方式でも Kyash の受取用アカウントは必要
        await self.preflight(guild_id, user_id, settings)
        provider = config.ChargeProvider.KYASH_CLAIM
        row = await self.db.get_provider_settings(guild_id, provider)
        if row is not None and not row["enabled"]:
            raise ChargeError(config.ErrorCode.PROVIDER_DISABLED)
        charge_rate, role_id = await self.resolve_provider_rate(
            guild_id, user_id, provider, settings
        )
        low, high = await self.provider_limits(guild_id, provider, settings)
        await self._check_limits(
            guild_id, user_id, amount, settings,
            charge_rate=charge_rate, minimum=low, maximum=high,
        )
        async with self._user_locks.acquire(f"charge:{guild_id}:{user_id}"):
            active = await self.db.count_active_transactions(guild_id, user_id)
            if active > 0:
                raise ChargeError(config.ErrorCode.ACTIVE_TRANSACTION_EXISTS)
            # 請求リンクを発行するアカウントを先に決める
            try:
                account = self.kyash.pick_slot(amount)
            except kyash_service.NoCapacityError as exc:
                raise ChargeError(config.ErrorCode.NO_KYASH_CAPACITY, str(exc)) from exc
            wallet_before: int | None = None
            try:
                wallet = await self.kyash.get_wallet(account.id)
                wallet_before = wallet.all_balance
            except kyash_service.KyashServiceError as exc:
                # 残高が取れなくても履歴照合で確認できるため続行する
                logger.info("請求リンク発行前の残高取得に失敗しました: %s", exc)
            try:
                claim = await self.kyash.create_claim_link(amount, account_id=account.id)
            except kyash_service.KyashServiceError as exc:
                logger.warning("請求リンクの発行に失敗しました guild=%s user=%s: %s",
                               guild_id, user_id, exc)
                raise ChargeError(
                    config.ErrorCode.CLAIM_LINK_FAILED, utils.safe_error_text(exc)
                ) from exc
            await self.db.ensure_user(guild_id, user_id)
            try:
                tx_id = await self.db.create_claim_transaction(
                    guild_id=guild_id,
                    user_id=user_id,
                    requested_amount=amount,
                    charge_rate=charge_rate,
                    link_hash=utils.link_hash(claim.link_id),
                    link_uuid=claim.link_uuid,
                    claim_link_id=claim.link_id,
                    wallet_before=wallet_before,
                    kyash_account_id=claim.account_id,
                    expires_at=utils.now_ts() + config.CLAIM_WAIT_SECONDS,
                )
            except Exception:
                # 取引を作れなかったリンクは残さない
                await self._safe(
                    self.kyash.cancel_link(claim.link_uuid, account_id=claim.account_id),
                    context="請求リンク破棄",
                )
                raise
        logger.info(
            "請求リンクを発行しました tx=%s guild=%s user=%s amount=%s rate=%s",
            tx_id, guild_id, user_id, amount, charge_rate,
        )
        await self._safe(self.log_event(
            guild_id,
            "🧾 請求リンクを発行しました",
            fields=(
                ("利用者", f"<@{user_id}>", True),
                ("金額", utils.fmt_yen(amount), True),
                ("取引ID", f"`{tx_id}`", True),
                ("適用レート",
                 utils.fmt_rate(charge_rate) + (f" (<@&{role_id}>)" if role_id else ""), True),
            ),
            color=config.Color.WARNING,
        ), context="請求リンクログ")
        return {
            "tx_id": tx_id,
            "amount": amount,
            "charge_rate": charge_rate,
            "role_id": role_id,
            "credited": utils.calc_credited_amount(amount, charge_rate),
            "url": claim.url,
            "expires_at": utils.now_ts() + config.CLAIM_WAIT_SECONDS,
        }

    async def check_claim_payment(self, tx_id: str, *, user_id: int | None = None) -> str:
        """請求リンクの支払いを確認し、確認できたら残高を付与する。

        Args:
            user_id: 指定した場合、その利用者の取引でなければ拒否する。

        Returns:
            ``"CREDITED"`` 付与した / ``"PENDING"`` まだ確認できない /
            ``"DONE"`` 既に処理済み / ``"REVIEW"`` 手動確認へ回した。
        """
        row = await self.db.get_transaction(tx_id)
        if row is None:
            raise ChargeError(config.ErrorCode.REQUEST_NOT_FOUND, "取引が見つかりません")
        if user_id is not None and int(row["user_id"]) != int(user_id):
            raise ChargeError(config.ErrorCode.NOT_ALLOWED, "他人の取引です")
        status = str(row["status"])
        if status == config.TxStatus.COMPLETED:
            return "DONE"
        if status in config.TERMINAL_STATUSES:
            raise ChargeError(
                config.ErrorCode.TRANSACTION_EXPIRED,
                f"この取引は既に終了しています ({config.STATUS_LABELS.get(status, status)})",
            )
        if status != config.TxStatus.WAITING_PAYMENT:
            # 既に受取済み → 付与処理だけ進める
            await self.credit_transaction(tx_id)
            return "CREDITED"

        link_uuid = str(row["link_uuid"] or "")
        if not link_uuid:
            await self._to_manual_review(
                tx_id, config.ErrorCode.UNKNOWN_ERROR, "請求リンクの識別子がありません"
            )
            return "REVIEW"

        amount = int(row["requested_amount"])
        # 請求リンクは「残高が増えた」だけでは判定しない。複数の請求リンクが
        # 同時に未払いで残り得るため、他人の支払いを自分のものと誤認しないよう
        # 履歴に自分の link_uuid が現れることを必須にする。
        verification = await self.kyash.verify_receipt(
            link_uuid=link_uuid,
            amount=amount,
            wallet_before=row["wallet_before"],
            allow_wallet_delta=False,
            account_id=row["kyash_account_id"],
        )
        if verification.verdict == kyash_service.Verdict.CONFIRMED:
            return await self._settle_claim(tx_id, amount, verification.detail)
        if verification.verdict == kyash_service.Verdict.NO_EVIDENCE:
            logger.debug("請求リンクの支払いは未確認です tx=%s (%s)", tx_id, verification.detail)
            return "PENDING"
        logger.warning("請求リンクの支払い確認ができません tx=%s: %s",
                       tx_id, verification.detail)
        return "PENDING"

    async def _settle_claim(self, tx_id: str, amount: int, detail: str) -> str:
        """支払い確認済みの請求リンク取引を受取済みにして残高を付与する。"""
        try:
            await self.db.mark_claim_paid(tx_id, amount)
        except IllegalStateTransition as exc:
            # 自動確認と手動確認が同時に走った場合。付与自体は冪等。
            logger.info("請求リンクの受取記録をスキップしました tx=%s: %s", tx_id, exc)
            fresh = await self.db.get_transaction(tx_id)
            if fresh is not None and str(fresh["status"]) == config.TxStatus.COMPLETED:
                return "DONE"
        logger.info("請求リンクの支払いを確認しました tx=%s (%s)", tx_id, detail)
        # 支払い済みのリンクは再利用させない (repeatable な請求リンクのため)
        row = await self.db.get_transaction(tx_id)
        if row is not None and row["link_uuid"]:
            await self._safe(
                self.kyash.cancel_link(
                    str(row["link_uuid"]), account_id=row["kyash_account_id"]
                ),
                context="請求リンク無効化",
            )
        await self.credit_transaction(tx_id)
        return "CREDITED"

    async def check_waiting_payments(self) -> int:
        """支払い待ちの請求リンクをまとめて確認する (バックグラウンド)。

        履歴の取得は 1 回だけ行い、その結果を全件の突合に使うことで
        Kyash への問い合わせ回数を抑える。

        Returns:
            残高を付与した件数。
        """
        rows = await self.db.list_waiting_payments(limit=50)
        if not rows:
            return 0
        if not self.kyash.is_usable:
            return 0
        # 履歴の取得はアカウントごとに1回だけ行う (問い合わせ回数を抑える)
        histories: dict[int | None, Any] = {}
        for account_id in {row["kyash_account_id"] for row in rows}:
            try:
                histories[account_id] = await self.kyash.get_history(
                    config.KYASH_HISTORY_LIMIT, account_id=account_id
                )
            except kyash_service.KyashServiceError as exc:
                logger.info("請求リンクの自動確認をスキップしました (履歴取得失敗): %s", exc)
        if not histories:
            return 0
        credited = 0
        for row in rows:
            tx_id = str(row["id"])
            link_uuid = str(row["link_uuid"] or "")
            if not link_uuid:
                continue
            timelines = histories.get(row["kyash_account_id"])
            if timelines is None:
                continue
            if not utils.json_contains_text(timelines, link_uuid):
                continue
            try:
                if await self._settle_claim(
                    tx_id, int(row["requested_amount"]), "履歴にリンク識別子を確認 (自動)"
                ) == "CREDITED":
                    credited += 1
            except Exception:  # noqa: BLE001
                logger.exception("請求リンクの自動付与に失敗しました tx=%s", tx_id)
        if credited:
            logger.info("請求リンクの支払いを %d 件自動で反映しました", credited)
        return credited

    async def expire_claim_transactions(self) -> int:
        """期限切れの請求リンク取引を閉じ、リンクを無効化する。"""
        rows = await self.db.list_waiting_payments(limit=100)
        now = utils.now_ts()
        expired = 0
        for row in rows:
            if not row["expires_at"] or int(row["expires_at"]) > now:
                continue
            tx_id = str(row["id"])
            # 期限切れの直前に支払われている可能性があるため、最後に一度確認する
            try:
                result = await self.check_claim_payment(tx_id)
                if result in ("CREDITED", "DONE"):
                    continue
            except ChargeError as exc:
                # 期限切れ処理は続行する (確認できないことは異常ではない)
                logger.debug("期限切れ直前の支払い確認に失敗しました tx=%s: %s", tx_id, exc.code)
            if row["link_uuid"]:
                await self._safe(
                    self.kyash.cancel_link(
                        str(row["link_uuid"]), account_id=row["kyash_account_id"]
                    ),
                    context="請求リンク無効化",
                )
            try:
                await self.db.transition_status(
                    tx_id, config.TxStatus.EXPIRED,
                    expected=(config.TxStatus.WAITING_PAYMENT,),
                    error_code=config.ErrorCode.TRANSACTION_EXPIRED,
                    error_message="請求リンクの支払い期限を過ぎました",
                )
            except IllegalStateTransition as exc:
                logger.info("請求リンクの期限処理をスキップしました tx=%s: %s", tx_id, exc)
                continue
            expired += 1
            await self._safe(self.notify_result(tx_id), context="期限切れDM")
        if expired:
            logger.info("期限切れの請求リンクを %d 件処理しました", expired)
        return expired

    async def cancel_claim_transaction(self, tx_id: str, user_id: int) -> None:
        """利用者が支払い前の請求リンクを取り消す。"""
        row = await self.db.get_transaction(tx_id)
        if row is None:
            raise ChargeError(config.ErrorCode.REQUEST_NOT_FOUND, "取引が見つかりません")
        if int(row["user_id"]) != int(user_id):
            raise ChargeError(config.ErrorCode.NOT_ALLOWED, "他人の取引です")
        if str(row["status"]) != config.TxStatus.WAITING_PAYMENT:
            raise ChargeError(
                config.ErrorCode.REQUEST_ALREADY_HANDLED,
                f"取り消せない状態です ({config.STATUS_LABELS.get(str(row['status']))})",
            )
        if row["link_uuid"]:
            await self._safe(
                self.kyash.cancel_link(
                    str(row["link_uuid"]), account_id=row["kyash_account_id"]
                ),
                context="請求リンク無効化",
            )
        await self.db.transition_status(
            tx_id, config.TxStatus.CANCELLED,
            expected=(config.TxStatus.WAITING_PAYMENT,),
            error_message="利用者が取り消しました",
        )
        logger.info("請求リンクを取り消しました tx=%s user=%s", tx_id, user_id)

    # ==================================================================
    # チャージ方式 (PayPay / LTC: 申請 → 管理者承認)
    # ==================================================================
    #: 審査チャンネル (全サーバー共通) を保存する system_settings のキー
    REVIEW_CHANNEL_KEY = "request_review_channel_id"
    #: サーバー管理者にも承認を許可するサーバー ID の一覧 (カンマ区切り)
    DELEGATED_GUILDS_KEY = "request_review_delegated_guilds"

    async def get_review_channel_id(self) -> int | None:
        """申請の審査カードを投稿するチャンネル (Bot Owner が設定)。"""
        raw = await self.db.get_system_value(self.REVIEW_CHANNEL_KEY)
        if raw and raw.isdigit():
            return int(raw)
        return None

    async def set_review_channel_id(self, channel_id: int | None) -> None:
        await self.db.set_system_value(
            self.REVIEW_CHANNEL_KEY, str(int(channel_id)) if channel_id else ""
        )
        # 審査チャンネルの有無で PayPay / LTC の利用可否が変わる
        self.invalidate_panel_view()

    async def get_delegated_guilds(self) -> set[int]:
        """管理者にも承認を許可したサーバー。

        入金先は Bot Owner のものなので、承認の既定は Owner だけ。
        信頼できるサーバーにだけ Owner が明示的に委任する。
        """
        raw = await self.db.get_system_value(self.DELEGATED_GUILDS_KEY) or ""
        return {int(x) for x in raw.split(",") if x.strip().isdigit()}

    async def set_delegated(self, guild_id: int, enabled: bool) -> set[int]:
        current = await self.get_delegated_guilds()
        if enabled:
            current.add(int(guild_id))
        else:
            current.discard(int(guild_id))
        await self.db.set_system_value(
            self.DELEGATED_GUILDS_KEY, ",".join(str(x) for x in sorted(current))
        )
        return current

    async def can_review(self, guild_id: int, user: discord.abc.User) -> bool:
        """その利用者が申請を承認できるか。"""
        if self.bot.is_bot_owner(user):
            return True
        return int(guild_id) in await self.get_delegated_guilds()

    async def resolve_provider_rate(
        self, guild_id: int, user_id: int, provider: str, settings: GuildSettings
    ) -> tuple[Decimal, int | None]:
        """方式とロールの両方を見てチャージ率を決める。

        方式別レートがサーバー既定を置き換え、ロール別レート (VIP 等) が
        あればその**高い方**を採用する。こうすることで

        * PayPay を低めに設定しても、その設定が効く
        * LTC を高めに設定したとき、VIP がそれより低くならない

        の両方が成り立つ。採用したレートは申請作成時に保存するため、
        後から設定を変えても既存の申請には影響しない。

        Returns:
            ``(charge_rate, role_id or None)``。role_id はロール別レートを
            採用した場合のみ。
        """
        role_rate, role_id = await self.resolve_charge_rate(guild_id, user_id, settings)
        row = await self.db.get_provider_settings(guild_id, provider)
        provider_rate = utils.to_decimal(row["charge_rate"]) if row else None
        if provider_rate is None:
            base, used_role = role_rate, role_id
        elif role_id is None:
            base, used_role = provider_rate, None
        elif role_rate >= provider_rate:
            base, used_role = role_rate, role_id
        else:
            base, used_role = provider_rate, None
        # ブーストは方式・ロールのどちらが採用されても最後に加算する
        boosted, _bonus = await self.apply_rate_boost(guild_id, user_id, base)
        return boosted, used_role

    async def provider_limits(
        self, guild_id: int, provider: str, settings: GuildSettings
    ) -> tuple[int, int]:
        """方式ごとの金額上下限 (未設定ならサーバー設定)。"""
        row = await self.db.get_provider_settings(guild_id, provider)
        low = settings.minimum_charge
        high = settings.maximum_charge
        if row is not None:
            if row["minimum_charge"] is not None:
                low = int(row["minimum_charge"])
            if row["maximum_charge"] is not None:
                high = int(row["maximum_charge"])
        return low, max(low, high)

    async def provider_availability(
        self, guild_id: int, settings: GuildSettings | None = None
    ) -> list[dict[str, Any]]:
        """各方式が使えるかどうかと、使えない理由を返す。

        利用者に選択肢を出す前に判定するため、「押したのに使えない」を防ぐ。
        """
        settings = settings or await self.db.get_settings(guild_id)
        rows = await self.db.list_provider_settings(guild_id)
        destinations = await self.db.list_destinations()
        review_channel = await self.get_review_channel_id()
        result: list[dict[str, Any]] = []
        # Kyash はサーバー設定でどちらか一方だけを見せる
        kyash_provider = config.KYASH_MODE_PROVIDER.get(
            settings.kyash_mode, config.ChargeProvider.KYASH
        )
        for provider in config.ALL_PROVIDERS:
            row = rows.get(provider)
            enabled = True if row is None else bool(row["enabled"])
            reason: str | None = None
            code: str | None = None
            if provider in config.KYASH_PROVIDERS and provider != kyash_provider:
                # 選ばれていない方式は、利用者に見せない (選択肢から外す)
                continue
            if not enabled:
                reason = "管理者が停止しています"
                code = config.ErrorCode.PROVIDER_DISABLED
            elif provider in config.KYASH_PROVIDERS:
                # 送金リンク・請求リンクのどちらも受取用 Kyash アカウントが必要
                if not self.kyash.is_usable:
                    reason = "受取用アカウントの準備中です"
                    code = config.ErrorCode.KYASH_UNAVAILABLE
                elif self.kyash.wallet_limit_reached:
                    reason = "受取用アカウントの残高上限に達しています"
                    code = config.ErrorCode.WALLET_LIMIT
            else:
                destination = destinations.get(provider)
                if destination is None:
                    reason = "入金先が未登録です"
                    code = config.ErrorCode.PROVIDER_NOT_CONFIGURED
                elif (
                    provider == config.ChargeProvider.PAYPAY
                    and settings.paypay_mode == config.PayPayMode.CLAIM_LINK
                    and not (destination["claim_url"] or "").strip()
                ):
                    # 請求リンク方式なのにリンクが未登録なら、押させない
                    reason = "PayPay の請求リンクが未登録です"
                    code = config.ErrorCode.PROVIDER_NOT_CONFIGURED
                elif review_channel is None:
                    reason = "審査チャンネルが未設定です"
                    code = config.ErrorCode.REVIEW_CHANNEL_NOT_SET
            low, high = await self.provider_limits(guild_id, provider, settings)
            rate, _role = await self.resolve_provider_rate(guild_id, 0, provider, settings)
            result.append({
                "provider": provider,
                "available": reason is None,
                "reason": reason,
                "error_code": code,
                "minimum": low,
                "maximum": high,
                "rate": rate,
                "destination": destinations.get(provider),
            })
        return result

    async def usable_providers(
        self, guild_id: int, settings: GuildSettings | None = None
    ) -> list[dict[str, Any]]:
        return [p for p in await self.provider_availability(guild_id, settings) if p["available"]]

    # ==================================================================
    # パネル表示用のキャッシュ (Discord の3秒制限を確実に守るため)
    # ==================================================================
    def cached_panel_view(self, guild_id: int) -> dict[str, Any] | None:
        """ボタン押下の判断に必要な情報を、DB を触らずに返す。

        Discord はボタン操作に **3秒以内**の応答を求める。DB は単一スレッドで
        直列化しているため、重い処理と重なると応答前の問い合わせが待たされ、
        「アプリケーションは応答しませんでした」となって操作が無効になる。

        そこで「方式の一覧」「設定」を短時間キャッシュし、ボタンの判断を
        **await ゼロ**で行えるようにする。キャッシュが無い・古い場合は None を
        返し、呼び出し側は先に defer してから作り直す (必ず3秒以内に応答する)。

        Returns:
            有効なキャッシュ、無ければ None。
        """
        entry = self._panel_cache.get(guild_id)
        if entry is None:
            return None
        expires_at, snapshot = entry
        if time.monotonic() >= expires_at:
            return None
        return snapshot

    async def refresh_panel_view(self, guild_id: int) -> dict[str, Any]:
        """パネル表示用の情報を作り直してキャッシュする。"""
        settings = await self.db.get_settings(guild_id)
        entries = await self.provider_availability(guild_id, settings)
        snapshot = {
            "settings": settings,
            "entries": entries,
            "usable": [e for e in entries if e["available"]],
            "built_at": utils.now_ts(),
        }
        self._panel_cache[guild_id] = (
            time.monotonic() + config.PANEL_CACHE_TTL, snapshot
        )
        return snapshot

    def invalidate_panel_view(self, guild_id: int | None = None) -> None:
        """設定が変わったときにキャッシュを捨てる。

        ``guild_id`` を省略すると全サーバーぶんを捨てる (入金先や審査チャンネルの
        ような Bot 全体の設定を変えたとき用)。
        """
        if guild_id is None:
            self._panel_cache.clear()
        else:
            self._panel_cache.pop(guild_id, None)

    async def panel_view(self, guild_id: int) -> dict[str, Any]:
        """キャッシュがあれば使い、無ければ作る。"""
        cached = self.cached_panel_view(guild_id)
        if cached is not None:
            return cached
        return await self.refresh_panel_view(guild_id)

    async def warm_panel_views(self) -> int:
        """よく使うサーバーのキャッシュを事前に温めておく。

        利用者が最初に押した1回だけ遅くなるのを避けるため、定期タスクから呼ぶ。
        """
        warmed = 0
        try:
            guild_ids = await self.db.list_guilds_with_settings()
        except Exception:  # noqa: BLE001
            logger.exception("パネルキャッシュの対象取得に失敗しました")
            return 0
        for guild_id in guild_ids:
            if not await self.db.is_guild_allowed(guild_id):
                continue
            try:
                await self.refresh_panel_view(guild_id)
                warmed += 1
            except Exception:  # noqa: BLE001
                logger.exception("パネルキャッシュの更新に失敗しました guild=%s", guild_id)
        return warmed


    async def _ensure_provider_usable(
        self, guild_id: int, provider: str, settings: GuildSettings
    ) -> sqlite3.Row:
        """方式が使える状態か確認し、入金先を返す。"""
        row = await self.db.get_provider_settings(guild_id, provider)
        if row is not None and not row["enabled"]:
            raise ChargeError(config.ErrorCode.PROVIDER_DISABLED)
        destination = await self.db.get_destination(provider)
        if destination is None:
            raise ChargeError(
                config.ErrorCode.PROVIDER_NOT_CONFIGURED, f"{provider} の入金先が未登録です"
            )
        if (
            provider == config.ChargeProvider.PAYPAY
            and settings.paypay_mode == config.PayPayMode.CLAIM_LINK
            and not str(destination["claim_url"] or "").strip()
        ):
            raise ChargeError(
                config.ErrorCode.PROVIDER_NOT_CONFIGURED,
                "PayPay の請求リンクが未登録です",
            )
        if await self.get_review_channel_id() is None:
            raise ChargeError(
                config.ErrorCode.REVIEW_CHANNEL_NOT_SET, "審査チャンネルが未設定です"
            )
        return destination

    async def start_manual_charge(
        self, guild_id: int, user_id: int, provider: str, raw_amount: str
    ) -> dict[str, Any]:
        """PayPay / LTC の申請を作る (入金先の案内まで)。

        LTC はここで価格を取得して確定させる。価格が信用できない場合は
        推測せず、チャージを拒否する。

        Returns:
            案内表示に必要な情報 (request_id / 入金先 / 送金額 / 期限 など)。
        """
        if provider not in config.MANUAL_PROVIDERS:
            raise ChargeError(config.ErrorCode.UNKNOWN_ERROR, f"承認制ではない方式です: {provider}")
        settings = await self.ensure_usable_guild(guild_id)
        # 入力検証を先に行い、書式エラーでレート制限を消費しない
        amount = utils.parse_user_amount(raw_amount)
        if amount is None:
            raise ChargeError(config.ErrorCode.INVALID_AMOUNT)
        # 価格 API を叩く前に必ずレート制限をかける (外部 API を守る)
        if not self._charge_rate_limiter.check(f"req:{guild_id}:{user_id}"):
            raise ChargeError(config.ErrorCode.RATE_LIMITED)
        # PayPay / LTC は Kyash を経由しないため、Kyash の状態は要求しない
        await self.preflight(guild_id, user_id, settings, require_kyash=False)
        destination = await self._ensure_provider_usable(guild_id, provider, settings)
        charge_rate, role_id = await self.resolve_provider_rate(
            guild_id, user_id, provider, settings
        )
        low, high = await self.provider_limits(guild_id, provider, settings)
        # Kyash のウォレット残量は関係しないため確認しない
        await self._check_limits(
            guild_id, user_id, amount, settings,
            charge_rate=charge_rate, minimum=low, maximum=high, check_wallet=False,
        )
        asset_amount: Decimal | None = None
        quote: price_service.PriceQuote | None = None
        if provider == config.ChargeProvider.LTC:
            try:
                quote = await self.price.get_price()
            except price_service.PriceError as exc:
                logger.warning("LTC 価格が使えないため申請を拒否しました: %s",
                               utils.sanitize_for_log(exc.detail, limit=200))
                raise ChargeError(config.ErrorCode.PRICE_UNAVAILABLE, exc.detail) from exc
            asset_amount = utils.asset_amount_for(amount, quote.price)
            if asset_amount < Decimal(config.LTC_MIN_AMOUNT):
                raise ChargeError(
                    config.ErrorCode.ASSET_AMOUNT_TOO_SMALL,
                    f"{utils.fmt_asset(asset_amount)} < 最低 {config.LTC_MIN_AMOUNT} LTC",
                )

        estimated = utils.calc_credited_amount(amount, charge_rate)
        await self.db.ensure_user(guild_id, user_id)
        try:
            request_id = await self.db.create_request(
                guild_id=guild_id,
                user_id=user_id,
                provider=provider,
                requested_amount=amount,
                charge_rate=charge_rate,
                role_id=role_id,
                estimated_credit=estimated,
                asset_amount=asset_amount,
                asset_price=quote.price if quote else None,
                price_source=quote.source if quote else None,
                price_fetched_at=quote.fetched_at if quote else None,
                destination=str(destination["address"]),
            )
        except RequestError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        self.metrics["requests_created"] += 1
        logger.info(
            "チャージ申請を作成しました request=%s guild=%s user=%s provider=%s "
            "amount=%s rate=%s asset=%s",
            request_id, guild_id, user_id, provider, amount, charge_rate,
            asset_amount if asset_amount is not None else "-",
        )
        return {
            "request_id": request_id,
            "provider": provider,
            "amount": amount,
            "charge_rate": charge_rate,
            "role_id": role_id,
            "estimated_credit": estimated,
            "asset_amount": asset_amount,
            "asset_price": quote.price if quote else None,
            "price_source": quote.source if quote else None,
            "price_stale": bool(quote.stale) if quote else False,
            "destination": destination,
            # 表示の仕方 (ID を見せるか請求リンクを見せるか) を UI へ渡す
            "paypay_mode": settings.paypay_mode,
            "claim_url": (
                str(destination["claim_url"] or "")
                if provider == config.ChargeProvider.PAYPAY
                and settings.paypay_mode == config.PayPayMode.CLAIM_LINK
                else ""
            ),
            "quote_expires_at": utils.now_ts() + config.QUOTE_WAIT_SECONDS,
        }

    async def submit_request(
        self, request_id: int, user_id: int, raw_proof: str, raw_asset: str | None = None
    ) -> dict[str, Any]:
        """証拠を登録して承認待ちにし、審査チャンネルへカードを投稿する。"""
        request = await self.db.get_request(request_id)
        if request is None:
            raise ChargeError(config.ErrorCode.REQUEST_NOT_FOUND)
        provider = str(request["provider"])
        proof_ref = (
            utils.normalize_txid(raw_proof)
            if provider == config.ChargeProvider.LTC
            else utils.normalize_payment_ref(raw_proof)
        )
        if proof_ref is None:
            raise ChargeError(
                config.ErrorCode.INVALID_PROOF,
                f"{config.PROVIDER_PROOF_LABELS.get(provider, '証拠')} の形式が不正です",
            )
        asset_amount: Decimal | None = None
        if provider == config.ChargeProvider.LTC and raw_asset:
            asset_amount = utils.parse_asset_amount(raw_asset)
            if asset_amount is None:
                raise ChargeError(
                    config.ErrorCode.INVALID_PROOF, "送金した LTC 数量の形式が不正です"
                )
        try:
            row = await self.db.submit_request_proof(
                request_id,
                user_id=user_id,
                proof_ref=proof_ref,
                proof_hash=utils.proof_hash(provider, proof_ref),
                proof_note=None,
                asset_amount=asset_amount,
            )
        except RequestError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        self.metrics["requests_submitted"] += 1
        logger.info("チャージ申請を受け付けました request=%s provider=%s", request_id, provider)
        await self._safe(self.post_review_card(request_id), context="審査カード投稿")
        await self._safe(self.log_event(
            int(row["guild_id"]),
            f"{config.PROVIDER_EMOJI.get(provider, '💠')} "
            f"{config.PROVIDER_LABELS.get(provider, provider)} のチャージ申請",
            fields=(
                ("申請ID", f"`#{request_id}`", True),
                ("利用者", f"<@{int(row['user_id'])}>", True),
                ("申請額", utils.fmt_yen(int(row["requested_amount"])), True),
                ("付与予定", utils.fmt_int(int(row["estimated_credit"])), True),
                (config.PROVIDER_PROOF_LABELS.get(provider, "証拠"),
                 f"`{utils.truncate(proof_ref, 80)}`", False),
            ),
            color=config.Color.WARNING,
        ), context="申請ログ")
        pending = await self.db.count_requests_by_status()
        return {
            "request_id": request_id,
            "provider": provider,
            "amount": int(row["requested_amount"]),
            "estimated_credit": int(row["estimated_credit"]),
            "proof_ref": proof_ref,
            "pending_total": pending.get(config.RequestStatus.PENDING, 0),
            "expires_at": row["expires_at"],
        }

    async def approve_request(
        self,
        request_id: int,
        *,
        operator_id: int,
        credited_amount: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """申請を承認し、Kyash と同じ経路で残高を付与する。

        ``claim_request_for_review`` が単一トランザクションで状態を
        遷移させるため、2人が同時に承認しても一度しか付与されない。
        """
        try:
            request = await self.db.claim_request_for_review(
                request_id, status=config.RequestStatus.APPROVED, operator_id=operator_id
            )
        except RequestError as exc:
            raise ChargeError(exc.code, exc.detail) from exc

        guild_id = int(request["guild_id"])
        user_id = int(request["user_id"])
        provider = str(request["provider"])
        amount = int(request["requested_amount"])
        charge_rate = utils.to_decimal(request["charge_rate"]) or Decimal(
            config.DEFAULT_CHARGE_RATE
        )
        credited = (
            int(credited_amount)
            if credited_amount is not None
            else int(request["estimated_credit"])
        )
        source = config.PROVIDER_TX_SOURCE[provider]
        tx_id = await self.db.create_manual_transaction(
            guild_id=guild_id, user_id=user_id, requested_amount=amount,
            received_amount=amount, charge_rate=charge_rate, source=source,
        )
        result = await self.db.credit_transaction(
            tx_id, credited,
            change_type=config.BalanceChangeType.CHARGE,
            reason=f"{config.PROVIDER_LABELS.get(provider, provider)} 承認 (申請 #{request_id})",
            operator_id=operator_id,
        )
        op_id = await self.db.add_audit_log(
            actor_id=operator_id,
            action="REQUEST_APPROVE",
            guild_id=guild_id,
            target_user_id=user_id,
            detail={
                "request_id": request_id,
                "provider": provider,
                "transaction_id": tx_id,
                "requested_amount": amount,
                "charge_rate": str(charge_rate),
                "credited_amount": credited,
                "estimated_credit": int(request["estimated_credit"]),
                "overridden": credited_amount is not None,
                "asset_amount": request["asset_amount"],
                "asset_price": request["asset_price"],
                "proof_ref": request["proof_ref"],
                "balance_before": result["balance_before"],
                "balance_after": result["balance_after"],
                "note": utils.truncate(note or "", 300),
            },
        )
        await self.db.finalize_request(
            request_id, credited_amount=credited, transaction_id=tx_id, operation_id=op_id
        )
        self.metrics["requests_approved"] += 1
        self.metrics["charges_completed"] += 1
        self.request_ranking_refresh(guild_id)
        await self._safe(self.post_achievement(tx_id), context="実績投稿")
        await self._safe(self.notify_result(tx_id), context="DM通知")
        await self._safe(self.update_review_card(request_id), context="審査カード更新")
        # 手動承認でも「チャージ完了」として招待報酬の確定判定を回す
        await self._safe(
            self.confirm_invite_after_charge(guild_id, user_id), context="招待確定"
        )
        await self._safe(self.check_tiers(guild_id, user_id), context="段位判定")
        await self._safe(self.log_event(
            guild_id,
            "🟢 チャージ申請を承認しました",
            fields=(
                ("申請ID", f"`#{request_id}`", True),
                ("方式", config.PROVIDER_LABELS.get(provider, provider), True),
                ("利用者", f"<@{user_id}>", True),
                ("承認者", f"<@{operator_id}>", True),
                ("送金額", utils.fmt_yen(amount), True),
                ("付与", utils.fmt_int(credited)
                 + (" (修正済み)" if credited_amount is not None else ""), True),
                ("取引ID", f"`{tx_id}`", True),
                ("操作ID", f"`{op_id}`", True),
            ),
            color=config.Color.SUCCESS,
        ), context="承認ログ")
        logger.info(
            "チャージ申請を承認しました request=%s tx=%s credited=%s operator=%s",
            request_id, tx_id, credited, operator_id,
        )
        return {
            "request_id": request_id,
            "transaction_id": tx_id,
            "operation_id": op_id,
            "credited_amount": credited,
            "provider": provider,
            "user_id": user_id,
            "guild_id": guild_id,
            **result,
        }

    async def reject_request(
        self, request_id: int, *, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """申請を却下する (残高は一切動かさない)。"""
        reason = utils.truncate(reason.strip() or "理由の記載なし", 400)
        try:
            request = await self.db.claim_request_for_review(
                request_id, status=config.RequestStatus.REJECTED, operator_id=operator_id
            )
        except RequestError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        guild_id = int(request["guild_id"])
        user_id = int(request["user_id"])
        provider = str(request["provider"])
        op_id = await self.db.add_audit_log(
            actor_id=operator_id,
            action="REQUEST_REJECT",
            guild_id=guild_id,
            target_user_id=user_id,
            detail={
                "request_id": request_id,
                "provider": provider,
                "requested_amount": int(request["requested_amount"]),
                "proof_ref": request["proof_ref"],
                "reason": reason,
            },
        )
        await self.db.finalize_request(request_id, operation_id=op_id, reject_reason=reason)
        self.metrics["requests_rejected"] += 1
        await self._safe(self.update_review_card(request_id), context="審査カード更新")
        await self._safe(self._dm_request_result(request_id), context="却下DM")
        await self._safe(self.log_event(
            guild_id,
            "🔴 チャージ申請を却下しました",
            fields=(
                ("申請ID", f"`#{request_id}`", True),
                ("方式", config.PROVIDER_LABELS.get(provider, provider), True),
                ("利用者", f"<@{user_id}>", True),
                ("却下者", f"<@{operator_id}>", True),
                ("申請額", utils.fmt_yen(int(request["requested_amount"])), True),
                ("操作ID", f"`{op_id}`", True),
                ("理由", reason, False),
            ),
            color=config.Color.DANGER,
        ), context="却下ログ")
        logger.info("チャージ申請を却下しました request=%s operator=%s", request_id, operator_id)
        return {"request_id": request_id, "operation_id": op_id, "reason": reason}

    async def cancel_own_request(self, request_id: int, user_id: int) -> dict[str, Any]:
        """利用者が自分の申請を取り消す。"""
        try:
            row = await self.db.cancel_request(request_id, user_id=user_id)
        except RequestError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        await self._safe(self.update_review_card(request_id), context="審査カード更新")
        logger.info("チャージ申請を取り消しました request=%s user=%s", request_id, user_id)
        return {"request_id": request_id, "provider": str(row["provider"])}

    async def expire_stale_requests(self) -> int:
        """期限切れの申請を処理し、利用者へ通知する。"""
        rows = await self.db.expire_requests()
        for row in rows:
            request_id = int(row["id"])
            self.metrics["requests_expired"] += 1
            await self._safe(self.update_review_card(request_id), context="審査カード更新")
            if row["status"] == config.RequestStatus.PENDING:
                # 申請済みで放置されたものだけ通知する (送金前の失効は通知しない)
                await self._safe(self._dm_request_result(request_id), context="期限切れDM")
        if rows:
            logger.info("期限切れのチャージ申請を %d 件処理しました", len(rows))
        return len(rows)

    async def _dm_request_result(self, request_id: int) -> None:
        """却下・期限切れを利用者へ DM で知らせる。"""
        row = await self.db.get_request(request_id)
        if row is None:
            return
        status = str(row["status"])
        if status not in (config.RequestStatus.REJECTED, config.RequestStatus.EXPIRED):
            return
        embed = ui.request_result_dm_embed(
            guild_name=self.guild_name(int(row["guild_id"])),
            request=row,
        )
        await self._send_dm(int(row["user_id"]), embed, queue_on_failure=True)

    # --- 審査カード -------------------------------------------------
    async def post_review_card(self, request_id: int) -> None:
        """審査チャンネルへ承認/却下ボタン付きのカードを投稿する。"""
        row = await self.db.get_request(request_id)
        if row is None:
            return
        channel_id = await self.get_review_channel_id()
        if channel_id is None:
            logger.warning("審査チャンネルが未設定のためカードを投稿できません request=%s",
                           request_id)
            return
        channel = await self._resolve_channel(
            int(row["guild_id"]), channel_id, self.REVIEW_CHANNEL_KEY
        )
        if channel is None:
            # 審査チャンネルは別サーバーにあり得るため、Bot 全体から解決し直す
            channel = await self._resolve_global_channel(channel_id)
        if channel is None:
            await self._safe(self.bot.alert_owner(
                f"申請 `#{request_id}` の審査カードを投稿できませんでした。"
                "`/provider review_channel` を設定し直してください。"
            ), context="Owner通知")
            return
        pending = await self.db.count_requests_by_status()
        embed = ui.review_card_embed(
            request=row,
            guild_name=self.guild_name(int(row["guild_id"])),
            pending_total=pending.get(config.RequestStatus.PENDING, 0),
        )
        try:
            message = await channel.send(embed=embed, view=ui.ReviewCardView())
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("審査カードの投稿に失敗しました request=%s: %s",
                           request_id, utils.safe_error_text(exc))
            return
        await self.db.set_request_review_message(
            request_id, channel_id=channel.id, message_id=message.id
        )

    async def update_review_card(self, request_id: int) -> None:
        """処理済みの審査カードを更新し、ボタンを外す。"""
        row = await self.db.get_request(request_id)
        if row is None or not row["review_message_id"]:
            return
        channel = await self._resolve_global_channel(int(row["review_channel_id"] or 0))
        if channel is None:
            return
        pending = await self.db.count_requests_by_status()
        embed = ui.review_card_embed(
            request=row,
            guild_name=self.guild_name(int(row["guild_id"])),
            pending_total=pending.get(config.RequestStatus.PENDING, 0),
        )
        done = str(row["status"]) != config.RequestStatus.PENDING
        try:
            message = await channel.fetch_message(int(row["review_message_id"]))
            await message.edit(embed=embed, view=None if done else ui.ReviewCardView())
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("審査カードの更新に失敗しました request=%s: %s",
                           request_id, utils.safe_error_text(exc))

    async def _resolve_global_channel(
        self, channel_id: int
    ) -> discord.TextChannel | discord.Thread | None:
        """サーバーを問わずチャンネルを解決する (審査チャンネル用)。"""
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException,
                    discord.InvalidData, ValueError):
                return None
            except Exception:  # noqa: BLE001
                # 審査カードの解決に失敗しても、承認・却下そのものは続行させる
                logger.warning("審査チャンネルの解決に失敗しました channel=%s",
                               channel_id, exc_info=True)
                return None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        me = channel.guild.me if channel.guild else None
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.send_messages and perms.embed_links):
                logger.warning("審査チャンネルへの送信権限がありません channel=%s", channel_id)
                return None
        return channel

    async def remind_pending_requests(self) -> int:
        """未処理の申請が溜まっていたら Owner へ知らせる。"""
        rows = await self.db.list_pending_requests(limit=25)
        if not rows:
            return 0
        now = utils.now_ts()
        stale = [r for r in rows
                 if now - int(r["submitted_at"] or r["created_at"]) >= config.REVIEW_REMIND_SECONDS]
        if not stale:
            return 0
        lines = [
            f"・`#{int(r['id'])}` {config.PROVIDER_LABELS.get(str(r['provider']), r['provider'])} "
            f"<@{int(r['user_id'])}> {utils.fmt_yen(int(r['requested_amount']))} "
            f"({utils.format_jst(int(r['submitted_at'] or r['created_at']))} から未処理)"
            for r in stale[:10]
        ]
        await self._safe(self.bot.alert_owner(
            f"**チャージ申請が {len(stale)} 件 未処理です**\n"
            + "\n".join(lines)
            + "\n審査チャンネルのボタン、または `/request approve` / `/request reject` "
              "で処理してください。"
        ), context="申請催促")
        return len(stale)

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
            item_type=str(purchase["item_type"] or config.ShopItemType.ROLE),
            subscription=bool(purchase["subscription"]),
            renewal_count=int(purchase["renewal_count"] or 0),
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
        embed = ui.charge_panel_embed(
            settings,
            kyash_ready=self.kyash.is_usable,
            providers=await self.provider_availability(guild_id, settings),
        )
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
        self, member: discord.Member, item_id: int, *,
        item_input: str | None = None, item_color: str | None = None,
    ) -> dict[str, Any]:
        """内部残高で商品を購入する。

        残高の引き落としは単一トランザクションで確定させ、その後に商品タイプごとの
        特典を渡す。特典を渡せなかった場合は自動で返金し、利用者へ明示する。

        Args:
            item_input: 利用者の入力 (カスタムロール名・ニックネーム・チャンネル名)。
                入力が必要なタイプで空の場合はエラーにする。
            item_color: カスタムロールの色 (``#RRGGBB``)。管理者が色を固定している
                場合は無視する。
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
        item_type = str(item["item_type"] or config.ShopItemType.ROLE)
        # 「買えたのに渡せない」を避けるため、引き落とす前に販売可能かを確かめる
        problem = self.shop_item_problem(guild, item)
        if problem:
            raise ChargeError(config.ErrorCode.SHOP_ITEM_UNAVAILABLE, problem)
        if item_type in config.SHOP_TYPES_NEED_INPUT and not (item_input or "").strip():
            raise ChargeError(config.ErrorCode.ITEM_INPUT_INVALID, "入力が必要な商品です")
        duration = int(item["duration_days"])
        if item_type in config.SHOP_TYPES_UNIQUE and duration == 0:
            role = guild.get_role(int(item["role_id"]))
            if role is not None and role in member.roles:
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
                granted = await self._setup_purchase(
                    member, item, result, item_input=item_input, item_color=item_color
                )
            except Exception as exc:  # noqa: BLE001 - 渡せなかったら必ず返金する
                logger.error(
                    "商品の用意に失敗したため返金します purchase=%s type=%s: %s",
                    purchase_id, item_type, utils.safe_error_text(exc),
                )
                # 途中まで作った物 (ロール・チャンネル) が残らないよう片付ける
                stale = await self.db.get_purchase(purchase_id)
                if stale is not None:
                    await self._safe(
                        self._teardown_purchase(
                            stale, reason=f"商品の用意に失敗 #{purchase_id}"),
                        context="失敗した購入の後片付け",
                    )
                try:
                    refund = await self.db.refund_purchase(
                        purchase_id, operator_id=None,
                        reason="商品の用意に失敗したため自動返金",
                        status=config.PurchaseStatus.FAILED,
                    )
                    await self._log_balance_from_history(
                        guild.id, member.id,
                        change_type=config.BalanceChangeType.SPEND_REFUND,
                        fallback=refund, reason="商品の用意に失敗したため自動返金",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("自動返金に失敗しました purchase=%s", purchase_id)
                    await self.alert_admins(
                        guild.id, "ショップの自動返金に失敗",
                        f"購入 `#{purchase_id}` の特典付与と返金の両方に失敗しました。"
                        "手動で `/shop refund` を実行してください。",
                    )
                if isinstance(exc, ChargeError):
                    raise
                raise ChargeError(
                    config.ErrorCode.ROLE_ASSIGN_FAILED
                    if item_type == config.ShopItemType.ROLE
                    else config.ErrorCode.ITEM_SETUP_FAILED
                ) from exc

            await self.db.activate_purchase(purchase_id)

        result.update(granted)
        self.metrics["purchases"] += 1
        await self._safe(self.post_shop_achievement(purchase_id), context="購入実績")
        await self.db.add_audit_log(
            actor_id=member.id, action="SHOP_PURCHASE", guild_id=guild.id,
            target_user_id=member.id,
            detail={
                "purchase_id": purchase_id, "item_id": item_id, "item": item["name"],
                "price": result["price"], "item_type": item_type,
                "role_id": granted.get("role_id"),
                "channel_id": granted.get("channel_id"),
                "detail": granted.get("detail"),
                "subscription": bool(result["subscription"]),
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
                ("種類", config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type)
                 + (" / サブスク" if result["subscription"] else ""), True),
                ("内容", (f"<@&{granted['role_id']}>" if granted.get("role_id")
                          else f"<#{granted['channel_id']}>" if granted.get("channel_id")
                          else str(granted.get("detail") or "-")), True),
                ("期限", utils.format_jst(result["expires_at"]) if result["expires_at"] else "無期限", True),
                ("残高", f"{utils.fmt_int(result['balance_before'])} → "
                         f"{utils.fmt_int(result['balance_after'])}", True),
            ),
            color=config.Color.ACCENT,
        ), context="購入ログ")
        self.request_ranking_refresh(guild.id)
        return result

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
        # ロール以外 (カスタムロール・チャンネル・ニックネーム・ブースト) も片付ける
        await self._teardown_purchase(purchase, reason=f"購入返金 #{purchase_id}")
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
            done = await self._teardown_purchase(
                row, reason=f"購入期限切れ #{purchase_id}"
            )
            item_type = str(row["item_type"] or config.ShopItemType.ROLE)
            await self._safe(self.log_event(
                int(row["guild_id"]), "⌛ 購入した商品の有効期限が切れました",
                fields=(
                    ("対象", f"<@{row['user_id']}>", True),
                    ("商品", str(row["item_name"]), True),
                    ("種類", config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type), True),
                    ("片付け", "、".join(done) if done else "対象なし", False),
                ),
                color=config.Color.NEUTRAL,
            ), context="期限切れログ")
            handled += 1
        if handled:
            logger.info("期限切れの購入 %s 件を処理しました", handled)
        return handled

    # ------------------------------------------------------------------
    # 商品タイプごとの用意 / 後片付け
    # ------------------------------------------------------------------
    def shop_item_problem(self, guild: discord.Guild, item: Any) -> str | None:
        """その商品を今このサーバーで販売できるか (できない理由を返す)。

        「買えたのに付与に失敗」を避けるため、購入前と商品追加時の両方で使う。
        """
        item_type = str(item["item_type"] or config.ShopItemType.ROLE)
        me = guild.me
        if me is None:
            return "Bot の情報を取得できません。"
        if item_type == config.ShopItemType.ROLE:
            role = guild.get_role(int(item["role_id"]))
            if role is None:
                return "商品のロールが存在しません (削除された可能性があります)。"
            return self.role_grant_problem(guild, role)
        if item_type == config.ShopItemType.CUSTOM_ROLE:
            if not me.guild_permissions.manage_roles:
                return "Bot に「ロールの管理」権限がありません。"
            if len(guild.roles) >= config.GUILD_ROLE_SOFT_LIMIT:
                return "サーバーのロール数が上限に近いため、新しいロールを作れません。"
            return None
        if item_type == config.ShopItemType.NICKNAME:
            if not me.guild_permissions.manage_nicknames:
                return "Bot に「ニックネームの管理」権限がありません。"
            return None
        if item_type == config.ShopItemType.PRIVATE_CHANNEL:
            if not me.guild_permissions.manage_channels:
                return "Bot に「チャンネルの管理」権限がありません。"
            payload = utils.load_json_dict(item["payload"])
            category_id = payload.get("category_id")
            if category_id:
                category = guild.get_channel(int(category_id))
                if category is None or not isinstance(category, discord.CategoryChannel):
                    return "作成先のカテゴリが見つかりません。"
            return None
        if item_type == config.ShopItemType.RATE_BOOST:
            payload = utils.load_json_dict(item["payload"])
            bonus = utils.to_decimal(payload.get("bonus_rate"))
            if bonus is None or bonus <= 0:
                return "ブースト量 (bonus_rate) が設定されていません。"
            if int(payload.get("hours") or 0) <= 0:
                return "ブーストの時間 (hours) が設定されていません。"
            return None
        return f"未知の商品タイプです: {item_type}"

    async def _setup_purchase(
        self,
        member: discord.Member,
        item: Any,
        purchase: dict[str, Any],
        *,
        item_input: str | None,
        item_color: str | None = None,
    ) -> dict[str, Any]:
        """商品タイプに応じて購入者へ実際の特典を渡す。

        失敗したら例外を投げる。呼び出し側 (:meth:`purchase_shop_item`) が
        自動返金を行うため、ここでは「渡せたか」だけに集中する。

        Returns:
            表示とログに使う情報 (``role_id`` / ``channel_id`` / ``detail`` 等)。
        """
        guild = member.guild
        item_type = str(purchase["item_type"])
        purchase_id = int(purchase["purchase_id"])
        payload: dict[str, Any] = dict(purchase.get("payload") or {})
        reason = utils.truncate(f"ショップ購入 #{purchase_id} ({item['name']})", 400)

        if item_type == config.ShopItemType.ROLE:
            role = guild.get_role(int(item["role_id"]))
            if role is None:
                raise ChargeError(config.ErrorCode.SHOP_ITEM_UNAVAILABLE,
                                  "商品のロールが存在しません")
            await member.add_roles(role, reason=reason)
            return {"role_id": role.id, "role_name": role.name}

        if item_type == config.ShopItemType.CUSTOM_ROLE:
            name = utils.clean_display_name(item_input, limit=config.CUSTOM_NAME_MAX_LEN)
            if not name:
                raise ChargeError(config.ErrorCode.ITEM_INPUT_INVALID, "ロール名が空です")
            # 管理者が色を固定していればそれを使い、していなければ購入者の指定を使う
            color_value = utils.parse_color(payload.get("color")) if payload.get("color") else None
            if color_value is None and item_color and item_color.strip():
                color_value = utils.parse_color(item_color)
                if color_value is None:
                    raise ChargeError(
                        config.ErrorCode.ITEM_INPUT_INVALID,
                        "色は #RRGGBB の形式で入力してください",
                    )
            role = await guild.create_role(
                name=name,
                colour=discord.Colour(color_value) if color_value is not None
                else discord.Colour.default(),
                hoist=bool(payload.get("hoist")),
                mentionable=False,
                reason=reason,
            )
            # 作成直後に記録する (記録より先に付与すると後片付けできなくなる)
            await self.db.add_purchase_asset(
                purchase_id=purchase_id, guild_id=guild.id, user_id=member.id,
                asset_type=config.PurchaseAssetType.ROLE, asset_id=role.id, detail=name,
            )
            await member.add_roles(role, reason=reason)
            return {"role_id": role.id, "role_name": role.name, "detail": name}

        if item_type == config.ShopItemType.NICKNAME:
            nickname = utils.clean_display_name(item_input, limit=config.NICKNAME_MAX_LEN)
            if not nickname:
                raise ChargeError(config.ErrorCode.ITEM_INPUT_INVALID, "ニックネームが空です")
            previous = member.nick or ""
            # 元に戻せるように、変更前の値を先に保存する
            await self.db.add_purchase_asset(
                purchase_id=purchase_id, guild_id=guild.id, user_id=member.id,
                asset_type=config.PurchaseAssetType.NICKNAME, asset_id=member.id,
                detail=previous,
            )
            await member.edit(nick=nickname, reason=reason)
            return {"detail": nickname, "previous": previous}

        if item_type == config.ShopItemType.RATE_BOOST:
            bonus = utils.to_decimal(payload.get("bonus_rate"))
            hours = int(payload.get("hours") or 0)
            if bonus is None or bonus <= 0 or hours <= 0:
                raise ChargeError(config.ErrorCode.SHOP_ITEM_UNAVAILABLE,
                                  "ブーストの設定が不正です")
            bonus = min(bonus, utils.to_decimal(config.RATE_BOOST_MAX_BONUS) or bonus)
            hours = min(hours, config.RATE_BOOST_MAX_HOURS)
            expires_at = utils.now_ts() + hours * 3600
            boost_id = await self.db.add_rate_boost(
                guild_id=guild.id, user_id=member.id, bonus_rate=str(bonus),
                expires_at=expires_at, source=f"SHOP-{purchase_id}", purchase_id=purchase_id,
            )
            return {"boost_id": boost_id, "bonus_rate": str(bonus), "hours": hours,
                    "boost_expires_at": expires_at,
                    "detail": f"+{utils.fmt_rate(bonus)} / {hours}時間"}

        if item_type == config.ShopItemType.PRIVATE_CHANNEL:
            raw_name = utils.clean_display_name(item_input, limit=config.CUSTOM_NAME_MAX_LEN)
            channel_name = utils.channel_name_from(raw_name or member.display_name)
            if not channel_name:
                raise ChargeError(config.ErrorCode.ITEM_INPUT_INVALID, "チャンネル名が空です")
            category: discord.CategoryChannel | None = None
            category_id = payload.get("category_id")
            if category_id:
                found = guild.get_channel(int(category_id))
                if isinstance(found, discord.CategoryChannel):
                    category = found
            me = guild.me
            overwrites: dict[Any, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True,
                ),
            }
            if me is not None:
                overwrites[me] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_channels=True,
                    read_message_history=True,
                )
            channel = await guild.create_text_channel(
                name=f"{config.PRIVATE_CHANNEL_PREFIX}{channel_name}",
                category=category, overwrites=overwrites, reason=reason,
            )
            await self.db.add_purchase_asset(
                purchase_id=purchase_id, guild_id=guild.id, user_id=member.id,
                asset_type=config.PurchaseAssetType.CHANNEL, asset_id=channel.id,
                detail=channel.name,
            )
            return {"channel_id": channel.id, "detail": channel.name}

        raise ChargeError(config.ErrorCode.SHOP_ITEM_UNAVAILABLE,
                          f"未対応の商品タイプです: {item_type}")

    async def _teardown_purchase(self, purchase: Any, *, reason: str) -> list[str]:
        """購入で渡したものを取り消す (返金・期限切れの共通処理)。

        片付けは「記録済みの作成物」を基準に行うので、同じ購入について
        二重に実行しても副作用は起きない (``removed_at`` で弾く)。

        Returns:
            実際に行った片付けの説明 (ログ用)。
        """
        guild_id = int(purchase["guild_id"])
        user_id = int(purchase["user_id"])
        purchase_id = int(purchase["id"])
        item_type = str(purchase["item_type"] or config.ShopItemType.ROLE)
        done: list[str] = []
        guild = self.bot.get_guild(guild_id)

        if item_type == config.ShopItemType.ROLE:
            if await self._remove_purchase_role(
                guild_id, user_id, int(purchase["role_id"]), reason=reason
            ):
                done.append("ロールを剥奪")
            return done

        if item_type == config.ShopItemType.RATE_BOOST:
            if await self.db.remove_rate_boosts_for_purchase(purchase_id):
                done.append("チャージ率ブーストを無効化")
            return done

        assets = await self.db.list_purchase_assets(purchase_id)
        for asset in assets:
            asset_row_id = int(asset["id"])
            asset_type = str(asset["asset_type"])
            target_id = asset["asset_id"]
            try:
                if asset_type == config.PurchaseAssetType.ROLE and guild is not None:
                    role = guild.get_role(int(target_id)) if target_id else None
                    if role is not None:
                        await role.delete(reason=utils.truncate(reason, 400))
                        done.append(f"作成したロール @{role.name} を削除")
                elif asset_type == config.PurchaseAssetType.CHANNEL and guild is not None:
                    channel = guild.get_channel(int(target_id)) if target_id else None
                    if channel is not None:
                        await channel.delete(reason=utils.truncate(reason, 400))
                        done.append(f"作成したチャンネル #{channel.name} を削除")
                elif asset_type == config.PurchaseAssetType.NICKNAME and guild is not None:
                    member = guild.get_member(user_id)
                    if member is not None:
                        previous = str(asset["detail"] or "") or None
                        await member.edit(nick=previous, reason=utils.truncate(reason, 400))
                        done.append("ニックネームを元に戻した")
            except discord.NotFound:
                done.append("対象は既に存在しませんでした")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "購入物の後片付けに失敗しました purchase=%s asset=%s: %s",
                    purchase_id, asset_row_id, utils.safe_error_text(exc),
                )
                await self.alert_admins(
                    guild_id, "購入物の後片付けに失敗",
                    f"購入 `#{purchase_id}` の {asset_type} (ID: `{target_id}`) を"
                    "自動で片付けられませんでした。手動で削除してください。",
                )
                continue  # 消せていないので removed_at は立てない
            await self.db.mark_asset_removed(asset_row_id)
        return done

    async def apply_rate_boost(
        self, guild_id: int, user_id: int, rate: Decimal
    ) -> tuple[Decimal, Decimal | None]:
        """有効なチャージ率ブーストを反映する。

        複数持っている場合は**最も大きい1つ**だけを適用する (合算しない)。
        合算を許すと、ブーストを買い集めるだけで率を無制限に上げられてしまう。

        Returns:
            ``(適用後の率, 加算したブースト量 or None)``
        """
        try:
            rows = await self.db.list_active_rate_boosts(guild_id, user_id)
        except Exception:  # noqa: BLE001 - 率の解決は失敗しても既定値で続行する
            logger.exception("チャージ率ブーストの取得に失敗しました guild=%s user=%s",
                             guild_id, user_id)
            return rate, None
        best: Decimal | None = None
        for row in rows:
            bonus = utils.to_decimal(row["bonus_rate"])
            if bonus is None or bonus <= 0:
                continue
            if best is None or bonus > best:
                best = bonus
        if best is None:
            return rate, None
        cap = utils.to_decimal(config.RATE_BOOST_MAX_BONUS)
        if cap is not None and best > cap:
            best = cap
        return rate + best, best

    # ------------------------------------------------------------------
    # サブスク (自動更新)
    # ------------------------------------------------------------------
    async def run_subscriptions(self) -> dict[str, int]:
        """サブスクの更新予告と自動更新をまとめて処理する。"""
        result = {"notified": 0, "renewed": 0, "failed": 0}
        try:
            notices = await self.db.list_subscription_notices()
        except Exception:  # noqa: BLE001
            logger.exception("サブスク予告の取得に失敗しました")
            notices = []
        for row in notices:
            purchase_id = int(row["id"])
            # 先に「送った」印を付ける。送信に失敗しても連続通知にはしない。
            if not await self.db.mark_subscription_notified(purchase_id):
                continue
            balance = await self.db.get_balance(int(row["guild_id"]), int(row["user_id"]))
            await self._send_dm(
                int(row["user_id"]),
                ui.subscription_notice_embed(
                    item_name=str(row["item_name"]), price=int(row["price"]),
                    next_charge_at=int(row["next_charge_at"]), balance=balance,
                    purchase_id=purchase_id,
                ),
                queue_on_failure=False,
            )
            result["notified"] += 1

        try:
            due = await self.db.list_subscription_due()
        except Exception:  # noqa: BLE001
            logger.exception("サブスク更新対象の取得に失敗しました")
            return result
        for row in due:
            purchase_id = int(row["id"])
            try:
                outcome = await self.db.renew_subscription(purchase_id)
            except Exception:  # noqa: BLE001
                logger.exception("サブスクの更新に失敗しました purchase=%s", purchase_id)
                continue
            if outcome.get("renewed"):
                result["renewed"] += 1
                await self._after_subscription_renewed(purchase_id, outcome)
            elif outcome.get("reason") == "NOT_DUE":
                continue
            else:
                result["failed"] += 1
                await self._after_subscription_failed(purchase_id, outcome)
        if result["renewed"] or result["failed"] or result["notified"]:
            logger.info(
                "サブスク処理: 予告 %s 件 / 更新 %s 件 / 停止 %s 件",
                result["notified"], result["renewed"], result["failed"],
            )
        return result

    async def _after_subscription_renewed(
        self, purchase_id: int, outcome: dict[str, Any]
    ) -> None:
        """更新に成功したときの通知・ログ。"""
        guild_id = int(outcome["guild_id"])
        user_id = int(outcome["user_id"])
        self.metrics["subscription_renewals"] += 1
        await self._safe(
            self.post_shop_achievement(purchase_id, post_if_missing=False),
            context="サブスク更新の実績更新",
        )
        await self.db.add_audit_log(
            actor_id=config.SYSTEM_ACTOR_ID, action="SUBSCRIPTION_RENEW", guild_id=guild_id,
            target_user_id=user_id,
            detail={"purchase_id": purchase_id, "price": outcome["price"],
                    "renewal_count": outcome["renewal_count"],
                    "expires_at": outcome["expires_at"]},
        )
        await self._log_balance_from_history(
            guild_id, user_id, change_type=config.BalanceChangeType.SUBSCRIPTION,
            fallback=outcome,
            reason=f"サブスク更新 ({outcome['renewal_count']}回目): {outcome['item_name']}",
            transaction_id=f"SHOP-{purchase_id}-R{outcome['renewal_count']}",
        )
        await self._send_dm(
            user_id,
            ui.subscription_renewed_embed(
                item_name=str(outcome["item_name"]), price=int(outcome["price"]),
                balance_after=int(outcome["balance_after"]),
                expires_at=int(outcome["expires_at"]),
                renewal_count=int(outcome["renewal_count"]), purchase_id=purchase_id,
            ),
            queue_on_failure=False,
        )
        await self._safe(self.log_event(
            guild_id, "🔁 サブスクを更新しました",
            fields=(
                ("利用者", f"<@{user_id}>", True),
                ("商品", str(outcome["item_name"]), True),
                ("価格", utils.fmt_int(int(outcome["price"])), True),
                ("回数", f"{outcome['renewal_count']}回目", True),
                ("次回", utils.format_jst(int(outcome["expires_at"])), True),
                ("残高", f"{utils.fmt_int(int(outcome['balance_before']))} → "
                         f"{utils.fmt_int(int(outcome['balance_after']))}", True),
            ),
            color=config.Color.ACCENT,
        ), context="サブスク更新ログ")
        self.request_ranking_refresh(guild_id)

    async def _after_subscription_failed(
        self, purchase_id: int, outcome: dict[str, Any]
    ) -> None:
        """更新できなかったとき (残高不足・商品停止・凍結) の後処理。

        自動更新は解除済みなので、特典はここで直ちに取り消す。
        期限切れタスクを待つと、支払いのない期間が生まれてしまう。
        """
        guild_id = int(outcome.get("guild_id") or 0)
        user_id = int(outcome.get("user_id") or 0)
        reason_code = str(outcome.get("reason") or "UNKNOWN")
        purchase = await self.db.get_purchase(purchase_id)
        if purchase is not None:
            await self.db.mark_purchase_expired(purchase_id)
            await self._teardown_purchase(
                purchase, reason=f"サブスク更新の失敗 #{purchase_id} ({reason_code})"
            )
            await self._safe(
                self.post_shop_achievement(purchase_id, post_if_missing=False),
                context="サブスク終了の実績更新",
            )
        await self.db.add_audit_log(
            actor_id=config.SYSTEM_ACTOR_ID, action="SUBSCRIPTION_STOP", guild_id=guild_id,
            target_user_id=user_id,
            detail={"purchase_id": purchase_id, "reason": reason_code},
        )
        if user_id:
            await self._send_dm(
                user_id,
                ui.subscription_stopped_embed(
                    item_name=str(outcome.get("item_name") or "商品"),
                    reason_code=reason_code,
                    price=int(outcome.get("price") or 0),
                    balance=int(outcome.get("balance") or 0),
                    purchase_id=purchase_id,
                ),
                queue_on_failure=False,
            )
        await self._safe(self.log_event(
            guild_id, "⏹ サブスクを終了しました",
            fields=(
                ("利用者", f"<@{user_id}>", True),
                ("商品", str(outcome.get("item_name") or "-"), True),
                ("理由", config.SUBSCRIPTION_STOP_REASONS.get(reason_code, reason_code), True),
            ),
            color=config.Color.WARNING,
        ), context="サブスク終了ログ")

    async def cancel_subscription(
        self, guild_id: int, purchase_id: int, *, user_id: int | None, operator_id: int
    ) -> dict[str, Any]:
        """自動更新を停止する (期限までは使えるまま残す)。"""
        row = await self.db.cancel_subscription(
            purchase_id, guild_id=guild_id, user_id=user_id
        )
        if row is None:
            raise ChargeError(config.ErrorCode.SUBSCRIPTION_NOT_FOUND)
        await self.db.add_audit_log(
            actor_id=operator_id, action="SUBSCRIPTION_CANCEL", guild_id=guild_id,
            target_user_id=int(row["user_id"]),
            detail={"purchase_id": purchase_id, "item": str(row["item_name"])},
        )
        await self._safe(
            self.post_shop_achievement(purchase_id, post_if_missing=False),
            context="サブスク解約の実績更新",
        )
        await self._safe(self.log_event(
            guild_id, "🚫 サブスクの自動更新を停止",
            fields=(
                ("利用者", f"<@{int(row['user_id'])}>", True),
                ("商品", str(row["item_name"]), True),
                ("期限", utils.format_jst(int(row["expires_at"])) if row["expires_at"]
                 else "無期限", True),
                ("操作者", f"<@{operator_id}>", True),
            ),
            color=config.Color.NEUTRAL,
        ), context="サブスク解約ログ")
        return {
            "purchase_id": purchase_id, "item_name": str(row["item_name"]),
            "expires_at": row["expires_at"], "user_id": int(row["user_id"]),
            "price": int(row["price"]),
        }

    # ==================================================================
    # オークション
    # ==================================================================
    async def create_auction(
        self,
        guild: discord.Guild,
        *,
        name: str,
        role: discord.Role,
        start_price: int,
        min_increment: int,
        hours: float,
        duration_days: int = 0,
        description: str | None = None,
        created_by: int,
    ) -> dict[str, Any]:
        """オークションを開始する。

        入札より先に「本当に景品を渡せるか」を確かめる。渡せないロールで
        開催すると、落札者から預かった残高を返すしかなくなる。
        """
        await self.ensure_usable_guild(guild.id)
        problem = self.role_grant_problem(guild, role)
        if problem:
            raise ChargeError(config.ErrorCode.ROLE_ASSIGN_FAILED, problem)
        open_count = await self.db.count_open_auctions(guild.id)
        if open_count >= config.MAX_OPEN_AUCTIONS:
            raise ChargeError(
                config.ErrorCode.AUCTION_LIMIT_REACHED,
                f"開催中のオークションが {open_count} 件あります",
            )
        seconds = int(hours * 3600)
        if seconds < config.AUCTION_MIN_SECONDS:
            raise ChargeError(
                config.ErrorCode.INVALID_AMOUNT,
                f"開催時間は {config.AUCTION_MIN_SECONDS // 60} 分以上にしてください",
            )
        ends_at = utils.now_ts() + seconds
        auction_id = await self.db.create_auction(
            guild_id=guild.id, name=name, role_id=role.id, start_price=start_price,
            min_increment=min_increment, ends_at=ends_at, duration_days=duration_days,
            description=description, created_by=created_by,
        )
        await self.db.add_audit_log(
            actor_id=created_by, action="AUCTION_CREATE", guild_id=guild.id,
            detail={"auction_id": auction_id, "role_id": role.id,
                    "start_price": start_price, "min_increment": min_increment,
                    "ends_at": ends_at, "duration_days": duration_days},
        )
        await self._safe(self.log_event(
            guild.id, "🔨 オークションを開始しました",
            fields=(
                ("名前", name, True),
                ("景品", role.mention, True),
                ("開始価格", utils.fmt_int(start_price), True),
                ("最低更新額", utils.fmt_int(min_increment), True),
                ("締切", utils.format_jst(ends_at), True),
                ("有効期間", f"{duration_days}日" if duration_days else "無期限", True),
            ),
            color=config.Color.ACCENT,
        ), context="オークション開始ログ")
        return {"auction_id": auction_id, "ends_at": ends_at}

    async def place_bid(
        self, member: discord.Member, auction_id: int, amount: int
    ) -> dict[str, Any]:
        """入札する。押さえた残高は上回られた時点で自動的に返す。"""
        guild = member.guild
        settings = await self.ensure_usable_guild(guild.id)
        if settings.emergency_stop:
            raise ChargeError(config.ErrorCode.EMERGENCY_STOP)
        if await self.db.is_frozen(guild.id, member.id):
            raise ChargeError(config.ErrorCode.USER_FROZEN)
        if amount <= 0 or amount > config.AUCTION_MAX_BID:
            raise ChargeError(config.ErrorCode.INVALID_AMOUNT)
        self.check_button_rate_limit(member.id)
        async with self._user_locks.acquire(f"auction:{guild.id}:{auction_id}"):
            try:
                result = await self.db.place_bid(
                    auction_id=auction_id, guild_id=guild.id, user_id=member.id,
                    amount=amount,
                )
            except AuctionError as exc:
                raise ChargeError(exc.code, exc.detail) from exc
        await self._log_balance_from_history(
            guild.id, member.id, change_type=config.BalanceChangeType.AUCTION_BID,
            fallback=result, reason=f"オークション入札: {result['name']}",
            transaction_id=f"AUC-{auction_id}-B{result['bid_id']}",
        )
        refunded = result.get("refunded")
        if refunded:
            # 上回られた人にはその場で返金しているので、必ず知らせる
            await self._log_balance_from_history(
                guild.id, int(refunded["user_id"]),
                change_type=config.BalanceChangeType.AUCTION_REFUND,
                fallback=refunded,
                reason=f"入札が上回られたため返金: {result['name']}",
                transaction_id=f"AUC-{auction_id}-B{result['bid_id']}",
            )
            await self._send_dm(
                int(refunded["user_id"]),
                ui.auction_outbid_dm_embed(
                    name=str(result["name"]), auction_id=auction_id,
                    your_bid=int(refunded["amount"]), new_bid=amount,
                    balance_after=int(refunded["balance_after"]),
                    ends_at=int(result["ends_at"]),
                ),
                queue_on_failure=False,
            )
        await self._safe(self.log_event(
            guild.id, "💸 オークション入札",
            fields=(
                ("オークション", f"`{auction_id}` {result['name']}", True),
                ("入札者", member.mention, True),
                ("入札額", utils.fmt_int(amount), True),
                ("前の入札者", f"<@{refunded['user_id']}> (返金 "
                              f"{utils.fmt_int(int(refunded['amount']))})"
                 if refunded else "なし", True),
                ("締切", utils.format_jst(int(result["ends_at"]))
                 + (" (延長)" if result["extended"] else ""), True),
                ("残高", f"{utils.fmt_int(int(result['balance_before']))} → "
                         f"{utils.fmt_int(int(result['balance_after']))}", True),
            ),
            color=config.Color.ACCENT,
        ), context="入札ログ")
        self.metrics["bids"] += 1
        await self._safe(self.refresh_auction_panel(auction_id), context="オークションパネル更新")
        self.request_ranking_refresh(guild.id)
        return result

    async def cancel_auction(
        self, guild_id: int, auction_id: int, *, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """オークションを中止して預かり分を返す。"""
        try:
            result = await self.db.cancel_auction(
                auction_id, guild_id=guild_id, operator_id=operator_id, reason=reason
            )
        except AuctionError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        refunded = result.get("refunded")
        await self.db.add_audit_log(
            actor_id=operator_id, action="AUCTION_CANCEL", guild_id=guild_id,
            detail={"auction_id": auction_id, "reason": utils.truncate(reason, 300),
                    "refunded_user": refunded["user_id"] if refunded else None,
                    "refunded_amount": refunded["amount"] if refunded else 0},
        )
        if refunded:
            await self._log_balance_from_history(
                guild_id, int(refunded["user_id"]),
                change_type=config.BalanceChangeType.AUCTION_REFUND,
                fallback=refunded, operator_id=operator_id,
                reason=f"オークション中止による返金: {result['name']}",
                transaction_id=f"AUC-{auction_id}-B{refunded['bid_id']}",
            )
            await self._send_dm(
                int(refunded["user_id"]),
                ui.auction_cancelled_dm_embed(
                    name=str(result["name"]), auction_id=auction_id,
                    refunded=int(refunded["amount"]),
                    balance_after=int(refunded["balance_after"]),
                    reason=reason,
                ),
                queue_on_failure=False,
            )
        await self._safe(self.log_event(
            guild_id, "⚫ オークションを中止しました",
            fields=(
                ("オークション", f"`{auction_id}` {result['name']}", True),
                ("操作者", f"<@{operator_id}>", True),
                ("返金", f"<@{refunded['user_id']}> へ "
                         f"{utils.fmt_int(int(refunded['amount']))}"
                 if refunded else "なし", True),
                ("理由", utils.truncate(reason, 200), False),
            ),
            color=config.Color.WARNING,
        ), context="オークション中止ログ")
        await self._safe(self.refresh_auction_panel(auction_id), context="オークションパネル更新")
        self.request_ranking_refresh(guild_id)
        return result

    async def close_auction(
        self, auction_id: int, *, force: bool = False, operator_id: int | None = None
    ) -> dict[str, Any]:
        """締切を確定し、落札者へ景品のロールを渡す。

        ロールを渡せなかった場合は落札額を返金し、中止として扱う。
        「支払ったのに何も無い」状態を残さないための救済措置。
        """
        outcome = await self.db.close_auction(auction_id, force=force)
        if not outcome.get("closed"):
            return outcome
        guild_id = int(outcome["guild_id"])
        if outcome["reason"] == "NO_BIDS":
            await self._safe(self.log_event(
                guild_id, "🔴 オークションは入札なしで終了しました",
                fields=(("オークション", f"`{auction_id}` {outcome['name']}", True),),
                color=config.Color.NEUTRAL,
            ), context="オークション終了ログ")
            await self._safe(self.refresh_auction_panel(auction_id),
                             context="オークションパネル更新")
            return outcome

        winner_id = int(outcome["winner_id"])
        granted = await self._grant_auction_role(outcome)
        if not granted:
            refund = await self.db.refund_auction_winner(
                auction_id,
                reason=f"景品のロールを渡せなかったため返金: {outcome['name']}",
                operator_id=operator_id,
            )
            if refund.get("refunded"):
                await self._log_balance_from_history(
                    guild_id, winner_id,
                    change_type=config.BalanceChangeType.AUCTION_REFUND,
                    fallback=refund, operator_id=operator_id,
                    reason=f"景品を渡せなかったため返金: {outcome['name']}",
                )
                await self._send_dm(
                    winner_id,
                    ui.auction_cancelled_dm_embed(
                        name=str(outcome["name"]), auction_id=auction_id,
                        refunded=int(refund["amount"]),
                        balance_after=int(refund["balance_after"]),
                        reason="景品のロールを付与できなかったため",
                    ),
                    queue_on_failure=False,
                )
            await self.alert_admins(
                guild_id, "オークションの景品を渡せませんでした",
                f"オークション `#{auction_id}` ({outcome['name']}) の景品 "
                f"<@&{outcome['role_id']}> を <@{winner_id}> へ付与できませんでした。\n"
                "落札額は返金し、オークションは中止として記録しました。",
            )
            await self._safe(self.refresh_auction_panel(auction_id),
                             context="オークションパネル更新")
            self.request_ranking_refresh(guild_id)
            return {**outcome, "delivered": False}

        self.metrics["auctions_closed"] += 1
        await self.db.add_audit_log(
            actor_id=operator_id if operator_id is not None else config.SYSTEM_ACTOR_ID,
            action="AUCTION_CLOSE", guild_id=guild_id, target_user_id=winner_id,
            detail={"auction_id": auction_id, "winning_bid": outcome["winning_bid"],
                    "role_id": outcome["role_id"],
                    "role_expires_at": outcome["role_expires_at"]},
        )
        balance = await self.db.get_balance(guild_id, winner_id)
        await self._send_dm(
            winner_id,
            ui.auction_won_dm_embed(
                name=str(outcome["name"]), auction_id=auction_id,
                winning_bid=int(outcome["winning_bid"]), role_id=int(outcome["role_id"]),
                role_expires_at=outcome["role_expires_at"], balance=balance,
            ),
            queue_on_failure=False,
        )
        await self._safe(self.log_event(
            guild_id, "🏁 オークションが落札されました",
            fields=(
                ("オークション", f"`{auction_id}` {outcome['name']}", True),
                ("落札者", f"<@{winner_id}>", True),
                ("落札額", utils.fmt_int(int(outcome["winning_bid"])), True),
                ("景品", f"<@&{outcome['role_id']}>", True),
                ("有効期限", utils.format_jst(int(outcome["role_expires_at"]))
                 if outcome["role_expires_at"] else "無期限", True),
            ),
            color=config.Color.SUCCESS,
        ), context="落札ログ")
        await self._safe(self.post_auction_result(auction_id), context="落札実績")
        await self._safe(self.refresh_auction_panel(auction_id), context="オークションパネル更新")
        self.request_ranking_refresh(guild_id)
        return {**outcome, "delivered": True}

    async def _grant_auction_role(self, outcome: dict[str, Any]) -> bool:
        """落札者へ景品のロールを渡す (成功したかを返す)。"""
        guild = self.bot.get_guild(int(outcome["guild_id"]))
        if guild is None:
            logger.error("落札処理でサーバーが見つかりません guild=%s", outcome["guild_id"])
            return False
        member = guild.get_member(int(outcome["winner_id"]))
        role = guild.get_role(int(outcome["role_id"]))
        if member is None or role is None:
            logger.error(
                "落札処理で対象が見つかりません auction=%s member=%s role=%s",
                outcome["auction_id"], member, role,
            )
            return False
        if role in getattr(member, "roles", []):
            return True
        try:
            await member.add_roles(
                role, reason=utils.truncate(
                    f"オークション落札 #{outcome['auction_id']} ({outcome['name']})", 400)
            )
        except Exception as exc:  # noqa: BLE001 - 失敗時は返金へ進む
            logger.error(
                "落札ロールの付与に失敗しました auction=%s: %s",
                outcome["auction_id"], utils.safe_error_text(exc),
            )
            return False
        return True

    async def close_due_auctions(self) -> int:
        """締切を過ぎたオークションを確定する。"""
        try:
            rows = await self.db.list_due_auctions()
        except Exception:  # noqa: BLE001
            logger.exception("締切オークションの取得に失敗しました")
            return 0
        handled = 0
        for row in rows:
            try:
                await self.close_auction(int(row["id"]))
                handled += 1
            except Exception:  # noqa: BLE001
                logger.exception("オークションの締切処理に失敗しました auction=%s", row["id"])
        if handled:
            logger.info("オークション %s 件を締め切りました", handled)
        return handled

    async def expire_auction_roles(self) -> int:
        """期間つきで落札されたロールの期限切れを処理する。"""
        try:
            rows = await self.db.list_auction_role_expiries()
        except Exception:  # noqa: BLE001
            logger.exception("落札ロールの期限一覧の取得に失敗しました")
            return 0
        handled = 0
        for row in rows:
            auction_id = int(row["id"])
            guild_id = int(row["guild_id"])
            winner_id = row["winner_id"]
            # 先に印を消す。剥奪に失敗しても毎回やり直して DM を連投しない。
            if not await self.db.clear_auction_role_expiry(auction_id):
                continue
            if winner_id is not None:
                await self._remove_auction_role(
                    guild_id, int(winner_id), int(row["role_id"]),
                    reason=f"オークション落札ロールの期限切れ #{auction_id}",
                )
            await self._safe(self.log_event(
                guild_id, "⌛ 落札ロールの有効期限が切れました",
                fields=(
                    ("オークション", f"`{auction_id}` {row['name']}", True),
                    ("対象", f"<@{winner_id}>" if winner_id else "-", True),
                    ("ロール", f"<@&{row['role_id']}>", True),
                ),
                color=config.Color.NEUTRAL,
            ), context="落札ロール期限ログ")
            handled += 1
        return handled

    async def _remove_auction_role(
        self, guild_id: int, user_id: int, role_id: int, *, reason: str
    ) -> bool:
        """落札で付与したロールを剥奪する。

        ショップで同じロールを購入している場合は剥がさない (二重に付与された
        ロールを片方の期限で外してしまうのを防ぐ)。
        """
        remaining = await self.db.list_active_purchases_for_role(guild_id, user_id, role_id)
        if remaining:
            logger.info(
                "ショップ購入が有効なためロールを維持します guild=%s user=%s role=%s",
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
        if role not in getattr(member, "roles", []):
            return True
        try:
            await member.remove_roles(role, reason=utils.truncate(reason, 400))
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "落札ロールの剥奪に失敗しました guild=%s user=%s role=%s: %s",
                guild_id, user_id, role_id, utils.safe_error_text(exc),
            )
            await self.alert_admins(
                guild_id, "落札ロールの剥奪に失敗",
                f"<@{user_id}> の <@&{role_id}> を剥奪できませんでした。手動で外してください。",
            )
            return False

    async def refresh_auction_panel(self, auction_id: int) -> bool:
        """オークションのパネルを最新の状態に更新する。"""
        auction = await self.db.get_auction(auction_id)
        if auction is None or not auction["message_id"]:
            return False
        bids = await self.db.list_auction_bids(auction_id, limit=5)
        counts = await self.db.count_auction_bids(auction_id)
        embed = ui.auction_panel_embed(auction, bids, counts=counts)
        channel = await self._resolve_message_channel(
            int(auction["guild_id"]), int(auction["channel_id"] or 0)
        )
        if channel is None:
            return False
        view = (
            ui.AuctionView() if str(auction["status"]) == config.AuctionStatus.OPEN
            else None
        )
        try:
            message = await channel.fetch_message(int(auction["message_id"]))
            await message.edit(embed=embed, view=view)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "オークションパネルの更新に失敗しました auction=%s: %s",
                auction_id, utils.safe_error_text(exc),
            )
            return False

    async def post_auction_result(self, auction_id: int) -> None:
        """落札結果を実績チャンネルへ投稿する。"""
        auction = await self.db.get_auction(auction_id)
        if auction is None:
            return
        counts = await self.db.count_auction_bids(auction_id)
        await self.post_generic_achievement(
            int(auction["guild_id"]), ui.auction_result_embed(auction, counts=counts)
        )

    # ==================================================================
    # サーバー全体のチャージ目標
    # ==================================================================
    async def create_goal(
        self,
        guild: discord.Guild,
        *,
        name: str,
        target_amount: int,
        reward_amount: int,
        reward_role: discord.Role | None,
        days: float | None,
        created_by: int,
    ) -> dict[str, Any]:
        """チャージ目標を作る。

        報酬にロールを使う場合は、先に付与できるかを確かめる。達成してから
        「配れませんでした」となると、参加者の期待を裏切ることになる。
        """
        await self.ensure_usable_guild(guild.id)
        if reward_role is not None:
            problem = self.role_grant_problem(guild, reward_role)
            if problem:
                raise ChargeError(config.ErrorCode.ROLE_ASSIGN_FAILED, problem)
        if reward_amount <= 0 and reward_role is None:
            raise ChargeError(
                config.ErrorCode.INVALID_AMOUNT,
                "報酬 (残高またはロール) を少なくとも1つ指定してください",
            )
        now = utils.now_ts()
        ends_at = now + int(days * 86400) if days else None
        try:
            goal_id = await self.db.create_goal(
                guild_id=guild.id, name=name, target_amount=target_amount,
                reward_amount=reward_amount,
                reward_role_id=reward_role.id if reward_role else None,
                starts_at=now, ends_at=ends_at, created_by=created_by,
            )
        except GoalError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        await self.db.add_audit_log(
            actor_id=created_by, action="GOAL_CREATE", guild_id=guild.id,
            detail={"goal_id": goal_id, "target": target_amount,
                    "reward_amount": reward_amount,
                    "reward_role_id": reward_role.id if reward_role else None,
                    "ends_at": ends_at},
        )
        await self._safe(self.log_event(
            guild.id, "🎯 チャージ目標を開始しました",
            fields=(
                ("目標", name, True),
                ("目標額", utils.fmt_yen(target_amount), True),
                ("報酬", (f"{utils.fmt_int(reward_amount)}" if reward_amount else "なし")
                 + (f" + {reward_role.mention}" if reward_role else ""), True),
                ("締切", utils.format_jst(ends_at) if ends_at else "期限なし", True),
            ),
            color=config.Color.ACCENT,
        ), context="目標開始ログ")
        await self._safe(self.refresh_goal_panels(guild.id), context="目標パネル更新")
        return {"goal_id": goal_id, "ends_at": ends_at}

    async def refresh_goal_panels(self, guild_id: int) -> int:
        """目標パネルへ最新の進捗を反映する。"""
        goal = await self.db.get_open_goal(guild_id)
        progress = await self.db.goal_progress(goal) if goal is not None else None
        return await self._update_panels(
            guild_id, config.PANEL_TYPE_GOAL,
            ui.goal_panel_embed(goal, progress), ui.GoalPanelView(),
        )

    async def check_goals(self) -> dict[str, int]:
        """すべてのサーバーの目標を見て、達成・期限切れを処理する。"""
        result = {"achieved": 0, "closed": 0, "refreshed": 0}
        try:
            guild_ids = await self.db.list_goal_guilds()
        except Exception:  # noqa: BLE001
            logger.exception("目標のあるサーバー一覧の取得に失敗しました")
            return result
        for guild_id in guild_ids:
            try:
                outcome = await self.check_guild_goal(guild_id)
            except Exception:  # noqa: BLE001
                logger.exception("目標の確認に失敗しました guild=%s", guild_id)
                continue
            for key in ("achieved", "closed", "refreshed"):
                result[key] += int(outcome.get(key, 0))
        return result

    async def check_guild_goal(self, guild_id: int) -> dict[str, int]:
        """1サーバーの目標を確認する (達成なら報酬を配る)。"""
        goal = await self.db.get_open_goal(guild_id)
        if goal is None:
            return {}
        goal_id = int(goal["id"])
        progress = await self.db.goal_progress(goal)
        total = int(progress["total"])
        target = int(goal["target_amount"])
        now = utils.now_ts()
        if total >= target:
            # 先に状態を進める。二重達成を防ぐため、更新できた側だけが配布する。
            if await self.db.mark_goal_achieved(goal_id, total=total):
                await self.distribute_goal_rewards(goal_id)
                return {"achieved": 1}
            return {}
        if goal["ends_at"] and int(goal["ends_at"]) <= now:
            if await self.db.close_goal(
                goal_id, status=config.GoalStatus.CLOSED, total=total
            ):
                await self._announce_goal_failed(goal, total)
                return {"closed": 1}
            return {}
        # 進捗が変わったときだけパネルを書き換える (無駄な編集を避ける)
        signature = f"{total}:{progress['users']}"
        if self._goal_signatures.get(guild_id) != signature:
            self._goal_signatures[guild_id] = signature
            await self._safe(self.refresh_goal_panels(guild_id), context="目標パネル更新")
            return {"refreshed": 1}
        return {}

    async def distribute_goal_rewards(self, goal_id: int) -> dict[str, int]:
        """達成した目標の報酬を期間内の参加者へ配る。

        ``goal_reward_grants`` の UNIQUE(goal_id, user_id) を先に立ててから
        残高を加算するので、途中で落ちても二重には配らない。
        """
        goal = await self.db.get_goal(goal_id)
        if goal is None:
            return {"granted": 0}
        guild_id = int(goal["guild_id"])
        guild = self.bot.get_guild(guild_id)
        reward_amount = int(goal["reward_amount"] or 0)
        role_id = goal["reward_role_id"]
        role = guild.get_role(int(role_id)) if (guild and role_id) else None
        participants = await self.db.list_goal_participants(goal)
        granted = 0
        role_failures = 0
        for row in participants:
            user_id = int(row["user_id"])
            if not await self.db.record_goal_grant(
                goal_id=goal_id, guild_id=guild_id, user_id=user_id,
                amount=reward_amount, role_id=role.id if role else None,
            ):
                continue  # 既に配布済み
            if reward_amount > 0:
                try:
                    await self.db.adjust_balance(
                        guild_id=guild_id, user_id=user_id, amount=reward_amount,
                        change_type=config.BalanceChangeType.GOAL_REWARD,
                        operator_id=config.SYSTEM_ACTOR_ID,
                        reason=utils.truncate(f"目標達成報酬: {goal['name']}", 500),
                        transaction_id=f"GOAL-{goal_id}-U{user_id}",
                    )
                except Exception:  # noqa: BLE001 - 1人の失敗で全体を止めない
                    logger.exception(
                        "目標報酬の付与に失敗しました goal=%s user=%s", goal_id, user_id
                    )
                    continue
            if role is not None and guild is not None:
                member = guild.get_member(user_id)
                if member is not None and role not in getattr(member, "roles", []):
                    try:
                        await member.add_roles(
                            role, reason=utils.truncate(
                                f"目標達成報酬 #{goal_id} ({goal['name']})", 400)
                        )
                    except Exception as exc:  # noqa: BLE001
                        role_failures += 1
                        logger.warning(
                            "目標報酬のロール付与に失敗しました goal=%s user=%s: %s",
                            goal_id, user_id, utils.safe_error_text(exc),
                        )
            granted += 1
            await self._send_dm(
                user_id,
                ui.goal_reward_dm_embed(
                    goal_name=str(goal["name"]),
                    guild_name=self.guild_name(guild_id),
                    reward_amount=reward_amount,
                    role_id=role.id if role else None,
                    total=int(goal["achieved_total"] or 0),
                    target=int(goal["target_amount"]),
                    contribution=int(row["amount"]),
                ),
                queue_on_failure=False,
            )
        if role_failures:
            await self.alert_admins(
                guild_id, "目標報酬のロールを配れませんでした",
                f"目標 `#{goal_id}` の報酬ロール <@&{role_id}> を "
                f"{role_failures} 人へ付与できませんでした。\n"
                "ロールの位置と Bot の権限を確認し、手動で付与してください。",
            )
        self.metrics["goal_rewards"] += granted
        await self.db.add_audit_log(
            actor_id=config.SYSTEM_ACTOR_ID, action="GOAL_ACHIEVED", guild_id=guild_id,
            detail={"goal_id": goal_id, "granted": granted,
                    "reward_amount": reward_amount,
                    "total": int(goal["achieved_total"] or 0)},
        )
        await self._safe(self.log_event(
            guild_id, "🎉 チャージ目標を達成しました",
            fields=(
                ("目標", str(goal["name"]), True),
                ("目標額", utils.fmt_yen(int(goal["target_amount"])), True),
                ("到達額", utils.fmt_yen(int(goal["achieved_total"] or 0)), True),
                ("配布人数", f"{granted}人", True),
                ("1人あたり", utils.fmt_int(reward_amount) if reward_amount else "なし", True),
                ("ロール", f"<@&{role_id}>" if role_id else "なし", True),
            ),
            color=config.Color.SUCCESS,
        ), context="目標達成ログ")
        await self._safe(self.post_generic_achievement(
            guild_id,
            ui.goal_achieved_embed(
                goal, granted=granted, guild_name=self.guild_name(guild_id)
            ),
        ), context="目標達成の実績投稿")
        await self._safe(self.refresh_goal_panels(guild_id), context="目標パネル更新")
        self._goal_signatures.pop(guild_id, None)
        self.request_ranking_refresh(guild_id)
        return {"granted": granted}

    async def _announce_goal_failed(self, goal: Any, total: int) -> None:
        """期限までに達成できなかったことを知らせる。"""
        guild_id = int(goal["guild_id"])
        await self._safe(self.log_event(
            guild_id, "⌛ チャージ目標は未達のまま終了しました",
            fields=(
                ("目標", str(goal["name"]), True),
                ("目標額", utils.fmt_yen(int(goal["target_amount"])), True),
                ("到達額", utils.fmt_yen(total), True),
            ),
            color=config.Color.NEUTRAL,
        ), context="目標終了ログ")
        await self._safe(self.refresh_goal_panels(guild_id), context="目標パネル更新")
        self._goal_signatures.pop(guild_id, None)

    async def close_goal(
        self, guild_id: int, goal_id: int, *, operator_id: int, cancel: bool
    ) -> dict[str, Any]:
        """目標を手動で終了する (中止か、その時点で締める)。

        ``cancel=False`` なら目標額に届いていれば達成として報酬を配る。
        """
        goal = await self.db.get_goal(goal_id, guild_id)
        if goal is None or str(goal["status"]) != config.GoalStatus.OPEN:
            raise ChargeError(config.ErrorCode.GOAL_NOT_FOUND)
        progress = await self.db.goal_progress(goal)
        total = int(progress["total"])
        if not cancel and total >= int(goal["target_amount"]):
            if await self.db.mark_goal_achieved(goal_id, total=total):
                outcome = await self.distribute_goal_rewards(goal_id)
                return {"status": config.GoalStatus.ACHIEVED, "total": total, **outcome}
        status = config.GoalStatus.CANCELLED if cancel else config.GoalStatus.CLOSED
        if not await self.db.close_goal(goal_id, status=status, total=total):
            raise ChargeError(config.ErrorCode.GOAL_NOT_FOUND)
        await self.db.add_audit_log(
            actor_id=operator_id,
            action="GOAL_CANCEL" if cancel else "GOAL_CLOSE",
            guild_id=guild_id,
            detail={"goal_id": goal_id, "total": total,
                    "target": int(goal["target_amount"])},
        )
        await self._safe(self.log_event(
            guild_id,
            "⚫ チャージ目標を中止しました" if cancel else "🏁 チャージ目標を締めました",
            fields=(
                ("目標", str(goal["name"]), True),
                ("到達額", utils.fmt_yen(total), True),
                ("目標額", utils.fmt_yen(int(goal["target_amount"])), True),
                ("操作者", f"<@{operator_id}>", True),
            ),
            color=config.Color.WARNING,
        ), context="目標終了ログ")
        await self._safe(self.refresh_goal_panels(guild_id), context="目標パネル更新")
        self._goal_signatures.pop(guild_id, None)
        return {"status": status, "total": total, "granted": 0}

    # ==================================================================
    # 不正検知
    # ==================================================================
    async def run_fraud_scan(self) -> dict[str, int]:
        """すべての許可サーバーで兆候を洗い出す。"""
        result = {"flagged": 0, "updated": 0}
        try:
            guild_ids = await self.db.list_fraud_guilds()
        except Exception:  # noqa: BLE001
            logger.exception("検知対象サーバーの取得に失敗しました")
            return result
        for guild_id in guild_ids:
            try:
                outcome = await self.scan_guild_fraud(guild_id)
            except Exception:  # noqa: BLE001
                logger.exception("不正検知に失敗しました guild=%s", guild_id)
                continue
            result["flagged"] += int(outcome.get("flagged", 0))
            result["updated"] += int(outcome.get("updated", 0))
        if result["flagged"]:
            logger.info("不正の兆候を %s 件検知しました", result["flagged"])
        return result

    async def scan_guild_fraud(self, guild_id: int) -> dict[str, int]:
        """1サーバーぶんの兆候を洗い出してフラグを立てる。

        ここでは「疑わしい」を機械的に拾うだけで、**自動で凍結などの処分は
        行わない**。誤検知で利用者を止めてしまう方が害が大きいため、
        判断は必ず管理者が行う。
        """
        flagged = 0
        updated = 0
        for candidate in await self._collect_fraud_candidates(guild_id):
            flag_id, created = await self.db.create_fraud_flag(
                guild_id=guild_id,
                user_id=int(candidate["user_id"]),
                kind=str(candidate["kind"]),
                severity=str(candidate["severity"]),
                detail=str(candidate["detail"]),
                evidence=candidate.get("evidence"),
            )
            if created:
                flagged += 1
                await self._safe(self.post_fraud_card(flag_id), context="検知カード")
            else:
                updated += 1
        return {"flagged": flagged, "updated": updated}

    async def _collect_fraud_candidates(self, guild_id: int) -> list[dict[str, Any]]:
        """各検知ルールを回して候補を集める。

        1つのルールが失敗しても他のルールは動かす (検知が全部止まらないように)。
        """
        candidates: list[dict[str, Any]] = []
        rules = (
            ("BURST_CHARGE", self._detect_burst_charge),
            ("SHARED_SENDER", self._detect_shared_sender),
            ("DRAIN_AND_LEAVE", self._detect_drain_and_leave),
            ("INVITE_ONLY", self._detect_invite_only),
            ("RAPID_REFUND", self._detect_rapid_refund),
        )
        for name, rule in rules:
            try:
                candidates.extend(await rule(guild_id))
            except Exception:  # noqa: BLE001
                logger.exception("検知ルールの実行に失敗しました rule=%s guild=%s",
                                 name, guild_id)
        return candidates

    async def _detect_burst_charge(self, guild_id: int) -> list[dict[str, Any]]:
        """短時間に何度もチャージしている (不正入手した資金の現金化の疑い)。"""
        rows = await self.db.find_burst_chargers(
            guild_id, window=config.FRAUD_BURST_WINDOW, minimum=config.FRAUD_BURST_COUNT
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            count = int(row["count"])
            total = int(row["total"] or 0)
            minutes = max(1, config.FRAUD_BURST_WINDOW // 60)
            out.append({
                "user_id": int(row["user_id"]),
                "kind": config.FraudKind.BURST_CHARGE,
                "severity": (
                    config.FraudSeverity.HIGH if count >= config.FRAUD_BURST_COUNT * 2
                    else config.FraudSeverity.WARN
                ),
                "detail": (
                    f"直近 {minutes} 分で **{count} 件** "
                    f"({utils.fmt_yen(total)}) のチャージが完了しています。"
                ),
                "evidence": {
                    "count": count, "total": total,
                    "first_at": utils.format_jst(int(row["first_at"])),
                    "last_at": utils.format_jst(int(row["last_at"])),
                    "window_seconds": config.FRAUD_BURST_WINDOW,
                },
            })
        return out

    async def _detect_shared_sender(self, guild_id: int) -> list[dict[str, Any]]:
        """同じ Kyash 送金者名を複数の利用者が使っている (名義貸し・転売の疑い)。"""
        rows = await self.db.find_shared_senders(
            guild_id, minimum_users=config.FRAUD_SHARED_SENDER_USERS
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            user_ids = [
                int(v) for v in str(row["user_ids"] or "").split(",") if v.strip().isdigit()
            ]
            others = len(user_ids)
            for user_id in user_ids:
                partners = [str(u) for u in user_ids if u != user_id][:10]
                out.append({
                    "user_id": user_id,
                    "kind": config.FraudKind.SHARED_SENDER,
                    "severity": (
                        config.FraudSeverity.HIGH if others >= 3
                        else config.FraudSeverity.WARN
                    ),
                    "detail": (
                        f"同じ送金者名を **{others} 人** が使っています。\n"
                        f"他の利用者: {', '.join(f'<@{u}>' for u in partners) or '-'}"
                    ),
                    # 送金者名そのものは伏せて記録する (第三者の氏名を広めない)
                    "evidence": {
                        "sender": utils.mask_identifier(str(row["sender_name"]), keep=2),
                        "users": others,
                        "user_ids": user_ids[:20],
                        "charges": int(row["count"]),
                        "total": int(row["total"] or 0),
                    },
                })
        return out

    async def _detect_drain_and_leave(self, guild_id: int) -> list[dict[str, Any]]:
        """チャージ直後に使い切って退出している (荒らし・転売の疑い)。"""
        rows = await self.db.find_drain_and_leave(
            guild_id, window=config.FRAUD_DRAIN_WINDOW
        )
        ratio = utils.to_decimal(config.FRAUD_DRAIN_RATIO) or Decimal("0.9")
        out: list[dict[str, Any]] = []
        for row in rows:
            user_id = int(row["user_id"])
            left_at = int(row["left_at"])
            credited = int(row["credited"] or 0)
            spent = await self.db.sum_spending(
                guild_id, user_id,
                since=left_at - config.FRAUD_DRAIN_WINDOW, until=left_at,
            )
            if credited <= 0 or Decimal(spent) < Decimal(credited) * ratio:
                continue
            out.append({
                "user_id": user_id,
                "kind": config.FraudKind.DRAIN_AND_LEAVE,
                "severity": config.FraudSeverity.WARN,
                "detail": (
                    f"退出前に取得した **{utils.fmt_int(credited)}** のうち "
                    f"**{utils.fmt_int(spent)}** を使い切って退出しています。"
                ),
                "evidence": {
                    "credited": credited, "spent": spent,
                    "left_at": utils.format_jst(left_at),
                    "window_seconds": config.FRAUD_DRAIN_WINDOW,
                },
            })
        return out

    async def _detect_invite_only(self, guild_id: int) -> list[dict[str, Any]]:
        """招待報酬だけを集めてチャージしない (自作アカウントによる周回の疑い)。"""
        rows = await self.db.find_invite_only_users(
            guild_id, minimum=config.FRAUD_INVITE_ONLY_COUNT
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            invites = int(row["invites"])
            out.append({
                "user_id": int(row["user_id"]),
                "kind": config.FraudKind.INVITE_ONLY,
                "severity": (
                    config.FraudSeverity.WARN
                    if invites >= config.FRAUD_INVITE_ONLY_COUNT * 2
                    else config.FraudSeverity.INFO
                ),
                "detail": (
                    f"確定した招待が **{invites} 件** ある一方で、"
                    "チャージが1件もありません。"
                ),
                "evidence": {
                    "invites": invites, "rewards": int(row["rewards"] or 0),
                },
            })
        return out

    async def _detect_rapid_refund(self, guild_id: int) -> list[dict[str, Any]]:
        """返金申請を繰り返している (規約の悪用の疑い)。"""
        rows = await self.db.find_rapid_refunders(
            guild_id, minimum=config.FRAUD_REFUND_COUNT
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            requests = int(row["requests"])
            rejected = int(row["rejected"] or 0)
            out.append({
                "user_id": int(row["user_id"]),
                "kind": config.FraudKind.RAPID_REFUND,
                "severity": (
                    config.FraudSeverity.WARN if rejected else config.FraudSeverity.INFO
                ),
                "detail": (
                    f"返金申請が **{requests} 件** あります "
                    f"(うち却下 {rejected} 件)。"
                ),
                "evidence": {"requests": requests, "rejected": rejected},
            })
        return out

    async def post_fraud_card(self, flag_id: int) -> None:
        """検知カードを審査チャンネルへ投稿する (無ければ Owner へ通知)。"""
        flag = await self.db.get_fraud_flag(flag_id)
        if flag is None:
            return
        guild_id = int(flag["guild_id"])
        embed = ui.fraud_card_embed(flag, guild_name=self.guild_name(guild_id))
        channel_id = await self.get_review_channel_id()
        channel = (
            await self._resolve_global_channel(channel_id) if channel_id else None
        )
        if channel is None:
            settings = await self.db.get_settings(guild_id)
            if settings.log_channel_id:
                channel = await self._resolve_channel(
                    guild_id, settings.log_channel_id, "log_channel_id"
                )
        if channel is None:
            # 投稿先が無い場合でも埋もれさせない (テキストで Owner へ知らせる)
            await self._safe(self.bot.alert_owner(
                f"🛡 **不正の兆候を検知しました** (検知ID `{flag_id}`)\n"
                f"対象: <@{int(flag['user_id'])}>\n"
                f"種別: {config.FRAUD_KIND_LABELS.get(str(flag['kind']), str(flag['kind']))}\n"
                f"{utils.truncate(str(flag['detail'] or ''), 500)}\n"
                "`/fraud list` で確認してください。"
            ), context="検知の Owner 通知")
            return
        try:
            message = await channel.send(embed=embed, view=ui.FraudCardView())
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("検知カードの投稿に失敗しました flag=%s: %s",
                           flag_id, utils.safe_error_text(exc))
            await self._safe(self.bot.alert_owner(
                f"🛡 検知カードを投稿できませんでした (検知ID `{flag_id}`)。"
                "`/fraud list` で確認してください。"
            ), context="検知の Owner 通知")
            return
        await self.db.set_fraud_message(
            flag_id, channel_id=channel.id, message_id=message.id
        )

    async def refresh_fraud_card(self, flag_id: int) -> bool:
        """検知カードを最新の状態に書き換える。"""
        flag = await self.db.get_fraud_flag(flag_id)
        if flag is None or not flag["message_id"]:
            return False
        guild_id = int(flag["guild_id"])
        embed = ui.fraud_card_embed(flag, guild_name=self.guild_name(guild_id))
        channel = await self._resolve_global_channel(int(flag["channel_id"] or 0))
        if channel is None:
            return False
        view = (
            ui.FraudCardView() if str(flag["status"]) == config.FraudStatus.OPEN else None
        )
        try:
            message = await channel.fetch_message(int(flag["message_id"]))
            await message.edit(embed=embed, view=view)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("検知カードの更新に失敗しました flag=%s: %s",
                           flag_id, utils.safe_error_text(exc))
            return False

    async def review_fraud_flag(
        self, flag_id: int, *, status: str, reviewed_by: int, note: str | None = None
    ) -> dict[str, Any]:
        """フラグを「対処済み」または「問題なし」にする。"""
        flag = await self.db.get_fraud_flag(flag_id)
        if flag is None:
            raise ChargeError(config.ErrorCode.FRAUD_FLAG_NOT_FOUND)
        if not await self.db.resolve_fraud_flag(
            flag_id, status=status, reviewed_by=reviewed_by, note=note
        ):
            raise ChargeError(
                config.ErrorCode.FRAUD_FLAG_NOT_FOUND, "この検知はすでに処理されています"
            )
        guild_id = int(flag["guild_id"])
        await self.db.add_audit_log(
            actor_id=reviewed_by,
            action="FRAUD_RESOLVE" if status == config.FraudStatus.RESOLVED
            else "FRAUD_IGNORE",
            guild_id=guild_id, target_user_id=int(flag["user_id"]),
            detail={"flag_id": flag_id, "kind": str(flag["kind"]),
                    "note": utils.truncate(note, 300) if note else None},
        )
        await self._safe(self.log_event(
            guild_id,
            "🛡 検知を対処済みにしました" if status == config.FraudStatus.RESOLVED
            else "🛡 検知を問題なしにしました",
            fields=(
                ("検知ID", f"`{flag_id}`", True),
                ("対象", f"<@{int(flag['user_id'])}>", True),
                ("種別", config.FRAUD_KIND_LABELS.get(
                    str(flag["kind"]), str(flag["kind"])), True),
                ("担当", f"<@{reviewed_by}>", True),
                ("メモ", utils.truncate(note, 200) if note else "-", False),
            ),
            color=config.Color.NEUTRAL,
        ), context="検知の処理ログ")
        await self._safe(self.refresh_fraud_card(flag_id), context="検知カード更新")
        return {"flag_id": flag_id, "status": status,
                "user_id": int(flag["user_id"]), "kind": str(flag["kind"])}

    # ==================================================================
    # 返金申請
    # ==================================================================
    async def request_refund(
        self, guild_id: int, user_id: int, tx_id: str, reason: str
    ) -> dict[str, Any]:
        """利用者からの返金 (チャージ取消) 申請を受け付ける。

        ここで承認されても、**Kyash での送金は自動では行わない**。
        添付モジュールに送金の手段が無いため、実際の返金は管理者が
        Kyash 側で手作業で行い、Bot は内部残高の取消だけを担当する。
        この点は申請の受付時と審査カードの双方に明記する。
        """
        settings = await self.ensure_usable_guild(guild_id)
        if settings.emergency_stop:
            raise ChargeError(config.ErrorCode.EMERGENCY_STOP)
        if await self.db.is_frozen(guild_id, user_id):
            raise ChargeError(config.ErrorCode.USER_FROZEN)
        if not self._request_rate_limiter.check(f"refund:{guild_id}:{user_id}"):
            raise ChargeError(config.ErrorCode.RATE_LIMITED)
        try:
            created = await self.db.create_refund_request(
                guild_id=guild_id, user_id=user_id, transaction_id=tx_id,
                reason=reason,
            )
        except RefundError as exc:
            raise ChargeError(exc.code, exc.detail) from exc
        request_id = int(created["request_id"])
        self.metrics["refund_requests"] += 1
        await self.db.add_audit_log(
            actor_id=user_id, action="REFUND_REQUEST", guild_id=guild_id,
            target_user_id=user_id,
            detail={"request_id": request_id, "transaction_id": tx_id,
                    "amount": created["amount"],
                    "reason": utils.truncate(reason, 300)},
        )
        await self._safe(self.post_refund_card(request_id), context="返金審査カード")
        await self._safe(self.log_event(
            guild_id, "↩️ 返金の申請",
            fields=(
                ("申請ID", f"`{request_id}`", True),
                ("利用者", f"<@{user_id}>", True),
                ("取引ID", f"`{tx_id}`", True),
                ("取消される残高", utils.fmt_int(int(created["amount"])), True),
                ("理由", utils.truncate(reason, 200), False),
            ),
            color=config.Color.WARNING,
        ), context="返金申請ログ")
        return created

    async def post_refund_card(self, request_id: int) -> None:
        """返金申請の審査カードを投稿する。"""
        request = await self.db.get_refund_request(request_id)
        if request is None:
            return
        guild_id = int(request["guild_id"])
        transaction = await self.db.get_transaction(str(request["transaction_id"]))
        history = await self.db.list_refund_requests(
            guild_id, user_id=int(request["user_id"]), limit=5
        )
        embed = ui.refund_card_embed(
            request, transaction, guild_name=self.guild_name(guild_id),
            past_requests=history[1],
        )
        channel_id = await self.get_review_channel_id()
        channel = (
            await self._resolve_global_channel(channel_id) if channel_id else None
        )
        if channel is None:
            settings = await self.db.get_settings(guild_id)
            if settings.log_channel_id:
                channel = await self._resolve_channel(
                    guild_id, settings.log_channel_id, "log_channel_id"
                )
        if channel is None:
            await self._safe(self.bot.alert_owner(
                f"↩️ **返金の申請があります** (申請ID `{request_id}`)\n"
                f"利用者: <@{int(request['user_id'])}>\n"
                f"取引: `{request['transaction_id']}`\n"
                "`/refund list` で確認してください。"
            ), context="返金申請の Owner 通知")
            return
        try:
            message = await channel.send(embed=embed, view=ui.RefundCardView())
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("返金審査カードの投稿に失敗しました request=%s: %s",
                           request_id, utils.safe_error_text(exc))
            return
        await self.db.set_refund_message(
            request_id, channel_id=channel.id, message_id=message.id
        )

    async def refresh_refund_card(self, request_id: int) -> bool:
        """審査カードを最新の状態に書き換える。"""
        request = await self.db.get_refund_request(request_id)
        if request is None or not request["message_id"]:
            return False
        guild_id = int(request["guild_id"])
        transaction = await self.db.get_transaction(str(request["transaction_id"]))
        embed = ui.refund_card_embed(
            request, transaction, guild_name=self.guild_name(guild_id)
        )
        channel = await self._resolve_global_channel(int(request["channel_id"] or 0))
        if channel is None:
            return False
        view = (
            ui.RefundCardView()
            if str(request["status"]) == config.RefundRequestStatus.PENDING else None
        )
        try:
            message = await channel.fetch_message(int(request["message_id"]))
            await message.edit(embed=embed, view=view)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("返金審査カードの更新に失敗しました request=%s: %s",
                           request_id, utils.safe_error_text(exc))
            return False

    async def approve_refund(
        self, request_id: int, *, operator_id: int
    ) -> dict[str, Any]:
        """返金申請を承認し、内部残高を取り消す。

        先に申請の状態を進めてから残高を取り消す。逆順にすると、
        取消の直後に落ちた場合に「残高は減ったが申請は審査待ち」という
        取り違えやすい状態が残る。
        """
        request = await self.db.get_refund_request(request_id)
        if request is None:
            raise ChargeError(config.ErrorCode.REQUEST_NOT_FOUND)
        tx_id = str(request["transaction_id"])
        try:
            await self.db.transition_refund_request(
                request_id, status=config.RefundRequestStatus.APPROVED,
                reviewed_by=operator_id,
            )
        except IllegalStateTransition as exc:
            raise ChargeError(config.ErrorCode.REQUEST_ALREADY_HANDLED, str(exc)) from exc
        try:
            result = await self.refund_charge_transaction(
                tx_id, operator_id=operator_id,
                reason=f"返金申請 #{request_id} の承認",
            )
        except Exception as exc:  # noqa: BLE001 - 取消できなければ申請を戻せない
            logger.exception("返金の取消処理に失敗しました request=%s tx=%s",
                             request_id, tx_id)
            await self.alert_admins(
                int(request["guild_id"]), "返金の取消処理に失敗",
                f"返金申請 `#{request_id}` は承認済みですが、取引 `{tx_id}` の"
                "残高取消に失敗しました。\n"
                "`/balance refund` で手動の取消を行ってください。",
            )
            raise ChargeError(config.ErrorCode.DATABASE_ERROR, str(exc)) from exc
        self.metrics["refunds_approved"] += 1
        guild_id = int(request["guild_id"])
        user_id = int(request["user_id"])
        await self.db.add_audit_log(
            actor_id=operator_id, action="REFUND_APPROVE", guild_id=guild_id,
            target_user_id=user_id,
            detail={"request_id": request_id, "transaction_id": tx_id,
                    "credited_amount": result["credited_amount"]},
        )
        await self._send_dm(
            user_id,
            ui.refund_result_dm_embed(
                guild_name=self.guild_name(guild_id), request_id=request_id,
                tx_id=tx_id, approved=True,
                amount=int(result["credited_amount"]),
                balance_after=int(result["balance_after"]),
                reason=None,
            ),
            queue_on_failure=False,
        )
        await self._safe(self.refresh_refund_card(request_id), context="返金カード更新")
        return {"request_id": request_id, "transaction_id": tx_id, **result}

    async def reject_refund(
        self, request_id: int, *, operator_id: int, reason: str
    ) -> dict[str, Any]:
        """返金申請を却下する (残高は動かさない)。"""
        request = await self.db.get_refund_request(request_id)
        if request is None:
            raise ChargeError(config.ErrorCode.REQUEST_NOT_FOUND)
        try:
            await self.db.transition_refund_request(
                request_id, status=config.RefundRequestStatus.REJECTED,
                reviewed_by=operator_id, reject_reason=reason,
            )
        except IllegalStateTransition as exc:
            raise ChargeError(config.ErrorCode.REQUEST_ALREADY_HANDLED, str(exc)) from exc
        self.metrics["refunds_rejected"] += 1
        guild_id = int(request["guild_id"])
        user_id = int(request["user_id"])
        await self.db.add_audit_log(
            actor_id=operator_id, action="REFUND_REJECT", guild_id=guild_id,
            target_user_id=user_id,
            detail={"request_id": request_id,
                    "transaction_id": str(request["transaction_id"]),
                    "reason": utils.truncate(reason, 300)},
        )
        await self._send_dm(
            user_id,
            ui.refund_result_dm_embed(
                guild_name=self.guild_name(guild_id), request_id=request_id,
                tx_id=str(request["transaction_id"]), approved=False,
                amount=int(request["amount"] or 0),
                balance_after=await self.db.get_balance(guild_id, user_id),
                reason=reason,
            ),
            queue_on_failure=False,
        )
        await self._safe(self.log_event(
            guild_id, "🔴 返金申請を却下",
            fields=(
                ("申請ID", f"`{request_id}`", True),
                ("利用者", f"<@{user_id}>", True),
                ("取引ID", f"`{request['transaction_id']}`", True),
                ("担当", f"<@{operator_id}>", True),
                ("理由", utils.truncate(reason, 200), False),
            ),
            color=config.Color.WARNING,
        ), context="返金却下ログ")
        await self._safe(self.refresh_refund_card(request_id), context="返金カード更新")
        return {"request_id": request_id, "status": config.RefundRequestStatus.REJECTED}

    async def cancel_refund_request(
        self, request_id: int, *, user_id: int
    ) -> dict[str, Any]:
        """利用者自身が申請を取り下げる。"""
        request = await self.db.get_refund_request(request_id)
        if request is None or int(request["user_id"]) != user_id:
            raise ChargeError(config.ErrorCode.REQUEST_NOT_FOUND)
        try:
            await self.db.transition_refund_request(
                request_id, status=config.RefundRequestStatus.CANCELLED,
                reviewed_by=user_id,
            )
        except IllegalStateTransition as exc:
            raise ChargeError(config.ErrorCode.REQUEST_ALREADY_HANDLED, str(exc)) from exc
        await self.db.add_audit_log(
            actor_id=user_id, action="REFUND_CANCEL",
            guild_id=int(request["guild_id"]), target_user_id=user_id,
            detail={"request_id": request_id,
                    "transaction_id": str(request["transaction_id"])},
        )
        await self._safe(self.refresh_refund_card(request_id), context="返金カード更新")
        return {"request_id": request_id, "status": config.RefundRequestStatus.CANCELLED}

    # ==================================================================
    # レシート (チャージの控え)
    # ==================================================================
    async def issue_receipt(
        self, guild_id: int, user_id: int, tx_id: str
    ) -> dict[str, Any]:
        """完了したチャージの控えを発行する。

        控えには署名を付けるため、後から改ざんを検出できる。
        本人以外の取引は発行しない。
        """
        row = await self.db.get_transaction(tx_id)
        if row is None or int(row["guild_id"]) != guild_id \
                or int(row["user_id"]) != user_id:
            raise ChargeError(config.ErrorCode.REFUND_NOT_ELIGIBLE, "取引が見つかりません")
        if str(row["status"]) != config.TxStatus.COMPLETED:
            raise ChargeError(
                config.ErrorCode.REFUND_NOT_ELIGIBLE,
                f"完了した取引のみ発行できます (状態: {row['status']})",
            )
        completed_at = int(row["completed_at"] or row["created_at"] or 0)
        code = self.receipts.issue(
            guild_id=guild_id, user_id=user_id, tx_id=tx_id,
            received=int(row["received_amount"] or 0),
            credited=int(row["credited_amount"] or 0),
            completed_at=completed_at,
        )
        return {
            "code": code,
            "tx_id": tx_id,
            "received": int(row["received_amount"] or 0),
            "credited": int(row["credited_amount"] or 0),
            "completed_at": completed_at,
            "provider": str(row["provider"] or config.ChargeProvider.KYASH),
            "refunded": bool(row["refunded_at"]),
        }

    async def verify_receipt_code(self, code: str) -> dict[str, Any]:
        """控えを検証する。

        署名が正しいことに加えて、**いまの記録と一致するか**も確かめる。
        署名は発行時点の内容を保証するだけなので、その後に取消された取引を
        「有効な控え」として扱わないようにする。
        """
        payload = self.receipts.verify(code)
        if payload is None:
            return {"valid": False, "reason": "SIGNATURE"}
        row = await self.db.get_transaction(str(payload["tx_id"]))
        if row is None:
            return {"valid": False, "reason": "NOT_FOUND", "payload": payload}
        mismatch = (
            int(row["guild_id"]) != int(payload["guild_id"])
            or int(row["user_id"]) != int(payload["user_id"])
            or int(row["received_amount"] or 0) != int(payload["received"])
            or int(row["credited_amount"] or 0) != int(payload["credited"])
        )
        if mismatch:
            return {"valid": False, "reason": "MISMATCH", "payload": payload, "row": row}
        if row["refunded_at"]:
            return {"valid": False, "reason": "REFUNDED", "payload": payload, "row": row}
        if str(row["status"]) != config.TxStatus.COMPLETED:
            return {"valid": False, "reason": "NOT_COMPLETED", "payload": payload,
                    "row": row}
        return {"valid": True, "reason": "OK", "payload": payload, "row": row}

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
    async def build_daily_chart(
        self, guild_id: int | None, *, days: int, title: str | None = None
    ) -> tuple[Any, dict[str, int]]:
        """日別推移のグラフ画像と、その期間の合計を作る。

        グラフを描けない環境 (Pillow 無し) では画像を ``None`` にして
        合計だけを返す。数字は Embed 側にも出すので、画像が無くても
        情報は失われない。

        描画は CPU を使うので専用スレッドで行い、Bot の応答を止めない。

        Returns:
            ``(discord.File | None, 合計の辞書)``
        """
        days = max(1, min(int(days), config.CHART_MAX_DAYS))
        rows = await self.db.get_daily_series(guild_id, days=days)
        series = chart.build_daily_series(rows, days=days)
        totals = {
            "amount": sum(p.amount for p in series),
            "count": sum(p.count for p in series),
            "credited": sum(p.credited for p in series),
            "days": days,
            "best_amount": max((p.amount for p in series), default=0),
            "active_days": sum(1 for p in series if p.count),
        }
        if not chart.available():
            return None, totals
        name = self.guild_name(guild_id) if guild_id is not None else "Bot 全体"
        subtitle = (
            f"{name} ・ 合計 {utils.fmt_yen(totals['amount'])} / "
            f"{totals['count']}件 ・ 付与 {utils.fmt_int(totals['credited'])}"
        )
        try:
            png = await asyncio.to_thread(
                chart.render_daily_chart,
                series,
                title=title or f"日別チャージ推移 ({days}日)",
                subtitle=subtitle,
            )
        except Exception:  # noqa: BLE001 - 画像が無くても統計は出す
            logger.exception("グラフの生成に失敗しました guild=%s", guild_id)
            return None, totals
        if not png:
            return None, totals
        return discord.File(io.BytesIO(png), filename=config.CHART_FILENAME), totals

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
        # 推移のグラフを添える (作れなければ Embed だけ送る)
        image, totals = await self.build_daily_chart(
            guild_id, days=config.CHART_DEFAULT_DAYS
        )
        if image is not None:
            embed.set_image(url=f"attachment://{config.CHART_FILENAME}")
            embed.add_field(
                name=f"直近 {totals['days']} 日の推移",
                value=(
                    f"合計 **{utils.fmt_yen(totals['amount'])}** / "
                    f"{totals['count']}件\n"
                    f"チャージのあった日: {totals['active_days']} / {totals['days']}日"
                ),
                inline=False,
            )
        try:
            if image is not None:
                await channel.send(embed=embed, file=image)
            else:
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
            requests=await self.db.count_requests_by_status(),
            price=await self.price.status_snapshot(),
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
