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
  TERRAPOD_BOOTSTRAP_SAMPLE_WORKSPACE      - If set (truthy), seed a sample workspace + a
                                             completed plan-only run so an evaluation instance
                                             shows a populated UI on first login. Intended for the
                                             eval profile only — NOT for production.
  TERRAPOD_BOOTSTRAP_SAMPLE_WORKSPACE_NAME - Sample workspace name (optional; default "example-vpc")
"""

import asyncio
import hashlib
import logging
import os
import secrets
import sys
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from terrapod.auth.passwords import hash_password
from terrapod.db.models import (
    AgentPool,
    AgentPoolToken,
    ConfigurationVersion,
    PlatformRoleAssignment,
    Run,
    User,
    Workspace,
    now_utc,
)
from terrapod.services import pool_set

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

    # Cloud-IAM DB auth (#573): authenticate the same way as the API when an IAM
    # mode is selected (TP_DB_* env from the Job template). No-op for the default
    # static-password mode. Without this the Job fails on an IAM-only / password-
    # less database (the API and migrations Job already do this).
    from terrapod.db import iam_auth

    iam_auth.register_engine_iam_auth(engine.sync_engine, database_url)

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
                    password_hash=await hash_password(admin_password),
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

            # ── Agent pools (optional) ──────────────────────────────
            pools = _pools_from_environment()
            _reject_duplicate_tokens(pools)
            failures: list[str] = []
            for spec in pools:
                try:
                    await _bootstrap_pool(session, spec)
                except Exception as exc:  # noqa: BLE001 — reported per pool below
                    # Every pool is attempted. Aborting on the first would hide
                    # the other failures, and an operator would then discover
                    # them one install at a time.
                    logger.error("Pool '%s' failed: %s", spec.name, exc)
                    failures.append(f"{spec.name}: {exc}")

            # The first pool is the one a sample workspace attaches to; with a
            # list there has to be an answer and "the first" is the predictable
            # one.
            pool_name = pools[0].name if pools else ""

            # ── Sample workspace + run (optional; eval profile only) ─
            if os.environ.get("TERRAPOD_BOOTSTRAP_SAMPLE_WORKSPACE", "").strip():
                await _bootstrap_sample_workspace(session, pool_name, admin_email)

            if failures:
                # Non-zero so the Job is visibly failed rather than quietly
                # partial. Everything that succeeded is committed and the run is
                # idempotent, so fixing the offending Secret and re-running does
                # only the outstanding work.
                raise SystemExit(
                    "bootstrap: "
                    + str(len(failures))
                    + " of "
                    + str(len(pools))
                    + " pools failed:\n  "
                    + "\n  ".join(failures)
                )

    await engine.dispose()
    logger.info("Bootstrap complete")


@dataclass(frozen=True)
class PoolSpec:
    """One pool to seed, and the token to register for it."""

    name: str
    #: None means "generate one and print it", which is only reachable from the
    #: legacy single-pool path — a list entry always supplies a token, because
    #: printing N generated tokens into a Job's logs is not a way to hand out
    #: credentials.
    token: str | None


def _pools_from_environment() -> list[PoolSpec]:
    """Which pools to seed, from either the indexed vars or the legacy pair.

    `TERRAPOD_BOOTSTRAP_POOL_COUNT` declares how many, then each is
    `TERRAPOD_BOOTSTRAP_POOL_{i}_NAME` and `..._{i}_TOKEN`. The token arrives by
    `secretKeyRef`, so it is never written into the Job spec — the same channel
    the admin password uses.

    The count is not redundant. Scanning until the first gap would silently drop
    every pool after a missing index, and the people this feature is for are
    hand-writing this Job today — so a gap is exactly the mistake to expect. With
    a declared count a gap is a loud failure instead.

    A Secret key that does not resolve never reaches here at all: the kubelet
    fails the container with `CreateContainerConfigError`, which is a clearer
    signal than anything this could report.
    """
    raw_count = os.environ.get("TERRAPOD_BOOTSTRAP_POOL_COUNT", "").strip()
    if raw_count:
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise SystemExit(
                f"TERRAPOD_BOOTSTRAP_POOL_COUNT is not a number: {raw_count!r}"
            ) from exc

        pools: list[PoolSpec] = []
        for index in range(count):
            name = os.environ.get(f"TERRAPOD_BOOTSTRAP_POOL_{index}_NAME", "").strip()
            token = os.environ.get(f"TERRAPOD_BOOTSTRAP_POOL_{index}_TOKEN", "").strip()
            if not name:
                raise SystemExit(
                    f"TERRAPOD_BOOTSTRAP_POOL_COUNT is {count} but "
                    f"TERRAPOD_BOOTSTRAP_POOL_{index}_NAME is missing or empty. "
                    f"Pool indices must run 0..{count - 1} with no gaps."
                )
            if not token:
                raise SystemExit(
                    f"Pool '{name}': TERRAPOD_BOOTSTRAP_POOL_{index}_TOKEN is empty. "
                    f"Check the Secret named in bootstrap.pools has the expected key."
                )
            pools.append(PoolSpec(name=name, token=token))
        return pools

    # Legacy single-pool form. Kept working exactly as before.
    name = os.environ.get("TERRAPOD_BOOTSTRAP_POOL_NAME", "").strip()
    if not name:
        return []
    token = os.environ.get("TERRAPOD_BOOTSTRAP_POOL_TOKEN", "").strip()
    return [PoolSpec(name=name, token=token or None)]


def _reject_duplicate_tokens(pools: list[PoolSpec]) -> None:
    """Refuse two pools sharing one join token.

    `agent_pool_tokens.token_hash` is unique across **all** pools, and the
    registration below skips a hash that already exists. With one pool that is
    harmless idempotency. With a list it is a trap: point two entries at the same
    Secret — a copy-paste away — and the second pool is created, its token is
    skipped as "already exists", and that pool ends up with **no** join token at
    all. Its listener never joins, and the Job reports success.

    So this fails loudly instead. A pool with no token is indistinguishable from
    a broken deployment later, and far harder to diagnose then.
    """
    seen: dict[str, str] = {}
    for spec in pools:
        if spec.token is None:
            continue
        digest = hashlib.sha256(spec.token.encode()).hexdigest()
        if digest in seen:
            raise SystemExit(
                f"Pools '{seen[digest]}' and '{spec.name}' were given the same join token. "
                f"Each pool needs its own: a shared token would leave one of them "
                f"unjoinable while the Job reported success."
            )
        seen[digest] = spec.name


async def _bootstrap_pool(session: AsyncSession, spec: PoolSpec) -> None:
    """Create an agent pool and join token if they don't already exist."""
    pool_name = spec.name
    raw_token = spec.token or ""
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

    # Check if a token with this hash already exists (unique constraint spans all pools)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    result = await session.execute(
        select(AgentPoolToken).where(AgentPoolToken.token_hash == token_hash)
    )
    existing_token = result.scalar_one_or_none()

    if existing_token:
        if existing_token.pool_id != pool.id:
            # The same trap as the pre-flight check, but from a previous run
            # rather than this one's input: the token is already registered
            # against a different pool, so registering it here is impossible and
            # skipping would leave this pool unjoinable.
            raise RuntimeError(
                f"its join token is already registered to a different pool. "
                f"Give pool '{pool_name}' a token of its own."
            )
        logger.info("Join token already exists for pool '%s', skipping", pool_name)
    else:
        token = AgentPoolToken(
            pool_id=pool.id,
            token_hash=token_hash,
            description="Bootstrap token",
            created_by="bootstrap",
        )
        session.add(token)
        logger.info("Created join token for pool '%s'", pool_name)
        if token_generated:
            print(f"Generated join token: {raw_token}")  # noqa: T201 — intentional one-time credential output
            print("IMPORTANT: Save this token now. It will not be shown again.")  # noqa: T201
        else:
            logger.info("Join token created from TERRAPOD_BOOTSTRAP_POOL_TOKEN")


