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
BOT_VERSION: Final[str] = "4.0.0"
SCHEMA_VERSION: Final[int] = 4

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
TASK_SHOP_EXPIRY_INTERVAL: Final[int] = 300
#: チャージ申請の期限処理・催促の間隔 (秒)
TASK_REQUEST_INTERVAL: Final[int] = 300       # 期限付きロールの剥奪確認
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
    #: 請求リンクを発行し、利用者の支払いを待っている
    WAITING_PAYMENT = "WAITING_PAYMENT"
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
    TxStatus.WAITING_PAYMENT,
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
    # 請求リンクは Bot が発行し、支払いを確認できたら受取済みとして扱う
    TxStatus.WAITING_PAYMENT: (
        TxStatus.RECEIVED, TxStatus.CREDITING, TxStatus.CANCELLED,
        TxStatus.EXPIRED, TxStatus.FAILED, TxStatus.MANUAL_REVIEW,
    ),
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
    TxStatus.WAITING_PAYMENT: "支払い待ち",
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
    TxStatus.WAITING_PAYMENT: "⌛",
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
    RANKING_REWARD = "RANKING_REWARD"    # ランキング報酬
    GOAL_REWARD = "GOAL_REWARD"          # チャージ目標の達成報酬
    AUCTION_BID = "AUCTION_BID"          # オークションの入札 (引き落とし)
    AUCTION_REFUND = "AUCTION_REFUND"    # 入札を上回られた分の返金
    SUBSCRIPTION = "SUBSCRIPTION"        # サブスク商品の継続課金


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
    BalanceChangeType.RANKING_REWARD: "ランキング報酬",
    BalanceChangeType.GOAL_REWARD: "目標達成報酬",
    BalanceChangeType.AUCTION_BID: "オークション入札",
    BalanceChangeType.AUCTION_REFUND: "入札の返金",
    BalanceChangeType.SUBSCRIPTION: "サブスク課金",
}

#: 残高を増やす種別 (max_balance の判定に使う)
BALANCE_INCREASE_TYPES: Final[frozenset[str]] = frozenset({
    BalanceChangeType.CHARGE, BalanceChangeType.ADMIN_ADD, BalanceChangeType.ADMIN_SET,
    BalanceChangeType.PROXY_ACHIEVEMENT, BalanceChangeType.ADMIN_MOVE_IN,
    BalanceChangeType.SPEND_REFUND, BalanceChangeType.INVITE_REWARD,
    BalanceChangeType.RANKING_REWARD, BalanceChangeType.GOAL_REWARD,
    BalanceChangeType.AUCTION_REFUND,
})

#: 管理者の手動操作として残高ログへ流す種別
MANUAL_BALANCE_TYPES: Final[frozenset[str]] = frozenset({
    BalanceChangeType.ADMIN_ADD, BalanceChangeType.ADMIN_REMOVE, BalanceChangeType.ADMIN_SET,
    BalanceChangeType.PROXY_ACHIEVEMENT, BalanceChangeType.ADMIN_MOVE_OUT,
    BalanceChangeType.ADMIN_MOVE_IN, BalanceChangeType.REVERSAL, BalanceChangeType.UNDO,
    BalanceChangeType.RECONCILE,
})


class TxSource:
    AUTOMATIC = "AUTOMATIC"        # Kyash の自動受取
    ADMIN_PROXY = "ADMIN_PROXY"    # 管理者の代理実績
    KYASH_CLAIM = "KYASH_CLAIM"      # Bot 発行の請求リンクへの支払い
    MANUAL_PAYPAY = "MANUAL_PAYPAY"  # PayPay 申請の承認
    MANUAL_LTC = "MANUAL_LTC"        # Litecoin 申請の承認


#: 取引の出自を利用者向けに表示する文言
TX_SOURCE_LABELS: Final[dict[str, str]] = {
    TxSource.AUTOMATIC: "Kyash (送金リンク)",
    TxSource.KYASH_CLAIM: "Kyash (請求リンク)",
    TxSource.ADMIN_PROXY: "管理者による代理登録",
    TxSource.MANUAL_PAYPAY: "PayPay (承認制)",
    TxSource.MANUAL_LTC: "Litecoin (承認制)",
}


