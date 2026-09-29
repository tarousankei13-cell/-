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

# ログの詳しさ  "INFO"（通常） / "DEBUG"（不具合調査時）
LOG_LEVEL = "INFO"

# ============================================================
#  ここから下は編集不要です
# ============================================================

import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

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
OWNER_IDS = _resolve_ids(OWNER_IDS, "OWNER_IDS")
ADMIN_ROLE_IDS = _resolve_ids(ADMIN_ROLE_IDS, "ADMIN_ROLE_IDS")
DATABASE_URL = (
    _resolve_text(DATABASE_URL, "DATABASE_URL") or "sqlite+aiosqlite:///./data/bot.db"
)
if GUILD_ID is None:
    _guild = _from_env("GUILD_ID")
    GUILD_ID = int(_guild) if _guild.isdigit() else None

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
COGS_DIR = BASE_DIR / "cogs"
SYNC_STATE_FILE = DATA_DIR / "sync_state.json"
PID_FILE = DATA_DIR / "bot.pid"
KEY_FILE = DATA_DIR / "encryption_key.txt"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


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
        # message_content は使わない（感想ゲート機能を使う場合のみ必要）。
        # 不要な特権インテントを要求しないことで、Discord側の申請も不要になる。
        intents = discord.Intents.default()

        super().__init__(
            # ⚠️ command_prefix に "/" を使わない。
            #    スラッシュコマンドと衝突し、コマンドが二重に見える原因になる。
            command_prefix=commands.when_mentioned,
            intents=intents,
            owner_ids=set(OWNER_IDS),
            help_command=None,
        )
        self._synced = False
        self.admin_role_ids = set(ADMIN_ROLE_IDS)
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

        await self._load_cogs()
        await self._register_persistent_views()

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

        if not self._synced:
            try:
                result = await self.sync_commands()
                log.info("スラッシュコマンド: %s", result)
            except Exception:
                log.exception("スラッシュコマンドの同期に失敗しました")
            self._synced = True

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
        from db.session import close_db

        await close_db()
        await super().close()


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
