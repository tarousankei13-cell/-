"""
マクドナルド Discord 注文BOT — エントリポイント

設定の入れ方は2通りあります。どちらでも構いません。

  A) 下の「設定ブロック」に直接書く（かんたん）
  B) ホスティング側の環境変数に入れる（安全・おすすめ）

直接書いた値が優先され、空欄なら環境変数を読みます。

⚠️ A) を選ぶ場合の注意 ⚠️
このファイルにトークンと暗号化キーが平文で入ります。
**絶対に公開リポジトリへ push しないでください。**
GitHub に上げる場合は .gitignore の `# main.py` のコメントを外してください。
"""

# ============================================================
#  設定ブロック — ここだけ書き換えてください
#
#  ★ 貼り付けるときは、前後のダブルクォート " を消さないこと ★
#
#      DISCORD_TOKEN = "ここに貼る"
#                      ↑        ↑  この2つは残す
#
#  片方でも消すと SyntaxError: unterminated string literal になります。
# ============================================================

# Discord BOT トークン
#   Developer Portal → あなたのアプリ → Bot → Reset Token
#   環境変数でも可: DISCORD_TOKEN / DISCORD_BOT_TOKEN / TOKEN / BOT_TOKEN
DISCORD_TOKEN = ""

# BOTオーナーの Discord ユーザーID（複数可）
#   すべての管理者コマンドが使えます
#   環境変数でも可: OWNER_IDS（カンマ区切り 例 123,456）
OWNER_IDS = [1324938326741876758]

# 管理者ロールID（任意 / 複数可）
#   ここに入れたロールを持つ人も管理者パネルを操作できます
#   環境変数でも可: ADMIN_ROLE_IDS（カンマ区切り）
ADMIN_ROLE_IDS = []

# 招待キャンペーンで、Discordの招待リンクを自動で追いかけるか
#   True にする前に、Developer Portal → Bot →
#   「SERVER MEMBERS INTENT」を必ず有効にしてください。
#   有効にせずに True のまま起動すると、BOTが立ち上がりません。
#   False でも、招待コードを本人に入力してもらう方式は使えます。
#   環境変数でも可: INVITE_AUTO_TRACK（true / false）
INVITE_AUTO_TRACK = False

# サーバー管理機能（チケット・認証・監視・モデレーション）を使うか
#   True にする前に、Developer Portal → Bot →
#   「SERVER MEMBERS INTENT」を必ず有効にしてください。
#   入退室の記録・認証・レイド検知は、これが無いと動きません。
#   （チケットとモデレーションは False でも使えます）
#   環境変数でも可: SERVER_MANAGEMENT（true / false）
SERVER_MANAGEMENT = False

# メッセージの内容を読む機能を使うか
#   ・消されたメッセージ／編集されたメッセージの**内容**の記録
#   ・NGワード、招待リンクの検知
#   ・同じ文の連投の検知
#   ・感想ゲート
#   これらを使う場合だけ True にしてください。
#
#   ⚠️ Developer Portal → Bot → 「MESSAGE CONTENT INTENT」を
#      有効にする必要があります。有効にせず True にすると起動しません。
#   ⚠️ False でも、入退室・ロール変更・メンション爆撃・連投の速さの
#      検知は動きます（内容を読まずに数えられるため）。
#   環境変数でも可: MESSAGE_CONTENT（true / false）
MESSAGE_CONTENT = False

# コマンドを反映させるサーバーID
#   指定あり → そのサーバーだけに即座に反映（単一サーバー運用ならこちら）
#   None     → 全サーバーに反映（反映まで最大1時間かかります）
#   環境変数でも可: GUILD_ID
GUILD_ID = None

# 暗号化キー ★空のままで大丈夫です★
#   登録したアカウント情報をDBの中で暗号化するための鍵です。
#   空にしておくと初回起動時に自動生成し、data/encryption_key.txt に保存します。
#   次回以降はそのファイルを読むので、設定する必要はありません。
#   （自分で決めたい場合や、環境変数 ENCRYPTION_KEY で渡したい場合だけ使ってください）
ENCRYPTION_KEY = ""

# データベース接続先
#   SQLite（既定・そのままでOK） : "sqlite+aiosqlite:///./data/bot.db"
#   PostgreSQL                  : "postgresql+asyncpg://user:pass@host/dbname"
#   環境変数でも可: DATABASE_URL
DATABASE_URL = "sqlite+aiosqlite:///./data/bot.db"

