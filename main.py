"""マクドナルド注文BOT — 起動と設定。

設定はこのファイルに直書きする。.env に置くのは Discord トークンと
オーナーID だけで、それ以外の値はすべてここで完結させる。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from mcd import cards, logsetup
from mcd.fraud import FraudConfig, FraudEngine
from mcd.kyashclient import KyashPool
from mcd.mcdclient import McdPool
from mcd.rates import Campaign
from mcd.store import Store

load_dotenv()

# =====================================================================
#  設定 — ここから
#  ※ DISCORD_TOKEN と OWNER_IDS だけは .env から読む
# =====================================================================

# ---- チャンネル / ロール -------------------------------------------------
# すべて右クリック →「IDをコピー」で取得した数値を入れる。0 のままだと
# その機能は無効になる（起動時にどれが未設定か一覧で出る）。

GUILD_ID = 0                  # スラッシュコマンドを即反映させたいサーバー。0 でグローバル
ORDER_ROLE_ID = 0             # このロールを持つ人だけがパネルから注文できる

PANEL_CHANNEL_ID = 0          # 注文パネルを置くチャンネル
ACHIEVEMENT_CHANNEL_ID = 0    # 実績（1段階目・2段階目）の投稿先
APPROVAL_CHANNEL_ID = 0       # 承認待ち（実績の承認・上限超過の注文）

LOG_ORDERS_CHANNEL_ID = 0     # 注文のログ
LOG_MONEY_CHANNEL_ID = 0      # チャージ・残高増減のログ
LOG_ERRORS_CHANNEL_ID = 0     # 例外のログ
LOG_ADMIN_CHANNEL_ID = 0      # 設定変更・承認操作のログ

# ---- 利用者負担率 --------------------------------------------------------
# 「利用者が定価の何%を払うか」で統一する。60 なら定価590円 -> 354円。
# 小さいほど利用者が得をする。優先順位は 個別 > min(ロール別, キャンペーン, 既定)

DEFAULT_USER_RATE = 60

ROLE_RATES: dict[int, int] = {
    # 1234567890123456789: 50,   # VIPロールは50%負担
}

USER_RATES: dict[int, int] = {
    # 9876543210987654321: 40,   # この人だけ40%負担
}

CAMPAIGNS: list[Campaign] = [
    # Campaign("金曜の夜", rate=50, weekdays=(4,), start_hour=18, end_hour=23),
]

# ---- 金額 ----------------------------------------------------------------

ORDER_FACE_LIMIT = 3000       # 定価がこれを超えたらオーナー承認に回す
APPROVAL_TIMEOUT_MINUTES = 30 # 承認依頼の有効期限

MIN_CHARGE = 100              # 1回のチャージの下限
MAX_CHARGE = 30000            # 1回のチャージの上限

PHOTO_BONUS = 50              # 実績に画像を添えたときの残高ボーナス
REFERRAL_BONUS_INVITER = 200  # 紹介した人への報酬
REFERRAL_BONUS_INVITEE = 200  # 紹介された人への報酬

# ---- 規約（機能15）-------------------------------------------------------
# TERMS_VERSION を変えると、全員に再同意を求める。

TERMS_VERSION = "1.0"
TERMS_TEXT = """\
**ご利用にあたって**

1. チャージした残高は **返金・払い戻しできません**。使い切りです。
2. 注文は実際にマクドナルドへ発注されます。確定後の取り消しはできません。
3. 注文後は感想の投稿が必要です。投稿と承認が完了するまで次の注文はできません。
4. 他人の画像の転用、複数アカウントの使い分け、紹介制度の自作自演を禁止します。
5. 上記に違反した場合、残高を没収し利用を停止することがあります。

