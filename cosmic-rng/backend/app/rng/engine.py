"""Versioned RNG engine.

Model
-----
Items are checked rarest-first ("cascade"). Item *i* with base odds ``1/N``
hits its own check with probability::

    c_i = min(1, L_i / N_i * boosts_i) ** flatten

where ``L_i`` is the *effective* luck for that item — the final luck passed
through the item's luck curve (by default ``luck ** rarity.luck_exponent`` so
that ultra-rare items respond less to luck). The first item whose check hits
is the result; the fallback item (odds 1) terminates the cascade.

Instead of drawing one random number per item, the cascade is compiled into an
exact categorical distribution::

    p_i = c_i * prod_{j rarer than i} (1 - c_j)

and sampled with a single uniform draw (bisect on the cumulative array). The
two formulations are mathematically identical; the compiled form gives O(log n)
sampling for offline batches, exact "final probability" figures for the UI and
a closed form for statistical tests.

Any change to these semantics must bump ``RNG_VERSION`` — every roll stores the
version and content version it was produced with.
"""
from __future__ import annotations

import bisect
import math
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

from ..content.registry import ItemDef, Snapshot

RNG_VERSION = 1

_sysrand = secrets.SystemRandom()


class RandomSource(Protocol):
    def random(self) -> float: ...
    def uniform(self, a: float, b: float) -> float: ...


def system_rng() -> RandomSource:
    """Cryptographically secure RNG used for all authoritative rolls."""
    return _sysrand


# ---------------------------------------------------------------------------
# Luck
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LuckBreakdown:
    base: float = 1.0
    equipment: float = 1.0
    biome: float = 1.0
    temporary: float = 1.0
    special: float = 1.0
    event: float = 1.0
    other: float = 1.0

    @property
    def final(self) -> float:
        v = self.base * self.equipment * self.biome * self.temporary * self.special * self.event * self.other
        if not math.isfinite(v):
            return 1e300
        return max(v, 1e-9)

    def as_dict(self) -> dict[str, float]:
        return {
            "base": self.base, "equipment": self.equipment, "biome": self.biome, "temporary": self.temporary,
            "special": self.special, "event": self.event, "other": self.other, "final": self.final,
        }


def effective_luck(luck: float, curve: dict[str, Any] | None, default_exponent: float) -> float:
    """Apply an item's luck curve. Supports power (default), linear, log, none; optional cap."""
    mode = (curve or {}).get("mode", "power")
    if mode == "none":
        eff = 1.0
    elif mode == "linear":
        eff = luck
    elif mode == "log":
        k = float((curve or {}).get("k", 1.0))
        eff = 1.0 + k * math.log(luck) if luck > 1 else luck
    else:
        exp = float((curve or {}).get("exponent", default_exponent))
        eff = luck ** exp
    cap = (curve or {}).get("cap")
    if cap is not None:
        eff = min(eff, float(cap))
    return eff


# ---------------------------------------------------------------------------
# Roll context and eligibility
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RollContext:
    biome_key: str
    luck: float
    biome_state: str | None = None
    level: int = 1
    special: bool = False
    hidden_special: str | None = None
    now: datetime | None = None
    active_events: frozenset[str] = frozenset()
    min_tier: int = 0
    flatten: float = 1.0
    item_mults: dict[int, float] = field(default_factory=dict)
    tier_mults: dict[int, float] = field(default_factory=dict)

    def cache_key(self, snap: Snapshot) -> tuple[Any, ...]:
        hour_bucket = None
        if self.now is not None:
            hour_bucket = (self.now.year, self.now.month, self.now.day, self.now.hour)
        return (
            snap.version, id(snap), self.biome_key, self.biome_state, self.luck, self.level, self.special, self.hidden_special,
            hour_bucket, self.active_events, self.min_tier, self.flatten,
            tuple(sorted(self.item_mults.items())), tuple(sorted(self.tier_mults.items())),
        )


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def time_condition_ok(cond: dict[str, Any], now: datetime | None) -> bool:
    if now is None:
        return False
    local = now.astimezone(_zone(str(cond.get("tz", "UTC"))))
    hours = cond.get("hours")
    if hours:
        start, end = int(hours[0]), int(hours[1])
        h = local.hour
        inside = (start <= h < end) if start <= end else (h >= start or h < end)
        if not inside:
            return False
    weekdays = cond.get("weekdays")
    if weekdays is not None and local.weekday() not in weekdays:
        return False
    return True


