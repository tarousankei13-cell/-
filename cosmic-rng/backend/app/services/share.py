"""Signed share links for notable rolls.

The signature keeps roll IDs from being enumerated: only a player who was
handed the URL (inside their own roll result) can open the public page.
"""
from __future__ import annotations

import hashlib
import hmac

from ..config import get_settings


def share_sig(roll_id: int) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"share.r.{roll_id}".encode(), hashlib.sha256).hexdigest()[:24]


def check_sig(roll_id: int, s: str) -> bool:
    return hmac.compare_digest(s, share_sig(roll_id))


def share_url(roll_id: int) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/share/r/{roll_id}?s={share_sig(roll_id)}"
