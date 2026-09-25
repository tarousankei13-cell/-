"""Batched global statistics.

Incrementing ``items.discovery_count`` on every roll would serialise all
players on the same hot rows (common items). Increments are accumulated in
memory and flushed in one statement every few seconds; periodic jobs
recompute derived values (owner counts, net worth, market reference price).
"""
from __future__ import annotations

import logging
from collections import defaultdict

from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.timeutil import utcnow
from ..db import is_sqlite

log = logging.getLogger("cosmic.stats")


class ItemStatsBuffer:
    def __init__(self) -> None:
        self.discoveries: dict[int, int] = defaultdict(int)

    def add(self, item_id: int, n: int = 1) -> None:
        self.discoveries[item_id] += n

    async def flush(self, db: AsyncSession) -> int:
        if not self.discoveries:
            return 0
        pending, self.discoveries = self.discoveries, defaultdict(int)
        ids = list(pending.keys())
        counts = [pending[i] for i in ids]
        try:
            if is_sqlite():
                # No array types: one parameterised UPDATE per item, sent as a single
                # executemany round trip inside the same transaction.
                await db.execute(
                    text("UPDATE items SET discovery_count = discovery_count + :n WHERE id = :id"),
                    [{"id": i, "n": n} for i, n in zip(ids, counts, strict=True)],
                )
            else:
                await db.execute(
                    text(
                        "UPDATE items AS i SET discovery_count = i.discovery_count + v.n "
                        "FROM unnest(CAST(:ids AS integer[]), CAST(:ns AS bigint[])) AS v(id, n) WHERE i.id = v.id"
                    ),
                    {"ids": ids, "ns": counts},
                )
            await db.commit()
        except Exception:
            await db.rollback()
            for i, n in pending.items():  # retry next flush
                self.discoveries[i] += n
            raise
        return len(ids)


item_stats = ItemStatsBuffer()


async def recompute_owner_counts(db: AsyncSession) -> None:
    if is_sqlite():
        await db.execute(text(
            "UPDATE items SET owner_count = ("
            " SELECT count(DISTINCT owner_id) FROM item_instances WHERE item_id = items.id)"
            " WHERE owner_count IS NOT ("
            " SELECT count(DISTINCT owner_id) FROM item_instances WHERE item_id = items.id)"
        ))
    else:
        await db.execute(text(
            "UPDATE items i SET owner_count = COALESCE(s.c, 0) FROM ("
            " SELECT it.id, c.c FROM items it LEFT JOIN ("
            "  SELECT item_id, count(DISTINCT owner_id) AS c FROM item_instances GROUP BY item_id) c ON c.item_id = it.id"
            ") s WHERE s.id = i.id AND i.owner_count IS DISTINCT FROM COALESCE(s.c, 0)"
        ))
    await db.commit()


async def recompute_best_items(db: AsyncSession) -> int:
    """Rebuild every player's rarest item from what they have actually obtained.

    Before granted and generated items counted towards the record, players who
    received a rare item from an admin, or generated one, had a best that was
    quietly lower than the truth. The collection table lists everything a player
    has ever obtained, so the rarest of those is the answer. Returns the number
    of players whose record changed.
    """
    from ..models import Collection, Item, UserStats

    rows = (await db.execute(
        select(Collection.user_id, Item.id, Item.odds).join(Item, Item.id == Collection.item_id).where(Item.odds.is_not(None))
    )).all()
    best: dict[int, tuple[float, int]] = {}
    for uid, iid, odds in rows:
        o = float(odds or 0)
        if uid not in best or o > best[uid][0]:
            best[uid] = (o, iid)
    changed = 0
    for st in (await db.execute(select(UserStats))).scalars().all():
        got = best.get(st.user_id)
        if got is None:
            continue
        odds, iid = got
        if odds > st.best_odds or (odds == st.best_odds and st.best_item_id is None):
            st.best_odds = odds
            st.best_item_id = iid
            changed += 1
    await db.commit()
    return changed