def item_eligible(item: ItemDef, ctx: RollContext) -> bool:
    if item.biome_keys and ctx.biome_key not in item.biome_keys:
        return False
    if ctx.biome_key in item.excluded_biome_keys:
        return False
    if item.min_luck is not None and ctx.luck < item.min_luck:
        return False
    if ctx.min_tier and item.tier < ctx.min_tier:
        return False
    c = item.conditions
    if c:
        if c.get("special_only") and not ctx.special:
            return False
        hs = c.get("hidden_special")
        if hs and ctx.hidden_special != hs:
            return False
        bs = c.get("biome_state")
        if bs and ctx.biome_state != bs:
            return False
        ev = c.get("event")
        if ev and ev not in ctx.active_events:
            return False
        ml = c.get("min_level")
        if ml and ctx.level < int(ml):
            return False
        tc = c.get("time")
        if tc and not time_condition_ok(tc, ctx.now):
            return False
    return True


# ---------------------------------------------------------------------------
# Compiled table
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CompiledTable:
    items: list[ItemDef]
    probs: list[float]
    chances: list[float]
    cum: list[float]
    tail: list[float]  # P(result is at least as rare as items[i]) (by base odds)
    index: dict[int, int]

    def sample_index(self, u: float) -> int:
        total = self.cum[-1]
        i = bisect.bisect_right(self.cum, u * total)
        return min(i, len(self.items) - 1)

    def sample(self, rng: RandomSource) -> ItemDef:
        return self.items[self.sample_index(rng.random())]

    def prob_of(self, item_id: int) -> float:
        i = self.index.get(item_id)
        return 0.0 if i is None else self.probs[i]

    def tail_of(self, item_id: int) -> float:
        i = self.index.get(item_id)
        return 1.0 if i is None else self.tail[i]


def compile_table(snap: Snapshot, ctx: RollContext) -> CompiledTable:
    biome = snap.biomes.get(ctx.biome_key)
    biome_boosts = biome.item_boosts if biome else {}
    fallback_key = snap.settings.get("roll.fallback_item", "cosmic_dust")

    items: list[ItemDef] = []
    chances: list[float] = []
    for it in snap.rollable:  # rarest first
        if not item_eligible(it, ctx):
            continue
        rar = snap.rarities.get(it.rarity_key)
        exp = rar.luck_exponent if rar else 1.0
        L = effective_luck(ctx.luck, it.luck_curve, exp)
        c = L / float(it.odds or 1.0)
        bb = biome_boosts.get(it.key)
        if bb:
            c *= float(bb)
        im = ctx.item_mults.get(it.id)
        if im:
            c *= im
        tm = ctx.tier_mults.get(it.tier)
        if tm:
            c *= tm
        if c >= 1.0:
            c = 1.0
        elif c < 0:
            c = 0.0
        if ctx.flatten != 1.0 and c > 0:
            c = c ** ctx.flatten
        items.append(it)
        chances.append(c)

    if not items:
        fb = snap.items_by_key.get(fallback_key)
        if fb is None:
            raise RuntimeError("RNG table is empty and no fallback item exists")
        items, chances = [fb], [1.0]

    probs: list[float] = []
    remaining = 1.0
    for c in chances:
        p = remaining * c
        probs.append(p)
        remaining -= p
        if remaining <= 0.0:
            remaining = 0.0
    if remaining > 1e-15:
        # No terminal (odds=1) item among eligible ones: assign remainder to the
        # fallback if eligible, otherwise renormalise (e.g. min rarity guarantee).
        fb_idx = next((i for i, it in enumerate(items) if it.key == fallback_key), None)
        if fb_idx is not None:
            probs[fb_idx] += remaining
        else:
            total = 1.0 - remaining
            if total <= 0:
                probs = [1.0 / len(items)] * len(items)
            else:
                probs = [p / total for p in probs]

    cum: list[float] = []
    acc = 0.0
    for p in probs:
        acc += p
        cum.append(acc)

    # Tail probabilities by base odds (items sorted rarest first).
    tail: list[float] = [0.0] * len(items)
    i = 0
    acc = 0.0
    n = len(items)
    while i < n:
        j = i
        odds_i = items[i].odds or 1.0
        group = 0.0
        while j < n and (items[j].odds or 1.0) == odds_i:
            group += probs[j]
            j += 1
        acc += group
        for k in range(i, j):
            tail[k] = acc
        i = j

    return CompiledTable(items=items, probs=probs, chances=chances, cum=cum, tail=tail,
                         index={it.id: idx for idx, it in enumerate(items)})


