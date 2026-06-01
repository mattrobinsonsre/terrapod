"""Run state machine and lifecycle management service."""

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.metrics import (
    RUN_APPLY_DURATION,
    RUN_PLAN_DURATION,
    RUNS_CREATED,
    RUNS_TERMINAL,
    RUNS_TRANSITIONED,
)
from terrapod.db.models import (
    ConfigurationVersion,
    Run,
    RunTrigger,
    VCSConnection,
    Workspace,
    now_utc,
)
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service
from terrapod.services.notification_service import STATUS_TO_TRIGGER

logger = get_logger(__name__)

# Valid state transitions
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"queued", "canceled", "errored"},
    "queued": {"planning", "canceled", "errored"},
    "planning": {"planned", "errored", "canceled"},
    # `planned → applied` is the no-op apply: when the plan reports
    # has_changes=False there is nothing for an apply Job to do and
    # no new state version to upload (tofu doesn't bump serial on a
    # zero-change apply, so the upload would 500 on the unique
    # constraint anyway). The reconciler short-circuits to applied
    # without launching a Job.
    "planned": {"confirmed", "applied", "discarded", "errored", "canceled"},
    "confirmed": {"applying", "errored", "canceled"},
    "applying": {"applied", "errored", "canceled"},
}

TERMINAL_STATES = {"applied", "errored", "discarded", "canceled"}


async def _enqueue_notification(run: Run, target_status: str) -> None:
    """Enqueue a notification trigger for a run status change.

    Maps the status to a trigger event and enqueues via the distributed
    scheduler. Deduplication prevents duplicate notifications for the
    same run+trigger combination.
    """
    from terrapod.services.scheduler import enqueue_trigger

    trigger: str | None = None

    if target_status == "planned":
        # Distinguish between needs_attention and planned
        if not run.auto_apply and not run.plan_only:
            trigger = "run:needs_attention"
        else:
            trigger = "run:planned"
    else:
        trigger = STATUS_TO_TRIGGER.get(target_status)

    if not trigger:
        return

    try:
        await enqueue_trigger(
            "notification_deliver",
            {
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
                "trigger": trigger,
            },
            dedup_key=f"notif:{run.id}:{trigger}",
            dedup_ttl=60,
        )
    except Exception as e:
        # Never let notification enqueuing break the run state machine
        logger.warning("Failed to enqueue notification", error=str(e))


async def _enqueue_vcs_status(run: Run, target_status: str) -> None:
    """Enqueue a VCS commit status update for a run state change.

    Only meaningful for VCS-sourced runs (those with a commit SHA).
    Posts commit status and optionally PR comments back to the provider.

    Drift-detection runs are skipped — their commit SHAs are the current
    default-branch HEAD (already merged), and posting "Has changes" /
    "No changes" statuses there would be noise on commits that aren't
    under review.

    `has_changes` is snapshotted into the payload so the dispatcher is
    independent of DB-commit timing (the trigger enqueue happens in the
    same transaction as the status transition, but the dispatcher in
    another replica may consume the trigger before the commit lands).
    """
    from terrapod.services.scheduler import enqueue_trigger

    if run.is_drift_detection:
        return

    try:
        await enqueue_trigger(
            "vcs_commit_status",
            {
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
                "target_status": target_status,
                "has_changes": run.has_changes,
            },
            dedup_key=f"vcs_status:{run.id}:{target_status}",
            dedup_ttl=60,
        )
    except Exception as e:
        logger.warning("Failed to enqueue VCS status", error=str(e))


async def _enqueue_module_test_status(run: Run, target_status: str) -> None:
    """Enqueue a module_test_completed trigger for VCS status posting."""
    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "module_test_completed",
            {
                "run_id": str(run.id),
                "target_status": target_status,
            },
            dedup_key=f"modtest:{run.id}:{target_status}",
            dedup_ttl=60,
        )
    except Exception as e:
        logger.warning("Failed to enqueue module test status", error=str(e))


