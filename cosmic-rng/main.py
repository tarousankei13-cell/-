#!/usr/bin/env python3
"""COSMIC RNG — 起動スクリプト / launcher.

    python main.py

設定はすべて下の CONFIG に書きます。.env ファイルは不要です。
データベースは既定で SQLite（data/cosmic.db）なので、PostgreSQL が無いホストでも
そのまま動きます。初回起動時にスキーマ作成・初期コンテンツ投入・管理者アカウント
作成まで自動で行います。
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ===========================================================================
#  設定 — ここだけ書き換えれば動きます
# ===========================================================================
CONFIG: dict[str, object] = {

    # --- 管理者アカウント ---------------------------------------------------
    # 初回起動時にこの内容で作成されます。ゲーム内のログイン画面から
    # このユーザー名（またはメールアドレス）とパスワードでログインしてください。
    "ADMIN_USERNAME": "admin",
    "ADMIN_EMAIL": "admin@cosmic-rng.local",
    "ADMIN_PASSWORD": "Runakunn0513",

    # 通常は False。True にすると、起動のたびに上のパスワードへ強制的に戻します
    # （パスワードを忘れたときの復旧用。戻したら False に戻してください）。
    "ADMIN_RESET_PASSWORD": False,

    # --- 公開設定 -----------------------------------------------------------
    # ブラウザからアクセスするURL。ポート番号まで実際のものと一致させてください。
    # ここが違うとログインだけが失敗します（403）。
    # 空にすると http://localhost:<ポート> として扱います。
    "PUBLIC_BASE_URL": "https://rng-e90io.puratya.com",

    # https://example.com/s/kazino/ のようにサブパスで配信する場合のみ "/s/kazino"。
    # ドメイン直下で配信するなら空のまま。
    "BASE_PATH": "",

    # 待ち受け設定。None にするとホストが渡す PORT / HOST 環境変数を使います
    # （見つからなければ 8000 / 0.0.0.0）。ホスティングパネルではこのままに。
    "PORT": None,
    "HOST": None,

    # --- データベース -------------------------------------------------------
    # 既定は単一ファイルの SQLite（外部サービス不要）。
    # PostgreSQL を使う場合:
    #   "postgresql+asyncpg://cosmic:パスワード@127.0.0.1:5432/cosmic_rng"
    "DATABASE_URL": "sqlite+aiosqlite:///./data/cosmic.db",

    # PostgreSQL のときだけ 2 以上にできます（SQLite は書き込みが単一のため 1 固定）。
    "WORKERS": 1,

    # --- 任意 ---------------------------------------------------------------
    # 新規登録の可否は管理パネルからも切り替えられます。

    # Discord ログインも併用したい場合のみ設定（空ならメール認証のみ）。
    "DISCORD_CLIENT_ID": "",
    "DISCORD_CLIENT_SECRET": "",
    # 上を設定した場合は PUBLIC_BASE_URL + "/api/auth/callback" を指定します。
    "DISCORD_REDIRECT_URI": "",
    # Discord DM で通知を送る場合のみ（Bot トークン）。
    "DISCORD_BOT_TOKEN": "",

    # 追加で許可したいアクセス元（カンマ区切り）。通常は空で構いません。
    "ALLOWED_ORIGINS": "",

    # リバースプロキシのアドレス（カンマ区切り）。
    # このアドレスから来たリクエストだけ X-Forwarded-For を信用して、本当の
    # 接続元IPを取り出します。ホスティングパネルの多くは localhost から
    # 転送するので既定のままで動きます。もしプロキシが別のアドレス
    # （Dockerの 172.17.0.1 など）にある場合はここに追加してください。
    # 設定しないと全員が同じIPとして扱われ、ログインの回数制限を共有します。
    "TRUSTED_PROXIES": "127.0.0.1,::1",

    "LOG_LEVEL": "INFO",
    # ログをファイルにも残す場合はパスを書きます（例 "logs/cosmic.log"）。
    "LOG_FILE": "",
}
# ===========================================================================
#  ここから下は通常編集不要です
# ===========================================================================

BACKEND = ROOT / "backend"
REQUIREMENTS = ROOT / "requirements.txt"
SECRET_FILE = ROOT / "data" / "secret.key"

BANNER = r"""
   ______  ____  __  ___ _____ ______   ____  _   __ ______
  / ____/ / __ \/  |/  // ___//  _/ // / __ \/ | / // ____/
 / /     / / / / /|_/ / \__ \ / // // / /_/ /  |/ // / __
