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

    @property
    def python_type(self) -> type:
        """`str`, the same as the TEXT column it decorates.

        `TypeDecorator` does not forward this to its impl, so without it the
        base `TypeEngine` raises `NotImplementedError` — and anything that
        introspects column types generically (replication's payload coercion
        among them) blows up the moment it meets an encrypted column, which is
        exactly when it is handling a credential.
        """
        return str

    def process_bind_param(self, value: str | None, dialect) -> str | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        from terrapod.crypto import envelope
        from terrapod.crypto.service import get_encryption

        svc = get_encryption()
        # With encryption OFF, encrypt() is a passthrough, so a value that
        # merely looks like an envelope is stored verbatim -- and every
        # subsequent read of the row runs it through decrypt(), which fails
        # loudly. The row becomes unreadable, and because the delete path loads
        # it first, undeletable too. Refused here rather than in one router
        # because every encrypted column passes through this one place, and the
        # reported instance (a workspace variable) was only the cheapest to
        # reach: a webhook secret or a VCS token does the same thing.
        #
        # Only when disabled. With encryption on the value is wrapped into a
        # real envelope and round-trips back as the literal text the user wrote,
        # so refusing it there would reject a legitimate value in exactly the
        # configuration where it is safe.
        if not svc.enabled and envelope.is_encrypted(value):
            raise ValueError(
                f"a value may not begin with {envelope.MARKER!r}: that prefix marks "
                "an encrypted value and would make this row unreadable"
            )
        return svc.encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        from terrapod.crypto.service import get_encryption

        return get_encryption().decrypt(value)