async def _enqueue_ai_plan_summary(run: Run, kind: str) -> None:
    """Enqueue an ai_plan_summary trigger for the summariser handler.

    Fast-path no-op when the feature is globally disabled so we don't
    charge Redis traffic for runs that will never be summarised.
    """
    from terrapod.config import settings

    if not settings.ai_summary.enabled:
        return
    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "ai_plan_summary",
            {"run_id": str(run.id), "kind": kind},
            dedup_key=f"aisum:{run.id}:{kind}",
            dedup_ttl=300,
        )
    except Exception as e:
        logger.debug("Failed to enqueue ai_plan_summary", error=str(e))


async def _enqueue_drift_completed(run: Run) -> None:
    """Enqueue a drift_run_completed trigger when a drift run finishes."""
    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "drift_run_completed",
            {
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
            },
            dedup_key=f"drift:{run.id}",
            dedup_ttl=60,
        )
    except Exception as e:
        logger.warning("Failed to enqueue drift completion", error=str(e))


async def _publish_run_available(run: Run) -> None:
    """Publish a run_available event to the pool's listener SSE channel.

    Called when a run transitions to queued or confirmed, notifying listeners
    that there is claimable work.
    """
    try:
        from terrapod.redis.client import publish_listener_event

        await publish_listener_event(
            str(run.pool_id),
            {"event": "run_available", "pool_id": str(run.pool_id)},
        )
    except Exception as e:
        # Never let SSE publishing break the state machine
        logger.debug("Failed to publish run_available", error=str(e))


async def _publish_run_event(run: Run, old_status: str, new_status: str) -> None:
    """Publish a run status change event via Redis pub/sub for SSE streaming."""
    try:
        from terrapod.redis.client import (
            ADMIN_EVENTS_CHANNEL,
            RUN_EVENTS_PREFIX,
            WORKSPACE_LIST_EVENTS_CHANNEL,
            publish_event,
        )

        payload = json.dumps(
            {
                "event": "run_status_change",
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
                "old_status": old_status,
                "new_status": new_status,
            }
        )
        # Per-workspace channel (run detail / workspace detail pages)
        await publish_event(f"{RUN_EVENTS_PREFIX}{run.workspace_id}", payload)
        # Admin health dashboard
        await publish_event(ADMIN_EVENTS_CHANNEL, payload)
        # Workspace list page
        await publish_event(WORKSPACE_LIST_EVENTS_CHANNEL, payload)
    except Exception as e:
        # Never let SSE publishing break the state machine
        logger.debug("Failed to publish run event", error=str(e))


def can_transition(current: str, target: str) -> bool:
    """Check if a state transition is valid."""
    if current in TERMINAL_STATES:
        return False
    return target in VALID_TRANSITIONS.get(current, set())


