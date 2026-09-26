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
BOT_VERSION: Final[str] = "2.1.0"
SCHEMA_VERSION: Final[int] = 2

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
DEFAULT_MAX_BALANCE: Final[int] = 0              # 1ユーザーの残高上限 (0=無制限)
DEFAULT_BALANCE_LOG_SCOPE: Final[str] = "MANUAL"  # MANUAL=手動操作のみ / ALL=全変動

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


class RankingType:
    """ランキングパネルの集計方式 (パネルごとに選択できる)。"""

    BALANCE = "BALANCE"    # 現在の内部残高 (balances が Source of Truth)
    WEEKLY = "WEEKLY"      # 直近7日のチャージ獲得残高
    MONTHLY = "MONTHLY"    # 当月のチャージ獲得残高
    INVITE = "INVITE"      # 招待キャンペーンの確定招待数


RANKING_TYPE_LABELS: Final[dict[str, str]] = {
    RankingType.BALANCE: "残高ランキング",
    RankingType.WEEKLY: "週間チャージランキング",
    RankingType.MONTHLY: "月間チャージランキング",
    RankingType.INVITE: "招待ランキング",
}

RANKING_TYPE_TITLES: Final[dict[str, str]] = {
    RankingType.BALANCE: "🏆 SERVER BALANCE RANKING",
    RankingType.WEEKLY: "📈 WEEKLY CHARGE RANKING",
    RankingType.MONTHLY: "📅 MONTHLY CHARGE RANKING",
    RankingType.INVITE: "🤝 INVITE RANKING",
}

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

# 連続失敗によるクールダウン (不正探索・いたずら対策)
FAILURE_COOLDOWN_THRESHOLD: Final[int] = 5       # 連続失敗回数
FAILURE_COOLDOWN_SECONDS: Final[int] = 600       # クールダウン時間 (秒)
FAILURE_COOLDOWN_WINDOW: Final[int] = 1800       # 連続と見なす時間窓 (秒)

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
#: アクセストークンの有効期間 (上流仕様: 発行から1ヶ月)
KYASH_TOKEN_LIFETIME_DAYS: Final[int] = 30
#: 失効の何日前から警告するか
KYASH_TOKEN_WARN_DAYS: Final[int] = 5
#: 受取用アカウントの残高しきい値の既定 (0=無効)。超過で管理者へ通知し新規チャージを止める
DEFAULT_WALLET_ALERT_THRESHOLD: Final[int] = 0

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
TASK_SHOP_EXPIRY_INTERVAL: Final[int] = 300       # 期限付きロールの剥奪確認
TASK_HEARTBEAT_INTERVAL: Final[int] = 60          # 死活監視の更新間隔
TASK_SUMMARY_INTERVAL: Final[int] = 300           # 日次サマリの投稿判定間隔
TASK_CAMPAIGN_INTERVAL: Final[int] = 600          # キャンペーン期限・保留の確認間隔
#: MANUAL_REVIEW が解決されないまま放置された場合の再通知間隔 (秒)
MANUAL_REVIEW_ESCALATION_SECONDS: Final[int] = 1800
#: 日次サマリを投稿する JST の時刻 (時, 分)
SUMMARY_POST_HOUR: Final[int] = 0
SUMMARY_POST_MINUTE: Final[int] = 5

RANKING_DEBOUNCE_SECONDS: Final[float] = 1.5     # 残高変更の連続発生をまとめる


class BackupRemote:
    """バックアップの外部保存方式。"""

    NONE = "NONE"                # ローカルのみ
    OWNER_DM = "OWNER_DM"        # Bot Owner の DM へ添付
    SECONDARY_DIR = "SECONDARY_DIR"  # 別ディレクトリ (マウント先など) へコピー


#: Discord の添付ファイル上限に対する安全マージン (8MiB)
BACKUP_DM_MAX_BYTES: Final[int] = 8 * 1024 * 1024
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
    ADMIN_MOVE_OUT = "ADMIN_MOVE_OUT"    # 管理者による付け替え (減算側)
    ADMIN_MOVE_IN = "ADMIN_MOVE_IN"      # 管理者による付け替え (加算側)
    SPEND = "SPEND"                      # ショップ購入などの消費
    SPEND_REFUND = "SPEND_REFUND"        # 消費の返金
    INVITE_REWARD = "INVITE_REWARD"      # 招待キャンペーン報酬
    REVERSAL = "REVERSAL"                # 取引の取消 (逆仕訳)
    UNDO = "UNDO"                        # 残高操作の取消 (逆仕訳)
    RECONCILE = "RECONCILE"              # 履歴との突合による修復


