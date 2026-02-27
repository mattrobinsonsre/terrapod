"""Configuration version upload endpoints (TFE V2 compatible).

Endpoints:
    POST   /api/v2/workspaces/{id}/configuration-versions
    GET    /api/v2/configuration-versions/{cv_id}
    PUT    /api/v2/configuration-versions/{cv_id}/upload  (tarball upload, no auth)
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import run_service
from terrapod.services.workspace_rbac_service import has_permission, resolve_workspace_permission
from terrapod.storage import get_storage
from terrapod.storage.keys import config_version_key

router = APIRouter(prefix="/api/v2", tags=["configuration-versions"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cv_json(cv: ConfigurationVersion) -> dict:
    """Serialize a ConfigurationVersion to TFE V2 JSON:API format."""
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")
    cv_id = f"cv-{cv.id}"

    return {
        "data": {
            "id": cv_id,
            "type": "configuration-versions",
            "attributes": {
                "source": cv.source,
                "status": cv.status,
                "auto-queue-runs": cv.auto_queue_runs,
                "speculative": cv.speculative,
                "upload-url": f"{base}/api/v2/configuration-versions/{cv_id}/upload",
                "created-at": _rfc3339(cv.created_at),
            },
            "relationships": {
                "workspace": {
                    "data": {"id": f"ws-{cv.workspace_id}", "type": "workspaces"},
                },
            },
            "links": {
                "self": f"/api/v2/configuration-versions/{cv_id}",
            },
        }
    }


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


@router.post("/workspaces/{workspace_id}/configuration-versions", status_code=201)
async def create_configuration_version(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a configuration version. Requires write on workspace."""
    ws = await _get_workspace(workspace_id, db)
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "write"):
        raise HTTPException(status_code=403, detail="Requires write permission on workspace")

    attrs = body.get("data", {}).get("attributes", {})

    cv = await run_service.create_configuration_version(
        db,
        workspace_id=ws.id,
        source=attrs.get("source", "tfe-api"),
        auto_queue_runs=attrs.get("auto-queue-runs", True),
        speculative=attrs.get("speculative", False),
    )
    await db.commit()
    await db.refresh(cv)

    return JSONResponse(content=_cv_json(cv), status_code=201)


@router.get("/configuration-versions/{cv_id}")
async def show_configuration_version(
    cv_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a configuration version."""
    cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))
    cv = await run_service.get_configuration_version(db, cv_uuid)
    if cv is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    return JSONResponse(content=_cv_json(cv))


@router.put("/configuration-versions/{cv_id}/upload")
async def upload_configuration(
    request: Request,
    cv_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload configuration tarball.

    No auth required — the CV UUID acts as a capability token (same pattern
    as state version upload). go-tfe sends no Authorization header.
    """
    cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))
    cv = await run_service.get_configuration_version(db, cv_uuid)
    if cv is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")

    if cv.status == "uploaded":
        raise HTTPException(status_code=409, detail="Configuration already uploaded")

    data = await request.body()
    if not data:
        raise HTTPException(status_code=422, detail="Upload data is required")

    # Store tarball
    storage = get_storage()
    key = config_version_key(str(cv.workspace_id), str(cv.id))
    await storage.put(key, data, content_type="application/x-tar")

    # Mark as uploaded
    cv = await run_service.mark_configuration_uploaded(db, cv)

    # Auto-queue runs if configured
    if cv.auto_queue_runs:
        # Find pending runs waiting for this config version
        result = await db.execute(
            select(Run).where(
                Run.configuration_version_id == cv.id,
                Run.status == "pending",
            )
        )
        pending_runs = result.scalars().all()
        for run in pending_runs:
            await run_service.queue_run(db, run)

    await db.commit()

    logger.info(
        "Configuration uploaded",
        cv_id=str(cv.id),
        size=len(data),
    )

    return Response(status_code=200)
