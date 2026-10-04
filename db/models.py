"""
データベースモデル

設計は docs/04 §3 / docs/08 §7.4 を参照。

移植性のための方針:
  - UUID は String(36) で保持する（SQLite と PostgreSQL の両方で動くように）
  - JSON は Text に文字列で格納する（同上）
  - 金額はすべて BigInteger の「円」。小数は使わない
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite は INTEGER PRIMARY KEY しか自動採番しない（BIGINT だと採番されない）。
# PostgreSQL では BIGSERIAL、SQLite では INTEGER になるようにする。
AutoBigInt = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """
    DBから読んだ日時を、必ずタイムゾーン付き(UTC)にして返す。

    ⚠️ SQLite は `DateTime(timezone=True)` を指定してもタイムゾーンを
       保存しない。書き込むときは付いていても、読み戻すと naive になる。
       そのまま `datetime.now(timezone.utc)` と引き算・比較すると
       `can't subtract offset-naive and offset-aware datetimes` で落ちる。

       保存している値は常にUTCなので、tzinfo が無ければUTCとして扱う。
       PostgreSQL では最初から付いているため、その場合はそのまま返す。

    DBから読んだ日時をPython側で計算に使うときは、**必ずこれを通すこと**。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ============================================================
#  設定・利用者
# ============================================================

class BotConfig(Base):
    """/config コマンドが読み書きする設定ストア（キー・バリュー）。"""
    __tablename__ = "bot_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON文字列
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Panel(Base):
    """設置済みの常設パネル。/panel refresh で貼り直すためにIDを覚えておく。"""
    __tablename__ = "panels"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)  # order/charge/admin
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deployed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 実績パネル用の匿名コード（例 U-7F3A）。discord_id から導出し、不変。
    anon_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 感想ゲートが有効なときだけ使う
    feedback_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    # 前回の注文内容。次回の手数を減らすために覚えておく。
    #   同じ店・同じ受取方法で頼む人が多いので、1タップで戻せるようにする。
    last_store_id: Mapped[str | None] = mapped_column(String(8))
    last_store_name: Mapped[str | None] = mapped_column(String(128))
    last_pickup: Mapped[str | None] = mapped_column(String(24))
    # 招待キャンペーン用。本人の招待コードと、誰に招待されたか。
    invite_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    invited_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # 紹介プログラムのお知らせを受け取るか（通知設定ボタンで切り替える）
    invite_notify: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================
#  複式元帳
# ============================================================

class Ledger(Base):
    """
    すべての残高変動を記録する追記専用の元帳。

    残高カラムを直接 UPDATE する設計は必ず壊れるため、残高は
    このテーブルの合計として導出する。

    不変条件: 同じ tx_id の amount の合計は必ず 0 になる。

    勘定科目:
      issuance              系の外から入ってきた金（チャージの相手勘定）
      user:<discord_id>     利用者の利用可能残高
      user:<discord_id>:hold 注文処理中のホールド
      subsidy_pool          管理者負担のプール
      settlement            マクドナルドへ支払った累計
    """
    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    tx_id: Mapped[str] = mapped_column(String(36), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 正=増 / 負=減
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(36))
    receipt_id: Mapped[str | None] = mapped_column(String(36))
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_ledger_account", "account"),
        Index("idx_ledger_tx", "tx_id"),
        Index("idx_ledger_created", "created_at"),
    )


class SubsidyRule(Base):
    """
    負担率ルール。priority が小さいほど優先。

    解決順: user 個別 → role → global
    """
    __tablename__ = "subsidy_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # user/role/global
    target_id: Mapped[int | None] = mapped_column(BigInteger)       # user_id or role_id
    subsidy_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)  # 管理者負担率(%)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    monthly_cap: Mapped[int | None] = mapped_column(BigInteger)     # 月間負担上限(円)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================
#  マクドナルドアカウント
# ============================================================

