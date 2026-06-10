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
    now_utc,
)
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import run_service

logger = get_logger(__name__)

# Drift checks should skip workspaces where terraform is actively
# running — a second concurrent plan against the same state could
# observe inconsistent intermediate state and produce a false drift
# signal. But "active" for that purpose is narrower than "non-terminal":
# {planning, applying} are actively consuming a runner slot;
# everything else (pending lock-acquire, queued for pickup, planned
# awaiting confirm, confirmed awaiting transition) is either
# instantaneous (pending, confirmed) or waits indefinitely on an
# external trigger (planned awaits operator confirm/discard) and
# does not conflict with a plan-only drift run on its own CV.
#
# This was previously a {pending, queued, planning, planned,
# confirmed, applying} set, which made any workspace with a `planned`
# run sitting awaiting confirmation skip drift forever — the
# operator-visible status column shows the LATEST run (applied) so
# the workspace looks healthy but `drift_status` quietly froze at
# the first errored attempt (production incident: four workspaces
# stuck for 1-7 weeks without a drift retry).
RUNNER_BUSY_STATES = {"planning", "applying"}


async def _is_runner_busy(db: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """Workspace has a run that's actively executing on a runner.

    NOTE — Code ↔ Tests contract (CLAUDE.md): the precise membership of
    RUNNER_BUSY_STATES is pinned by
    `tests/services/test_drift_detection_service.py::TestDriftCheckCycle::
    test_is_runner_busy_only_counts_planning_or_applying`. Widening this
    set (e.g. re-adding `planned`) reintroduces the production incident
    where workspaces with stale `planned` peers froze drift forever.
    Narrow it deliberately — and update both the source comment AND the
    regression test together.

    True only when terraform is mid-plan or mid-apply for this
    workspace — running a parallel drift check then would produce a
    racy/inconsistent observation. Runs in `planned` (awaiting
    confirm/discard) or `confirmed` (awaiting applying transition)
    do NOT count: they don't hold the runner, and a plan-only drift
    run on its own CV doesn't conflict with them.
    """
    result = await db.execute(
        select(func.count())
        .select_from(Run)
        .where(
            Run.workspace_id == workspace_id,
            Run.status.in_(RUNNER_BUSY_STATES),
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

    Goes through the same VCSArchiveCache / git_fetch pipeline that the
    regular VCS-poll path uses (`_stream_cv_upload_from_cache`) — NOT
    the raw `download_archive` provider API. The raw API returns a
    tarball wrapped in a top-level `<owner>-<repo>-<sha>/` directory;
    the runner's `chdir /workspace` after extraction lands one level
    above the actual repo content, and any `var-files` referenced from
    the workspace's settings (e.g. `envs/prod-us2.tfvars`) resolve as
    missing. Pre-v0.35.1 this latent bug was masked because drift
    detection rarely fired (the `_is_workspace_busy` gate was so wide
    it skipped almost everything); when v0.35.1 narrowed the gate to
    `RUNNER_BUSY_STATES`, drift started running on every drift-enabled
    workspace and tripped on this immediately — every drift run errored
    with `Given variables file <path> does not exist`.

    Using `VCSArchiveCache.get_or_fetch` produces a clean, root-level
    tarball identical to what regular VCS-poll runs use, so the runner
    init step behaves the same as on a normal apply.
    """
    from terrapod.services.vcs_archive_cache import VCSArchiveCache
    from terrapod.services.vcs_poller import (
        _get_branch_sha,
        _parse_repo_url,
        _resolve_branch,
        _stream_cv_upload_from_cache,
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

    cache = VCSArchiveCache()
    try:
        # `paths=None` fetches the whole repo. Drift detection has no
        # narrowing context (no trigger_prefixes equivalent for the
        # drift-check-fires-on-its-own path); the full repo matches
        # what a normal VCS-poll apply would have used. The cache is
        # keyed by (conn, sha, paths) so other concurrent paths
        # narrowing the same sha don't share this entry.
        cache_storage_key = await cache.get_or_fetch(conn, owner, repo, sha, paths=None)
    except Exception as e:
        logger.error(
            "Failed to fetch repo archive for drift check", workspace=ws.name, error=str(e)
        )
        return None

    cv = await run_service.create_configuration_version(
        db,
        workspace_id=ws.id,
        source="drift-detection",
        auto_queue_runs=False,
        speculative=False,
    )
    await db.flush()

    try:
        await _stream_cv_upload_from_cache(cache_storage_key, ws.id, cv.id)
    except Exception as e:
        logger.error(
            "Failed to materialise cached archive for drift CV",
            workspace=ws.name,
            error=str(e),
        )
        return None

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

    Uses the configuration version from the latest successful apply —
    i.e. the bytes that produced the workspace's current state. Picking
    "latest uploaded" instead would compare live state against a config
    that has never been applied, surfacing phantom diffs (between two
    configs) rather than actual infrastructure drift.

    Returns None if the workspace has never been applied — there's no
    reference config to diff against, so drift detection is a no-op.
    """
    result = await db.execute(
        select(ConfigurationVersion)
        .join(Run, Run.configuration_version_id == ConfigurationVersion.id)
        .where(
            Run.workspace_id == ws.id,
            Run.status == "applied",
            ConfigurationVersion.status == "uploaded",
        )
        .order_by(Run.apply_finished_at.desc())
        .limit(1)
    )
    cv = result.scalar_one_or_none()

    if cv is None:
        logger.debug(
            "No applied configuration version for drift check",
            workspace=ws.name,
        )
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
    now = now_utc()

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

                # Skip workspaces that are actively running terraform.
                # NOT "any non-terminal run" — `planned` runs awaiting
                # operator confirm can sit indefinitely and would have
                # blocked drift forever (production incident on the
                # mgmt deployment: workspaces with a `planned` run
                # awaiting confirm froze drift_status at the first
                # errored attempt; status column showed "applied" so
                # the issue was invisible until Health Issues lit up).
                if await _is_runner_busy(db, ws.id):
                    logger.debug("Skipping drift check: runner busy", workspace=ws.name)
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

        # Pin the run that produced this status so the workspace-list UI can
        # link the badge to it. Updated for every status the badge can show
        # (drifted / no_drift / errored) — that way the Errored badge also
        # links to the drift run that produced the error, not to whichever
        # earlier drift run set drift_latest_run_id last.
        ws.drift_latest_run_id = run.id
        ws.drift_last_checked_at = now_utc()
        await db.commit()

        logger.info(
            "Workspace drift status updated",
            workspace=ws.name,
            drift_status=ws.drift_status,
            run_id=run_id,
        )

        # Publish drift status change to SSE channels.
        #
        # The workspace LIST page subscribes to WORKSPACE_LIST_EVENTS_CHANNEL,
        # the admin dashboard to ADMIN_EVENTS_CHANNEL, and the workspace DETAIL
        # page to tp:run_events:{workspace_id}. When a drift run completes we
        # need to notify all three — without the per-workspace publish below,
        # the detail page only sees the earlier run_status_change event (fired
        # when the run transitioned to `planned`, before this handler ran) and
        # so its loadWorkspace() call reads the stale drift_status. Publishing
        # here too triggers a second refresh that picks up the updated status.
        try:
            from terrapod.redis.client import (
                ADMIN_EVENTS_CHANNEL,
                RUN_EVENTS_PREFIX,
                WORKSPACE_LIST_EVENTS_CHANNEL,
                publish_event,
            )

            drift_payload = json.dumps(
                {
                    "event": "drift_status_change",
                    "workspace_id": str(ws.id),
                    "drift_status": ws.drift_status,
                }
            )
            await publish_event(ADMIN_EVENTS_CHANNEL, drift_payload)
            await publish_event(WORKSPACE_LIST_EVENTS_CHANNEL, drift_payload)
            await publish_event(f"{RUN_EVENTS_PREFIX}{ws.id}", drift_payload)
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
