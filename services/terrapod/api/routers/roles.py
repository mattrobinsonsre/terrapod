"""Role CRUD endpoints (admin only).

UX CONTRACT: Role endpoints are consumed by the web frontend:
  - web/src/app/admin/roles/page.tsx (role CRUD, roles tab)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.

Endpoints:
    GET    /api/terrapod/v1/roles               — list all roles (built-in + custom)
    POST   /api/terrapod/v1/roles               — create custom role
    GET    /api/terrapod/v1/roles/{name}        — show role
    PATCH  /api/terrapod/v1/roles/{name}        — update custom role
    DELETE /api/terrapod/v1/roles/{name}        — delete custom role
"""

from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin, require_admin_or_audit
from terrapod.auth.builtin_roles import BUILTIN_ROLES, is_builtin_role
from terrapod.auth.capabilities import (
    GRANTABLE_CAPABILITIES,
    capabilities_for_builtin,
    expand_preset,
    normalize_capabilities,
    summarize_capabilities,
)
from terrapod.db.models import Role
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(tags=["roles"])
logger = get_logger(__name__)

VALID_PERMISSIONS = {"read", "plan", "write", "admin"}
VALID_POOL_PERMISSIONS = {"read", "write", "admin"}
VALID_REGISTRY_PERMISSIONS = {"read", "write", "admin"}
# Catalog access is an opt-in extension: "none" (default) grants nothing, so it
# is a valid value here (unlike the other axes, which floor at "read").
VALID_CATALOG_PERMISSIONS = {"none", "read", "use", "admin"}


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _effective_capabilities(role: Role) -> list[str]:
    """The capability set a role actually grants: its stored ``capabilities`` if
    populated (capability-authored / migrated), else the expansion of its legacy
    level fields (a level-only role). Mirrors the enforcement resolver so the API
    shows exactly what it enforces (#585)."""
    if role.capabilities:
        return list(role.capabilities)
    return expand_preset(
        workspace_permission=role.workspace_permission,
        pool_permission=role.pool_permission,
        registry_permission=role.registry_permission,
        catalog_permission=role.catalog_permission,
    )


def _validate_capabilities(caps_in) -> list[str]:
    """Normalise (alias-upgrade) then reject any token that is not a grantable
    capability — platform:* and typos are refused here (422)."""
    if not isinstance(caps_in, list) or not all(isinstance(c, str) for c in caps_in):
        raise HTTPException(status_code=422, detail="capabilities must be a list of strings")
    normalized = normalize_capabilities(caps_in)
    unknown = sorted(set(normalized) - GRANTABLE_CAPABILITIES)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or non-grantable capabilities: {unknown}",
        )
    return normalized