同意いただける場合のみ「同意して始める」を押してください。
"""

# ---- 不正検知（機能3・14・17）-------------------------------------------

FRAUD = FraudConfig(
    referral_min_account_age_days=30,   # 作成30日未満のDiscordアカウントは減点
    referral_min_guild_age_hours=24,    # 参加24時間未満は減点
    referral_max_per_user=20,           # 1人が紹介できる上限
    referral_review_score=40,           # このスコア以上でオーナー承認へ回す
    multi_account_sender_threshold=2,   # 同じKyashが何人に使われたら警告か
    image_hash_threshold=6,             # 画像の類似判定（小さいほど厳しい）
    order_burst_window_minutes=10,
    order_burst_max=3,
    order_amount_alert=5000,
)

# ---- 動作まわり ----------------------------------------------------------

BRAND_NAME = "Order Bot"      # 番号カードとEmbedフッターに出る名前
FONT_PATH = ""                # 番号カード用フォント。空なら自動検出
DB_PATH = "data/bot.sqlite3"
LOG_DIR = "logs"
LOG_LEVEL = logging.INFO
LOG_KEEP_DAYS = 14

BUZZER_POLL_SECONDS = 30      # 呼び出し番号を見に行く間隔
BUZZER_POLL_MAX = 20          # 最大何回見に行くか（30秒 x 20 = 10分）

MONTHLY_REPORT_DAY = 1        # 月次レポートを送る日
MONTHLY_REPORT_HOUR = 10      # 送る時刻（JST）

HEALTH_CHECK_HOURS = 6        # トークン生存確認の間隔
KYASH_TOKEN_WARN_DAYS = 7     # 失効まで何日を切ったら警告するか

ORDER_TIMEOUT_SECONDS = 120   # 確認ボタンの有効時間

# ---- 絵文字 --------------------------------------------------------------
# Discord で確実に表示される範囲だけを使う。
# 環境によって出ないものが混ざるとEmbedの組み立てで事故るので、ここで一元管理する。

E_OK = "\N{WHITE HEAVY CHECK MARK}"          # 白いチェック
E_NG = "\N{CROSS MARK}"                      # バツ
E_WARN = "\N{WARNING SIGN}"                  # 警告
E_MONEY = "\N{MONEY BAG}"                    # 財布
E_FOOD = "\N{HAMBURGER}"                     # ハンバーガー
E_GIFT = "\N{WRAPPED PRESENT}"               # プレゼント
E_MEMO = "\N{MEMO}"                          # メモ
E_CAMERA = "\N{CAMERA WITH FLASH}"           # カメラ
E_BELL = "\N{BELL}"                          # ベル
E_CHART = "\N{BAR CHART}"                    # 棒グラフ
E_LOCK = "\N{LOCK}"                          # 錠前
E_CLOCK = "\N{HOURGLASS WITH FLOWING SAND}"  # 砂時計
E_FLAG = "\N{TRIANGULAR FLAG ON POST}"       # 旗
E_SHOP = "\N{CONVENIENCE STORE}"             # 店
E_MAIL = "\N{ENVELOPE}"                      # 封筒
E_KEY = "\N{KEY}"                            # 鍵

# =====================================================================
#  設定 — ここまで
# =====================================================================


class Config:
    """main.py の定数をまとめて cog から触れるようにする入れ物。"""

    def __init__(self) -> None:
        module = sys.modules[__name__]
        for name in dir(module):
            if name.isupper():
                setattr(self, name, getattr(module, name))

    def missing_channels(self) -> list[str]:
        required = [
            "PANEL_CHANNEL_ID",
            "ACHIEVEMENT_CHANNEL_ID",
            "APPROVAL_CHANNEL_ID",
            "LOG_ORDERS_CHANNEL_ID",
            "LOG_MONEY_CHANNEL_ID",
            "LOG_ERRORS_CHANNEL_ID",
            "LOG_ADMIN_CHANNEL_ID",
            "ORDER_ROLE_ID",
        ]
        return [name for name in required if not getattr(self, name, 0)]


COGS_DIR = Path(__file__).parent / "cogs"


class Bot(commands.Bot):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True          # 紹介判定で参加日時を見るため
        intents.dm_messages = True

        owner_ids = parse_owner_ids()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            owner_ids=owner_ids or None,
            help_command=None,
        )
        self.cfg = cfg
        self.store = Store(cfg.DB_PATH)
        self.mcd = McdPool(self.store)
        self.kyash = KyashPool(self.store)
        self.fraud = FraudEngine(self.store, cfg.FRAUD)
        self.log = logging.getLogger("bot")

    async def setup_hook(self) -> None:
        for cog_file in sorted(COGS_DIR.glob("*.py")):
            if cog_file.stem.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                self.log.info("拡張を読み込みました: %s", ext)
            except Exception:
                self.log.exception("拡張の読み込みに失敗しました: %s", ext)

        if self.cfg.GUILD_ID:
            guild = discord.Object(id=self.cfg.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.log.info("スラッシュコマンドをギルド %s に同期しました", self.cfg.GUILD_ID)
        else:
            await self.tree.sync()
            self.log.info("スラッシュコマンドをグローバルに同期しました")

    async def on_ready(self) -> None:
        self.log.info("ログイン: %s (ID: %s)", self.user, self.user.id)
        missing = self.cfg.missing_channels()
        if missing:
            self.log.warning(
                "main.py で未設定の項目があります（該当機能は動きません）: %s",
                ", ".join(missing),
            )
        self.log.info(
            "マクドナルドアカウント %s 件 / Kyash アカウント %s 件",
            len(self.store.list_mcd_accounts(only_enabled=True)),
            len(self.store.list_kyash_accounts(only_enabled=True)),
        )

    async def close(self) -> None:
        try:
            self.store.close()
        finally:
            await super().close()

    # ---- ログ送信の共通口 ------------------------------------------------

    async def send_log(self, channel_id: int, embed: discord.Embed) -> None:
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                self.log.warning("ログチャンネル %s を取得できませんでした", channel_id)
                return
        try:
            await channel.send(embed=embed)
        except Exception:
            self.log.exception("ログの送信に失敗しました (channel=%s)", channel_id)


def parse_owner_ids() -> set[int]:
    raw = os.getenv("OWNER_IDS", "")
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip().isdigit()}


def main() -> None:
    cfg = Config()
    logsetup.setup(cfg.LOG_DIR, level=cfg.LOG_LEVEL, keep_days=cfg.LOG_KEEP_DAYS)
    cards.configure_font(cfg.FONT_PATH)

    log = logging.getLogger("bot")

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.critical("DISCORD_TOKEN が設定されていません。.env を確認してください。")
        sys.exit(1)
    if not parse_owner_ids():
        log.critical("OWNER_IDS が設定されていません。.env を確認してください。")
        sys.exit(1)

    bot = Bot(cfg)
    try:
        bot.run(token, log_handler=None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
