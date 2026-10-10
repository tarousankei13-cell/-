import os
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

# PayPay が保留した受け取りリンクを見に行く間隔（分）
#
# ⚠️ **見張りの細かさは、見に行く間隔より細かくできない。**
#    charge.py は「2分後→5分後→…」と予定を立てるが、それを読む
#    ループが1時間に1回しか回らなければ、2分の予定は意味を持たない。
#    最初に作ったときこれを取り違えており、利用者は保留が解けても
#    最長1時間待たされる作りになっていた。
#    保留はお金を待たせている状態なので、短くする。
PAYPAY_HOLD_POLL_MINUTES = 2
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
# ⚠️ 負担率は既定で出さない。誰がいくら持ち出しているかは、
#    利用者に見せるものではない。見たい管理者だけが足せばよい。
ACHIEVEMENT_FIELDS_DEFAULT = [
    "anon_code",     # 匿名コード（U-7F3A）
    "list_price",    # 定価
    "user_amount",   # 利用者の支払額
    "daily_count",   # 本日の通算
]
# 既定でOFF: username / store_name / receipt_number / pickup_method

# ------------------------------------------------------------
#  チャージ率・注文の入口の条件
# ------------------------------------------------------------
# チャージ率（％）。120 なら 1,000円の送金で 1,200円ぶんの残高になる。
# ⚠️ 100 を超えたぶんは運営の持ち出し。上げる前に必ず予算を決めること。
#    端数は切り捨てる（¥333 × 120% = ¥399）。
CHARGE_RATE = 100

# 注文できる最低額（定価・円）。0 で制限なし。
# ⚠️ 定価で見る。負担率を変えても基準がぶれないようにするため。
ORDER_MIN = 0

# はじめての注文の前に、累計いくら以上チャージしてもらうか。
# ⚠️ 数えるのは **実際に送金された額**。チャージ率で増えたぶんは含めない。
#    含めると、率を上げたぶんだけ条件が緩くなってしまう。
FIRST_CHARGE_GATE = False        # 既定は無効
FIRST_CHARGE_MIN = 1000          # 有効にしたときの金額（円）

# ------------------------------------------------------------
#  できあがり通知
# ------------------------------------------------------------
# ⚠️ 相手に問い合わせ続けるので、既定はOFF。
#    1件の注文につき「見張る分数 ÷ 間隔」回だけ通信が増える。
READY_NOTIFY = False
READY_WATCH_MINUTES = 30      # 注文後、何分まで見張るか
READY_POLL_SECONDS = 45       # 問い合わせの間隔（秒）
READY_MAX_WATCHED = 20        # 同時に見張る注文の上限

# ------------------------------------------------------------
#  返金（実際にお金が出ていく）
# ------------------------------------------------------------
# ⚠️ 既定はOFF。有効にする前に、誰がいくらまで返せるかを決めること。
REFUND_ENABLED = False
REFUND_MAX = 10000            # 1回に返せる上限（円）
REFUND_LINK_HOURS = 72        # 作った送金リンクの有効時間（案内に使う）

