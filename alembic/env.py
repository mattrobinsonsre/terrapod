"""Alembic environment configuration for async SQLAlchemy."""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import event, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from terrapod.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow DATABASE_URL env var to override alembic.ini (used by K8s migration Job)
database_url = os.environ.get("DATABASE_URL", "")
if database_url:
    # Ensure async driver
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Cloud-IAM DB auth (#573) for the migrations Job: mirror the API's
    # per-connection token + TLS injection so the Job authenticates the same way
    # as the running app (the DB role is IAM-only / TLS-required). Config comes
    # via TP_DB_* env vars set by the migrations Job template. Default
    # auth_mode="password" leaves the engine untouched.
    auth_mode = os.environ.get("TP_DB_AUTH_MODE", "password")
    if auth_mode in ("aws_iam", "gcp_iam", "azure_ad"):
        from terrapod.db import iam_auth

        host, port, user = iam_auth.parse_pg_target(config.get_main_option("sqlalchemy.url") or "")
        event.listen(
            connectable.sync_engine,
            "do_connect",
            iam_auth.make_do_connect_handler(
                auth_mode=auth_mode,
                host=host,
                port=port,
                user=user,
                region=os.environ.get("TP_DB_AWS_IAM_REGION", ""),
                ssl_mode=os.environ.get("TP_DB_SSL_MODE", ""),
                ssl_root_cert=os.environ.get("TP_DB_SSL_ROOT_CERT", ""),
            ),
        )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
