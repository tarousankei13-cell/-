"""Idempotency keys for economic operations.

The key row is inserted *inside the same transaction* as the operation:

* first request: insert succeeds, operation runs, stored response is written,
  everything commits atomically;
* concurrent duplicate: blocks on the unique index until the first commits,
  then observes the conflict and replays the stored response;
* failed first request: the transaction (including the key row) rolls back so
  the client may retry with the same key.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import upsert as insert
from ..models import IdempotencyKey
from .errors import AppError, Conflict

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")


class Replay(Exception):
    """Raised when a stored response exists for the idempotency key."""

    def __init__(self, response: Any, status_code: int) -> None:
        self.response = response
        self.status_code = status_code


def get_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if not _KEY_RE.match(key):
        raise AppError("Idempotency-Key ヘッダーが必要です", code="idempotency_key_required")
    return key


async def begin(db: AsyncSession, user_id: int, key: str, endpoint: str) -> None:
    stmt = (
        insert(IdempotencyKey)
        .values(user_id=user_id, key=key, endpoint=endpoint)
        .on_conflict_do_nothing(index_elements=["user_id", "key"])
        .returning(IdempotencyKey.key)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    if inserted is not None:
        return
    row = (
        await db.execute(select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key))
    ).scalar_one()
    if row.endpoint != endpoint:
        raise Conflict("Idempotency-Key が別の操作で使用済みです", code="idempotency_key_reused")
    if row.response is None:
        raise Conflict("同じリクエストを処理中です", code="duplicate_in_progress")
    raise Replay(row.response, row.status_code or 200)


async def finish(db: AsyncSession, user_id: int, key: str, response: Any, status_code: int = 200) -> None:
    await db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
        .values(response=response, status_code=status_code)
    )


async def run(db: AsyncSession, user_id: int, request: Request, endpoint: str, op: Any) -> Any:
    """Run ``op()`` (async callable returning a JSON-able dict) exactly once per key."""
    key = get_key(request)
    try:
        await begin(db, user_id, key, endpoint)
    except Replay as r:
        await db.rollback()
        if isinstance(r.response, dict):
            return {**r.response, "replayed": True}
        return r.response
    result = await op()
    await finish(db, user_id, key, result)
    await db.commit()
    return result
