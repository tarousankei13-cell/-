"""Batched global statistics.

Incrementing ``items.discovery_count`` on every roll would serialise all
players on the same hot rows (common items). Increments are accumulated in
memory and flushed in one statement every few seconds; periodic jobs
recompute derived values (owner counts, net worth, market reference price).
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    await db.execute(text(
        "UPDATE items i SET owner_count = COALESCE(s.c, 0) FROM ("
        " SELECT it.id, c.c FROM items it LEFT JOIN ("
        "  SELECT item_id, count(DISTINCT owner_id) AS c FROM item_instances GROUP BY item_id) c ON c.item_id = it.id"
        ") s WHERE s.id = i.id AND i.owner_count IS DISTINCT FROM COALESCE(s.c, 0)"
    ))
    await db.commit()


async def recompute_market_values(db: AsyncSession) -> None:
    """Reference market value = median of the last 20 sales (last 30 days)."""
    await db.execute(text(
        "UPDATE items i SET market_value = s.med FROM ("
        " SELECT item_id, CAST(percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS bigint) AS med FROM ("
        "  SELECT item_id, price, row_number() OVER (PARTITION BY item_id ORDER BY sold_at DESC) rn"
        "  FROM market_listings WHERE status = 'sold' AND sold_at > now() - interval '30 days') x"
        " WHERE rn <= 20 GROUP BY item_id) s WHERE s.item_id = i.id"
    ))
    await db.commit()


async def recompute_net_worth(db: AsyncSession, active_minutes: int = 30) -> None:
    await db.execute(text(
        "UPDATE user_stats us SET net_worth = u.stardust + COALESCE(v.val, 0) FROM users u LEFT JOIN ("
        " SELECT ii.owner_id, sum(GREATEST(it.sell_value, it.market_value)) AS val FROM item_instances ii"
        " JOIN items it ON it.id = ii.item_id GROUP BY ii.owner_id) v ON v.owner_id = u.id"
        " WHERE us.user_id = u.id AND (u.last_seen_at > now() - make_interval(mins => :m) OR us.net_worth = 0)"
    ), {"m": active_minutes})
    await db.commit()
