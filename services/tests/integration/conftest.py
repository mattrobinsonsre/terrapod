"""
Integration test fixtures — real Postgres, real Redis, real filesystem storage.

Auth is overridden (SSO can't be replicated in tests), everything else is real.
The app lifespan initializes DB/Redis/storage but skips the scheduler.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from terrapod.api.dependencies import (
    AuthenticatedUser,
    ListenerIdentity,
    get_current_user,
    get_listener_identity,
)
from terrapod.db.models import Base

# ---------------------------------------------------------------------------
# Ensure test-friendly defaults (must precede any Settings import)
# ---------------------------------------------------------------------------
os.environ.setdefault("TERRAPOD_STORAGE__BACKEND", "filesystem")
os.environ.setdefault("TERRAPOD_JSON_LOGS", "false")
os.environ.setdefault("TERRAPOD_LOG_LEVEL", "WARNING")
os.environ.setdefault("TERRAPOD_RATE_LIMIT__ENABLED", "false")

# All tables for TRUNCATE CASCADE (order doesn't matter with CASCADE)
_ALL_TABLES = [
    "task_stage_results",
    "task_stages",
    "run_tasks",
    "notification_configurations",
    "run_triggers",
    "audit_logs",
    "runs",
    "configuration_versions",
    "variable_set_workspaces",
    "variable_set_variables",
    "variable_sets",
    "variables",
    "state_versions",
    "module_workspace_links",
    "cached_binaries",
    "cached_provider_packages",
    "registry_provider_platforms",
    "registry_provider_versions",
    "registry_module_versions",
    "registry_modules",
    "registry_providers",
    "gpg_keys",
    "agent_pool_tokens",
    "agent_pools",
    "api_tokens",
    "role_assignments",
    "platform_role_assignments",
    "roles",
    "workspaces",
    "vcs_connections",
    "certificate_authority",
    "users",
]

_TRUNCATE_SQL = "TRUNCATE " + ", ".join(_ALL_TABLES) + " CASCADE"


# ---------------------------------------------------------------------------
# Helper: build test users
# ---------------------------------------------------------------------------


def admin_user(email: str = "admin@test.com") -> AuthenticatedUser:
    return AuthenticatedUser(
        email=email,
        display_name="Admin",
        roles=["admin", "everyone"],
        provider_name="local",
        auth_method="session",
    )


def regular_user(email: str = "user@test.com") -> AuthenticatedUser:
    return AuthenticatedUser(
        email=email,
        display_name="Regular User",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def user_with_roles(email: str, roles: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        email=email,
        display_name=email.split("@")[0].title(),
        roles=roles,
        provider_name="local",
        auth_method="session",
    )


def set_auth(app: FastAPI, user: AuthenticatedUser) -> None:
    """Override auth dependency to return *user* for all requests.

    Runner tokens (``runtok:`` prefix) are validated normally so that
    artifact-upload endpoints work with real scoped tokens.
    """
    from starlette.requests import Request

    async def _override(request: Request) -> AuthenticatedUser:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer runtok:"):
            token = auth_header.removeprefix("Bearer ")
            from terrapod.auth.runner_tokens import verify_runner_token

            run_id = verify_runner_token(token)
            if run_id is not None:
                return AuthenticatedUser(
                    email="runner",
                    display_name="Runner Job",
                    roles=["everyone"],
                    provider_name="runner_token",
                    auth_method="runner_token",
                    run_id=run_id,
                )
        return user

    app.dependency_overrides[get_current_user] = _override


def set_listener_auth(
    app: FastAPI,
    listener_id: str,
    pool_id: str,
    name: str = "test-listener",
) -> None:
    """Override listener certificate auth dependency."""
    import uuid as _uuid

    identity = ListenerIdentity(
        listener_id=_uuid.UUID(listener_id),
        name=name,
        pool_id=_uuid.UUID(pool_id),
        certificate_fingerprint="fake-fingerprint",
        certificate_expires_at=None,
    )

    async def _override() -> ListenerIdentity:
        return identity

    app.dependency_overrides[get_listener_identity] = _override


AUTH = {"Authorization": "Bearer integration-test-token"}


# ---------------------------------------------------------------------------
# Session-scoped: create/drop tables once per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def _create_tables():
    """Create all tables in the real test Postgres, yield, then drop.

    Uses a dedicated engine that is immediately disposed after DDL so it
    doesn't conflict with the per-test app engines (asyncpg forbids
    concurrent operations on the same connection).
    """
    from terrapod.config import settings

    engine = create_async_engine(str(settings.database_url), echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()

    yield  # tests run here

    # Teardown: drop all tables
    engine = create_async_engine(str(settings.database_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Function-scoped: FastAPI app with test lifespan (no scheduler)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _test_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Lightweight lifespan: DB + Redis + storage + connectors, no scheduler."""
    from terrapod.auth.connectors import init_connectors
    from terrapod.db.session import close_db, init_db
    from terrapod.redis.client import close_redis, init_redis
    from terrapod.storage import close_storage, init_storage

    await init_db()
    await init_redis()
    init_connectors()
    await init_storage()

    # Initialize Certificate Authority (required for agent pool join flow)
    from terrapod.auth.ca import init_ca
    from terrapod.db.session import get_db_session

    async with get_db_session() as db:
        await init_ca(db)

    yield

    await close_storage()
    await close_redis()
    await close_db()