# BOT全体が外へ出るときに通すプロキシ（任意・空ならそのまま出ます）
#   書き方  "http://ホスト:ポート"
#           "http://利用者名:パスワード@ホスト:ポート"（認証あり）
#
#   ⚠️ 使えるのは http と https だけです。socks には対応していません
#      （追加の部品が必要になるうえ、Discord への接続では使えないため）。
#
#   ここに入れると、Discord・マクドナルド・Kyash・PayPay・画像取得の
#   すべてがこのプロキシを通ります。
#   サービスごとに分けたいときは、起動後に /proxy set で設定できます
#   （そちらの設定が、ここに書いた値より優先されます）。
#
#   ⚠️ PayPay と マクドナルド は日本国内からしか使えません。
#      海外のサーバーで動かす場合は、日本のプロキシを必ず入れてください。
#   環境変数でも可: BOT_PROXY
PROXY_URL = ""

# ログの詳しさ  "INFO"（通常） / "DEBUG"（不具合調査時）
LOG_LEVEL = "INFO"

# ============================================================
#  ここから下は編集不要です
# ============================================================

import base64
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands


# ------------------------------------------------------------
#  設定の解決（直接書いた値 → 環境変数 の順に探す）
# ------------------------------------------------------------

def _from_env(*names: str) -> str:
    """環境変数を順に探す。前後の空白と、誤って含めた引用符は取り除く。"""
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip().strip('"').strip("'")
    return ""


def _resolve_text(written: str, *env_names: str) -> str:
    text = (written or "").strip()
    return text if text else _from_env(*env_names)


def _resolve_ids(written, *env_names: str) -> list[int]:
    if written:
        return [int(v) for v in written]
    raw = _from_env(*env_names).replace(" ", "")
    return [int(p) for p in raw.split(",") if p.isdigit()]


DISCORD_TOKEN = _resolve_text(
    DISCORD_TOKEN, "DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "TOKEN", "BOT_TOKEN"
)
ENCRYPTION_KEY = _resolve_text(ENCRYPTION_KEY, "ENCRYPTION_KEY")
INVITE_AUTO_TRACK = (
    os.getenv("INVITE_AUTO_TRACK", "").strip().lower() in ("1", "true", "yes")
    or INVITE_AUTO_TRACK
)
SERVER_MANAGEMENT = (
    os.getenv("SERVER_MANAGEMENT", "").strip().lower() in ("1", "true", "yes")
    or SERVER_MANAGEMENT
)
MESSAGE_CONTENT = (
    os.getenv("MESSAGE_CONTENT", "").strip().lower() in ("1", "true", "yes")
    or MESSAGE_CONTENT
)
OWNER_IDS = _resolve_ids(OWNER_IDS, "OWNER_IDS")
ADMIN_ROLE_IDS = _resolve_ids(ADMIN_ROLE_IDS, "ADMIN_ROLE_IDS")
DATABASE_URL = (
    _resolve_text(DATABASE_URL, "DATABASE_URL") or "sqlite+aiosqlite:///./data/bot.db"
)
if GUILD_ID is None:
    _guild = _from_env("GUILD_ID")
    GUILD_ID = int(_guild) if _guild.isdigit() else None
_PROXY_WRITTEN = bool((PROXY_URL or "").strip())
PROXY_URL = _resolve_text(PROXY_URL, "BOT_PROXY", "HTTPS_PROXY", "ALL_PROXY")
PROXY_SOURCE = (
    "main.py の PROXY_URL" if _PROXY_WRITTEN
    else ("環境変数" if PROXY_URL else "")
)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
COGS_DIR = BASE_DIR / "cogs"
SYNC_STATE_FILE = DATA_DIR / "sync_state.json"
PID_FILE = DATA_DIR / "bot.pid"
KEY_FILE = DATA_DIR / "encryption_key.txt"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    # %(corr)s は相関ID。1回の注文に紐づくログを絞り込めるようにする。
    format="%(asctime)s [%(levelname)s] %(name)s: %(corr)s%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 相関IDを全てのログ行に載せる（付いていない行は空欄になる）
import emoji as E  # noqa: E402
from core.telemetry import CorrelationFilter  # noqa: E402

for _handler in logging.root.handlers:
    _handler.addFilter(CorrelationFilter())
