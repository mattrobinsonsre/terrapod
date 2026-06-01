"""Authenticated artifact download/upload endpoints for runner Jobs.

Runners authenticate with a short-lived runner token (HMAC-signed, scoped
to a single run). The token's run_id must match the path run_id.

Downloads return 302 redirects to presigned storage URLs.
Uploads accept raw bytes and write to storage directly.

Endpoints:
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/config      — download config archive
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/state        — download current state
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/plan-file    — download plan file
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/lock-file    — download .terraform.lock.hcl from plan
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/plan-log     — upload plan log
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/plan-file    — upload plan file
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/lock-file    — upload .terraform.lock.hcl from plan
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/plan-json-output — upload plan JSON
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/apply-log    — upload apply log
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/state        — upload new state
"""

import asyncio
import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_runner_for_run
from terrapod.config import settings
from terrapod.db.models import Run, StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.plan_summary import summarize_plan_json
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    apply_log_key,
    config_version_key,
    lock_file_key,
    plan_json_output_key,
    plan_log_key,
    plan_output_key,
    state_key,
)

router = APIRouter(tags=["run-artifacts"])
logger = get_logger(__name__)


async def _get_run(run_id: str, db: AsyncSession) -> Run:
    """Get a run by UUID string."""
    run = await db.get(Run, uuid.UUID(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


# ── Downloads (302 redirect to presigned GET URL) ────────────────────────


@router.get("/runs/{run_id}/artifacts/config")
async def download_config(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the configuration archive for a run."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    if not run.configuration_version_id:
        raise HTTPException(status_code=404, detail="No configuration version")

    storage = get_storage()
    key = config_version_key(str(run.workspace_id), str(run.configuration_version_id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/state")
async def download_state(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the current state for the run's workspace."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    result = await db.execute(
        select(StateVersion)
        .where(StateVersion.workspace_id == run.workspace_id)
        .order_by(StateVersion.serial.desc())
        .limit(1)
    )
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="No state version")

    storage = get_storage()
    key = state_key(str(run.workspace_id), str(sv.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/plan-file")
async def download_plan_file(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the plan file from the plan phase."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    storage = get_storage()
    key = plan_output_key(str(run.workspace_id), str(run.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/lock-file")
async def download_lock_file(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the `.terraform.lock.hcl` produced by the plan-phase init.

    Carried into the apply phase so apply's `terraform init` resolves to
    the same provider versions plan used, rather than re-evaluating the
    version constraint and potentially picking up a newer matching
    version published in the plan→apply window. See #306.

    The runner treats a 404/non-2xx here as a warning, not an error — the
    apply phase still works (with the today-behaviour drift risk) when
    the plan ran on an older runner that didn't upload a lock file.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    storage = get_storage()
    key = lock_file_key(str(run.workspace_id), str(run.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


# ── Uploads (receive body, write to storage) ─────────────────────────────


@router.put("/runs/{run_id}/artifacts/plan-log")
async def upload_plan_log(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the plan log."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_log_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/plan-file")
async def upload_plan_file(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the plan file."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_output_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/lock-file")
async def upload_lock_file(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the `.terraform.lock.hcl` produced by the plan-phase init.

    See `download_lock_file` for the rationale. The runner treats this
    upload as best-effort — a failure here just means the apply phase
    falls back to re-resolving providers (today's behaviour).
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = lock_file_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/plan-json-output")
async def upload_plan_json_output(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the structured JSON plan output (`tofu show -json tfplan`).

    Sets `runs.has_json_output = true` so plan responses can advertise
    the read URL with confidence (errored / older / failed-upload runs
    leave the flag at its default `false`).
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_json_output_key(str(run.workspace_id), str(run.id))
    # Order matters: write storage first, then flip the flag. If the
    # commit fails after a successful upload, the artifact is reachable
    # only via retention sweep — annoying, but better than the reverse,
    # which would advertise a URL pointing at nothing.
    await storage.put(key, body)
    run.has_json_output = True
    # Parse the plan in a thread so a multi-MB JSON doesn't block the
    # event loop. A parse failure leaves the count columns null — the
    # download URL is still served, just no UI summary.
    summary = await asyncio.to_thread(summarize_plan_json, body)
    if summary is not None:
        run.resource_additions = summary["additions"]
        run.resource_changes = summary["changes"]
        run.resource_destructions = summary["destructions"]
        run.resource_replacements = summary["replacements"]
        run.resource_imports = summary["imports"]
    else:
        logger.warning(
            "plan_json_output.summary_unparseable",
            run_id=str(run.id),
            workspace_id=str(run.workspace_id),
            body_bytes=len(body),
        )
    await db.commit()

    # AI plan summariser (#401) — enqueue the `plan_summary` kind now
    # that the JSON is actually in storage. Previously this fired from
    # run_service.transition_run on the planned transition, which
    # raced the runner: transition_run runs on the plan-result POST,
    # which the runner sends BEFORE uploading plan-json-output. The
    # summariser would then hit "Object not found" half the time and
    # write status='errored'. Firing here closes the race — by the
    # time the trigger is enqueued the storage put + db commit have
    # both succeeded. Failure-analysis kind still fires from
    # transition_run on errored runs (no JSON involved).
    if settings.ai_summary.enabled:
        try:
            from terrapod.services.scheduler import enqueue_trigger

            await enqueue_trigger(
                "ai_plan_summary",
                {"run_id": str(run.id), "kind": "plan_summary"},
                dedup_key=f"aisum:{run.id}:plan_summary",
                dedup_ttl=300,
            )
        except Exception as e:
            logger.debug("Failed to enqueue ai_plan_summary after upload", error=str(e))

    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/apply-log")
async def upload_apply_log(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the apply log."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = apply_log_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/state")
async def upload_state(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload new state after apply.

    Parses the uploaded state JSON, creates a StateVersion record, and
    stores the state at the canonical key so that subsequent plans can
    find it via the standard state download path.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()

    # Parse state JSON to extract metadata
    try:
        state_data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid state JSON") from exc

    serial = state_data.get("serial", 0)
    lineage = state_data.get("lineage", "")
    # Hash off the event loop — runner state uploads can be multi-MB
    md5 = await asyncio.to_thread(lambda: hashlib.md5(body).hexdigest())  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5

    # Reject duplicate-serial uploads with 409 Conflict instead of letting
    # the unique constraint surface as a 500. tofu doesn't bump the state
    # serial on a no-op apply, so a runner re-uploading the same state would
    # otherwise blow up with `IntegrityError on uq_state_versions`. The
    # reconciler short-circuits planned→applied for has_changes=False so
    # this path shouldn't be reached in steady state, but explicit 409 is
    # correct semantics regardless: a stale serial is a client-visible
    # conflict, not a server error.
    #
    # Two-layer check: (1) pre-INSERT lookup gives the common case a clean
    # 409 with a helpful message; (2) IntegrityError catch on the INSERT
    # closes the race window where a concurrent upload inserts between our
    # SELECT and INSERT — the unique constraint is the source of truth.
    _existing_serial_msg = (
        f"State serial {serial} already exists for this workspace. "
        "tofu apply did not bump the serial — likely a no-op apply that "
        "should not have produced a state upload."
    )
    existing = await db.execute(
        select(StateVersion).where(
            StateVersion.workspace_id == run.workspace_id,
            StateVersion.serial == serial,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=_existing_serial_msg)

    # Create StateVersion record
    sv = StateVersion(
        workspace_id=run.workspace_id,
        serial=serial,
        lineage=lineage,
        md5=md5,
        state_size=len(body),
        run_id=run.id,
        created_by=run.created_by or None,
    )
    db.add(sv)
    try:
        await db.flush()
    except IntegrityError:
        # Race: another upload inserted the same (workspace_id, serial)
        # between our SELECT and INSERT. Roll back so the session is
        # usable for any caller-side cleanup, then return 409.
        await db.rollback()
        raise HTTPException(status_code=409, detail=_existing_serial_msg) from None

    # Store at canonical key (same format used by download_state)
    storage = get_storage()
    key = state_key(str(run.workspace_id), str(sv.id))
    await storage.put(key, body)

    # Clear state_diverged flag on successful state upload
    ws = await db.get(Workspace, run.workspace_id)
    if ws and ws.state_diverged:
        ws.state_diverged = False

    await db.commit()
    logger.info(
        "state_version_created_from_runner",
        run_id=run_id,
        workspace_id=str(run.workspace_id),
        state_version_id=str(sv.id),
        serial=serial,
    )

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(run.workspace_id), "state_version_created")

    return Response(status_code=204)


@router.post("/runs/{run_id}/state-diverged")
async def mark_state_diverged(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Mark a workspace as having diverged state.

    Called by the runner entrypoint when a state upload fails after a
    successful apply. The workspace is flagged so the UI can warn users.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    ws.state_diverged = True
    await db.commit()

    logger.warning(
        "workspace_state_diverged",
        run_id=run_id,
        workspace_id=str(run.workspace_id),
    )

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(run.workspace_id), "state_diverged")

    return Response(status_code=204)