class TableCache:
    """LRU cache of compiled tables keyed by the full roll context."""

    def __init__(self, maxsize: int = 4096) -> None:
        self._data: OrderedDict[tuple[Any, ...], CompiledTable] = OrderedDict()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0

    def get(self, snap: Snapshot, ctx: RollContext) -> CompiledTable:
        key = ctx.cache_key(snap)
        t = self._data.get(key)
        if t is not None:
            self._data.move_to_end(key)
            self.hits += 1
            return t
        self.misses += 1
        t = compile_table(snap, ctx)
        self._data[key] = t
        if len(self._data) > self.maxsize:
            self._data.popitem(last=False)
        return t

    def clear(self) -> None:
        self._data.clear()


table_cache = TableCache()


# ---------------------------------------------------------------------------
# Fortune rating
# ---------------------------------------------------------------------------
FORTUNE_BANDS: list[tuple[float, str, str]] = [
    (0.5, "ordinary", "Ordinary"),
    (0.1, "good", "Good"),
    (0.01, "lucky", "Lucky"),
    (1e-3, "great", "Great Fortune"),
    (1e-4, "blessed", "Blessed"),
    (1e-5, "miraculous", "Miraculous"),
    (1e-6, "celestial", "Celestial"),
    (0.0, "cosmic", "Cosmic Miracle"),
]


def fortune(tail_probability: float) -> dict[str, Any]:
    t = max(min(tail_probability, 1.0), 1e-300)
    for threshold, key, label in FORTUNE_BANDS:
        if t > threshold:
            return {"key": key, "label": label, "top_percent": t * 100, "score": -math.log10(t)}
    return {"key": "cosmic", "label": "Cosmic Miracle", "top_percent": t * 100, "score": -math.log10(t)}


def pick_rarer(a: ItemDef, b: ItemDef) -> ItemDef:
    return a if (a.odds or 1) >= (b.odds or 1) else b


def expected_distribution(snap: Snapshot, ctx: RollContext) -> list[tuple[ItemDef, float]]:
    t = compile_table(snap, ctx)
    return list(zip(t.items, t.probs))


def sample_many(table: CompiledTable, n: int, rng: RandomSource) -> dict[int, int]:
    """Sample n results from a compiled table; returns {table index: count}."""
    counts: dict[int, int] = {}
    cum = table.cum
    total = cum[-1]
    bis = bisect.bisect_right
    last = len(cum) - 1
    for _ in range(n):
        i = bis(cum, rng.random() * total)
        if i > last:
            i = last
        counts[i] = counts.get(i, 0) + 1
    return counts


def iter_top(table: CompiledTable, limit: int = 12) -> Iterable[tuple[ItemDef, float]]:
    return list(zip(table.items, table.probs))[:limit]