BALANCE_TYPE_LABELS: Final[dict[str, str]] = {
    BalanceChangeType.CHARGE: "チャージ",
    BalanceChangeType.ADMIN_ADD: "管理者加算",
    BalanceChangeType.ADMIN_REMOVE: "管理者減算",
    BalanceChangeType.ADMIN_SET: "管理者設定",
    BalanceChangeType.PROXY_ACHIEVEMENT: "代理実績",
    BalanceChangeType.ADMIN_MOVE_OUT: "付け替え (出金)",
    BalanceChangeType.ADMIN_MOVE_IN: "付け替え (入金)",
    BalanceChangeType.SPEND: "消費",
    BalanceChangeType.SPEND_REFUND: "消費の返金",
    BalanceChangeType.INVITE_REWARD: "招待報酬",
    BalanceChangeType.REVERSAL: "取引取消",
    BalanceChangeType.UNDO: "操作取消",
    BalanceChangeType.RECONCILE: "突合修復",
}

#: 残高を増やす種別 (max_balance の判定に使う)
BALANCE_INCREASE_TYPES: Final[frozenset[str]] = frozenset({
    BalanceChangeType.CHARGE, BalanceChangeType.ADMIN_ADD, BalanceChangeType.ADMIN_SET,
    BalanceChangeType.PROXY_ACHIEVEMENT, BalanceChangeType.ADMIN_MOVE_IN,
    BalanceChangeType.SPEND_REFUND, BalanceChangeType.INVITE_REWARD,
})

#: 管理者の手動操作として残高ログへ流す種別
MANUAL_BALANCE_TYPES: Final[frozenset[str]] = frozenset({
    BalanceChangeType.ADMIN_ADD, BalanceChangeType.ADMIN_REMOVE, BalanceChangeType.ADMIN_SET,
    BalanceChangeType.PROXY_ACHIEVEMENT, BalanceChangeType.ADMIN_MOVE_OUT,
    BalanceChangeType.ADMIN_MOVE_IN, BalanceChangeType.REVERSAL, BalanceChangeType.UNDO,
    BalanceChangeType.RECONCILE,
})


class TxSource:
    AUTOMATIC = "AUTOMATIC"
    ADMIN_PROXY = "ADMIN_PROXY"


# ---------------------------------------------------------------------------
# ショップ (内部残高でロールを購入する)
# ---------------------------------------------------------------------------
class PurchaseStatus:
    PENDING = "PENDING"            # 残高を引き落とし、ロール付与待ち
    ACTIVE = "ACTIVE"              # 付与済み (期限内)
    EXPIRED = "EXPIRED"            # 期限切れでロール剥奪済み
    REFUNDED = "REFUNDED"          # 返金済み (ロール剥奪)
    FAILED = "FAILED"              # ロール付与に失敗し自動返金済み


PURCHASE_STATUS_LABELS: Final[dict[str, str]] = {
    PurchaseStatus.PENDING: "⌛ 付与待ち",
    PurchaseStatus.ACTIVE: "🟢 有効",
    PurchaseStatus.EXPIRED: "⚫ 期限切れ",
    PurchaseStatus.REFUNDED: "↩️ 返金済み",
    PurchaseStatus.FAILED: "🔴 失敗 (返金済み)",
}

SHOP_PRICE_MIN: Final[int] = 1
SHOP_PRICE_MAX: Final[int] = 1_000_000_000
SHOP_DURATION_MAX_DAYS: Final[int] = 3650
#: 商品名・説明・キャンペーン名の保存長。Embed の上限内に必ず収まる値にする。
SHOP_NAME_MAX_LEN: Final[int] = 100
SHOP_DESC_MAX_LEN: Final[int] = 500
CAMPAIGN_NAME_MAX_LEN: Final[int] = 100


# ---------------------------------------------------------------------------
# 招待キャンペーン
# ---------------------------------------------------------------------------
class CampaignStatus:
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class InviteStatus:
    PENDING = "PENDING"        # 参加を記録。報酬は未確定
    CONFIRMED = "CONFIRMED"    # 条件達成により報酬を付与済み
    HOLD = "HOLD"              # 不正の疑いがあり管理者レビュー待ち
    REJECTED = "REJECTED"      # 条件を満たさない / 却下


