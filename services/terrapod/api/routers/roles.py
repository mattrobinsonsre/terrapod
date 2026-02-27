"""Role CRUD endpoints (admin only).

Endpoints:
    GET    /api/v2/roles               — list all roles (built-in + custom)
    POST   /api/v2/roles               — create custom role
    GET    /api/v2/roles/{name}        — show role
    PATCH  /api/v2/roles/{name}        — update custom role
    DELETE /api/v2/roles/{name}        — delete custom role
"""

from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin, require_admin_or_audit
from terrapod.auth.builtin_roles import BUILTIN_ROLES, is_builtin_role
from terrapod.db.models import Role
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(prefix="/api/v2", tags=["roles"])
logger = get_logger(__name__)

VALID_PERMISSIONS = {"read", "plan", "write", "admin"}


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _role_json(role: Role) -> dict:
    return {
        "name": role.name,
        "type": "roles",
        "attributes": {
            "description": role.description or "",
            "allow-labels": role.allow_labels,
            "allow-names": role.allow_names,
            "deny-labels": role.deny_labels,
            "deny-names": role.deny_names,
            "workspace-permission": role.workspace_permission,
            "built-in": False,
            "created-at": _rfc3339(role.created_at),
            "updated-at": _rfc3339(role.updated_at),
        },
    }


def _builtin_role_json(name: str, info: dict) -> dict:
    return {
        "name": name,
        "type": "roles",
        "attributes": {
            "description": info.get("description", ""),
            "allow-labels": info.get("allow_labels", {}),
            "allow-names": [],
            "deny-labels": {},
            "deny-names": [],
            "workspace-permission": "admin" if name == "admin" else "read",
            "built-in": True,
            "created-at": "",
            "updated-at": "",
        },
    }


@router.get("/roles")
async def list_roles(
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all roles (built-in + custom)."""
    # Built-in roles
    data = [_builtin_role_json(name, info) for name, info in BUILTIN_ROLES.items()]

    # Custom roles
    result = await db.execute(select(Role).order_by(Role.name))
    for role in result.scalars().all():
        data.append(_role_json(role))

    return JSONResponse(content={"data": data})


@router.post("/roles", status_code=201)
async def create_role(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a custom role."""
    attrs = body.get("data", {}).get("attributes", {})
    name = body.get("data", {}).get("name", "") or attrs.get("name", "")
    if not name:
        raise HTTPException(status_code=422, detail="Role name is required")

    if is_builtin_role(name):
        raise HTTPException(
            status_code=422, detail=f"Cannot create role with built-in name '{name}'"
        )

    # Check for existing
    existing = await db.execute(select(Role).where(Role.name == name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=422, detail=f"Role '{name}' already exists")

    ws_perm = attrs.get("workspace-permission", "read")
    if ws_perm not in VALID_PERMISSIONS:
        raise HTTPException(status_code=422, detail=f"Invalid workspace-permission: {ws_perm}")

    role = Role(
        name=name,
        description=attrs.get("description", ""),
        allow_labels=attrs.get("allow-labels", {}),
        allow_names=attrs.get("allow-names", []),
        deny_labels=attrs.get("deny-labels", {}),
        deny_names=attrs.get("deny-names", []),
        workspace_permission=ws_perm,
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)

    logger.info("Role created", role=name)
    return JSONResponse(content={"data": _role_json(role)}, status_code=201)


@router.get("/roles/{role_name}")
async def show_role(
    role_name: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a role by name."""
    if is_builtin_role(role_name):
        return JSONResponse(
            content={"data": _builtin_role_json(role_name, BUILTIN_ROLES[role_name])}
        )

    result = await db.execute(select(Role).where(Role.name == role_name))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")

    return JSONResponse(content={"data": _role_json(role)})


@router.patch("/roles/{role_name}")
async def update_role(
    role_name: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a custom role."""
    if is_builtin_role(role_name):
        raise HTTPException(status_code=422, detail="Cannot modify built-in roles")

    result = await db.execute(select(Role).where(Role.name == role_name))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")

    attrs = body.get("data", {}).get("attributes", {})
    if "description" in attrs:
        role.description = attrs["description"]
    if "allow-labels" in attrs:
        role.allow_labels = attrs["allow-labels"]
    if "allow-names" in attrs:
        role.allow_names = attrs["allow-names"]
    if "deny-labels" in attrs:
        role.deny_labels = attrs["deny-labels"]
    if "deny-names" in attrs:
        role.deny_names = attrs["deny-names"]
    if "workspace-permission" in attrs:
        ws_perm = attrs["workspace-permission"]
        if ws_perm not in VALID_PERMISSIONS:
            raise HTTPException(status_code=422, detail=f"Invalid workspace-permission: {ws_perm}")
        role.workspace_permission = ws_perm

    await db.commit()
    await db.refresh(role)

    logger.info("Role updated", role=role_name)
    return JSONResponse(content={"data": _role_json(role)})


@router.delete("/roles/{role_name}", status_code=204)
async def delete_role(
    role_name: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a custom role (cascades to role assignments)."""
    if is_builtin_role(role_name):
        raise HTTPException(status_code=422, detail="Cannot delete built-in roles")

    result = await db.execute(select(Role).where(Role.name == role_name))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")

    await db.delete(role)
    await db.commit()

    logger.info("Role deleted", role=role_name)
