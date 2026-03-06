"""Run CRUD and lifecycle endpoints (TFE V2 compatible).

UX CONTRACT: Run endpoints are consumed by the web frontend:
  - web/src/app/workspaces/[id]/page.tsx (runs tab: list, create)
  - web/src/app/workspaces/[id]/runs/[runId]/page.tsx (run detail, logs, confirm/discard/cancel)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to those frontend pages.

Endpoints:
    POST   /api/v2/runs                              (create run)
    GET    /api/v2/runs/{run_id}                      (show run)
    GET    /api/v2/workspaces/{id}/runs               (list runs)
    POST   /api/v2/runs/{run_id}/actions/confirm      (confirm plan)
    POST   /api/v2/runs/{run_id}/actions/discard      (discard plan)
    POST   /api/v2/runs/{run_id}/actions/cancel       (cancel run)
    POST   /api/v2/runs/{run_id}/actions/retry        (retry run — create new run from terminal run)
    GET    /api/v2/runs/{run_id}/plan                 (plan details)
    GET    /api/v2/runs/{run_id}/apply                (apply details)
    PATCH  /api/v2/listeners/{id}/runs/{run_id}       (listener status update)
    GET    /api/v2/listeners/{id}/runs/next            (poll for next run)
"""

import asyncio
import json
import re
import uuid
from datetime import UTC
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import agent_pool_service, run_service
from terrapod.services.workspace_rbac_service import has_permission, resolve_workspace_permission
from terrapod.storage import get_storage
from terrapod.storage.keys import apply_log_key, plan_log_key
from terrapod.storage.protocol import ObjectNotFoundError