# ---------------------------------------------------------------------------
# チャージ方式 (Kyash は自動 / PayPay・LTC は申請 → 管理者承認)
# ---------------------------------------------------------------------------
class ChargeProvider:
    """チャージ手段。DB の ``charge_requests.provider`` に保存する。"""

    KYASH = "KYASH"              # 利用者が送金リンクを作り、Bot が受け取る
    KYASH_CLAIM = "KYASH_CLAIM"  # Bot が請求リンクを発行し、利用者が支払う
    PAYPAY = "PAYPAY"
    LTC = "LTC"


#: 選択メニュー・パネルに表示する名前
PROVIDER_LABELS: Final[dict[str, str]] = {
    ChargeProvider.KYASH: "Kyash (送金リンク)",
    ChargeProvider.KYASH_CLAIM: "Kyash (請求リンク)",
    ChargeProvider.PAYPAY: "PayPay",
    ChargeProvider.LTC: "Litecoin (LTC)",
}

#: 方式の性質を一言で説明する (利用者が選ぶときの判断材料)
PROVIDER_DESCRIPTIONS: Final[dict[str, str]] = {
    ChargeProvider.KYASH: "自分で送金リンクを作って送る (自動で反映)",
    ChargeProvider.KYASH_CLAIM: "Botが出す請求リンクを支払うだけ (自動で反映・金額ミスなし)",
    ChargeProvider.PAYPAY: "送金後に申請 → 管理者の承認で反映されます",
    ChargeProvider.LTC: "その時のレートで送金 → 申請 → 管理者の承認で反映されます",
}

PROVIDER_EMOJI: Final[dict[str, str]] = {
    ChargeProvider.KYASH: "💰",
    ChargeProvider.KYASH_CLAIM: "🧾",
    ChargeProvider.PAYPAY: "🅿️",
    ChargeProvider.LTC: "Ł",
}

#: すべての方式 (表示順)
ALL_PROVIDERS: Final[tuple[str, ...]] = (
    ChargeProvider.KYASH, ChargeProvider.KYASH_CLAIM,
    ChargeProvider.PAYPAY, ChargeProvider.LTC,
)

#: Kyash の受取用アカウントを使う方式 (どちらも自動で反映される)
KYASH_PROVIDERS: Final[tuple[str, ...]] = (
    ChargeProvider.KYASH, ChargeProvider.KYASH_CLAIM,
)

#: 管理者承認が必要な方式
MANUAL_PROVIDERS: Final[tuple[str, ...]] = (ChargeProvider.PAYPAY, ChargeProvider.LTC)

#: 承認時に作る取引の source
PROVIDER_TX_SOURCE: Final[dict[str, str]] = {
    ChargeProvider.KYASH: TxSource.AUTOMATIC,
    ChargeProvider.KYASH_CLAIM: TxSource.KYASH_CLAIM,
    ChargeProvider.PAYPAY: TxSource.MANUAL_PAYPAY,
    ChargeProvider.LTC: TxSource.MANUAL_LTC,
}

#: 方式ごとに利用者へ求める証拠の名前
PROVIDER_PROOF_LABELS: Final[dict[str, str]] = {
    ChargeProvider.PAYPAY: "PayPay の取引ID",
    ChargeProvider.LTC: "トランザクションID (txid)",
}


class RequestStatus:
    """チャージ申請の状態。"""

    QUOTED = "QUOTED"        # 入金先を案内済み・送金待ち (LTC はレート確定済み)
    PENDING = "PENDING"      # 申請済み・管理者の承認待ち
    APPROVED = "APPROVED"    # 承認され残高付与済み
    REJECTED = "REJECTED"    # 却下 (残高は動かない)
    EXPIRED = "EXPIRED"      # 期限切れ
    CANCELLED = "CANCELLED"  # 利用者が取り消した


REQUEST_STATUS_LABELS: Final[dict[str, str]] = {
    RequestStatus.QUOTED: "⌛ 送金待ち",
    RequestStatus.PENDING: "🟡 承認待ち",
    RequestStatus.APPROVED: "🟢 承認済み",
    RequestStatus.REJECTED: "🔴 却下",
    RequestStatus.EXPIRED: "⚫ 期限切れ",
    RequestStatus.CANCELLED: "⚪ 取消",
}

