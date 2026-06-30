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
        self._provider_id: str = ""
        self._canary_ok: bool = True

    def status(self) -> dict:
        """Operator-facing health: are we currently able to decrypt everything?

        ``decryptable`` is the headline durability signal — true when the canary
        decrypts and the active DEK is loaded. Used by the admin status endpoint
        and the encryption doctor.
        """
        decryptable = self._canary_ok and (not self.enabled or self._active_version is not None)
        return {
            "enabled": self.enabled,
            "provider": self._provider_id,
            "active_version": self._active_version,
            "dek_versions": sorted(self._deks),
            "canary_ok": self._canary_ok,
            "decryptable": decryptable,
        }

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
        # First enable: mint a DEK and wrap it. DEATH-BY-ENCRYPTION GUARD — before
        # we persist the key or let a single byte of data get encrypted, PROVE the
        # provider can both wrap AND unwrap: wrap, then unwrap and assert we get
        # the exact DEK back. If the provider is misconfigured (wrong KMS key,
        # no decrypt permission, unreachable Vault) this raises here and nothing
        # is ever written encrypted — so you can never reach a state where data
        # is encrypted under a key you cannot use.
        dek = envelope.new_dek()
        wrapped = await provider.wrap(dek)
        if await provider.unwrap(wrapped) != dek:
            raise RuntimeError(
                "encryption enable aborted: KEK provider wrap→unwrap round-trip failed "
                f"(provider={provider.id}). Refusing to encrypt data the provider cannot decrypt."
            )
        canary = envelope.encrypt(_CANARY, dek, 1)
        row = CryptoKey(
            version=1, wrapped_dek=wrapped, provider=provider.id, canary=canary, active=True
        )
        db.add(row)
        await db.commit()
        rows = [row]
        logger.info("Encryption at rest enabled — minted DEK v1", provider=provider.id)

    svc._provider_id = provider.id

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
        svc._canary_ok = True

    _service = svc
    logger.info(
        "Encryption at rest initialised",
        enabled=svc.enabled,
        provider=provider.id,
        dek_versions=sorted(svc._deks),
        active_version=svc._active_version,
    )


async def verify_live(db: AsyncSession) -> dict:
    """Independently prove every stored DEK is decryptable RIGHT NOW.

    Unlike the cached boot state, this re-builds the provider and re-unwraps every
    ``crypto_keys`` row live (catching e.g. a KMS permission revoked or a Vault key
    deleted after startup), then verifies each row's canary. Returns a result dict
    with ``ok``; the encryption doctor exits non-zero when ``ok`` is False. This is
    the "can I still decrypt everything?" drill.
    """
    rows = (await db.execute(select(CryptoKey))).scalars().all()
    if not rows:
        return {
            "ok": True,
            "enabled": settings.encryption.enabled,
            "checked_versions": [],
            "failures": [],
            "note": "no DEKs (encryption never enabled)",
        }

    provider = build_provider()
    failures: list[str] = []
    checked: list[int] = []
    for row in rows:
        try:
            dek = await provider.unwrap(row.wrapped_dek)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"v{row.version}: unwrap failed: {exc}")
            continue
        # The canary decrypt itself can raise (e.g. AES-GCM InvalidTag when the
        # unwrapped key is wrong) — catch it so the doctor reports a clean
        # failure instead of crashing.
        try:
            if row.canary and envelope.decrypt(row.canary, dek) != _CANARY:
                failures.append(f"v{row.version}: canary did not decrypt to the expected value")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"v{row.version}: canary decrypt failed: {exc}")
            continue
        checked.append(row.version)

    return {
        "ok": not failures,
        "enabled": settings.encryption.enabled,
        "provider": provider.id,
        "checked_versions": sorted(checked),
        "failures": failures,
    }


def reset_encryption_for_tests() -> None:
    """Test helper — drop the singleton so the next get_encryption() is fresh."""
    global _service  # noqa: PLW0603
    _service = None
