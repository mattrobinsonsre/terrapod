"""API token management for terraform CLI and automation.

API tokens are long-lived Bearer tokens stored as SHA-256 hashes in PostgreSQL.
The raw token value is only available at creation time. Lookup is by hash
(indexed column) on every request.

Token format: {random_id}.tpod.{random_secret}
"""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import APIToken, utc_now
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Minimum interval between last_used_at updates (seconds).
# Avoids a DB write on every single API request.
LAST_USED_UPDATE_INTERVAL = 60


def _generate_token_id() -> str:
    """Generate a token ID in the format 'at-{random}'."""
    return f"at-{secrets.token_hex(8)}"


def _generate_raw_token() -> str:
    """Generate a raw token in the format '{random_id}.tpod.{random_secret}'."""
    random_id = secrets.token_urlsafe(12)
    random_secret = secrets.token_urlsafe(32)
    return f"{random_id}.tpod.{random_secret}"


def hash_token(raw_token: str) -> str:
    """SHA-256 hash a raw token for storage."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def create_api_token(
    db: AsyncSession,
    user_email: str,
    description: str = "",
    token_type: str = "user",
    lifespan_hours: int | None = None,
) -> tuple[APIToken, str]:
    """Create an API token. Returns (model, raw_token_value).

    The raw token value is only available at creation time.

    If lifespan_hours is provided, it is clamped to the global max TTL.
    If None, the token uses the global max TTL at validation time.
    """
    raw_token = _generate_raw_token()
    token_id = _generate_token_id()

    # Clamp lifespan to global max if set
    max_ttl = settings.auth.api_token_max_ttl_hours
    if lifespan_hours is not None and max_ttl > 0:
        lifespan_hours = min(lifespan_hours, max_ttl)

    api_token = APIToken(
        id=token_id,
        token_hash=hash_token(raw_token),
        description=description,
        user_email=user_email,
        token_type=token_type,
        lifespan_hours=lifespan_hours,
    )

    db.add(api_token)
    await db.flush()

    logger.info(
        "API token created",
        token_id=token_id,
        user_email=user_email,
        token_type=token_type,
        lifespan_hours=lifespan_hours,
    )

    return api_token, raw_token


async def validate_api_token(db: AsyncSession, raw_token: str) -> APIToken | None:
    """Validate a Bearer token against the database.

    SHA-256 hash the token, look up by hash. Check expiry.
    Update last_used_at (rate-limited to once per minute).
    """
    token_hash = hash_token(raw_token)

    result = await db.execute(select(APIToken).where(APIToken.token_hash == token_hash))
    api_token = result.scalar_one_or_none()

    if api_token is None:
        return None

    # Check token lifetime: per-token lifespan takes precedence, else global max
    now = utc_now()
    effective_ttl = (
        api_token.lifespan_hours
        if api_token.lifespan_hours is not None
        else settings.auth.api_token_max_ttl_hours
    )
    if effective_ttl > 0:
        expiry = api_token.created_at + timedelta(hours=effective_ttl)
        if now > expiry:
            logger.debug("API token expired", token_id=api_token.id, ttl_hours=effective_ttl)
            return None

    # Update last_used_at (rate-limited)
    should_update = (
        api_token.last_used_at is None
        or (now - api_token.last_used_at).total_seconds() > LAST_USED_UPDATE_INTERVAL
    )
    if should_update:
        await db.execute(
            update(APIToken).where(APIToken.id == api_token.id).values(last_used_at=now)
        )

    return api_token


async def list_user_tokens(db: AsyncSession, user_email: str) -> list[APIToken]:
    """List all API tokens for a user."""
    result = await db.execute(
        select(APIToken)
        .where(APIToken.user_email == user_email)
        .order_by(APIToken.created_at.desc())
    )
    return list(result.scalars().all())


async def get_token_by_id(db: AsyncSession, token_id: str) -> APIToken | None:
    """Get a token by its public ID."""
    result = await db.execute(select(APIToken).where(APIToken.id == token_id))
    return result.scalar_one_or_none()


async def revoke_token(db: AsyncSession, token_id: str) -> bool:
    """Revoke (delete) an API token by ID.

    Returns True if the token existed, False if not found.
    """
    result = await db.execute(select(APIToken).where(APIToken.id == token_id))
    api_token = result.scalar_one_or_none()

    if api_token is None:
        return False

    await db.delete(api_token)
    await db.flush()

    logger.info("API token revoked", token_id=token_id)
    return True
