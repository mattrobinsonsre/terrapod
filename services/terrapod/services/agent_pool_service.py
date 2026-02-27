"""Agent pool, join token, and listener management service."""

import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.ca import (
    get_ca,
    get_certificate_fingerprint,
    serialize_certificate,
    serialize_private_key,
)
from terrapod.db.models import AgentPool, AgentPoolToken, RunnerListener, utc_now
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


async def create_pool(
    db: AsyncSession,
    name: str,
    description: str = "",
    org_name: str = "default",
    service_account_name: str = "",
) -> AgentPool:
    """Create a new agent pool."""
    pool = AgentPool(
        name=name,
        description=description,
        org_name=org_name,
        service_account_name=service_account_name,
    )
    db.add(pool)
    await db.flush()
    return pool


async def get_pool(db: AsyncSession, pool_id: uuid.UUID) -> AgentPool | None:
    """Get an agent pool by ID."""
    result = await db.execute(select(AgentPool).where(AgentPool.id == pool_id))
    return result.scalar_one_or_none()


async def get_pool_by_name(db: AsyncSession, name: str) -> AgentPool | None:
    """Get an agent pool by name."""
    result = await db.execute(select(AgentPool).where(AgentPool.name == name))
    return result.scalar_one_or_none()


async def list_pools(db: AsyncSession, org_name: str = "default") -> list[AgentPool]:
    """List all agent pools."""
    result = await db.execute(
        select(AgentPool).where(AgentPool.org_name == org_name).order_by(AgentPool.name)
    )
    return list(result.scalars().all())


async def update_pool(
    db: AsyncSession,
    pool: AgentPool,
    name: str | None = None,
    description: str | None = None,
    service_account_name: str | None = None,
) -> AgentPool:
    """Update an agent pool."""
    if name is not None:
        pool.name = name
    if description is not None:
        pool.description = description
    if service_account_name is not None:
        pool.service_account_name = service_account_name
    await db.flush()
    return pool


async def delete_pool(db: AsyncSession, pool: AgentPool) -> None:
    """Delete an agent pool."""
    await db.delete(pool)
    await db.flush()


# --- Join Tokens ---


def generate_join_token() -> tuple[str, str]:
    """Generate a join token. Returns (raw_token, sha256_hash)."""
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


async def create_pool_token(
    db: AsyncSession,
    pool_id: uuid.UUID,
    description: str,
    created_by: str,
    expires_at=None,
    max_uses: int | None = None,
) -> tuple[AgentPoolToken, str]:
    """Create a join token for an agent pool. Returns (token_record, raw_token)."""
    raw_token, token_hash = generate_join_token()

    token = AgentPoolToken(
        pool_id=pool_id,
        token_hash=token_hash,
        description=description,
        created_by=created_by,
        expires_at=expires_at,
        max_uses=max_uses,
    )
    db.add(token)
    await db.flush()
    return token, raw_token