@pytest.fixture
async def app(_create_tables) -> AsyncGenerator[FastAPI]:
    """Provide a FastAPI app wired to the real test DB/Redis/storage."""
    from terrapod.api.app import create_application

    application = create_application()
    # Swap lifespan to the test version (no scheduler)
    application.router.lifespan_context = _test_lifespan

    async with application.router.lifespan_context(application):
        yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """httpx AsyncClient talking to the test app via ASGI transport."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Per-test cleanup — uses the app's own DB session
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def clean_db(app):
    """Truncate all tables between tests using the app's DB pool."""
    yield
    from terrapod.db.session import get_db_session

    async with get_db_session() as session:
        await session.execute(text(_TRUNCATE_SQL))


@pytest.fixture(autouse=True)
async def clean_redis(app):
    """Flush Redis between tests."""
    yield
    from terrapod.redis.client import get_redis_client

    try:
        redis = get_redis_client()
        await redis.flushdb()
    except RuntimeError:
        pass  # Redis not initialized (test didn't use app fixture)


# ---------------------------------------------------------------------------
# Direct DB helpers (for seeding data outside the request cycle)
# ---------------------------------------------------------------------------


async def insert_role(
    engine,  # unused but kept for API compat — uses app's session
    name: str,
    workspace_permission: str = "read",
    allow_labels: dict | None = None,
    allow_names: list[str] | None = None,
    deny_labels: dict | None = None,
    deny_names: list[str] | None = None,
) -> None:
    """Insert a custom role directly into Postgres via the app's DB pool."""
    import json

    from terrapod.db.session import get_db_session

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO roles (name, workspace_permission, allow_labels, allow_names, "
                "deny_labels, deny_names, created_at, updated_at) "
                "VALUES (:name, :perm, :al, :an, :dl, :dn, now(), now())"
            ),
            {
                "name": name,
                "perm": workspace_permission,
                "al": json.dumps(allow_labels or {}),
                "an": json.dumps(allow_names or []),
                "dl": json.dumps(deny_labels or {}),
                "dn": json.dumps(deny_names or []),
            },
        )


async def assign_role(engine, provider: str, email: str, role_name: str) -> None:
    """Insert a custom role assignment directly into Postgres."""
    from terrapod.db.session import get_db_session

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO role_assignments (provider_name, email, role_name, created_at) "
                "VALUES (:p, :e, :r, now())"
            ),
            {"p": provider, "e": email, "r": role_name},
        )


async def assign_platform_role(engine, provider: str, email: str, role_name: str) -> None:
    """Insert a platform role assignment (admin/audit) directly into Postgres."""
    from terrapod.db.session import get_db_session

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO platform_role_assignments (provider_name, email, role_name, created_at) "
                "VALUES (:p, :e, :r, now())"
            ),
            {"p": provider, "e": email, "r": role_name},
        )
