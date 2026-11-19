"""Encryption at rest for GitHub OAuth tokens (spec §4 item 2: encrypted, backend-only).

Fernet (AES-128-CBC + HMAC-SHA256). The key comes from `SWF_TOKEN_ENCRYPTION_KEY` and never
reaches the database or the task container.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TokenDecryptError(Exception):
    pass


class TokenCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("token encryption key must be a urlsafe-base64 Fernet key") from exc

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, blob: str) -> str:
        try:
            return self._fernet.decrypt(blob.encode()).decode()
        except InvalidToken as exc:
            raise TokenDecryptError("stored token does not decrypt with the current key") from exc
