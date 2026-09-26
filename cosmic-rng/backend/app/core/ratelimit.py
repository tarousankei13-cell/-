"""In-process token bucket rate limiter.

Buckets are keyed by (bucket name, principal) where principal is the user id
for authenticated requests or the client IP otherwise. Limits are per worker
process; authoritative gameplay limits (roll cooldown, gift cooldown, purchase
limits) are enforced in the database and do not rely on this module.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import RateLimited


@dataclass(frozen=True)
class Limit:
    rate: float  # tokens per second
    burst: float


LIMITS: dict[str, Limit] = {
    "auth": Limit(rate=0.5, burst=10),
    "api": Limit(rate=25, burst=60),
    "roll": Limit(rate=25, burst=40),
    "write": Limit(rate=6, burst=20),
    "admin": Limit(rate=20, burst=60),
    "ws_msg": Limit(rate=10, burst=30),
    "ws_connect": Limit(rate=0.5, burst=10),
    "search": Limit(rate=3, burst=10),
    "guest_roll": Limit(rate=1.5, burst=12),
}


class RateLimiter:
    def __init__(self, max_keys: int = 200_000) -> None:
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._max_keys = max_keys
        self.enabled = True

    def check(self, bucket: str, principal: str, cost: float = 1.0) -> None:
        if not self.enabled:
            return
        limit = LIMITS[bucket]
        now = time.monotonic()
        key = (bucket, principal)
        tokens, last = self._buckets.get(key, (limit.burst, now))
        tokens = min(limit.burst, tokens + (now - last) * limit.rate)
        if tokens < cost:
            retry = max(0.05, (cost - tokens) / limit.rate)
            self._buckets[key] = (tokens, now)
            raise RateLimited(
                "リクエストが多すぎます。少し待ってから再試行してください。",
                data={"retry_after": round(retry, 2)},
                headers={"Retry-After": str(max(1, int(retry + 0.999)))},
            )
        self._buckets[key] = (tokens - cost, now)
        if len(self._buckets) > self._max_keys:
            self._evict(now)

    def _evict(self, now: float) -> None:
        # Drop buckets that have fully refilled — they carry no state.
        stale = [k for k, (_, last) in self._buckets.items() if now - last > 120]
        for k in stale:
            self._buckets.pop(k, None)

    def reset(self) -> None:
        self._buckets.clear()


limiter = RateLimiter()
