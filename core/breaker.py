"""
サーキットブレーカー

壊れている相手に要求を送り続けると、全員がタイムアウトを待たされる。
1つのアカウントが使えなくなっただけで、待ち行列が伸びて全体が遅くなる。

そこで、続けて失敗した相手は**一定時間そっとしておく**。
時間が経ったら1回だけ試して、通れば元に戻す。

    閉（closed）   ふつうに通す
    開（open）     しばらく通さない。すぐ諦めて別の手に回す
    半開（half）   様子見。1回だけ通してみる

⚠️ これは「相手が壊れている」ときの仕組み。
   こちらの要求が悪い（4xx）場合は数えない。
   数えてしまうと、利用者の入力ミスで経路が閉じてしまう。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("bot.breaker")

CLOSED = "closed"
OPEN = "open"
HALF = "half"

# 既定値
FAILURES_TO_OPEN = 5      # 連続でこの回数失敗したら開く
COOLDOWN = 60.0           # 開いてからこの秒数は通さない
SUCCESSES_TO_CLOSE = 2    # 半開でこの回数通れば元に戻す


class CircuitOpen(Exception):
    """いま通せない。呼び出し側は別の手に回すこと。"""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(
            f"{name} はしばらく使えません（あと {retry_after:.0f} 秒）"
        )
        self.name = name
        self.retry_after = retry_after


@dataclass
class Breaker:
    """1つの相手（グループやアカウント）ぶんの状態。"""
    name: str
    failures_to_open: int = FAILURES_TO_OPEN
    cooldown: float = COOLDOWN
    successes_to_close: int = SUCCESSES_TO_CLOSE

    state: str = CLOSED
    consecutive_failures: int = 0
    half_successes: int = 0
    opened_at: float = 0.0
    last_error: str = ""
    total_blocked: int = 0

    def _now(self) -> float:
        return time.monotonic()

    @property
    def retry_after(self) -> float:
        if self.state != OPEN:
            return 0.0
        return max(0.0, self.cooldown - (self._now() - self.opened_at))

    def allows(self) -> bool:
        """いま通してよいか。副作用として半開へ移ることがある。"""
        if self.state == CLOSED:
            return True
        if self.state == HALF:
            return True
        # OPEN
        if self.retry_after <= 0:
            self.state = HALF
            self.half_successes = 0
            log.info("%s を様子見に切り替えます", self.name)
            return True
        self.total_blocked += 1
        return False

    def check(self) -> None:
        """通せなければ例外にする。"""
        if not self.allows():
            raise CircuitOpen(self.name, self.retry_after)

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_error = ""
        if self.state == HALF:
            self.half_successes += 1
            if self.half_successes >= self.successes_to_close:
                self.state = CLOSED
                self.half_successes = 0
                log.info("%s を元に戻しました", self.name)
        elif self.state == OPEN:
            self.state = CLOSED

    def record_failure(self, error: str = "") -> None:
        self.last_error = error[:200]
        if self.state == HALF:
            # 様子見で失敗＝まだ直っていない。もう一度閉じる
            self.state = OPEN
            self.opened_at = self._now()
            log.warning("%s はまだ回復していません。引き続き使いません", self.name)
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failures_to_open and self.state == CLOSED:
            self.state = OPEN
            self.opened_at = self._now()
            log.error(
                "%s を一時的に使わないようにしました（%d回連続失敗: %s）",
                self.name, self.consecutive_failures, self.last_error or "?",
            )

    def reset(self) -> None:
        self.state = CLOSED
        self.consecutive_failures = 0
        self.half_successes = 0
        self.opened_at = 0.0
        self.last_error = ""

    def describe(self) -> str:
        if self.state == CLOSED:
            return "正常"
        if self.state == HALF:
            return "様子見"
        return f"停止中（あと{self.retry_after:.0f}秒）"


@dataclass
class Registry:
    """名前ごとにブレーカーを持つ。"""
    breakers: dict[str, Breaker] = field(default_factory=dict)

    def get(self, name: str, **kw) -> Breaker:
        b = self.breakers.get(name)
        if b is None:
            b = self.breakers[name] = Breaker(name=name, **kw)
        return b

    def allows(self, name: str) -> bool:
        return self.get(name).allows()

    def record_success(self, name: str) -> None:
        self.get(name).record_success()

    def record_failure(self, name: str, error: str = "") -> None:
        self.get(name).record_failure(error)

    def healthy(self) -> list[str]:
        return [n for n, b in self.breakers.items() if b.state == CLOSED]

    def blocked(self) -> list[Breaker]:
        return [b for b in self.breakers.values() if b.state == OPEN]

    def snapshot(self) -> list[Breaker]:
        return sorted(self.breakers.values(), key=lambda b: b.name)

    def reset_all(self) -> None:
        for b in self.breakers.values():
            b.reset()


# グループ（配信元）ごと
groups = Registry()
# マクドナルドのアカウントごと
accounts = Registry()
