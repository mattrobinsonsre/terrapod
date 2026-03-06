"""Run task CRUD, task stage viewing, callback, and override endpoints.

UX CONTRACT: Run task endpoints are consumed by the web frontend:
  - web/src/app/workspaces/[id]/page.tsx (run tasks tab)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.

Endpoints:
    POST   /api/v2/workspaces/{id}/run-tasks          (create)
    GET    /api/v2/workspaces/{id}/run-tasks            (list)
    GET    /api/v2/run-tasks/{id}                       (show)
    PATCH  /api/v2/run-tasks/{id}                       (update)
    DELETE /api/v2/run-tasks/{id}                       (delete)
    GET    /api/v2/runs/{run_id}/task-stages             (list stages for a run)
    GET    /api/v2/task-stages/{id}                      (show stage with results)
    POST   /api/v2/task-stages/{id}/actions/override     (override failed stage)
    PATCH  /api/v2/task-stage-results/{id}/callback      (external callback)
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import Run, RunTask, TaskStage, TaskStageResult, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.run_task_service import (
    VALID_ENFORCEMENT_LEVELS,
    VALID_STAGES,
    get_task_stage,
    get_task_stage_result,
    resolve_stage,
    verify_callback_token,
)
from terrapod.services.workspace_rbac_service import has_permission, resolve_workspace_permission

router = APIRouter(prefix="/api/v2", tags=["run-tasks"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:  # type: ignore[no-untyped-def]
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_task_json(rt: RunTask) -> dict:
    """Serialize a RunTask to TFE V2 JSON:API format."""
    rt_id = f"task-{rt.id}"
    return {
        "id": rt_id,
        "type": "run-tasks",
        "attributes": {
            "name": rt.name,
            "url": rt.url,
            "enabled": rt.enabled,
            "stage": rt.stage,
            "enforcement-level": rt.enforcement_level,
            "has-hmac-key": rt.hmac_key is not None and rt.hmac_key != "",
            "created-at": _rfc3339(rt.created_at),
            "updated-at": _rfc3339(rt.updated_at),
        },
        "relationships": {
            "workspace": {
                "data": {"id": f"ws-{rt.workspace_id}", "type": "workspaces"},
            },
        },
        "links": {
            "self": f"/api/v2/run-tasks/{rt_id}",
        },
    }


def _task_stage_result_json(tsr: TaskStageResult) -> dict:
    """Serialize a TaskStageResult."""
    return {
        "id": f"tsr-{tsr.id}",
        "type": "task-stage-results",
        "attributes": {
            "status": tsr.status,
            "message": tsr.message,
            "started-at": _rfc3339(tsr.started_at),
            "finished-at": _rfc3339(tsr.finished_at),
            "created-at": _rfc3339(tsr.created_at),
        },
        "relationships": {
            "run-task": {
                "data": {"id": f"task-{tsr.run_task_id}", "type": "run-tasks"}
                if tsr.run_task_id
                else None,
            },
            "task-stage": {
                "data": {"id": f"ts-{tsr.task_stage_id}", "type": "task-stages"},
            },
        },
    }


def _task_stage_json(ts: TaskStage) -> dict:
    """Serialize a TaskStage with results."""
    return {
        "id": f"ts-{ts.id}",
        "type": "task-stages",
        "attributes": {
            "stage": ts.stage,
            "status": ts.status,
            "created-at": _rfc3339(ts.created_at),
            "updated-at": _rfc3339(ts.updated_at),
        },
        "relationships": {
            "run": {
                "data": {"id": f"run-{ts.run_id}", "type": "runs"},
            },
            "task-stage-results": {
                "data": [
                    {"id": f"tsr-{r.id}", "type": "task-stage-results"}
                    for r in (ts.results if hasattr(ts, "results") and ts.results else [])
                ],
            },
        },
        "included": [
            _task_stage_result_json(r)
            for r in (ts.results if hasattr(ts, "results") and ts.results else [])
        ],
    }


# ── Helpers ───────────────────────────────────────────────────────────


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


async def _require_ws_permission(
    ws: Workspace, required: str, user: AuthenticatedUser, db: AsyncSession
) -> None:
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on workspace",
        )


async def _get_run_task(rt_id: str, db: AsyncSession) -> RunTask:
    rt_uuid = uuid.UUID(rt_id.removeprefix("task-"))
    result = await db.execute(
        select(RunTask).options(selectinload(RunTask.workspace)).where(RunTask.id == rt_uuid)
    )
    rt = result.scalar_one_or_none()
    if rt is None:
        raise HTTPException(status_code=404, detail="Run task not found")
    return rt


# ── Run Task CRUD ─────────────────────────────────────────────────────


@router.post("/workspaces/{workspace_id}/run-tasks", status_code=201)
async def create_run_task(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a run task. Requires admin on the workspace."""
    ws = await _get_workspace(workspace_id, db)
    await _require_ws_permission(ws, "admin", user, db)

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    url = attrs.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="url is required")

    stage = attrs.get("stage", "")
    if stage not in VALID_STAGES:
        raise HTTPException(
            status_code=422,
            detail=f"stage must be one of: {', '.join(sorted(VALID_STAGES))}",
        )

    enforcement = attrs.get("enforcement-level", "mandatory")
    if enforcement not in VALID_ENFORCEMENT_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"enforcement-level must be one of: {', '.join(sorted(VALID_ENFORCEMENT_LEVELS))}",
        )

    hmac_key = attrs.get("hmac-key", "") or None

    rt = RunTask(
        workspace_id=ws.id,
        name=name,
        url=url,
        hmac_key=hmac_key,
        enabled=attrs.get("enabled", True),
        stage=stage,
        enforcement_level=enforcement,
    )
    db.add(rt)
    await db.flush()
    await db.refresh(rt, attribute_names=["workspace"])
    await db.commit()

    logger.info("Run task created", task_id=str(rt.id), workspace=ws.name, stage=stage)

    return JSONResponse(content={"data": _run_task_json(rt)}, status_code=201)