async def create_run(
    db: AsyncSession,
    workspace: Workspace,
    message: str = "",
    is_destroy: bool = False,
    auto_apply: bool | None = None,
    plan_only: bool = False,
    source: str = "tfe-api",
    terraform_version: str = "",
    configuration_version_id: uuid.UUID | None = None,
    created_by: str = "",
    is_drift_detection: bool = False,
    target_addrs: list[str] | None = None,
    replace_addrs: list[str] | None = None,
    refresh_only: bool = False,
    refresh: bool = True,
    allow_empty_apply: bool = False,
) -> Run:
    """Create a new run for a workspace.

    The run starts in 'pending' status and transitions to 'queued'
    when a configuration version is uploaded (or immediately if none needed).
    """
    if auto_apply is None:
        auto_apply = workspace.auto_apply

    pool_id = workspace.agent_pool_id

    # Pin the execution version to an exact x.y.z at run creation, the
    # same way CPU/memory are snapshotted. The workspace stores the
    # operator's intent (e.g. "1.11" = "track the latest 1.11.x"); the
    # run must carry a concrete version because a bare "1.11" only
    # resolves inside the binary-cache API call — the runner's
    # upstream fallback interpolates it verbatim into a release-artifact
    # URL that requires x.y.z and 404s otherwise (#338). resolve_version
    # is Redis-cached and returns its input unchanged if it can't reach
    # the upstream index, so this never blocks run creation.
    requested_version = terraform_version or workspace.terraform_version
    from terrapod.services.binary_cache_service import resolve_version

    try:
        pinned_version = await resolve_version(workspace.execution_backend, requested_version)
    except Exception:  # never block run creation on version resolution
        logger.warning(
            "Execution version resolution failed; pinning requested version as-is",
            requested=requested_version,
            backend=workspace.execution_backend,
            exc_info=True,
        )
        pinned_version = requested_version

    run = Run(
        workspace_id=workspace.id,
        configuration_version_id=configuration_version_id,
        status="pending",
        message=message,
        is_destroy=is_destroy,
        auto_apply=auto_apply,
        plan_only=plan_only,
        source=source,
        execution_backend=workspace.execution_backend,
        terraform_version=pinned_version,
        resource_cpu=workspace.resource_cpu,
        resource_memory=workspace.resource_memory,
        pool_id=pool_id,
        created_by=created_by,
        is_drift_detection=is_drift_detection,
        target_addrs=target_addrs or None,
        replace_addrs=replace_addrs or None,
        refresh_only=refresh_only,
        refresh=refresh,
        allow_empty_apply=allow_empty_apply,
    )
    db.add(run)
    await db.flush()

    RUNS_CREATED.labels(source=source, plan_only=str(plan_only)).inc()

    logger.info(
        "Run created",
        run_id=str(run.id),
        workspace=workspace.name,
        status=run.status,
    )

    # Enqueue run:created notification
    await _enqueue_notification(run, "pending")

    # Publish SSE event
    await _publish_run_event(run, "", "pending")

    return run


async def transition_run(
    db: AsyncSession,
    run: Run,
    target_status: str,
    error_message: str = "",
) -> Run:
    """Transition a run to a new status."""
    if not can_transition(run.status, target_status):
        raise ValueError(f"Invalid transition: {run.status} → {target_status}")

    now = now_utc()
    old_status = run.status
    run.status = target_status

    if error_message:
        run.error_message = error_message

    # Clear stale Job state when entering apply phase.
    # The plan phase's job_name and Redis job_status would otherwise cause
    # Phase-keyed Redis status prevents stale plan "succeeded" from leaking
    # into the apply phase (tp:job_status:{run_id}:plan vs :apply). We still
    # clear the plan key as hygiene and reset job_name so the reconciler
    # ignores this run until the new apply Job is reported.
    if target_status == "applying":
        run.job_name = None
        run.job_namespace = None
        try:
            from terrapod.redis.client import delete_job_status

            await delete_job_status(str(run.id), "plan")
        except Exception:
            pass  # Best-effort cleanup

    # Track phase timestamps
    if target_status == "planning":
        run.plan_started_at = now
    elif (
        target_status in ("planned", "errored") and run.plan_started_at and not run.plan_finished_at
    ):
        run.plan_finished_at = now
    elif target_status == "applying":
        run.apply_started_at = now
    elif (
        target_status in ("applied", "errored")
        and run.apply_started_at
        and not run.apply_finished_at
    ):
        run.apply_finished_at = now

    await db.flush()

    RUNS_TRANSITIONED.labels(from_status=old_status, to_status=target_status).inc()
    if target_status in TERMINAL_STATES:
        RUNS_TERMINAL.labels(status=target_status).inc()
    if run.plan_started_at and run.plan_finished_at and target_status in ("planned", "errored"):
        duration = (run.plan_finished_at - run.plan_started_at).total_seconds()
        RUN_PLAN_DURATION.labels(status=target_status).observe(duration)
    if run.apply_started_at and run.apply_finished_at and target_status in ("applied", "errored"):
        duration = (run.apply_finished_at - run.apply_started_at).total_seconds()
        RUN_APPLY_DURATION.labels(status=target_status).observe(duration)

    logger.info(
        "Run transitioned",
        run_id=str(run.id),
        from_status=old_status,
        to_status=target_status,
    )

    # Publish SSE event
    await _publish_run_event(run, old_status, target_status)

    # Notify listeners when a run becomes claimable
    if target_status in ("queued", "confirmed") and run.pool_id:
        await _publish_run_available(run)

    # Fire run triggers when a non-speculative run completes apply
    if target_status == "applied" and not run.plan_only:
        await fire_run_triggers(db, run.workspace_id)

    # #314: a successful opt-in autodiscovery destroy archives the
    # workspace (soft-delete; retained for audit). Literal source
    # compare avoids importing the lifecycle service (it imports us).
    if (
        target_status == "applied"
        and run.is_destroy
        and not run.plan_only
        and run.source == "autodiscovery-lifecycle"
    ):
        ws = await db.get(Workspace, run.workspace_id)
        if ws is not None and ws.lifecycle_state != "archived":
            ws.lifecycle_state = "archived"
            ws.lifecycle_reason = "autodiscovery destroy completed — archived"

    # Enqueue notification for this status change
    await _enqueue_notification(run, target_status)

    # Enqueue VCS commit status for VCS-sourced runs
    if run.vcs_commit_sha:
        await _enqueue_vcs_status(run, target_status)

    # Enqueue module test status when a module-test run reaches a meaningful state
    if run.source == "module-test" and run.module_overrides:
        await _enqueue_module_test_status(run, target_status)

    # Enqueue drift status update when a drift detection run reaches a terminal state.
    # Plan-only drift runs end in "planned" (not in TERMINAL_STATES), so check that too.
    drift_terminal = TERMINAL_STATES | {"planned"}
    if run.is_drift_detection and target_status in drift_terminal:
        await _enqueue_drift_completed(run)

    # AI plan summariser (#401) — failure-analysis kind only.
    # The `plan_summary` kind is enqueued from
    # routers/run_artifacts.upload_plan_json_output AFTER the runner has
    # uploaded the structured plan JSON. Firing it here on the `planned`
    # transition raced the runner: transition_run runs on the
    # plan-result POST, but plan-json-output upload happens a few
    # operations later in the runner entrypoint, so the summariser
    # would hit `Object not found` half the time. Errored plans never
    # upload JSON, so failure-analysis still belongs here.
    if target_status == "errored" and run.apply_started_at is None:
        await _enqueue_ai_plan_summary(run, "failure_analysis")

    return run