#: 申請の状態遷移。ここに無い遷移は DB 層で拒否する。
REQUEST_TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    RequestStatus.QUOTED: (
        RequestStatus.PENDING, RequestStatus.EXPIRED, RequestStatus.CANCELLED,
    ),
    RequestStatus.PENDING: (
        RequestStatus.APPROVED, RequestStatus.REJECTED,
        RequestStatus.EXPIRED, RequestStatus.CANCELLED,
    ),
    RequestStatus.APPROVED: (),
    RequestStatus.REJECTED: (),
    RequestStatus.EXPIRED: (),
    RequestStatus.CANCELLED: (),
}

#: 入金先を案内してから送金待ちを打ち切るまで (秒)
QUOTE_WAIT_SECONDS: Final[int] = 30 * 60
#: 申請してから承認されないまま期限切れにするまで (秒)
REQUEST_REVIEW_SECONDS: Final[int] = 72 * 3600
#: 1利用者が同時に持てる未処理申請の数
MAX_OPEN_REQUESTS_PER_USER: Final[int] = 3
#: 申請が未処理のまま この時間を超えたら Owner へ催促する (秒)
REVIEW_REMIND_SECONDS: Final[int] = 6 * 3600

# --- Kyash 請求リンク ---
#: 請求リンクを発行してから支払いを待つ時間 (秒)
CLAIM_WAIT_SECONDS: Final[int] = 20 * 60
#: 支払い確認を自動で回す間隔 (秒)
TASK_CLAIM_INTERVAL: Final[int] = 45
#: 請求リンクに載せるメッセージ (Kyash アプリに表示される)
CLAIM_LINK_MESSAGE: Final[str] = "チャージ"

# --- Litecoin ---
#: LTC の最小単位 (1 litoshi = 1e-8 LTC)
LTC_DECIMALS: Final[int] = 8
#: これ未満は手数料負けするため受け付けない
LTC_MIN_AMOUNT: Final[str] = "0.0005"
#: txid は 64 桁の 16 進数
LTC_TXID_LENGTH: Final[int] = 64