INVITE_STATUS_LABELS: Final[dict[str, str]] = {
    InviteStatus.PENDING: "⌛ 保留 (条件待ち)",
    InviteStatus.CONFIRMED: "🟢 確定",
    InviteStatus.HOLD: "🟠 要確認",
    InviteStatus.REJECTED: "🔴 無効",
}


class InviteRejectReason:
    """招待が無効・保留になる理由 (監査と管理者表示に使う)。"""

    SELF_INVITE = "SELF_INVITE"
    BOT_ACCOUNT = "BOT_ACCOUNT"
    REJOIN = "REJOIN"                      # 過去に在籍していた (退出→再入場)
    ALREADY_INVITED = "ALREADY_INVITED"    # 既に被招待者として記録済み
    ACCOUNT_TOO_NEW = "ACCOUNT_TOO_NEW"
    DAILY_LIMIT = "DAILY_LIMIT"
    TOTAL_LIMIT = "TOTAL_LIMIT"
    BLACKLISTED = "BLACKLISTED"
    UNKNOWN_INVITER = "UNKNOWN_INVITER"    # バニティURL等で招待者を特定できない
    AMBIGUOUS = "AMBIGUOUS"                # 同時に複数の招待が使われ特定できない
    SUSPICIOUS_BURST = "SUSPICIOUS_BURST"  # 短時間の大量参加
    SUSPICIOUS_AGE = "SUSPICIOUS_AGE"      # 招待者と被招待者の作成日が近すぎる
    CAMPAIGN_ENDED = "CAMPAIGN_ENDED"


INVITE_REASON_LABELS: Final[dict[str, str]] = {
    InviteRejectReason.SELF_INVITE: "自己招待",
    InviteRejectReason.BOT_ACCOUNT: "Botアカウント",
    InviteRejectReason.REJOIN: "再入場 (過去に在籍)",
    InviteRejectReason.ALREADY_INVITED: "既に被招待者として記録済み",
    InviteRejectReason.ACCOUNT_TOO_NEW: "アカウント作成が新しすぎる",
    InviteRejectReason.DAILY_LIMIT: "招待者の日次上限に到達",
    InviteRejectReason.TOTAL_LIMIT: "招待者の累計上限に到達",
    InviteRejectReason.BLACKLISTED: "ブラックリスト対象",
    InviteRejectReason.UNKNOWN_INVITER: "招待者を特定できない",
    InviteRejectReason.AMBIGUOUS: "招待の特定が曖昧",
    InviteRejectReason.SUSPICIOUS_BURST: "短時間の大量参加を検知",
    InviteRejectReason.SUSPICIOUS_AGE: "アカウント作成日が近接",
    InviteRejectReason.CAMPAIGN_ENDED: "キャンペーン期間外",
}

