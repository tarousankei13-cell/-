"""
認証情報の暗号化

マクドナルド／Kyash のメールアドレス・パスワード・トークンは、
DBに平文で置かない。AES-256-GCM で暗号化して保存する。

⚠️ ENCRYPTION_KEY を変更すると、保存済みのデータは復号できなくなる。
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_SIZE = 12
_VERSION = b"\x01"   # 将来アルゴリズムを変えられるようにしておく


class CryptoError(Exception):
    pass


class Cipher:
    """AES-256-GCM による暗号化・復号。"""

    def __init__(self, key: str) -> None:
        if not key:
            raise CryptoError("ENCRYPTION_KEY が設定されていません")
        self._key = self._derive_key(key)
        self._aes = AESGCM(self._key)

    @staticmethod
    def _derive_key(key: str) -> bytes:
        """
        設定値から32バイトの鍵を作る。

        base64 で32バイトならそのまま使い、そうでなければ SHA-256 で
        32バイトに伸ばす（設定を書き間違えても起動はできるように）。
        """
        try:
            raw = base64.urlsafe_b64decode(key.encode())
            if len(raw) == 32:
                return raw
        except Exception:
            pass
        return hashlib.sha256(key.encode()).digest()

    def encrypt(self, plaintext: str | None) -> bytes | None:
        if plaintext is None:
            return None
        nonce = os.urandom(_NONCE_SIZE)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return _VERSION + nonce + ct

    def decrypt(self, blob: bytes | None) -> str | None:
        if blob is None:
            return None
        if len(blob) < 1 + _NONCE_SIZE + 16:
            raise CryptoError("暗号文が短すぎます（データが壊れています）")
        if blob[:1] != _VERSION:
            raise CryptoError(f"未対応の暗号化バージョンです: {blob[:1]!r}")
        nonce = blob[1:1 + _NONCE_SIZE]
        ct = blob[1 + _NONCE_SIZE:]
        try:
            return self._aes.decrypt(nonce, ct, None).decode("utf-8")
        except Exception as e:
            raise CryptoError(
                "復号に失敗しました。ENCRYPTION_KEY が変更された可能性があります。"
            ) from e


_cipher: Cipher | None = None


def init_cipher(key: str) -> Cipher:
    global _cipher
    _cipher = Cipher(key)
    return _cipher


def get_cipher() -> Cipher:
    if _cipher is None:
        raise CryptoError("init_cipher() がまだ呼ばれていません")
    return _cipher


def mask(value: str | None, keep: int = 6) -> str:
    """ログ出力用。トークンやリンクを先頭数文字だけにする。"""
    if not value:
        return "(なし)"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…({len(value)}文字)"
