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
    POST   /api/v2/runs/{run_id}/actions/apply        (confirm plan for apply)
    POST   /api/v2/runs/{run_id}/actions/discard      (discard plan)
    POST   /api/v2/runs/{run_id}/actions/cancel       (cancel run)
    POST   /api/v2/runs/{run_id}/actions/retry        (retry run — create new run from terminal run)
    GET    /api/v2/runs/{run_id}/plan                 (plan details)
    GET    /api/v2/plans/{plan_id}                    (plan details by ID)
    GET    /api/v2/runs/{run_id}/apply                (apply details)
    GET    /api/v2/applies/{apply_id}                 (apply details by ID)
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

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, get_listener_identity
from terrapod.db.models import Run, VCSConnection, Workspace
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


def _run_json(run: Run, *, workspace_has_vcs: bool = False) -> dict:
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
                "resource-cpu": run.resource_cpu,
                "resource-memory": run.resource_memory,
                "error-message": run.error_message,
                "target-addrs": run.target_addrs or [],
                "replace-addrs": run.replace_addrs or [],
                "refresh-only": run.refresh_only,
                "refresh": run.refresh,
                "allow-empty-apply": run.allow_empty_apply,
                "is-drift-detection": run.is_drift_detection,
                "has-changes": run.has_changes,
                "workspace-has-vcs": workspace_has_vcs,
                "module-overrides": run.module_overrides,
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
                    "is-cancelable": run.status not in run_service.TERMINAL_STATES
                    and not (run.plan_only and run.status == "planned"),
                    "is-retryable": run.status in run_service.TERMINAL_STATES
                    or (run.plan_only and run.status == "planned"),
                },
                "permissions": {
                    "can-apply": run.status == "planned" and not run.plan_only,
                    "can-cancel": run.status not in run_service.TERMINAL_STATES
                    and not (run.plan_only and run.status == "planned"),
                    "can-discard": run.status == "planned" and not run.plan_only,
                    "can-retry": run.status in run_service.TERMINAL_STATES
                    or (run.plan_only and run.status == "planned"),
                    "can-force-execute": False,
                    "can-force-cancel": False,
                },
            },
            "relationships": {
                "workspace": {
                    "data": {"id": f"ws-{run.workspace_id}", "type": "workspaces"},
                },
                "configuration-version": {
                    "data": (
                        {
                            "id": f"cv-{run.configuration_version_id}",
                            "type": "configuration-versions",
                        }
                        if run.configuration_version_id
                        else None
                    ),
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


async def _fetch_vcs_config(
    db: AsyncSession, ws: Workspace, *, ref_override: str = ""
) -> tuple[uuid.UUID, str, str]:
    """Download code from VCS and create a ConfigurationVersion.

    Used when the UI queues a run on a VCS-connected workspace without
    uploading code (no CLI config version). Replicates the same flow
    the VCS poller uses: resolve branch → get HEAD SHA → download
    tarball → create CV → upload → mark uploaded.

    When ref_override is set, fetches code from that branch/tag/SHA instead
    of the workspace's tracked branch.

    Returns (cv_id, commit_sha, ref_name).
    """
    from terrapod.services.vcs_poller import (
        _download_archive,
        _get_branch_sha,
        _list_tags,
        _parse_repo_url,
        _resolve_branch,
        _strip_top_level_dir,
    )
    from terrapod.storage import get_storage
    from terrapod.storage.keys import config_version_key

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if not conn or conn.status != "active":
        raise HTTPException(status_code=422, detail="VCS connection is not active")

    parsed = _parse_repo_url(conn, ws.vcs_repo_url)
    if not parsed:
        raise HTTPException(status_code=422, detail="Cannot parse VCS repo URL")
    owner, repo = parsed

    if ref_override:
        # Try as branch first, then tag, then treat as raw SHA
        sha = await _get_branch_sha(conn, owner, repo, ref_override)
        ref_name = ref_override
        if not sha:
            tags = await _list_tags(conn, owner, repo)
            tag_match = next((t for t in tags if t["name"] == ref_override), None)
            if tag_match:
                sha = tag_match["sha"]
            else:
                # Treat as raw SHA — download_archive accepts any git ref
                sha = ref_override
    else:
        ref_name = await _resolve_branch(conn, ws, owner, repo) or ""
        if not ref_name:
            raise HTTPException(status_code=422, detail="Cannot determine VCS branch")
        sha = await _get_branch_sha(conn, owner, repo, ref_name)
        if not sha:
            raise HTTPException(status_code=422, detail="Cannot get branch HEAD SHA")

    archive = await _download_archive(conn, owner, repo, sha)
    archive = _strip_top_level_dir(archive)

    cv = await run_service.create_configuration_version(
        db, workspace_id=ws.id, source="tfe-api", auto_queue_runs=False
    )
    await db.flush()

    storage = get_storage()
    key = config_version_key(str(ws.id), str(cv.id))
    await storage.put(key, archive, content_type="application/x-tar")

    cv = await run_service.mark_configuration_uploaded(db, cv)

    return cv.id, sha, ref_name


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

    # CLI-initiated runs on VCS-connected remote workspaces: plan is allowed,
    # apply is not — VCS is the source of truth. Non-VCS ("CLI-driven") remote
    # workspaces allow both plan and apply from the CLI.
    # The guard only applies when a configuration version is being uploaded
    # (CLI workflow). Runs without a CV (UI-queued, VCS, drift) get code from VCS.
    plan_only = attrs.get("plan-only", False)
    source = attrs.get("source", "tfe-api")
    cv_data = relationships.get("configuration-version", {}).get("data", {})
    has_cv = bool(cv_data.get("id", "") if cv_data else "")
    if (
        ws.execution_mode == "remote"
        and ws.vcs_connection_id is not None
        and source not in ("vcs", "drift-detection")
        and has_cv
    ):
        if not plan_only:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Apply is not allowed from the CLI on VCS-connected remote workspaces. "
                "Use 'tofu plan' for speculative plans, or trigger applies via VCS integration and/or the UI.",
            )
        plan_only = True

    # Check permission: plan-only requires plan, apply requires write
    required = "plan" if plan_only else "write"
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on workspace",
        )

    # Configuration version (optional — provided by CLI uploads)
    cv_data = relationships.get("configuration-version", {}).get("data", {})
    cv_id = cv_data.get("id", "") if cv_data else ""
    cv_uuid = None
    if cv_id:
        cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))

    # VCS ref override: plan against an arbitrary branch/tag (always plan-only)
    vcs_ref = attrs.get("vcs-ref", "")
    if vcs_ref:
        if not ws.vcs_connection_id or not ws.vcs_repo_url:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="vcs-ref can only be used on VCS-connected workspaces",
            )
        plan_only = True  # Server-side enforcement — non-default refs are always plan-only

    # If no config version provided and workspace has VCS, fetch code from VCS
    vcs_sha = ""
    vcs_branch = ""
    if cv_uuid is None and ws.vcs_connection_id and ws.vcs_repo_url:
        cv_uuid, vcs_sha, vcs_branch = await _fetch_vcs_config(db, ws, ref_override=vcs_ref)

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
        target_addrs=attrs.get("target-addrs"),
        replace_addrs=attrs.get("replace-addrs"),
        refresh_only=attrs.get("refresh-only", False),
        refresh=attrs.get("refresh", True),
        allow_empty_apply=attrs.get("allow-empty-apply", False),
    )

    # Attach VCS metadata if we fetched code from VCS
    if vcs_sha:
        run.vcs_commit_sha = vcs_sha
        run.vcs_branch = vcs_branch

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

    return JSONResponse(
        content=_run_json(run, workspace_has_vcs=ws.vcs_connection_id is not None),
        status_code=201,
    )


