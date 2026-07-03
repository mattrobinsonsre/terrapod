"""Execution hook library CRUD + workspace association (#619).

Terrapod-native management surface at ``/api/terrapod/v1/execution-hooks``.
A hook is a reusable custom-shell step run inside the runner Job at one of five
fixed points; it reaches a workspace only via explicit association (no global
flag). Managing hooks is platform-admin-gated (like variable sets) because a
hook is operator-supplied code that runs with the runner's cloud identity.

Consumed by ``web/src/app/admin/execution-hooks/*`` and go-terrapod (PR2).
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.db.models import ExecutionHook, ExecutionHookWorkspace, Workspace
from terrapod.db.session import get_db
from terrapod.services import execution_hook_service

router = APIRouter(tags=["execution-hooks"])


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hook_json(h: ExecutionHook) -> dict:
    """Serialize an ExecutionHook to JSON:API. Guards relationship access with
    ``sa_inspect(...).dict`` so it never triggers a lazy load when the
    assignments weren't eagerly loaded."""
    loaded = sa_inspect(h).dict
    assignments = h.workspace_assignments if "workspace_assignments" in loaded else []
    relationships: dict = {
        "workspaces": {
            "data": [{"id": f"ws-{a.workspace_id}", "type": "workspaces"} for a in assignments]
        }
    }
    return {
        "id": f"hook-{h.id}",
        "type": "execution-hooks",
        "attributes": {
            "name": h.name,
            "description": h.description,
            "hook-point": h.hook_point,
            "script": h.script,
            "enabled": h.enabled,
            "priority": h.priority,
            "workspace-count": len(assignments),
            "created-at": _rfc3339(h.created_at),
            "updated-at": _rfc3339(h.updated_at),
        },
        "relationships": relationships,
    }


async def _get_hook(hook_id: str, db: AsyncSession) -> ExecutionHook:
    try:
        h_uuid = uuid.UUID(hook_id.removeprefix("hook-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Execution hook not found") from exc
    result = await db.execute(
        select(ExecutionHook)
        .where(ExecutionHook.id == h_uuid)
        .options(selectinload(ExecutionHook.workspace_assignments))
    )
    hook = result.scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="Execution hook not found")
    return hook


@router.get("/execution-hooks")
async def list_hooks(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all execution hooks. Requires admin."""
    result = await db.execute(
        select(ExecutionHook)
        .options(selectinload(ExecutionHook.workspace_assignments))
        .order_by(ExecutionHook.name)
    )
    hooks = result.scalars().all()
    return JSONResponse(content={"data": [_hook_json(h) for h in hooks]})


@router.post("/execution-hooks", status_code=201)
async def create_hook(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create an execution hook. Requires admin."""
    attrs = body.get("data", {}).get("attributes", {})
    name = (attrs.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Execution hook name is required")
    hook_point = attrs.get("hook-point", "")
    execution_hook_service.validate_hook_point(hook_point)

    hook = ExecutionHook(
        name=name,
        description=attrs.get("description", ""),
        hook_point=hook_point,
        script=attrs.get("script", ""),
        enabled=attrs.get("enabled", True),
        priority=int(attrs.get("priority", 0) or 0),
    )
    db.add(hook)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"An execution hook named '{name}' already exists"
        ) from exc
    await db.refresh(hook, ["workspace_assignments"])
    return JSONResponse(content={"data": _hook_json(hook)}, status_code=201)


@router.get("/execution-hooks/{hook_id}")
async def show_hook(
    hook_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show an execution hook. Requires admin."""
    hook = await _get_hook(hook_id, db)
    return JSONResponse(content={"data": _hook_json(hook)})


@router.patch("/execution-hooks/{hook_id}")
async def update_hook(
    hook_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update an execution hook. Requires admin."""
    hook = await _get_hook(hook_id, db)
    attrs = body.get("data", {}).get("attributes", {})
    if "name" in attrs:
        new_name = (attrs["name"] or "").strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="Execution hook name is required")
        hook.name = new_name
    if "description" in attrs:
        hook.description = attrs["description"]
    if "hook-point" in attrs:
        execution_hook_service.validate_hook_point(attrs["hook-point"])
        hook.hook_point = attrs["hook-point"]
    if "script" in attrs:
        hook.script = attrs["script"]
    if "enabled" in attrs:
        hook.enabled = bool(attrs["enabled"])
    if "priority" in attrs:
        hook.priority = int(attrs["priority"] or 0)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"An execution hook named '{hook.name}' already exists"
        ) from exc
    await db.refresh(hook, ["workspace_assignments"])
    return JSONResponse(content={"data": _hook_json(hook)})


@router.delete("/execution-hooks/{hook_id}", status_code=204)
async def delete_hook(
    hook_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an execution hook. Requires admin."""
    hook = await _get_hook(hook_id, db)
    await db.delete(hook)
    await db.commit()


@router.post("/execution-hooks/{hook_id}/relationships/workspaces", status_code=204)
async def add_hook_workspaces(
    hook_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Associate workspaces with a hook. Requires admin. Idempotent."""
    hook = await _get_hook(hook_id, db)
    for item in body.get("data", []):
        ws_id = (item.get("id") or "").removeprefix("ws-")
        try:
            ws_uuid = uuid.UUID(ws_id)
        except ValueError:
            continue
        ws = await db.get(Workspace, ws_uuid)
        if ws is None:
            continue
        existing = await db.execute(
            select(ExecutionHookWorkspace).where(
                ExecutionHookWorkspace.hook_id == hook.id,
                ExecutionHookWorkspace.workspace_id == ws_uuid,
            )
        )
        if existing.scalar_one_or_none() is None:
            db.add(ExecutionHookWorkspace(hook_id=hook.id, workspace_id=ws_uuid))
    await db.commit()


@router.delete("/execution-hooks/{hook_id}/relationships/workspaces", status_code=204)
async def remove_hook_workspaces(
    hook_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove workspace associations from a hook. Requires admin."""
    hook = await _get_hook(hook_id, db)
    for item in body.get("data", []):
        ws_id = (item.get("id") or "").removeprefix("ws-")
        try:
            ws_uuid = uuid.UUID(ws_id)
        except ValueError:
            continue
        result = await db.execute(
            select(ExecutionHookWorkspace).where(
                ExecutionHookWorkspace.hook_id == hook.id,
                ExecutionHookWorkspace.workspace_id == ws_uuid,
            )
        )
        assoc = result.scalar_one_or_none()
        if assoc:
            await db.delete(assoc)
    await db.commit()
