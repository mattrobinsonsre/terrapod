"""Notification configuration CRUD endpoints (TFE V2 compatible).

UX CONTRACT: Notification endpoints are consumed by the web frontend:
  - web/src/app/workspaces/[id]/page.tsx (notifications tab)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.

Endpoints:
    POST   /api/v2/workspaces/{id}/notification-configurations      (create)
    GET    /api/v2/workspaces/{id}/notification-configurations      (list)
    GET    /api/v2/notification-configurations/{id}                  (show)
    PATCH  /api/v2/notification-configurations/{id}                  (update)
    DELETE /api/v2/notification-configurations/{id}                  (delete)
    POST   /api/v2/notification-configurations/{id}/actions/verify   (verify)
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import NotificationConfiguration, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.notification_service import (
    VALID_TRIGGERS,
    build_verification_payload,
    deliver_notification,
    record_delivery_response,
)
from terrapod.services.workspace_rbac_service import has_permission, resolve_workspace_permission

router = APIRouter(prefix="/api/v2", tags=["notification-configurations"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:  # type: ignore[no-untyped-def]
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nc_json(nc: NotificationConfiguration) -> dict:
    """Serialize a NotificationConfiguration to TFE V2 JSON:API format."""
    nc_id = f"nc-{nc.id}"

    return {
        "id": nc_id,
        "type": "notification-configurations",
        "attributes": {
            "name": nc.name,
            "destination-type": nc.destination_type,
            "url": nc.url,
            "enabled": nc.enabled,
            "has-token": nc.token is not None and nc.token != "",
            "triggers": nc.triggers or [],
            "email-addresses": nc.email_addresses or [],
            "delivery-responses": nc.delivery_responses or [],
            "created-at": _rfc3339(nc.created_at),
            "updated-at": _rfc3339(nc.updated_at),
        },
        "relationships": {
            "workspace": {
                "data": {"id": f"ws-{nc.workspace_id}", "type": "workspaces"},
            },
        },
        "links": {
            "self": f"/api/v2/notification-configurations/{nc_id}",
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


def _validate_triggers(triggers: list) -> list[str]:
    """Validate trigger list, return cleaned list."""
    if not isinstance(triggers, list):
        raise HTTPException(status_code=422, detail="triggers must be a list")
    invalid = set(triggers) - VALID_TRIGGERS
    if invalid:
        raise HTTPException(
            status_code=422, detail=f"Invalid triggers: {', '.join(sorted(invalid))}"
        )
    return triggers


@router.post("/workspaces/{workspace_id}/notification-configurations", status_code=201)
async def create_notification_configuration(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a notification configuration. Requires admin on the workspace."""
    ws = await _get_workspace(workspace_id, db)
    await _require_ws_permission(ws, "admin", user, db)

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    dest_type = attrs.get("destination-type", "")
    if dest_type not in ("generic", "slack", "email"):
        raise HTTPException(
            status_code=422, detail="destination-type must be generic, slack, or email"
        )

    url = attrs.get("url", "")
    if dest_type in ("generic", "slack") and not url:
        raise HTTPException(status_code=422, detail="url is required for generic and slack types")

    triggers = _validate_triggers(attrs.get("triggers", []))

    email_addresses = attrs.get("email-addresses", [])
    if dest_type == "email" and not email_addresses:
        raise HTTPException(status_code=422, detail="email-addresses is required for email type")

    token = attrs.get("token", "") or None

    nc = NotificationConfiguration(
        workspace_id=ws.id,
        name=name,
        destination_type=dest_type,
        url=url,
        token=token,
        enabled=attrs.get("enabled", False),
        triggers=triggers,
        email_addresses=email_addresses,
    )
    db.add(nc)
    await db.flush()

    # Eagerly load workspace for serialization
    await db.refresh(nc, attribute_names=["workspace"])
    await db.commit()

    logger.info(
        "Notification configuration created",
        nc_id=str(nc.id),
        workspace=ws.name,
        destination_type=dest_type,
    )

    return JSONResponse(content={"data": _nc_json(nc)}, status_code=201)


