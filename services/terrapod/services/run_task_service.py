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


_signing_key: bytes | None = None


def _get_signing_key() -> bytes:
    """Get a stable signing key for callback tokens.

    Derives from the database URL as a stable per-deployment secret.
    """
    global _signing_key  # noqa: PLW0603
    if _signing_key is not None:
        return _signing_key
    from terrapod.config import settings

    _signing_key = hashlib.sha256(str(settings.database_url).encode()).digest()
    return _signing_key


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
    """
    if stage_name not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage_name}")

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

    # Create results and enqueue webhook calls
    from terrapod.services.scheduler import enqueue_trigger

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

        # Enqueue webhook delivery
        try:
            await enqueue_trigger(
                "run_task_call",
                {"task_stage_result_id": str(tsr.id)},
                dedup_key=f"run_task:{tsr.id}",
                dedup_ttl=300,
            )
        except Exception as e:
            logger.warning("Failed to enqueue run task call", error=str(e))

    logger.info(
        "Task stage created",
        task_stage_id=str(ts.id),
        run_id=str(run_id),
        stage=stage_name,
        task_count=len(tasks),
    )

    return ts


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