@router.get("/runs/{run_id}")
async def show_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a run. Requires read on workspace."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "read", user, db)
    ws = await db.get(Workspace, run.workspace_id)
    return JSONResponse(
        content=_run_json(run, workspace_has_vcs=bool(ws and ws.vcs_connection_id)),
    )


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
    has_vcs = ws.vcs_connection_id is not None
    return JSONResponse(
        content={"data": [_run_json(r, workspace_has_vcs=has_vcs)["data"] for r in runs]}
    )


@router.post("/runs/{run_id}/actions/apply")
async def confirm_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Confirm a planned run for apply. Requires write."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "write", user, db)

    # Block apply for CLI-uploaded code on VCS-connected remote workspaces
    if run.source not in ("vcs", "drift-detection"):
        ws = await db.get(Workspace, run.workspace_id)
        if ws and ws.execution_mode == "remote" and ws.vcs_connection_id is not None:
            raise HTTPException(
                status_code=422,
                detail="Apply is not supported for CLI-uploaded code on VCS-connected remote workspaces. "
                "Only VCS-managed code can be applied on VCS-connected workspaces.",
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

    is_terminal = run.status in run_service.TERMINAL_STATES or (
        run.plan_only and run.status == "planned"
    )
    if not is_terminal:
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
        target_addrs=run.target_addrs,
        replace_addrs=run.replace_addrs,
        refresh_only=run.refresh_only,
        refresh=run.refresh,
        allow_empty_apply=run.allow_empty_apply,
    )

    # Copy VCS metadata and module overrides from the original run
    new_run.vcs_commit_sha = run.vcs_commit_sha
    new_run.vcs_branch = run.vcs_branch
    new_run.vcs_pull_request_number = run.vcs_pull_request_number
    new_run.module_overrides = run.module_overrides

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


