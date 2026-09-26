"""Bot 全体の静的設定・定数・エラーコード定義。

秘密情報 (Discord Token / Owner ID / Bootstrap Guild ID) は main.py に直接記述する。
このファイルには秘密情報を置かない。運用設定は DB (guild_settings / system_settings) に保存される。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# バージョン
# ---------------------------------------------------------------------------
BOT_VERSION: Final[str] = "1.0.0"
SCHEMA_VERSION: Final[int] = 1

# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------
BASE_DIR: Final[Path] = Path(__file__).resolve().parent
DATA_DIR: Final[Path] = BASE_DIR / "data"
DB_PATH: Final[Path] = DATA_DIR / "charge_bot.db"
BACKUP_DIR: Final[Path] = DATA_DIR / "backups"
SECRET_KEY_PATH: Final[Path] = DATA_DIR / "secret.key"
VENDOR_DIR: Final[Path] = BASE_DIR / "vendor"

# ---------------------------------------------------------------------------
# guild_settings デフォルト値 (すべて Discord コマンドで変更可能)
# ---------------------------------------------------------------------------
DEFAULT_CHARGE_RATE: Final[str] = "130"          # % (Decimal 文字列で保持)
DEFAULT_MINIMUM_CHARGE: Final[int] = 100         # 円
DEFAULT_MAXIMUM_CHARGE: Final[int] = 50_000      # 円
DEFAULT_DAILY_LIMIT: Final[int] = 100_000        # 円 / 1ユーザー / 1日
DEFAULT_GUILD_DAILY_LIMIT: Final[int] = 0        # 円 / サーバー全体 / 1日 (0=無効)
DEFAULT_RANKING_LIMIT: Final[int] = 10
DEFAULT_RANKING_INTERVAL: Final[int] = 300       # 秒 (バックグラウンド更新間隔)

# チャージ率の許容範囲 (%)
MIN_CHARGE_RATE: Final[Decimal] = Decimal("1")
MAX_CHARGE_RATE: Final[Decimal] = Decimal("1000")

# 金額設定の許容範囲 (円)
AMOUNT_HARD_MIN: Final[int] = 1
AMOUNT_HARD_MAX: Final[int] = 1_000_000
# ユーザー入力として受け付ける文字数上限 (異常に長い数値の拒否)
AMOUNT_INPUT_MAX_LEN: Final[int] = 9

# ランキング表示件数の許容範囲 (Discord Embed の安全上限)
RANKING_LIMIT_MIN: Final[int] = 5
RANKING_LIMIT_MAX: Final[int] = 25
RANKING_INTERVAL_MIN: Final[int] = 30
RANKING_INTERVAL_MAX: Final[int] = 3600

# ---------------------------------------------------------------------------
# チャージセッション / キュー / リトライ
# ---------------------------------------------------------------------------
LINK_WAIT_SECONDS: Final[int] = 900              # 送金リンク入力待ち (15分)
MAX_RETRY: Final[int] = 5
RETRY_BACKOFF_BASE: Final[int] = 5               # 秒 (指数バックオフ: 5,10,20,40,80)
RETRY_BACKOFF_MAX: Final[int] = 600
STUCK_PROCESSING_SECONDS: Final[int] = 300       # 5分以上 PROCESSING → 異常候補
QUEUE_IDLE_SLEEP: Final[float] = 3.0             # キューが空のときの待機秒

# ユーザーごとのレート制限
RATE_LIMIT_CHARGE_COUNT: Final[int] = 5          # チャージ開始
RATE_LIMIT_CHARGE_WINDOW: Final[int] = 60        # 秒
RATE_LIMIT_BUTTON_COUNT: Final[int] = 20         # 一般ボタン操作
RATE_LIMIT_BUTTON_WINDOW: Final[int] = 60

# ---------------------------------------------------------------------------
# Kyash 通信
# ---------------------------------------------------------------------------
# 添付モジュールは requests を同期実行し timeout を指定していないため、
# socket のデフォルトタイムアウトで上限を強制し、さらに asyncio 側でも待ち時間を制限する。
KYASH_SOCKET_TIMEOUT: Final[float] = 30.0
KYASH_CALL_BUDGET: Final[float] = 75.0           # asyncio.wait_for の上限 (socket timeout より長く取る)
KYASH_HEALTH_INTERVAL: Final[int] = 300          # セッション健康確認間隔 (秒)
KYASH_LOGIN_PENDING_TTL: Final[int] = 600        # OTP 入力待ちの保持時間 (秒)
KYASH_RECEIPT_RECHECK_DELAY: Final[float] = 4.0  # 受取確認が取れないときの再確認待ち
KYASH_HISTORY_LIMIT: Final[int] = 30             # 受取確認で参照する履歴件数

# ---------------------------------------------------------------------------
# バックグラウンドタスク間隔 (秒)
# ---------------------------------------------------------------------------
TASK_EXPIRE_INTERVAL: Final[int] = 60
TASK_STUCK_INTERVAL: Final[int] = 120
TASK_NOTIFICATION_INTERVAL: Final[int] = 60
TASK_RANKING_INTERVAL: Final[int] = 60           # 各Guildの ranking_interval を満たしたものだけ更新
TASK_BACKUP_INTERVAL: Final[int] = 3600
TASK_INTEGRITY_INTERVAL: Final[int] = 21600
BACKUP_MIN_INTERVAL: Final[int] = 86400          # 最低1日1回
BACKUP_KEEP: Final[int] = 14                     # 保持世代数

RANKING_DEBOUNCE_SECONDS: Final[float] = 1.5     # 残高変更の連続発生をまとめる
NOTIFICATION_MAX_ATTEMPTS: Final[int] = 5

# ---------------------------------------------------------------------------
# Transaction 状態
# ---------------------------------------------------------------------------
class TxStatus:
    CREATED = "CREATED"
    WAITING_LINK = "WAITING_LINK"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    RECEIVED = "RECEIVED"
    CREDITING = "CREDITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


#: 未完了 (何らかの後続処理が必要) とみなす状態
ACTIVE_STATUSES: Final[tuple[str, ...]] = (
    TxStatus.CREATED,
    TxStatus.WAITING_LINK,
    TxStatus.VALIDATING,
    TxStatus.QUEUED,
    TxStatus.PROCESSING,
    TxStatus.RECEIVED,
    TxStatus.CREDITING,
    TxStatus.MANUAL_REVIEW,
)

#: 終了状態
TERMINAL_STATUSES: Final[tuple[str, ...]] = (
    TxStatus.COMPLETED,
    TxStatus.FAILED,
    TxStatus.CANCELLED,
    TxStatus.EXPIRED,
)

#: 日次上限の計算対象 (失敗・キャンセル・期限切れは消費しない)
DAILY_LIMIT_STATUSES: Final[tuple[str, ...]] = (
    TxStatus.QUEUED,
    TxStatus.PROCESSING,
    TxStatus.RECEIVED,
    TxStatus.CREDITING,
    TxStatus.COMPLETED,
    TxStatus.MANUAL_REVIEW,
)

#: 許可された状態遷移のみを実行する (不正遷移は禁止)
ALLOWED_TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    TxStatus.CREATED: (TxStatus.WAITING_LINK, TxStatus.CANCELLED, TxStatus.EXPIRED, TxStatus.FAILED),
    TxStatus.WAITING_LINK: (TxStatus.VALIDATING, TxStatus.CANCELLED, TxStatus.EXPIRED, TxStatus.FAILED),
    TxStatus.VALIDATING: (TxStatus.QUEUED, TxStatus.WAITING_LINK, TxStatus.FAILED, TxStatus.CANCELLED, TxStatus.EXPIRED),
    TxStatus.QUEUED: (TxStatus.PROCESSING, TxStatus.FAILED, TxStatus.CANCELLED, TxStatus.EXPIRED, TxStatus.MANUAL_REVIEW),
    TxStatus.PROCESSING: (TxStatus.RECEIVED, TxStatus.QUEUED, TxStatus.FAILED, TxStatus.MANUAL_REVIEW),
    TxStatus.RECEIVED: (TxStatus.CREDITING, TxStatus.MANUAL_REVIEW),
    TxStatus.CREDITING: (TxStatus.COMPLETED, TxStatus.MANUAL_REVIEW),
    TxStatus.MANUAL_REVIEW: (TxStatus.RECEIVED, TxStatus.CREDITING, TxStatus.COMPLETED, TxStatus.FAILED, TxStatus.QUEUED),
    TxStatus.COMPLETED: (),
    TxStatus.FAILED: (),
    TxStatus.CANCELLED: (),
    TxStatus.EXPIRED: (),
}

STATUS_LABELS: Final[dict[str, str]] = {
    TxStatus.CREATED: "作成済み",
    TxStatus.WAITING_LINK: "リンク待ち",
    TxStatus.VALIDATING: "検証中",
    TxStatus.QUEUED: "受取待ち",
    TxStatus.PROCESSING: "受取処理中",
    TxStatus.RECEIVED: "受取完了",
    TxStatus.CREDITING: "残高付与中",
    TxStatus.COMPLETED: "完了",
    TxStatus.FAILED: "失敗",
    TxStatus.CANCELLED: "キャンセル",
    TxStatus.EXPIRED: "期限切れ",
    TxStatus.MANUAL_REVIEW: "確認中",
}

STATUS_EMOJI: Final[dict[str, str]] = {
    TxStatus.CREATED: "⚪",
    TxStatus.WAITING_LINK: "⌛",
    TxStatus.VALIDATING: "🔍",
    TxStatus.QUEUED: "🟡",
    TxStatus.PROCESSING: "🟡",
    TxStatus.RECEIVED: "🔵",
    TxStatus.CREDITING: "🔵",
    TxStatus.COMPLETED: "🟢",
    TxStatus.FAILED: "🔴",
    TxStatus.CANCELLED: "⚫",
    TxStatus.EXPIRED: "⚫",
    TxStatus.MANUAL_REVIEW: "🟠",
}


# ---------------------------------------------------------------------------
# Kyash 受取アカウント状態
# ---------------------------------------------------------------------------
class KyashAccountStatus:
    UNCONFIGURED = "UNCONFIGURED"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ERROR = "ERROR"


KYASH_STATUS_LABELS: Final[dict[str, str]] = {
    KyashAccountStatus.UNCONFIGURED: "⚪ 未登録",
    KyashAccountStatus.ACTIVE: "🟢 正常",
    KyashAccountStatus.INACTIVE: "⚫ 停止中",
    KyashAccountStatus.AUTH_REQUIRED: "🟠 再認証が必要",
    KyashAccountStatus.ERROR: "🔴 エラー",
}


# ---------------------------------------------------------------------------
# 残高変更種別 (balance_history.type)
# ---------------------------------------------------------------------------
class BalanceChangeType:
    CHARGE = "CHARGE"
    ADMIN_ADD = "ADMIN_ADD"
    ADMIN_REMOVE = "ADMIN_REMOVE"
    ADMIN_SET = "ADMIN_SET"
    PROXY_ACHIEVEMENT = "PROXY_ACHIEVEMENT"


BALANCE_TYPE_LABELS: Final[dict[str, str]] = {
    BalanceChangeType.CHARGE: "チャージ",
    BalanceChangeType.ADMIN_ADD: "管理者加算",
    BalanceChangeType.ADMIN_REMOVE: "管理者減算",
    BalanceChangeType.ADMIN_SET: "管理者設定",
    BalanceChangeType.PROXY_ACHIEVEMENT: "代理実績",
}


class TxSource:
    AUTOMATIC = "AUTOMATIC"
    ADMIN_PROXY = "ADMIN_PROXY"


# ---------------------------------------------------------------------------
# エラーコード
# ---------------------------------------------------------------------------
class ErrorCode:
    INVALID_AMOUNT = "INVALID_AMOUNT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    AMOUNT_BELOW_MIN = "AMOUNT_BELOW_MIN"
    AMOUNT_ABOVE_MAX = "AMOUNT_ABOVE_MAX"
    DAILY_LIMIT_EXCEEDED = "DAILY_LIMIT_EXCEEDED"
    GUILD_DAILY_LIMIT_EXCEEDED = "GUILD_DAILY_LIMIT_EXCEEDED"
    INVALID_LINK = "INVALID_LINK"
    LINK_IS_CLAIM = "LINK_IS_CLAIM"
    LINK_EXPIRED = "LINK_EXPIRED"
    LINK_ALREADY_USED = "LINK_ALREADY_USED"
    KYASH_AUTH_ERROR = "KYASH_AUTH_ERROR"
    KYASH_TIMEOUT = "KYASH_TIMEOUT"
    KYASH_NETWORK_ERROR = "KYASH_NETWORK_ERROR"
    KYASH_REJECTED = "KYASH_REJECTED"
    KYASH_UNAVAILABLE = "KYASH_UNAVAILABLE"
    DATABASE_ERROR = "DATABASE_ERROR"
    USER_FROZEN = "USER_FROZEN"
    GUILD_DISABLED = "GUILD_DISABLED"
    MAINTENANCE = "MAINTENANCE"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    TRANSACTION_EXPIRED = "TRANSACTION_EXPIRED"
    ACTIVE_TRANSACTION_EXISTS = "ACTIVE_TRANSACTION_EXISTS"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_ALLOWED = "NOT_ALLOWED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


#: 利用者向け日本語メッセージ (内部詳細・Stack Trace は出さない)
USER_ERROR_MESSAGES: Final[dict[str, str]] = {
    ErrorCode.INVALID_AMOUNT: "金額の入力が正しくありません。半角数字で入力してください。",
    ErrorCode.AMOUNT_MISMATCH: "入力した金額と送金リンクの金額が一致しません。金額を確認してやり直してください。",
    ErrorCode.AMOUNT_BELOW_MIN: "最低チャージ額を下回っています。",
    ErrorCode.AMOUNT_ABOVE_MAX: "最大チャージ額を超えています。",
    ErrorCode.DAILY_LIMIT_EXCEEDED: "本日のチャージ上限に達しています。日付が変わってからお試しください。",
    ErrorCode.GUILD_DAILY_LIMIT_EXCEEDED: "サーバー全体の本日のチャージ上限に達しています。",
    ErrorCode.INVALID_LINK: "送金リンクを確認できませんでした。有効なKyashの送金リンクを送信してください。",
    ErrorCode.LINK_IS_CLAIM: "このリンクは請求リンクです。送金リンクを作成して送信してください。",
    ErrorCode.LINK_EXPIRED: "この送金リンクは利用できません。新しいリンクを作成してください。",
    ErrorCode.LINK_ALREADY_USED: "この送金リンクは既に使用されています。",
    ErrorCode.KYASH_AUTH_ERROR: "現在チャージを受け付けられません。時間をおいて再度お試しください。",
    ErrorCode.KYASH_TIMEOUT: "処理に時間がかかっています。状況を確認していますので、そのままお待ちください。",
    ErrorCode.KYASH_NETWORK_ERROR: "通信エラーが発生しました。時間をおいて再度お試しください。",
    ErrorCode.KYASH_REJECTED: "送金リンクの受け取りに失敗しました。リンクの状態を確認してください。",
    ErrorCode.KYASH_UNAVAILABLE: "現在チャージ機能を利用できません。管理者にお問い合わせください。",
    ErrorCode.DATABASE_ERROR: "内部エラーが発生しました。管理者にお問い合わせください。",
    ErrorCode.USER_FROZEN: "あなたのアカウントは現在チャージを利用できません。管理者にお問い合わせください。",
    ErrorCode.GUILD_DISABLED: "このサーバーではBotの利用が許可されていません。",
    ErrorCode.MAINTENANCE: "現在メンテナンス中のため、新規チャージを受け付けていません。",
    ErrorCode.EMERGENCY_STOP: "現在システムを緊急停止しています。復旧までお待ちください。",
    ErrorCode.TRANSACTION_EXPIRED: "チャージの有効期限が切れました。最初からやり直してください。",
    ErrorCode.ACTIVE_TRANSACTION_EXISTS: "進行中のチャージがあります。完了するかキャンセルしてから再度お試しください。",
    ErrorCode.RATE_LIMITED: "操作が多すぎます。少し時間をおいてから再度お試しください。",
    ErrorCode.NOT_ALLOWED: "この操作を行う権限がありません。",
    ErrorCode.MANUAL_REVIEW: "現在処理結果を確認しています。確認が完了するまでお待ちください。",
    ErrorCode.UNKNOWN_ERROR: "予期しないエラーが発生しました。管理者にお問い合わせください。",
}

#: 再試行してはいけないエラー
NON_RETRYABLE_ERRORS: Final[frozenset[str]] = frozenset({
    ErrorCode.INVALID_AMOUNT,
    ErrorCode.AMOUNT_MISMATCH,
    ErrorCode.AMOUNT_BELOW_MIN,
    ErrorCode.AMOUNT_ABOVE_MAX,
    ErrorCode.DAILY_LIMIT_EXCEEDED,
    ErrorCode.GUILD_DAILY_LIMIT_EXCEEDED,
    ErrorCode.INVALID_LINK,
    ErrorCode.LINK_IS_CLAIM,
    ErrorCode.LINK_EXPIRED,
    ErrorCode.LINK_ALREADY_USED,
    ErrorCode.USER_FROZEN,
    ErrorCode.GUILD_DISABLED,
    ErrorCode.TRANSACTION_EXPIRED,
    ErrorCode.KYASH_REJECTED,
})

# ---------------------------------------------------------------------------
# UI カラー (黒・ダーク・シンプル・高級感 / 紫一色にしない)
# ---------------------------------------------------------------------------
class Color:
    BASE = 0x1B1B1F        # ダークベース
    ACCENT = 0xC9A227      # ゴールド (高級感)
    SUCCESS = 0x2D9A6B     # 深緑
    WARNING = 0xD9902B     # アンバー
    DANGER = 0xB03A34      # 深紅
    INFO = 0x3C6E9E        # 落ち着いた青
    NEUTRAL = 0x2B2D31     # Discord ダーク背景に近い色
    RANKING = 0xC9A227


RANK_MEDALS: Final[dict[int, str]] = {1: "🥇", 2: "🥈", 3: "🥉"}

# ---------------------------------------------------------------------------
# Persistent View custom_id (Bot 再起動後もボタンが動くよう固定値にする)
# ---------------------------------------------------------------------------
class CustomID:
    CHARGE_START = "chargebot:charge:start"
    CHARGE_BALANCE = "chargebot:charge:balance"
    CHARGE_HISTORY = "chargebot:charge:history"
    CHARGE_HELP = "chargebot:charge:help"
    CHARGE_REFRESH = "chargebot:charge:refresh"
    RANKING_REFRESH = "chargebot:ranking:refresh"
    RANKING_MYRANK = "chargebot:ranking:myrank"


PANEL_TYPE_CHARGE: Final[str] = "CHARGE"

# ---------------------------------------------------------------------------
# ロガー名
# ---------------------------------------------------------------------------
LOGGER_BOT = "bot"
LOGGER_DB = "bot.database"
LOGGER_CHARGE = "bot.charge"
LOGGER_KYASH = "bot.kyash"
LOGGER_QUEUE = "bot.queue"
LOGGER_DISCORD_EVENTS = "bot.discord"
LOGGER_AUDIT = "bot.audit"
LOGGER_TASKS = "bot.tasks"