router = APIRouter(prefix="/api/v2", tags=["runs"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_json(run: Run) -> dict:
    """Serialize a Run to TFE V2 JSON:API format."""
    run_id = f"run-{run.id}"

    return {
        "data": {
            "id": run_id,
            "type": "runs",
            "attributes": {
                "status": run.status,
                "message": run.message,
                "is-destroy": run.is_destroy,
                "auto-apply": run.auto_apply,
                "plan-only": run.plan_only,
                "source": run.source,
                "execution-backend": run.execution_backend,
                "terraform-version": run.terraform_version,
                "error-message": run.error_message,
                "is-drift-detection": run.is_drift_detection,
                "has-changes": run.has_changes,
                "vcs-commit-sha": run.vcs_commit_sha,
                "vcs-branch": run.vcs_branch,
                "vcs-pull-request-number": run.vcs_pull_request_number,
                "status-timestamps": {
                    k: v
                    for k, v in {
                        "plan-queued-at": _rfc3339(run.created_at),
                        "planning-at": _rfc3339(run.plan_started_at),
                        "planned-at": _rfc3339(run.plan_finished_at),
                        "applying-at": _rfc3339(run.apply_started_at),
                        "applied-at": _rfc3339(run.apply_finished_at),
                    }.items()
                    if v
                },
                "created-at": _rfc3339(run.created_at),
                "updated-at": _rfc3339(run.updated_at),
                "actions": {
                    "is-confirmable": run.status == "planned"
                    and not run.auto_apply
                    and not run.plan_only,
                    "is-discardable": run.status == "planned" and not run.plan_only,
                    "is-cancelable": run.status not in run_service.TERMINAL_STATES,
                    "is-retryable": run.status in run_service.TERMINAL_STATES,
                },
                "permissions": {
                    "can-apply": run.status == "planned" and not run.plan_only,
                    "can-cancel": run.status not in run_service.TERMINAL_STATES,
                    "can-discard": run.status == "planned" and not run.plan_only,
                    "can-retry": run.status in run_service.TERMINAL_STATES,
                    "can-force-execute": False,
                    "can-force-cancel": False,
                },
            },
            "relationships": {
                "workspace": {
                    "data": {"id": f"ws-{run.workspace_id}", "type": "workspaces"},
                },
                "plan": {
                    "data": {"id": f"plan-{run.id}", "type": "plans"},
                },
                "apply": {
                    "data": {"id": f"apply-{run.id}", "type": "applies"},
                },
                "task-stages": {
                    "links": {"related": f"/api/v2/runs/{run_id}/task-stages"},
                },
            },
            "links": {
                "self": f"/api/v2/runs/{run_id}",
            },
        }
    }


async def _get_run(run_id: str, db: AsyncSession) -> Run:
    run_uuid = uuid.UUID(run_id.removeprefix("run-"))
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


async def _require_run_ws_permission(
    run: Run, required: str, user: AuthenticatedUser, db: AsyncSession
) -> None:
    """Check that user has the required permission on the run's workspace."""
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on workspace",
        )


@router.post("/runs", status_code=201)
async def create_run(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new run. Plan-only requires plan; apply requires write."""
    attrs = body.get("data", {}).get("attributes", {})
    relationships = body.get("data", {}).get("relationships", {})

    ws_data = relationships.get("workspace", {}).get("data", {})
    ws_id = ws_data.get("id", "")
    if not ws_id:
        raise HTTPException(status_code=422, detail="Workspace relationship is required")

    ws = await _get_workspace(ws_id, db)

    # CLI-initiated runs in remote mode are always plan-only.
    # Only VCS-sourced runs are allowed to apply.
    plan_only = attrs.get("plan-only", False)
    source = attrs.get("source", "tfe-api")
    if ws.execution_mode == "remote" and source not in ("vcs", "drift-detection"):
        plan_only = True

    # Check permission: plan-only requires plan, apply requires write
    required = "plan" if plan_only else "write"
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on workspace",
        )

    # Configuration version (optional)
    cv_data = relationships.get("configuration-version", {}).get("data", {})
    cv_id = cv_data.get("id", "") if cv_data else ""
    cv_uuid = None
    if cv_id:
        cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))

    run = await run_service.create_run(
        db,
        workspace=ws,
        message=attrs.get("message", ""),
        is_destroy=attrs.get("is-destroy", False),
        auto_apply=attrs.get("auto-apply"),
        plan_only=plan_only,
        source=attrs.get("source", "tfe-api"),
        terraform_version=attrs.get("terraform-version", ""),
        configuration_version_id=cv_uuid,
        created_by=user.email,
        is_drift_detection=attrs.get("is-drift-detection", False),
    )

    # Queue immediately if no config needed, or config already uploaded
    if cv_uuid is None:
        run = await run_service.queue_run(db, run)
    else:
        from terrapod.db.models import ConfigurationVersion

        cv = await db.get(ConfigurationVersion, cv_uuid)
        if cv and cv.status == "uploaded":
            run = await run_service.queue_run(db, run)

    await db.commit()
    await db.refresh(run)

    return JSONResponse(content=_run_json(run), status_code=201)


@router.get("/runs/{run_id}")
async def show_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a run. Requires read on workspace."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "read", user, db)
    return JSONResponse(content=_run_json(run))


@router.get("/workspaces/{workspace_id}/runs")
async def list_workspace_runs(
    workspace_id: str = Path(...),
    page_number: int = Query(1, alias="page[number]"),
    page_size: int = Query(20, alias="page[size]"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List runs for a workspace. Requires read."""
    ws = await _get_workspace(workspace_id, db)
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires read permission on workspace",
        )
    runs = await run_service.list_workspace_runs(db, ws.id, page_number, page_size)
    return JSONResponse(content={"data": [_run_json(r)["data"] for r in runs]})


@router.post("/runs/{run_id}/actions/confirm")
async def confirm_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Confirm a planned run for apply. Requires write."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "write", user, db)

    # Block apply for CLI-uploaded code in remote execution mode
    if run.source not in ("vcs", "drift-detection"):
        ws = await db.get(Workspace, run.workspace_id)
        if ws and ws.execution_mode == "remote":
            raise HTTPException(
                status_code=422,
                detail="Apply is not supported for CLI-uploaded code in remote execution mode. Only VCS-managed code can be applied.",
            )

    try:
        run = await run_service.confirm_run(db, run)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return JSONResponse(content=_run_json(run))


@router.post("/runs/{run_id}/actions/discard")
async def discard_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Discard a planned run. Requires plan."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "plan", user, db)
    try:
        run = await run_service.discard_run(db, run)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return JSONResponse(content=_run_json(run))


@router.post("/runs/{run_id}/actions/cancel")
async def cancel_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Cancel a run. Requires plan."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "plan", user, db)
    try:
        run = await run_service.cancel_run(db, run)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return JSONResponse(content=_run_json(run))


@router.post("/runs/{run_id}/actions/retry")
async def retry_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Retry a terminal run by creating a new run with the same parameters.

    Creates a new run for the same workspace using the same configuration
    version, VCS metadata, and settings as the original run. Only terminal
    runs (errored, canceled, discarded, applied, planned plan-only) can be retried.
    Requires plan permission.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "plan", user, db)

    if run.status not in run_service.TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry run in non-terminal state '{run.status}'",
        )

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    new_run = await run_service.create_run(
        db,
        workspace=ws,
        message=f"Retry of run-{run.id}",
        source=run.source,
        plan_only=run.plan_only,
        configuration_version_id=run.configuration_version_id,
        created_by=user.email,
    )

    # Copy VCS metadata from the original run
    new_run.vcs_commit_sha = run.vcs_commit_sha
    new_run.vcs_branch = run.vcs_branch
    new_run.vcs_pull_request_number = run.vcs_pull_request_number

    new_run = await run_service.queue_run(db, new_run)
    await db.commit()

    return JSONResponse(content=_run_json(new_run), status_code=201)