async def complete_plan(
    db: AsyncSession,
    run: Run,
    has_changes: bool | None = None,
) -> Run:
    """Drive a `planning` run to its post-plan terminal state.

    Idempotent landing point shared by the two paths that can authoritatively
    declare a plan finished:

    1. The runner Job posting `/plan-result` (runner is source of truth — it
       has the exit code and the diff in hand).
    2. The reconciler observing the K8s Job as Completed via the listener
       (indirect; covers the case where the runner died before posting).

    Either path can race the other. The first one to win does the work; the
    second sees `run.status != "planning"` and returns the run unchanged.

    Mirrors the previous behaviour of `_handle_succeeded` in the reconciler:
    optionally runs post-plan task stages, transitions to `planned`, releases
    the lock for plan-only runs, short-circuits zero-change non-speculative
    plans straight to `applied`, and auto-applies when configured.
    """
    from terrapod.services import run_task_service

    # Idempotency guard — both callers can race; later one is a no-op.
    if run.status != "planning":
        return run

    # Stamp the plan-phase end timestamp now, before the run-task and policy
    # gates can hold the run in `planning`. Semantically the plan phase is
    # done (the runner posted plan-result or its Job reported success); the
    # gates are a downstream platform concern. Doing this here keeps the
    # `planned-at` UI timestamp tied to "plan finished" rather than "gates
    # cleared", and makes the field available to anything else that wants
    # it (metrics, confirm_run, drift detection) regardless of how long
    # the run is held by a gate.
    if run.plan_finished_at is None and run.plan_started_at is not None:
        from terrapod.db.models import now_utc

        run.plan_finished_at = now_utc()

    if has_changes is not None:
        run.has_changes = has_changes

    # Post-plan task stage gate. If the stage isn't passed/overridden yet,
    # leave the run in `planning` for the next reconciler tick to re-check.
    ts = await run_task_service.create_task_stage(db, run.id, run.workspace_id, "post_plan")
    if ts is not None:
        stage_status = await run_task_service.resolve_stage(db, ts.id)
        if stage_status not in ("passed", "overridden"):
            if stage_status == "failed":
                await transition_run(
                    db, run, "errored", error_message="Post-plan task stage failed"
                )
            return run

    # Post-plan OPA policy gate (#343). The runner has already evaluated
    # applicable policies and posted results to /policy-results before
    # posting plan-result — so by the time we get here the
    # policy_evaluation rows already exist (or there were no applicable
    # sets). A mandatory unoverridden failure keeps the run in
    # `planning` (surfaced via the run's policy-checks attribute) rather
    # than erroring it — the idempotent complete_plan then re-drives
    # the run once an admin overrides, with no reconciler race.
    from terrapod.services import policy_set_service

    gate = await policy_set_service.evaluate_post_plan(db, run)
    if gate != policy_set_service.GATE_PASSED:
        return run

    run = await transition_run(db, run, "planned")

    # Unlock workspace for plan-only runs — they make no further state moves.
    if run.plan_only:
        ws = await db.get(Workspace, run.workspace_id)
        if ws and ws.locked:
            ws.locked = False
            ws.lock_id = None

    # Zero-change non-speculative plans short-circuit straight to `applied`.
    # No apply Job is launched — the runner couldn't change anything anyway,
    # and an empty apply triggers the duplicate-serial 500 on state upload.
    if not run.plan_only and run.has_changes is False:
        run = await complete_planned_as_noop(db, run)
        logger.info("Plan succeeded — no changes, skipping apply", run_id=str(run.id))
        return run

    if run.auto_apply and not run.plan_only:
        run = await transition_run(db, run, "confirmed")

    logger.info("Plan succeeded", run_id=str(run.id))
    return run