#: LTC/JPY の価格取得元 (CoinGecko の公開エンドポイント)
PRICE_SOURCE_COINGECKO: Final[str] = "COINGECKO"
PRICE_SOURCE_MANUAL: Final[str] = "MANUAL"
PRICE_SOURCE_LABELS: Final[dict[str, str]] = {
    PRICE_SOURCE_COINGECKO: "CoinGecko API (自動)",
    PRICE_SOURCE_MANUAL: "管理者が設定した固定価格",
}
COINGECKO_PRICE_URL: Final[str] = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_LTC_ID: Final[str] = "litecoin"
COINGECKO_VS_CURRENCY: Final[str] = "jpy"
#: 価格 API のタイムアウト・キャッシュ・許容鮮度
PRICE_HTTP_TIMEOUT: Final[float] = 10.0
PRICE_CACHE_SECONDS: Final[int] = 60
PRICE_MAX_AGE_SECONDS: Final[int] = 15 * 60
#: 直近の価格から この割合 (%) 以上跳ねた場合は採用せず Owner へ通知する
PRICE_DEVIATION_GUARD_PERCENT: Final[str] = "35"
#: 価格として受け付ける範囲 (円)。桁違いの応答を弾くための安全弁。
PRICE_MIN_JPY: Final[str] = "100"
PRICE_MAX_JPY: Final[str] = "100000000"


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
    # --- チャージ方式 / 申請 ---
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    DUPLICATE_PROOF = "DUPLICATE_PROOF"
    INVALID_PROOF = "INVALID_PROOF"
    OPEN_REQUEST_LIMIT = "OPEN_REQUEST_LIMIT"
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
    REQUEST_ALREADY_HANDLED = "REQUEST_ALREADY_HANDLED"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    ASSET_AMOUNT_TOO_SMALL = "ASSET_AMOUNT_TOO_SMALL"
    REVIEW_CHANNEL_NOT_SET = "REVIEW_CHANNEL_NOT_SET"
    CLAIM_LINK_FAILED = "CLAIM_LINK_FAILED"
    AUCTION_NOT_OPEN = "AUCTION_NOT_OPEN"
    BID_TOO_LOW = "BID_TOO_LOW"
    ALREADY_HIGHEST = "ALREADY_HIGHEST"
    AUCTION_LIMIT_REACHED = "AUCTION_LIMIT_REACHED"
    GOAL_ALREADY_OPEN = "GOAL_ALREADY_OPEN"
    GOAL_NOT_FOUND = "GOAL_NOT_FOUND"
    FRAUD_FLAG_NOT_FOUND = "FRAUD_FLAG_NOT_FOUND"
    REFUND_NOT_ELIGIBLE = "REFUND_NOT_ELIGIBLE"
    REFUND_ALREADY_REQUESTED = "REFUND_ALREADY_REQUESTED"
    ITEM_INPUT_INVALID = "ITEM_INPUT_INVALID"
    ITEM_SETUP_FAILED = "ITEM_SETUP_FAILED"
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    NO_KYASH_CAPACITY = "NO_KYASH_CAPACITY"
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
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
    ErrorCode.PROVIDER_DISABLED: "この方法でのチャージは現在受け付けていません。",
    ErrorCode.PROVIDER_NOT_CONFIGURED: "この方法はまだ利用できる状態になっていません。",
    ErrorCode.DUPLICATE_PROOF: "その取引は既に申請されています。同じものを二重に申請することはできません。",
    ErrorCode.INVALID_PROOF: "入力された情報の形式が正しくありません。",
    ErrorCode.OPEN_REQUEST_LIMIT: "未処理の申請が多すぎます。先の申請が処理されるまでお待ちください。",
    ErrorCode.REQUEST_NOT_FOUND: "申請が見つかりません。",
    ErrorCode.REQUEST_ALREADY_HANDLED: "その申請は既に処理されています。",
    ErrorCode.QUOTE_EXPIRED: "入金の受付時間が過ぎました。最初からやり直してください。",
    ErrorCode.PRICE_UNAVAILABLE: "レートを取得できないため、現在この方法は利用できません。",
    ErrorCode.ASSET_AMOUNT_TOO_SMALL: "金額が小さすぎます。もう少し大きい金額でお試しください。",
    ErrorCode.REVIEW_CHANNEL_NOT_SET: "この方法はまだ利用できる状態になっていません。",
    ErrorCode.CLAIM_LINK_FAILED: "請求リンクを発行できませんでした。時間をおいてお試しください。",
    ErrorCode.PAYMENT_NOT_FOUND: "まだ支払いを確認できていません。",
    ErrorCode.AUCTION_NOT_OPEN: "このオークションは入札を受け付けていません。",
    ErrorCode.BID_TOO_LOW: "入札額が足りません。",
    ErrorCode.ALREADY_HIGHEST: "すでにあなたが最高入札者です。",
    ErrorCode.REFUND_NOT_ELIGIBLE: "この取引は返金を申請できません。",
    ErrorCode.REFUND_ALREADY_REQUESTED: "この取引はすでに返金を申請しています。",
    ErrorCode.AUCTION_LIMIT_REACHED: "同時に開催できるオークションの数を超えています。",
    ErrorCode.GOAL_ALREADY_OPEN: "すでに集計中のチャージ目標があります。",
    ErrorCode.GOAL_NOT_FOUND: "対象のチャージ目標が見つかりません。",
    ErrorCode.FRAUD_FLAG_NOT_FOUND: "対象の検知が見つからない、または既に処理済みです。",
    ErrorCode.ITEM_INPUT_INVALID: "入力内容が正しくありません。",
    ErrorCode.ITEM_SETUP_FAILED: "商品の用意に失敗しました。代金は自動で返金されています。",
    ErrorCode.SUBSCRIPTION_NOT_FOUND: "継続中の対象が見つかりません。",
    ErrorCode.NO_KYASH_CAPACITY: "現在チャージを受け付けられません。時間をおいてお試しください。",
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
    ErrorCode.PROVIDER_DISABLED: "別のチャージ方法を選ぶか、再開までお待ちください。",
    ErrorCode.PROVIDER_NOT_CONFIGURED: "別のチャージ方法を選んでください (管理者の設定待ちです)。",
    ErrorCode.DUPLICATE_PROOF: "`📜 履歴` で前の申請の状態を確認してください。取引IDの打ち間違いなら、正しいIDで再申請してください。",
    ErrorCode.INVALID_PROOF: "取引ID / txid を、余分な文字を入れずにそのまま貼り付けてください。",
    ErrorCode.OPEN_REQUEST_LIMIT: "`📜 履歴` で未処理の申請を確認し、不要なものは取り消してください。",
    ErrorCode.REQUEST_NOT_FOUND: "`📜 履歴` から申請を選び直してください。",
    ErrorCode.REQUEST_ALREADY_HANDLED: "`📜 履歴` で結果を確認してください。",
    ErrorCode.QUOTE_EXPIRED: "もう一度 `💰 チャージ` を押して、表示された金額を時間内に送ってください。",
    ErrorCode.PRICE_UNAVAILABLE: "別のチャージ方法を選ぶか、しばらくしてからもう一度お試しください。",
    ErrorCode.ASSET_AMOUNT_TOO_SMALL: "表示された最低金額以上でもう一度お試しください。",
    ErrorCode.REVIEW_CHANNEL_NOT_SET: "別のチャージ方法を選んでください (管理者の設定待ちです)。",
    ErrorCode.CLAIM_LINK_FAILED: "少し待ってから、もう一度 `💰 チャージ` を試してください。別の方法でもチャージできます。",
    ErrorCode.PAYMENT_NOT_FOUND: "Kyash アプリで支払いが完了しているか確認し、30秒ほど待って `🔄 支払いを確認` をもう一度押してください。自動でも確認しています。",
    ErrorCode.AUCTION_NOT_OPEN: "パネルの表示を最新にして、開催中のオークションをご確認ください。",
    ErrorCode.BID_TOO_LOW: "表示されている「次の入札額」以上の金額で入札してください。",
    ErrorCode.ALREADY_HIGHEST: "他の人に上回られるまで待ってください。上回られたら自動で返金されます。",
    ErrorCode.REFUND_NOT_ELIGIBLE: "完了したチャージのうち、まだ取消されていないものだけが対象です。期限を過ぎた取引は管理者へご相談ください。",
    ErrorCode.REFUND_ALREADY_REQUESTED: "`/refund list` で申請の状態を確認してください。",
    ErrorCode.AUCTION_LIMIT_REACHED: "開催中のオークションが終わってから追加してください。",
    ErrorCode.GOAL_ALREADY_OPEN: "`/goal list` で確認し、"
                                 "先に `/goal close` か `/goal cancel` で終了してください。",
    ErrorCode.GOAL_NOT_FOUND: "`/goal list` で目標IDを確認してください。",
    ErrorCode.FRAUD_FLAG_NOT_FOUND: "`/fraud list` で未処理の検知を確認してください。",
    ErrorCode.ITEM_INPUT_INVALID: "入力欄の説明にある形式で、もう一度入力してください。",
    ErrorCode.ITEM_SETUP_FAILED: "残高が戻っているか確認し、時間をおいてもう一度お試しください。"
                                 "続く場合は管理者へご連絡ください。",
    ErrorCode.SUBSCRIPTION_NOT_FOUND: "`📦 購入履歴` で継続中の商品と購入IDを確認してください。",
    ErrorCode.NO_KYASH_CAPACITY: "別のチャージ方法を選ぶか、時間をおいてもう一度お試しください。",
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
# v4: 段位 / ランキング報酬 / オークション / 目標 / 不正検知 / 返金申請
# ---------------------------------------------------------------------------
#: 累計チャージの段位を判定する間隔 (秒)
TASK_TIER_INTERVAL: Final[int] = 900
#: 段位を設定できる最大数 (表示が破綻しない範囲)
MAX_TIERS_PER_GUILD: Final[int] = 10


