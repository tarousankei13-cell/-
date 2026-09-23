"""Management CLI.

    python -m app.cli migrate            # alembic upgrade head
    python -m app.cli seed [--force]     # insert missing content (--force resets seeded rows)
    python -m app.cli set-role <discord_id> admin|player
    python -m app.cli backup [--kind scheduled]
    python -m app.cli restore <filename> --yes
    python -m app.cli check              # configuration & connectivity self-check
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select, text

from .config import BASE_DIR, get_settings


def _alembic_upgrade() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    command.upgrade(cfg, "head")


async def _seed(force: bool) -> None:
    from .content.seeder import seed
    from .db import session_scope

    async with session_scope() as db:
        await seed(db, force=force)


async def _set_role(discord_id: int, role: str) -> None:
    from .db import session_scope
    from .models import AuditLog, User

    async with session_scope() as db:
        u = (await db.execute(select(User).where(User.discord_id == discord_id))).scalar_one_or_none()
        if u is None:
            print("user not found (the user must log in once first)", file=sys.stderr)
            raise SystemExit(1)
        old = u.role
        u.role = role
        db.add(AuditLog(action="cli_set_role", target_user_id=u.id, entity_type="user", entity_id=str(u.id), old_value=old,
                        new_value=role, reason="CLI"))
        await db.commit()
        print(f"{u.display_name} (#{u.id}) role: {old} -> {role}")


async def _backup(kind: str) -> None:
    from .db import session_scope
    from .services import backup

    async with session_scope() as db:
        r = await backup.create(db, kind=kind)
        print(r)
        if r["status"] != "done":
            raise SystemExit(1)


async def _restore(filename: str) -> None:
    from .services import backup

    ok, err = await backup.restore(filename)
    print("restore", "OK" if ok else "FAILED", err[-2000:])
    if not ok:
        raise SystemExit(1)


async def _check() -> None:
    from .db import session_scope

    s = get_settings()
    print("environment:", s.environment)
    print("public_base_url:", s.public_base_url)
    print("origins:", s.origins)
    print("discord oauth configured:", bool(s.discord_client_id and s.discord_client_secret and s.discord_redirect_uri))
    print("discord bot token (DM notifications):", bool(s.discord_bot_token))
    print("admin discord ids:", sorted(s.admin_ids))
    async with session_scope() as db:
        v = (await db.execute(text("SELECT version()"))).scalar_one()
        print("database:", v.split(",")[0])
        rev = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        print("migration revision:", rev)
    print("backup dir:", Path(s.backup_dir).resolve())


def main() -> None:
    p = argparse.ArgumentParser(prog="cosmic-rng")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sp = sub.add_parser("seed")
    sp.add_argument("--force", action="store_true")
    rp = sub.add_parser("set-role")
    rp.add_argument("discord_id", type=int)
    rp.add_argument("role", choices=["admin", "player"])
    bp = sub.add_parser("backup")
    bp.add_argument("--kind", default="scheduled", choices=["scheduled", "manual"])
    rs = sub.add_parser("restore")
    rs.add_argument("filename")
    rs.add_argument("--yes", action="store_true")
    sub.add_parser("check")
    a = p.parse_args()
    if a.cmd == "migrate":
        _alembic_upgrade()
    elif a.cmd == "seed":
        asyncio.run(_seed(a.force))
    elif a.cmd == "set-role":
        asyncio.run(_set_role(a.discord_id, a.role))
    elif a.cmd == "backup":
        asyncio.run(_backup(a.kind))
    elif a.cmd == "restore":
        if not a.yes:
            print("Refusing to restore without --yes (this overwrites the database).", file=sys.stderr)
            raise SystemExit(2)
        asyncio.run(_restore(a.filename))
    elif a.cmd == "check":
        asyncio.run(_check())


if __name__ == "__main__":
    main()