@router.get("/workspaces/{workspace_id}/run-tasks")
async def list_run_tasks(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List run tasks for a workspace. Requires read."""
    ws = await _get_workspace(workspace_id, db)
    await _require_ws_permission(ws, "read", user, db)

    result = await db.execute(
        select(RunTask)
        .options(selectinload(RunTask.workspace))
        .where(RunTask.workspace_id == ws.id)
        .order_by(RunTask.created_at.asc())
    )
    tasks = list(result.scalars().all())

    return JSONResponse(content={"data": [_run_task_json(rt) for rt in tasks]})


@router.get("/run-tasks/{rt_id}")
async def show_run_task(
    rt_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a run task. Requires read on the workspace."""
    rt = await _get_run_task(rt_id, db)
    await _require_ws_permission(rt.workspace, "read", user, db)
    return JSONResponse(content={"data": _run_task_json(rt)})


@router.patch("/run-tasks/{rt_id}")
async def update_run_task(
    rt_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a run task. Requires admin on the workspace."""
    rt = await _get_run_task(rt_id, db)
    await _require_ws_permission(rt.workspace, "admin", user, db)

    attrs = body.get("data", {}).get("attributes", {})

    if "name" in attrs:
        name = attrs["name"].strip()
        if not name:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        rt.name = name

    if "url" in attrs:
        url = attrs["url"].strip()
        if not url:
            raise HTTPException(status_code=422, detail="url cannot be empty")
        rt.url = url

    if "enabled" in attrs:
        rt.enabled = bool(attrs["enabled"])

    if "stage" in attrs:
        if attrs["stage"] not in VALID_STAGES:
            raise HTTPException(
                status_code=422,
                detail=f"stage must be one of: {', '.join(sorted(VALID_STAGES))}",
            )
        rt.stage = attrs["stage"]

    if "enforcement-level" in attrs:
        if attrs["enforcement-level"] not in VALID_ENFORCEMENT_LEVELS:
            raise HTTPException(
                status_code=422,
                detail=f"enforcement-level must be one of: {', '.join(sorted(VALID_ENFORCEMENT_LEVELS))}",
            )
        rt.enforcement_level = attrs["enforcement-level"]

    if "hmac-key" in attrs:
        hmac_key = attrs["hmac-key"]
        rt.hmac_key = hmac_key if hmac_key else None

    await db.flush()
    await db.commit()
    await db.refresh(rt, attribute_names=["workspace"])

    logger.info("Run task updated", task_id=str(rt.id))

    return JSONResponse(content={"data": _run_task_json(rt)})


@router.delete("/run-tasks/{rt_id}", status_code=204)
async def delete_run_task(
    rt_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a run task. Requires admin on the workspace."""
    rt = await _get_run_task(rt_id, db)
    await _require_ws_permission(rt.workspace, "admin", user, db)

    await db.delete(rt)
    await db.commit()

    logger.info("Run task deleted", task_id=rt_id)


# ── Task Stages ───────────────────────────────────────────────────────


@router.get("/runs/{run_id}/task-stages")
async def list_task_stages(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List task stages for a run. Requires read on the workspace."""
    from terrapod.services import run_service

    run_uuid = uuid.UUID(run_id.removeprefix("run-"))
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(status_code=403, detail="Requires read permission on workspace")

    from terrapod.services.run_task_service import list_run_task_stages

    stages = await list_run_task_stages(db, run_uuid)

    return JSONResponse(content={"data": [_task_stage_json(ts) for ts in stages]})


@router.get("/task-stages/{ts_id}")
async def show_task_stage(
    ts_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a task stage with results. Requires read on the workspace."""
    ts_uuid = uuid.UUID(ts_id.removeprefix("ts-"))
    ts = await get_task_stage(db, ts_uuid)
    if ts is None:
        raise HTTPException(status_code=404, detail="Task stage not found")

    run = await db.get(Run, ts.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(status_code=403, detail="Requires read permission on workspace")

    return JSONResponse(content={"data": _task_stage_json(ts)})


@router.post("/task-stages/{ts_id}/actions/override")
async def override_task_stage(
    ts_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Override a failed task stage. Requires admin on the workspace."""
    from terrapod.services.run_task_service import override_stage

    ts_uuid = uuid.UUID(ts_id.removeprefix("ts-"))
    ts = await get_task_stage(db, ts_uuid)
    if ts is None:
        raise HTTPException(status_code=404, detail="Task stage not found")

    run = await db.get(Run, ts.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "admin"):
        raise HTTPException(status_code=403, detail="Requires admin permission on workspace")

    try:
        ts = await override_stage(db, ts_uuid)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    # Re-fetch with results
    ts = await get_task_stage(db, ts_uuid)
    return JSONResponse(content={"data": _task_stage_json(ts)})


# ── Callback (Unauthenticated, Token-Verified) ──────────────────────


@router.patch("/task-stage-results/{tsr_id}/callback")
async def task_stage_result_callback(
    tsr_id: str = Path(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """External service reports pass/fail for a task stage result.

    Unauthenticated — verified via access_token in body.
    """
    from terrapod.db.models import utc_now

    access_token = body.get("access_token", "")
    if not access_token:
        raise HTTPException(status_code=401, detail="access_token is required")

    # Verify the token
    verified_id = verify_callback_token(access_token)
    if verified_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired callback token")

    # Ensure the token matches the result ID in the path
    tsr_uuid = uuid.UUID(tsr_id.removeprefix("tsr-"))
    if verified_id != tsr_uuid:
        raise HTTPException(status_code=401, detail="Token does not match result ID")

    tsr = await get_task_stage_result(db, tsr_uuid)
    if tsr is None:
        raise HTTPException(status_code=404, detail="Task stage result not found")

    # Only accept callbacks for running results
    if tsr.status not in ("pending", "running"):
        raise HTTPException(
            status_code=409, detail=f"Result already in terminal state: {tsr.status}"
        )

    result_status = body.get("status", "")
    if result_status not in ("passed", "failed"):
        raise HTTPException(status_code=422, detail="status must be 'passed' or 'failed'")

    tsr.status = result_status
    tsr.message = body.get("message", "")
    tsr.finished_at = utc_now()
    await db.flush()

    # Resolve the parent stage
    stage_status = await resolve_stage(db, tsr.task_stage_id)
    await db.commit()

    logger.info(
        "Task stage result callback received",
        tsr_id=str(tsr.id),
        status=result_status,
        stage_status=stage_status,
    )

    return JSONResponse(content={"data": {"status": result_status, "stage-status": stage_status}})
