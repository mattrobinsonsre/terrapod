"""Onboarding discovery service (#824 P2).

Orchestrates the D1 *schema* phase of resource onboarding **in the API** (no
runner Job, for responsiveness): resolve the workspace's engine binary, run a
credential-less ``tofu init`` + ``terrapod-query schema`` in an ephemeral
directory on the CSP-attached PVC, and cache the resulting "discovery surface"
(the strong-signal data sources + their inputs) per engine+version+provider.

Why this can live in the API and stay responsive:
- Schema introspection is **credential-less** — ``tofu providers schema`` only
  reads the provider plugin, it never touches the cloud. So no pool creds and no
  Job are needed (those are for D2/D3, which run on the runner).
- A provider schema is **stable for a given provider version**, so the slow part
  (downloading the provider once, ~hundreds of MB) happens at most once per
  (engine, version, provider) across the whole deployment and is then served
  from the Redis surface cache. Every subsequent discovery is instant.

Hard rules honoured here:
- **Rule 13 (no sync work in async):** every subprocess / blocking-IO step runs
  under ``asyncio.to_thread`` — the event loop is never blocked.
- **Rule 14 (substantial tempfiles on the PVC):** the engine binary download and
  the ``tofu init`` working directory (which holds the multi-hundred-MB provider
  plugin) are created under ``settings.vcs.tmpdir`` (the ephemeral PVC), never
  ``/tmp`` (RAM-backed).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import stat
import subprocess  # noqa: S404 — used only via to_thread with fixed argv, no shell
import tempfile
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import OnboardingSession, Workspace
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

# Path of the terrapod-query binary baked into the API image (docker/Dockerfile.api).
QUERY_BIN = "/usr/local/bin/terrapod-query"

# A provider schema is version-stable, so the surface can be cached for a long
# time; a version bump changes the cache key, so stale entries simply expire.
_SURFACE_TTL_SECONDS = 30 * 24 * 3600

# Bound the credential-less init+schema so a wedged download can't pin a thread.
_DISCOVERY_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Redis surface cache (pure — keyed by engine + version + provider)
# ---------------------------------------------------------------------------
def _surface_cache_key(engine: str, engine_version: str, provider: str) -> str:
    return f"tp:onboard:surface:{engine}:{engine_version}:{provider}"


async def get_cached_surface(
    engine: str, engine_version: str, provider: str
) -> dict[str, Any] | None:
    """Return the cached discovery surface for (engine, version, provider), or None."""
    try:
        raw = await get_redis_client().get(_surface_cache_key(engine, engine_version, provider))
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        logger.debug("onboarding_surface_cache_get_failed", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def set_cached_surface(
    engine: str, engine_version: str, provider: str, surface: dict[str, Any]
) -> None:
    try:
        await get_redis_client().set(
            _surface_cache_key(engine, engine_version, provider),
            json.dumps(surface),
            ex=_SURFACE_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("onboarding_surface_cache_set_failed", error=str(exc))


async def get_session_surface(session: OnboardingSession) -> dict[str, Any] | None:
    """A session's discovery surface from the Redis cache, or None if expired.

    The surface is Redis-only and time-limited, so a ``schema_ready`` session
    whose cache entry has since expired returns None here — the caller re-runs
    discovery (cheap: a warm cache for that engine+version+provider repopulates
    it, and a cold one is the one-time provider download).
    """
    if not (session.engine and session.engine_version and session.provider):
        return None
    return await get_cached_surface(session.engine, session.engine_version, session.provider)


# A terraform provider short-name. Validated because it is interpolated into
# generated HCL (`provider "<name>" {}`) — restrict to the identifier charset so
# a session's provider field can never inject arbitrary config.
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def is_valid_provider(provider: str) -> bool:
    return bool(_PROVIDER_RE.match(provider))


# ---------------------------------------------------------------------------
# Session CRUD (pure DB)
# ---------------------------------------------------------------------------
async def create_session(
    db: AsyncSession, *, workspace_id: uuid.UUID, provider: str, created_by: str
) -> OnboardingSession:
    """Create a pending onboarding session for a workspace."""
    session = OnboardingSession(
        workspace_id=workspace_id,
        provider=provider.strip(),
        created_by=created_by,
        status="pending",
    )
    db.add(session)
    await db.flush()
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> OnboardingSession | None:
    return await db.get(OnboardingSession, session_id)


async def list_sessions(db: AsyncSession, workspace_id: uuid.UUID) -> list[OnboardingSession]:
    result = await db.execute(
        select(OnboardingSession)
        .where(OnboardingSession.workspace_id == workspace_id)
        .order_by(OnboardingSession.created_at.desc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# D1 discovery orchestration
# ---------------------------------------------------------------------------
def _local_platform() -> tuple[str, str]:
    """(os, arch) of this API pod, in terraform-release naming."""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return "linux", arch


def _resolve_tmpdir() -> str | None:
    """The CSP-attached ephemeral PVC dir (Rule 14), or None for the system default."""
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


def _provider_config_hcl(provider: str) -> str:
    """Minimal HCL to make ``tofu init`` install the provider for schema reads.

    Only ``required_providers`` is needed for ``providers schema`` — the empty
    provider block keeps tofu happy without any credentials or region.
    """
    # Hosted registry source: a bare provider name resolves to hashicorp/<name>.
    return (
        "terraform {\n"
        "  required_providers {\n"
        f'    {provider} = {{ source = "{provider}" }}\n'
        "  }\n"
        "}\n"
        f'provider "{provider}" {{}}\n'
    )


async def _download_engine_binary(db: AsyncSession, engine: str, version: str) -> str:
    """Resolve + fetch the tofu/terraform binary to the PVC, return its path.

    The API image bakes no engine binary (removed in #824 P1); we pull the
    workspace's exact version through the binary cache, same as the runner.
    """
    from terrapod.services import binary_cache_service
    from terrapod.storage import get_storage

    os_, arch = _local_platform()
    resolved = await binary_cache_service.resolve_version(engine, version)
    url = await binary_cache_service.get_or_cache_binary(
        db, get_storage(), engine, resolved, os_, arch
    )

    tmpdir = _resolve_tmpdir()
    dest_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="onb-bin-", dir=tmpdir)
    dest = os.path.join(dest_dir, engine)

    # Stream the (zip) release to the PVC, extract the single binary. Both the
    # download and the unzip are blocking → threaded.
    async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
        zip_path = os.path.join(dest_dir, f"{engine}.zip")
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            f = await asyncio.to_thread(open, zip_path, "wb")
            try:
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    await asyncio.to_thread(f.write, chunk)
            finally:
                await asyncio.to_thread(f.close)
    await asyncio.to_thread(_unzip_engine, zip_path, dest, engine)
    return dest


def _unzip_engine(zip_path: str, dest: str, engine: str) -> None:
    """Extract the engine binary from a release zip to ``dest`` and mark it +x."""
    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        member = next((n for n in zf.namelist() if os.path.basename(n) == engine), None)
        if member is None:
            raise RuntimeError(f"{engine} binary not found in release archive")
        with zf.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _discover_surface_blocking(engine_bin: str, provider: str, workdir: str) -> dict[str, Any]:
    """Run ``tofu init`` + ``terrapod-query schema`` in ``workdir`` (BLOCKING).

    Isolated so callers wrap it in ``asyncio.to_thread`` and tests can mock it.
    Credential-less and read-only: schema introspection never touches the cloud.
    """
    with open(os.path.join(workdir, "providers.tf"), "w") as f:
        f.write(_provider_config_hcl(provider))

    env = {**os.environ, "TF_IN_AUTOMATION": "1"}
    init = subprocess.run(  # noqa: S603 — fixed argv, no shell, trusted binary
        [engine_bin, "init", "-no-color", "-input=false"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=_DISCOVERY_TIMEOUT_SECONDS,
    )
    if init.returncode != 0:
        raise RuntimeError(f"{os.path.basename(engine_bin)} init failed: {init.stderr[-2000:]}")

    query = subprocess.run(  # noqa: S603
        [QUERY_BIN, "schema", "--dir", workdir, "--tofu", engine_bin],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=_DISCOVERY_TIMEOUT_SECONDS,
    )
    if query.returncode != 0:
        raise RuntimeError(f"terrapod-query schema failed: {query.stderr[-2000:]}")
    return json.loads(query.stdout)


async def run_schema_discovery(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Populate a session's discovery surface (D1), using the cache when warm.

    Idempotent and safe to retry: on any failure the session is marked errored
    with the detail, and the workspace is never touched.
    """
    session = await db.get(OnboardingSession, session_id)
    if session is None:
        return
    workspace = await db.get(Workspace, session.workspace_id)
    if workspace is None:
        session.status = "errored"
        session.error = "workspace no longer exists"
        await db.flush()
        return

    engine = (
        workspace.execution_backend
        if workspace.execution_backend in ("tofu", "terraform")
        else "tofu"
    )
    version = workspace.terraform_version or "latest"
    provider = session.provider

    # Pin the cache key on the session (small scalars) so the surface can be
    # re-fetched from Redis on read even if the workspace's engine/version later
    # changes. The surface itself is never written to the session row.
    session.engine = engine
    session.engine_version = version

    cached = await get_cached_surface(engine, version, provider)
    if cached is not None:
        session.status = "schema_ready"
        session.error = ""
        await db.flush()
        return

    workdir: str | None = None
    try:
        engine_bin = await _download_engine_binary(db, engine, version)
        tmpdir = _resolve_tmpdir()
        workdir = await asyncio.to_thread(tempfile.mkdtemp, prefix="onb-d1-", dir=tmpdir)
        surface = await asyncio.wait_for(
            asyncio.to_thread(_discover_surface_blocking, engine_bin, provider, workdir),
            timeout=_DISCOVERY_TIMEOUT_SECONDS + 30,
        )
        # Surface goes to the time-limited Redis cache ONLY — never the DB row.
        await set_cached_surface(engine, version, provider, surface)
        session.status = "schema_ready"
        session.error = ""
        await db.flush()
        logger.info(
            "onboarding_schema_ready",
            session_id=str(session_id),
            provider=provider,
            data_sources=surface.get("count"),
        )
    except Exception as exc:  # noqa: BLE001 — record any failure on the session
        session.status = "errored"
        session.error = str(exc)[:2000]
        await db.flush()
        logger.warning(
            "onboarding_schema_discovery_failed", session_id=str(session_id), error=str(exc)
        )
    finally:
        if workdir:
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)


async def handle_schema_discover_trigger(payload: dict) -> None:
    """Scheduler trigger handler: run D1 discovery off the request thread.

    The API POST enqueues this so the (potentially slow, first-time) provider
    download never blocks the HTTP request. Multi-replica-safe — any replica
    dequeues and runs it; the surface cache makes re-runs cheap.
    """
    from terrapod.db.session import get_db_session

    session_id = payload.get("session_id", "")
    if not session_id:
        logger.warning("onboarding_schema_discover_missing_session_id", payload=payload)
        return
    async with get_db_session() as db:
        await run_schema_discovery(db, uuid.UUID(session_id))
        await db.commit()