/ /___  / /_/ / /  / / ___/ // // // / _, _/ /|  // /_/ /
\____/  \____/_/  /_/ /____/___/_//_/_/ |_/_/ |_/ \____/   RNG
"""


def log(msg: str) -> None:
    print(f"[cosmic] {msg}", flush=True)


def secret_key() -> str:
    """Stable signing key, generated once and kept out of the source file.

    Sessions are signed with it, so a key that changed on every boot would log
    everyone out each restart.
    """
    if SECRET_FILE.exists():
        key = SECRET_FILE.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(key, encoding="utf-8")
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass  # filesystems without POSIX permissions (some shared hosts)
    log(f"署名鍵を生成しました: {SECRET_FILE.relative_to(ROOT)}（削除すると全員ログアウトになります）")
    return key


def apply_config() -> tuple[str, int, str]:
    """Publish CONFIG as the environment the app reads. Returns host, port, public URL."""
    port = int(CONFIG["PORT"] or os.environ.get("PORT") or 8000)
    # The host assigns these; hard-coding 127.0.0.1 would make the site
    # unreachable from outside, which is the usual way this goes wrong.
    host = str(CONFIG["HOST"] or os.environ.get("HOST") or "0.0.0.0")
    base_path = str(CONFIG["BASE_PATH"] or "").rstrip("/")
    public = str(CONFIG["PUBLIC_BASE_URL"] or "").rstrip("/") or f"http://localhost:{port}{base_path}"

    env = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": secret_key(),
        "DATABASE_URL": str(CONFIG["DATABASE_URL"]),
        "PUBLIC_BASE_URL": public,
        "BASE_PATH": base_path,
        "ALLOWED_ORIGINS": str(CONFIG["ALLOWED_ORIGINS"] or ""),
        "TRUSTED_PROXIES": str(CONFIG["TRUSTED_PROXIES"] or "127.0.0.1,::1"),
        "COOKIE_SECURE": "true" if public.startswith("https://") else "false",
        "ADMIN_USERNAME": str(CONFIG["ADMIN_USERNAME"] or ""),
        "ADMIN_EMAIL": str(CONFIG["ADMIN_EMAIL"] or ""),
        "ADMIN_PASSWORD": str(CONFIG["ADMIN_PASSWORD"] or ""),
        "ADMIN_RESET_PASSWORD": "true" if CONFIG["ADMIN_RESET_PASSWORD"] else "false",
        "DISCORD_CLIENT_ID": str(CONFIG["DISCORD_CLIENT_ID"] or ""),
        "DISCORD_CLIENT_SECRET": str(CONFIG["DISCORD_CLIENT_SECRET"] or ""),
        "DISCORD_REDIRECT_URI": str(CONFIG["DISCORD_REDIRECT_URI"] or ""),
        "DISCORD_BOT_TOKEN": str(CONFIG["DISCORD_BOT_TOKEN"] or ""),
        "LOG_LEVEL": str(CONFIG["LOG_LEVEL"] or "INFO"),
        "LOG_FILE": str(CONFIG["LOG_FILE"] or ""),
        "EVENT_BUS": "auto",
        "BACKUP_DIR": str(ROOT / "backups"),
    }
    os.environ.update(env)
    return host, port, public


def database_url() -> str:
    return os.environ["DATABASE_URL"]


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------
def missing_packages() -> list[str]:
    import importlib.util

    needed = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "sqlalchemy": "sqlalchemy",
        "pydantic_settings": "pydantic-settings",
        "httpx": "httpx",
        "PIL": "pillow",
    }
    sqlite = database_url().startswith("sqlite")
    needed["aiosqlite" if sqlite else "asyncpg"] = "aiosqlite" if sqlite else "asyncpg"
    if not sqlite:
        needed["alembic"] = "alembic"
    return [dist for mod, dist in needed.items() if importlib.util.find_spec(mod) is None]


def ensure_dependencies() -> None:
    missing = missing_packages()
    if not missing:
        return
    log(f"不足している依存関係: {', '.join(missing)}")
    if os.environ.get("COSMIC_NO_AUTO_INSTALL") == "1":
        log(f"次を実行してください: {sys.executable} -m pip install -r requirements.txt")
        raise SystemExit(1)
    log("requirements.txt からインストールします（COSMIC_NO_AUTO_INSTALL=1 で抑制できます）")
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)]
    if subprocess.call(cmd) != 0:
        log("自動インストールに失敗しました。手動で次を実行してください:")
        log(f"  {sys.executable} -m pip install -r requirements.txt")
        raise SystemExit(1)
    if missing_packages():
        log(f"インストール後もまだ読み込めません: {', '.join(missing_packages())}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def check_config(public: str, port: int) -> None:
    problems: list[str] = []
    password = str(CONFIG["ADMIN_PASSWORD"] or "")
    if CONFIG["ADMIN_USERNAME"] and not password:
        problems.append("ADMIN_PASSWORD が空です。管理者アカウントは作成されません。")
    elif password and len(password) < 8:
        problems.append("ADMIN_PASSWORD が8文字未満です。管理者アカウントは作成されません。")
    if not str(CONFIG["ADMIN_EMAIL"] or "").count("@"):
        problems.append("ADMIN_EMAIL の形式が正しくありません。")
    for p in problems:
        log(f"警告: {p}")

    if any(h in public for h in ("localhost", "127.0.0.1", "0.0.0.0")):
        log("ヒント: PUBLIC_BASE_URL がローカルアドレスです。外部からアクセスするなら、")
        log("        ブラウザに入力するURL（ポート番号まで一致）を CONFIG に設定してください。")
        log("        不一致のままだとログインだけが 403 になります。")
    elif public.startswith("http://"):
        log("警告: PUBLIC_BASE_URL が http:// です。公開時は https:// を強く推奨します")
        log("      （http:// ではセッションCookieが暗号化されずに流れます）。")


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------
def prepare_database() -> None:
    import asyncio

    from app.cli import _alembic_upgrade  # noqa: PLC2701 - internal on purpose
    from app.config import get_settings

    if get_settings().is_sqlite:
        from app.db import sqlite_path

        sqlite_path().parent.mkdir(parents=True, exist_ok=True)
    log("データベーススキーマを確認しています")
    _alembic_upgrade()

    async def seed_if_empty() -> None:
        from sqlalchemy import select

        from app.db import dispose_engine, session_scope
        from app.models import Rarity

        async with session_scope() as db:
            if (await db.execute(select(Rarity).limit(1))).scalar_one_or_none() is None:
                log("初期コンテンツを投入しています（アイテム・Biome・装備・実績ほか）")
                from app.content.seeder import seed

                await seed(db)
                log("初期コンテンツの投入が完了しました")
        await dispose_engine()

    asyncio.run(seed_if_empty())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    print(BANNER, flush=True)
    sys.path.insert(0, str(BACKEND))
    os.chdir(ROOT)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    host, port, public = apply_config()
    ensure_dependencies()
    check_config(public, port)

    url = database_url()
    sqlite = url.startswith("sqlite")
    # SQLite has a single writer; more than one worker would only contend for it.
    workers = 1 if sqlite else max(1, int(CONFIG["WORKERS"] or 1))

    prepare_database()

    base_path = os.environ["BASE_PATH"]
    log(f"データベース: {'SQLite (' + url.split('///')[-1] + ')' if sqlite else 'PostgreSQL'}")
    log(f"待ち受け: http://{host}:{port}{base_path}/" + (f" · ワーカー {workers}" if workers > 1 else ""))
    log(f"公開URL : {public}")
    if CONFIG["ADMIN_USERNAME"]:
        log(f"管理者ログイン: ユーザー名 {CONFIG['ADMIN_USERNAME']} / メール {CONFIG['ADMIN_EMAIL']}")

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=workers if workers > 1 else None,
        log_level=str(CONFIG["LOG_LEVEL"] or "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )


if __name__ == "__main__":
    main()
