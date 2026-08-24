"""TFE V2 compatible variable CRUD endpoints.

UX CONTRACT: Variable endpoints are consumed by the web frontend:
  - web/src/app/workspaces/[id]/page.tsx (variables tab)
  - web/src/app/admin/variable-sets/page.tsx (variable set list + create)
  - web/src/app/admin/variable-sets/[id]/page.tsx (variable set detail + edit)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to those frontend pages.

Endpoints:
    GET/POST       /api/v2/workspaces/{id}/vars
    PATCH/DELETE   /api/v2/workspaces/{id}/vars/{var_id}
    POST/GET       /api/v2/organizations/default/varsets
    GET/PATCH/DELETE /api/v2/varsets/{varset_id}
    POST/GET/PATCH/DELETE /api/terrapod/v1/varsets/{varset_id}/relationships/vars[/{var_id}]
    POST/DELETE    /api/v2/varsets/{varset_id}/relationships/workspaces
"""

import uuid
from datetime import UTC

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.api.pagination import paginate
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import (
    Variable,
    VariableSet,
    VariableSetVariable,
    VariableSetWorkspace,
    Workspace,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import variable_service
from terrapod.services.workspace_rbac_service import (
    resolve_workspace_capabilities_for,
)

router = APIRouter(prefix="/api/v2", tags=["variables"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _var_json(var: Variable) -> dict:
    """Serialize a Variable to TFE V2 JSON:API format."""
    return {
        "id": f"var-{var.id}",
        "type": "vars",
        "attributes": {
            "key": var.key,
            "value": None if var.sensitive else var.value,
            "sensitive": var.sensitive,
            "category": var.category,
            # Both names, always equal. `hcl` is what go-tfe reads (#1435).
            "structured": var.structured,
            "hcl": var.structured,
            "description": var.description,
            "version-id": var.version_id,
            "created-at": _rfc3339(var.created_at),
            "updated-at": _rfc3339(var.updated_at),
        },
        "relationships": {
            "configurable": {
                "data": {
                    "id": f"ws-{var.workspace_id}",
                    "type": "workspaces",
                },
            },
        },
    }


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


# ── Workspace Variables ──────────────────────────────────────────────────


def _structured_from(attrs: dict, *, default: bool | None = None) -> bool | None:
    """Read the flag under either name, preferring `structured` (#1435).

    `hcl` is not deprecated here and never will be: this is `/api/v2`, and `tfci`
    and `go-tfe` send it. Accepting both is permanent, not transitional.

    Supplying both with different values is a 422 rather than a silent
    precedence rule — a client that disagrees with itself about whether a value
    is typed has a bug, and picking a winner would hide it.
    """
    has_structured = "structured" in attrs
    has_hcl = "hcl" in attrs
    if has_structured and has_hcl and bool(attrs["structured"]) != bool(attrs["hcl"]):
        raise HTTPException(
            status_code=422,
            detail=(
                "'structured' and 'hcl' are the same flag under two names and "
                "disagree; send either one, or both with the same value"
            ),
        )
    if has_structured:
        return bool(attrs["structured"])
    if has_hcl:
        return bool(attrs["hcl"])
    return default


@router.get("/workspaces/{workspace_id}/vars")
async def list_workspace_vars(
    workspace_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all variables for a workspace. Requires read."""
    ws = await _get_workspace(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.VAR_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires read permission on workspace"
        )
    variables = await variable_service.list_variables(db, ws.id)
    items = [_var_json(v) for v in variables]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/workspaces/{workspace_id}/vars", status_code=201)
async def create_workspace_var(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a variable for a workspace. Requires write."""
    ws = await _get_workspace(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.VAR_WRITE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires write permission on workspace"
        )

    attrs = body.get("data", {}).get("attributes", {})
    key = attrs.get("key", "")
    if not key:
        raise HTTPException(status_code=422, detail="Variable key is required")

    try:
        var = await variable_service.create_variable(
            db,
            workspace_id=ws.id,
            key=key,
            value=attrs.get("value", ""),
            category=attrs.get("category", "terraform"),
            description=attrs.get("description", ""),
            structured=_structured_from(attrs, default=False),
            sensitive=attrs.get("sensitive", False),
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "workspace_variable_change")

    return JSONResponse(content={"data": _var_json(var)}, status_code=201)


@router.patch("/workspaces/{workspace_id}/vars/{var_id}")
async def update_workspace_var(
    workspace_id: str = Path(...),
    var_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a workspace variable. Requires write."""
    ws = await _get_workspace(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.VAR_WRITE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires write permission on workspace"
        )
    var_uuid = uuid.UUID(var_id.removeprefix("var-"))

    var = await variable_service.get_variable(db, ws.id, var_uuid)
    if var is None:
        raise HTTPException(status_code=404, detail="Variable not found")

    attrs = body.get("data", {}).get("attributes", {})

    try:
        var = await variable_service.update_variable(
            db,
            var,
            key=attrs.get("key"),
            value=attrs.get("value"),
            category=attrs.get("category"),
            description=attrs.get("description"),
            structured=_structured_from(attrs),
            sensitive=attrs.get("sensitive"),
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "workspace_variable_change")

    return JSONResponse(content={"data": _var_json(var)})


@router.delete("/workspaces/{workspace_id}/vars/{var_id}", status_code=204)
async def delete_workspace_var(
    workspace_id: str = Path(...),
    var_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a workspace variable. Requires write."""
    ws = await _get_workspace(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.VAR_WRITE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires write permission on workspace"
        )
    var_uuid = uuid.UUID(var_id.removeprefix("var-"))

    var = await variable_service.get_variable(db, ws.id, var_uuid)
    if var is None:
        raise HTTPException(status_code=404, detail="Variable not found")

    await variable_service.delete_variable(db, var)
    await db.commit()

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "workspace_variable_change")


# ── Variable Sets ────────────────────────────────────────────────────────


def _varset_json(vs: VariableSet) -> dict:
    """Serialize a VariableSet to TFE V2 JSON:API format."""
    # Count variables and workspaces from eagerly loaded relationships
    var_count = len(vs.variables) if "variables" in sa.inspect(vs).dict else 0
    ws_assignments = (
        vs.workspace_assignments if "workspace_assignments" in sa.inspect(vs).dict else []
    )
    ws_count = len(ws_assignments)

    relationships: dict = {
        "organization": {
            "data": {"id": "default", "type": "organizations"},
        },
    }

    # Include workspace relationship data when loaded
    if ws_assignments:
        relationships["workspaces"] = {
            "data": [
                {
                    "id": f"ws-{a.workspace_id}",
                    "type": "workspaces",
                    "attributes": {
                        "name": a.workspace.name if a.workspace else str(a.workspace_id)
                    },
                }
                for a in ws_assignments
            ],
        }
    else:
        relationships["workspaces"] = {"data": []}

    return {
        "id": f"varset-{vs.id}",
        "type": "varsets",
        "attributes": {
            "name": vs.name,
            "description": vs.description,
            "global": vs.global_set,
            "priority": vs.priority,
            "var-count": var_count,
            "workspace-count": ws_count,
            "created-at": _rfc3339(vs.created_at),
            "updated-at": _rfc3339(vs.updated_at),
        },
        "relationships": relationships,
    }


@router.get("/organizations/default/varsets")
async def list_varsets(
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all variable sets."""

    result = await db.execute(
        select(VariableSet)
        .options(
            selectinload(VariableSet.variables), selectinload(VariableSet.workspace_assignments)
        )
        .order_by(VariableSet.name)
    )
    varsets = result.scalars().all()
    items = [_varset_json(vs) for vs in varsets]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/organizations/default/varsets", status_code=201)
async def create_varset(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a variable set. Requires admin."""

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "")
    if not name:
        raise HTTPException(status_code=422, detail="Variable set name is required")

    vs = VariableSet(
        name=name,
        description=attrs.get("description", ""),
        global_set=attrs.get("global", False),
        priority=attrs.get("priority", False),
    )
    db.add(vs)
    await db.commit()
    await db.refresh(vs)

    return JSONResponse(content={"data": _varset_json(vs)}, status_code=201)


async def _get_varset(varset_id: str, db: AsyncSession) -> VariableSet:
    vs_uuid = uuid.UUID(varset_id.removeprefix("varset-"))
    result = await db.execute(
        select(VariableSet)
        .where(VariableSet.id == vs_uuid)
        .options(
            selectinload(VariableSet.variables), selectinload(VariableSet.workspace_assignments)
        )
    )
    vs = result.scalar_one_or_none()
    if vs is None:
        raise HTTPException(status_code=404, detail="Variable set not found")
    return vs


@router.get("/varsets/{varset_id}")
async def show_varset(
    varset_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a variable set."""
    vs = await _get_varset(varset_id, db)
    return JSONResponse(content={"data": _varset_json(vs)})


@router.patch("/varsets/{varset_id}")
async def update_varset(
    varset_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a variable set. Requires admin."""
    vs = await _get_varset(varset_id, db)

    attrs = body.get("data", {}).get("attributes", {})
    if "name" in attrs:
        vs.name = attrs["name"]
    if "description" in attrs:
        vs.description = attrs["description"]
    if "global" in attrs:
        vs.global_set = attrs["global"]
    if "priority" in attrs:
        vs.priority = attrs["priority"]

    await db.commit()
    await db.refresh(vs)
    return JSONResponse(content={"data": _varset_json(vs)})


@router.delete("/varsets/{varset_id}", status_code=204)
async def delete_varset(
    varset_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a variable set. Requires admin."""
    vs = await _get_varset(varset_id, db)
    await db.delete(vs)
    await db.commit()


# ── Variable Set Variables ───────────────────────────────────────────────


def _vsvar_json(vsv: VariableSetVariable, varset_id: str) -> dict:
    """Serialize a VariableSetVariable."""
    return {
        "id": f"var-{vsv.id}",
        "type": "vars",
        "attributes": {
            "key": vsv.key,
            "value": None if vsv.sensitive else vsv.value,
            "sensitive": vsv.sensitive,
            "category": vsv.category,
            "structured": vsv.structured,
            "hcl": vsv.structured,
            "description": vsv.description,
            "version-id": vsv.version_id,
            "created-at": _rfc3339(vsv.created_at),
            "updated-at": _rfc3339(vsv.updated_at),
        },
        "relationships": {
            "varset": {
                "data": {"id": varset_id, "type": "varsets"},
            },
        },
    }


@router.get("/varsets/{varset_id}/relationships/vars")
async def list_varset_vars(
    varset_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List variables in a variable set."""
    vs = await _get_varset(varset_id, db)
    await db.refresh(vs, ["variables"])
    items = [_vsvar_json(v, varset_id) for v in vs.variables]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/varsets/{varset_id}/relationships/vars", status_code=201)
async def create_varset_var(
    varset_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a variable in a variable set. Requires admin."""
    vs = await _get_varset(varset_id, db)

    attrs = body.get("data", {}).get("attributes", {})
    key = attrs.get("key", "")
    if not key:
        raise HTTPException(status_code=422, detail="Variable key is required")

    value = attrs.get("value", "")
    sensitive = attrs.get("sensitive", False)
    try:
        category = variable_service._validated_category(attrs.get("category", "terraform"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if category in variable_service.GIT_AUTH_CATEGORIES:
        sensitive = True  # git-auth values are always secret

    vsv = VariableSetVariable(
        variable_set_id=vs.id,
        key=key,
        value=value,
        description=attrs.get("description", ""),
        category=category,
        structured=_structured_from(attrs, default=False),
        sensitive=sensitive,
        version_id=variable_service._version_hash(key, value, category),
    )
    db.add(vsv)
    await db.commit()
    await db.refresh(vsv)

    return JSONResponse(content={"data": _vsvar_json(vsv, varset_id)}, status_code=201)


@router.patch("/varsets/{varset_id}/relationships/vars/{var_id}")
async def update_varset_var(
    varset_id: str = Path(...),
    var_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a variable in a variable set. Requires admin."""
    vs = await _get_varset(varset_id, db)
    var_uuid = uuid.UUID(var_id.removeprefix("var-"))

    result = await db.execute(
        select(VariableSetVariable).where(
            VariableSetVariable.id == var_uuid,
            VariableSetVariable.variable_set_id == vs.id,
        )
    )
    vsv = result.scalar_one_or_none()
    if vsv is None:
        raise HTTPException(status_code=404, detail="Variable not found")

    attrs = body.get("data", {}).get("attributes", {})
    if "key" in attrs:
        vsv.key = attrs["key"]
    if "description" in attrs:
        vsv.description = attrs["description"]
    if "category" in attrs:
        try:
            vsv.category = variable_service._validated_category(attrs["category"])
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    supplied = _structured_from(attrs)
    if supplied is not None:
        vsv.structured = supplied
    was_sensitive = vsv.sensitive
    if "value" in attrs:
        vsv.value = attrs["value"]
        vsv.version_id = variable_service._version_hash(vsv.key, attrs["value"], vsv.category)
    # git-auth categories are always secret and can never be downgraded.
    force_sensitive = vsv.category in variable_service.GIT_AUTH_CATEGORIES
    if force_sensitive:
        vsv.sensitive = True
    elif "sensitive" in attrs:
        vsv.sensitive = attrs["sensitive"]

    # Security: a sensitive → non-sensitive downgrade must not expose the
    # previously-hidden value. If no fresh value was supplied in the same
    # request, clear it so the old secret is never returned in plaintext
    # (mirrors variable_service.update_variable for workspace vars).
    if not force_sensitive and was_sensitive and vsv.sensitive is False and "value" not in attrs:
        vsv.value = ""
        vsv.version_id = variable_service._version_hash(vsv.key, "", vsv.category)

    await db.commit()
    await db.refresh(vsv)
    return JSONResponse(content={"data": _vsvar_json(vsv, varset_id)})


@router.delete("/varsets/{varset_id}/relationships/vars/{var_id}", status_code=204)
async def delete_varset_var(
    varset_id: str = Path(...),
    var_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a variable from a variable set. Requires admin."""
    vs = await _get_varset(varset_id, db)
    var_uuid = uuid.UUID(var_id.removeprefix("var-"))

    result = await db.execute(
        select(VariableSetVariable).where(
            VariableSetVariable.id == var_uuid,
            VariableSetVariable.variable_set_id == vs.id,
        )
    )
    vsv = result.scalar_one_or_none()
    if vsv is None:
        raise HTTPException(status_code=404, detail="Variable not found")

    await db.delete(vsv)
    await db.commit()


# ── Variable Set Workspace Assignments ───────────────────────────────────


@router.post("/varsets/{varset_id}/relationships/workspaces", status_code=204)
async def add_varset_workspaces(
    varset_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Assign workspaces to a variable set. Requires admin."""
    vs = await _get_varset(varset_id, db)

    data = body.get("data", [])
    for item in data:
        ws_id = item.get("id", "").removeprefix("ws-")
        try:
            ws_uuid = uuid.UUID(ws_id)
        except ValueError:
            continue

        # Check workspace exists
        ws = await db.get(Workspace, ws_uuid)
        if ws is None:
            continue

        # Check not already assigned
        existing = await db.execute(
            select(VariableSetWorkspace).where(
                VariableSetWorkspace.variable_set_id == vs.id,
                VariableSetWorkspace.workspace_id == ws_uuid,
            )
        )
        if existing.scalar_one_or_none() is None:
            db.add(VariableSetWorkspace(variable_set_id=vs.id, workspace_id=ws_uuid))

    await db.commit()


@router.delete("/varsets/{varset_id}/relationships/workspaces", status_code=204)
async def remove_varset_workspaces(
    varset_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove workspaces from a variable set. Requires admin."""
    vs = await _get_varset(varset_id, db)

    data = body.get("data", [])
    for item in data:
        ws_id = item.get("id", "").removeprefix("ws-")
        try:
            ws_uuid = uuid.UUID(ws_id)
        except ValueError:
            continue

        result = await db.execute(
            select(VariableSetWorkspace).where(
                VariableSetWorkspace.variable_set_id == vs.id,
                VariableSetWorkspace.workspace_id == ws_uuid,
            )
        )
        vsw = result.scalar_one_or_none()
        if vsw:
            await db.delete(vsw)

    await db.commit()
