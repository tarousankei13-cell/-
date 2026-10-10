"""
再送の方針

通信は失敗する。多くは一時的なもので、少し待って送り直せば通る。
ただし**送り直してはいけない要求**がある。注文の登録や支払いの確定は、
相手に届いていたのに応答だけ失われた場合、送り直すと二重になる。

そこで、要求の性質をコード上の型として持たせ、
**再送してよいものだけ**を再送する。

    SAFE   読み取り。何度送っても結果は変わらない
           （店舗情報・メニュー・注文状態の確認）

    KEYED  冪等キーを持つ。相手が重複を弾いてくれる

    ONCE   一度きり。失敗しても絶対に送り直さない
           （注文の登録・支払いの確定・トークンの更新）

待ち時間は回ごとに倍にし、さらにばらつきを加える。
大人数が同時に使う場面で、再送のタイミングが揃って相手を
一斉に叩くのを避けるため。
"""

from __future__ import annotations

import asyncio
import logging
import random
from enum import Enum
from typing import Awaitable, Callable, TypeVar

log = logging.getLogger("bot.retry")

T = TypeVar("T")


class Idempotency(Enum):
    """その要求を送り直してよいか。"""

    SAFE = "safe"     # 読み取り。何度でも送ってよい
    KEYED = "keyed"   # 冪等キー付き。相手が重複を弾く
    ONCE = "once"     # 一度きり。絶対に送り直さない

    @property
    def retryable(self) -> bool:
        return self is not Idempotency.ONCE


# 既定の設定
MAX_ATTEMPTS = 3          # 最初の1回を含めた回数
BASE_DELAY = 0.4          # 1回目の待ち（秒）
MAX_DELAY = 6.0           # 待ちの上限
JITTER = 0.5              # ばらつきの割合（±50%）


def backoff_delay(attempt: int, *, base: float = BASE_DELAY,
                  cap: float = MAX_DELAY, jitter: float = JITTER) -> float:
    """
    attempt 回目（1始まり）の待ち時間。

    倍々に増やしたうえで、±jitter の範囲でばらつかせる。
    ばらつきを入れないと、同時に失敗した全員が同じ時刻に
    再送してしまい、相手をさらに苦しめる。
    """
    raw = min(base * (2 ** max(attempt - 1, 0)), cap)
    spread = raw * jitter
    return max(0.0, raw + random.uniform(-spread, spread))


class RetryError(Exception):
    """再送しても最後まで失敗した。"""

    def __init__(self, message: str, last: BaseException | None = None) -> None:
        super().__init__(message)
        self.last = last


async def call(
    fn: Callable[[], Awaitable[T]],
    *,
    idempotency: Idempotency,
    attempts: int = MAX_ATTEMPTS,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (),
    label: str = "",
    should_retry: Callable[[BaseException], bool] | None = None,
) -> T:
    """
    fn を呼び、必要なら送り直す。

    ⚠️ idempotency=ONCE のときは**1回しか呼ばない**。
       ここを緩めると二重注文・二重課金になる。

    give_up_on に挙げた例外は、再送せずそのまま投げる
    （認証エラーなど、送り直しても結果が変わらないもの）。
    """
    if not idempotency.retryable:
        attempts = 1

    last: BaseException | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            return await fn()
        except give_up_on:
            raise
        except retry_on as e:
            last = e
            if attempt >= attempts:
                break
            if should_retry is not None and not should_retry(e):
                raise
            delay = backoff_delay(attempt)
            log.info(
                "%s に失敗しました。%.1f秒後に送り直します (%d/%d): %s",
                label or "通信", delay, attempt, attempts, e,
            )
            await asyncio.sleep(delay)

    if last is not None:
        raise last
    raise RetryError(f"{label or '通信'}に失敗しました")


def guard_once(name: str) -> None:
    """
    「ここは絶対に再送しない」ことをコード上に残すための印。

    呼ぶと記録に残るだけだが、あとから読む人に意図が伝わる。
    """
    log.debug("再送しない処理です: %s", name)
