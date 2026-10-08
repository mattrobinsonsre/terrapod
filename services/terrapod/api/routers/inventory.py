"""Ansible inventory: the eight structures, each addressable (#1967, #1968).

Native surface only. None of this is on the TFE-compatible prefix: no
`terraform`, `tofu` or `tfci` invocation consumes it, so by the rule in
`docs/tfe-cli-surface.md` it belongs under the Terrapod-native prefix.

## One route shape, eight structures

Ansible's inventory has one-to-many and many-to-many mappings, so each
structure is its own addressable resource: hosts, groups, the memberships
between them, the nestings between groups, and variables on a host, on a group
or inventory-wide. Each follows the same pattern -- a nested `POST` under its
parent, then top-level `GET` / `PATCH` / `DELETE` by typed id.

That the joins are addressable is the point rather than an accident. A Terraform
resource needs a row it can address, and a membership that is only a list on one
side cannot be one. `workspace_remote_state_consumers` made the same call for
the same reason; `variable_set_workspaces` chose a composite primary key and
consequently has no route at all.

## Authorization, and the one implicit grant

Two kinds of caller:

* **A person or an API token** needs `inventory:read` to read and
  `inventory:write` to change. `write` is deliberate rather than `admin`: the
  Terraform that declares hosts runs under an apply, and an API stricter than
  the path every row actually arrives by would be incoherent.

* **A runner token** may manage the inventory of **its own run's workspace** and
  nothing else. That is the single implicit grant #1968 asks for, and it is the
  same shape as the implicit registry read runner tokens already carry
  (`capability_resolver`) for the same reason: `terraform apply` cannot work
  without it. It is enforced here rather than as a capability floor because the
  grant is scoped to one workspace -- the one its run belongs to -- and the
  capability resolver has no way to know which that is.

  **Writes are bound to the apply phase**; reads are unphased. A plan has to
  read inventory to diff it and never writes, so this follows the phase claim
  (GHSA-xmrf-hxq9-m59m) without needing a new concept.

## Why the integrity errors are translated rather than caught by checking first

A create does not verify that its parents exist or that they share the
workspace. The composite foreign keys answer both, and a `SELECT` first would be
check-then-act: two concurrent applies could each pass the check and then
collide, which is exactly the race the constraint exists to settle. So the write
is attempted and the driver's SQLSTATE decides the status -- `23505` is the
caller's duplicate (409, which is what makes a Terraform import the next step),
`23503` is a parent that is absent or in another workspace (422).
"""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.ids import parse_id_for, strip_id_prefix
from terrapod.api.pagination import paginate
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import (
    InventoryGlobalVar,
    InventoryGroup,
    InventoryGroupChild,
    InventoryGroupVar,
    InventoryHost,
    InventoryHostGroup,
    InventoryHostVar,
    InventorySettings,
    Run,
    Workspace,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import inventory_service as inv
from terrapod.services.inventory_resolution import InventoryValidationError
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

router = APIRouter(tags=["inventory"])
logger = get_logger(__name__)

#: Postgres SQLSTATEs, read off the driver exception rather than matched in the
#: message text -- a constraint name is not a contract and differs between
#: backends, while these are in the SQL standard.
_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"
#: A CHECK constraint. Translated because two of them are reachable from a
#: write -- a group nested inside itself, and settings naming a repository with
#: no connection -- and both are the caller's input. The service refuses each
#: first with a better message, so this is the backstop; without it the backstop
#: answered 500, which is the one status that reads as "our fault" for a request
#: that is plainly the caller's.
_CHECK_VIOLATION = "23514"

#: What a masked variable reads as. A fixed string rather than the value's
#: length or shape, because either leaks something about it.
_MASKED = "***"


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Serializers ──────────────────────────────────────────────────────────────


def _ws_rel(workspace_id: uuid.UUID) -> dict:
    return {"workspace": {"data": {"id": f"ws-{workspace_id}", "type": "workspaces"}}}


def _settings_json(settings: InventorySettings) -> dict:
    """The settings, identified by the workspace because that IS the key.

    One inventory per workspace, so there is no surrogate id to carry and the
    workspace id is the only honest identifier.
    """
    return {
        "id": f"ws-{settings.workspace_id}",
        "type": "inventory-settings",
        "attributes": {
            "include-platform": settings.include_platform,
            "repo-url": settings.repo_url or "",
            "branch": settings.branch or "",
            "working-directory": settings.working_directory or "",
            "ignore-paths": list(settings.ignore_paths or []),
            "created-at": _rfc3339(settings.created_at),
            "updated-at": _rfc3339(settings.updated_at),
        },
        "relationships": {
            **_ws_rel(settings.workspace_id),
            "vcs-connection": {
                "data": (
                    {
                        "id": f"vcs-{settings.vcs_connection_id}",
                        "type": "vcs-connections",
                    }
                    if settings.vcs_connection_id
                    else None
                )
            },
        },
    }


def _host_json(host: InventoryHost, *, groups: int = 0, variables: int = 0) -> dict:
    return {
        "id": f"invhost-{host.id}",
        "type": "inventory-hosts",
        "attributes": {
            "name": host.name,
            # Counts, not the rows: a host list shows "3 groups, 2 variables"
            # and embedding either would make one request grow with the whole
            # inventory. The rows are one drill-down away.
            "group-count": groups,
            "variable-count": variables,
            "created-at": _rfc3339(host.created_at),
            "updated-at": _rfc3339(host.updated_at),
        },
        "relationships": _ws_rel(host.workspace_id),
    }


def _group_json(
    group: InventoryGroup, *, members: int = 0, children: int = 0, variables: int = 0
) -> dict:
    return {
        "id": f"invgroup-{group.id}",
        "type": "inventory-groups",
        "attributes": {
            "name": group.name,
            "member-count": members,
            "child-count": children,
            "variable-count": variables,
            "created-at": _rfc3339(group.created_at),
            "updated-at": _rfc3339(group.updated_at),
        },
        "relationships": _ws_rel(group.workspace_id),
    }


def _host_group_json(link: InventoryHostGroup) -> dict:
    return {
        "id": f"invhg-{link.id}",
        "type": "inventory-host-groups",
        "attributes": {"created-at": _rfc3339(link.created_at)},
        "relationships": {
            **_ws_rel(link.workspace_id),
            "host": {"data": {"id": f"invhost-{link.host_id}", "type": "inventory-hosts"}},
            "group": {"data": {"id": f"invgroup-{link.group_id}", "type": "inventory-groups"}},
        },
    }


def _group_child_json(link: InventoryGroupChild) -> dict:
    return {
        "id": f"invgc-{link.id}",
        "type": "inventory-group-children",
        "attributes": {"created-at": _rfc3339(link.created_at)},
        "relationships": {
            **_ws_rel(link.workspace_id),
            "parent-group": {
                "data": {"id": f"invgroup-{link.parent_group_id}", "type": "inventory-groups"}
            },
            "child-group": {
                "data": {"id": f"invgroup-{link.child_group_id}", "type": "inventory-groups"}
            },
        },
    }


def _var_json(
    var: InventoryHostVar | InventoryGroupVar | InventoryGlobalVar,
    *,
    var_id: str,
    type_name: str,
    parent: dict | None = None,
) -> dict:
    """One variable. A sensitive value is masked, never returned.

    `sensitive` is a DISPLAY flag and nothing more: every value is encrypted at
    rest (`EncryptedText`, registered in `crypto/columns.py`), because a column
    cannot be conditionally encrypted and a host variable is an ordinary place
    for a become password. So this flag decides what a reader sees, not what the
    database holds.

    `var_id` arrives already prefixed rather than being assembled from a prefix
    argument here. The prefix then sits as a literal beside the type name it
    accompanies in each of the three callers, which is both easier to read and
    what lets `test_id_prefix_tolerance` see it -- that gate scans for literals,
    so a prefix passed down as a parameter is invisible to it and would look
    like a table entry no serializer emits.
    """
    return {
        "id": var_id,
        "type": type_name,
        "attributes": {
            "key": var.key,
            "value": _MASKED if var.sensitive else (var.value or ""),
            "structured": var.structured,
            "sensitive": var.sensitive,
            "created-at": _rfc3339(var.created_at),
            "updated-at": _rfc3339(var.updated_at),
        },
        "relationships": {**_ws_rel(var.workspace_id), **(parent or {})},
    }


def _host_var_json(var: InventoryHostVar) -> dict:
    return _var_json(
        var,
        var_id=f"invhvar-{var.id}",
        type_name="inventory-host-vars",
        parent={"host": {"data": {"id": f"invhost-{var.host_id}", "type": "inventory-hosts"}}},
    )


def _group_var_json(var: InventoryGroupVar) -> dict:
    return _var_json(
        var,
        var_id=f"invgvar-{var.id}",
        type_name="inventory-group-vars",
        parent={"group": {"data": {"id": f"invgroup-{var.group_id}", "type": "inventory-groups"}}},
    )


def _global_var_json(var: InventoryGlobalVar) -> dict:
    return _var_json(var, var_id=f"invvar-{var.id}", type_name="inventory-global-vars")


# ── Lookups and authorization ────────────────────────────────────────────────


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = parse_id_for(workspace_id, "workspaces", detail="Workspace not found")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


async def _runner_run_workspace(db: AsyncSession, user: AuthenticatedUser) -> uuid.UUID | None:
    """The workspace of the run a runner token names, or None.

    The token's `run_id` is a bare uuid; liveness and signature were already
    settled when the principal was minted (`api/dependencies.py`), so this only
    has to answer which workspace the run belongs to.
    """
    raw = strip_id_prefix(user.run_id or "", "run-")
    try:
        run_uuid = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None
    result = await db.execute(select(Run.workspace_id).where(Run.id == run_uuid))
    return result.scalar_one_or_none()


async def _authorize(
    ws: Workspace,
    *,
    required: str,
    user: AuthenticatedUser,
    db: AsyncSession,
    phase: str | None = None,
) -> None:
    """Allow a runner token on its own workspace, else require the capability.

    See the module docstring for why the runner grant lives here rather than in
    the capability resolver.
    """
    if user.auth_method == "runner_token":
        run_workspace = await _runner_run_workspace(db, user)
        if run_workspace is None or run_workspace != ws.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Runner token is not scoped to a run on this workspace",
            )
        # Absence of a phase claim passes any phase -- a listener older than the
        # claim, exactly as `require_runner_for_run` treats it. The run-scoping
        # above still holds.
        if phase is not None and user.run_phase is not None and user.run_phase != phase:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Token not scoped to the {phase} phase of this run",
            )
        return

    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} capability on workspace",
        )


