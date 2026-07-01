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
    AXIS_LEVEL_MAPS,
    GRANTABLE_CAPABILITIES,
    axis_all_caps,
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


# axis JSON:API key → (short axis name, valid level values)
_LEVEL_INPUT: dict[str, tuple[str, set[str]]] = {
    "workspace-permission": ("workspace", VALID_PERMISSIONS),
    "pool-permission": ("pool", VALID_POOL_PERMISSIONS),
    "registry-permission": ("registry", VALID_REGISTRY_PERMISSIONS),
    "catalog-permission": ("catalog", VALID_CATALOG_PERMISSIONS),
}


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


def _level(attrs: dict, key: str, default: str, valid: set[str]) -> str:
    v = attrs.get(key, default)
    if v not in valid:
        raise HTTPException(status_code=422, detail=f"Invalid {key}: {v}")
    return v


def _caps_from_level_input(attrs: dict) -> list[str]:
    """Expand a create request's level shorthand into a capability set. Absent
    axes use their default (read/read/read/none)."""
    return expand_preset(
        workspace_permission=_level(attrs, "workspace-permission", "read", VALID_PERMISSIONS),
        pool_permission=_level(attrs, "pool-permission", "read", VALID_POOL_PERMISSIONS),
        registry_permission=_level(
            attrs, "registry-permission", "read", VALID_REGISTRY_PERMISSIONS
        ),
        catalog_permission=_level(attrs, "catalog-permission", "none", VALID_CATALOG_PERMISSIONS),
    )


def _apply_level_edits(role: Role, attrs: dict) -> None:
    """A partial level edit replaces ONLY the edited axis's capabilities in the
    stored set, preserving any granular capabilities on the other axes."""
    caps = set(role.capabilities)
    for key, (axis, valid) in _LEVEL_INPUT.items():
        if key in attrs:
            level = _level(attrs, key, "", valid)
            caps -= axis_all_caps(axis)
            caps |= AXIS_LEVEL_MAPS[axis].get(level, frozenset())
    role.capabilities = sorted(caps)


def _role_json(role: Role) -> dict:
    # Levels are NOT stored — derive them as a display summary from the persisted
    # capability set (a preset name per axis, or "custom").
    summary = summarize_capabilities(role.capabilities)
    return {
        "name": role.name,
        "type": "roles",
        "attributes": {
            "description": role.description or "",
            "allow-labels": role.allow_labels,
            "allow-names": role.allow_names,
            "deny-labels": role.deny_labels,
            "deny-names": role.deny_names,
            # Derived, read-only summary of the capabilities (not persisted).
            "workspace-permission": summary["workspace_permission"],
            "pool-permission": summary["pool_permission"],
            "registry-permission": summary["registry_permission"],
            "catalog-permission": summary["catalog_permission"],
            # The role's grant + the single source of truth for enforcement (#585).
            "capabilities": list(role.capabilities),
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

    # The role's grant is the persisted capability set — the single source of
    # truth (#585). An explicit `capabilities` set is stored verbatim; otherwise
    # the level shorthand (validated here) is expanded into it. Levels are never
    # stored.
    capabilities = (
        _validate_capabilities(attrs["capabilities"])
        if "capabilities" in attrs
        else _caps_from_level_input(attrs)
    )
    role = Role(
        name=name,
        description=attrs.get("description", ""),
        allow_labels=attrs.get("allow-labels", {}),
        allow_names=attrs.get("allow-names", []),
        deny_labels=attrs.get("deny-labels", {}),
        deny_names=attrs.get("deny-names", []),
        capabilities=capabilities,
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

    # The role's grant is the persisted capabilities. An explicit `capabilities`
    # set replaces it wholesale; otherwise a level edit replaces only the edited
    # axis's capabilities (preserving granular caps on the other axes). Levels are
    # never stored. Capability-authoring wins over any level fields sent alongside.
    if "capabilities" in attrs:
        role.capabilities = _validate_capabilities(attrs["capabilities"])
    elif _LEVEL_INPUT.keys() & attrs.keys():
        _apply_level_edits(role, attrs)

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