@router.get("/workspaces/{workspace_id}/notification-configurations")
async def list_notification_configurations(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List notification configs for a workspace. Requires read."""
    ws = await _get_workspace(workspace_id, db)
    await _require_ws_permission(ws, "read", user, db)

    result = await db.execute(
        select(NotificationConfiguration)
        .options(selectinload(NotificationConfiguration.workspace))
        .where(NotificationConfiguration.workspace_id == ws.id)
        .order_by(NotificationConfiguration.created_at.asc())
    )
    configs = list(result.scalars().all())

    return JSONResponse(content={"data": [_nc_json(nc) for nc in configs]})


async def _get_nc(nc_id: str, db: AsyncSession) -> NotificationConfiguration:
    """Load a notification configuration by ID."""
    nc_uuid = uuid.UUID(nc_id.removeprefix("nc-"))
    result = await db.execute(
        select(NotificationConfiguration)
        .options(selectinload(NotificationConfiguration.workspace))
        .where(NotificationConfiguration.id == nc_uuid)
    )
    nc = result.scalar_one_or_none()
    if nc is None:
        raise HTTPException(status_code=404, detail="Notification configuration not found")
    return nc


@router.get("/notification-configurations/{nc_id}")
async def show_notification_configuration(
    nc_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a notification configuration. Requires read on the workspace."""
    nc = await _get_nc(nc_id, db)
    await _require_ws_permission(nc.workspace, "read", user, db)
    return JSONResponse(content={"data": _nc_json(nc)})


@router.patch("/notification-configurations/{nc_id}")
async def update_notification_configuration(
    nc_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a notification configuration. Requires admin on the workspace."""
    nc = await _get_nc(nc_id, db)
    await _require_ws_permission(nc.workspace, "admin", user, db)

    attrs = body.get("data", {}).get("attributes", {})

    if "name" in attrs:
        name = attrs["name"].strip()
        if not name:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        nc.name = name

    if "enabled" in attrs:
        nc.enabled = bool(attrs["enabled"])

    if "url" in attrs:
        nc.url = attrs["url"]

    if "triggers" in attrs:
        nc.triggers = _validate_triggers(attrs["triggers"])

    if "email-addresses" in attrs:
        nc.email_addresses = attrs["email-addresses"]

    if "token" in attrs:
        token = attrs["token"]
        nc.token = token if token else None

    await db.flush()
    await db.commit()

    # Reload for serialization
    await db.refresh(nc, attribute_names=["workspace"])

    logger.info("Notification configuration updated", nc_id=str(nc.id))

    return JSONResponse(content={"data": _nc_json(nc)})


@router.delete("/notification-configurations/{nc_id}", status_code=204)
async def delete_notification_configuration(
    nc_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a notification configuration. Requires admin on the workspace."""
    nc = await _get_nc(nc_id, db)
    await _require_ws_permission(nc.workspace, "admin", user, db)

    await db.delete(nc)
    await db.commit()

    logger.info("Notification configuration deleted", nc_id=nc_id)


@router.post("/notification-configurations/{nc_id}/actions/verify")
async def verify_notification_configuration(
    nc_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Send a test notification. Requires admin on the workspace."""
    nc = await _get_nc(nc_id, db)
    await _require_ws_permission(nc.workspace, "admin", user, db)

    payload = build_verification_payload(nc.name)

    token: str | None = nc.token or None

    email_addresses = nc.email_addresses if nc.email_addresses else []
    response = await deliver_notification(
        destination_type=nc.destination_type,
        url=nc.url,
        token=token,
        email_addresses=email_addresses,
        payload=payload,
    )

    # Record the delivery response
    from terrapod.config import settings

    await record_delivery_response(
        db, nc.id, response, settings.notifications.max_delivery_responses
    )
    await db.commit()

    # Sanitize response — strip raw exception details from failed deliveries
    # to avoid leaking internal stack traces to API clients.
    safe_response = {
        "status": response.get("status", 0),
        "success": response.get("success", False),
        "body": response.get("body", "") if response.get("success") else "Delivery failed",
    }
    # codeql[py/stack-trace-exposure]
    return JSONResponse(content={"data": {"type": "verification", "attributes": safe_response}})
