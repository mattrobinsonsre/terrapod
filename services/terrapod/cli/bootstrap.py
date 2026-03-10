"""
Bootstrap script for creating the initial admin user and optional agent pool.

Idempotent: skips if resources already exist.
Run via: python -m terrapod.cli.bootstrap

Reads configuration from environment variables:
  TERRAPOD_BOOTSTRAP_ADMIN_EMAIL    - Admin email (required)
  TERRAPOD_BOOTSTRAP_ADMIN_PASSWORD - Admin password (optional; generated if omitted)
  DATABASE_URL                       - PostgreSQL connection URL (from Helm)
  TERRAPOD_BOOTSTRAP_POOL_NAME      - Agent pool name (optional; creates pool + join token)
  TERRAPOD_BOOTSTRAP_POOL_TOKEN     - Raw join token value (optional; generated if pool name set)
"""

import asyncio
import hashlib
import logging
import os
import secrets
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from terrapod.auth.passwords import hash_password
from terrapod.db.models import AgentPool, AgentPoolToken, PlatformRoleAssignment, User

# Use stdlib logging — structlog isn't configured yet during bootstrap
logger = logging.getLogger("terrapod.bootstrap")
logging.basicConfig(level=logging.INFO, format="%(message)s")


async def bootstrap() -> None:
    admin_email = os.environ.get("TERRAPOD_BOOTSTRAP_ADMIN_EMAIL", "").strip()
    admin_password = os.environ.get("TERRAPOD_BOOTSTRAP_ADMIN_PASSWORD", "").strip()
    database_url = os.environ.get("DATABASE_URL", "").strip()

    if not admin_email:
        logger.error("TERRAPOD_BOOTSTRAP_ADMIN_EMAIL is required")
        sys.exit(1)

    if not database_url:
        logger.error("DATABASE_URL is required")
        sys.exit(1)

    # Ensure async driver
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    generated = False
    if not admin_password:
        admin_password = secrets.token_urlsafe(24)
        generated = True

    engine = create_async_engine(database_url, echo=False)

    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        logger.info("Connected to database")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            # ── Admin user ──────────────────────────────────────────
            result = await session.execute(select(User).where(User.email == admin_email))
            existing_user = result.scalar_one_or_none()

            if existing_user:
                logger.info("User %s already exists, skipping user creation", admin_email)
            else:
                user = User(
                    email=admin_email,
                    display_name="Admin",
                    password_hash=hash_password(admin_password),
                    is_active=True,
                )
                session.add(user)
                logger.info("Created user: %s", admin_email)
                if generated:
                    print(f"Generated password: {admin_password}")  # noqa: T201 — intentional one-time credential output
                    print("IMPORTANT: Save this password now. It will not be shown again.")  # noqa: T201

            # ── Admin role ──────────────────────────────────────────
            result = await session.execute(
                select(PlatformRoleAssignment).where(
                    PlatformRoleAssignment.provider_name == "local",
                    PlatformRoleAssignment.email == admin_email,
                    PlatformRoleAssignment.role_name == "admin",
                )
            )
            existing_assignment = result.scalar_one_or_none()

            if existing_assignment:
                logger.info("Admin role already assigned to %s, skipping", admin_email)
            else:
                assignment = PlatformRoleAssignment(
                    provider_name="local",
                    email=admin_email,
                    role_name="admin",
                )
                session.add(assignment)
                logger.info("Assigned admin role to %s (provider: local)", admin_email)

            # ── Agent pool (optional) ───────────────────────────────
            pool_name = os.environ.get("TERRAPOD_BOOTSTRAP_POOL_NAME", "").strip()
            if pool_name:
                await _bootstrap_pool(session, pool_name)

    await engine.dispose()
    logger.info("Bootstrap complete")


async def _bootstrap_pool(session: AsyncSession, pool_name: str) -> None:
    """Create an agent pool and join token if they don't already exist."""
    raw_token = os.environ.get("TERRAPOD_BOOTSTRAP_POOL_TOKEN", "").strip()
    token_generated = False
    if not raw_token:
        raw_token = secrets.token_urlsafe(48)
        token_generated = True

    # Check if pool already exists
    result = await session.execute(select(AgentPool).where(AgentPool.name == pool_name))
    pool = result.scalar_one_or_none()

    if pool:
        logger.info("Agent pool '%s' already exists, skipping pool creation", pool_name)
    else:
        pool = AgentPool(name=pool_name, description=f"Bootstrapped pool: {pool_name}")
        session.add(pool)
        await session.flush()
        logger.info("Created agent pool: %s (id: %s)", pool_name, pool.id)

    # Check if a token already exists for this pool
    result = await session.execute(
        select(AgentPoolToken).where(
            AgentPoolToken.pool_id == pool.id,
            AgentPoolToken.is_revoked.is_(False),
        )
    )
    existing_token = result.scalar_one_or_none()

    if existing_token:
        # nosemgrep: python-logger-credential-disclosure
        logger.info("Join token already exists for pool '%s', skipping", pool_name)
    else:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        token = AgentPoolToken(
            pool_id=pool.id,
            token_hash=token_hash,
            description="Bootstrap token",
            created_by="bootstrap",
        )
        session.add(token)
        # nosemgrep: python-logger-credential-disclosure
        logger.info("Created join token for pool '%s'", pool_name)
        if token_generated:
            print(f"Generated join token: {raw_token}")  # noqa: T201 — intentional one-time credential output
            print("IMPORTANT: Save this token now. It will not be shown again.")  # noqa: T201
        else:
            logger.info("Join token created from TERRAPOD_BOOTSTRAP_POOL_TOKEN")


if __name__ == "__main__":
    asyncio.run(bootstrap())
