"""Drift detection service — scheduled plan-only runs to detect infrastructure drift.

Periodic handler: drift_check_cycle() scans all drift-enabled workspaces and creates
plan-only runs for those whose interval has elapsed.

Triggered handler: handle_drift_run_completed() updates workspace drift_status based
on the run outcome and fires drift_detected notifications when drift is found.
"""

import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import (
    ConfigurationVersion,
    Run,
    StateVersion,
    VCSConnection,
    Workspace,
    utc_now,
)
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import run_service
from terrapod.storage import get_storage
from terrapod.storage.keys import config_version_key

logger = get_logger(__name__)

# States that indicate a run is still in progress
ACTIVE_STATES = {"pending", "queued", "planning", "planned", "confirmed", "applying"}


async def _is_workspace_busy(db: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """Check if a workspace has any active (non-terminal) runs."""
    result = await db.execute(
        select(func.count())
        .select_from(Run)
        .where(
            Run.workspace_id == workspace_id,
            Run.status.in_(ACTIVE_STATES),
        )
    )
    return result.scalar_one() > 0


async def _has_state(db: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """Check if a workspace has any state versions (something to drift against)."""
    result = await db.execute(
        select(func.count())
        .select_from(StateVersion)
        .where(StateVersion.workspace_id == workspace_id)
    )
    return result.scalar_one() > 0


async def _create_drift_run_vcs(
    db: AsyncSession,
    ws: Workspace,
) -> Run | None:
    """Create a drift detection run for a VCS-connected workspace.

    Downloads the archive from VCS, creates a ConfigurationVersion,
    and queues a plan-only drift run.
    """
    from terrapod.services.vcs_poller import (
        _download_archive,
        _get_branch_sha,
        _parse_repo_url,
        _resolve_branch,
    )

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if not conn or conn.status != "active":
        logger.warning("VCS connection not active for drift check", workspace=ws.name)
        return None

    parsed = _parse_repo_url(conn, ws.vcs_repo_url)
    if not parsed:
        logger.warning("Cannot parse VCS repo URL for drift check", workspace=ws.name)
        return None

    owner, repo = parsed
    branch = await _resolve_branch(conn, ws, owner, repo)
    if not branch:
        return None

    try:
        sha = await _get_branch_sha(conn, owner, repo, branch)
    except Exception as e:
        logger.error("Failed to get branch SHA for drift check", workspace=ws.name, error=str(e))
        return None

    if not sha:
        return None

    try:
        archive = await _download_archive(conn, owner, repo, sha)
    except Exception as e:
        logger.error("Failed to download archive for drift check", workspace=ws.name, error=str(e))
        return None

    cv = await run_service.create_configuration_version(
        db,
        workspace_id=ws.id,
        source="drift-detection",
        auto_queue_runs=False,
        speculative=False,
    )
    await db.flush()

    storage = get_storage()
    key = config_version_key(str(ws.id), str(cv.id))
    await storage.put(key, archive, content_type="application/x-tar")

    cv = await run_service.mark_configuration_uploaded(db, cv)

    run = await run_service.create_run(
        db,
        workspace=ws,
        message="Drift detection check",
        plan_only=True,
        source="drift-detection",
        configuration_version_id=cv.id,
        created_by="drift-detection",
        is_drift_detection=True,
    )

    run.vcs_commit_sha = sha
    run.vcs_branch = branch

    run = await run_service.queue_run(db, run)
    return run


async def _create_drift_run_non_vcs(
    db: AsyncSession,
    ws: Workspace,
) -> Run | None:
    """Create a drift detection run for a non-VCS workspace.

    Uses the latest uploaded ConfigurationVersion.
    """
    result = await db.execute(
        select(ConfigurationVersion)
        .where(
            ConfigurationVersion.workspace_id == ws.id,
            ConfigurationVersion.status == "uploaded",
        )
        .order_by(ConfigurationVersion.created_at.desc())
        .limit(1)
    )
    cv = result.scalar_one_or_none()

    if cv is None:
        logger.debug("No uploaded config version for drift check", workspace=ws.name)
        return None

    run = await run_service.create_run(
        db,
        workspace=ws,
        message="Drift detection check",
        plan_only=True,
        source="drift-detection",
        configuration_version_id=cv.id,
        created_by="drift-detection",
        is_drift_detection=True,
    )

    run = await run_service.queue_run(db, run)
    return run


async def drift_check_cycle() -> None:
    """Execute one drift check cycle: scan all drift-enabled workspaces.

    Called by the distributed scheduler as a periodic task. Only one
    replica runs this per interval across the entire deployment.
    """
    min_interval = settings.drift_detection.min_workspace_interval_seconds
    now = utc_now()

    async with get_db_session() as db:
        result = await db.execute(
            select(Workspace).where(Workspace.drift_detection_enabled.is_(True))
        )
        workspaces = list(result.scalars().all())

        if not workspaces:
            return

        logger.debug("Drift check cycle", workspace_count=len(workspaces))

        for ws in workspaces:
            try:
                # Check if due
                interval = max(ws.drift_detection_interval_seconds, min_interval)
                if ws.drift_last_checked_at is not None:
                    elapsed = (now - ws.drift_last_checked_at).total_seconds()
                    if elapsed < interval:
                        continue

                # Skip locked workspaces
                if ws.locked:
                    logger.debug("Skipping drift check: workspace locked", workspace=ws.name)
                    continue

                # Skip workspaces with active runs
                if await _is_workspace_busy(db, ws.id):
                    logger.debug("Skipping drift check: active run", workspace=ws.name)
                    continue

                # Skip workspaces with no state
                if not await _has_state(db, ws.id):
                    logger.debug("Skipping drift check: no state", workspace=ws.name)
                    continue

                # Create drift run
                if ws.vcs_connection_id and ws.vcs_repo_url:
                    run = await _create_drift_run_vcs(db, ws)
                else:
                    run = await _create_drift_run_non_vcs(db, ws)

                if run:
                    ws.drift_last_checked_at = now
                    await db.commit()

                    logger.info(
                        "Drift detection run created",
                        workspace=ws.name,
                        run_id=str(run.id),
                    )

            except Exception as e:
                logger.error(
                    "Error during drift check",
                    workspace=ws.name,
                    error=str(e),
                    exc_info=e,
                )


async def handle_drift_run_completed(payload: dict) -> None:
    """Update workspace drift_status based on a completed drift detection run.

    Called by the distributed scheduler's trigger consumer.
    """
    run_id = payload.get("run_id", "")
    workspace_id = payload.get("workspace_id", "")

    if not run_id or not workspace_id:
        logger.warning("Invalid drift_run_completed payload", payload=payload)
        return

    async with get_db_session() as db:
        run = await run_service.get_run(db, uuid.UUID(run_id))
        if run is None:
            return

        if not run.is_drift_detection:
            return

        ws = await db.get(Workspace, uuid.UUID(workspace_id))
        if ws is None:
            return

        # Map run outcome to drift status
        if run.status == "planned":
            if run.has_changes is True:
                ws.drift_status = "drifted"
            elif run.has_changes is False:
                ws.drift_status = "no_drift"
            else:
                # has_changes unknown — conservative: assume drift
                ws.drift_status = "drifted"
        elif run.status == "errored":
            ws.drift_status = "errored"
        elif run.status in ("canceled", "discarded"):
            # Don't update drift status for user-canceled runs
            return
        else:
            return

        ws.drift_last_checked_at = utc_now()
        await db.commit()

        logger.info(
            "Workspace drift status updated",
            workspace=ws.name,
            drift_status=ws.drift_status,
            run_id=run_id,
        )

        # Publish drift status change to SSE channels
        try:
            from terrapod.redis.client import (
                ADMIN_EVENTS_CHANNEL,
                WORKSPACE_LIST_EVENTS_CHANNEL,
                publish_event,
            )

            drift_payload = json.dumps({
                "event": "drift_status_change",
                "workspace_id": str(ws.id),
                "drift_status": ws.drift_status,
            })
            await publish_event(ADMIN_EVENTS_CHANNEL, drift_payload)
            await publish_event(WORKSPACE_LIST_EVENTS_CHANNEL, drift_payload)
        except Exception as e:
            logger.debug("Failed to publish drift event", error=str(e))

        # Fire drift_detected notification if drifted
        if ws.drift_status == "drifted":
            await _enqueue_drift_notification(run)


async def _enqueue_drift_notification(run: Run) -> None:
    """Enqueue a drift_detected notification for the workspace."""
    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "notification_deliver",
            {
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
                "trigger": "run:drift_detected",
            },
            dedup_key=f"drift_notif:{run.id}",
            dedup_ttl=60,
        )
    except Exception as e:
        logger.warning("Failed to enqueue drift notification", error=str(e))