class McdAccount(Base):
    __tablename__ = "mcd_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    email_enc: Mapped[bytes] = mapped_column(nullable=False)
    # ⚠️ 更新のたびにローテーションする。取得したら即保存すること。
    refresh_token_enc: Mapped[bytes | None] = mapped_column()
    card_id: Mapped[str | None] = mapped_column(String(64))

    # 端末フィンガープリント（アカウント作成時に一度だけ生成して固定）
    device_uid: Mapped[str] = mapped_column(String(36), nullable=False)
    wmop_device_id: Mapped[str] = mapped_column(String(36), nullable=False)
    fb_instance_id: Mapped[str] = mapped_column(String(32), nullable=False)
    home_lat: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    home_lng: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    proxy_url: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    orders_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class McdToken(Base):
    """トークンキャッシュ。毎回の再取得を避けて注文を速くする。"""
    __tablename__ = "mcd_tokens"

    mcd_account_id: Mapped[int] = mapped_column(
        ForeignKey("mcd_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    access_token_enc: Mapped[bytes | None] = mapped_column()
    access_exp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_paseto_enc: Mapped[bytes | None] = mapped_column()
    root_exp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StoreCache(Base):
    """
    店舗情報のキャッシュ。

    store.api.ordRootUrl から group が直接わかるため、
    group-e〜j の総当たりは不要（docs/08 §4）。
    """
    __tablename__ = "store_cache"

    store_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    group_name: Mapped[str] = mapped_column(String(16), nullable=False)
    store_name: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6))
    cat_root_url: Mapped[str | None] = mapped_column(String(128))
    ord_root_url: Mapped[str | None] = mapped_column(String(128))
    # 対応する受取方法 {"takeOut": true, ...} をJSONで
    delivery_methods: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 解決した回数。管理者の統計用で、利用者には見せない。
    # （以前は「最近よく使われているお店」として出していたが、
    #   他の利用者の行動が伝わるため取りやめた）
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # ETag を覚えておき、変更が無ければ 304 で済ませる（高頻度同期のため）
    store_etag: Mapped[str | None] = mapped_column(String(128))
    # 選択肢専用の商品（ソース・ドレッシング・おもちゃ）。JSON配列。
    # products に載らないため menu_products には入れられない。
    menu_extras: Mapped[str | None] = mapped_column(Text)
    menu_etag: Mapped[str | None] = mapped_column(String(128))
    menu_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 注文できるかの判定に使う（店舗を選んだ時点で理由を出すため）
    #   mop_enabled : モバイルオーダーに対応しているか
    #   foe_status  : 店舗の稼働状態。NORMAL 以外は一時休業など
    #   method_hours: 受取方法ごとの営業時間 {"eatIn": {"2026-09-30": {...}}} をJSONで
    mop_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    foe_status: Mapped[str | None] = mapped_column(String(32))
    method_hours: Mapped[str | None] = mapped_column(Text)


# ============================================================
#  Kyash
# ============================================================

class KyashAccount(Base):
    __tablename__ = "kyash_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    email_enc: Mapped[bytes] = mapped_column(nullable=False)
    password_enc: Mapped[bytes | None] = mapped_column()
    # ⚠️ この2つは1セット。両方あれば再ログイン時のOTPが不要になる
    client_uuid: Mapped[str | None] = mapped_column(String(36))
    installation_uuid: Mapped[str | None] = mapped_column(String(36))
    access_token_enc: Mapped[bytes | None] = mapped_column()
    token_obtained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_kyc: Mapped[bool | None] = mapped_column(Boolean)
    monthly_cap: Mapped[int | None] = mapped_column(BigInteger)
    received_this_month: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_balance: Mapped[int | None] = mapped_column(BigInteger)
    proxy_url: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KyashReceipt(Base):
    """
    チャージ（送金リンクの受け取り）の記録。

    link_uuid の UNIQUE 制約が、同じリンクを2回使われることを防ぐ要。
    """
    __tablename__ = "kyash_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    link_uuid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kyash_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("kyash_accounts.id", ondelete="SET NULL")
    )
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(64))
    wallet_before: Mapped[int | None] = mapped_column(BigInteger)
    wallet_after: Mapped[int | None] = mapped_column(BigInteger)
    # RESERVED/RECEIVING/RECEIVED/CREDITED/FAILED/MANUAL_REVIEW
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_link: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ============================================================
#  注文
# ============================================================