# 通信ライブラリは1リクエストごとに INFO を出すため、店舗同期を回すと
# 15分ごとに数百行が流れてログが読めなくなる。警告だけにしておく。
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("bot")

# プロキシの既定値を登録する。
#   ⚠️ import より前には置けないが、通信が始まる前でなければならない。
#      ここは logging の設定直後で、まだ何も通信していない。
from core import proxy  # noqa: E402

proxy.set_bootstrap(PROXY_URL, source=PROXY_SOURCE or "main.py")
if PROXY_URL:
    log.info("プロキシを使います（%s）: %s", PROXY_SOURCE, proxy.mask(PROXY_URL))


# ------------------------------------------------------------
#  起動前チェック
# ------------------------------------------------------------

def load_or_create_encryption_key() -> str:
    """
    暗号化キーを用意する。

    設定されていなければ自動生成し、data/encryption_key.txt に保存する。
    次回以降はそのファイルから読むので、利用者が意識する必要はない。

    この鍵はDBの中のアカウント情報（マクドナルド・Kyashの認証情報）だけを
    守るためのもの。残高や注文履歴は暗号化していないため、万一この鍵を
    失っても、アカウントを登録し直せば元どおり使える。
    """
    if ENCRYPTION_KEY:
        return ENCRYPTION_KEY

    if KEY_FILE.exists():
        saved = KEY_FILE.read_text().strip()
        if saved:
            return saved

    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    KEY_FILE.write_text(key)
    try:
        KEY_FILE.chmod(0o600)   # 本人だけが読めるようにする
    except OSError:
        pass
    log.info("暗号化キーを自動生成しました: %s", KEY_FILE)
    log.info("  このファイルは登録済みアカウントの復号に必要です。消さないでください。")
    return key


def validate_config() -> None:
    """設定の書き忘れや貼り間違いを、起動前にわかりやすく知らせる。"""
    errors = []
    if not DISCORD_TOKEN:
        errors.append(
            "DISCORD_TOKEN が空です。\n"
            "      main.py に直接書くか、環境変数 DISCORD_TOKEN に設定してください。"
        )
    elif DISCORD_TOKEN.count(".") != 2 or len(DISCORD_TOKEN) < 50:
        # BOTトークンは「.」で3つに区切られた形。貼り付けミスを早めに知らせる。
        errors.append(
            "DISCORD_TOKEN の形式が正しくないようです。\n"
            "      トークンの一部が欠けていないか確認してください。\n"
            "      （読み込めた文字数: %d文字）" % len(DISCORD_TOKEN)
        )

    # ENCRYPTION_KEY は空でよい（初回起動時に自動生成する）
    if not OWNER_IDS:
        errors.append("OWNER_IDS が空です。あなたの Discord ユーザーIDを設定してください。")

    if errors:
        log.critical("設定に不備があります:")
        for e in errors:
            log.critical("  ✗ %s", e)
        log.critical("")
        log.critical("  設定の入れ方は main.py の冒頭か README.md をご覧ください。")
        sys.exit(1)


def acquire_pid_lock() -> None:
    """
    二重起動を防ぐ。

    BOTが2つ動くと、同じ注文を二重に処理したり、スラッシュコマンドの
    同期が競合したりする。
    """
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and old_pid != os.getpid():
            try:
                os.kill(old_pid, 0)  # シグナル0は生存確認のみ
            except ProcessLookupError:
                log.warning("前回のプロセス (PID %s) は終了済みです。ロックを再取得します。", old_pid)
            except PermissionError:
                log.critical("BOTは既に PID %s で起動しています。二重起動はできません。", old_pid)
                sys.exit(1)
            else:
                log.critical("BOTは既に PID %s で起動しています。二重起動はできません。", old_pid)
                sys.exit(1)
    PID_FILE.write_text(str(os.getpid()))


# ------------------------------------------------------------
#  BOT 本体
# ------------------------------------------------------------

