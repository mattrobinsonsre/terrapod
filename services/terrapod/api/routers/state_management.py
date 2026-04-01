"""Terrapod-specific state management endpoints.

These endpoints are NOT part of the TFE V2 API specification. They provide
state lifecycle operations consumed by the web UI: delete, rollback, and
manual upload.

Endpoints:
    DELETE /api/v2/state-versions/{id}/manage — delete a non-current state version
    POST   /api/v2/state-versions/{id}/actions/rollback — rollback to an older version
    POST   /api/v2/workspaces/{id}/state-versions/actions/upload — manual state upload
"""

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import has_permission, resolve_workspace_permission
from terrapod.storage import get_storage
from terrapod.storage.keys import state_key

router = APIRouter(prefix="/api/v2", tags=["state-management"])
logger = get_logger(__name__)


async def _get_state_version(state_version_id: str, db: AsyncSession) -> StateVersion:
    """Look up a state version by its sv-{uuid} ID."""
    sv_uuid = state_version_id.removeprefix("sv-")
    result = await db.execute(select(StateVersion).where(StateVersion.id == sv_uuid))
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="State version not found")
    return sv


async def _require_sv_workspace_permission(
    sv: StateVersion,
    required: str,
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Workspace:
    """Check permission on the state version's workspace. Returns workspace."""
    ws = await db.get(Workspace, sv.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on workspace",
        )
    return ws


@router.delete("/state-versions/{state_version_id}/manage")
async def delete_state_version(
    state_version_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a non-current state version. Requires admin permission.

    The current (highest serial) state version cannot be deleted — this
    protects against accidentally removing the active workspace state.
    """
    sv = await _get_state_version(state_version_id, db)
    ws = await _require_sv_workspace_permission(sv, "admin", user, db)

    # Prevent deleting the current (latest) state version
    max_serial_result = await db.execute(
        select(func.max(StateVersion.serial)).where(StateVersion.workspace_id == sv.workspace_id)
    )
    max_serial = max_serial_result.scalar_one_or_none()
    if max_serial is not None and sv.serial == max_serial:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the current state version",
        )

    # Delete from object storage
    storage = get_storage()
    key = state_key(str(sv.workspace_id), str(sv.id))
    try:
        await storage.delete(key)
    except Exception:
        logger.warning(
            "state_version_storage_delete_failed",
            state_version_id=str(sv.id),
            key=key,
        )

    await db.delete(sv)
    await db.commit()

    logger.info(
        "state_version_deleted",
        workspace=ws.name,
        serial=sv.serial,
        state_version_id=str(sv.id),
        deleted_by=user.email,
    )

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "state_version_created")

    return Response(status_code=204)


@router.post("/state-versions/{state_version_id}/actions/rollback")
async def rollback_state_version(
    state_version_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Rollback to an older state version. Requires write permission.

    Creates a NEW state version with the content of the specified version
    and serial = max existing serial + 1. This is a "copy forward" rollback
    — no versions are deleted, history is preserved.
    """
    sv = await _get_state_version(state_version_id, db)
    ws = await _require_sv_workspace_permission(sv, "write", user, db)

    # Download the old state bytes
    storage = get_storage()
    old_key = state_key(str(sv.workspace_id), str(sv.id))
    try:
        state_bytes = await storage.get(old_key)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail="State data not found in storage",
        ) from None

    # Determine next serial
    max_serial_result = await db.execute(
        select(func.max(StateVersion.serial)).where(StateVersion.workspace_id == sv.workspace_id)
    )
    max_serial = max_serial_result.scalar_one() or 0
    new_serial = max_serial + 1

    # Create new state version record
    new_sv = StateVersion(
        workspace_id=sv.workspace_id,
        serial=new_serial,
        lineage=sv.lineage,
        md5=hashlib.md5(state_bytes).hexdigest(),
        state_size=len(state_bytes),
        created_by=user.email,
    )
    db.add(new_sv)
    await db.flush()

    # Store state bytes at new key
    new_key = state_key(str(sv.workspace_id), str(new_sv.id))
    await storage.put(new_key, state_bytes)

    await db.commit()
    await db.refresh(new_sv)

    logger.info(
        "state_version_rolled_back",
        workspace=ws.name,
        from_serial=sv.serial,
        to_serial=new_serial,
        state_version_id=str(new_sv.id),
        rolled_back_by=user.email,
    )

    from terrapod.api.metrics import STATE_VERSIONS_CREATED

    STATE_VERSIONS_CREATED.inc()

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "state_version_created")

    from terrapod.api.routers.tfe_v2 import _state_version_json

    return JSONResponse(
        content=_state_version_json(new_sv),
        status_code=201,
    )


@router.post("/workspaces/{workspace_id}/state-versions/actions/upload")
async def upload_state_manual(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Upload a state file manually. Requires write permission.

    Accepts raw state JSON. Serial is auto-assigned as max existing + 1
    to prevent conflicts. Lineage is extracted from the state file.
    """
    from terrapod.api.routers.tfe_v2 import _get_workspace_by_id

    ws = await _get_workspace_by_id(workspace_id, db)
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires write permission on workspace",
        )

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    try:
        state_data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid state JSON") from exc

    lineage = state_data.get("lineage", "")
    md5 = hashlib.md5(body).hexdigest()

    # Auto-assign serial
    max_serial_result = await db.execute(
        select(func.max(StateVersion.serial)).where(StateVersion.workspace_id == ws.id)
    )
    max_serial = max_serial_result.scalar_one() or 0
    new_serial = max_serial + 1

    sv = StateVersion(
        workspace_id=ws.id,
        serial=new_serial,
        lineage=lineage,
        md5=md5,
        state_size=len(body),
        created_by=user.email,
    )
    db.add(sv)
    await db.flush()

    storage = get_storage()
    key = state_key(str(ws.id), str(sv.id))
    await storage.put(key, body)

    await db.commit()
    await db.refresh(sv)

    logger.info(
        "state_version_uploaded_manually",
        workspace=ws.name,
        serial=new_serial,
        state_version_id=str(sv.id),
        uploaded_by=user.email,
    )

    from terrapod.api.metrics import STATE_VERSIONS_CREATED

    STATE_VERSIONS_CREATED.inc()

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "state_version_created")

    from terrapod.api.routers.tfe_v2 import _state_version_json

    return JSONResponse(
        content=_state_version_json(sv),
        status_code=201,
    )