# ── Phase Status Mapping ─────────────────────────────────────────────────


def _plan_status(run: Run) -> str:
    """Map run status to go-tfe plan phase status."""
    s = run.status
    if s in ("pending", "queued"):
        return "pending"
    if s == "planning":
        return "running"
    if s in ("planned", "confirmed", "applying", "applied"):
        return "finished"
    if s == "errored":
        # Errored during plan phase (plan never finished)
        if run.plan_finished_at is None:
            return "errored"
        return "finished"
    if s in ("canceled", "discarded"):
        return "canceled"
    return s


def _apply_status(run: Run) -> str:
    """Map run status to go-tfe apply phase status."""
    s = run.status
    if s in ("pending", "queued", "planning", "planned"):
        return "unreachable"
    if s == "confirmed":
        return "pending"
    if s == "applying":
        return "running"
    if s == "applied":
        return "finished"
    if s == "errored":
        # Errored during apply phase (apply was started but never finished)
        if run.apply_started_at and not run.apply_finished_at:
            return "errored"
        return "unreachable"
    if s in ("canceled", "discarded"):
        return "canceled"
    return s


# ── Run Events (go-tfe compatibility) ────────────────────────────────────


@router.get("/runs/{run_id}/run-events")
async def list_run_events(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List run events (status transitions) for go-tfe compatibility.

    go-tfe uses this endpoint to track run progress during cloud runs.
    We synthesize events from the run's status timestamps.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "read", user, db)

    events = []
    event_pairs = [
        ("queued", run.created_at),
        ("planning", run.plan_started_at),
        ("planned", run.plan_finished_at),
        ("applying", run.apply_started_at),
        ("applied", run.apply_finished_at),
    ]

    for i, (action, ts) in enumerate(event_pairs):
        if ts is None:
            continue
        events.append(
            {
                "id": f"re-{run.id}-{i}",
                "type": "run-events",
                "attributes": {
                    "action": action,
                    "created-at": _rfc3339(ts),
                },
                "relationships": {
                    "run": {"data": {"id": f"run-{run.id}", "type": "runs"}},
                },
            }
        )

    return JSONResponse(content={"data": events})


# ── Plan & Apply Details ─────────────────────────────────────────────────


def _plan_json(run: Run) -> dict:
    """Build plan JSON:API response for a run."""
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")
    return {
        "data": {
            "id": f"plan-{run.id}",
            "type": "plans",
            "attributes": {
                "status": _plan_status(run),
                "log-read-url": f"{base}/api/v2/plans/{run.id}/log",
                "has-changes": run.status in ("planned", "confirmed", "applying", "applied"),
            },
            "links": {
                "self": f"/api/v2/plans/plan-{run.id}",
            },
        }
    }


@router.get("/plans/{plan_id}")
async def show_plan_by_id(
    plan_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show plan details by plan ID (go-tfe compatibility).

    go-tfe fetches plans via GET /api/v2/plans/{plan_id} during cloud runs.
    Plan IDs use the same UUID as the run with a 'plan-' prefix.
    """
    run = await _get_run(plan_id.replace("plan-", "run-"), db)
    await _require_run_ws_permission(run, "read", user, db)
    return JSONResponse(content=_plan_json(run))


@router.get("/runs/{run_id}/plan")
async def show_plan(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show plan details including log URL."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "read", user, db)
    return JSONResponse(content=_plan_json(run))


@router.get("/runs/{run_id}/apply")
async def show_apply(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show apply details including log URL."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "read", user, db)
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")

    return JSONResponse(
        content={
            "data": {
                "id": f"apply-{run.id}",
                "type": "applies",
                "attributes": {
                    "status": _apply_status(run),
                    "log-read-url": f"{base}/api/v2/applies/{run.id}/log",
                },
                "links": {
                    "self": f"/api/v2/runs/{run_id}/apply",
                },
            }
        }
    )


# ── SSE (Server-Sent Events) ─────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/runs/events")
async def run_events_stream(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventSourceResponse:
    """Stream run status change events via SSE for real-time UI updates."""
    ws = await _get_workspace(workspace_id, db)
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires read permission on workspace",
        )

    from terrapod.redis.client import RUN_EVENTS_PREFIX, subscribe_channel

    channel = f"{RUN_EVENTS_PREFIX}{ws.id}"
    pubsub = await subscribe_channel(channel)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    payload = json.loads(data)
                    yield {
                        "event": payload.get("event", "run_status_change"),
                        "data": json.dumps(payload),
                    }
                else:
                    # Send keepalive comment every cycle when no messages
                    yield {"comment": "keepalive"}
                    await asyncio.sleep(1)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


# ── Listener Run Queue ───────────────────────────────────────────────────


@router.get("/listeners/{listener_id}/runs/next")
async def next_run(
    listener_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Poll for the next queued run assigned to this listener.

    Returns 204 No Content if no run is available.
    """
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    listener = await agent_pool_service.get_listener(db, l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")

    run = await run_service.claim_next_run(db, listener)
    if run is None:
        return Response(status_code=204)

    # Generate presigned URLs for the run
    urls = await run_service.get_run_presigned_urls(db, run)

    # Resolve workspace variables for injection into the runner Job
    from terrapod.services.variable_service import resolve_variables

    resolved = await resolve_variables(db, run.workspace_id)
    env_vars = [{"key": v.key, "value": v.value} for v in resolved if v.category == "env"]
    terraform_vars = [
        {"key": v.key, "value": v.value} for v in resolved if v.category == "terraform"
    ]

    await db.commit()

    run_data = _run_json(run)
    run_data["data"]["attributes"]["presigned-urls"] = urls
    run_data["data"]["attributes"]["env-vars"] = env_vars
    run_data["data"]["attributes"]["terraform-vars"] = terraform_vars

    return JSONResponse(content=run_data)


@router.patch("/listeners/{listener_id}/runs/{run_id}")
async def update_run_status(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Listener reports run status update."""
    run = await _get_run(run_id, db)

    # Verify this listener owns the run
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    target_status = body.get("status", "")
    error_message = body.get("error_message", "")
    has_changes = body.get("has_changes")

    if not target_status:
        raise HTTPException(status_code=422, detail="status is required")

    # Set has_changes before transition (so it's visible in drift handler)
    if has_changes is not None:
        run.has_changes = has_changes

    try:
        run = await run_service.transition_run(db, run, target_status, error_message=error_message)

        # Auto-apply if configured
        if target_status == "planned" and run.auto_apply and not run.plan_only:
            run = await run_service.transition_run(db, run, "confirmed")

        # Unlock workspace when plan-only run reaches planned
        # (plan-only runs don't mutate state, so no need to hold the lock)
        if target_status == "planned" and run.plan_only:
            ws = await db.get(Workspace, run.workspace_id)
            if ws and ws.locked:
                ws.locked = False
                ws.lock_id = None

        # Unlock workspace on terminal state
        if target_status in run_service.TERMINAL_STATES:
            ws = await db.get(Workspace, run.workspace_id)
            if ws and ws.locked:
                ws.locked = False
                ws.lock_id = None

        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return JSONResponse(content=_run_json(run))


# ── Log Streaming Endpoints ──────────────────────────────────────────────

# These endpoints serve raw log content compatible with the go-tfe LogReader
# protocol.  No auth — the URL is a capability token (matches presigned URL
# pattern; go-tfe's LogReader does not send Authorization headers).

_STX = b"\x02"
_ETX = b"\x03"

_POST_PLAN_STATES = frozenset(
    {
        "planned",
        "confirmed",
        "applying",
        "applied",
        "errored",
        "discarded",
        "canceled",
    }
)


@router.get("/plans/{plan_id}/log")
async def plan_log(
    plan_id: str = Path(...),
    offset: int = Query(0),
    limit: int = Query(65536),
    format: Literal["raw", "plain"] = Query("raw"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream plan log content (go-tfe LogReader compatible)."""
    run_uuid = uuid.UUID(plan_id.removeprefix("plan-"))
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    return await _serve_log(
        run=run,
        log_key=plan_log_key(str(run.workspace_id), str(run.id)),
        phase_complete_states=_POST_PLAN_STATES,
        offset=offset,
        limit=limit,
        strip_ansi=format == "plain",
    )


@router.get("/applies/{apply_id}/log")
async def apply_log(
    apply_id: str = Path(...),
    offset: int = Query(0),
    limit: int = Query(65536),
    format: Literal["raw", "plain"] = Query("raw"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream apply log content (go-tfe LogReader compatible)."""
    run_uuid = uuid.UUID(apply_id.removeprefix("apply-"))
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Apply not found")

    return await _serve_log(
        run=run,
        log_key=apply_log_key(str(run.workspace_id), str(run.id)),
        phase_complete_states=frozenset({"applied", "errored", "discarded", "canceled"}),
        offset=offset,
        limit=limit,
        strip_ansi=format == "plain",
    )


_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")


async def _serve_log(
    run: Run,
    log_key: str,
    phase_complete_states: frozenset[str],
    offset: int,
    limit: int,
    strip_ansi: bool = False,
) -> Response:
    """Shared log serving logic with STX/ETX framing."""
    storage = get_storage()
    phase_done = run.status in phase_complete_states

    try:
        data = await storage.get(log_key)
    except ObjectNotFoundError:
        if phase_done:
            # Phase finished but no log — return empty complete stream
            return Response(content=_STX + _ETX, media_type="text/plain")
        # Still running, no log yet — return empty (client retries)
        return Response(content=b"", media_type="text/plain")

    if strip_ansi:
        data = _ANSI_RE.sub(b"", data)

    chunk = data[offset : offset + limit]
    result = b""
    if offset == 0:
        result += _STX
    result += chunk
    # Append ETX if phase is done and this is the last chunk
    if phase_done and offset + limit >= len(data):
        result += _ETX
    return Response(content=result, media_type="text/plain")


# ── Presigned URLs for Listeners ────────────────────────────────────────


@router.get("/listeners/{listener_id}/runs/{run_id}/plan-urls")
async def get_plan_urls(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get presigned URLs for the plan phase."""
    run = await _get_run(run_id, db)

    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    urls = await run_service.get_run_presigned_urls(db, run)
    return JSONResponse(content=urls)


@router.get("/listeners/{listener_id}/runs/{run_id}/apply-urls")
async def get_apply_urls(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get presigned URLs for the apply phase."""
    run = await _get_run(run_id, db)

    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    urls = await run_service.get_apply_presigned_urls(db, run)
    return JSONResponse(content=urls)
