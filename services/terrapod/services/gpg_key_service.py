"""Service layer for GPG key management.

Handles CRUD for GPG public keys used for provider signing verification.
Uses pgpy (pure Python) to parse key IDs from ASCII armor blocks.
"""

import uuid

import pgpy
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


async def create_gpg_key(
    db: AsyncSession,
    org: str,
    ascii_armor: str,
    source: str = "terrapod",
    source_url: str | None = None,
) -> GPGKey:
    """Create a new GPG key by parsing the key_id from the ASCII armor."""
    key_id = extract_key_id(ascii_armor)

    gpg_key = GPGKey(
        org_name=org,
        key_id=key_id,
        ascii_armor=ascii_armor,
        source=source,
        source_url=source_url,
    )
    db.add(gpg_key)
    await db.flush()

    logger.info("GPG key created", org=org, key_id=key_id)
    return gpg_key


async def list_gpg_keys(
    db: AsyncSession,
    org: str | None = None,
) -> list[GPGKey]:
    """List GPG keys, optionally filtered by organization."""
    stmt = select(GPGKey).order_by(GPGKey.created_at.desc())
    if org is not None:
        stmt = stmt.where(GPGKey.org_name == org)
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
    org: str,
    key_id: str,
) -> GPGKey | None:
    """Get a GPG key by org + GPG key ID."""
    result = await db.execute(
        select(GPGKey).where(GPGKey.org_name == org, GPGKey.key_id == key_id)
    )
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
