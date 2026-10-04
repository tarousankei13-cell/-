"""
絵文字定数

Discord のどのサーバーでも確実に表示されるよう、Unicode 絵文字のみを使う。
カスタム絵文字（<:name:id> 形式）は、そのサーバーに無いと表示されないため使わない。

⚠️ 異体字セレクタ（U+FE0F）付きの絵文字は、文字列を手で書くと選択子が
   抜けてモノクロのテキスト記号として表示されることがある。
   必ずこのモジュールの定数を経由して使うこと。
"""

# 状態
OK = "✅"          # 成功・完了
NG = "❌"          # 失敗・拒否
WARN = "⚠️"        # 注意
INFO = "ℹ️"        # 案内
BAN = "🚫"         # 権限なし・利用停止
MAINTENANCE = "🔧"  # メンテナンス中

# 進捗
LOADING = "⏳"     # 処理中
PROGRESS = "🔄"    # 進行中の工程
WAIT = "⬜"        # 未着手の工程

# お金
YEN = "💴"         # 金額
WALLET = "👛"      # 残高
CHARGE = "🔗"      # チャージ・リンク
CARD = "💳"        # カード

# 注文
BURGER = "🍔"      # 注文
FRIES = "🍟"       # 利用回数
DRINK = "🥤"       # ドリンク
RECEIPT = "🧾"     # 注文番号・レシート
STORE = "🏪"       # 店舗
PIN = "📍"         # 受取方法
BELL = "🔔"        # できあがり通知
CART = "🛒"        # カート
HISTORY = "📜"     # 履歴
REPEAT = "🔁"      # 再注文

# 管理
CHART = "📊"       # 統計
GEAR = "⚙️"        # 設定
KEY = "🔑"         # 認証・アカウント
USER = "👤"        # 利用者
SYNC = "♻️"        # 更新・同期
NOTE = "📝"        # メモ・感想

# その他
PARTY = "🎉"       # 初回・達成
GIFT = "🎁"        # 特典
TICKET = "🎟️"      # プロモコード
MAIL = "📨"        # DM
UNLOCK = "🔓"      # 利用条件
PLUS = "➕"
MINUS = "➖"

# サーバー管理
SHIELD = "🛡️"      # 監視・守り
LOG = "📋"         # 記録・ログ
HAMMER = "🔨"      # 処分（キック・BAN）
MUTE = "🔇"        # 発言停止（タイムアウト）
LOCK = "🔒"        # 封鎖・低速モード
BROOM = "🧹"       # 一括削除
SIREN = "🚨"       # 荒らし検知
IN = "📥"          # 入室
OUT = "📤"         # 退室
SPEAK = "🔊"       # ボイスチャンネル
PENCIL = "✏️"      # 編集
TRASH = "🗑️"       # 削除
ROBOT = "🤖"       # 自動の処理
PEOPLE = "👥"      # メンバー数

# アカウント健全性
GREEN = "🟢"       # ACTIVE
YELLOW = "🟡"      # DEGRADED
RED = "🔴"         # QUARANTINED / BANNED

# 健全性の状態 → 絵文字
HEALTH = {
    "ACTIVE": GREEN,
    "DEGRADED": YELLOW,
    "QUARANTINED": RED,
    "BANNED": RED,
    "DISABLED": "⚫",
}
