"""Run reconciler — periodic task that drives run state transitions.

The API owns all run lifecycle state. The reconciler:
1. Finds runs in planning/applying with job_name set
2. Publishes check_job_status events to the pool's listener SSE channel
3. Reads Job status responses from Redis (posted by listeners)
4. Transitions runs based on Job outcomes
5. Publishes stream_logs events for live log streaming
6. Detects stale runs (>1h with no Job status) and errors them

Registered as a periodic task (10s interval) in app.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Runs stuck longer than this with no Job status are errored
STALE_TIMEOUT = timedelta(hours=1)


async def reconcile_runs() -> None:
    """Drive run state transitions based on Job outcomes.

    This is the main entry point, called every 30s by the scheduler.
    """
    async with get_db_session() as db:
        # Find runs in planning/applying with job_name set
        result = await db.execute(
            select(Run).where(
                Run.status.in_(["planning", "applying"]),
                Run.job_name.isnot(None),
            )
        )
        runs = list(result.scalars().all())

        if not runs:
            return

        for run in runs:
            try:
                await _reconcile_one(db, run)
            except Exception as e:
                logger.error(
                    "Failed to reconcile run",
                    run_id=str(run.id),
                    error=str(e),
                )

        await db.commit()


async def _reconcile_one(db: AsyncSession, run: Run) -> None:
    """Reconcile a single run."""
    from terrapod.redis.client import get_job_status_from_redis, publish_listener_event

    # Publish check_job_status event to the pool's listener channel
    if run.pool_id:
        await publish_listener_event(
            str(run.pool_id),
            {
                "event": "check_job_status",
                "request_id": str(uuid.uuid4()),
                "run_id": str(run.id),
                "job_name": run.job_name,
                "job_namespace": run.job_namespace or "",
            },
        )

        # Also request log streaming for in-progress runs
        await publish_listener_event(
            str(run.pool_id),
            {
                "event": "stream_logs",
                "run_id": str(run.id),
                "job_name": run.job_name,
                "job_namespace": run.job_namespace or "",
                "tail_lines": 500,
            },
        )

    # Check for recently reported status in Redis
    status = await get_job_status_from_redis(str(run.id))
    if status is None:
        # No status yet — check for stale runs
        await _check_stale(db, run)
        return

    if status == "running":
        return  # Still running, no-op

    if status == "succeeded":
        await _handle_succeeded(db, run)
    elif status in ("failed", "deleted"):
        await _handle_failed(db, run, f"Job {status}")


async def _handle_succeeded(db: AsyncSession, run: Run) -> None:
    """Handle a succeeded Job."""
    from terrapod.services import run_service, run_task_service

    if run.status == "planning":
        # Check post_plan task stage
        ts = await run_task_service.create_task_stage(db, run.id, run.workspace_id, "post_plan")
        if ts is not None:
            # Task stage created — resolve it
            stage_status = await run_task_service.resolve_stage(db, ts.id)
            if stage_status not in ("passed", "overridden"):
                if stage_status == "failed":
                    await run_service.transition_run(
                        db, run, "errored", error_message="Post-plan task stage failed"
                    )
                # Stage still pending/running — will be re-checked next cycle
                return

        run = await run_service.transition_run(db, run, "planned")

        # Unlock workspace for plan-only runs
        if run.plan_only:
            ws = await db.get(Workspace, run.workspace_id)
            if ws and ws.locked:
                ws.locked = False
                ws.lock_id = None

        # Auto-apply if configured
        if run.auto_apply and not run.plan_only:
            run = await run_service.transition_run(db, run, "confirmed")

        logger.info("Plan succeeded", run_id=str(run.id))

    elif run.status == "applying":
        run = await run_service.transition_run(db, run, "applied")

        # Unlock workspace
        ws = await db.get(Workspace, run.workspace_id)
        if ws and ws.locked:
            ws.locked = False
            ws.lock_id = None

        logger.info("Apply succeeded", run_id=str(run.id))


async def _handle_failed(db: AsyncSession, run: Run, error_message: str) -> None:
    """Handle a failed or deleted Job."""
    from terrapod.services import run_service

    run = await run_service.transition_run(db, run, "errored", error_message=error_message)

    # Unlock workspace
    ws = await db.get(Workspace, run.workspace_id)
    if ws and ws.locked:
        ws.locked = False
        ws.lock_id = None

    logger.info("Run errored", run_id=str(run.id), reason=error_message)


async def _check_stale(db: AsyncSession, run: Run) -> None:
    """Check if a run is stale (stuck without Job status for too long)."""
    phase_start = run.plan_started_at if run.status == "planning" else run.apply_started_at
    if phase_start is None:
        return

    now = datetime.now(UTC)
    if now - phase_start > STALE_TIMEOUT:
        await _handle_failed(db, run, f"Run stale — no Job status for >{STALE_TIMEOUT}")
        logger.warning("Stale run errored", run_id=str(run.id), status=run.status)
