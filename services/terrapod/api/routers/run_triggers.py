"""Run trigger CRUD endpoints (TFE V2 compatible).

UX CONTRACT: Run trigger endpoints are consumed by the web frontend:
  - web/src/app/workspaces/[id]/page.tsx (workspace detail, indirectly via runs)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.

Endpoints:
    POST   /api/v2/workspaces/{id}/run-triggers      (create trigger)
    GET    /api/v2/workspaces/{id}/run-triggers       (list inbound/outbound)
    GET    /api/v2/run-triggers/{id}                   (show trigger)
    DELETE /api/v2/run-triggers/{id}                   (delete trigger)
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import RunTrigger, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import has_permission, resolve_workspace_permission

router = APIRouter(prefix="/api/v2", tags=["run-triggers"])
logger = get_logger(__name__)

MAX_SOURCES_PER_WORKSPACE = 20


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trigger_json(trigger: RunTrigger) -> dict:
    """Serialize a RunTrigger to TFE V2 JSON:API format."""
    trigger_id = f"rt-{trigger.id}"
    ws_name = trigger.workspace.name if trigger.workspace else ""
    source_name = trigger.source_workspace.name if trigger.source_workspace else ""

    return {
        "id": trigger_id,
        "type": "run-triggers",
        "attributes": {
            "workspace-name": ws_name,
            "sourceable-name": source_name,
            "created-at": _rfc3339(trigger.created_at),
        },
        "relationships": {
            "workspace": {
                "data": {"id": f"ws-{trigger.workspace_id}", "type": "workspaces"},
            },
            "sourceable": {
                "data": {"id": f"ws-{trigger.source_workspace_id}", "type": "workspaces"},
            },
        },
        "links": {
            "self": f"/api/v2/run-triggers/{trigger_id}",
        },
    }


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


@router.post("/workspaces/{workspace_id}/run-triggers", status_code=201)
async def create_run_trigger(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a run trigger. Requires admin on the destination workspace."""
    ws = await _get_workspace(workspace_id, db)
    await _require_ws_permission(ws, "admin", user, db)

    # Extract source workspace from relationships
    relationships = body.get("data", {}).get("relationships", {})
    source_data = relationships.get("sourceable", {}).get("data", {})
    source_id = source_data.get("id", "")
    if not source_id:
        raise HTTPException(status_code=422, detail="sourceable relationship is required")

    source_ws = await _get_workspace(source_id, db)

    # Validate: not self-referential
    if ws.id == source_ws.id:
        raise HTTPException(status_code=422, detail="A workspace cannot trigger itself")

    # Validate: no duplicate
    existing = await db.execute(
        select(RunTrigger).where(
            RunTrigger.workspace_id == ws.id,
            RunTrigger.source_workspace_id == source_ws.id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Run trigger already exists for this pair")

    # Validate: max sources per destination
    count_result = await db.execute(
        select(func.count()).select_from(RunTrigger).where(RunTrigger.workspace_id == ws.id)
    )
    count = count_result.scalar_one()
    if count >= MAX_SOURCES_PER_WORKSPACE:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum of {MAX_SOURCES_PER_WORKSPACE} source workspaces per destination",
        )

    trigger = RunTrigger(
        workspace_id=ws.id,
        source_workspace_id=source_ws.id,
    )
    db.add(trigger)
    await db.flush()

    # Eagerly load relationships for serialization
    await db.refresh(trigger, attribute_names=["workspace", "source_workspace"])

    await db.commit()

    logger.info(
        "Run trigger created",
        trigger_id=str(trigger.id),
        destination=ws.name,
        source=source_ws.name,
    )

    return JSONResponse(content={"data": _trigger_json(trigger)}, status_code=201)


@router.get("/workspaces/{workspace_id}/run-triggers")
async def list_run_triggers(
    workspace_id: str = Path(...),
    filter_type: str | None = Query(None, alias="filter[run-trigger][type]"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List run triggers for a workspace (inbound or outbound). Requires read."""
    ws = await _get_workspace(workspace_id, db)
    await _require_ws_permission(ws, "read", user, db)

    if filter_type not in ("inbound", "outbound"):
        raise HTTPException(
            status_code=422,
            detail="filter[run-trigger][type] is required and must be 'inbound' or 'outbound'",
        )

    if filter_type == "inbound":
        query = (
            select(RunTrigger)
            .options(selectinload(RunTrigger.workspace), selectinload(RunTrigger.source_workspace))
            .where(RunTrigger.workspace_id == ws.id)
            .order_by(RunTrigger.created_at.asc())
        )
    else:
        query = (
            select(RunTrigger)
            .options(selectinload(RunTrigger.workspace), selectinload(RunTrigger.source_workspace))
            .where(RunTrigger.source_workspace_id == ws.id)
            .order_by(RunTrigger.created_at.asc())
        )

    result = await db.execute(query)
    triggers = list(result.scalars().all())

    return JSONResponse(content={"data": [_trigger_json(t) for t in triggers]})


@router.get("/run-triggers/{run_trigger_id}")
async def show_run_trigger(
    run_trigger_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a run trigger. Requires read on the destination workspace."""
    rt_uuid = uuid.UUID(run_trigger_id.removeprefix("rt-"))
    result = await db.execute(
        select(RunTrigger)
        .options(selectinload(RunTrigger.workspace), selectinload(RunTrigger.source_workspace))
        .where(RunTrigger.id == rt_uuid)
    )
    trigger = result.scalar_one_or_none()
    if trigger is None:
        raise HTTPException(status_code=404, detail="Run trigger not found")

    ws = trigger.workspace
    await _require_ws_permission(ws, "read", user, db)

    return JSONResponse(content={"data": _trigger_json(trigger)})


@router.delete("/run-triggers/{run_trigger_id}", status_code=204)
async def delete_run_trigger(
    run_trigger_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a run trigger. Requires admin on the destination workspace."""
    rt_uuid = uuid.UUID(run_trigger_id.removeprefix("rt-"))
    result = await db.execute(
        select(RunTrigger)
        .options(selectinload(RunTrigger.workspace))
        .where(RunTrigger.id == rt_uuid)
    )
    trigger = result.scalar_one_or_none()
    if trigger is None:
        raise HTTPException(status_code=404, detail="Run trigger not found")

    ws = trigger.workspace
    await _require_ws_permission(ws, "admin", user, db)

    await db.delete(trigger)
    await db.commit()

    logger.info("Run trigger deleted", trigger_id=str(trigger.id))