async def _host(host_id: str, db: AsyncSession) -> InventoryHost:
    host_uuid = parse_id_for(host_id, "inventory-hosts", detail="Host not found")
    host = await inv.get_host(db, host_uuid)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


async def _group(group_id: str, db: AsyncSession) -> InventoryGroup:
    group_uuid = parse_id_for(group_id, "inventory-groups", detail="Group not found")
    group = await inv.get_group(db, group_uuid)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


def _attrs(body: dict) -> dict:
    data = body.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="data must be an object")
    attrs = data.get("attributes")
    if attrs is None:
        return {}
    if not isinstance(attrs, dict):
        raise HTTPException(status_code=422, detail="data.attributes must be an object")
    return attrs


def _rel_id(body: dict, name: str, resource_type: str) -> uuid.UUID | None:
    """A relationship's id, parsed, or None when the relationship is absent.

    Relationships rather than `*-id` attributes, because these are links and
    the house style says a link is a relationship. There is no `*-id` attribute
    to keep compatible: none of this exists on any release.
    """
    data = body.get("data")
    rels = data.get("relationships") if isinstance(data, dict) else None
    if not isinstance(rels, dict):
        return None
    entry = rels.get(name)
    if not isinstance(entry, dict):
        return None
    inner = entry.get("data")
    if not isinstance(inner, dict):
        return None
    raw = inner.get("id")
    if not isinstance(raw, str) or not raw:
        return None
    return parse_id_for(raw, resource_type, detail=f"{name} not found", status=422)