async def complete_apply(db: AsyncSession, run: Run) -> Run:
    """Drive an `applying` run to `applied`.

    Idempotent counterpart to :func:`complete_plan`. Same dual-path rationale
    — either the runner's `/apply-result` POST or the reconciler's listener
    round-trip can win; whichever lands first does the transition.

    On successful apply for a PR-associated run, schedules cross-workspace
    gate evaluation (#282 phase 8): invalidate stale sibling-PR plans on
    the same workspace, refresh the status comment, and fire auto-merge
    if every PR-affected workspace has met its required state.
    """
    if run.status != "applying":
        return run

    run = await transition_run(db, run, "applied")

    ws = await db.get(Workspace, run.workspace_id)
    if ws and ws.locked:
        ws.locked = False
        ws.lock_id = None

    logger.info("Apply succeeded", run_id=str(run.id))

    # Apply-then-merge follow-ups: invalidate stale sibling-PR plans on
    # the same workspace, then trigger cross-workspace gate evaluation
    # for the PR this run was associated with. Enqueueing here keeps
    # the transition synchronous and the gate evaluation async.
    if run.vcs_pull_request_number is not None and ws is not None:
        await _invalidate_sibling_pr_plans(db, ws.id, run.id, run.vcs_pull_request_number)
        from terrapod.services.scheduler import enqueue_trigger

        await enqueue_trigger(
            "vcs_apply_completed",
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
                "pr_number": run.vcs_pull_request_number,
            },
            dedup_key=f"vcs_apply_completed:{run.id}",
        )
    return run


async def _invalidate_sibling_pr_plans(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    just_applied_run_id: uuid.UUID,
    just_applied_pr_number: int,
) -> None:
    """Cancel any other-PR `planned` runs on this workspace.

    After a successful state-mutating apply, sibling PR plans against
    pre-apply state are no longer valid — `tofu apply tfplan` would
    refuse with a state-lineage error. We cancel them proactively
    (#282 cross-PR lock race section); the poller's next cycle will
    re-plan against the new state once the workspace lock allows.
    """
    sibling_q = await db.execute(
        select(Run).where(
            Run.workspace_id == workspace_id,
            Run.status == "planned",
            Run.vcs_pull_request_number.isnot(None),
            Run.vcs_pull_request_number != just_applied_pr_number,
            Run.id != just_applied_run_id,
        )
    )
    for sibling in sibling_q.scalars().all():
        sibling.vcs_apply_blocked_reason = (
            f"Plan superseded by apply of PR #{just_applied_pr_number}."
        )
        try:
            await cancel_run(db, sibling, force=True)
            logger.info(
                "invalidated sibling-PR plan",
                run_id=str(sibling.id),
                workspace_id=str(workspace_id),
                superseded_by_pr=just_applied_pr_number,
            )
        except Exception as e:
            logger.warning(
                "failed to cancel sibling-PR plan",
                run_id=str(sibling.id),
                error=str(e),
            )