class RankingPeriod:
    """ランキング報酬で締める期間の種類。"""

    WEEKLY = "WEEKLY"     # 月曜 00:00 JST 区切り
    MONTHLY = "MONTHLY"   # 1日 00:00 JST 区切り


RANKING_PERIOD_LABELS: Final[dict[str, str]] = {
    RankingPeriod.WEEKLY: "週間",
    RankingPeriod.MONTHLY: "月間",
}

#: ランキング報酬の配布を確認する間隔 (秒)
TASK_RANKING_REWARD_INTERVAL: Final[int] = 1800
#: 1つの順位範囲に設定できる最大順位
MAX_REWARD_RANK: Final[int] = 100


class AuctionStatus:
    OPEN = "OPEN"            # 入札受付中
    CLOSED = "CLOSED"        # 締切・落札者確定
    CANCELLED = "CANCELLED"  # 中止 (全額返金)
    FAILED = "FAILED"        # 入札なしで終了


AUCTION_STATUS_LABELS: Final[dict[str, str]] = {
    AuctionStatus.OPEN: "🟢 入札受付中",
    AuctionStatus.CLOSED: "🏁 落札",
    AuctionStatus.CANCELLED: "⚫ 中止",
    AuctionStatus.FAILED: "🔴 入札なし",
}