class McdBot(commands.Bot):
    def __init__(self) -> None:
        # 既定では特権インテントを一切要求しない。
        # 要求しなければ Discord側の申請も不要で、そのまま動かせる。
        intents = discord.Intents.default()

        # SERVER MEMBERS INTENT が要るもの
        #   ・招待キャンペーンの自動追跡
        #   ・入退室の記録、認証、レイド検知、メンバー数カウンター
        # Developer Portal → Bot → SERVER MEMBERS INTENT を有効にすること。
        # 有効にしていない状態で起動すると PrivilegedIntentsRequired で
        # 止まるため、設定で切れるようにしてある。
        if INVITE_AUTO_TRACK or SERVER_MANAGEMENT:
            intents.members = True

        # MESSAGE CONTENT INTENT が要るもの
        #   ・消された／編集されたメッセージの内容の記録
        #   ・NGワード、招待リンク、同じ文の連投の検知
        #   ・感想ゲート
        # ⚠️ 無くても、入退室の記録・メンション爆撃・連投の速さは数えられる。
        #    内容を読まずに判定できるものは、こちらを要求せずに動かす。
        if MESSAGE_CONTENT:
            intents.message_content = True

        # Discord 自身への接続（API・ゲートウェイ）に通すプロキシ。
        #   ⚠️ Discord は認証情報をURLに入れた形を受け取らない。
        #      URL と 利用者名/パスワード に分けて渡す必要がある。
        #   ⚠️ ここは /proxy set の Discord 設定を**使えない**。
        #      ログインはDBを読む前に始まるため、設定欄か環境変数だけが効く。
        #      設定で変えた場合は、次の起動から反映される。
        d_proxy, d_auth = proxy.split_auth(proxy.resolve("discord"))
        extra: dict = {}
        if d_proxy:
            extra["proxy"] = d_proxy
            if d_auth:
                extra["proxy_auth"] = aiohttp.BasicAuth(d_auth[0], d_auth[1])
            log.info("Discord への接続にプロキシを使います: %s",
                     proxy.mask(d_proxy))

        # ⚠️ 貸していないサーバーでは**すべてのコマンドを止める**ための器。
        #    コマンドごとに判定を書くと、足すたびに書き忘れて穴が開く。
        #    入口を1つにしてある（ui/gate.py）。
        from ui.gate import LicensedTree

        super().__init__(
            # ⚠️ command_prefix に "/" を使わない。
            #    スラッシュコマンドと衝突し、コマンドが二重に見える原因になる。
            command_prefix=commands.when_mentioned,
            intents=intents,
            owner_ids=set(OWNER_IDS),
            help_command=None,
            tree_cls=LicensedTree,
            **extra,
        )
        self._synced = False
        self.admin_role_ids = set(ADMIN_ROLE_IDS)
        # サーバーごとの「招待リンク → 使われた回数」。入室時の比較に使う。
        self._invite_uses: dict[int, dict[str, int]] = {}
        self.encryption_key = load_or_create_encryption_key()
        self.database_url = DATABASE_URL

    # -- 起動シーケンス ---------------------------------------

    async def setup_hook(self) -> None:
        """
        ログイン直後・on_ready の前に呼ばれる。

        ⚠️ ここではスラッシュコマンドを同期しない。
           この時点では self.guilds が空で、残留したギルドコマンドを
           掃除できないため（二重表示の原因）。同期は on_ready で行う。
        """
        from core.crypto import init_cipher
        from db.session import init_db

        init_cipher(self.encryption_key)
        await init_db(self.database_url)
        log.info("データベースを初期化しました")

        from core import settings

        await settings.load_all()

        # 開いているチケットを覚え直す。
        # ⚠️ これをしないと、再起動後に「発言があった」ことを拾えず、
        #    対応中のチケットが放置と見なされて自動で閉じてしまう。
        try:
            from services.server import tickets

            n = await tickets.reload_cache()
            if n:
                log.info("対応中のお問い合わせ %d 件を読み込みました", n)
        except Exception:
            log.warning("お問い合わせの読み込みに失敗しました", exc_info=True)

        # 相手の名前を先に引いておく。最初の注文で待たずに済むほか、
        # DNSが引けなくなっても前回の結果で動き続けられる。
        from core import dns

        await dns.prewarm()

        # 店名で検索できるよう、同梱の店舗一覧を読み込む
        from services.mcd.store_index import load_index

        count = load_index()
        if count:
            log.info("店名検索が使えます（%d 店舗）", count)

        await self._load_cogs()
        await self._register_persistent_views()
        # ⚠️ 注文番号ページは on_ready を待たずに立ち上げる。
        #    ホスティングサービスは「すぐポートが開くか」で起動の成否を
        #    判断することが多く、Discordへの接続完了まで待つと
        #    起動失敗と見なされることがある。
        await self._start_web()

    async def _load_cogs(self) -> None:
        if not COGS_DIR.exists():
            return
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            ext = f"cogs.{path.stem}"
            try:
                await self.load_extension(ext)
                log.info("Cog を読み込みました: %s", ext)
            except Exception:
                log.exception("Cog の読み込みに失敗しました: %s", ext)

    async def _register_persistent_views(self) -> None:
        """
        常設パネルのボタンを復元する。

        これを呼ばないと、再起動後にチャンネルへ貼ってあるパネルの
        ボタンが反応しなくなる。
        """
        try:
            from ui.panels import PERSISTENT_VIEWS
        except ImportError:
            log.debug("常設パネルはまだ未実装です")
            return
        for view_factory in PERSISTENT_VIEWS:
            self.add_view(view_factory())
        log.info("常設パネルを %d 件復元しました", len(PERSISTENT_VIEWS))

    async def on_ready(self) -> None:
        log.info("ログインしました: %s (ID: %s)", self.user, self.user.id)
        log.info("discord.py %s / サーバー数 %d", discord.__version__, len(self.guilds))

        await self._ensure_home_guild()

        if not self._synced:
            try:
                result = await self.sync_commands()
                log.info("スラッシュコマンド: %s", result)
            except Exception:
                log.exception("スラッシュコマンドの同期に失敗しました")
            self._synced = True

        await self._refresh_panels()
        await self._refresh_invite_cache()
        await self._report_licenses()

    # -- BOTの貸し出し ------------------------------------------

    async def _ensure_home_guild(self) -> None:
        """持ち主自身のサーバーを「ホーム」にしておく。

        ⚠️ これが無いと詰む。貸していないサーバーでは全コマンドが
           止まるので、ホームが未設定だと `/lend` すら打てず、
           自分で自分を締め出すことになる。

        ⚠️ 入っているサーバーが**1つのときだけ**自動で決める。
           複数あるときに勝手に選ぶと、よその貸し先をホームにして
           しまい、無期限で使われ続ける。
        """
        from core import license as lic

        try:
            if await lic.home_guild_id() is not None:
                return
            if len(self.guilds) == 1:
                g = self.guilds[0]
                await lic.set_home(g.id)
                log.info("ホームサーバーを自動設定しました: %s (%s)", g.name, g.id)
                return
            log.warning(
                "ホームサーバーが未設定です。サーバーが %d 個あるため自動では決めません。"
                "オーナーが `/lend home` を実行してください", len(self.guilds),
            )
        except Exception:
            log.exception("ホームサーバーの確認に失敗しました")

    async def _report_licenses(self) -> None:
        """起動時に、貸し出しの状況をログへ出す。"""
        from core import license as lic

        try:
            rows = await lic.all_licenses()
        except Exception:
            log.exception("貸し出しの一覧を読めませんでした")
            return
        known = {r.guild_id for r in rows}
        for g in self.guilds:
            st = await lic.status(g.id)
            mark = "ホーム" if st.is_home else (
                f"残り{st.days_left}日" if st.allowed else f"停止({st.reason})"
            )
            log.info("  サーバー %s (%s): %s", g.name, g.id, mark)
            if g.id not in known:
                log.warning(
                    "    ⚠️ 貸し出していないサーバーに入っています。"
                    "`/lend grant` で貸すか、退出させてください"
                )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """招かれたサーバー。貸していなければ、その旨だけ伝える。

        ⚠️ 勝手に使えるようにしない。マクドナルドのアカウントと
           カードはこちら持ちなので、無断で使われると損害になる。
        """
        from core import license as lic

        try:
            st = await lic.status(guild.id)
        except Exception:
            log.exception("招待先の確認に失敗しました: %s", guild.id)
            return
        log.info("サーバーに招かれました: %s (%s) 使える=%s", guild.name, guild.id,
                 st.allowed)
        if st.allowed:
            return
        ch = guild.system_channel
        if ch is None:
            ch = next((c for c in guild.text_channels
                       if c.permissions_for(guild.me).send_messages), None)
        if ch is None:
            return
        try:
            from ui.gate import blocked_embed
            await ch.send(embed=blocked_embed(st))
        except discord.HTTPException:
            log.debug("招待先への案内を送れませんでした", exc_info=True)

    # -- 招待の自動追跡 -----------------------------------------
    #
    # Discord は「誰の招待リンクで入ったか」を直接教えてくれない。
    # 入室の前後で各リンクの使用回数を見比べて、増えたものを探す。
    #
    # ⚠️ 同時に2人入ると取り違えることがある。そのときは紐づけず、
    #    本人にコードを入力してもらう（パネルの保険の方）に任せる。
    #    間違った人に特典を渡すより、渡さないほうがまだよい。

    async def _snapshot_invites(self, guild: discord.Guild) -> dict[str, int]:
        try:
            return {i.code: (i.uses or 0) for i in await guild.invites()}
        except (discord.Forbidden, discord.HTTPException):
            return {}

    async def _refresh_invite_cache(self) -> None:
        if not INVITE_AUTO_TRACK:
            return
        for guild in self.guilds:
            self._invite_uses[guild.id] = await self._snapshot_invites(guild)

    async def on_member_join(self, member: discord.Member) -> None:
        if not INVITE_AUTO_TRACK or member.bot:
            return
        try:
            await self._track_join(member)
        except Exception:
            log.exception("招待の自動追跡に失敗しました")

    async def _track_join(self, member: discord.Member) -> None:
        from core import invite as inv
        from core import users as user_repo

        before = self._invite_uses.get(member.guild.id, {})
        after = await self._snapshot_invites(member.guild)
        self._invite_uses[member.guild.id] = after

        grown = [code for code, uses in after.items() if uses > before.get(code, 0)]
        if len(grown) != 1:
            # 0件＝権限が無い／リンク以外から参加
            # 2件以上＝同時入室で見分けがつかない
            log.info("招待元を特定できませんでした（候補 %d 件）", len(grown))
            return

        code = grown[0]

        # ⚠️ BOT が本人に代わって作った招待は、Discord 側の inviter が
        #    **BOT** になる。先に控えの表から持ち主を引くこと。
        #    ここを飛ばすと、紹介者ではなく BOT の実績になってしまう。
        inviter_id = await inv.owner_of_link(code)
        if inviter_id is None:
            inviter = next(
                (i.inviter for i in await member.guild.invites() if i.code == code),
                None,
            )
            if inviter is None or inviter.bot:
                return
            inviter_id = inviter.id
        if inviter_id == member.id:
            return

        await user_repo.get_or_create(member.id)
        await user_repo.get_or_create(inviter_id)
        try:
            # 自動で入った方は「参加からの時間」は問わない（入った瞬間なので）
            inv.check_eligibility(
                account_created=member.created_at, joined_at=None, manual=False,
            )
            await inv.link(
                member.id, inviter_id, source="auto", guild_id=member.guild.id,
            )
        except inv.InviteError as e:
            log.info("自動の紐づけを見送りました: %s", e)
            return

        # ⚠️ ここではまだ特典を渡さない。本人が DM の受取ボタンを
        #    押して初めて数に入る。
        from ui import invite_flows

        await invite_flows.send_claim_dm(self, member.id, inviter_id)

    async def _start_web(self) -> None:
        """
        注文番号ページを立ち上げる（/config web で有効にしたときだけ）。

        ⚠️ 失敗しても BOT は動かす。注文番号は DM の控えにも入っている。
        """
        try:
            from services import web as web_site

            url = await web_site.start()
        except Exception:
            log.exception("注文番号ページの起動に失敗しました")
            return
        if url:
            log.info("注文番号ページ: %s", url)

    async def _refresh_panels(self) -> None:
        """
        設置済みパネルを起動時に最新にする。

        ボタンの反応は `_register_persistent_views` で戻るが、
        **チャンネルに貼ってあるメッセージの中身は古いまま**になる。
        新しいボタンを足しても、貼り直すか `/panel refresh` を
        叩くまで出てこない。管理者がそれを覚えておくのは無理なので、
        起動のたびに自動で合わせる。

        ⚠️ ここで失敗しても BOT は動く。絶対に起動を止めない。
        """
        try:
            from cogs.panel import refresh_all

            lines = await refresh_all(self)
        except Exception:
            log.exception("パネルの更新に失敗しました")
            return

        if not lines:
            return
        failed = [l for l in lines if l.startswith(E.NG)]
        if failed:
            log.warning("パネルを更新しました（%d件中%d件が失敗）: %s",
                        len(lines), len(failed), " / ".join(failed))
        else:
            log.info("パネルを最新にしました（%d件）", len(lines))

    # -- スラッシュコマンドの同期（二重表示の解消） -------------

    def _command_signature(self) -> str:
        """
        コマンド定義のハッシュ。

        定義が変わっていなければ同期をスキップする。
        Discord の同期APIはレート制限が厳しいため、毎回叩かない。
        """
        payloads = []
        for cmd in self.tree.get_commands():
            try:
                data = cmd.to_dict(self.tree)
            except TypeError:  # discord.py のバージョン差を吸収
                data = cmd.to_dict()
            payloads.append(json.dumps(data, sort_keys=True, ensure_ascii=False))
        blob = "".join(sorted(payloads))
        return hashlib.sha256(blob.encode()).hexdigest()

    def _load_sync_state(self) -> dict:
        try:
            return json.loads(SYNC_STATE_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save_sync_state(self, signature: str) -> None:
        SYNC_STATE_FILE.write_text(
            json.dumps({"signature": signature, "guild_id": GUILD_ID}, ensure_ascii=False)
        )

    async def _clear_global_commands(self) -> None:
        """グローバル登録を空にする（tree は変更しない）。"""
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, [])
        except Exception:
            log.exception("グローバルコマンドの削除に失敗しました")

    async def _clear_guild_commands(self, guild_id: int) -> None:
        """指定ギルドの登録を空にする（tree は変更しない）。"""
        try:
            await self.http.bulk_upsert_guild_commands(self.application_id, guild_id, [])
        except Exception:
            log.exception("ギルドコマンドの削除に失敗しました: %s", guild_id)

    async def sync_commands(self, *, force: bool = False) -> str:
        """
        スラッシュコマンドを同期する。

        コマンドが Discord 上で二重に表示される原因は、同じコマンドが
        「グローバル」と「ギルド」の両方に登録されていること。
        過去に片方へ同期した履歴はもう片方へ切り替えても残り続けるため、
        毎回 “使わない側を明示的に空にする” 必要がある。
        """
        signature = self._command_signature()
        state = self._load_sync_state()
        if (
            not force
            and state.get("signature") == signature
            and state.get("guild_id") == GUILD_ID
        ):
            return "定義に変更がないため同期をスキップしました"

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            # ① 先にグローバル定義をギルドへコピーする。
            #    （順序が逆だとコピー元が空になり、コマンドが1つも登録されない）
            self.tree.clear_commands(guild=guild)  # ギルド側の器だけを空にする
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            # ② 使わないグローバル登録を消す（二重表示の解消）
            await self._clear_global_commands()
            scope = f"サーバー {GUILD_ID}"
        else:
            # ① 使わないギルド登録を全サーバー分消す（二重表示の解消）
            for guild in self.guilds:
                await self._clear_guild_commands(guild.id)
            # ② グローバルへ登録
            synced = await self.tree.sync()
            scope = "全サーバー（グローバル）"

        self._save_sync_state(signature)
        return f"{scope} に {len(synced)} 件を同期しました"

    # -- 後片付け ---------------------------------------------

    async def close(self) -> None:
        """
        終了時の後片付け。

        ⚠️ close は1つだけにすること。以前は2つ定義してしまっていて、
           あとに書いたほうが勝ち、注文番号ページが止まらないままだった。
           同じポートで立ち上げ直すと「使用中」で失敗する。

        ⚠️ ひとつ失敗しても残りは必ず片付ける。
           途中で例外が出ると、その先が実行されない。
        """
        for name, job in (
            ("注文番号ページ", self._stop_web),
            ("通信の後始末", self._stop_http),
            ("データベース", self._stop_db),
        ):
            try:
                await job()
            except Exception:
                log.exception("%sの停止に失敗しました", name)
        await super().close()

    async def _stop_web(self) -> None:
        from services import web as web_site

        await web_site.stop()

    async def _stop_http(self) -> None:
        from core.http import close_shared

        await close_shared()

    async def _stop_db(self) -> None:
        from db.session import close_db

        await close_db()


# ------------------------------------------------------------
#  起動
# ------------------------------------------------------------

def main() -> None:
    validate_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    acquire_pid_lock()

    bot = McdBot()
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.critical("DISCORD_TOKEN が正しくありません。設定を確認してください。")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "特権インテントが必要と言われました。Developer Portal → Bot で "
            "SERVER MEMBERS INTENT を有効にしてください。"
        )
        sys.exit(1)
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