async def complete_planned_as_noop(db: AsyncSession, run: Run) -> Run:
    """Transition a planned run directly to `applied` without an apply Job.

    Used when the plan reports `has_changes=False`: tofu apply on a
    zero-change plan does no work and doesn't bump the state serial, so
    launching an apply Job is wasted compute and triggers the duplicate-
    serial 500 on state upload. Skip straight to `applied`.

    Sets `apply_started_at` before transitioning so `transition_run` sets
    `apply_finished_at` in turn — the resulting run reports an
    `applied-at` status timestamp, keeping the API response coherent for
    UI timelines and TFE clients that expect both timestamps on a
    terminal `applied` run. Apply duration recorded as 0s, which is
    accurate (the apply phase was a no-op).

    Releases the workspace lock since no Job will run.
    """
    run.apply_started_at = now_utc()
    run = await transition_run(db, run, "applied")
    ws = await db.get(Workspace, run.workspace_id)
    if ws and ws.locked:
        ws.locked = False
        ws.lock_id = None
    return run


async def queue_run(db: AsyncSession, run: Run) -> Run:
    """Queue a run for execution."""
    return await transition_run(db, run, "queued")


async def fire_run_triggers(
    db: AsyncSession,
    source_workspace_id: uuid.UUID,
) -> None:
    """Fire run triggers for downstream workspaces after a successful apply.

    Queries all RunTrigger rows where this workspace is the source,
    then creates and queues a new run for each destination workspace.
    """
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(RunTrigger)
        .options(selectinload(RunTrigger.workspace))
        .where(RunTrigger.source_workspace_id == source_workspace_id)
    )
    triggers = list(result.scalars().all())

    if not triggers:
        return

    # Get source workspace name for the run message
    source_ws = await db.get(Workspace, source_workspace_id)
    source_name = source_ws.name if source_ws else str(source_workspace_id)

    for trigger in triggers:
        dest_ws = trigger.workspace
        if dest_ws is None:
            continue

        run = await create_run(
            db,
            workspace=dest_ws,
            message=f"Triggered by successful apply in workspace '{source_name}'",
            auto_apply=dest_ws.auto_apply,
            plan_only=False,
            source="tfe-api",
        )
        await queue_run(db, run)

        logger.info(
            "Run trigger fired",
            source_workspace=source_name,
            destination_workspace=dest_ws.name,
            run_id=str(run.id),
        )


