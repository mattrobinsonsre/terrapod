"""Bounded auto-retry for platform-initiated *lifecycle* destroy runs.

`terraform destroy` is meaningfully more flaky than apply: teardown hits
transient dependency-release ordering, eventual consistency, and draining
(an ENI still attached, a security group still referenced, an S3 bucket not
yet empty, an LB still draining). A re-run usually just works, and re-running
is *safe* — destroy is declarative and incremental, so a partial destroy leaves
the state reflecting what remains and the next run targets only that.

This periodic task retries a failed lifecycle destroy a bounded number of times.
It is scoped to **server-owned** lifecycle destroys only:

  * `catalog-lifecycle`        — a catalog instance destroy
  * `autodiscovery-lifecycle`  — an autodiscovery directory destroy

A user's own CLI `terraform destroy` (source `tfe-api`/`vcs`) is **never**
auto-retried — that error belongs to the user to act on.

Safety rests on the existing invariant: the workspace is archived only on a
**successful** destroy apply (`run_service.transition_run`). So a bounded retry
can never lose data or strand the record — it either eventually succeeds and
archives, or exhausts its attempts and stays `errored` for an operator.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import load_runner_config
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import run_service

logger = structlog.get_logger("services.lifecycle_destroy_retry")

# Server-initiated lifecycle teardown sources eligible for silent retry.
LIFECYCLE_DESTROY_SOURCES = ("catalog-lifecycle", "autodiscovery-lifecycle")


async def _latest_run(db: AsyncSession, workspace_id: uuid.UUID) -> Run | None:
    result = await db.execute(
        select(Run).where(Run.workspace_id == workspace_id).order_by(Run.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _consecutive_failed_destroys(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Count the tail of consecutive errored lifecycle-destroy runs (newest
    first) — i.e. the failures in the *current* destroy episode. The walk stops
    at the first run that isn't an errored lifecycle destroy, so a prior episode
    (separated by a successful run, or a re-provision) is not counted."""
    result = await db.execute(
        select(Run).where(Run.workspace_id == workspace_id).order_by(Run.created_at.desc())
    )
    n = 0
    for r in result.scalars().all():
        if r.is_destroy and r.status == "errored" and r.source in LIFECYCLE_DESTROY_SOURCES:
            n += 1
        else:
            break
    return n


async def lifecycle_destroy_retry_cycle() -> None:
    """Queue a fresh destroy run for any lifecycle destroy that errored, up to
    the configured cap. Registered as a periodic scheduler task (multi-replica
    safe — exactly one replica runs each cycle)."""
    cfg = load_runner_config()
    retries = cfg.lifecycle_destroy_retries
    if retries <= 0:
        return
    backoff = timedelta(seconds=max(0, cfg.lifecycle_destroy_retry_backoff_seconds))
    cutoff = datetime.now(UTC) - backoff

    async with get_db_session() as db:
        result = await db.execute(
            select(Run).where(
                Run.is_destroy.is_(True),
                Run.status == "errored",
                Run.source.in_(LIFECYCLE_DESTROY_SOURCES),
                Run.updated_at < cutoff,
            )
        )
        errored = list(result.scalars().all())
        queued = 0
        for run in errored:
            # Only retry if this errored destroy is still the workspace's latest
            # run — a newer run (a retry we already queued, a re-provision, or a
            # successful destroy) supersedes it and means we must not act.
            latest = await _latest_run(db, run.workspace_id)
            if latest is None or latest.id != run.id:
                continue

            attempts = await _consecutive_failed_destroys(db, run.workspace_id)
            if not (1 <= attempts <= retries):
                continue  # exhausted (attempts == retries + 1) → leave for a human

            ws = await db.get(Workspace, run.workspace_id)
            if ws is None:
                continue

            cv = await run_service.get_latest_uploaded_cv(db, ws.id)
            if cv is None:
                # No config to destroy against — a config-less destroy run would
                # just re-error and burn the attempt budget. Leave it for a human.
                logger.warning(
                    "Lifecycle destroy retry skipped: no uploaded configuration version",
                    workspace_id=str(ws.id),
                    source=run.source,
                )
                continue
            retry = await run_service.create_run(
                db,
                workspace=ws,
                message=f"Lifecycle destroy retry (attempt {attempts + 1}/{retries + 1})",
                is_destroy=True,
                auto_apply=True,
                plan_only=False,
                source=run.source,
                configuration_version_id=cv.id,
                created_by="system",
            )
            await run_service.queue_run(db, retry)
            queued += 1
            logger.info(
                "Lifecycle destroy retry queued",
                workspace_id=str(ws.id),
                source=run.source,
                attempt=attempts + 1,
                cap=retries + 1,
            )
        if queued:
            await db.commit()