#: オークションの締切を確認する間隔 (秒)
TASK_AUCTION_INTERVAL: Final[int] = 30
#: 開催できる期間の上限 (日)
AUCTION_MAX_DAYS: Final[int] = 30
#: 1サーバーが同時に開催できるオークション数
MAX_OPEN_AUCTIONS: Final[int] = 5
#: 締切直前の入札で締切を延長する秒数 (駆け込み入札の対策・0で無効)
AUCTION_ANTI_SNIPE_SECONDS: Final[int] = 120
#: オークションの最短開催時間 (秒)
AUCTION_MIN_SECONDS: Final[int] = 300
#: 入札額の上限 (残高の上限と同じ考え方で暴走を防ぐ)
AUCTION_MAX_BID: Final[int] = 100_000_000


class GoalStatus:
    OPEN = "OPEN"            # 集計中
    ACHIEVED = "ACHIEVED"    # 達成・報酬配布済み
    CLOSED = "CLOSED"        # 未達のまま終了
    CANCELLED = "CANCELLED"


GOAL_STATUS_LABELS: Final[dict[str, str]] = {
    GoalStatus.OPEN: "🟢 集計中",
    GoalStatus.ACHIEVED: "🎉 達成",
    GoalStatus.CLOSED: "⚫ 終了 (未達)",
    GoalStatus.CANCELLED: "⚫ 中止",
}

#: 目標の進捗を確認する間隔 (秒)
TASK_GOAL_INTERVAL: Final[int] = 120
#: 進捗バーの桁数
GOAL_BAR_WIDTH: Final[int] = 12


class FraudKind:
    """不正の兆候の種類。"""

    BURST_CHARGE = "BURST_CHARGE"          # 短時間の大量チャージ
    SHARED_SENDER = "SHARED_SENDER"        # 同一 Kyash 送金者名を複数人が使用
    DRAIN_AND_LEAVE = "DRAIN_AND_LEAVE"    # チャージ直後に使い切って退出
    INVITE_ONLY = "INVITE_ONLY"            # 招待報酬だけを集め活動しない
    RAPID_REFUND = "RAPID_REFUND"          # 返金申請を繰り返す


FRAUD_KIND_LABELS: Final[dict[str, str]] = {
    FraudKind.BURST_CHARGE: "短時間の大量チャージ",
    FraudKind.SHARED_SENDER: "送金者名の共有 (名義貸し・転売の疑い)",
    FraudKind.DRAIN_AND_LEAVE: "チャージ直後の使い切りと退出",
    FraudKind.INVITE_ONLY: "招待報酬のみの収集",
    FraudKind.RAPID_REFUND: "返金申請の繰り返し",
}


class FraudSeverity:
    INFO = "INFO"
    WARN = "WARN"
    HIGH = "HIGH"


FRAUD_SEVERITY_LABELS: Final[dict[str, str]] = {
    FraudSeverity.INFO: "🔵 参考",
    FraudSeverity.WARN: "🟠 注意",
    FraudSeverity.HIGH: "🔴 重要",
}


class FraudStatus:
    OPEN = "OPEN"          # 未処理
    RESOLVED = "RESOLVED"  # 対処済み
    IGNORED = "IGNORED"    # 問題なしと判断


FRAUD_STATUS_LABELS: Final[dict[str, str]] = {
    FraudStatus.OPEN: "🟠 未処理",
    FraudStatus.RESOLVED: "🟢 対処済み",
    FraudStatus.IGNORED: "⚪ 問題なし",
}

#: 不正検知を回す間隔 (秒)
TASK_FRAUD_INTERVAL: Final[int] = 600
#: 短時間の大量チャージ: この秒数以内に この件数 を超えたら検知
FRAUD_BURST_WINDOW: Final[int] = 600
FRAUD_BURST_COUNT: Final[int] = 5
#: 同一送金者名を この人数 以上が使っていたら検知
FRAUD_SHARED_SENDER_USERS: Final[int] = 2
#: チャージ後 この秒数以内 に残高をほぼ使い切ったら検知
FRAUD_DRAIN_WINDOW: Final[int] = 3600
FRAUD_DRAIN_RATIO: Final[str] = "0.9"
#: 招待報酬のみ: 確定招待が この件数 以上でチャージが0件なら検知
FRAUD_INVITE_ONLY_COUNT: Final[int] = 3
#: 返金申請を この件数 以上繰り返したら検知
FRAUD_REFUND_COUNT: Final[int] = 3


class RefundRequestStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


REFUND_STATUS_LABELS: Final[dict[str, str]] = {
    RefundRequestStatus.PENDING: "🟡 審査待ち",
    RefundRequestStatus.APPROVED: "🟢 承認 (返金済み)",
    RefundRequestStatus.REJECTED: "🔴 却下",
    RefundRequestStatus.CANCELLED: "⚪ 取消",
}

REFUND_TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    RefundRequestStatus.PENDING: (
        RefundRequestStatus.APPROVED, RefundRequestStatus.REJECTED,
        RefundRequestStatus.CANCELLED,
    ),
    RefundRequestStatus.APPROVED: (),
    RefundRequestStatus.REJECTED: (),
    RefundRequestStatus.CANCELLED: (),
}

#: 返金申請できるのはチャージ完了から この秒数以内
REFUND_REQUEST_WINDOW: Final[int] = 14 * 86400
#: 1利用者が同時に出せる返金申請の数
MAX_OPEN_REFUND_REQUESTS: Final[int] = 2

#: レシート署名のバージョン (形式を変えるときに上げる)
RECEIPT_VERSION: Final[str] = "R1"

#: グラフ画像のサイズと既定の日数
CHART_WIDTH: Final[int] = 900
CHART_HEIGHT: Final[int] = 420
CHART_DEFAULT_DAYS: Final[int] = 14
CHART_MAX_DAYS: Final[int] = 90


class ShopItemType:
    """ショップ商品の種類。"""

    ROLE = "ROLE"                      # 既存のロール販売
    CUSTOM_ROLE = "CUSTOM_ROLE"        # 名前と色を指定してロールを作る
    NICKNAME = "NICKNAME"              # ニックネームの変更権
    RATE_BOOST = "RATE_BOOST"          # 一定時間チャージ率が上がる
    PRIVATE_CHANNEL = "PRIVATE_CHANNEL"  # 本人専用チャンネル


SHOP_ITEM_TYPE_LABELS: Final[dict[str, str]] = {
    ShopItemType.ROLE: "ロール付与",
    ShopItemType.CUSTOM_ROLE: "カスタムロール作成",
    ShopItemType.NICKNAME: "ニックネーム変更",
    ShopItemType.RATE_BOOST: "チャージ率ブースト",
    ShopItemType.PRIVATE_CHANNEL: "専用チャンネル",
}

SHOP_ITEM_TYPE_EMOJI: Final[dict[str, str]] = {
    ShopItemType.ROLE: "🎫",
    ShopItemType.CUSTOM_ROLE: "🎨",
    ShopItemType.NICKNAME: "✏️",
    ShopItemType.RATE_BOOST: "⚡",
    ShopItemType.PRIVATE_CHANNEL: "🔒",
}

