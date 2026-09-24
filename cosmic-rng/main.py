#!/usr/bin/env python3
"""COSMIC RNG — 単一エントリポイント起動スクリプト / single-file launcher.

    python main.py

これ一つで .env の生成・依存関係の確認・DBスキーマ作成・初期コンテンツ投入・
サーバ起動まで行います。データベースは既定で SQLite（data/cosmic.db）なので、
PostgreSQL が無いホストでもそのまま動きます。DATABASE_URL に PostgreSQL の
URL を書けば、そちらへ切り替わります（本番向け・マルチワーカー可）。

主な環境変数（.env で設定）:
    DATABASE_URL   sqlite+aiosqlite:///./data/cosmic.db  もしくは postgresql+asyncpg://...
    PORT / HOST    待ち受けポート・アドレス
    BASE_PATH      /s/kazino のようにサブパス配信するとき
    ENVIRONMENT    production にすると開発用ログインを完全に無効化
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
ENV_FILE = ROOT / ".env"
REQUIREMENTS = ROOT / "requirements.txt"

BANNER = r"""
   ______  ____  __  ___ _____ ______   ____  _   __ ______
  / ____/ / __ \/  |/  // ___//  _/ // / __ \/ | / // ____/
 / /     / / / / /|_/ / \__ \ / // // / /_/ /  |/ // / __
/ /___  / /_/ / /  / / ___/ // // // / _, _/ /|  // /_/ /
\____/  \____/_/  /_/ /____/___/_//_/_/ |_/_/ |_/ \____/   RNG
"""


def log(msg: str) -> None:
    print(f"[cosmic] {msg}", flush=True)


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def write_default_env() -> None:
    """First run: generate a working .env with a fresh secret key."""
    key = secrets.token_urlsafe(48)
    port = os.environ.get("PORT", "8000")
    ENV_FILE.write_text(
        f"""# COSMIC RNG 設定ファイル（初回起動時に自動生成されました）
# ---------------------------------------------------------------------------
# この鍵はセッション Cookie の署名に使われます。絶対に公開しないでください。
SECRET_KEY={key}

# development = 動作確認モード（下の DEV_LOGIN_ENABLED が使えます）
# production  = 本番モード（Discord ログインのみ / 開発用ログインは無効）
ENVIRONMENT=development

# データベース。既定は単一ファイルの SQLite で、外部サービスは不要です。
# PostgreSQL を使う場合は次の行を書き換えてください:
#   DATABASE_URL=postgresql+asyncpg://cosmic:PASSWORD@127.0.0.1:5432/cosmic_rng
DATABASE_URL=sqlite+aiosqlite:///./data/cosmic.db

# 公開URL。ブラウザからアクセスするアドレスをそのまま書いてください。
PUBLIC_BASE_URL=http://localhost:{port}
# https:// で公開する場合は true のままに。http:// で試すときは false。
COOKIE_SECURE=false

# サブパスで配信する場合のみ設定（例: https://example.com/s/kazino/ なら /s/kazino）
BASE_PATH=

# --- Discord ログイン -------------------------------------------------------
# 本番運用にはこの3つが必要です。https://discord.com/developers/applications
# で作成し、OAuth2 のリダイレクトURLに PUBLIC_BASE_URL + /api/auth/callback を登録。
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
DISCORD_REDIRECT_URI=

# 管理者にする Discord ユーザーID（カンマ区切り）。空なら管理者は存在しません。
ADMIN_DISCORD_IDS=

# Discord DM 通知を使う場合のみ（Bot トークン）
DISCORD_BOT_TOKEN=

# --- 動作確認用ログイン -----------------------------------------------------
# true の間は Discord なしで任意のIDとしてログインできます。
# 一般公開する前に必ず false にし、ENVIRONMENT=production にしてください。
DEV_LOGIN_ENABLED=true

