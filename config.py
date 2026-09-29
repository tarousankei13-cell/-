from datetime import datetime, timedelta, timezone

"""
既定値と定数

ここの値は「初回起動時の初期値」。
運用中の変更は /config コマンドで行い、DB (bot_config) に保存される。
機密情報はこのファイルに置かない（main.py の設定ブロックに書く）。
"""

from decimal import Decimal

# ------------------------------------------------------------
#  負担率
# ------------------------------------------------------------
# 管理者が負担する割合(%)。利用者の支払いは (100 - この値) %。
#   例) 40 → 定価¥590 のとき利用者は ¥354 を支払う
DEFAULT_SUBSIDY_RATE = Decimal("40.00")

# 端数処理: 利用者の支払額を切り上げる（運営側が損をしない）
ROUNDING = "CEIL"

# 1人あたりの月間負担上限（円）。None で無制限
DEFAULT_MONTHLY_SUBSIDY_CAP = None

# ------------------------------------------------------------
#  チャージ
# ------------------------------------------------------------
CHARGE_MIN = 100      # 1回のチャージ下限（円）
CHARGE_MAX = 50_000   # 1回のチャージ上限（円）

# 内部残高の払い戻しは行わない
REFUND_ENABLED = False

# ------------------------------------------------------------
#  注文
# ------------------------------------------------------------
ORDER_MAX_AMOUNT = None    # 1注文あたりの上限（円）。None で無制限
ORDER_DAILY_LIMIT = None   # 1人あたりの1日の注文回数。None で無制限

# 注文方式
#   "both" … 注文コード(HEX)とメニューの両方から注文できる
#   "hex"  … 注文コードを貼る方法だけ
#   "menu" … メニューから選ぶ方法だけ（注文コードは使わない）
ORDER_MODE = "both"

# 一時ビュー（カート・確認画面）の有効時間（秒）
VIEW_TIMEOUT = 120

# 投機的先読みの結果を保持する時間（秒）
PREFETCH_TTL = 90

# ------------------------------------------------------------
#  メニュー同期
# ------------------------------------------------------------
# 商品・提供時間帯・店舗情報は、マクドナルド側で随時変わる。
# ETag により「変更が無ければ通信量ゼロ」で確認できるため、高頻度で同期する。
MENU_SYNC_INTERVAL_MINUTES = 15   # メニューの定期同期（分）
STORE_REFRESH_MINUTES = 30        # 店舗情報・営業時間の再取得（分）

# 店舗一覧そのものの同期（新店舗・閉店・店名変更・モバイルオーダー可否）
STORE_INDEX_SYNC_MINUTES = 15     # 巡回更新の間隔（分）
STORE_INDEX_REFRESH_BATCH = 400   # 1回に取り直す店舗数（全3,036店舗を約2時間で一周）
STORE_SITEMAP_CHECK_MINUTES = 60  # 店舗IDの一覧を照合する間隔（分）
MENU_STALE_MINUTES = 10           # 注文直前に取り直す古さのしきい値（分）
MENU_ACTIVE_STORE_DAYS = 30       # 「直近で使われた店舗」とみなす日数
MENU_NOTIFY_DIFF = True           # 新商品・値上げを管理者へ通知するか
# 提供時間帯（limitedAbility / daypart）は日付ごとに定義されるため、
# 日付が変わったら ETag を無視して必ず取り直す。
MENU_FORCE_REFRESH_HOUR = 0

# ------------------------------------------------------------
#  アカウント
# ------------------------------------------------------------
MCD_FAILURES_TO_DEGRADE = 3      # 連続失敗でDEGRADEDにする回数
MCD_FAILURES_TO_QUARANTINE = 5   # 連続失敗で隔離する回数
MCD_HEALTHCHECK_INTERVAL_HOURS = 1

KYASH_TOKEN_LIFETIME_DAYS = 30   # アクセストークンの寿命
KYASH_TOKEN_WARN_DAYS = 7        # 残りこの日数で管理者へ通知

# ------------------------------------------------------------
#  トークンキャッシュ
# ------------------------------------------------------------
TOKEN_REFRESH_MARGIN_SECONDS = 60   # 期限の何秒前に先回りして更新するか
ROOT_PASETO_TTL_SECONDS = 3600      # root PASETO の想定寿命
TOKEN_WARM_INTERVAL_MINUTES = 45    # バックグラウンドでの事前更新間隔

