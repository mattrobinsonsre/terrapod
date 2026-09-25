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
from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
# Every status a task stage is stored with.
STAGE_STATUSES = frozenset(
    {"pending", "running", "passed", "failed", "errored", "canceled", "overridden"}
)

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


async def _existing_stage(db: AsyncSession, run_id: uuid.UUID, stage_name: str) -> TaskStage | None:
    """Return the canonical (first-created) stage for a run+boundary, if any."""
    res = await db.execute(
        select(TaskStage)
        .where(TaskStage.run_id == run_id, TaskStage.stage == stage_name)
        .order_by(TaskStage.created_at.asc(), TaskStage.id.asc())
        .limit(1)
    )
    return res.scalars().first()


async def _enqueue_deliveries(result_ids: list[uuid.UUID]) -> None:
    """Enqueue the webhook call for each result. Call only after committing them."""
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


async def reserve_task_stage(
    db: AsyncSession,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    stage_name: str,
) -> TaskStage | None:
    """Create a run's task stage when the run is created, calling nothing yet (#1704).

    Terraform Enterprise creates a run's task stages with the run, and the
    `tofu`/`terraform` CLI reads them once, straight after creating the run: a
    stage that first appears when the plan finishes is never waited on, never
    shown, and never offered for override. So in the Terraform Enterprise
    vocabulary the stage is reserved here, `pending`, with a `pending` result
    per enabled task. `create_task_stage` starts it when its boundary is
    reached; `cancel_pending_stages` closes it if the run ends first.

    Flushes only: the caller commits with the run. Returns None when the
    workspace has no enabled task at this boundary.
    """
    if stage_name not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage_name}")
    prior = await _existing_stage(db, run_id, stage_name)
    if prior is not None:
        return prior

    tasks = list(
        (
            await db.execute(
                select(RunTask).where(
                    RunTask.workspace_id == workspace_id,
                    RunTask.stage == stage_name,
                    RunTask.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not tasks:
        return None

    ts = TaskStage(run_id=run_id, stage=stage_name, status="pending")
    db.add(ts)
    await db.flush()
    for task in tasks:
        tsr = TaskStageResult(task_stage_id=ts.id, run_task_id=task.id, status="pending")
        db.add(tsr)
        await db.flush()
        tsr.callback_token = generate_callback_token(tsr.id)
    await db.flush()
    logger.info(
        "Task stage reserved", task_stage_id=str(ts.id), run_id=str(run_id), stage=stage_name
    )
    return ts


async def _start_reserved_stage(db: AsyncSession, ts: TaskStage) -> None:
    """Start a stage `reserve_task_stage` created: mark it running, call its tasks.

    Two replicas can start the same stage; the per-result dedup key on the
    delivery trigger keeps each task to one call.
    """
    results = list(
        (
            await db.execute(
                select(TaskStageResult.id).where(
                    TaskStageResult.task_stage_id == ts.id,
                    TaskStageResult.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    ts.status = "running"
    # Commit before enqueueing, for the reason `create_task_stage` gives (#739).
    await db.commit()
    await _enqueue_deliveries(results)
    logger.info("Task stage started", task_stage_id=str(ts.id), task_count=len(results))


async def cancel_pending_stages(db: AsyncSession, run_id: uuid.UUID) -> int:
    """Cancel a run's reserved stages that never started, because the run ended.

    Without this a plan that errors leaves its post-plan stage `pending`
    forever, and the CLI polls a pending stage indefinitely. `canceled` is the
    status Terraform Enterprise gives such a stage. Returns how many changed;
    the caller commits.
    """
    stages = list(
        (
            await db.execute(
                select(TaskStage).where(TaskStage.run_id == run_id, TaskStage.status == "pending")
            )
        )
        .scalars()
        .all()
    )
    for ts in stages:
        ts.status = "canceled"
    if stages:
        await db.flush()
    return len(stages)


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
    # canonical first-created stage. This read-then-insert is backed by a
    # UniqueConstraint(run_id, stage) (#742), so a concurrent cross-replica
    # insert that slips past this check is caught below via IntegrityError.
    prior = await _existing_stage(db, run_id, stage_name)
    if prior is not None:
        if prior.status == "pending":
            await _start_reserved_stage(db, prior)
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

    # Create task stage + its results, then commit. The UniqueConstraint on
    # (run_id, stage) makes this race-safe: if a concurrent tick on another
    # replica committed the same stage between our existence check above and
    # this commit, the commit raises IntegrityError — we roll back and return
    # the winner (it enqueued its own delivery triggers, so we don't re-enqueue).
    try:
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
    except IntegrityError:
        # A concurrent creator won the (run_id, stage) race. Return their stage.
        await db.rollback()
        winner = await _existing_stage(db, run_id, stage_name)
        if winner is not None:
            return winner
        raise

    await _enqueue_deliveries(result_ids)

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


# Gate verdicts, shared by all three boundaries. `GATE_PASSED` covers the
# no-applicable-tasks case as well as a stage that resolved clean: a boundary
# nobody configured must not hold anything.
GATE_PASSED = "passed"
GATE_RUNNING = "running"
GATE_FAILED = "failed"

# Boundaries whose failed stage an admin may wave through.
#
# ONLY `post_plan`. The other two are deliberately final (#1837): the platform
# already opens itself to human intervention between plan and apply — a run
# sits `planned` for a person to confirm or discard — so a `pre_apply` verdict
# is a go/no-go taken *before* any infrastructure moves, and once an apply has
# started there is nothing an override could usefully release. The escape from
# a failed pre-apply gate is the ordinary one: discard, fix the cause, re-plan.
# See docs/run-tasks.md.
OVERRIDABLE_STAGES = frozenset({"post_plan"})


async def evaluate_gate(db: AsyncSession, run, stage_name: str) -> str:
    """Open (or re-read) this run's stage at ``stage_name`` and judge it.

    The single predicate behind all three boundaries, so their semantics
    cannot drift apart. Safe to call repeatedly: `create_task_stage` is
    idempotent per (run, stage), so re-entry re-resolves the SAME stage rather
    than spawning a fresh webhook each time — which is what lets every caller
    here be a re-drive rather than a one-shot.

    Returns `GATE_PASSED` (proceed), `GATE_RUNNING` (hold, ask again later) or
    `GATE_FAILED` (a mandatory task said no).
    """
    ts = await create_task_stage(db, run.id, run.workspace_id, stage_name)
    if ts is None:
        return GATE_PASSED
    status = await resolve_stage(db, ts.id)
    if status in ("passed", "overridden"):
        return GATE_PASSED
    if status == "failed":
        return GATE_FAILED
    return GATE_RUNNING


async def failed_task_summary(db: AsyncSession, run_id: uuid.UUID, stage_name: str) -> str:
    """Name the mandatory tasks that failed, for the operator-facing message.

    A bare "a run task failed" sends someone to the API to find out which one;
    the stage is right here, so say it.
    """
    stage = await _existing_stage(db, run_id, stage_name)
    if stage is None:
        return ""
    stage = await get_task_stage(db, stage.id)
    if stage is None:
        return ""
    names = [
        r.run_task.name
        for r in stage.results
        if r.status in ("failed", "errored", "unreachable")
        and r.run_task is not None
        and r.run_task.enforcement_level == "mandatory"
    ]
    return ", ".join(sorted(names))


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


def expire_unreachable_results(ts: TaskStage) -> int:
    """Fail results whose callback token has expired. Returns how many.

    **The wedge this closes.** `run_task_dispatcher` leaves a result at
    ``running`` when the webhook returns 2xx, and `resolve_stage` holds the
    whole stage ``running`` while any result is non-terminal. Nothing else ages
    a result out, so an external service that accepts the webhook and then
    never calls back holds the run **forever** — and the enforcement level is
    irrelevant, because an *advisory* task that never answers blocks exactly as
    hard as a mandatory one. The stage simply never resolves.

    That was survivable while `post_plan` was the only boundary: a held run sat
    in ``planning``, where the reconciler's staleness backstop could still
    reach a plan-only run. `pre_plan` (held ``queued``) and `pre_apply` (held
    ``planned``) have no such backstop at all, so #1837 turned a bounded
    annoyance into an unbounded one. Hence fixing it here, for all three
    boundaries at once, rather than at either new gate.

    **The deadline is derived, not invented.** A callback is authenticated by
    `verify_callback_token`, which refuses anything older than
    ``_CALLBACK_TOKEN_TTL``. So once that elapses the external service *cannot*
    report back — its callback would 401. The result is not slow, it is
    unreachable, and saying so is a statement of fact rather than a policy
    choice. That is also why this needs no config knob: a tunable timeout would
    only let an operator pick a number that disagrees with the token.
    """
    from terrapod.db.models import now_utc

    cutoff = now_utc() - timedelta(seconds=_CALLBACK_TOKEN_TTL)
    expired = 0
    for r in ts.results:
        if r.status not in ("pending", "running"):
            continue
        created = r.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created > cutoff:
            continue
        r.status = "errored"
        r.message = (
            "No callback was received before the callback token expired "
            f"({_CALLBACK_TOKEN_TTL // 60} minutes), so this result can no "
            "longer be reported. Check the external service."
        )
        r.finished_at = now_utc()
        expired += 1
    return expired


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

    expire_unreachable_results(ts)

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
    """Override a failed `post_plan` task stage, allowing the run to proceed.

    Only applicable to stages in 'failed' status, and only at a boundary in
    `OVERRIDABLE_STAGES`.

    The boundary check is enforced HERE rather than at the router, because
    until #1837 `post_plan` was the only stage anything created — so this
    function never needed to ask, and the endpoint would have started
    accepting `pre_plan` and `pre_apply` stages the moment they began to
    exist. That would have shipped an override path for both without anyone
    deciding to add one, which is the opposite of the intent.

    Overridability is a property the operator chooses by picking a boundary:
    a gate that someone should be able to wave through belongs at
    `post_plan`; one whose verdict is meant to be final belongs at
    `pre_plan` or `pre_apply`. Nothing is lost by the restriction.
    """
    ts = await get_task_stage(db, task_stage_id)
    if ts is None:
        return None

    if ts.stage not in OVERRIDABLE_STAGES:
        raise ValueError(
            f"A '{ts.stage}' task stage cannot be overridden — its verdict is final. "
            "Fix the cause and queue a new run, or move the task to the 'post_plan' "
            "stage, which supports an admin override."
        )

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


async def sweep_unreachable_stages(db: AsyncSession) -> int:
    """Resolve stages whose results can no longer be reported, and re-drive.

    `expire_unreachable_results` runs inside `resolve_stage`, which covers
    every boundary that has something asking again: `pre_plan` is re-driven by
    the listener poll, `post_plan` by the reconciler, and a manually-confirmed
    `pre_apply` by the operator clicking Apply.

    One case has nobody asking. A run on an AUTO-APPLYING workspace that
    reached `planned` and was declined by a held `pre_apply` gate is driven
    only by the run task callback — and the whole failure mode here is that
    the callback never comes. The reconciler does not work `planned` runs and
    `_complete_plan` returns early once a run leaves `planning`, so without
    this sweep that run waits for a webhook that is never going to arrive,
    with no human expected to be watching.

    Returns the number of stages resolved.
    """
    from terrapod.db.models import now_utc

    cutoff = now_utc() - timedelta(seconds=_CALLBACK_TOKEN_TTL)
    stale = await db.execute(
        select(TaskStage.id)
        .join(TaskStageResult, TaskStageResult.task_stage_id == TaskStage.id)
        .where(
            TaskStage.status == "running",
            TaskStageResult.status.in_(["pending", "running"]),
            TaskStageResult.created_at < cutoff,
        )
        .distinct()
    )
    stage_ids = [row[0] for row in stale.all()]

    resolved = 0
    for stage_id in stage_ids:
        try:
            stage = await get_task_stage(db, stage_id)
            if stage is None:
                continue
            run_id, boundary = stage.run_id, stage.stage
            status = await resolve_stage(db, stage_id)
            await db.commit()
            resolved += 1
            logger.info(
                "Resolved a task stage whose callbacks can no longer arrive",
                task_stage_id=str(stage_id),
                stage=boundary,
                status=status,
            )
            # Only `pre_apply` needs pushing: the other two boundaries are
            # re-driven by something that is already polling.
            if boundary == "pre_apply":
                from terrapod.db.models import Run
                from terrapod.services import run_service

                run = await db.get(Run, run_id)
                if run is not None:
                    await run_service.redrive_auto_apply(db, run)
                    await db.commit()
        except Exception:
            # One bad stage must not stop the sweep, and the rollback keeps the
            # session usable for the next one (see the pre-plan pre-pass).
            logger.exception("failed to resolve an unreachable task stage", stage_id=str(stage_id))
            try:
                await db.rollback()
            except Exception:
                logger.exception("rollback during task stage sweep also failed")
    return resolved


async def unreachable_stage_sweep_cycle() -> None:
    """Periodic entry point for :func:`sweep_unreachable_stages`."""
    from terrapod.db.session import get_db_session

    async with get_db_session() as db:
        await sweep_unreachable_stages(db)


async def runs_with_unresolved_gate(db: AsyncSession, run_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these runs have an unresolved `pre_plan`/`pre_apply` stage.

    One query for a whole page. `blocked_by` needs this answer per run, and a
    list endpoint asking per run turns a page of N into N queries — which for
    `queued` and `planned` runs used to be zero, since `blocked_by` returned
    early for anything not in `planning`. Rebuilding that cost into a list
    serializer is the shape of regression the workspace-list work existed to
    remove, so the list path batches instead.
    """
    if not run_ids:
        return set()
    rows = await db.execute(
        select(TaskStage.run_id).where(
            TaskStage.run_id.in_(run_ids),
            TaskStage.stage.in_(["pre_plan", "pre_apply"]),
            TaskStage.status.notin_(["passed", "overridden"]),
        )
    )
    return {row[0] for row in rows.all()}