LOG_LEVEL=INFO
""",
        encoding="utf-8",
    )
    log(f".env を生成しました: {ENV_FILE}")


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
    url = database_url()
    needed["aiosqlite" if url.startswith("sqlite") else "asyncpg"] = (
        "aiosqlite" if url.startswith("sqlite") else "asyncpg"
    )
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
    still = missing_packages()
    if still:
        log(f"インストール後もまだ読み込めません: {', '.join(still)}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
def load_env_file() -> None:
    """Read .env into the process before anything imports the settings."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/cosmic.db")


def warn_about_open_login() -> None:
    env = os.environ.get("ENVIRONMENT", "production")
    dev_login = os.environ.get("DEV_LOGIN_ENABLED", "").lower() in ("1", "true", "yes")
    if env == "production" or not dev_login:
        return
    admins = [x for x in os.environ.get("ADMIN_DISCORD_IDS", "").replace(" ", "").split(",") if x]
    print(
        "\n"
        "  ┌──────────────────────────────────────────────────────────────┐\n"
        "  │  動作確認モードで起動しています（DEV_LOGIN_ENABLED=true）    │\n"
        "  │  Discord 認証なしで、誰でも任意のIDとしてログインできます。  │\n"
        "  │  一般公開する前に .env を次のように変更してください:         │\n"
        "  │      ENVIRONMENT=production                                  │\n"
        "  │      DEV_LOGIN_ENABLED=false                                 │\n"
        "  │      DISCORD_CLIENT_ID / SECRET / REDIRECT_URI を設定        │\n"
        "  └──────────────────────────────────────────────────────────────┘",
        flush=True,
    )
    if admins:
        log(f"警告: 管理者ID {', '.join(admins)} が設定されています。"
            "このモードでは第三者がその管理者としてログインできます。")
    print(flush=True)


def warn_about_public_url(public: str, port: int) -> None:
    """PUBLIC_BASE_URL anchors the CSRF origin check, so a wrong value does not
    fail loudly at boot -- it fails later, as a 403 on every login."""
    if not public:
        log("警告: PUBLIC_BASE_URL が未設定です。ログインが 403 になります。")
        return
    local = any(h in public for h in ("localhost", "127.0.0.1", "0.0.0.0"))
    if local:
        log("ヒント: PUBLIC_BASE_URL がローカルアドレスのままです。外部からアクセスする場合は、")
        log("        ブラウザに入力するURL（ポート番号まで一致）へ .env を書き換えてください。")
        log("        不一致のままだとログインが 403（不正なリクエスト元です）になります。")
    elif public.startswith("http://"):
        log("警告: PUBLIC_BASE_URL が http:// です。公開時は https:// にし、COOKIE_SECURE=true を推奨します。")
    if f":{port}" not in public and not public.startswith("https://") and not local:
        log(f"ヒント: 待ち受けポート {port} が PUBLIC_BASE_URL に含まれていません。"
            "リバースプロキシ経由でないなら :ポート番号 を付けてください。")


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------
def prepare_database() -> None:
    """Create the schema (and seed content on an empty database)."""
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
    if not ENV_FILE.exists():
        write_default_env()
    load_env_file()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.path.insert(0, str(BACKEND))
    os.chdir(ROOT)

    ensure_dependencies()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    url = database_url()
    sqlite = url.startswith("sqlite")
    # SQLite has a single writer; more than one worker would only contend for it.
    workers = 1 if sqlite else int(os.environ.get("WORKERS", "1"))

    public = os.environ.get("PUBLIC_BASE_URL", "")
    prepare_database()
    warn_about_open_login()

    log(f"データベース: {'SQLite (' + url.split('///')[-1] + ')' if sqlite else 'PostgreSQL'}")
    log(f"待ち受け: http://{host}:{port}{os.environ.get('BASE_PATH', '')}/")
    log(f"公開URL : {public} (PUBLIC_BASE_URL)")
    warn_about_public_url(public, port)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=workers if workers > 1 else None,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )


if __name__ == "__main__":
    main()
