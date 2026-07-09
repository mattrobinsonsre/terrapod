"""Run task service — stage creation, callback tokens, and resolution.

Manages the lifecycle of task stages within a run: creating stage instances
with individual results for each applicable run task, generating HMAC-signed
callback tokens for external services, and resolving stage pass/fail based
on enforcement levels.
"""

import hashlib
import hmac
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import (
    RunTask,
    TaskStage,
    TaskStageResult,
)
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

VALID_STAGES = frozenset({"pre_plan", "post_plan", "pre_apply"})
VALID_ENFORCEMENT_LEVELS = frozenset({"mandatory", "advisory"})
RESULT_TERMINAL_STATES = frozenset({"passed", "failed", "errored", "unreachable"})

# Callback token validity: 1 hour
_CALLBACK_TOKEN_TTL = 3600


def _get_signing_key() -> bytes:
    """Get the stable HMAC signing key for callback tokens.

    Uses the dedicated `token_signing_key` secret when configured, else
    falls back to `sha256(database_url)` (see auth.token_signing).
    """
    from terrapod.auth.token_signing import get_token_signing_key

    return get_token_signing_key()


def generate_callback_token(result_id: uuid.UUID) -> str:
    """Generate an HMAC-SHA256 callback token for a task stage result.

    Format: {result_id}:{timestamp}:{signature}
    The token is valid for _CALLBACK_TOKEN_TTL seconds.
    """
    ts = str(int(time.time()))
    msg = f"{result_id}:{ts}".encode()
    sig = hmac.new(_get_signing_key(), msg, hashlib.sha256).hexdigest()
    return f"{result_id}:{ts}:{sig}"


