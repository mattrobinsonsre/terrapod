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

from ..meta.shard_plan import plan_shards

# ---------------------------------------------------------------------------
# Ensure test-friendly defaults (must precede any Settings import)
# ---------------------------------------------------------------------------
os.environ.setdefault("TERRAPOD_STORAGE__BACKEND", "filesystem")
os.environ.setdefault("TERRAPOD_JSON_LOGS", "false")
os.environ.setdefault("TERRAPOD_LOG_LEVEL", "WARNING")
os.environ.setdefault("TERRAPOD_RATE_LIMIT__ENABLED", "false")


# Tables to TRUNCATE between tests, DERIVED from the models (#1161).
#
# This was a hand-maintained list, and it had drifted: 23 of 56 tables were
# missing, so isolation was silently broken for everything added since somebody
# last remembered to append to it — replication, execution hooks, policies, the
# catalog, the AI surfaces, and `oauth_clients`, which is what surfaced it (a peer
# client created by one test was still there for the next, which then hit the
# duplicate-refusal path and failed only when the file ran as a whole).
#
# Derived, so it cannot drift again. `crypto_keys` is deliberately included: with
# every other table emptied there is nothing left encrypted under the old DEK, and
# a stale key surviving into a test that writes an encrypted column is the subtler
# hazard. Names come from the model metadata, never from input, so the
# TRUNCATE below is still built from trusted strings.
def _all_tables() -> list[str]:
    from terrapod.db.models import Base

    return [table.name for table in Base.metadata.sorted_tables]


_TRUNCATE_SQL = "TRUNCATE " + ", ".join(_all_tables()) + " CASCADE"


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
        # _TRUNCATE_SQL is built from the model metadata's table names (see top
        # of file), not from any request/user input — no injection surface in
        # this test cleanup fixture.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
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
    """Insert a custom role directly into Postgres via the app's DB pool.

    The legacy hierarchical ``workspace_permission`` level (#585) is no longer a
    column — it is expanded into the stored ``capabilities`` list via
    ``expand_preset``, exactly as the roles router does on write."""
    import json

    from terrapod.auth.capabilities import expand_preset
    from terrapod.db.session import get_db_session

    capabilities = expand_preset(
        workspace_permission=workspace_permission,
        pool_permission=None,
        registry_permission=None,
        catalog_permission=None,
    )

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO roles (name, capabilities, allow_labels, allow_names, "
                "deny_labels, deny_names, created_at, updated_at) "
                "VALUES (:name, :caps, :al, :an, :dl, :dn, now(), now())"
            ),
            {
                "name": name,
                "caps": json.dumps(capabilities),
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


# ── Runner-level sharding (#1468) ─────────────────────────
#
# `--shard k/N` keeps only the files assigned to shard k. Splitting happens
# AFTER collection, so it sees the true set of files and a new test file cannot
# be silently missed — the failure mode of a hand-maintained path list.
#
# Runner-level, not xdist: the objection to xdist here is that its workers share
# one database and race to create the same tables. Separate runners each get
# their own compose stack, which is how the unit and services-api slices already
# work.


def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        default=None,
        help="Run only this shard of the suite, as k/N (1-based). Splits by file, "
        "balanced on collected test count.",
    )


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if not spec:
        return

    try:
        index_s, total_s = spec.split("/", 1)
        index, total = int(index_s), int(total_s)
    except ValueError:
        raise pytest.UsageError(f"--shard expects k/N, got {spec!r}") from None
    if not 1 <= index <= total:
        raise pytest.UsageError(f"--shard {spec}: k must be within 1..N")

    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.path)] = counts.get(str(item.path), 0) + 1

    keep = set(plan_shards(counts, total)[index - 1])
    selected = [i for i in items if str(i.path) in keep]
    deselected = [i for i in items if str(i.path) not in keep]

    # Printed so a shard that selects nothing is visible in the log rather than
    # passing as a vacuous success.
    print(
        f"\nshard {index}/{total}: {len(keep)} files, {len(selected)} tests "
        f"({len(deselected)} deselected)"
    )
    if not selected:
        raise pytest.UsageError(
            f"--shard {spec} selected no tests. With {len(counts)} files collected "
            "this means the split is wrong, not that there is nothing to run."
        )

    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