def _rel_is_explicit_null(body: dict, name: str) -> bool:
    """Whether the caller sent `{"data": null}` rather than omitting it.

    The distinction a patch depends on: omitted means "leave it alone", an
    explicit null means "remove it". Collapsing them would make a binding
    impossible to clear without a full `PUT`.
    """
    data = body.get("data")
    rels = data.get("relationships") if isinstance(data, dict) else None
    if not isinstance(rels, dict) or name not in rels:
        return False
    entry = rels[name]
    return isinstance(entry, dict) and "data" in entry and entry["data"] is None


def _require_rel(body: dict, name: str, resource_type: str) -> uuid.UUID:
    value = _rel_id(body, name, resource_type)
    if value is None:
        raise HTTPException(
            status_code=422,
            detail=f"a {name} relationship is required: "
            f'{{"data": {{"relationships": {{"{name}": {{"data": {{"id": "...", '
            f'"type": "{resource_type}"}}}}}}}}}}',
        )
    return value


def _as_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise HTTPException(status_code=422, detail=f"{field} must be a list of strings")
    return list(value)


def _as_bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"{field} must be a boolean")
    return value


def _integrity_error(
    exc: IntegrityError,
    *,
    duplicate: str,
    parent: str,
    check: str = "That inventory row is not a valid shape",
) -> HTTPException:
    """Translate a write's `IntegrityError` into the status it deserves.

    Three cases, three genuinely different answers. A duplicate is the caller's
    input and a 409, which is what tells a practitioner to import the row that
    already exists. A dangling or cross-workspace parent is a 422: nothing
    conflicts, the request names something that is not there, and a 409 would
    send them looking for a collision. A CHECK violation is a 422 too -- the
    service refuses each reachable one first with a better message, so reaching
    here means the backstop caught it, and a backstop that answers 500 blames
    us for the caller's input.

    Neither echoes the constraint name: it is an internal detail, and the caller
    does not need it to fix either case.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == _UNIQUE_VIOLATION:
        return HTTPException(status_code=409, detail=duplicate)
    if sqlstate == _FOREIGN_KEY_VIOLATION:
        return HTTPException(status_code=422, detail=parent)
    if sqlstate == _CHECK_VIOLATION:
        return HTTPException(status_code=422, detail=check)
    # Anything else is a server-side problem, and guessing a 4xx for it would
    # hide our own bug behind a message blaming the caller.
    logger.warning("Unexpected inventory integrity error", sqlstate=sqlstate)
    return HTTPException(status_code=500, detail="Could not write the inventory row")


async def _fail(db: AsyncSession, exc: HTTPException) -> HTTPException:
    """Roll back, then return the status to raise.

    Every write path needs both, and forgetting the rollback leaves the session
    poisoned for whatever the request does next.
    """
    await db.rollback()
    return exc


# ── Settings ─────────────────────────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/inventory/settings")
async def show_inventory_settings(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The workspace's inventory settings.

    `404` when there are none, which is the ordinary state for a workspace whose
    inventory is entirely declared: the settings row exists only to bind a VCS
    source or to turn the declared rows off.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    settings = await inv.get_settings(db, ws.id)
    if settings is None:
        raise HTTPException(
            status_code=404,
            detail="This workspace has no inventory settings. That is the default: the "
            "declared hosts and groups are the whole inventory until a VCS source is bound.",
        )
    return JSONResponse(content={"data": _settings_json(settings)})


@router.put("/workspaces/{workspace_id}/inventory/settings")
async def put_inventory_settings(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create or replace the settings. A full replace, not a patch.

    `PUT` on a singleton, so the body is the complete intended state: an absent
    attribute takes its default rather than keeping the stored value. A caller
    changing one field reads first, which it has to do anyway to know the rest.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    attrs = _attrs(body)
    try:
        settings = await inv.put_settings(
            db,
            ws.id,
            include_platform=_as_bool(attrs.get("include-platform"), "include-platform", True),
            vcs_connection_id=_rel_id(body, "vcs-connection", "vcs-connections"),
            repo_url=attrs.get("repo-url") or "",
            branch=attrs.get("branch") or "",
            working_directory=attrs.get("working-directory") or "",
            ignore_paths=_as_str_list(attrs.get("ignore-paths"), "ignore-paths"),
        )
    except InventoryValidationError as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate="These inventory settings already exist",
                parent="That VCS connection does not exist",
                check="A repository needs a VCS connection to fetch it with",
            ),
        ) from exc

    await db.commit()
    logger.info("Inventory settings written", workspace=ws.name, by=user.email)
    return JSONResponse(content={"data": _settings_json(settings)})


@router.patch("/workspaces/{workspace_id}/inventory/settings")
async def patch_inventory_settings(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Change some of the settings and leave the rest alone.

    Beside the `PUT` rather than instead of it. A `PUT` is the right shape for
    Terraform, which always knows the whole intended state; a `PATCH` is the
    right shape for a person or a script changing one thing, and making them
    read the row first to avoid clobbering the others is work with nothing
    behind it.

    To REMOVE the VCS binding, send the relationship explicitly as null:
    `{"relationships": {"vcs-connection": {"data": null}}}`. Omitting it leaves
    the binding alone, which is the distinction a patch exists to draw.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    settings = await inv.get_settings(db, ws.id)
    if settings is None:
        raise HTTPException(
            status_code=404,
            detail="This workspace has no inventory settings to patch. PUT creates them.",
        )

    attrs = _attrs(body)
    try:
        settings = await inv.patch_settings(
            db,
            settings,
            include_platform=(
                _as_bool(attrs.get("include-platform"), "include-platform", True)
                if "include-platform" in attrs
                else None
            ),
            vcs_connection_id=_rel_id(body, "vcs-connection", "vcs-connections"),
            clear_vcs_connection=_rel_is_explicit_null(body, "vcs-connection"),
            repo_url=attrs.get("repo-url") if "repo-url" in attrs else None,
            branch=attrs.get("branch") if "branch" in attrs else None,
            working_directory=(
                attrs.get("working-directory") if "working-directory" in attrs else None
            ),
            ignore_paths=(
                _as_str_list(attrs.get("ignore-paths"), "ignore-paths")
                if "ignore-paths" in attrs
                else None
            ),
        )
    except InventoryValidationError as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate="These inventory settings already exist",
                parent="That VCS connection does not exist",
                check="A repository needs a VCS connection to fetch it with",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _settings_json(settings)})


@router.delete("/workspaces/{workspace_id}/inventory/settings", status_code=204)
async def delete_inventory_settings(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Clear the settings, and with them the VCS binding.

    Declared hosts and groups are untouched: they belong to the workspace, so
    unbinding a repository leaves the inventory with its declared half.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    settings = await inv.get_settings(db, ws.id)
    if settings is None:
        raise HTTPException(status_code=404, detail="This workspace has no inventory settings")
    await inv.delete_settings(db, settings)
    await db.commit()
    logger.info("Inventory settings cleared", workspace=ws.name, by=user.email)


# ── Hosts ────────────────────────────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/inventory/hosts")
async def list_inventory_hosts(
    workspace_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The workspace's hosts, with their group and variable counts.

    Unphased for a runner token: a plan reads inventory to diff it.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    hosts = await inv.list_hosts(db, ws.id)
    # Two grouped queries rather than two per host: a list view needs every
    # count, and the per-row version is the N+1 nothing in the response
    # explains.
    groups = await inv.host_group_counts(db, ws.id)
    variables = await inv.host_var_counts(db, ws.id)
    page, meta = paginate(
        [
            _host_json(h, groups=groups.get(h.id, 0), variables=variables.get(h.id, 0))
            for h in hosts
        ],
        request,
    )
    return JSONResponse(content={"data": page, "meta": meta})


@router.post("/workspaces/{workspace_id}/inventory/hosts", status_code=201)
async def create_inventory_host(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Declare a host. Bound to the apply phase for a runner token."""
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    name = _attrs(body).get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="name is required")

    try:
        host = await inv.create_host(db, ws.id, name=name)
    except (InventoryValidationError, inv.InventoryLimitExceeded) as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate=f"A host named {name!r} is already declared in this workspace",
                parent="That workspace does not exist",
            ),
        ) from exc

    await db.commit()
    logger.info("Inventory host declared", workspace=ws.name, host=name, by=user.email)
    return JSONResponse(content={"data": _host_json(host)}, status_code=201)


