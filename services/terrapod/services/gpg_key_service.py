"""Service layer for GPG key management.

Handles CRUD for GPG public keys used for provider signing verification.
Uses pgpy (pure Python) to parse key IDs from ASCII armor blocks.
Includes auto-generation of signing keys and detached signature creation.
"""

import uuid

import pgpy
from pgpy.constants import (
    CompressionAlgorithm,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import GPGKey
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


def extract_key_id(ascii_armor: str) -> str:
    """Extract the GPG key ID from an ASCII-armored public key block.

    Returns the full 16-character key ID (last 16 hex digits of fingerprint).
    """
    key, _ = pgpy.PGPKey.from_blob(ascii_armor)
    return key.fingerprint.replace(" ", "")[-16:].upper()


def generate_signing_keypair() -> tuple[str, str]:
    """Generate a GPG keypair for provider signing.

    Returns (ascii_armor_public, ascii_armor_private).
    """
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("Terrapod Registry", email="registry@terrapod.local")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.ZLIB],
    )
    return str(key.pubkey), str(key)


def sign_data(private_key_armor: str, data: bytes) -> bytes:
    """Create a detached binary GPG signature over data.

    Returns the raw binary signature bytes (OpenPGP packet format),
    matching the format terraform/tofu expects for SHA256SUMS.sig.
    """
    key, _ = pgpy.PGPKey.from_blob(private_key_armor)
    msg = pgpy.PGPMessage.new(data, compression=CompressionAlgorithm.Uncompressed)
    sig = key.sign(msg)
    return bytes(sig)


def derive_public_key(private_key_armor: str) -> str:
    """Derive the ASCII-armored public key from a private key."""
    key, _ = pgpy.PGPKey.from_blob(private_key_armor)
    if not key.is_public:
        return str(key.pubkey)
    return str(key)


async def create_gpg_key(
    db: AsyncSession,
    ascii_armor: str,
    source: str = "terrapod",
    source_url: str | None = None,
    private_key_armor: str | None = None,
) -> GPGKey:
    """Create a new GPG key by parsing the key_id from the ASCII armor."""
    key_id = extract_key_id(ascii_armor)

    gpg_key = GPGKey(
        key_id=key_id,
        ascii_armor=ascii_armor,
        source=source,
        source_url=source_url,
        private_key=private_key_armor,
    )
    db.add(gpg_key)
    await db.flush()

    logger.info("GPG key created", key_id=key_id)
    return gpg_key


async def import_signing_key(
    db: AsyncSession,
    private_key_armor: str,
) -> GPGKey:
    """Import a user-provided private key for signing.

    Derives the public key from the private key and stores both.
    Replaces any existing signing key.
    """
    public_armor = derive_public_key(private_key_armor)

    # Delete any existing signing keys
    result = await db.execute(select(GPGKey).where(GPGKey.private_key.isnot(None)))
    for old_key in result.scalars().all():
        await db.delete(old_key)
    await db.flush()

    gpg_key = await create_gpg_key(
        db,
        ascii_armor=public_armor,
        source="terrapod",
        private_key_armor=private_key_armor,
    )
    logger.info("Signing key imported", key_id=gpg_key.key_id)
    return gpg_key


async def get_or_create_signing_key(db: AsyncSession) -> tuple[GPGKey, str]:
    """Get an existing signing key, import from config, or auto-generate one.

    Resolution order:
    1. Existing key with private key in DB -> use it
    2. TERRAPOD_REGISTRY__SIGNING_KEY env var / config -> import and use
    3. Auto-generate a new keypair

    Returns (GPGKey model, private_key_armor).
    """
    # 1. Existing key in DB
    result = await db.execute(
        select(GPGKey)
        .where(GPGKey.private_key.isnot(None))
        .order_by(GPGKey.created_at.desc())
        .limit(1)
    )
    gpg_key = result.scalars().first()

    if gpg_key is not None:
        return gpg_key, gpg_key.private_key  # type: ignore[return-value]

    # 2. Provided via config/env var
    from terrapod.config import settings

    if settings.registry.signing_key:
        logger.info("Importing signing key from config (TERRAPOD_REGISTRY__SIGNING_KEY)")
        gpg_key = await import_signing_key(db, settings.registry.signing_key)
        return gpg_key, settings.registry.signing_key

    # 3. Auto-generate
    logger.info("No signing key found, auto-generating GPG keypair")
    public_armor, private_armor = generate_signing_keypair()

    gpg_key = await create_gpg_key(
        db,
        ascii_armor=public_armor,
        source="terrapod",
        private_key_armor=private_armor,
    )
    return gpg_key, private_armor


async def list_gpg_keys(
    db: AsyncSession,
) -> list[GPGKey]:
    """List all GPG keys."""
    stmt = select(GPGKey).order_by(GPGKey.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_gpg_key(
    db: AsyncSession,
    key_db_id: uuid.UUID,
) -> GPGKey | None:
    """Get a GPG key by its database ID."""
    result = await db.execute(select(GPGKey).where(GPGKey.id == key_db_id))
    return result.scalars().first()


async def get_gpg_key_by_key_id(
    db: AsyncSession,
    key_id: str,
) -> GPGKey | None:
    """Get a GPG key by its GPG key ID."""
    result = await db.execute(select(GPGKey).where(GPGKey.key_id == key_id))
    return result.scalars().first()


async def delete_gpg_key(
    db: AsyncSession,
    key_db_id: uuid.UUID,
) -> bool:
    """Delete a GPG key by database ID. Returns True if found."""
    gpg_key = await get_gpg_key(db, key_db_id)
    if gpg_key is None:
        return False
    await db.delete(gpg_key)
    await db.flush()
    return True