async def _bootstrap_sample_workspace(
    session: AsyncSession, pool_name: str, owner_email: str
) -> None:
    """Seed a sample workspace with one completed plan-only run.

    So an evaluation instance shows a populated, real-looking UI on first
    login instead of an empty workspace list. Idempotent: skips if the sample
    workspace already exists. The run is a terminal plan-only run seeded
    directly as DB rows — no runner execution and no state is involved, so it
    is safe on a stack with no real configuration.
    """
    sample_name = (
        os.environ.get("TERRAPOD_BOOTSTRAP_SAMPLE_WORKSPACE_NAME", "").strip() or "example-vpc"
    )

    result = await session.execute(select(Workspace).where(Workspace.name == sample_name))
    if result.scalar_one_or_none():
        logger.info("Sample workspace '%s' already exists, skipping seed", sample_name)
        return

    # Attach to the bootstrapped pool if one was created (agent execution mode).
    pool_id = None
    if pool_name:
        pool_result = await session.execute(select(AgentPool).where(AgentPool.name == pool_name))
        pool = pool_result.scalar_one_or_none()
        if pool:
            pool_id = pool.id

    workspace = Workspace(
        name=sample_name,
        execution_mode="agent",
        execution_backend="tofu",
        terraform_version="1.12",
        owner_email=owner_email,
        labels={"env": "demo", "team": "platform"},
    )
    if pool_id:
        pool_set.set_workspace_pools(workspace, [pool_id])
    session.add(workspace)
    await session.flush()

    cv = ConfigurationVersion(
        workspace_id=workspace.id,
        source="tfe-api",
        status="uploaded",
        auto_queue_runs=False,
    )
    session.add(cv)
    await session.flush()

    now = now_utc()
    run = Run(
        workspace_id=workspace.id,
        configuration_version_id=cv.id,
        status="planned",
        plan_only=True,
        source="tfe-api",
        created_by=owner_email,
        message="Example plan — welcome to Terrapod",
        execution_backend="tofu",
        terraform_version="1.12",
        has_changes=True,
        resource_additions=3,
        resource_changes=1,
        resource_destructions=0,
        plan_started_at=now,
        plan_finished_at=now,
    )
    session.add(run)
    logger.info("Seeded sample workspace '%s' with one completed plan-only run", sample_name)


if __name__ == "__main__":
    asyncio.run(bootstrap())
