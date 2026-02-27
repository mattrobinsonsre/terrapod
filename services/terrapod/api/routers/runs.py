"""Run CRUD and lifecycle endpoints (TFE V2 compatible).

Endpoints:
    POST   /api/v2/runs                              (create run)
    GET    /api/v2/runs/{run_id}                      (show run)
    GET    /api/v2/workspaces/{id}/runs               (list runs)
    POST   /api/v2/runs/{run_id}/actions/confirm      (confirm plan)
    POST   /api/v2/runs/{run_id}/actions/discard      (discard plan)
    POST   /api/v2/runs/{run_id}/actions/cancel       (cancel run)
    GET    /api/v2/runs/{run_id}/plan                 (plan details)
    GET    /api/v2/runs/{run_id}/apply                (apply details)
    PATCH  /api/v2/listeners/{id}/runs/{run_id}       (listener status update)
    GET    /api/v2/listeners/{id}/runs/next            (poll for next run)
"""

import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    from datetime import timezone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_json(run: Run) -> dict:
    """Serialize a Run to TFE V2 JSON:API format."""
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")
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
                "terraform-version": run.terraform_version,
                "error-message": run.error_message,
                "vcs-commit-sha": run.vcs_commit_sha,
                "vcs-branch": run.vcs_branch,
                "vcs-pull-request-number": run.vcs_pull_request_number,
                "status-timestamps": {
                    "plan-queued-at": _rfc3339(run.created_at),
                    "planning-at": _rfc3339(run.plan_started_at),
                    "planned-at": _rfc3339(run.plan_finished_at),
                    "applying-at": _rfc3339(run.apply_started_at),
                    "applied-at": _rfc3339(run.apply_finished_at),
                },
                "created-at": _rfc3339(run.created_at),
                "updated-at": _rfc3339(run.updated_at),
                "actions": {
                    "is-confirmable": run.status == "planned" and not run.auto_apply,
                    "is-discardable": run.status == "planned",
                    "is-cancelable": run.status not in run_service.TERMINAL_STATES,
                },
                "permissions": {
                    "can-apply": run.status == "planned",
                    "can-cancel": run.status not in run_service.TERMINAL_STATES,
                    "can-discard": run.status == "planned",
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

    # Check permission: plan-only requires plan, apply requires write
    plan_only = attrs.get("plan-only", False)
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
    )

    # If config version already uploaded or no config needed, queue immediately
    if cv_uuid is None:
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
    return JSONResponse(
        content={"data": [_run_json(r)["data"] for r in runs]}
    )


@router.post("/runs/{run_id}/actions/confirm")
async def confirm_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Confirm a planned run for apply. Requires write."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "write", user, db)
    try:
        run = await run_service.confirm_run(db, run)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
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
        raise HTTPException(status_code=409, detail=str(e))
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
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse(content=_run_json(run))


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


# ── Plan & Apply Details ─────────────────────────────────────────────────


@router.get("/runs/{run_id}/plan")
async def show_plan(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show plan details including log URL."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "read", user, db)
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")

    return JSONResponse(
        content={
            "data": {
                "id": f"plan-{run.id}",
                "type": "plans",
                "attributes": {
                    "status": _plan_status(run),
                    "log-read-url": f"{base}/api/v2/plans/{run.id}/log",
                    "has-changes": run.status in ("planned", "confirmed", "applying", "applied"),
                },
                "links": {
                    "self": f"/api/v2/runs/{run_id}/plan",
                },
            }
        }
    )


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
        return JSONResponse(content=None, status_code=204)

    # Generate presigned URLs for the run
    urls = await run_service.get_run_presigned_urls(db, run)
    await db.commit()

    run_data = _run_json(run)
    run_data["data"]["attributes"]["presigned-urls"] = urls

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

    if not target_status:
        raise HTTPException(status_code=422, detail="status is required")

    try:
        run = await run_service.transition_run(
            db, run, target_status, error_message=error_message
        )

        # Auto-apply if configured
        if target_status == "planned" and run.auto_apply and not run.plan_only:
            run = await run_service.transition_run(db, run, "confirmed")

        # Unlock workspace on terminal state
        if target_status in run_service.TERMINAL_STATES:
            ws = await db.get(Workspace, run.workspace_id)
            if ws and ws.locked:
                ws.locked = False
                ws.lock_id = None

        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return JSONResponse(content=_run_json(run))


# ── Log Streaming Endpoints ──────────────────────────────────────────────

# These endpoints serve raw log content compatible with the go-tfe LogReader
# protocol.  No auth — the URL is a capability token (matches presigned URL
# pattern; go-tfe's LogReader does not send Authorization headers).

_STX = b"\x02"
_ETX = b"\x03"

_POST_PLAN_STATES = frozenset({
    "planned", "confirmed", "applying", "applied",
    "errored", "discarded", "canceled",
})


@router.get("/plans/{plan_id}/log")
async def plan_log(
    plan_id: str = Path(...),
    offset: int = Query(0),
    limit: int = Query(65536),
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
    )


@router.get("/applies/{apply_id}/log")
async def apply_log(
    apply_id: str = Path(...),
    offset: int = Query(0),
    limit: int = Query(65536),
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
    )


async def _serve_log(
    run: Run,
    log_key: str,
    phase_complete_states: frozenset[str],
    offset: int,
    limit: int,
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

    chunk = data[offset : offset + limit]
    result = b""
    if offset == 0:
        result += _STX
    result += chunk
    # Append ETX if phase is done and this is the last chunk
    if phase_done and offset + limit >= len(data):
        result += _ETX
    return Response(content=result, media_type="text/plain")


# ── Apply URLs for Remote Listeners ─────────────────────────────────────


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
