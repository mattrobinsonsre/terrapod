"""Encryption service singleton: DEK cache, canary, encrypt/decrypt.

Lifecycle (in app lifespan, before init_ca which reads an encrypted column):
  init_encryption(db) → if encryption is enabled (or any DEK rows already exist),
  build the KEK provider, unwrap every DEK into an in-memory cache, and verify the
  decryptability **canary**. A wrong/missing key fails loud here — we never serve
  or write data we cannot read back.

encrypt()/decrypt() are synchronous and local (AES-GCM on small secrets, with the
cached DEK) — safe on the async hot path; only init/rotation touch the KEK.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.crypto import envelope
from terrapod.crypto.providers import build_provider
from terrapod.db.models import CryptoKey
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Known plaintext encrypted at key-creation; decrypted + compared on every boot.
_CANARY = "terrapod-encryption-canary-v1"


class EncryptionService:
    """Holds the unwrapped DEK cache and performs the local crypto."""

    def __init__(self) -> None:
        self.enabled: bool = False
        self._deks: dict[int, bytes] = {}
        self._active_version: int | None = None

    def encrypt(self, plaintext: str) -> str:
        """Encrypt when enabled; otherwise return plaintext unchanged."""
        if not self.enabled or self._active_version is None:
            return plaintext
        return envelope.encrypt(plaintext, self._deks[self._active_version], self._active_version)

    def decrypt(self, stored: str) -> str:
        """Decrypt a tpenc envelope; pass legacy plaintext through unchanged."""
        if not envelope.is_encrypted(stored):
            return stored
        version = envelope.parse_version(stored)
        dek = self._deks.get(version)
        if dek is None:
            # Fail loud — never hand a caller ciphertext as if it were plaintext.
            raise RuntimeError(
                f"cannot decrypt: no DEK for version {version} "
                "(wrong/rotated-away key, or encryption disabled before a decrypt pass)"
            )
        return envelope.decrypt(stored, dek)


_service: EncryptionService | None = None


def get_encryption() -> EncryptionService:
    """Return the service, defaulting to a disabled passthrough if uninitialised.

    Uninitialised happens in the migrations Job and in unit tests that never run
    the app lifespan — there, encryption is simply off (passthrough).
    """
    global _service  # noqa: PLW0603
    if _service is None:
        _service = EncryptionService()
    return _service


async def init_encryption(db: AsyncSession) -> None:
    """Build the provider, unwrap DEKs, run the canary. Fail closed on error."""
    global _service  # noqa: PLW0603
    svc = EncryptionService()

    rows = (await db.execute(select(CryptoKey))).scalars().all()
    if not settings.encryption.enabled and not rows:
        # Off and never enabled — pure passthrough, no provider needed.
        _service = svc
        logger.info("Encryption at rest disabled")
        return

    provider = build_provider()

    if not rows and settings.encryption.enabled:
        # First enable: mint a DEK, wrap it, stash the canary.
        dek = envelope.new_dek()
        wrapped = await provider.wrap(dek)
        canary = envelope.encrypt(_CANARY, dek, 1)
        row = CryptoKey(
            version=1, wrapped_dek=wrapped, provider=provider.id, canary=canary, active=True
        )
        db.add(row)
        await db.commit()
        rows = [row]
        logger.info("Encryption at rest enabled — minted DEK v1", provider=provider.id)

    # Unwrap all DEK versions into the cache (older versions needed for reads).
    for row in rows:
        svc._deks[row.version] = await provider.unwrap(row.wrapped_dek)
        if row.active:
            svc._active_version = row.version
    if svc._active_version is None and rows:
        svc._active_version = max(r.version for r in rows)

    svc.enabled = settings.encryption.enabled

    # Decryptability canary on the active key — fail closed on mismatch.
    active = next((r for r in rows if r.version == svc._active_version), None)
    if active is not None and active.canary:
        if svc.decrypt(active.canary) != _CANARY:
            raise RuntimeError("encryption canary mismatch — refusing to start (wrong key?)")

    _service = svc
    logger.info(
        "Encryption at rest initialised",
        enabled=svc.enabled,
        provider=provider.id,
        dek_versions=sorted(svc._deks),
        active_version=svc._active_version,
    )


def reset_encryption_for_tests() -> None:
    """Test helper — drop the singleton so the next get_encryption() is fresh."""
    global _service  # noqa: PLW0603
    _service = None
