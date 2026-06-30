"""SQLAlchemy column type that transparently encrypts at the DB boundary.

A column declared ``EncryptedText`` is encrypted on write and decrypted on read
via the process-wide encryption service. When encryption is disabled (the
default) it is a pure passthrough — and on read it always passes through legacy
plaintext (un-prefixed values) unchanged, so enabling/disabling is a migration,
not a hard cutover. The DB column is still ``TEXT`` (no schema migration needed
to adopt it on an existing column).
"""

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


class EncryptedText(TypeDecorator):
    """TEXT column encrypted at rest via the app-layer encryption service."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        from terrapod.crypto.service import get_encryption

        return get_encryption().encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        from terrapod.crypto.service import get_encryption

        return get_encryption().decrypt(value)