@router.get("/inventory-hosts/{host_id}")
async def show_inventory_host(
    host_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    host = await _host(host_id, db)
    ws = await _get_workspace(f"ws-{host.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    groups = await inv.host_group_counts(db, ws.id)
    variables = await inv.host_var_counts(db, ws.id)
    return JSONResponse(
        content={
            "data": _host_json(
                host, groups=groups.get(host.id, 0), variables=variables.get(host.id, 0)
            )
        }
    )


@router.patch("/inventory-hosts/{host_id}")
async def update_inventory_host(
    host_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Rename a host. `name` is the only mutable field it has."""
    host = await _host(host_id, db)
    ws = await _get_workspace(f"ws-{host.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    attrs = _attrs(body)
    if "name" not in attrs:
        return JSONResponse(content={"data": _host_json(host)})
    name = attrs.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="name must be a non-empty string")

    try:
        host = await inv.rename_host(db, host, name=name)
    except InventoryValidationError as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate=f"A host named {name!r} is already declared in this workspace",
                parent="That workspace does not exist",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _host_json(host)})


@router.delete("/inventory-hosts/{host_id}", status_code=204)
async def delete_inventory_host(
    host_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a host. Its memberships and variables go with it."""
    host = await _host(host_id, db)
    ws = await _get_workspace(f"ws-{host.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")
    await inv.delete_host(db, host)
    await db.commit()
    logger.info("Inventory host removed", workspace=ws.name, host=host.name, by=user.email)


# ── Groups ───────────────────────────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/inventory/groups")
async def list_inventory_groups(
    workspace_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    groups = await inv.list_groups(db, ws.id)
    members = await inv.group_member_counts(db, ws.id)
    children = await inv.group_child_counts(db, ws.id)
    variables = await inv.group_var_counts(db, ws.id)
    page, meta = paginate(
        [
            _group_json(
                g,
                members=members.get(g.id, 0),
                children=children.get(g.id, 0),
                variables=variables.get(g.id, 0),
            )
            for g in groups
        ],
        request,
    )
    return JSONResponse(content={"data": page, "meta": meta})


@router.post("/workspaces/{workspace_id}/inventory/groups", status_code=201)
async def create_inventory_group(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Declare a group.

    `all` and `ungrouped` are refused: ansible derives both, and the rendered
    document is rooted at `all:`. Variables that apply to every host go to
    `.../inventory/vars`, which is ansible's `group_vars/all`.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    name = _attrs(body).get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="name is required")

    try:
        group = await inv.create_group(db, ws.id, name=name)
    except (InventoryValidationError, inv.InventoryLimitExceeded) as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate=f"A group named {name!r} already exists in this workspace",
                parent="That workspace does not exist",
            ),
        ) from exc

    await db.commit()
    logger.info("Inventory group declared", workspace=ws.name, group=name, by=user.email)
    return JSONResponse(content={"data": _group_json(group)}, status_code=201)


@router.get("/inventory-groups/{group_id}")
async def show_inventory_group(
    group_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    members = await inv.group_member_counts(db, ws.id)
    children = await inv.group_child_counts(db, ws.id)
    variables = await inv.group_var_counts(db, ws.id)
    return JSONResponse(
        content={
            "data": _group_json(
                group,
                members=members.get(group.id, 0),
                children=children.get(group.id, 0),
                variables=variables.get(group.id, 0),
            )
        }
    )


@router.patch("/inventory-groups/{group_id}")
async def update_inventory_group(
    group_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    attrs = _attrs(body)
    if "name" not in attrs:
        return JSONResponse(content={"data": _group_json(group)})
    name = attrs.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="name must be a non-empty string")

    try:
        group = await inv.rename_group(db, group, name=name)
    except InventoryValidationError as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate=f"A group named {name!r} already exists in this workspace",
                parent="That workspace does not exist",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _group_json(group)})


@router.delete("/inventory-groups/{group_id}", status_code=204)
async def delete_inventory_group(
    group_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a group. Its memberships, nestings and variables go with it.

    The hosts do not: a host belongs to the workspace, so this ungroups them
    rather than deleting them.
    """
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")
    await inv.delete_group(db, group)
    await db.commit()
    logger.info("Inventory group removed", workspace=ws.name, group=group.name, by=user.email)


# ── Membership ───────────────────────────────────────────────────────────────


@router.get("/inventory-groups/{group_id}/hosts")
async def list_group_members(
    group_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The memberships of this group -- the links, not the hosts.

    The link is what carries the id a Terraform resource addresses, so a caller
    managing membership needs these rather than the hosts they point at.
    """
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    links = await inv.list_host_groups(db, group_id=group.id)
    page, meta = paginate([_host_group_json(link) for link in links], request)
    return JSONResponse(content={"data": page, "meta": meta})


@router.get("/inventory-hosts/{host_id}/groups")
async def list_host_memberships(
    host_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The same links from the host's side."""
    host = await _host(host_id, db)
    ws = await _get_workspace(f"ws-{host.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    links = await inv.list_host_groups(db, host_id=host.id)
    page, meta = paginate([_host_group_json(link) for link in links], request)
    return JSONResponse(content={"data": page, "meta": meta})


@router.post("/inventory-groups/{group_id}/hosts", status_code=201)
async def create_group_member(
    group_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Put a host in this group.

    The group comes from the path and the host from a relationship, so the
    nesting matches every other child resource here. A host in another
    workspace is a `422` from the composite foreign key rather than a check
    this code performs -- see the module docstring.
    """
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    host_id = _require_rel(body, "host", "inventory-hosts")
    try:
        link = await inv.create_host_group(
            db, workspace_id=ws.id, host_id=host_id, group_id=group.id
        )
    except inv.InventoryLimitExceeded as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate="That host is already in this group",
                parent="That host does not exist in this workspace",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _host_group_json(link)}, status_code=201)


@router.post("/inventory-hosts/{host_id}/groups", status_code=201)
async def create_host_membership(
    host_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Put this host in a group -- the same link, created from the host's side.

    The membership is symmetric, so both sides can create it. Which one a caller
    reaches for depends on what it is iterating: a loop over a group's intended
    members wants the group route, a loop over a host's groups wants this one,
    and forcing either to invert its loop buys nothing. The row, the constraint
    and the response are identical.
    """
    host = await _host(host_id, db)
    ws = await _get_workspace(f"ws-{host.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    group_id = _require_rel(body, "group", "inventory-groups")
    try:
        link = await inv.create_host_group(
            db, workspace_id=ws.id, host_id=host.id, group_id=group_id
        )
    except inv.InventoryLimitExceeded as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate="That host is already in this group",
                parent="That group does not exist in this workspace",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _host_group_json(link)}, status_code=201)


@router.get("/inventory-host-groups/{link_id}")
async def show_group_member(
    link_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    link_uuid = parse_id_for(link_id, "inventory-host-groups", detail="Membership not found")
    link = await inv.get_host_group(db, link_uuid)
    if link is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    ws = await _get_workspace(f"ws-{link.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    return JSONResponse(content={"data": _host_group_json(link)})


@router.delete("/inventory-host-groups/{link_id}", status_code=204)
async def delete_group_member(
    link_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Take a host out of a group. Neither the host nor the group is touched."""
    link_uuid = parse_id_for(link_id, "inventory-host-groups", detail="Membership not found")
    link = await inv.get_host_group(db, link_uuid)
    if link is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    ws = await _get_workspace(f"ws-{link.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")
    await inv.delete_host_group(db, link)
    await db.commit()


# ── Group nesting ────────────────────────────────────────────────────────────


@router.get("/inventory-groups/{group_id}/children")
async def list_group_child_links(
    group_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    links = await inv.list_group_children(db, parent_group_id=group.id)
    page, meta = paginate([_group_child_json(link) for link in links], request)
    return JSONResponse(content={"data": page, "meta": meta})


@router.get("/inventory-groups/{group_id}/parents")
async def list_group_parent_links(
    group_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The nestings this group is the CHILD of.

    A group may have several parents, which ansible allows, so this is a real
    question rather than the inverse of a single field.
    """
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    links = await inv.list_group_children(db, child_group_id=group.id)
    page, meta = paginate([_group_child_json(link) for link in links], request)
    return JSONResponse(content={"data": page, "meta": meta})


@router.post("/inventory-groups/{group_id}/children", status_code=201)
async def create_group_child_link(
    group_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Nest a group inside this one -- a `[groupname:children]` entry."""
    parent = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{parent.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    child_id = _require_rel(body, "child-group", "inventory-groups")
    try:
        link = await inv.create_group_child(
            db, workspace_id=ws.id, parent_group_id=parent.id, child_group_id=child_id
        )
    except (InventoryValidationError, inv.InventoryLimitExceeded) as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate="That group is already a child of this one",
                parent="That group does not exist in this workspace",
                check="A group cannot be nested inside itself",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _group_child_json(link)}, status_code=201)


@router.post("/inventory-groups/{group_id}/parents", status_code=201)
async def create_group_parent_link(
    group_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Nest this group inside another -- the same link, from the child's side.

    A group may have several parents, so "add a parent to this group" is as
    natural a thing to say as "add a child to that one", and the row is the
    same either way.
    """
    child = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{child.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    parent_id = _require_rel(body, "parent-group", "inventory-groups")
    try:
        link = await inv.create_group_child(
            db, workspace_id=ws.id, parent_group_id=parent_id, child_group_id=child.id
        )
    except (InventoryValidationError, inv.InventoryLimitExceeded) as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate="That group is already a child of this one",
                parent="That group does not exist in this workspace",
                check="A group cannot be nested inside itself",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _group_child_json(link)}, status_code=201)


@router.get("/inventory-group-children/{link_id}")
async def show_group_child_link(
    link_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    link_uuid = parse_id_for(link_id, "inventory-group-children", detail="Nesting not found")
    link = await inv.get_group_child(db, link_uuid)
    if link is None:
        raise HTTPException(status_code=404, detail="Nesting not found")
    ws = await _get_workspace(f"ws-{link.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    return JSONResponse(content={"data": _group_child_json(link)})


@router.delete("/inventory-group-children/{link_id}", status_code=204)
async def delete_group_child_link(
    link_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    link_uuid = parse_id_for(link_id, "inventory-group-children", detail="Nesting not found")
    link = await inv.get_group_child(db, link_uuid)
    if link is None:
        raise HTTPException(status_code=404, detail="Nesting not found")
    ws = await _get_workspace(f"ws-{link.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")
    await inv.delete_group_child(db, link)
    await db.commit()


# ── Variables ────────────────────────────────────────────────────────────────


def _var_attrs(body: dict) -> tuple[str, str, bool, bool]:
    """The four fields a variable write carries, validated."""
    attrs = _attrs(body)
    key = attrs.get("key")
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=422, detail="key is required")
    value = attrs.get("value")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail="value must be a string. A list, number or object is expressed by "
            "setting structured=true and sending its literal source in value, the same "
            "way a structured workspace variable does.",
        )
    return (
        key,
        value,
        _as_bool(attrs.get("structured"), "structured", False),
        _as_bool(attrs.get("sensitive"), "sensitive", False),
    )


@router.get("/inventory-hosts/{host_id}/vars")
async def list_inventory_host_vars(
    host_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    host = await _host(host_id, db)
    ws = await _get_workspace(f"ws-{host.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    page, meta = paginate(
        [_host_var_json(v) for v in await inv.list_host_vars(db, host.id)], request
    )
    return JSONResponse(content={"data": page, "meta": meta})


@router.post("/inventory-hosts/{host_id}/vars", status_code=201)
async def create_inventory_host_var(
    host_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """One entry in this host's `host_vars`."""
    host = await _host(host_id, db)
    ws = await _get_workspace(f"ws-{host.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    key, value, structured, sensitive = _var_attrs(body)
    try:
        var = await inv.create_host_var(
            db,
            workspace_id=ws.id,
            host_id=host.id,
            key=key,
            value=value,
            structured=structured,
            sensitive=sensitive,
        )
    except (InventoryValidationError, inv.InventoryLimitExceeded) as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate=f"This host already has a variable named {key!r}",
                parent="That host does not exist in this workspace",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _host_var_json(var)}, status_code=201)


@router.get("/inventory-groups/{group_id}/vars")
async def list_inventory_group_vars(
    group_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    page, meta = paginate(
        [_group_var_json(v) for v in await inv.list_group_vars(db, group.id)], request
    )
    return JSONResponse(content={"data": page, "meta": meta})


@router.post("/inventory-groups/{group_id}/vars", status_code=201)
async def create_inventory_group_var(
    group_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """One entry in this group's `group_vars`."""
    group = await _group(group_id, db)
    ws = await _get_workspace(f"ws-{group.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    key, value, structured, sensitive = _var_attrs(body)
    try:
        var = await inv.create_group_var(
            db,
            workspace_id=ws.id,
            group_id=group.id,
            key=key,
            value=value,
            structured=structured,
            sensitive=sensitive,
        )
    except (InventoryValidationError, inv.InventoryLimitExceeded) as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate=f"This group already has a variable named {key!r}",
                parent="That group does not exist in this workspace",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _group_var_json(var)}, status_code=201)


@router.get("/workspaces/{workspace_id}/inventory/vars")
async def list_inventory_global_vars(
    workspace_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Variables that apply to every host -- ansible's `group_vars/all`."""
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    page, meta = paginate(
        [_global_var_json(v) for v in await inv.list_global_vars(db, ws.id)], request
    )
    return JSONResponse(content={"data": page, "meta": meta})


@router.post("/workspaces/{workspace_id}/inventory/vars", status_code=201)
async def create_inventory_global_var(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """One entry in `group_vars/all`.

    On the workspace rather than on a group, because `all` is refused as a group
    name: the rendered document is rooted at `all:`, so a declared group of that
    name would collide with the document's own structure.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    key, value, structured, sensitive = _var_attrs(body)
    try:
        var = await inv.create_global_var(
            db,
            workspace_id=ws.id,
            key=key,
            value=value,
            structured=structured,
            sensitive=sensitive,
        )
    except (InventoryValidationError, inv.InventoryLimitExceeded) as exc:
        raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
    except IntegrityError as exc:
        raise await _fail(
            db,
            _integrity_error(
                exc,
                duplicate=f"This inventory already has a variable named {key!r}",
                parent="That workspace does not exist",
            ),
        ) from exc

    await db.commit()
    return JSONResponse(content={"data": _global_var_json(var)}, status_code=201)


# The three variable surfaces share their show / patch / delete, because the row
# is the same shape in each and only the lookup and the serializer differ.
# Written as a table rather than nine near-identical handlers: nine copies is
# where the fourth one quietly behaves differently.
#
# The table holds the LABEL and the SERIALIZER; the lookup is dispatched in
# `_var_row` by reading the attribute off `inv` at call time. Holding the
# function in the table would capture it at import, which binds the route to
# whatever the module had then -- fine in production and wrong in two ways that
# matter: it cannot be substituted in a test, and a later indirection in the
# service (a cache, an instrumentation wrapper) would be silently bypassed.
_VAR_KINDS: dict[str, tuple[str, Any]] = {
    "inventory-host-vars": ("Host variable", _host_var_json),
    "inventory-group-vars": ("Group variable", _group_var_json),
    "inventory-global-vars": ("Inventory variable", _global_var_json),
}


async def _var_row(kind: str, var_id: str, db: AsyncSession):
    label, serializer = _VAR_KINDS[kind]
    var_uuid = parse_id_for(var_id, kind, detail=f"{label} not found")

    # Resolved per call, through the module, so the current function is the one
    # that runs. Three explicit branches rather than a name looked up with
    # `getattr`, so the call sites are greppable.
    if kind == "inventory-host-vars":
        var = await inv.get_host_var(db, var_uuid)
    elif kind == "inventory-group-vars":
        var = await inv.get_group_var(db, var_uuid)
    else:
        var = await inv.get_global_var(db, var_uuid)

    if var is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return var, serializer


def _var_routes(kind: str) -> None:
    """Register show / patch / delete for one variable surface."""

    @router.get(f"/{kind}/{{var_id}}", name=f"show_{kind.replace('-', '_')}")
    async def _show(  # noqa: ANN202  -- the decorator defines the response
        var_id: str = Path(...),
        user: AuthenticatedUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
        _kind: str = kind,
    ) -> JSONResponse:
        var, serializer = await _var_row(_kind, var_id, db)
        ws = await _get_workspace(f"ws-{var.workspace_id}", db)
        await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
        return JSONResponse(content={"data": serializer(var)})

    @router.patch(f"/{kind}/{{var_id}}", name=f"update_{kind.replace('-', '_')}")
    async def _update(  # noqa: ANN202
        var_id: str = Path(...),
        body: dict = Body(...),
        user: AuthenticatedUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
        _kind: str = kind,
    ) -> JSONResponse:
        """Partial update. `key` is immutable -- it is half the row's identity.

        An absent attribute is left alone, which is what lets a caller set
        `sensitive` without resending a value it may have read back masked.
        """
        var, serializer = await _var_row(_kind, var_id, db)
        ws = await _get_workspace(f"ws-{var.workspace_id}", db)
        await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

        attrs = _attrs(body)
        key = attrs.get("key") if "key" in attrs else None
        if key is not None and (not isinstance(key, str) or not key):
            raise HTTPException(status_code=422, detail="key must be a non-empty string")
        value = attrs.get("value") if "value" in attrs else None
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422, detail="value must be a string")

        try:
            var = await inv.update_var(
                db,
                var,
                key=key,
                value=value,
                structured=(
                    _as_bool(attrs.get("structured"), "structured", False)
                    if "structured" in attrs
                    else None
                ),
                sensitive=(
                    _as_bool(attrs.get("sensitive"), "sensitive", False)
                    if "sensitive" in attrs
                    else None
                ),
            )
        except InventoryValidationError as exc:
            raise await _fail(db, HTTPException(status_code=422, detail=str(exc))) from exc
        except IntegrityError as exc:
            # A rename can collide, which is the one new way this write fails.
            raise await _fail(
                db,
                _integrity_error(
                    exc,
                    duplicate=f"A variable named {key!r} already exists here",
                    parent="That variable's parent no longer exists",
                ),
            ) from exc

        await db.commit()
        return JSONResponse(content={"data": serializer(var)})

    @router.delete(f"/{kind}/{{var_id}}", status_code=204, name=f"delete_{kind.replace('-', '_')}")
    async def _delete(  # noqa: ANN202
        var_id: str = Path(...),
        user: AuthenticatedUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
        _kind: str = kind,
    ) -> None:
        var, _ = await _var_row(_kind, var_id, db)
        ws = await _get_workspace(f"ws-{var.workspace_id}", db)
        await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")
        await inv.delete_var(db, var)
        await db.commit()


for _kind in _VAR_KINDS:
    _var_routes(_kind)