class Order(Base):
    """注文。state は docs/04 §4 の状態機械に従う。"""
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # ボタン連打・再送による二重注文を防ぐ
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)

    hex_payload: Mapped[str] = mapped_column(Text, nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(8))
    store_name: Mapped[str | None] = mapped_column(String(128))
    group_name: Mapped[str | None] = mapped_column(String(16))
    pickup_method: Mapped[str | None] = mapped_column(String(32))
    items_json: Mapped[str | None] = mapped_column(Text)  # 複数商品に対応（docs/09 §3.2）

    list_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subsidy_rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    user_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subsidy_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # アカウントを削除しても履歴は残す（参照はNULLになる）
    mcd_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("mcd_accounts.id", ondelete="SET NULL")
    )
    order_token: Mapped[str | None] = mapped_column(Text)
    order_code: Mapped[str | None] = mapped_column(String(64))
    receipt_number: Mapped[str | None] = mapped_column(String(16))  # 注文番号（例 7161）
    # 注文番号ページのURLに入れる、推測できない合い言葉。
    # ⚠️ 注文番号そのものをURLにしてはいけない。4桁しかないので、
    #    順に試すだけで他人の注文が覗けてしまう。
    view_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    # ---- できあがり通知 ----
    # ⚠️ 相手の状態値（status）の意味は分かっていない。注文が済んだ時点の
    #    値を控えておき、**変わったら**できあがりとみなす。
    #    ブザー番号が出たら、そちらのほうが確かな合図。
    last_status: Mapped[int | None] = mapped_column(Integer)
    buzzer_number: Mapped[int | None] = mapped_column(Integer)
    ready_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    hold_tx_id: Mapped[str | None] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("idx_orders_user", "discord_id"),
        Index("idx_orders_state", "state"),
    )


class OrderEvent(Base):
    """注文の状態遷移ログ。障害調査と復旧の手がかりになる。"""
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Cart(Base):
    """
    組み立て中のカート。

    利用者はパネル操作のみなので、カートの状態を View に持たせられない
    （再起動で消えるため）。DBに保存して一時ビューから読み書きする。
    """
    __tablename__ = "carts"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)  # order / hex
    store_id: Mapped[str | None] = mapped_column(String(8))
    pickup_method: Mapped[str | None] = mapped_column(String(32))
    items_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ============================================================
#  メニューカタログ
# ============================================================