def _apply_capabilities(role: Role, caps_in) -> None:
    """Capability-author a role: store the validated set and recompute the level
    fields as the derived summary (a preset name per axis, or ``custom``) so the
    legacy level columns stay a faithful, display-only cache of the caps."""
    caps = _validate_capabilities(caps_in)
    role.capabilities = caps
    summary = summarize_capabilities(caps)
    role.workspace_permission = summary["workspace_permission"]
    role.pool_permission = summary["pool_permission"]
    role.registry_permission = summary["registry_permission"]
    role.catalog_permission = summary["catalog_permission"]


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
            # Level fields are the authored shorthand for level-only roles and a
            # derived summary ("custom" where the caps match no preset) for
            # capability-authored roles.
            "workspace-permission": role.workspace_permission,
            "pool-permission": role.pool_permission,
            "registry-permission": role.registry_permission,
            "catalog-permission": role.catalog_permission,
            # The explicit capability set this role grants + enforces (#585) —
            # authored directly, or expanded from the levels for a level-only role.
            "capabilities": _effective_capabilities(role),
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
            "pool-permission": "admin" if name == "admin" else "read",
            "registry-permission": "admin" if name == "admin" else "read",
            # Catalog is opt-in with no `everyone` floor: admin → admin,
            # audit → read, everyone → none (grants nothing).
            "catalog-permission": (
                "admin" if name == "admin" else "read" if name == "audit" else "none"
            ),
            "capabilities": capabilities_for_builtin(name),
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

    pool_perm = attrs.get("pool-permission", "read")
    if pool_perm not in VALID_POOL_PERMISSIONS:
        raise HTTPException(status_code=422, detail=f"Invalid pool-permission: {pool_perm}")

    registry_perm = attrs.get("registry-permission", "read")
    if registry_perm not in VALID_REGISTRY_PERMISSIONS:
        raise HTTPException(status_code=422, detail=f"Invalid registry-permission: {registry_perm}")

    catalog_perm = attrs.get("catalog-permission", "none")
    if catalog_perm not in VALID_CATALOG_PERMISSIONS:
        raise HTTPException(status_code=422, detail=f"Invalid catalog-permission: {catalog_perm}")

    role = Role(
        name=name,
        description=attrs.get("description", ""),
        allow_labels=attrs.get("allow-labels", {}),
        allow_names=attrs.get("allow-names", []),
        deny_labels=attrs.get("deny-labels", {}),
        deny_names=attrs.get("deny-names", []),
        workspace_permission=ws_perm,
        pool_permission=pool_perm,
        registry_permission=registry_perm,
        catalog_permission=catalog_perm,
    )
    # Capability-authoring (#585): if the caller supplies an explicit capability
    # set it becomes the stored truth and the levels above are recomputed as its
    # derived summary. Otherwise the role is level-authored (capabilities stays
    # empty; enforcement expands the levels).
    if "capabilities" in attrs:
        _apply_capabilities(role, attrs["capabilities"])
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

    _level_keys = {
        "workspace-permission",
        "pool-permission",
        "registry-permission",
        "catalog-permission",
    }
    # A level edit on a role that was capability-authored (or migrated) must take
    # effect: clear the stored caps so enforcement expands the new levels (no
    # expand-on-write — the resolver falls back). Applied after the level fields
    # are updated below. Capability-authoring wins over any level fields sent
    # alongside it (the caps are the truth).
    revert_to_levels = "capabilities" not in attrs and bool(_level_keys & attrs.keys())
    if "capabilities" in attrs:
        _apply_capabilities(role, attrs["capabilities"])
    # Level fields are only honoured when not capability-authoring (caps win).
    if "capabilities" not in attrs:
        if "workspace-permission" in attrs:
            ws_perm = attrs["workspace-permission"]
            if ws_perm not in VALID_PERMISSIONS:
                raise HTTPException(
                    status_code=422, detail=f"Invalid workspace-permission: {ws_perm}"
                )
            role.workspace_permission = ws_perm
        if "pool-permission" in attrs:
            pool_perm = attrs["pool-permission"]
            if pool_perm not in VALID_POOL_PERMISSIONS:
                raise HTTPException(status_code=422, detail=f"Invalid pool-permission: {pool_perm}")
            role.pool_permission = pool_perm
        if "registry-permission" in attrs:
            registry_perm = attrs["registry-permission"]
            if registry_perm not in VALID_REGISTRY_PERMISSIONS:
                raise HTTPException(
                    status_code=422, detail=f"Invalid registry-permission: {registry_perm}"
                )
            role.registry_permission = registry_perm
        if "catalog-permission" in attrs:
            catalog_perm = attrs["catalog-permission"]
            if catalog_perm not in VALID_CATALOG_PERMISSIONS:
                raise HTTPException(
                    status_code=422, detail=f"Invalid catalog-permission: {catalog_perm}"
                )
            role.catalog_permission = catalog_perm

    if revert_to_levels:
        # The role's granular stored caps (if any) no longer reflect the edited
        # levels — drop them so enforcement expands the new levels.
        role.capabilities = []

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
