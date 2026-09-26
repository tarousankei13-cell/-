"""The try-before-you-register experience.

A visitor gets a fixed number of real rolls, decided entirely on the server
with the same table the game uses at level 1 in the starting biome. Their
result list lives in an HMAC-signed cookie — the client cannot add to it or
edit what it drew — and is handed to the account they create afterwards.

Procedural slots are re-drawn rather than materialized: a guest must not be
able to mint permanent item rows in the database.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError
from ..core.security import sign_value, unsign_value
from ..rng import engine as rng_engine
from ..rng.engine import RollContext, system_rng, table_cache
from . import inventory as inv_svc
from . import users as users_svc

log = logging.getLogger("cosmic.guest")

COOKIE = "cosmic_guest"
COOKIE_TTL = 14 * 24 * 3600
MAX_ROLLS = 10
_REDRAW_LIMIT = 8  # attempts to avoid a procedural slot before giving up


def _decode(raw: str | None) -> dict[str, Any]:
    """{'n': rolls used, 'ids': item ids won} — never trusted unless signed."""
    value = unsign_value(raw)
    if not value:
        return {"n": 0, "ids": []}
    head, _, rest = value.partition("|")
    if not head.isdigit():
        return {"n": 0, "ids": []}
    ids = [int(x) for x in rest.split(",") if x.isdigit()]
    return {"n": min(int(head), MAX_ROLLS), "ids": ids[:MAX_ROLLS]}


def _encode(state: dict[str, Any]) -> str:
    return sign_value(f"{state['n']}|{','.join(str(i) for i in state['ids'])}", COOKIE_TTL)


def _table() -> Any:
    snap = get_registry().snap
    biome = snap.default_biome
    ctx = RollContext(biome_key=biome.key, luck=1.0, level=1)
    return table_cache.get(snap, ctx), biome


def status(raw: str | None) -> dict[str, Any]:
    st = _decode(raw)
    return {"used": st["n"], "limit": MAX_ROLLS, "remaining": max(0, MAX_ROLLS - st["n"]), "items": st["ids"]}


async def roll(db: AsyncSession, raw: str | None) -> tuple[dict[str, Any], str]:
    st = _decode(raw)
    if st["n"] >= MAX_ROLLS:
        raise AppError("お試しRollはここまでです。登録すると続きから遊べます", code="guest_limit", status_code=403)
    snap = get_registry().snap
    table, biome = _table()
    rng = system_rng()
    item = None
    for _ in range(_REDRAW_LIMIT):
        cand = table.items[table.sample_index(rng.random())]
        if cand.kind != "procedural_slot":
            item = cand
            break
    if item is None:  # every draw hit a slot: fall back to the commonest real item
        item = next((i for i in table.items if i.kind != "procedural_slot"), table.items[0])
    info = item.public(snap.rarities)
    p = table.prob_of(item.id)
    st["n"] += 1
    st["ids"].append(item.id)
    body = {
        "number": st["n"], "used": st["n"], "limit": MAX_ROLLS, "remaining": MAX_ROLLS - st["n"],
        "item": info, "odds": float(item.odds or 1), "final_chance": p,
        "final_odds": (1 / p) if p > 0 else None,
        "fortune": rng_engine.fortune(table.tail_of(item.id)),
        "biome": {"key": biome.key, "name": biome.name, "name_ja": biome.name_ja, "state": None},
        "guest": True,
    }
    return body, _encode(st)


async def carry_over(db: AsyncSession, user: Any, raw: str | None) -> int:
    """Hand a fresh account everything its guest session drew."""
    st = _decode(raw)
    ids = st["ids"][:MAX_ROLLS]
    if not ids:
        return 0
    from .rolls import collection_upsert, note_best_item

    snap = get_registry().snap
    stats = await users_svc.lock_stats(db, user.id)
    counts: dict[int, int] = {}
    for i in ids:
        counts[i] = counts.get(i, 0) + 1
    granted = 0
    for item_id, n in counts.items():
        d = snap.items.get(item_id)
        if d is None or d.kind == "procedural_slot":
            continue
        await inv_svc.create_instances(db, user.id, item_id, n, "guest", tier=d.tier)
        # These are the account's first discoveries, same as if it had rolled
        # them itself — the collection count has to agree with the book.
        if await collection_upsert(db, user.id, item_id, n) and item_id in snap.collectible_ids:
            stats.discovered_count += 1
        note_best_item(stats, item_id, float(d.odds or 0))
        granted += n
    stats.items_obtained += granted
    log.info("carried over %s guest items to user %s", granted, user.id)
    return granted
