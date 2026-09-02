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
from terrapod.services import variable_service, workspace_search_service
from terrapod.services.vault_source_service import (
    VALUE_SOURCES,
    VaultSourceError,
    parse_reference,
)
from terrapod.services.workspace_rbac_service import (
    resolve_workspace_capabilities_for,
)

router = APIRouter(prefix="/api/v2", tags=["variables"])

#: The association views (#1440) are Terrapod-native, not part of the TFE V2
#: surface `tfci` consumes, so they are mounted at `/api/terrapod/v1/` by the app
#: factory rather than sitting alongside the CLI-contract routes above.
native_router = APIRouter(tags=["variables"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _visible_value(var) -> str | None:
    """The value the API may return: masked when secret, shown when a reference."""
    if var.value_source == "vault":
        return var.value
    return None if var.sensitive else var.value


def _validated_value_source(attrs: dict, current: str = "static") -> str:
    """Validate `value-source`, and the reference that must accompany `vault`.

    Validated at write time rather than at run time so a malformed reference is
    an error the operator sees now, not a run that fails hours later.
    """
    if "value-source" not in attrs:
        return current
    src = attrs["value-source"] or "static"
    if src not in VALUE_SOURCES:
        raise HTTPException(
            status_code=422, detail=f"value-source must be one of {sorted(VALUE_SOURCES)}"
        )
    return src


def _reject_vault_on_local(ws, value_source: str) -> None:
    """A vault reference only resolves on the agent path.

    `resolve_vault_variables` runs in the listener claim endpoint, so a
    local-execution workspace would receive no value, no error and no failed
    run — the one outcome this feature is built to prevent. Refuse it at the
    point of writing rather than fail silently at run time.
    """
    if value_source != "vault":
        return
    if getattr(ws, "execution_mode", "agent") == "local":
        raise HTTPException(
            status_code=422,
            detail="A Vault-sourced variable needs agent execution: Terrapod "
            "resolves the reference server-side when a runner claims the run, "
            "and a local-execution workspace runs terraform on your own machine "
            "where that never happens. Switch the workspace to agent execution, "
            "or supply the value another way.",
        )


def _apply_value_source(attrs: dict, current: str = "static") -> tuple[str, bool]:
    """Resolve `value-source` for a write and validate its reference.

    Returns ``(value_source, force_sensitive)``. A vault-sourced variable is
    always sensitive: what it resolves to is a secret, even though the reference
    itself is not.
    """
    src = _validated_value_source(attrs, current)
    if src != "vault":
        if current == "vault" and "value" not in attrs:
            # Symmetric with the flip the other way. Without this the JSON
            # reference stayed as the literal value and the runner delivered
            # `{"mount":...}` to terraform as the credential — silently.
            raise HTTPException(
                status_code=422,
                detail="changing value-source away from 'vault' requires a "
                "replacement `value`; the stored reference is not a usable "
                "literal and would be delivered to the run as one",
            )
        return src, False
    if "value" in attrs:
        # Whenever a value is supplied on a vault-sourced variable it must be a
        # valid reference — including on an existing one, or a PATCH could
        # replace a good reference with anything.
        try:
            parse_reference(attrs.get("value") or "", key=attrs.get("key", "<variable>"))
        except VaultSourceError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    elif current != "vault":
        # A *transition* to vault must carry the reference. Without this, a
        # value-less flip left the previous literal in place and the
        # reference-is-not-a-secret display rule then returned it — a write
        # capability became a read-back of a stored secret.
        #
        # Only on the transition: a variable already sourced from vault is
        # edited for its key, description or category without resupplying the
        # reference, and requiring one there broke every partial update.
        raise HTTPException(
            status_code=422,
            detail="value-source 'vault' requires a reference in `value` "
            '(for example {"mount":"secret","path":"apps/x","field":"token"}); '
            "supply one rather than converting an existing value in place",
        )
    return src, True


def _var_json(var: Variable) -> dict:
    """Serialize a Variable to TFE V2 JSON:API format."""
    return {
        "id": f"var-{var.id}",
        "type": "vars",
        "attributes": {
            "key": var.key,
            # A vault-sourced value is a *reference*, not a secret — the path you
            # configured, which you need to be able to see. The secret it points
            # at is resolved at run time and never persisted, returned or logged
            # (#1439). Masking the reference would make the variable unreadable
            # without hiding anything that is actually sensitive.
            "value": _visible_value(var),
            "sensitive": var.sensitive,
            "category": var.category,
            "hcl": var.hcl,
            "value-source": var.value_source,
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

    value_source, force_sensitive = _apply_value_source(attrs)
    _reject_vault_on_local(ws, value_source)

    try:
        var = await variable_service.create_variable(
            db,
            workspace_id=ws.id,
            key=key,
            value=attrs.get("value", ""),
            category=attrs.get("category", "terraform"),
            description=attrs.get("description", ""),
            hcl=attrs.get("hcl", False),
            sensitive=force_sensitive or attrs.get("sensitive", False),
            value_source=value_source,
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
        value_source, _force = _apply_value_source(attrs, var.value_source)
        _reject_vault_on_local(ws, value_source)
        var = await variable_service.update_variable(
            db,
            var,
            key=attrs.get("key"),
            value=attrs.get("value"),
            category=attrs.get("category"),
            description=attrs.get("description"),
            hcl=attrs.get("hcl"),
            sensitive=attrs.get("sensitive"),
            value_source=value_source,
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
            # #1440. Null means the set uses explicit assignment (or is global);
            # a filter means membership is derived per run from workspace
            # attributes, so `workspace-count` above — which counts only explicit
            # rows — is not the whole answer. The association views
            # (`/varsets/{id}/relationships/workspaces`) are.
            "assignment-rule": vs.assignment_rule,
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


def _validated_assignment_rule(attrs: dict) -> dict | None:
    """Validate an assignment rule at write time (#1440).

    Parsed here rather than only at evaluation because a rule that does not
    parse matches nothing — which would leave an operator staring at a set that
    silently applies to no workspace, with nothing to tell them why. Rejecting
    it at the point of writing turns a silent no-op into an error they can act
    on.

    A rule and `global` together is contradictory rather than additive: global
    already means every workspace, so a filter alongside it could only ever
    narrow nothing. Reject rather than pick a winner.
    """
    if "assignment-rule" not in attrs:
        return None
    rule = attrs["assignment-rule"]
    if rule in (None, {}):
        return None
    if not isinstance(rule, dict):
        raise HTTPException(status_code=422, detail="assignment-rule must be an object")
    if attrs.get("global"):
        raise HTTPException(
            status_code=422,
            detail="A global variable set already applies to every workspace; "
            "it cannot also carry an assignment-rule",
        )
    # Normalise first: parse_filter accepts hyphens, so an underscore-only guard
    # below would be decorative — `workspace-ids` sailed straight past it.
    rule = {str(k).replace("-", "_"): v for k, v in rule.items()}

    if "workspace_ids" in rule:
        # A literal list of ids is not a rule — it is explicit assignment, which
        # the relationships endpoint already does and the UI already surfaces as
        # its own tab. Allowing both would mean two mechanisms for one thing, and
        # a rule whose membership never actually re-evaluates.
        raise HTTPException(
            status_code=422,
            detail="assignment-rule cannot use 'workspace_ids'; assign those "
            "workspaces explicitly instead. A rule selects by attributes so that "
            "membership re-evaluates as workspaces change.",
        )
    if rule.get("all"):
        # `all: true` inside a rule is the one shape that silently widens a
        # scoped credential set to the entire estate, and it duplicates a thing
        # the API already expresses properly. Point at `global` rather than
        # quietly accepting a second spelling of it.
        raise HTTPException(
            status_code=422,
            detail="assignment-rule cannot use 'all'; set the variable set to "
            "global instead if it should apply to every workspace",
        )
    # A falsy `all` is harmless but storable, and anything storable that a
    # typed consumer (the Terraform provider) does not model comes back as
    # perpetual plan drift. Drop it so the stored shape is exactly the set of
    # scoping dimensions, and the provider can model that set completely.
    rule = {k: v for k, v in rule.items() if k != "all"}
    try:
        parsed = workspace_search_service.parse_filter(rule)
        # Build the query too, not just parse. The "at least one selector" check
        # lives in the builder, so parsing alone accepted rules that select
        # nothing — and a blank string counted as a dimension while the builder
        # skipped it, so `{"name_prefix": ""}` produced a query with no WHERE
        # clause and matched EVERY workspace. Validating with the same builder
        # resolution uses is the only way these cannot diverge.
        workspace_search_service.build_workspace_query(parsed)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid assignment-rule: {e}") from e
    return rule


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
        assignment_rule=_validated_assignment_rule(attrs),
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
    if "assignment-rule" in attrs:
        vs.assignment_rule = _validated_assignment_rule(attrs)

    # Validate the *resulting* state, not just the patch: a PATCH that only sets
    # `global` on a set that already carries a rule reaches the contradiction
    # without either field being newly invalid on its own.
    if vs.global_set and vs.assignment_rule:
        raise HTTPException(
            status_code=422,
            detail="A global variable set already applies to every workspace; "
            "it cannot also carry an assignment-rule",
        )

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
            # Same rule as a workspace variable: a vault reference is a path,
            # not a secret, so it is shown rather than masked (#1439).
            "value": _visible_value(vsv),
            "sensitive": vsv.sensitive,
            "category": vsv.category,
            "hcl": vsv.hcl,
            "value-source": vsv.value_source,
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

    value_source, force_sensitive = _apply_value_source(attrs)
    if force_sensitive:
        sensitive = True

    vsv = VariableSetVariable(
        variable_set_id=vs.id,
        value_source=value_source,
        key=key,
        value=value,
        description=attrs.get("description", ""),
        category=category,
        hcl=attrs.get("hcl", False),
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
    if "hcl" in attrs:
        vsv.hcl = attrs["hcl"]
    # Always run it, not only when the caller sends `value-source`: a PATCH that
    # supplies a new `value` on an existing vault variable must still have that
    # value validated as a reference.
    vsv.value_source, _vault_forced = _apply_value_source(attrs, vsv.value_source)
    was_sensitive = vsv.sensitive
    if "value" in attrs:
        vsv.value = attrs["value"]
        vsv.version_id = variable_service._version_hash(vsv.key, attrs["value"], vsv.category)
    # git-auth categories are always secret and can never be downgraded.
    # `or value_source == "vault"` matches variable_service.update_variable.
    # Omitting it let a PATCH carrying `sensitive: false` downgrade a
    # vault-sourced varset variable, whose resolved value is always a secret.
    force_sensitive = (
        vsv.category in variable_service.GIT_AUTH_CATEGORIES or vsv.value_source == "vault"
    )
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


@native_router.get("/vault/availability")
async def vault_availability(
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Whether the Vault value source is configured, and which instances exist.

    So the UI can offer the option only where it will work — presenting a source
    a deployment has not configured produces a variable that fails its first run.

    Names only. Addresses and credentials are infrastructure detail the person
    choosing a secret has no need for, and anyone who can write a variable needs
    to be able to pick an instance, so this is not admin-gated.
    """
    from terrapod.config import settings as _settings

    cfg = _settings.vault
    return JSONResponse(
        content={
            "data": {
                "type": "vault-availability",
                "id": "vault",
                "attributes": {
                    "enabled": cfg.enabled,
                    "instances": [i.name for i in cfg.instances] if cfg.enabled else [],
                    "default-instance": next((i.name for i in cfg.instances if i.default), "")
                    if cfg.enabled
                    else "",
                },
            }
        }
    )


@native_router.get("/varsets/{varset_id}/relationships/workspaces")
async def list_varset_workspaces(
    varset_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Which workspaces this variable set currently applies to, and why (#1440).

    Read-only by design. An explicit assignment is edited through the existing
    relationship endpoints; a global or rule-derived one has no per-workspace
    thing to edit, and offering a delete that silently did nothing would be
    worse than showing none.

    This is the blast-radius view: for a set carrying a credential, "who
    receives this" is a question an operator must be able to answer without
    reading the rule and simulating it in their head. Before this there was no
    GET on the relationship at all.
    """
    vs = await _get_varset(varset_id, db)

    rows = await variable_service.workspaces_for_varset(db, vs)
    items = [
        {
            "type": "workspaces",
            "id": f"ws-{ws.id}",
            "attributes": {"name": ws.name, "assignment-source": source},
        }
        for ws, source in rows
    ]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@native_router.get("/workspaces/{workspace_id}/varsets")
async def list_workspace_varsets(
    workspace_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Which variable sets apply to this workspace, and why (#1440).

    The other half of the same question, and the one that answers "where did
    this variable come from" — previously unanswerable from the workspace at
    all.

    Uses the same resolver as injection, so what is listed here is what the run
    will actually receive.
    """
    ws = await _get_workspace(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.WORKSPACE_READ):
        raise HTTPException(status_code=404, detail="Workspace not found")

    rows = await variable_service.applicable_varsets(db, ws.id)
    items = [
        {
            "type": "varsets",
            "id": f"varset-{vs.id}",
            "attributes": {
                "name": vs.name,
                "priority": vs.priority,
                "assignment-source": source,
                "variable-count": len(vs.variables),
            },
        }
        for vs, source in rows
    ]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


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
