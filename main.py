"""
マクドナルド Discord 注文BOT — エントリポイント

起動前に下の「設定ブロック」を書き換えてください。

⚠️ 重要 ⚠️
このファイルには BOT トークンと暗号化キーを直接書きます。
**絶対に公開リポジトリへ push しないでください。**
GitHub に上げる場合は .gitignore に main.py を追加してください。
"""

# ============================================================
#  設定ブロック — ここだけ書き換えてください
# ============================================================

# Discord BOT トークン
#   Developer Portal → あなたのアプリ → Bot → Reset Token
DISCORD_TOKEN = ""

# BOTオーナーの Discord ユーザーID（複数可）
#   すべての管理者コマンドが使えます
OWNER_IDS = [1324938326741876758]

# 管理者ロールID（任意 / 複数可）
#   ここに入れたロールを持つ人も管理者パネルを操作できます
ADMIN_ROLE_IDS = []

# コマンドを反映させるサーバーID
#   指定あり → そのサーバーだけに即座に反映（開発・単一サーバー運用ならこちら）
#   None     → 全サーバーに反映（反映まで最大1時間かかります）
GUILD_ID = None

# 暗号化キー（認証情報の保護に使用）
#   下のコマンドで生成して貼り付けてください:
#     python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
#   ⚠️ 一度設定したら変更しないでください。変更すると保存済みの認証情報が読めなくなります。
ENCRYPTION_KEY = ""

# データベース接続先
#   SQLite（既定・そのままでOK） : "sqlite+aiosqlite:///./data/bot.db"
#   PostgreSQL                  : "postgresql+asyncpg://user:pass@host/dbname"
DATABASE_URL = "sqlite+aiosqlite:///./data/bot.db"

# ログの詳しさ  "INFO"（通常） / "DEBUG"（不具合調査時）
LOG_LEVEL = "INFO"

# ============================================================
#  ここから下は編集不要です
# ============================================================

import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
COGS_DIR = BASE_DIR / "cogs"
SYNC_STATE_FILE = DATA_DIR / "sync_state.json"
PID_FILE = DATA_DIR / "bot.pid"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


# ------------------------------------------------------------
#  起動前チェック
# ------------------------------------------------------------

def validate_config() -> None:
    """設定の書き忘れを、起動前にわかりやすく知らせる。"""
    errors = []
    if not DISCORD_TOKEN:
        errors.append("DISCORD_TOKEN が空です。Developer Portal から取得して設定してください。")
    if not ENCRYPTION_KEY:
        errors.append(
            "ENCRYPTION_KEY が空です。次のコマンドで生成して設定してください:\n"
            '      python -c "import os,base64;'
            'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
        )
    if not OWNER_IDS:
        errors.append("OWNER_IDS が空です。あなたの Discord ユーザーIDを設定してください。")

    if errors:
        log.critical("設定に不備があります:")
        for e in errors:
            log.critical("  ✗ %s", e)
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
        self.encryption_key = ENCRYPTION_KEY
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
