from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def ts(dt: datetime | None) -> float | None:
    """Epoch milliseconds for the client (None-safe)."""
    return None if dt is None else round(dt.timestamp() * 1000)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat()


def period_key(now: datetime, tz_name: str) -> str:
    """Daily period key in the configured reset timezone (e.g. Asia/Tokyo)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = UTC  # type: ignore[assignment]
    return now.astimezone(tz).strftime("%Y-%m-%d")


def next_reset(now: datetime, tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = UTC  # type: ignore[assignment]
    local = now.astimezone(tz)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(UTC)