async def backfill_generated_names_ja(db: AsyncSession) -> int:
    """Give already-generated items the Japanese name they would get today.

    A generated item stores which parts made it, so its Japanese name can be
    composed after the fact once the parts have theirs. Returns rows changed.
    """
    from ..models import Item, ItemPart
    from ..rng.procedural import compose_name_ja

    parts = {(r.part_type, r.key): {"name_ja": r.name_ja} for r in (await db.execute(select(ItemPart))).scalars().all()}
    changed = 0
    for it in (await db.execute(select(Item).where(Item.kind == "generated", Item.name_ja == ""))).scalars().all():
        chosen_keys = (it.procedural or {}).get("parts") or {}
        chosen = {ptype: parts.get((ptype, key)) for ptype, key in chosen_keys.items()}
        if any(v is None for v in chosen.values()):
            continue
        ja = compose_name_ja(chosen)
        if ja:
            it.name_ja = ja
            changed += 1
    await db.commit()
    return changed


async def recompute_market_values(db: AsyncSession) -> None:
    """Reference market value = median of the last 20 sales (last 30 days)."""
    cutoff = utcnow() - timedelta(days=30)
    if is_sqlite():
        # No percentile_cont: take the middle row(s) of the window by rank and
        # average them, which is the same value for both odd and even counts.
        await db.execute(text(
            "UPDATE items SET market_value = COALESCE(("
            " SELECT CAST(avg(price) AS INTEGER) FROM ("
            "  SELECT price, row_number() OVER (ORDER BY price) AS rn, count(*) OVER () AS c FROM ("
            "   SELECT price FROM market_listings WHERE item_id = items.id AND status = 'sold'"
            "   AND sold_at > :cut ORDER BY sold_at DESC LIMIT 20)) "
            " WHERE rn IN ((c + 1) / 2, (c + 2) / 2)), market_value)"
            " WHERE EXISTS (SELECT 1 FROM market_listings WHERE item_id = items.id"
            " AND status = 'sold' AND sold_at > :cut)"
        ), {"cut": cutoff})
    else:
        await db.execute(text(
            "UPDATE items i SET market_value = s.med FROM ("
            " SELECT item_id, CAST(percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS bigint) AS med FROM ("
            "  SELECT item_id, price, row_number() OVER (PARTITION BY item_id ORDER BY sold_at DESC) rn"
            "  FROM market_listings WHERE status = 'sold' AND sold_at > :cut) x"
            " WHERE rn <= 20 GROUP BY item_id) s WHERE s.item_id = i.id"
        ), {"cut": cutoff})
    await db.commit()


async def recompute_net_worth(db: AsyncSession, active_minutes: int = 30) -> None:
    cutoff = utcnow() - timedelta(minutes=active_minutes)
    if is_sqlite():
        await db.execute(text(
            "UPDATE user_stats SET net_worth = ("
            " SELECT u.stardust + COALESCE(("
            "  SELECT sum(max(it.sell_value, it.market_value)) FROM item_instances ii"
            "  JOIN items it ON it.id = ii.item_id WHERE ii.owner_id = u.id), 0)"
            " FROM users u WHERE u.id = user_stats.user_id)"
            " WHERE net_worth = 0 OR user_id IN (SELECT id FROM users WHERE last_seen_at > :cut)"
        ), {"cut": cutoff})
    else:
        await db.execute(text(
            "UPDATE user_stats us SET net_worth = u.stardust + COALESCE(v.val, 0) FROM users u LEFT JOIN ("
            " SELECT ii.owner_id, sum(GREATEST(it.sell_value, it.market_value)) AS val FROM item_instances ii"
            " JOIN items it ON it.id = ii.item_id GROUP BY ii.owner_id) v ON v.owner_id = u.id"
            " WHERE us.user_id = u.id AND (u.last_seen_at > :cut OR us.net_worth = 0)"
        ), {"cut": cutoff})
    await db.commit()