#: 既存のロールを指定する必要がある種類 (それ以外は role_id を使わない)
SHOP_TYPES_NEED_ROLE: Final[tuple[str, ...]] = (ShopItemType.ROLE,)
#: Bot が作成物を後片付けする必要がある種類
SHOP_TYPES_WITH_ASSET: Final[tuple[str, ...]] = (
    ShopItemType.CUSTOM_ROLE, ShopItemType.PRIVATE_CHANNEL,
)
#: 購入時に利用者の入力が必要な種類 (Modal を出す)
SHOP_TYPES_NEED_INPUT: Final[tuple[str, ...]] = (
    ShopItemType.CUSTOM_ROLE, ShopItemType.NICKNAME, ShopItemType.PRIVATE_CHANNEL,
)
#: 同じものを重複して持てない種類 (無期限の場合に所持チェックをする)
SHOP_TYPES_UNIQUE: Final[tuple[str, ...]] = (ShopItemType.ROLE,)
#: 購入時に利用者へ入力を求めるラベル (Modal の項目名)
SHOP_INPUT_LABELS: Final[dict[str, str]] = {
    ShopItemType.CUSTOM_ROLE: "ロール名",
    ShopItemType.NICKNAME: "新しいニックネーム",
    ShopItemType.PRIVATE_CHANNEL: "チャンネル名",
}
#: 各種類の説明 (管理者・利用者の双方に見せる)
SHOP_ITEM_TYPE_DESCRIPTIONS: Final[dict[str, str]] = {
    ShopItemType.ROLE: "設定済みのロールをそのまま付与します。",
    ShopItemType.CUSTOM_ROLE: "購入者が名前と色を決めたロールを新しく作って付与します。",
    ShopItemType.NICKNAME: "購入者のニックネームを変更します (期限が切れると元に戻します)。",
    ShopItemType.RATE_BOOST: "一定時間だけチャージ率が上がります。",
    ShopItemType.PRIVATE_CHANNEL: "購入者だけが見られる専用チャンネルを作ります。",
}
#: 全種類 (コマンドの選択肢生成に使う)
ALL_SHOP_ITEM_TYPES: Final[tuple[str, ...]] = (
    ShopItemType.ROLE, ShopItemType.CUSTOM_ROLE, ShopItemType.NICKNAME,
    ShopItemType.RATE_BOOST, ShopItemType.PRIVATE_CHANNEL,
)
#: 専用チャンネル名の接頭辞 (作成物だと分かるようにする)
PRIVATE_CHANNEL_PREFIX: Final[str] = "🔒"
#: 監査ログで「Bot の自動処理」を表す実行者ID
#: Discord のユーザーIDと衝突しない 0 を使い、表示時は「自動処理」と出す。
SYSTEM_ACTOR_ID: Final[int] = 0

#: 1サーバーで作れるロール数の安全上限 (Discord の上限 250 に余裕を持たせる)
GUILD_ROLE_SOFT_LIMIT: Final[int] = 230
#: サブスクが終了した理由の表示名
SUBSCRIPTION_STOP_REASONS: Final[dict[str, str]] = {
    "INSUFFICIENT_BALANCE": "残高不足",
    "ITEM_UNAVAILABLE": "商品が販売停止になった",
    "USER_FROZEN": "利用者が凍結されている",
    "CANCELLED": "利用者が自動更新を停止した",
    "UNKNOWN": "不明",
}


class PurchaseAssetType:
    ROLE = "ROLE"          # Bot が作成したロール
    CHANNEL = "CHANNEL"    # Bot が作成したチャンネル
    NICKNAME = "NICKNAME"  # 変更前のニックネーム (戻すために保存する)
    RATE_BOOST = "RATE_BOOST"  # 付与したチャージ率ブースト


#: カスタムロール・専用チャンネルの名前の長さ
CUSTOM_NAME_MAX_LEN: Final[int] = 40
#: ニックネームの長さ (Discord の上限)
NICKNAME_MAX_LEN: Final[int] = 32
#: チャージ率ブーストの上限 (%ポイント)
RATE_BOOST_MAX_BONUS: Final[str] = "100"
#: チャージ率ブーストの最長時間 (時間)
RATE_BOOST_MAX_HOURS: Final[int] = 720

#: サブスク商品の更新を確認する間隔 (秒)
TASK_SUBSCRIPTION_INTERVAL: Final[int] = 600
#: 更新の何秒前に予告 DM を送るか
SUBSCRIPTION_NOTICE_SECONDS: Final[int] = 86400


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
    AUCTION_BID = "chargebot:auction:bid"
    AUCTION_INFO = "chargebot:auction:info"
    GOAL_REFRESH = "chargebot:goal:refresh"
    FRAUD_RESOLVE = "chargebot:fraud:resolve"
    FRAUD_IGNORE = "chargebot:fraud:ignore"
    FRAUD_DETAIL = "chargebot:fraud:detail"
    REFUND_APPROVE = "chargebot:refund:approve"
    REFUND_REJECT = "chargebot:refund:reject"
    REQUEST_APPROVE = "chargebot:request:approve"
    REQUEST_REJECT = "chargebot:request:reject"
    REQUEST_EDIT_APPROVE = "chargebot:request:editapprove"
    REQUEST_DETAIL = "chargebot:request:detail"
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
PANEL_TYPE_GOAL: Final[str] = "GOAL"
PANEL_TYPE_AUCTION: Final[str] = "AUCTION"

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
