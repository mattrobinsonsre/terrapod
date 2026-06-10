"""Run reconciler — periodic task that drives run state transitions.

The API owns all run lifecycle state. The reconciler:
1. Finds runs in planning/applying with job_name set
2. Publishes check_job_status events to the pool's listener SSE channel
3. Reads Job status responses from Redis (posted by listeners)
4. Transitions runs based on Job outcomes
5. Publishes stream_logs events for live log streaming
6. Detects stale runs (>1h with no Job status) and errors them

Registered as a periodic task (2s interval) in app.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import load_runner_config
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Default stale timeout (1 hour) — overridden by RunnerConfig.stale_timeout_seconds
DEFAULT_STALE_TIMEOUT_SECONDS = 3600


async def _persist_live_log_if_missing(run: Run, phase: str) -> None:
    """Promote live-streamed log from Redis to object storage if the runner
    didn't upload its own final log.  This prevents log loss when a Job fails
    before the entrypoint's log upload step (or when the upload itself fails).
    """
    from terrapod.redis.client import LOG_STREAM_PREFIX, get_redis_client
    from terrapod.storage import get_storage
    from terrapod.storage.keys import apply_log_key, plan_log_key
    from terrapod.storage.protocol import ObjectNotFoundError

    storage = get_storage()
    ws_id = str(run.workspace_id)
    run_id = str(run.id)
    log_key = plan_log_key(ws_id, run_id) if phase == "plan" else apply_log_key(ws_id, run_id)

    # Check if runner already uploaded the final log
    try:
        await storage.get(log_key)
        return  # Already in storage — nothing to do
    except ObjectNotFoundError:
        pass

    # Promote Redis live log to storage
    try:
        redis = get_redis_client()
        live_data = await redis.get(f"{LOG_STREAM_PREFIX}{run.id}:{phase}")
        if live_data:
            if isinstance(live_data, str):
                live_data = live_data.encode()
            await storage.put(log_key, live_data)
            logger.info(
                "Persisted live log from Redis to storage",
                run_id=run_id,
                phase=phase,
            )
    except Exception as e:
        logger.warning("Failed to persist live log", run_id=run_id, error=str(e))


async def reconcile_runs() -> None:
    """Drive run state transitions based on Job outcomes.

    This is the main entry point, called every 2s by the scheduler.

    Picks up two cohorts:
    1. planning/applying with job_name set — drive on Job status reports
    2. planning/applying with NO job_name set — the listener claimed the run
       but never reported job-launched (auth failure, K8s outage at create
       time, listener died mid-launch). Without picking these up they sit
       indefinitely. `_check_stale` applies a shorter `launch_timeout` to
       this cohort so failures surface in minutes, not hours.
    """
    async with get_db_session() as db:
        # `canceling` joins planning/applying here: it's the intermediate
        # state entered when a user cancels an in-flight apply. The
        # reconciler waits for the listener's Job-status report (the
        # listener deletes the K8s Job on receiving cancel_job; the next
        # check_job_status reports "deleted") and then resolves the
        # canceling run to applied/canceled/errored based on whether a
        # state-version was actually uploaded.
        result = await db.execute(
            select(Run).where(Run.status.in_(["planning", "applying", "canceling"]))
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

    # `canceling` was entered from `applying`, so the Job that may need
    # resolution is the apply Job — `phase` follows the same mapping.
    phase = "plan" if run.status == "planning" else "apply"

    # If no Job has been launched yet (listener never POSTed job-launched),
    # there's nothing for listeners to query — skip the SSE round-trip and
    # rely on the launch_timeout in _check_stale.
    if run.job_name is None:
        await _check_stale(db, run)
        return

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
                "phase": phase,
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
                "phase": phase,
            },
        )

    # Check for recently reported status in Redis (phase-keyed to prevent
    # stale plan "succeeded" from causing premature apply transitions)
    status = await get_job_status_from_redis(str(run.id), phase)
    if status is None:
        # No status yet — check for stale runs
        await _check_stale(db, run)
        return

    if status == "running":
        return  # Still running, no-op

    # `canceling` resolution is keyed on the Job-completion signal AND
    # whether a state-version was uploaded by this run; both branches
    # of the normal-path handlers (succeeded → applied, failed →
    # errored) would skip the StateVersion check and the
    # state_diverged signalling.
    if run.status == "canceling":
        from terrapod.services import run_service

        await _persist_live_log_if_missing(run, phase)
        await run_service.resolve_canceling_run(db, run, job_status=status)
        return

    if status == "succeeded":
        await _handle_succeeded(db, run)
    elif status in ("failed", "deleted"):
        # Typed OOM message when the listener captured the K8s
        # terminated reason (#430). runner_exit_status is set by
        # report_job_status; we just read it here.
        await _handle_failed(db, run, _build_failure_message(run, status))


def _build_failure_message(run: Run, status: str) -> str:
    """Render a typed error message from runner_exit_status when set.

    When the listener captured a K8s container-terminated `reason`, the
    job-status endpoint sets `run.runner_exit_status` to one of:
        "oom"    — reason == "OOMKilled"
        "killed" — exit 137 with no explicit reason (could be OOM,
                   could be eviction; we can't tell after pod GC)
        "error"  — non-zero exit, not 137
        "clean"  — exit 0 (shouldn't reach here since status==failed)
    Returns a human-readable message naming the cause and pointing at
    the actionable knob (resource_memory bump for OOM).
    """
    if run.runner_exit_status == "oom":
        peak_h = _human_bytes(run.peak_memory_bytes) if run.peak_memory_bytes else None
        if peak_h:
            return (
                f"Runner OOM-killed (peak memory {peak_h}). "
                f"Workspace resource_memory is {run.resource_memory}; "
                f"limit is 2× that. Increase resource_memory + retry."
            )
        return (
            f"Runner OOM-killed (workspace resource_memory={run.resource_memory}, "
            f"limit is 2× that). Increase resource_memory + retry."
        )
    if run.runner_exit_status == "killed":
        return (
            "Runner killed (SIGKILL, exit 137) without an explicit K8s reason. "
            "Most likely OOM — check the workspace's resource_memory; could "
            "also be a node-level eviction."
        )
    if run.runner_exit_status == "error" and run.runner_exit_code is not None:
        return f"Runner exited with code {run.runner_exit_code}"
    # Fall back to the generic message — matches pre-#430 behaviour.
    return f"Job {status}"


def _human_bytes(n: int) -> str:
    """Render a byte count as a short human string. Matches K8s convention
    (Gi/Mi/Ki for binary units; matches values shown in resource_memory)."""
    for unit, scale in (("Gi", 1 << 30), ("Mi", 1 << 20), ("Ki", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.2f} {unit}"
    return f"{n} B"


async def _handle_succeeded(db: AsyncSession, run: Run) -> None:
    """Handle a succeeded Job.

    Thin wrapper around the shared `run_service.complete_plan` /
    `complete_apply` helpers. Both helpers are idempotent — if the runner's
    direct POST (`/plan-result` / `/apply-result`) already drove the
    transition, this is a no-op.
    """
    from terrapod.services import run_service

    phase = "plan" if run.status == "planning" else "apply"
    await _persist_live_log_if_missing(run, phase)

    if run.status == "planning":
        await run_service.complete_plan(db, run)
    elif run.status == "applying":
        await run_service.complete_apply(db, run)


async def _handle_failed(db: AsyncSession, run: Run, error_message: str) -> None:
    """Handle a failed or deleted Job."""
    from terrapod.services import run_service

    phase = "plan" if run.status == "planning" else "apply"
    await _persist_live_log_if_missing(run, phase)

    run = await run_service.transition_run(db, run, "errored", error_message=error_message)

    # Unlock workspace
    ws = await db.get(Workspace, run.workspace_id)
    if ws and ws.locked:
        ws.locked = False
        ws.lock_id = None

    logger.info("Run errored", run_id=str(run.id), reason=error_message)


async def _check_stale(db: AsyncSession, run: Run) -> None:
    """Check if a run is stale (stuck without Job status / Job launch for too long).

    Two timeouts apply, depending on what stage the run is stuck at:
    - **launch_timeout** (default 5 min) — run has no `job_name` yet. Listener
      claimed it but never POSTed `job-launched`. Catches /runner-token auth
      failures, K8s outages at create time, listener crashes mid-launch.
    - **stale_timeout** (default 1 h) — Job exists but reports no status.
      Catches dead listeners that stopped reporting, lost SSE channels,
      pods that never produced output. Looser default because a long
      `terraform plan` legitimately produces no extra status.
    """
    phase_start = run.plan_started_at if run.status == "planning" else run.apply_started_at
    if phase_start is None:
        return

    cfg = load_runner_config()
    now = datetime.now(UTC)

    # Drift-detection runs get a separate, tighter cap regardless of whether
    # they're actively reporting status. Background-priority + plan-only +
    # auto-scheduled = an upper bound that says "if drift hasn't decided in
    # 30 min, we want the slot back". Without it, a single workspace with
    # a multi-hour github-provider refresh (terrapod-config: ~30 rate-limited
    # API reads) blocks drift on that workspace indefinitely and the operator
    # never sees a clear "this drift run timed out" signal. Set to 0 to
    # disable. Checked BEFORE the generic stale/launch timeouts so a
    # drift run that's stale AND over-cap is reported as "drift cap exceeded"
    # (the more actionable failure mode).
    if run.is_drift_detection and run.status == "planning" and cfg.drift_max_duration_seconds > 0:
        cap = timedelta(seconds=cfg.drift_max_duration_seconds)
        if now - phase_start > cap:
            await _handle_failed(
                db, run, f"Drift run exceeded max duration ({cfg.drift_max_duration_seconds}s)"
            )
            logger.warning(
                "Drift run errored: max duration exceeded",
                run_id=str(run.id),
                duration_seconds=int((now - phase_start).total_seconds()),
                cap_seconds=cfg.drift_max_duration_seconds,
            )
            return

    if run.job_name is None:
        timeout = timedelta(seconds=cfg.launch_timeout_seconds)
        message_prefix = "Run stuck pre-launch"
    else:
        timeout = timedelta(seconds=cfg.stale_timeout_seconds)
        message_prefix = "Run stale"

    if now - phase_start > timeout:
        await _handle_failed(db, run, f"{message_prefix} — no progress for >{timeout}")
        if run.job_name is None:
            from terrapod.api.metrics import LISTENER_PRELAUNCH_TIMEOUTS

            LISTENER_PRELAUNCH_TIMEOUTS.inc()
        logger.warning(
            "Stale run errored",
            run_id=str(run.id),
            status=run.status,
            had_job=run.job_name is not None,
        )
