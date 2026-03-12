"""Run state machine and lifecycle management service."""

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    ConfigurationVersion,
    Run,
    RunnerListener,
    RunTrigger,
    Workspace,
    utc_now,
)
from terrapod.logging_config import get_logger
from terrapod.services.notification_service import STATUS_TO_TRIGGER
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    apply_log_key,
    config_version_key,
    plan_log_key,
    plan_output_key,
    state_key,
)

logger = get_logger(__name__)

# Valid state transitions
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"queued", "canceled", "errored"},
    "queued": {"planning", "canceled", "errored"},
    "planning": {"planned", "errored", "canceled"},
    "planned": {"confirmed", "discarded", "errored", "canceled"},
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
    """
    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "vcs_commit_status",
            {
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
                "target_status": target_status,
            },
            dedup_key=f"vcs_status:{run.id}:{target_status}",
            dedup_ttl=60,
        )
    except Exception as e:
        logger.warning("Failed to enqueue VCS status", error=str(e))


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
) -> Run:
    """Create a new run for a workspace.

    The run starts in 'pending' status and transitions to 'queued'
    when a configuration version is uploaded (or immediately if none needed).
    """
    if auto_apply is None:
        auto_apply = workspace.auto_apply

    pool_id = workspace.agent_pool_id

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
        terraform_version=terraform_version or workspace.terraform_version,
        resource_cpu=workspace.resource_cpu,
        resource_memory=workspace.resource_memory,
        pool_id=pool_id,
        created_by=created_by,
        is_drift_detection=is_drift_detection,
    )
    db.add(run)
    await db.flush()

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

    now = utc_now()
    old_status = run.status
    run.status = target_status

    if error_message:
        run.error_message = error_message

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

    logger.info(
        "Run transitioned",
        run_id=str(run.id),
        from_status=old_status,
        to_status=target_status,
    )

    # Publish SSE event
    await _publish_run_event(run, old_status, target_status)

    # Fire run triggers when a non-speculative run completes apply
    if target_status == "applied" and not run.plan_only:
        await fire_run_triggers(db, run.workspace_id)

    # Enqueue notification for this status change
    await _enqueue_notification(run, target_status)

    # Enqueue VCS commit status for VCS-sourced runs
    if run.vcs_commit_sha:
        await _enqueue_vcs_status(run, target_status)

    # Enqueue drift status update when a drift detection run reaches a terminal state.
    # Plan-only drift runs end in "planned" (not in TERMINAL_STATES), so check that too.
    drift_terminal = TERMINAL_STATES | {"planned"}
    if run.is_drift_detection and target_status in drift_terminal:
        await _enqueue_drift_completed(run)

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


async def confirm_run(db: AsyncSession, run: Run) -> Run:
    """Confirm a planned run for apply."""
    if run.status != "planned":
        raise ValueError(f"Can only confirm runs in 'planned' status, got '{run.status}'")
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


async def cancel_run(db: AsyncSession, run: Run) -> Run:
    """Cancel a run."""
    if run.status in TERMINAL_STATES:
        raise ValueError(f"Cannot cancel run in terminal state '{run.status}'")
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
    listener: RunnerListener,
) -> Run | None:
    """Claim the next queued run for a listener.

    Uses SELECT ... FOR UPDATE SKIP LOCKED for Postgres job queue pattern.
    """
    query = (
        select(Run)
        .where(
            Run.status == "queued",
            Run.pool_id == listener.pool_id,
        )
        .order_by(Run.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    result = await db.execute(query)
    run = result.scalar_one_or_none()

    if run is None:
        return None

    # Claim the run
    run.listener_id = listener.id
    run = await transition_run(db, run, "planning")
    await db.flush()

    logger.info(
        "Run claimed by listener",
        run_id=str(run.id),
        listener=listener.name,
    )

    return run


async def get_run_presigned_urls(
    db: AsyncSession,
    run: Run,
) -> dict[str, str]:
    """Generate presigned URLs for a run's artifacts.

    Returns URLs the runner Job needs to download/upload artifacts.
    """
    storage = get_storage()
    ws_id = str(run.workspace_id)
    run_id = str(run.id)

    urls: dict[str, str] = {}

    # Config archive download
    if run.configuration_version_id:
        cv_key = config_version_key(ws_id, str(run.configuration_version_id))
        urls["config_download_url"] = (await storage.presigned_get_url(cv_key)).url

    # Current state download (latest state version)
    from terrapod.db.models import StateVersion

    sv_result = await db.execute(
        select(StateVersion)
        .where(StateVersion.workspace_id == run.workspace_id)
        .order_by(StateVersion.serial.desc())
        .limit(1)
    )
    sv = sv_result.scalar_one_or_none()
    if sv:
        sk = state_key(ws_id, str(sv.id))
        urls["state_download_url"] = (await storage.presigned_get_url(sk)).url

    # Plan log upload
    urls["plan_log_upload_url"] = (await storage.presigned_put_url(plan_log_key(ws_id, run_id))).url

    # Plan file upload
    urls["plan_file_upload_url"] = (
        await storage.presigned_put_url(plan_output_key(ws_id, run_id))
    ).url

    # Apply log upload
    urls["apply_log_upload_url"] = (
        await storage.presigned_put_url(apply_log_key(ws_id, run_id))
    ).url

    # State upload (for apply phase)
    urls["state_upload_url"] = (
        await storage.presigned_put_url(state_key(ws_id, f"{run_id}-new"))
    ).url

    # Binary URL: runners detect their own OS/arch at runtime and download
    # from the API binary cache endpoint directly (no pre-generated URL needed).
    # TP_API_URL + TP_BACKEND + TP_VERSION are passed as Job env vars.

    return urls


async def get_apply_presigned_urls(
    db: AsyncSession,
    run: Run,
) -> dict[str, str]:
    """Generate presigned URLs needed for the apply phase.

    Returns URLs for plan file download, config download, state download,
    apply log upload, and new state upload.
    """
    storage = get_storage()
    ws_id = str(run.workspace_id)
    run_id = str(run.id)

    urls: dict[str, str] = {}

    # Plan file download (saved from plan phase)
    urls["plan_file_download_url"] = (
        await storage.presigned_get_url(plan_output_key(ws_id, run_id))
    ).url

    # Config archive download
    if run.configuration_version_id:
        cv_key = config_version_key(ws_id, str(run.configuration_version_id))
        urls["config_download_url"] = (await storage.presigned_get_url(cv_key)).url

    # Current state download (latest state version)
    from terrapod.db.models import StateVersion

    sv_result = await db.execute(
        select(StateVersion)
        .where(StateVersion.workspace_id == run.workspace_id)
        .order_by(StateVersion.serial.desc())
        .limit(1)
    )
    sv = sv_result.scalar_one_or_none()
    if sv:
        urls["state_download_url"] = (
            await storage.presigned_get_url(state_key(ws_id, str(sv.id)))
        ).url

    # Apply log upload
    urls["apply_log_upload_url"] = (
        await storage.presigned_put_url(apply_log_key(ws_id, run_id))
    ).url

    # State upload (for new state after apply)
    urls["state_upload_url"] = (
        await storage.presigned_put_url(state_key(ws_id, f"{run_id}-new"))
    ).url

    # Binary cache URL (resolve partial version → exact)
    try:
        from terrapod.services.binary_cache_service import get_or_cache_binary, resolve_version

        resolved_version = await resolve_version(run.execution_backend, run.terraform_version)
        urls["binary_url"] = await get_or_cache_binary(
            db, storage, run.execution_backend, resolved_version, "linux", "amd64"
        )
    except Exception as e:
        logger.warning("Failed to resolve binary URL for apply", error=str(e))

    return urls


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