#: しきい値プリセット (標準を既定とする)
CAMPAIGN_PRESETS: Final[dict[str, dict[str, int]]] = {
    "STRICT": {"min_account_age_days": 30, "daily_limit": 3, "total_limit": 20,
               "require_review": 1, "require_days": 0},
    "STANDARD": {"min_account_age_days": 7, "daily_limit": 5, "total_limit": 50,
                 "require_review": 0, "require_days": 0},
    "LOOSE": {"min_account_age_days": 1, "daily_limit": 10, "total_limit": 200,
              "require_review": 0, "require_days": 0},
}
DEFAULT_CAMPAIGN_PRESET: Final[str] = "STANDARD"
DEFAULT_INVITER_REWARD: Final[int] = 500
DEFAULT_INVITED_REWARD: Final[int] = 300
#: 短時間の大量参加を疑う判定 (同一招待者で N 秒以内に M 件)
INVITE_BURST_WINDOW: Final[int] = 300
INVITE_BURST_COUNT: Final[int] = 4
#: 招待者と被招待者の Discord アカウント作成日がこの日数以内なら要確認
INVITE_AGE_PROXIMITY_DAYS: Final[int] = 2


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
    COOLDOWN = "COOLDOWN"
    MAX_BALANCE_EXCEEDED = "MAX_BALANCE_EXCEEDED"
    WALLET_LIMIT = "WALLET_LIMIT"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    SHOP_ITEM_UNAVAILABLE = "SHOP_ITEM_UNAVAILABLE"
    SHOP_OUT_OF_STOCK = "SHOP_OUT_OF_STOCK"
    SHOP_ALREADY_OWNED = "SHOP_ALREADY_OWNED"
    SHOP_LIMIT_REACHED = "SHOP_LIMIT_REACHED"
    ROLE_ASSIGN_FAILED = "ROLE_ASSIGN_FAILED"
    CAMPAIGN_NOT_ACTIVE = "CAMPAIGN_NOT_ACTIVE"
    INVITE_NOT_AVAILABLE = "INVITE_NOT_AVAILABLE"
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
    ErrorCode.COOLDOWN: "エラーが続いたため、一時的に操作を制限しています。しばらく待ってからお試しください。",
    ErrorCode.MAX_BALANCE_EXCEEDED: "残高の上限に達するため、このチャージは受け付けられません。",
    ErrorCode.WALLET_LIMIT: "現在チャージを受け付けられません。時間をおいてお試しください。",
    ErrorCode.INSUFFICIENT_BALANCE: "残高が不足しています。",
    ErrorCode.SHOP_ITEM_UNAVAILABLE: "この商品は現在購入できません。",
    ErrorCode.SHOP_OUT_OF_STOCK: "この商品は在庫切れです。",
    ErrorCode.SHOP_ALREADY_OWNED: "すでにこのロールを所持しています。",
    ErrorCode.SHOP_LIMIT_REACHED: "この商品の購入上限に達しています。",
    ErrorCode.ROLE_ASSIGN_FAILED: "ロールの付与に失敗したため、残高を返金しました。管理者にお問い合わせください。",
    ErrorCode.CAMPAIGN_NOT_ACTIVE: "現在開催中の招待キャンペーンはありません。",
    ErrorCode.INVITE_NOT_AVAILABLE: "招待リンクを発行できませんでした。管理者にお問い合わせください。",
    ErrorCode.UNKNOWN_ERROR: "予期しないエラーが発生しました。管理者にお問い合わせください。",
}