async def validate_join_token(
    db: AsyncSession, raw_token: str
) -> AgentPoolToken | None:
    """Validate a join token. Returns the token record if valid, None otherwise."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    result = await db.execute(
        select(AgentPoolToken).where(AgentPoolToken.token_hash == token_hash)
    )
    token = result.scalar_one_or_none()
    if token is None:
        return None

    # Check revoked
    if token.is_revoked:
        return None

    # Check expiry
    if token.expires_at and utc_now() > token.expires_at:
        return None

    # Check max uses
    if token.max_uses is not None and token.use_count >= token.max_uses:
        return None

    return token


async def list_pool_tokens(
    db: AsyncSession, pool_id: uuid.UUID
) -> list[AgentPoolToken]:
    """List all tokens for an agent pool."""
    result = await db.execute(
        select(AgentPoolToken)
        .where(AgentPoolToken.pool_id == pool_id)
        .order_by(AgentPoolToken.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_pool_token(db: AsyncSession, token: AgentPoolToken) -> None:
    """Revoke a join token."""
    token.is_revoked = True
    await db.flush()


async def delete_pool_token(db: AsyncSession, token: AgentPoolToken) -> None:
    """Delete a join token."""
    await db.delete(token)
    await db.flush()


# --- Listeners ---


async def join_listener(
    db: AsyncSession,
    pool: AgentPool,
    token: AgentPoolToken,
    name: str,
    runner_definitions: list[str],
) -> dict:
    """Register a new listener via join token exchange.

    Returns dict with listener_id, certificate PEM, private key PEM, CA cert PEM.
    """
    ca = get_ca()

    # Issue certificate
    cert, private_key = ca.issue_listener_certificate(name, pool.name)
    fingerprint = get_certificate_fingerprint(cert)

    # Create listener record
    listener = RunnerListener(
        pool_id=pool.id,
        name=name,
        certificate_fingerprint=fingerprint,
        certificate_expires_at=cert.not_valid_after_utc,
        runner_definitions=runner_definitions,
    )
    db.add(listener)

    # Increment token use count
    token.use_count += 1
    await db.flush()

    logger.info(
        "Listener joined",
        listener=name,
        pool=pool.name,
        fingerprint=fingerprint[:16],
    )

    return {
        "listener_id": str(listener.id),
        "certificate": serialize_certificate(cert).decode(),
        "private_key": serialize_private_key(private_key).decode(),
        "ca_certificate": ca.ca_cert_pem,
    }


async def register_local_listener(
    db: AsyncSession,
    pool_name: str = "default",
    listener_name: str = "local",
    runner_definitions: list[str] | None = None,
) -> RunnerListener:
    """Register or update the local in-cluster listener.

    Called on startup when TERRAPOD_LISTENER_MODE=local. No join token needed.
    """
    if runner_definitions is None:
        runner_definitions = ["standard"]

    # Ensure pool exists
    pool = await get_pool_by_name(db, pool_name)
    if pool is None:
        pool = await create_pool(db, pool_name, description="Default local pool")

    # Check if listener already exists
    result = await db.execute(
        select(RunnerListener).where(RunnerListener.name == listener_name)
    )
    listener = result.scalar_one_or_none()

    if listener:
        listener.runner_definitions = runner_definitions
        listener.pool_id = pool.id
        await db.flush()
        logger.info("Updated local listener registration", listener=listener_name)
    else:
        listener = RunnerListener(
            pool_id=pool.id,
            name=listener_name,
            runner_definitions=runner_definitions,
        )
        db.add(listener)
        await db.flush()
        logger.info("Registered local listener", listener=listener_name)

    return listener


async def get_listener(db: AsyncSession, listener_id: uuid.UUID) -> RunnerListener | None:
    """Get a listener by ID."""
    result = await db.execute(
        select(RunnerListener).where(RunnerListener.id == listener_id)
    )
    return result.scalar_one_or_none()


async def get_listener_by_name(db: AsyncSession, name: str) -> RunnerListener | None:
    """Get a listener by name."""
    result = await db.execute(
        select(RunnerListener).where(RunnerListener.name == name)
    )
    return result.scalar_one_or_none()


async def list_listeners(
    db: AsyncSession, pool_id: uuid.UUID
) -> list[RunnerListener]:
    """List all listeners for an agent pool."""
    result = await db.execute(
        select(RunnerListener)
        .where(RunnerListener.pool_id == pool_id)
        .order_by(RunnerListener.name)
    )
    return list(result.scalars().all())


async def delete_listener(db: AsyncSession, listener: RunnerListener) -> None:
    """Delete a listener."""
    await db.delete(listener)
    await db.flush()


async def renew_listener_certificate(
    db: AsyncSession,
    listener: RunnerListener,
    pool: AgentPool,
) -> dict:
    """Renew a listener's certificate."""
    ca = get_ca()
    cert, private_key = ca.issue_listener_certificate(listener.name, pool.name)
    fingerprint = get_certificate_fingerprint(cert)

    listener.certificate_fingerprint = fingerprint
    listener.certificate_expires_at = cert.not_valid_after_utc
    await db.flush()

    logger.info(
        "Renewed listener certificate",
        listener=listener.name,
        fingerprint=fingerprint[:16],
    )

    return {
        "certificate": serialize_certificate(cert).decode(),
        "private_key": serialize_private_key(private_key).decode(),
        "ca_certificate": ca.ca_cert_pem,
    }
