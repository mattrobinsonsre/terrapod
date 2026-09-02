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
    POST   /api/terrapod/v1/roles/preview       — reach of an UNSAVED role body
    GET    /api/terrapod/v1/roles/{name}/preview — reach of a saved role
"""

from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin, require_admin_or_audit
from terrapod.api.pagination import paginate
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
from terrapod.services import role_reach_service

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
            # An estate-wide grant must be visible wherever the role is: a
            # role that reaches everything and looks like one that reaches
            # nothing is the failure this attribute exists to prevent.
            "allow-all": role.allow_all,
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
            "allow-all": False,
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
    request: Request = None,
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

    page_items, meta = paginate(data, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


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
        allow_all=bool(attrs.get("allow-all", False)),
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
    if "allow-all" in attrs:
        role.allow_all = bool(attrs["allow-all"])
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


def _role_from_attrs(attrs: dict) -> Role:
    """A transient Role from a request body, for previewing an UNSAVED rule.

    Never added to the session. Built through the same validation the create
    path uses, so a preview cannot accept a rule that a save would reject —
    otherwise the preview would be answering about a role that cannot exist.
    """
    capabilities = (
        _validate_capabilities(attrs["capabilities"])
        if "capabilities" in attrs
        else _caps_from_level_input(attrs)
    )
    return Role(
        name=attrs.get("name", "") or "(unsaved)",
        description=attrs.get("description", ""),
        allow_all=bool(attrs.get("allow-all", False)),
        allow_labels=attrs.get("allow-labels", {}) or {},
        allow_names=attrs.get("allow-names", []) or [],
        deny_labels=attrs.get("deny-labels", {}) or {},
        deny_names=attrs.get("deny-names", []) or [],
        capabilities=capabilities,
    )


def _preview_page(request: Request) -> tuple[int, int]:
    """`page[size]` / `page[number]` off the raw request, matching the shared
    pagination convention (no declared Query params, so the route contract is
    untouched). Capped: a preview is for reading, and an unbounded page would
    reintroduce the fleet-sized response this feature exists to avoid."""
    params = request.query_params
    try:
        size = int(params.get("page[size]", "25") or 25)
    except ValueError:
        size = 25
    try:
        number = int(params.get("page[number]", "1") or 1)
    except ValueError:
        number = 1
    size = max(1, min(size, 100))
    number = max(1, number)
    return size, (number - 1) * size


def _preview_json(role_name: str, result: dict) -> dict:
    return {
        "data": {
            "type": "role-previews",
            "id": role_name,
            "attributes": result,
        }
    }


@router.post("/roles/preview")
async def preview_unsaved_role(
    request: Request,
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Which workspaces an UNSAVED role body would reach.

    The point of the unsaved form: the allow/deny interaction is where rules go
    wrong, and seeing "matched 47, denied 3" while typing is what makes a deny
    rule safe to write. Read-only — nothing is persisted, and the same
    admin/audit gate as viewing roles applies, since the result reveals nothing
    a role listing plus a workspace listing would not.
    """
    attrs = body.get("data", {}).get("attributes", {})
    role = _role_from_attrs(attrs)
    limit, offset = _preview_page(request)
    result = await role_reach_service.preview_role_reach(
        db, role, limit=limit, offset=offset, viewer_roles=user.roles
    )
    return JSONResponse(content=_preview_json(role.name, result))


@router.get("/roles/{role_name}/preview")
async def preview_saved_role(
    request: Request,
    role_name: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Which workspaces a saved role currently reaches.

    Built-in roles are rejected rather than answered: `admin` and `audit` grant
    through the platform path on every workspace regardless of label rules, so a
    label-reach answer for them would be true and deeply misleading.
    """
    if is_builtin_role(role_name):
        raise HTTPException(
            status_code=422,
            detail=f"'{role_name}' is a built-in role granted through the platform path on "
            "every workspace, so it has no label-based reach to preview.",
        )
    result = await db.execute(select(Role).where(Role.name == role_name))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail=f"Role '{role_name}' not found")
    limit, offset = _preview_page(request)
    reach = await role_reach_service.preview_role_reach(
        db, role, limit=limit, offset=offset, viewer_roles=user.roles
    )
    return JSONResponse(content=_preview_json(role.name, reach))


# ── The reverse view: who can reach a given resource (#1456) ──────────

#: The resource kinds an access view is offered for, and how to load one.
#: Keyed by the URL segment so the routes stay honest about what they serve.
_ACCESS_KINDS: dict[str, tuple[str, str, str]] = {
    # url segment      -> (axis, model attribute name, id prefix to strip)
    "workspaces": ("workspace", "Workspace", "ws-"),
    "agent-pools": ("pool", "AgentPool", "apool-"),
    "registry-modules": ("registry", "RegistryModule", ""),
    "registry-providers": ("registry", "RegistryProvider", ""),
    "catalog-items": ("catalog", "CatalogItem", ""),
}


async def _resource_access(
    db, resource_kind: str, resource_id: str, viewer_roles: list[str]
) -> JSONResponse:
    """Shared body for the per-kind access routes below."""
    import uuid as _uuid

    from terrapod.db import models as _models
    from terrapod.services import role_reach_service

    axis, model_name, prefix = _ACCESS_KINDS[resource_kind]
    model = getattr(_models, model_name)
    try:
        pk = _uuid.UUID(resource_id.removeprefix(prefix))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"{resource_id!r} is not a valid id") from e

    obj = await db.get(model, pk)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{resource_kind} {resource_id} not found")

    result = await role_reach_service.resolve_resource_access(
        db, obj, axis=axis, kind=resource_kind, viewer_roles=viewer_roles
    )
    return JSONResponse(
        content={
            "data": {
                "type": "resource-access",
                "id": result["resource"]["id"],
                "attributes": result,
            }
        }
    )


_ACCESS_DOC = """Which roles reach this resource, at what capability, and who holds them.

    The inverse of the role preview, and the question asked when looking at one
    thing rather than at one role: *who can touch this?* Roles are few, so this
    evaluates every role against one resource — no pagination is needed.

    `platform-paths` is not decoration. A list of roles reads as the complete
    answer when it is not: a platform admin reaches everything, an owner holds
    admin on their own resource, and an `access: everyone` label makes a thing
    readable with no role involved at all. Answering "who can reach this" while
    omitting those would be worse than not answering it.
    """


@router.get("/workspaces/{resource_id}/access")
async def workspace_access(
    resource_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _resource_access(db, "workspaces", resource_id, user.roles)


@router.get("/agent-pools/{resource_id}/access")
async def agent_pool_access(
    resource_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _resource_access(db, "agent-pools", resource_id, user.roles)


@router.get("/registry-modules/{resource_id}/access")
async def registry_module_access(
    resource_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _resource_access(db, "registry-modules", resource_id, user.roles)


@router.get("/registry-providers/{resource_id}/access")
async def registry_provider_access(
    resource_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _resource_access(db, "registry-providers", resource_id, user.roles)


@router.get("/catalog-items/{resource_id}/access")
async def catalog_item_access(
    resource_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    return await _resource_access(db, "catalog-items", resource_id, user.roles)


workspace_access.__doc__ = _ACCESS_DOC
agent_pool_access.__doc__ = _ACCESS_DOC
registry_module_access.__doc__ = _ACCESS_DOC
registry_provider_access.__doc__ = _ACCESS_DOC
catalog_item_access.__doc__ = _ACCESS_DOC