class AuditLog(Base):
    """
    管理操作の記録。

    お金を扱うため、誰がいつ何を変えたかを残す。
    「誰が負担率を60%にしたのか」「この残高付与は誰の判断か」に
    答えられないと、複数人で運用できない。

    ⚠️ この表は**消さない**。バックアップにも必ず含める。
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(AutoBigInt, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    actor_name: Mapped[str | None] = mapped_column(String(64))
    # 何をしたか（subsidy.global / balance.grant / account.remove など）
    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    # 対象（利用者ID・アカウントID・設定キーなど）
    target: Mapped[str | None] = mapped_column(String(64), index=True)
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(Text)


class Invite(Base):
    """
    招待の記録。

    ⚠️ 1人の招待は**1回だけ**成立する。discord_id を主キーにして、
       何度押しても二重に特典が出ないようにする。
       「同じ人が何回も招待された」はDBの形で起こらないようにしておく。
    """
    __tablename__ = "invites"

    # 招待された人（この人は一度しか招待されない）
    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 紐づいた方法: code（本人がコードを入力） / auto（Discordの招待から自動）
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # 特典を渡したか。条件（初回注文など）を満たすまでは False。
    rewarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reward_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # ---- ここから下は、紹介プログラムのリニューアルで増えたもの ----
    # どのサーバーでの招待か（リンクは1人1サーバーにつき1本）
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # 本人がDMの受取ボタンを押したか。押すまでは数に入れない。
    claimed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 条件を満たす注文（既定で定価400円以上）を終えたか
    qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InviteLink(Base):
    """
    BOT が本人に代わって発行した Discord の招待リンク。

    ⚠️ Discord 側の `invite.inviter` は **BOT** になるので、
       そのままでは誰の招待か分からない。ここで結び付けておく。
    """
    __tablename__ = "invite_links"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvitePayout(Base):
    """
    紹介者へ渡した特典の記録。

    ⚠️ 「2名ごとに¥500」は**何度も発火する**。同じ区切りで二度払わない
       ことを、DBの形（主キー）で保証する。計算間違いで二重に配ると
       実際にお金が出ていく。
    """
    __tablename__ = "invite_payouts"

    inviter_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # 何回目の発火か（1回目・2回目…）。達成人数ではなく回数で数える。
    milestone: Mapped[int] = mapped_column(Integer, primary_key=True)
    amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # この発火を満たした時点の達成人数（あとから検算できるように）
    reached: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Refund(Base):
    """
    実際にお金をお返しした記録。

    ⚠️ 残高を戻すだけの「取り消し」とは別物。ここに載るのは
       **Kyash で現金が出ていった** ものだけ。
       同じ申請で二度送らないよう、申請IDを主キーにする。
    """
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    # 作った送金リンク（利用者がこれを開いて受け取る）
    link_url: Mapped[str | None] = mapped_column(String(255))
    link_uuid: Mapped[str | None] = mapped_column(String(64), unique=True)
    kyash_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("kyash_accounts.id")
    )
    # PENDING（作成済み・未受取）/ DONE（受取済み）/ CANCELLED / FAILED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    requested_by: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MenuProduct(Base):
    __tablename__ = "menu_products"

    store_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    product_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name_ja: Mapped[str] = mapped_column(String(128), nullable=False)
    product_class: Mapped[str | None] = mapped_column(String(24))  # PRODUCT / VALUE_MEAL
    day_part: Mapped[str | None] = mapped_column(String(32))
    price_eatin: Mapped[int | None] = mapped_column(Integer)
    price_takeout: Mapped[int | None] = mapped_column(Integer)
    price_other: Mapped[int | None] = mapped_column(Integer)
    pre_price: Mapped[int | None] = mapped_column(Integer)  # セットの表示価格
    structure: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    time_windows: Mapped[str | None] = mapped_column(Text)  # limitedAbility の checkoutable
    # サイズ違いをまとめる代表コード。コカ・コーラ S/M/L は同じ値を持つ。
    size_group: Mapped[str | None] = mapped_column(String(16))
    # 説明文・画像・注意書きなど「見せるためだけ」の情報（JSON）。
    # 注文の組み立てには使わないので、1列にまとめて持つ。
    display: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MenuCollection(Base):
    __tablename__ = "menu_collections"

    store_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    collection_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name_ja: Mapped[str] = mapped_column(String(64), nullable=False)
    product_codes: Mapped[str] = mapped_column(Text, nullable=False)  # JSON配列
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class StoreDaypart(Base):
    """朝マック / レギュラー / ヒルマック / 夜マック の提供時間帯。"""
    __tablename__ = "store_dayparts"

    store_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    daypart: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    visible: Mapped[str] = mapped_column(Text, nullable=False)       # [{start,end}]
    checkoutable: Mapped[str] = mapped_column(Text, nullable=False)  # [{start,end}]


__all__ = [
    "Base", "utcnow", "new_uuid",
    "BotConfig", "Panel", "User", "Ledger", "SubsidyRule",
    "McdAccount", "McdToken", "StoreCache",
    "KyashAccount", "KyashReceipt",
    "Order", "OrderEvent", "Cart",
    "MenuProduct", "MenuCollection", "StoreDaypart",
]
