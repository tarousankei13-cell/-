"""Application configuration loaded from environment variables / .env.

Secrets never live in code. Everything here is infrastructure-level
configuration; game balance lives in the database (game_settings) and is
editable from the admin panel.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(BASE_DIR.parent / ".env"), str(BASE_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["production", "development", "test"] = "production"
    secret_key: str = Field(default="", min_length=0)

    database_url: str = "postgresql+asyncpg://cosmic:cosmic@127.0.0.1:5432/cosmic_rng"
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_echo: bool = False
    db_null_pool: bool = False  # tests: no connection reuse across event loops

    public_base_url: str = "http://localhost:8000"
    allowed_origins: str = ""
    # Sub-path the site is served under, e.g. "/s/kazino" behind a shared host.
    # Empty means the app owns the whole origin.
    base_path: str = ""

    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = ""
    discord_bot_token: str = ""
    discord_api_base: str = "https://discord.com/api/v10"
    discord_oauth_authorize_url: str = "https://discord.com/oauth2/authorize"

    admin_discord_ids: str = ""

    cookie_secure: bool = True
    cookie_domain: str | None = None
    session_ttl_days: int = 30

    # "auto" follows the database: LISTEN/NOTIFY on PostgreSQL, in-process on SQLite.
    event_bus: Literal["auto", "postgres", "local"] = "auto"
    worker_id: str = ""

    backup_dir: str = str(BASE_DIR.parent / "backups")
    pg_dump_path: str = "pg_dump"
    pg_restore_path: str = "pg_restore"
    backup_keep: int = 30

    log_level: str = "INFO"
    log_file: str = ""
    log_json: bool = False

    trusted_proxies: str = "127.0.0.1,::1"

    dev_login_enabled: bool = False
    run_scheduler: bool = True

    @field_validator("base_path", mode="before")
    @classmethod
    def _normalise_base(cls, v: str | None) -> str:
        v = (v or "").strip().rstrip("/")
        if v and not v.startswith("/"):
            v = "/" + v
        return v

    @field_validator("cookie_domain", mode="before")
    @classmethod
    def _empty_domain(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.environment == "production":
            if len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must be at least 32 characters in production")
            if self.dev_login_enabled:
                raise ValueError("DEV_LOGIN_ENABLED must never be true in production")
        elif not self.secret_key:
            # Non-production convenience only; production refuses to boot above.
            self.secret_key = "dev-insecure-secret-key-change-me-0123456789"
        if self.event_bus == "auto":
            self.event_bus = "local" if self.database_url.startswith("sqlite") else "postgres"
        elif self.event_bus == "postgres" and self.database_url.startswith("sqlite"):
            raise ValueError("EVENT_BUS=postgres needs a PostgreSQL DATABASE_URL")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_discord_ids.replace(" ", "").split(",") if x.isdigit()}

    @staticmethod
    def _origin_of(url: str) -> str:
        """scheme://host[:port] — an Origin header never carries a path, so a
        PUBLIC_BASE_URL like https://host/s/kazino must be reduced to compare."""
        u = urlsplit(url.strip())
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
        return url.strip().rstrip("/")

    @property
    def origins(self) -> list[str]:
        items: list[str] = []
        for o in self.allowed_origins.split(","):
            if o.strip():
                origin = self._origin_of(o)
                if origin not in items:
                    items.append(origin)
        base = self._origin_of(self.public_base_url)
        if base and base not in items:
            items.append(base)
        return items

    @property
    def trusted_proxy_set(self) -> set[str]:
        return {x.strip() for x in self.trusted_proxies.split(",") if x.strip()}

    def url(self, path: str) -> str:
        """Absolute in-app path, including the sub-path the site is mounted on."""
        return f"{self.base_path}{path}" if path.startswith("/") else path

    @property
    def cookie_path(self) -> str:
        return self.base_path or "/"

    @property
    def is_dev(self) -> bool:
        return self.environment != "production"

    @property
    def sync_database_url(self) -> str:
        """URL usable by libpq tools (pg_dump) — strips the SQLAlchemy driver."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
