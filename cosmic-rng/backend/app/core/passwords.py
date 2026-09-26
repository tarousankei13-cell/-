"""Password hashing.

``hashlib.scrypt`` is a memory-hard KDF in the standard library, so accounts get
a proper slow hash without pulling in a native dependency that has to compile on
whatever host this ends up on.

Stored format (single column, self-describing so the cost can be raised later
without invalidating existing hashes):

    scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata

# ~64 MiB and roughly 100 ms on a small VPS core. Raising N later is safe: old
# hashes keep their own parameters and are re-hashed on the next successful login.
N = 2 ** 16
R = 8
P = 1
DK_LEN = 32
SALT_BYTES = 16
MAXMEM = 128 * N * R * 2

MIN_PASSWORD = 8
MAX_PASSWORD = 128  # bound the work an unauthenticated request can ask for
USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def normalise(password: str) -> bytes:
    """NFKC so a password typed on a phone matches the one typed on a desktop."""
    return unicodedata.normalize("NFKC", password).encode("utf-8")


def hash_password(password: str, *, n: int = N, r: int = R, p: int = P) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.scrypt(normalise(password), salt=salt, n=n, r=r, p=p, dklen=DK_LEN, maxmem=MAXMEM)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. False for any malformed or missing hash."""
    if not stored:
        return False
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_s)
        expected = base64.b64decode(hash_s)
    except (ValueError, TypeError):
        return False
    dk = hashlib.scrypt(normalise(password), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                        maxmem=128 * n * r * 2)
    return hmac.compare_digest(dk, expected)


def needs_rehash(stored: str | None) -> bool:
    """True when a hash was made with weaker parameters than we now use."""
    if not stored:
        return True
    try:
        scheme, n_s, r_s, p_s, _, _ = stored.split("$")
    except ValueError:
        return True
    return scheme != "scrypt" or int(n_s) < N or int(r_s) < R or int(p_s) < P


# ---------------------------------------------------------------------------
# input rules (the API reports these; the UI shows the same text)
# ---------------------------------------------------------------------------
def check_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"パスワードは{MIN_PASSWORD}文字以上にしてください"
    if len(password) > MAX_PASSWORD:
        return f"パスワードは{MAX_PASSWORD}文字以内にしてください"
    if password.strip() != password:
        return "パスワードの前後に空白は使えません"
    return None


def check_username(username: str) -> str | None:
    if not USERNAME_RE.match(username):
        return "ユーザー名は3〜32文字の半角英数字と _ . - が使えます（先頭は英数字か _）"
    return None


def check_email(email: str) -> str | None:
    if len(email) > 190 or not EMAIL_RE.match(email):
        return "メールアドレスの形式が正しくありません"
    return None