# ------------------------------------------------------------
#  実績パネル（プライバシー重視）
# ------------------------------------------------------------
ACHIEVEMENT_FIELDS_DEFAULT = [
    "anon_code",     # 匿名コード（U-7F3A）
    "list_price",    # 定価
    "subsidy_rate",  # 負担率
    "user_amount",   # 利用者の支払額
    "daily_count",   # 本日の通算
]
# 既定でOFF: username / store_name / receipt_number / pickup_method

# 感想ゲート（既定OFF。ONにする場合は message_content インテントが必要）
FEEDBACK_GATE_ENABLED = False

# ------------------------------------------------------------
#  レシート画像（docs/04 §6.1 の実測値）
# ------------------------------------------------------------
RECEIPT_TEMPLATE = "assets/templates/receipt.png"
RECEIPT_FONT = "assets/fonts/NotoSansJP-Bold.otf"
RECEIPT_CLEAR_BOX = (150, 103, 362, 168)   # 元の番号を消す領域
RECEIPT_CENTER = (255, 134)                # 描画の中心
RECEIPT_BG_COLOR = (247, 247, 247)
RECEIPT_INK_COLOR = (45, 45, 45)
RECEIPT_BASE_FONT_SIZE = 66
RECEIPT_MAX_WIDTH = 200

# 受け取り画面のURL（完了DMのリンクボタン用）
#
# ⚠️ これは**外部サイト**への飾りのリンク。BOTの動作には関わらない。
#    注文・メニュー同期・店舗同期はすべてマクドナルド公式のAPIだけで
#    完結しており、このURLが落ちても影響しない。
#
#    店頭で必要な注文番号は、BOTが自分で作るレシート画像に入っている。
#    そのため、このリンクが使えないときはボタンを出さないだけでよい。
#
#    公式のweb版モバイルオーダーは存在しない（アプリのみ）。
#    また注文はBOTのアカウントで行うため、利用者ご自身のアプリを開いても
#    その注文は表示されない。代わりになる公式のURLは無い。
#
#    /config receipt_url で変更・無効化できる（空にするとボタンを出さない）。
RECEIPT_VIEW_URL = "https://mcdon.asia/order/{store_id}?orderId={receipt_number}"

# リンク先が落ちていたらボタンを自動で隠す。その確認の間隔（分）。
RECEIPT_URL_CHECK_MINUTES = 60

# ------------------------------------------------------------
#  マクドナルド API
# ------------------------------------------------------------
# 店舗はこのいずれか1つのグループだけが配信している。
# 店舗数の多い順に並べてあるので、総当たりでも早く当たる。
# （j:693 i:686 h:649 g:631 f:374 e:3 / 全3,036店舗を実測）
MCD_GROUPS = ["group-j", "group-i", "group-h", "group-g", "group-f", "group-e"]
MCD_DATA_URL = "https://data.cat.{group}.prod.mop.mcd.qorcommerce.com/{path}"
HTTP_TIMEOUT = 20

# 受取方法（docs/07 §4.2 / CreateDeliveryMethod の oneof）
PICKUP_METHODS = {
    "takeOut":        {"field": 2, "label": "テイクアウト",           "enabled": True},
    "eatIn":          {"field": 1, "label": "店内（カウンター受取）", "enabled": True},
    "tableDelivery":  {"field": 3, "label": "店内（席まで）",         "enabled": False},
    "curbsidePickUp": {"field": 4, "label": "駐車場で受け取る",       "enabled": False},
    "driveThru":      {"field": 5, "label": "ドライブスルー",         "enabled": False},
    "addressDelivery":{"field": 6, "label": "デリバリー",             "enabled": False},
}

# ============================================================
#  時刻
# ============================================================
# マクドナルドの提供時間帯（朝マック・夜マック）は**日本時間**で定義されている。
#   ・mopDaypartAbilityLists の日付キーは日本の日付
#   ・start/end の値は日本時間の「0時からの経過分」（朝マック 350〜620 = 5:50〜10:20）
#
# サーバーのタイムゾーン設定に頼ると、UTCの環境では9時間ずれて
# 朝マックが一切選べなくなる。日付・時刻の判定は必ずこれを使うこと。
JST = timezone(timedelta(hours=9))


def now_jst() -> datetime:
    """いまの日本時間。"""
    return datetime.now(JST)


def today_jst() -> str:
    """日本時間の今日（YYYY-MM-DD）。提供時間帯の日付キーに使う。"""
    return now_jst().strftime("%Y-%m-%d")


def jst_midnight() -> datetime:
    """日本時間の今日の0時。「本日の件数」の集計に使う。"""
    return now_jst().replace(hour=0, minute=0, second=0, microsecond=0)