# ------------------------------------------------------------
#  PayPay
# ------------------------------------------------------------
# ⚠️ アプリ側のAPIを使うのでトークンは90日もつ。
#    Web側（2時間で切れ、更新手段なし）とは別物。
PAYPAY_TOKEN_LIFETIME_DAYS = 90
PAYPAY_TOKEN_WARN_DAYS = 14        # 残りこの日数で管理者へ警告

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
    """
    いまの日本時間。

    ⚠️ 環境変数 BOT_FAKE_JST（"YYYY-MM-DD HH:MM"）があれば、その時刻を
       返す。**テスト専用**。提供時間帯の判定は時刻で変わるため、
       これが無いと「朝だけ落ちるテスト」を書いてしまう。
       本番では設定しないこと。
    """
    fake = os.getenv("BOT_FAKE_JST", "").strip()
    if fake:
        try:
            return datetime.strptime(fake, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            pass
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



# ------------------------------------------------------------
#  サーバー管理：チケット
# ------------------------------------------------------------
TICKET_ENABLED = False
# channel（専用チャンネル） / thread（プライベートスレッド）
#
#   ⚠️ 既定をチャンネルにしてある。理由は**担当者から見えるかどうか**。
#      チャンネルなら、ロールに閲覧権限を与えるだけで担当者全員に見える。
#      プライベートスレッドは、ロールを @メンション しても
#      **そのロールの人がスレッドに入らない**（Discordの仕様）。
#      担当者に見せるには、親チャンネルで「スレッドの管理」権限を
#      与えておく必要がある。
#
#   チャンネルの上限
#      サーバー全体で500個、1カテゴリに50個まで。
#      自動クローズ（TICKET_AUTO_CLOSE_HOURS）を入れてあるので
#      普通の使い方では埋まらないが、残りが少なくなったら知らせる。
TICKET_MODE = "channel"
TICKET_CATEGORY_LIMIT = 50    # 1カテゴリに作れるチャンネル数（Discordの制限）
TICKET_CATEGORY_WARN = 5      # 残りこの数で管理者に知らせる
TICKET_MAX_OPEN = 2           # 1人が同時に開けるチケット数
TICKET_AUTO_CLOSE_HOURS = 72  # 放置で自動クローズ（0で無効）
TICKET_PING_STAFF = True      # 作成時に担当ロールへ声をかける

# 問い合わせの種別。/ticket kinds で変更できる。
TICKET_KINDS_DEFAULT = [
    {"key": "order", "label": "注文のトラブル", "emoji": "🍔",
     "desc": "注文できない・間違って届いた など"},
    {"key": "charge", "label": "チャージ・残高", "emoji": "💴",
     "desc": "チャージが反映されない など"},
    {"key": "refund", "label": "返金の相談", "emoji": "🔙",
     "desc": "返金してほしい場合"},
    {"key": "other", "label": "その他", "emoji": "💬",
     "desc": "上にあてはまらないこと"},
]

# ------------------------------------------------------------
#  サーバー管理：認証
# ------------------------------------------------------------
VERIFY_ENABLED = False
# button（ボタンを押すだけ） / captcha（画像の文字を入力）
VERIFY_MODE = "button"
VERIFY_MIN_ACCOUNT_DAYS = 0   # アカウント作成からの日数条件（0で無し）
VERIFY_KICK_HOURS = 0         # 未認証のまま何時間でキック（0で無効）
VERIFY_CAPTCHA_LENGTH = 5     # 画像認証の文字数
VERIFY_MAX_ATTEMPTS = 5       # 画像認証の失敗上限

# 画像認証で使う文字。
#   ⚠️ 0とO、1とI、2とZ など**見間違える文字を入れない**。
#      入れると、正しく読めたのに弾かれる人が出る。
VERIFY_CAPTCHA_CHARS = "34679ACDEFHJKLMNPQRTUVWXY"

# ------------------------------------------------------------
#  サーバー管理：監視
# ------------------------------------------------------------
# 記録する出来事。/guard events で切り替える。
GUARD_EVENTS_DEFAULT = ["join", "leave", "ban", "role", "nick", "timeout"]
GUARD_EVENTS_ALL = [
    "join", "leave", "ban", "role", "nick", "timeout",
    "msgdelete", "msgedit", "channel", "voice",
]
# ⚠️ msgdelete / msgedit は **MESSAGE CONTENT INTENT が必要**。
#    無い場合、内容が空のまま記録される（誰がいつ消したかだけ分かる）。
GUARD_EVENTS_NEED_CONTENT = ["msgdelete", "msgedit"]

# 短時間の大量入室（レイド）
GUARD_RAID_ENABLED = False
GUARD_RAID_JOINS = 5          # 何人で
GUARD_RAID_SECONDS = 10       # 何秒以内なら
GUARD_RAID_ACTION = "notify"  # notify（知らせるだけ） / lockdown（入室を止める）

# 連投（スパム）
#   ⚠️ 同じ文の繰り返しの判定は MESSAGE CONTENT INTENT が必要。
#      無い場合は「速さ」だけで見る（これは内容がなくても数えられる）。
GUARD_SPAM_ENABLED = False
GUARD_SPAM_MESSAGES = 6       # 何件を
GUARD_SPAM_SECONDS = 5        # 何秒以内に出したら
GUARD_SPAM_ACTION = "timeout"  # delete（消す） / timeout（発言停止）
GUARD_TIMEOUT_MINUTES = 10    # 自動の発言停止の長さ

# メンション爆撃。1通に入れられるメンションの上限（0で無効）
#   ⚠️ これは内容を読まなくても数えられる（Discord が別の項目で送ってくる）。
GUARD_MENTION_LIMIT = 6

# 招待リンク・NGワード（どちらも MESSAGE CONTENT INTENT が必要）
GUARD_INVITE_BLOCK = False
GUARD_WORDS_DEFAULT: list[str] = []

# 作りたてのアカウントが入ってきたら知らせる（0で無効）
GUARD_NEW_ACCOUNT_DAYS = 0

# メンバー数をチャンネル名に出す
#   ⚠️ チャンネル名の変更は **10分に2回** までしか通らない（Discordの制限）。
#      そのため更新は10分間隔にしてある。
GUARD_COUNTER_FORMAT = "👥 メンバー: {count}"
GUARD_COUNTER_MINUTES = 10

# ------------------------------------------------------------
#  サーバー管理：モデレーション
# ------------------------------------------------------------
MOD_WARN_TIMEOUT_AT = 3       # 警告が何回たまったら発言停止（0で無し）
MOD_WARN_KICK_AT = 0          # 何回でキック（0で無し）
MOD_WARN_BAN_AT = 0           # 何回でBAN（0で無し）
MOD_WARN_TIMEOUT_MINUTES = 60
MOD_DM_ON_ACTION = True       # 処分の理由を本人にDMする
MOD_PURGE_MAX = 200           # /mod purge で一度に消せる上限

# ⚠️ Discord のタイムアウトは **28日**までしか設定できない。
MOD_TIMEOUT_MAX_DAYS = 28


# ------------------------------------------------------------
#  声かけ（ようこそ・途中離脱・残高のお知らせ）
# ------------------------------------------------------------
# ⚠️ **しつこいDMは逆効果。** 同じことは二度言わない仕組み（Nudge表）と、
#    利用者が1タップで止められる設定（User.nudge_notify）が前提にある。
#    既定の間隔は「気づいてもらえるが、うるさくない」ところに置いてある。

NUDGE_ENABLED = False          # 全体のスイッチ

# ① ようこそ案内（認証した方・初めて見かけた方へ1回だけ）
NUDGE_WELCOME = True

# ② カートを残したまま離れた方へ
#    ⚠️ カートの有効時間（CART_RESUME_MINUTES = 15分）より**短く**すること。
#       消えたあとに「続きからどうぞ」と言っても、続きが無い。
NUDGE_CART_MINUTES = 8

# ③ チャージしたのに注文していない方へ
NUDGE_CHARGED_HOURS = 6

# ④ 残高があるのに使っていない方へ
#    ⚠️ ここは運営の持ち出しが増えない（すでに預かっている額の範囲）。
NUDGE_IDLE_DAYS = 14           # 何日注文が無ければ声をかけるか
NUDGE_IDLE_MIN_BALANCE = 300   # これ未満の残高では声をかけない
NUDGE_IDLE_REPEAT_DAYS = 30    # 一度言ったら、次に言うまで空ける日数

# 1回の定期処理で送る上限。
#   ⚠️ 一度に大量のDMを送るとDiscordに制限される。少しずつ送る。
NUDGE_BATCH = 20

# ------------------------------------------------------------
#  再注文
# ------------------------------------------------------------
REORDER_HISTORY = 5            # 履歴から選び直せる件数

# ------------------------------------------------------------
#  紹介ランキング
# ------------------------------------------------------------
RANKING_TOP = 5                # 何位まで出すか
RANKING_REFRESH_MINUTES = 30   # パネルの自動更新の間隔
# 名前を出すか。False なら実績パネルと同じ匿名コードで出す。
RANKING_SHOW_NAMES = True
