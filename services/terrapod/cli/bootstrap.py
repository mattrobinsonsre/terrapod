"""
Bootstrap script for creating the initial admin user.

Idempotent: skips if resources already exist.
Run via: python -m terrapod.cli.bootstrap

Reads configuration from environment variables:
  TERRAPOD_BOOTSTRAP_ADMIN_EMAIL    - Admin email (required)
  TERRAPOD_BOOTSTRAP_ADMIN_PASSWORD - Admin password (optional; generated if omitted)
  DATABASE_URL                       - PostgreSQL connection URL (from Helm)
"""

import asyncio
import logging
import os
import secrets
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from terrapod.auth.passwords import hash_password
from terrapod.db.models import PlatformRoleAssignment, User

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
            # Check if user already exists
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
                    logger.info("Generated password: %s", admin_password)
                    logger.warning("IMPORTANT: Save this password now. It will not be shown again.")

            # Check if admin role assignment exists
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

    await engine.dispose()
    logger.info("Bootstrap complete")


if __name__ == "__main__":
    asyncio.run(bootstrap())