#: 利用者向けの「次にどうすればよいか」。エラー表示に添えて迷わせない。
USER_ERROR_NEXT_ACTIONS: Final[dict[str, str]] = {
    ErrorCode.INVALID_AMOUNT: "もう一度 `💰 チャージ` を押して、半角数字だけで金額を入力してください。",
    ErrorCode.AMOUNT_BELOW_MIN: "パネルに表示されている最低チャージ額以上の金額で、もう一度お試しください。",
    ErrorCode.AMOUNT_ABOVE_MAX: "パネルに表示されている最大チャージ額以下に分けて、もう一度お試しください。",
    ErrorCode.AMOUNT_MISMATCH: "入力した金額と**同じ金額**の送金リンクを作り直し、最初からやり直してください。",
    ErrorCode.DAILY_LIMIT_EXCEEDED: "日付が変わると上限がリセットされます。明日以降にお試しください。",
    ErrorCode.GUILD_DAILY_LIMIT_EXCEEDED: "サーバー全体の上限です。時間をおいてお試しください。",
    ErrorCode.INVALID_LINK: "Kyash アプリで**送金リンクを新しく作成**し、URL をそのまま貼ってください。",
    ErrorCode.LINK_IS_CLAIM: "「送る」から作成した**送金リンク**を使ってください (請求リンクは使えません)。",
    ErrorCode.LINK_EXPIRED: "Kyash アプリで新しい送金リンクを作成してください。",
    ErrorCode.LINK_ALREADY_USED: "新しい送金リンクを作成して、もう一度お試しください。",
    ErrorCode.KYASH_TIMEOUT: "この取引はまだ有効です。少し待ってから同じリンクを再送信できます。",
    ErrorCode.KYASH_NETWORK_ERROR: "この取引はまだ有効です。少し待ってから同じリンクを再送信できます。",
    ErrorCode.KYASH_AUTH_ERROR: "復旧までしばらくお待ちください。送金リンクはまだ使えます。",
    ErrorCode.KYASH_UNAVAILABLE: "受付が再開されるまでお待ちください。",
    ErrorCode.WALLET_LIMIT: "受付が再開されるまでお待ちください。管理者が対応します。",
    ErrorCode.MAINTENANCE: "メンテナンス終了までお待ちください。残高と履歴はいつでも確認できます。",
    ErrorCode.EMERGENCY_STOP: "復旧までお待ちください。残高は保持されています。",
    ErrorCode.TRANSACTION_EXPIRED: "もう一度 `💰 チャージ` から始めてください。",
    ErrorCode.ACTIVE_TRANSACTION_EXISTS: "`💰 チャージ` を押すと進行中の手続きを再開できます。",
    ErrorCode.RATE_LIMITED: "1分ほど待ってから、もう一度お試しください。",
    ErrorCode.COOLDOWN: "時間をおいてから再度お試しください。解除は管理者に依頼できます。",
    ErrorCode.MANUAL_REVIEW: "確認が終わると DM でお知らせします。そのままお待ちください。",
    ErrorCode.MAX_BALANCE_EXCEEDED: "残高を使ってから、もう一度お試しください。",
    ErrorCode.INSUFFICIENT_BALANCE: "チャージして残高を増やしてから、もう一度お試しください。",
    ErrorCode.SHOP_ALREADY_OWNED: "すでに所持しているため購入は不要です。",
    ErrorCode.SHOP_OUT_OF_STOCK: "在庫が補充されるまでお待ちください。",
    ErrorCode.SHOP_LIMIT_REACHED: "この商品はこれ以上購入できません。",
    ErrorCode.ROLE_ASSIGN_FAILED: "残高は返金済みです。管理者へお問い合わせください。",
    ErrorCode.CAMPAIGN_NOT_ACTIVE: "キャンペーンが始まるまでお待ちください。",
    ErrorCode.USER_FROZEN: "サーバーの管理者へお問い合わせください。",
    ErrorCode.KYASH_REJECTED: "新しい送金リンクを作成して、もう一度お試しください。",
    ErrorCode.SHOP_ITEM_UNAVAILABLE: "他の商品をお試しいただくか、管理者へお問い合わせください。",
    ErrorCode.INVITE_NOT_AVAILABLE: "サーバーの管理者へお問い合わせください。",
    ErrorCode.NOT_ALLOWED: "このサーバーではまだ使えません。サーバーの管理者に有効化を依頼してください。",
    ErrorCode.GUILD_DISABLED: "このサーバーの利用が停止されています。サーバーの管理者にお問い合わせください。",
    ErrorCode.DATABASE_ERROR: "時間をおいてもう一度お試しください。続く場合は管理者にお知らせください。",
    ErrorCode.UNKNOWN_ERROR: "時間をおいてもう一度お試しください。続く場合は取引IDを添えて管理者にお知らせください。",
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
    ErrorCode.MAX_BALANCE_EXCEEDED,
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
    SHOP_OPEN = "chargebot:shop:open"
    SHOP_MYITEMS = "chargebot:shop:myitems"
    INVITE_GET = "chargebot:invite:get"
    INVITE_STATUS = "chargebot:invite:status"
    INVITE_RANK = "chargebot:invite:rank"
    ADMIN_REFRESH = "chargebot:admin:refresh"
    ADMIN_MAINTENANCE = "chargebot:admin:maintenance"
    ADMIN_QUEUE = "chargebot:admin:queue"
    ADMIN_REVIEW = "chargebot:admin:review"
    CHARGE_BALANCE = "chargebot:charge:balance"
    CHARGE_HISTORY = "chargebot:charge:history"
    CHARGE_HELP = "chargebot:charge:help"
    CHARGE_REFRESH = "chargebot:charge:refresh"
    RANKING_REFRESH = "chargebot:ranking:refresh"
    RANKING_MYRANK = "chargebot:ranking:myrank"


#: 実績チャンネルへ投稿する種別
class AchievementKind:
    CHARGE = "CHARGE"
    SHOP = "SHOP"
    INVITE = "INVITE"


PANEL_TYPE_CHARGE: Final[str] = "CHARGE"
PANEL_TYPE_SHOP: Final[str] = "SHOP"
PANEL_TYPE_INVITE: Final[str] = "INVITE"
PANEL_TYPE_ADMIN: Final[str] = "ADMIN"

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
LOGGER_SHOP = "bot.shop"
LOGGER_INVITE = "bot.invite"

# ---------------------------------------------------------------------------
# ログ出力形式 ("text" または "json")
# ---------------------------------------------------------------------------
LOG_FORMAT: Final[str] = "text"
