"""Database backup / restore via pg_dump / pg_restore (custom format)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core.errors import AppError, NotFound
from ..core.timeutil import utcnow
from ..models import Backup

log = logging.getLogger("cosmic.backup")
NAME_RE = re.compile(r"^cosmic-rng-\d{8}-\d{6}(-[a-z]+)?\.dump$")
_lock = asyncio.Lock()


def _conn_env() -> tuple[list[str], dict[str, str]]:
    """libpq args without the password on the command line (passed via PGPASSWORD)."""
    u = urlparse(get_settings().sync_database_url)
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    args = ["-h", u.hostname or "127.0.0.1", "-p", str(u.port or 5432), "-U", unquote(u.username or "postgres")]
    db = (u.path or "/").lstrip("/")
    return args + ["-d", db], env


def backup_dir() -> Path:
    p = Path(get_settings().backup_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _run(cmd: list[str], env: dict[str, str], timeout: int = 3600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode or 0, (err or b"").decode(errors="replace")[-4000:]


async def create(db: AsyncSession, *, kind: str = "manual", by: int | None = None, note: str | None = None) -> dict[str, Any]:
    if _lock.locked():
        raise AppError("別のバックアップ/リストアが実行中です", code="backup_busy", status_code=409)
    async with _lock:
        name = f"cosmic-rng-{utcnow().strftime('%Y%m%d-%H%M%S')}-{kind}.dump"
        path = backup_dir() / name
        row = Backup(filename=name, kind=kind, status="running", created_by=by, note=note)
        db.add(row)
        await db.commit()
        args, env = _conn_env()
        code, err = await _run([get_settings().pg_dump_path, "-Fc", "--no-owner", "-f", str(path), *args], env)
        row.finished_at = utcnow()
        if code == 0 and path.exists():
            row.status = "done"
            row.size_bytes = path.stat().st_size
        else:
            row.status = "failed"
            row.note = (note or "") + f"\n{err[-1000:]}"
            log.error("backup failed: %s", err)
        await db.commit()
        if row.status == "done":
            await prune(db)
        return backup_public(row, path.exists())


async def prune(db: AsyncSession) -> None:
    keep = max(1, get_settings().backup_keep)
    rows = (await db.execute(select(Backup).where(Backup.status == "done", Backup.kind == "scheduled")
                             .order_by(Backup.id.desc()))).scalars().all()
    for r in rows[keep:]:
        p = backup_dir() / r.filename
        if p.exists():
            p.unlink()
        r.status = "pruned"
    await db.commit()


def backup_public(r: Backup, exists: bool) -> dict[str, Any]:
    return {"id": r.id, "filename": r.filename, "size_bytes": r.size_bytes, "kind": r.kind, "status": r.status, "note": r.note,
            "created_by": r.created_by, "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "file_exists": exists}


async def list_backups(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Backup).order_by(Backup.id.desc()).limit(200))).scalars().all()
    known = {r.filename for r in rows}
    out = [backup_public(r, (backup_dir() / r.filename).exists()) for r in rows]
    # Files created by the CLI/cron script that are not in the table (e.g. after a restore)
    for p in sorted(backup_dir().glob("cosmic-rng-*.dump"), reverse=True):
        if p.name not in known and NAME_RE.match(p.name):
            out.append({"id": None, "filename": p.name, "size_bytes": p.stat().st_size, "kind": "file", "status": "done", "note": None,
                        "created_by": None, "created_at": None, "finished_at": None, "file_exists": True})
    return out


def resolve_file(filename: str) -> Path:
    if not NAME_RE.match(filename):
        raise AppError("ファイル名が不正です", code="invalid_filename")
    p = backup_dir() / filename
    if not p.exists() or p.resolve().parent != backup_dir().resolve():
        raise NotFound("バックアップファイルが見つかりません")
    return p


async def restore(filename: str) -> tuple[bool, str]:
    """Restore the database from a backup file. Caller must hold super-admin rights and confirmation."""
    path = resolve_file(filename)
    if _lock.locked():
        raise AppError("別のバックアップ/リストアが実行中です", code="backup_busy", status_code=409)
    async with _lock:
        args, env = _conn_env()
        code, err = await _run([get_settings().pg_restore_path, "--clean", "--if-exists", "--no-owner", "--single-transaction",
                                *args, str(path)], env, timeout=7200)
        return code == 0, err


async def delete_file(db: AsyncSession, filename: str) -> None:
    p = resolve_file(filename)
    p.unlink()
    row = (await db.execute(select(Backup).where(Backup.filename == filename))).scalar_one_or_none()
    if row is not None:
        row.status = "deleted"
