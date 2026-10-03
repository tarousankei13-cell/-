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

# 組み立て途中のカートを「続きから」で拾える時間（分）。
# Discord のビューは VIEW_TIMEOUT 秒で反応しなくなるが、中身はDBに
# 残っている。少し目を離しただけで作り直しになるのは不親切なので、
# この時間内なら拾い直せるようにする。
# ⚠️ 長くしすぎないこと。値段も販売時間も変わる。
CART_RESUME_MINUTES = 15

# 1回の注文に入れられる商品の上限。
# マクドナルド側の上限は分かっていないが、青天井にすると
# 操作ミスで大量注文になりうるので、こちらで線を引く。
CART_MAX_ITEMS = 30

# ============================================================
#  注文番号ページ（BOT が自分で配信する小さなサイト）
# ============================================================
# 店頭で注文番号を見せるためだけのページ。
# BOT と同じプロセスで動く（aiohttp は discord.py が持っているので追加導入は不要）。
WEB_ENABLED = False          # /config web で有効にする
WEB_HOST = "127.0.0.1"       # 前段に nginx を置く前提。外に直接出すなら 0.0.0.0
WEB_PORT = 8080
WEB_BASE_URL = ""            # 例 https://example.com/order　公開URLの組み立てに使う

# ページを開ける時間。注文番号は当日しか使わないので、長く残さない。
RECEIPT_PAGE_HOURS = 12

# ページを**別の場所**で動かす場合の送り先。
# receipt_site/ を別のサーバーに置き、ここにその登録先を入れる。
#   例 https://example.com/api/receipts
# 合い言葉はページ側の環境変数 PUSH_SECRET と一致させること。
WEB_PUSH_URL = ""
WEB_PUSH_SECRET = ""

# ============================================================
#  招待キャンペーン
# ============================================================
# ⚠️ 残高を配る＝実際にお金が出ていく。既定は**無効**にしてある。
#    有効にする前に、1人あたりいくら・全体でいくらまでを必ず決めること。
INVITE_ENABLED = False
INVITE_REWARD = 500              # 発火1回につき、紹介した人に渡す額（円）
INVITE_REWARD_EVERY = 2          # 何名の達成ごとに発火するか（1なら1人ごと）
INVITE_REWARD_INVITEE = 0        # 招待された人に渡す額（0なら渡さない）
INVITE_MAX_PER_USER = 5          # 1人が特典をもらえる招待の上限（人数）
INVITE_BUDGET = 10000            # キャンペーン全体の上限（円）。0で無制限

# 達成とみなす最低注文額（定価・円）。
# ⚠️ 0 にすると、100円の商品を2回頼むだけで特典が出る。
INVITE_MIN_ORDER = 400

# 招待された側に求める条件。
# ⚠️ 作ったばかりの捨てアカウントで人数を稼ぐのを防ぐための歯止め。
INVITE_MIN_ACCOUNT_DAYS = 14     # Discordアカウント作成からの日数
INVITE_MIN_MEMBER_HOURS = 1      # サーバー参加からの時間（手動入力のときだけ）

# BOTが発行する招待リンク
# ⚠️ 発行先チャンネルは管理者が必ず指定する（未設定なら発行できない）。
#    意図しない場所へ人を流さないため。
INVITE_LINK_CHANNEL = 0
INVITE_LINK_DAYS = 3             # 招待リンクの有効期限（日）

# 特典を渡す条件:
#   join        参加しただけで渡す（一番ばらまきやすい）
#   first_order 招待された人が初めて注文したら渡す（既定・推奨）
INVITE_CONDITION = "first_order"

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
MENU_SYNC_CONCURRENCY = 6         # メニュー同期を何店舗まで同時に行うか

# 注文の同時実行
#   大人数が一斉に注文すると、マクドナルド側から見て不自然な量の要求が
#   短時間に集中する。同時に処理する数に上限を設け、超えた分は待たせる。
ORDER_CONCURRENCY = 3             # 同時に処理する注文の数
ORDER_MAX_WAIT_SECONDS = 120      # 順番を待つ上限（0で無制限）
ORDER_MAX_QUEUE = 20              # 待機列の上限。これ以上は断る
ORDER_OUTAGE_WAIT_SECONDS = 60    # マクドナルドが落ちているときに復帰を待つ上限

# 残高の増減を公開するパネルで、何を出すか
#   name    誰の分か（表示名）。off にすると匿名コードになる
#   amount  増減額
#   balance 変動後の残高
#   reason  理由（チャージ／注文）
# ⚠️ 既定では残高を出さない。いくら持っているかは知られたくない人が多い。
BALANCE_PANEL_FIELDS_DEFAULT = ["name", "amount", "reason"]
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
# 外形監視（マクドナルド側が落ちていないか）の間隔（分）
MONITOR_INTERVAL_MINUTES = 5

KYASH_TOKEN_LIFETIME_DAYS = 30   # アクセストークンの寿命
KYASH_TOKEN_WARN_DAYS = 7        # 残りこの日数で管理者へ通知

# ------------------------------------------------------------
#  トークンキャッシュ
# ------------------------------------------------------------
TOKEN_REFRESH_MARGIN_SECONDS = 60   # 期限の何秒前に先回りして更新するか
ROOT_PASETO_TTL_SECONDS = 3600      # root PASETO の想定寿命
TOKEN_WARM_INTERVAL_MINUTES = 45    # バックグラウンドでの事前更新間隔
POS_PASETO_TTL_SECONDS = 600        # POSトークンを使い回す時間（注文のたびに取らない）

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
# 表示名は実物のモバイルオーダーに合わせる（services/mcd/protocol.PICKUP_LABEL と同じ）
PICKUP_METHODS = {
    "eatIn":          {"field": 1, "label": "店内でお召し上がり",     "enabled": True},
    "takeOut":        {"field": 2, "label": "お持ち帰り",             "enabled": True},
    "tableDelivery":  {"field": 3, "label": "席までお届け",           "enabled": False},
    "curbsidePickUp": {"field": 4, "label": "パーキングでお受け取り", "enabled": False},
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
    """日本時間の今日の0時（タイムゾーン付き）。表示に使う。"""
    return now_jst().replace(hour=0, minute=0, second=0, microsecond=0)


def to_db(dt: datetime) -> datetime:
    """
    DBの比較に使える形へ直す。

    ⚠️ DBには**UTCのタイムゾーン無し**で入っている。
       日本時間の値をそのままSQLの条件に使うと、
       「2026-10-03 00:00（日本）」と「2026-10-02 15:00（UTC）」を
       比べることになり、9時間ずれる。
       日付の区切りは日本時間で決め、比較はUTCに直してから行うこと。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def jst_midnight_utc() -> datetime:
    """日本時間の今日の0時を、DBの比較に使える形で。"""
    return to_db(jst_midnight())


def jst_month_start_utc() -> datetime:
    """日本時間の今月1日0時を、DBの比較に使える形で。"""
    return to_db(jst_midnight().replace(day=1))


def utcnow_naive() -> datetime:
    """いまのUTC（タイムゾーン無し）。DBの比較に使う。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

