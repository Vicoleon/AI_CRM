"""Encrypt personal OAuth tokens before they reach a workspace database."""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from rubik_core.tenancy.secrets import decrypt_personal, encrypt_personal


class EncryptedString(TypeDecorator[str]):
    """Fernet-backed string with plaintext only on the ORM object boundary."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return encrypt_personal(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return decrypt_personal(value)
