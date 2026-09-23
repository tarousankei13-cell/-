"""Per-worker in-memory metrics (rolls/sec, errors) used by the admin dashboard."""
from __future__ import annotations

import time
from collections import deque


class Metrics:
    def __init__(self) -> None:
        self.rolls_window: deque[tuple[float, int]] = deque()
        self.total_rolls = 0
        self.errors = 0
        self.started = time.time()

    def add_rolls(self, n: int = 1) -> None:
        now = time.monotonic()
        self.total_rolls += n
        self.rolls_window.append((now, n))
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self.rolls_window and now - self.rolls_window[0][0] > 60:
            self.rolls_window.popleft()

    def rolls_per_sec(self, window: float = 10.0) -> float:
        now = time.monotonic()
        self._trim(now)
        return sum(n for t, n in self.rolls_window if now - t <= window) / window


metrics = Metrics()