class ApplyBlocked(Exception):
    """Raised when confirm_run rejects an apply because the underlying
    PR/MR isn't mergeable per the VCS provider's gate (#282).

    The reason string is what gets surfaced on the PR status comment.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def _check_mergeability_or_block(db: AsyncSession, run: Run) -> None:
    """For apply-then-merge runs, query the VCS provider's mergeability
    gate before allowing confirm. If blocked, persist the reason on the
    run and raise ApplyBlocked.

    No-op for runs that aren't PR-associated apply-then-merge runs
    (default-mode runs, drift-detection runs, CLI runs). The branch is
    gated to keep the default-mode `confirm_run` path zero-cost.
    """
    if run.vcs_pull_request_number is None:
        return
    workspace = await db.get(Workspace, run.workspace_id)
    if workspace is None or workspace.vcs_workflow != "apply_then_merge":
        return
    if workspace.vcs_connection_id is None or not workspace.vcs_repo_url:
        return
    conn = await db.get(VCSConnection, workspace.vcs_connection_id)
    if conn is None:
        return

    # Provider dispatch — github_service / gitlab_service expose
    # get_pull_request_mergeability with the same signature.
    if conn.provider == "github":
        parsed = github_service.parse_repo_url(workspace.vcs_repo_url)
        check = github_service.get_pull_request_mergeability
    elif conn.provider == "gitlab":
        parsed = gitlab_service.parse_repo_url(workspace.vcs_repo_url)
        check = gitlab_service.get_pull_request_mergeability
    else:
        logger.warning(
            "confirm_run: unknown VCS provider, skipping mergeability check",
            provider=conn.provider,
        )
        return
    if parsed is None:
        logger.warning(
            "confirm_run: could not parse repo URL, skipping mergeability check",
            repo_url=workspace.vcs_repo_url,
        )
        return
    owner, repo = parsed

    try:
        status = await check(conn, owner, repo, run.vcs_pull_request_number)
    except Exception as e:
        # If the provider call fails we don't want to silently apply —
        # surface the error as a transient block so the user retries.
        reason = f"Could not verify mergeability ({e}); retry shortly."
        run.vcs_apply_blocked_reason = reason
        raise ApplyBlocked(reason) from e

    if status.unknown:
        # GitHub returns mergeable=null for a few seconds after a push
        # while it computes the merge check. Surface this as a transient
        # block — caller / user retries within seconds.
        reason = status.reason or "Mergeability is still being computed; retry shortly."
        run.vcs_apply_blocked_reason = reason
        raise ApplyBlocked(reason)
    if not status.mergeable:
        run.vcs_apply_blocked_reason = status.reason or f"PR is not mergeable ({status.state})."
        raise ApplyBlocked(run.vcs_apply_blocked_reason)
    # Mergeable now — clear any stale block reason from a previous attempt.
    run.vcs_apply_blocked_reason = None


async def confirm_run(db: AsyncSession, run: Run) -> Run:
    """Confirm a planned run for apply.

    For apply-then-merge runs, the VCS provider's mergeability gate
    fires first — if the PR isn't mergeable, this raises `ApplyBlocked`
    with the provider's own language (`dirty` / `blocked` / `behind` /
    `draft` / etc.) attached to the run for the status comment.
    """
    if run.status != "planned":
        raise ValueError(f"Can only confirm runs in 'planned' status, got '{run.status}'")
    await _check_mergeability_or_block(db, run)
    return await transition_run(db, run, "confirmed")


async def discard_run(db: AsyncSession, run: Run) -> Run:
    """Discard a planned run."""
    if run.status != "planned":
        raise ValueError(f"Can only discard runs in 'planned' status, got '{run.status}'")
    # Unlock workspace
    workspace = await db.get(Workspace, run.workspace_id)
    if workspace and workspace.locked:
        workspace.locked = False
        workspace.lock_id = None
    return await transition_run(db, run, "discarded")


# User-cancelable states: only in-progress. `planned` is awaiting user
# confirm/discard and should be resolved that way — cancelling a
# planned-awaiting-confirm run leaves the workspace in an ambiguous state
# (plan exists, nothing applied, no record of user decision). Terminal
# states have nothing left to cancel.
#
# Internal callers (vcs_poller, module_impact) legitimately cancel stale
# `planned` speculative runs when a newer commit supersedes them; they
# pass `force=True` below to bypass the user-action gate.
CANCELABLE_STATES = frozenset({"pending", "queued", "planning", "confirmed", "applying"})


async def cancel_run(db: AsyncSession, run: Run, *, force: bool = False) -> Run:
    """Cancel a run.

    By default only in-progress states (`CANCELABLE_STATES`) are cancelable.
    Pass `force=True` to bypass that check — only for internal callers
    that need to cancel superseded `planned` runs as part of cleanup.
    Terminal states are always rejected.
    """
    if run.status in TERMINAL_STATES:
        raise ValueError(f"Cannot cancel run in terminal state '{run.status}'")
    if not force and run.status not in CANCELABLE_STATES:
        raise ValueError(f"Cannot cancel run in state '{run.status}'")
    # Unlock workspace
    workspace = await db.get(Workspace, run.workspace_id)
    if workspace and workspace.locked:
        workspace.locked = False
        workspace.lock_id = None
    return await transition_run(db, run, "canceled")


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> Run | None:
    """Get a run by ID."""
    result = await db.execute(select(Run).where(Run.id == run_id))
    return result.scalar_one_or_none()


async def list_workspace_runs(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    page_number: int = 1,
    page_size: int = 20,
) -> list[Run]:
    """List runs for a workspace, ordered by creation time desc."""
    result = await db.execute(
        select(Run)
        .where(Run.workspace_id == workspace_id)
        .order_by(Run.created_at.desc())
        .offset((page_number - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all())


async def claim_next_run(
    db: AsyncSession,
    listener_id: uuid.UUID,
    pool_id: uuid.UUID,
    listener_name: str = "",
) -> tuple[Run, str] | None:
    """Claim the next available run for a listener.

    Returns (run, phase) where phase is "plan" or "apply", or None if
    no runs are available.

    Looks for both queued runs (plan phase) and confirmed runs (apply phase).
    This ensures apply phases are picked up even if the original listener
    that ran the plan is no longer available — listeners are stateless.

    Uses SELECT ... FOR UPDATE SKIP LOCKED for Postgres job queue pattern.
    """
    # Try queued runs first (plan phase), then confirmed runs (apply phase)
    for target_status, phase, next_status in [
        ("queued", "plan", "planning"),
        ("confirmed", "apply", "applying"),
    ]:
        query = (
            select(Run)
            .where(
                Run.status == target_status,
                Run.pool_id == pool_id,
            )
            .order_by(Run.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        result = await db.execute(query)
        run = result.scalar_one_or_none()

        if run is not None:
            run.listener_id = listener_id
            run = await transition_run(db, run, next_status)
            await db.flush()

            logger.info(
                "Run claimed by listener",
                run_id=str(run.id),
                listener=listener_name,
                phase=phase,
            )

            return run, phase

    return None


# --- Configuration Versions ---


async def create_configuration_version(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    source: str = "tfe-api",
    auto_queue_runs: bool = True,
    speculative: bool = False,
) -> ConfigurationVersion:
    """Create a configuration version."""
    cv = ConfigurationVersion(
        workspace_id=workspace_id,
        source=source,
        status="pending",
        auto_queue_runs=auto_queue_runs,
        speculative=speculative,
    )
    db.add(cv)
    await db.flush()
    return cv


async def get_configuration_version(
    db: AsyncSession, cv_id: uuid.UUID
) -> ConfigurationVersion | None:
    """Get a configuration version by ID."""
    result = await db.execute(select(ConfigurationVersion).where(ConfigurationVersion.id == cv_id))
    return result.scalar_one_or_none()


async def mark_configuration_uploaded(
    db: AsyncSession, cv: ConfigurationVersion
) -> ConfigurationVersion:
    """Mark a configuration version as uploaded."""
    cv.status = "uploaded"
    await db.flush()
    return cv


async def get_latest_uploaded_cv(
    db: AsyncSession, workspace_id: uuid.UUID
) -> ConfigurationVersion | None:
    """Return the most recent fully-uploaded, non-speculative CV for a
    workspace, or None.

    Used by UI-queued runs on non-VCS workspaces (#358): there's no VCS
    to auto-fetch from and the CLI is the only producer of CVs, so the
    last successful CLI upload is the right default for "Queue Plan".
    Speculative CVs are excluded — they belong to PR/MR speculative runs
    and don't reflect the workspace's apply-able state.
    """
    result = await db.execute(
        select(ConfigurationVersion)
        .where(
            ConfigurationVersion.workspace_id == workspace_id,
            ConfigurationVersion.status == "uploaded",
            ConfigurationVersion.speculative.is_(False),
        )
        .order_by(ConfigurationVersion.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_orphaned_runs(
    db: AsyncSession,
    listener_ids: list[uuid.UUID],
) -> list[Run]:
    """Find runs stuck in planning/applying for listeners that are offline."""
    result = await db.execute(
        select(Run).where(
            Run.status.in_(["planning", "applying"]),
            Run.listener_id.in_(listener_ids),
        )
    )
    return list(result.scalars().all())