def verify_callback_token(token: str) -> uuid.UUID | None:
    """Verify a callback token and return the result ID if valid.

    Returns None if the token is invalid, expired, or tampered with.
    """
    parts = token.split(":")
    if len(parts) != 3:
        return None

    result_id_str, ts_str, sig = parts

    try:
        result_id = uuid.UUID(result_id_str)
        ts = int(ts_str)
    except (ValueError, TypeError):
        return None

    # Check expiry
    if time.time() - ts > _CALLBACK_TOKEN_TTL:
        return None

    # Verify HMAC
    msg = f"{result_id}:{ts_str}".encode()
    expected = hmac.new(_get_signing_key(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    return result_id


async def create_task_stage(
    db: AsyncSession,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    stage_name: str,
) -> TaskStage | None:
    """Create a task stage for a run at the given stage boundary.

    Queries enabled RunTasks for the workspace+stage, creates a TaskStage
    with individual TaskStageResults, and enqueues webhook triggers for each.

    Returns None if no applicable run tasks exist (caller should proceed).

    **Idempotent per (run, stage).** A run has exactly one stage per boundary
    (one ``post_plan``, one ``pre_apply``, …). The gate caller
    (``run_service.complete_plan``) is re-driven on every reconciler tick while
    the run sits in ``planning``, so a non-idempotent create would spawn a fresh
    stage — with a fresh, still-``running`` webhook — on every tick, and the
    gate would never resolve to ``passed``. That wedges the run in ``planning``
    forever, accumulating one dead stage per tick (observed live: an advisory
    ``post_plan`` task pointed at an unreachable URL produced dozens of
    duplicate stages and a run that never reached ``planned``). If a stage
    already exists for this run+boundary, return it so the caller re-resolves
    the SAME stage each tick.
    """
    if stage_name not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage_name}")

    # Idempotency: reuse an existing stage for this run+boundary if present.
    # Order by creation (with the id as a stable tiebreak, since created_at
    # can collide within a tick) so re-entry deterministically returns the
    # canonical first-created stage rather than an arbitrary row. NOTE: this
    # is a read-then-insert with no DB-level uniqueness — a concurrent
    # cross-replica insert can still duplicate; #742 adds the constraint.
    existing = await db.execute(
        select(TaskStage)
        .where(TaskStage.run_id == run_id, TaskStage.stage == stage_name)
        .order_by(TaskStage.created_at.asc(), TaskStage.id.asc())
        .limit(1)
    )
    prior = existing.scalars().first()
    if prior is not None:
        return prior

    # Find applicable run tasks
    result = await db.execute(
        select(RunTask).where(
            RunTask.workspace_id == workspace_id,
            RunTask.stage == stage_name,
            RunTask.enabled.is_(True),
        )
    )
    tasks = list(result.scalars().all())

    if not tasks:
        return None

    # Create task stage
    ts = TaskStage(
        run_id=run_id,
        stage=stage_name,
        status="running",
    )
    db.add(ts)
    await db.flush()

    # Create results (webhook delivery is enqueued AFTER commit — see below).
    result_ids: list[uuid.UUID] = []
    for task in tasks:
        tsr = TaskStageResult(
            task_stage_id=ts.id,
            run_task_id=task.id,
            status="pending",
        )
        db.add(tsr)
        await db.flush()

        # Generate callback token
        tsr.callback_token = generate_callback_token(tsr.id)
        await db.flush()
        result_ids.append(tsr.id)

    ts_id = ts.id

    # Commit the stage + result rows BEFORE enqueuing the delivery triggers
    # (#739). The `run_task_call` consumer runs in a *separate* DB session
    # (and possibly on another replica) and looks the TaskStageResult up by
    # id. If we enqueue while the rows are only flushed-not-committed — the
    # caller (`run_service.complete_plan`) commits much later, up the stack —
    # the consumer races ahead, reads "task stage result not found", and
    # silently drops the webhook. The result then sits at `pending` forever,
    # the stage never resolves, and the run wedges in `planning`. Committing
    # here makes the rows visible before any trigger can fire.
    await db.commit()

    from terrapod.services.scheduler import enqueue_trigger

    for tsr_id in result_ids:
        try:
            await enqueue_trigger(
                "run_task_call",
                {"task_stage_result_id": str(tsr_id)},
                dedup_key=f"run_task:{tsr_id}",
                dedup_ttl=300,
            )
        except Exception as e:
            logger.warning("Failed to enqueue run task call", error=str(e))

    logger.info(
        "Task stage created",
        task_stage_id=str(ts_id),
        run_id=str(run_id),
        stage=stage_name,
        task_count=len(tasks),
    )

    # Return the committed stage. (Sessions use expire_on_commit=False, so the
    # instance is still live after the commit above — this get() just resolves
    # it from the identity map for the caller, which immediately reads ts.id /
    # resolves the stage in complete_plan.)
    return await db.get(TaskStage, ts_id)


async def get_task_stage(db: AsyncSession, ts_id: uuid.UUID) -> TaskStage | None:
    """Get a task stage by ID with results loaded."""
    result = await db.execute(
        select(TaskStage)
        .options(selectinload(TaskStage.results).selectinload(TaskStageResult.run_task))
        .where(TaskStage.id == ts_id)
    )
    return result.scalar_one_or_none()


async def get_task_stage_result(db: AsyncSession, tsr_id: uuid.UUID) -> TaskStageResult | None:
    """Get a task stage result by ID."""
    return await db.get(TaskStageResult, tsr_id)


async def resolve_stage(db: AsyncSession, task_stage_id: uuid.UUID) -> str:
    """Check all results for a task stage and resolve its status.

    Resolution logic:
    - If any mandatory task failed → stage fails
    - If any task is still pending/running → stage stays running
    - If all tasks passed (or advisory failures only) → stage passes

    Returns the resolved stage status.
    """
    ts = await get_task_stage(db, task_stage_id)
    if ts is None:
        return "errored"

    if ts.status in ("passed", "failed", "errored", "canceled", "overridden"):
        return ts.status

    has_pending = False
    has_mandatory_failure = False

    for r in ts.results:
        if r.status in ("pending", "running"):
            has_pending = True
        elif r.status == "failed":
            # Check enforcement level
            rt = r.run_task
            if rt and rt.enforcement_level == "mandatory":
                has_mandatory_failure = True
        elif r.status in ("errored", "unreachable"):
            # Treat errored/unreachable as failure for mandatory tasks
            rt = r.run_task
            if rt and rt.enforcement_level == "mandatory":
                has_mandatory_failure = True

    if has_pending:
        return ts.status  # Still running

    # All results are terminal
    if has_mandatory_failure:
        ts.status = "failed"
    else:
        ts.status = "passed"

    await db.flush()

    logger.info(
        "Task stage resolved",
        task_stage_id=str(task_stage_id),
        status=ts.status,
    )

    return ts.status


async def override_stage(db: AsyncSession, task_stage_id: uuid.UUID) -> TaskStage | None:
    """Override a failed task stage, allowing the run to proceed.

    Only applicable to stages in 'failed' status.
    """
    ts = await get_task_stage(db, task_stage_id)
    if ts is None:
        return None

    if ts.status != "failed":
        raise ValueError(f"Can only override stages in 'failed' status, got '{ts.status}'")

    ts.status = "overridden"
    await db.flush()

    logger.info("Task stage overridden", task_stage_id=str(task_stage_id))

    return ts


async def list_run_task_stages(db: AsyncSession, run_id: uuid.UUID) -> list[TaskStage]:
    """List all task stages for a run."""
    result = await db.execute(
        select(TaskStage)
        .options(selectinload(TaskStage.results).selectinload(TaskStageResult.run_task))
        .where(TaskStage.run_id == run_id)
        .order_by(TaskStage.created_at.asc())
    )
    return list(result.scalars().all())