def _apply_json(run: Run) -> dict:
    """Build apply JSON:API response for a run."""
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")
    return {
        "data": {
            "id": f"apply-{run.id}",
            "type": "applies",
            "attributes": {
                "status": _apply_status(run),
                "log-read-url": f"{base}/api/v2/applies/{run.id}/log",
            },
            "links": {
                "self": f"/api/v2/applies/apply-{run.id}",
            },
        }
    }


@router.get("/applies/{apply_id}")
async def show_apply_by_id(
    apply_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show apply details by apply ID (go-tfe compatibility).

    go-tfe fetches applies via GET /api/v2/applies/{apply_id} during cloud runs.
    Apply IDs use the same UUID as the run with an 'apply-' prefix.
    """
    run = await _get_run(apply_id.replace("apply-", "run-"), db)
    await _require_run_ws_permission(run, "read", user, db)
    return JSONResponse(content=_apply_json(run))


@router.get("/runs/{run_id}/apply")
async def show_apply(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show apply details including log URL."""
    run = await _get_run(run_id, db)
    await _require_run_ws_permission(run, "read", user, db)
    return JSONResponse(content=_apply_json(run))


# ── SSE (Server-Sent Events) ─────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/runs/events")
async def run_events_stream(
    request: Request,
    workspace_id: str = Path(...),
) -> EventSourceResponse:
    """Stream run status change events via SSE for real-time UI updates.

    Uses short-lived DB session for auth/RBAC check, then releases it
    before entering the long-lived SSE streaming loop. This prevents
    holding a DB pool connection for the entire SSE connection lifetime.
    """
    from terrapod.api.dependencies import authenticate_request
    from terrapod.db.session import get_db_session
    from terrapod.redis.client import RUN_EVENTS_PREFIX, subscribe_channel

    user = await authenticate_request(request)

    async with get_db_session() as db:
        ws = await _get_workspace(workspace_id, db)
        perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
        if not has_permission(perm, "read"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires read permission on workspace",
            )
        ws_id = str(ws.id)

    channel = f"{RUN_EVENTS_PREFIX}{ws_id}"
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
    listener = await agent_pool_service.get_listener(l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")

    claim = await run_service.claim_next_run(
        db,
        listener_id=l_uuid,
        pool_id=uuid.UUID(listener["pool_id"]),
        listener_name=listener.get("name", ""),
    )
    if claim is None:
        return Response(status_code=204)

    run, phase = claim

    # Fetch workspace once for variables + var_files
    ws = await db.get(Workspace, run.workspace_id)

    # Resolve workspace variables for injection into the runner Job
    from terrapod.services.variable_service import resolve_variables

    resolved = await resolve_variables(db, run.workspace_id)
    env_vars = [{"key": v.key, "value": v.value} for v in resolved if v.category == "env"]
    terraform_vars = [
        {"key": v.key, "value": v.value} for v in resolved if v.category == "terraform"
    ]

    await db.commit()

    run_data = _run_json(run)
    run_data["data"]["attributes"]["env-vars"] = env_vars
    run_data["data"]["attributes"]["terraform-vars"] = terraform_vars
    run_data["data"]["attributes"]["var-files"] = ws.var_files if ws and ws.var_files else []
    run_data["data"]["attributes"]["working-directory"] = ws.working_directory if ws else ""
    run_data["data"]["attributes"]["phase"] = phase

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


# ── Runner Token ──────────────────────────────────────────────────────


@router.post("/listeners/{listener_id}/runs/{run_id}/runner-token")
async def create_runner_token(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(default={}),
    listener: object = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Generate a short-lived runner token for a run.

    Called by the listener after claiming a run. The token authenticates
    runner Job API calls (binary cache, provider mirror, artifact upload/download).
    """
    from terrapod.auth.runner_tokens import generate_runner_token
    from terrapod.config import load_runner_config

    run = await _get_run(run_id, db)

    # Verify this listener owns the run
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    config = load_runner_config()
    requested_ttl = body.get("ttl", config.token_ttl_seconds)
    token = generate_runner_token(run.id, ttl=requested_ttl)

    # Compute actual TTL (may have been clamped)
    max_ttl = config.max_token_ttl_seconds
    actual_ttl = min(requested_ttl, max_ttl) if max_ttl > 0 else requested_ttl

    return JSONResponse(content={"token": token, "expires_in": actual_ttl})


# ── Job Lifecycle Callbacks ───────────────────────────────────────────────


@router.post("/listeners/{listener_id}/runs/{run_id}/job-launched")
async def report_job_launched(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Listener reports Job creation for a run.

    After the listener creates a K8s Job + auth Secret, it calls this endpoint
    to register the Job name and namespace. The API uses this to track the Job
    and query its status via the reconciler.
    """
    run = await _get_run(run_id, db)

    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    job_name = body.get("job_name", "")
    job_namespace = body.get("job_namespace", "")
    if not job_name:
        raise HTTPException(status_code=422, detail="job_name is required")

    run.job_name = job_name
    run.job_namespace = job_namespace
    await db.commit()

    return JSONResponse(content={"status": "ok"})


@router.post("/listeners/{listener_id}/runs/{run_id}/job-status")
async def report_job_status(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Listener reports Job status in response to a check_job_status event.

    The reconciler publishes check_job_status events via SSE. The listener
    queries K8s for the Job status and POSTs the result here. The reconciler
    picks up the status from Redis on its next cycle.
    """
    run = await _get_run(run_id, db)

    job_status = body.get("status", "")
    if not job_status:
        raise HTTPException(status_code=422, detail="status is required")

    phase = body.get("phase", "plan")

    from terrapod.redis.client import set_job_status

    await set_job_status(str(run.id), phase, job_status)

    return JSONResponse(content={"status": "ok"})


@router.put("/listeners/{listener_id}/runs/{run_id}/log-stream")
async def upload_log_stream(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    phase: str = Query("plan"),
    request: Request = ...,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Listener uploads live pod log data for an in-progress run.

    The reconciler publishes stream_logs events via SSE. The listener reads
    pod logs from K8s and PUTs them here. The API stores the data in Redis
    and serves it from the log endpoints until the final log is uploaded to
    object storage by the runner Job.

    Phase is passed explicitly via query param to prevent late-arriving plan
    log data from being stored under the apply phase key when the run has
    already transitioned.
    """
    run = await _get_run(run_id, db)
    if phase not in ("plan", "apply"):
        phase = "plan" if run.status == "planning" else "apply"
    body_bytes = await request.body()

    from terrapod.redis.client import (
        LOG_STREAM_PREFIX,
        RUN_EVENTS_PREFIX,
        get_redis_client,
        publish_event,
    )

    redis = get_redis_client()
    await redis.setex(f"{LOG_STREAM_PREFIX}{run.id}:{phase}", 300, body_bytes)

    # Notify frontend that fresh log data is available
    try:
        payload = json.dumps(
            {
                "event": "log_updated",
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
                "phase": phase,
            }
        )
        await publish_event(f"{RUN_EVENTS_PREFIX}{run.workspace_id}", payload)
    except Exception:
        pass  # Never let SSE publishing break the log upload

    return Response(status_code=204)


@router.post("/runs/{run_id}/plan-result")
async def report_plan_result(
    run_id: str = Path(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Runner Job reports plan has_changes result.

    Called by the runner entrypoint after plan completes. The has_changes
    value is used by the reconciler to set the run's has_changes field.
    """
    run = await _get_run(run_id, db)

    has_changes = body.get("has_changes")
    if has_changes is not None:
        run.has_changes = has_changes
        await db.commit()

    return JSONResponse(content={"status": "ok"})


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
    limit: int = Query(0),
    format: Literal["raw", "plain"] = Query("raw"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream plan log content (go-tfe LogReader compatible)."""
    try:
        run_uuid = uuid.UUID(plan_id.removeprefix("plan-").removeprefix("run-"))
    except ValueError:
        raise HTTPException(status_code=404, detail="Plan not found") from None
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
    limit: int = Query(0),
    format: Literal["raw", "plain"] = Query("raw"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream apply log content (go-tfe LogReader compatible)."""
    try:
        run_uuid = uuid.UUID(apply_id.removeprefix("apply-").removeprefix("run-"))
    except ValueError:
        raise HTTPException(status_code=404, detail="Apply not found") from None
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
        phase="apply",
    )


_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")


async def _serve_log(
    run: Run,
    log_key: str,
    phase_complete_states: frozenset[str],
    offset: int,
    limit: int,
    strip_ansi: bool = False,
    phase: str = "plan",
) -> Response:
    """Shared log serving logic with STX/ETX framing.

    Data source priority:
    1. Object storage (authoritative — final log uploaded by Job on completion)
    2. Redis live stream (live-streamed data from listener during execution)
    3. Empty response (no data available yet — client retries)
    """
    storage = get_storage()
    phase_done = run.status in phase_complete_states

    try:
        data = await storage.get(log_key)
    except ObjectNotFoundError:
        # Try live-streamed data from Redis (available for both in-progress
        # and recently-completed runs where the Job didn't upload final logs)
        try:
            from terrapod.redis.client import LOG_STREAM_PREFIX, get_redis_client

            redis = get_redis_client()
            live_data = await redis.get(f"{LOG_STREAM_PREFIX}{run.id}:{phase}")
            if live_data is not None:
                if isinstance(live_data, str):
                    live_data = live_data.encode()
                data = live_data
            elif phase_done:
                # Phase finished, no log in storage or Redis — empty stream
                return Response(content=_STX + _ETX, media_type="text/plain")
            else:
                # Still running, no log yet — return empty (client retries)
                return Response(content=b"", media_type="text/plain")
        except Exception:
            if phase_done:
                return Response(content=_STX + _ETX, media_type="text/plain")
            return Response(content=b"", media_type="text/plain")

    if strip_ansi:
        data = _ANSI_RE.sub(b"", data)

    if limit > 0:
        chunk = data[offset : offset + limit]
    else:
        chunk = data[offset:]
    result = b""
    if offset == 0:
        result += _STX
    result += chunk
    # Append ETX if phase is done and this is the last chunk
    if phase_done and (limit == 0 or offset + limit >= len(data)):
        result += _ETX
    return Response(content=result, media_type="text/plain")
