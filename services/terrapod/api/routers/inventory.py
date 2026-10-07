"""Ansible inventory: declared hosts, the inventory object, and its snapshots.

#1967 (the inventory, its ordered sources, the `InventoryVersion` snapshot) and
#1968 (`terrapod_inventory_item`, declared by the workspace's own Terraform).

Native surface only. None of this is on the TFE-compatible prefix: no
`terraform`, `tofu` or `tfci` invocation consumes it, so by the rule in
`docs/tfe-cli-surface.md` it belongs under the Terrapod-native prefix.

## Two endpoints that do not pretend to be each other (#1967 decision 5)

* `…/inventory-items` — the **declared rows**. API-owned, instant, and makes no
  claim about resolution.
* `…/inventories/{id}/resolved` — what the inventory **is**: the merged host and
  group set.

Keeping them distinct is what stops a second implementation of "what does
ansible think this is" growing for the fast path.

## Authorization, and the one implicit grant

Two kinds of caller:

* **A person or an API token** needs `inventory:read` to read and
  `inventory:write` to change. Both are new capabilities in the workspace axis,
  granted by the existing `read` and `write` presets — so no role became more or
  less powerful, and `write` is deliberate rather than `admin`: the Terraform
  that declares hosts runs under an apply, and an API stricter than the path
  every item actually arrives by would be incoherent.

* **A runner token** may manage the inventory items of **its own run's
  workspace** and nothing else. That is the single new implicit grant #1968 asks
  for, and it is the same shape as the implicit registry read runner tokens
  already carry (`capability_resolver`) for the same reason: `terraform apply`
  cannot work without it. It is enforced here rather than as a capability floor
  because the grant is scoped to one workspace — the one its run belongs to —
  and the capability resolver has no way to know which that is.

  **Writes are bound to the apply phase**; reads are unphased. A plan has to
  read inventory to diff it and never writes, so this follows the phase claim
  (GHSA-xmrf-hxq9-m59m) without needing a new concept.
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
    Inventory,
    InventoryItem,
    InventorySource,
    InventoryVersion,
    Run,
    Workspace,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import inventory_service as inv
from terrapod.services.inventory_resolution import (
    InventoryValidationError,
    ResolvedInventory,
    limit_matches,
    to_ansible_inventory,
)
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

router = APIRouter(tags=["inventory"])
logger = get_logger(__name__)

#: Postgres SQLSTATEs, read off the driver exception rather than matched in the
#: message text -- a constraint name is not a contract and differs between
#: backends, while these are in the SQL standard.
_UNIQUE_VIOLATION = "23505"

#: A defensive ceiling on declared hosts per workspace. Generous: a few hundred
#: hosts is an ordinary fleet and `for_each` over them is the documented shape.
#: It exists so a runaway `for_each` fails with a message instead of filling a
#: table one API call at a time.
MAX_ITEMS_PER_WORKSPACE = 5000


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Serializers ──────────────────────────────────────────────────────────────


def _item_json(item: InventoryItem) -> dict:
    item_id = f"invitem-{item.id}"
    return {
        "id": item_id,
        "type": "inventory-items",
        "attributes": {
            "name": item.name,
            "address": item.address or "",
            "groups": list(item.groups or []),
            "vars": dict(item.vars or {}),
            "created-at": _rfc3339(item.created_at),
            "updated-at": _rfc3339(item.updated_at),
        },
        "relationships": {
            "workspace": {
                "data": {"id": f"ws-{item.workspace_id}", "type": "workspaces"},
            },
        },
        "links": {"self": f"/api/v1/inventory-items/{item_id}"},
    }


def _source_json(source: InventorySource) -> dict:
    """One source as a plain object, for `attributes.sources` on its inventory.

    Deliberately **not** a nested JSON:API resource object, and not a
    `relationships` entry either. A source has no route of its own -- it is not
    addressable, fetchable or mutable on its own -- so presenting it as a
    resource would promise a `/inventory-sources/{id}` that does not exist, and
    a relationship with no `included` document behind it would be a link to
    nowhere. It is part of the inventory's composition, like an ordered list of
    strings that happen to have fields, so it lives in the inventory's
    attributes. The typed id is still carried: it is what a future route would
    address, and what a UI keys a list on.
    """
    return {
        "id": f"invsrc-{source.id}",
        "position": source.position,
        "kind": source.kind,
        "config": dict(source.config or {}),
        # Whether the API can resolve this source by itself, or whether it
        # needs ansible and therefore a runner. Surfaced per source, not just
        # as the inventory's rolled-up `api-resolvable`, so a UI can say WHICH
        # source is why a snapshot is as old as it is -- the same information
        # the resolve refusal names, rather than leaving a reader to infer it.
        "api-resolvable": source.kind in InventorySource.API_RESOLVABLE_KINDS,
        "created-at": _rfc3339(source.created_at),
    }


def _inventory_json(inventory: Inventory, sources: list[InventorySource]) -> dict:
    inv_id = f"inv-{inventory.id}"
    return {
        "id": inv_id,
        "type": "inventories",
        "attributes": {
            "name": inventory.name,
            "description": inventory.description or "",
            "api-resolvable": inv.api_can_resolve(sources),
            # The composition, in `-i` order: a lower position resolves first,
            # so a higher one wins a conflicting host variable.
            "sources": [_source_json(s) for s in sources],
            "created-at": _rfc3339(inventory.created_at),
            "updated-at": _rfc3339(inventory.updated_at),
        },
        "relationships": {
            "workspace": {
                "data": {"id": f"ws-{inventory.workspace_id}", "type": "workspaces"},
            },
        },
        "links": {"self": f"/api/v1/inventories/{inv_id}"},
    }


def _version_json(version: InventoryVersion, *, include_contents: bool) -> dict:
    ver_id = f"invver-{version.id}"
    attrs: dict[str, Any] = {
        "host-count": version.host_count,
        "group-count": version.group_count,
        "produced-by": version.produced_by,
        "produced-by-ref": version.produced_by_ref or "",
        # The freshness an operator has to be able to see: a preview is as fresh
        # as the last resolve, and saying when that was is the honest surface
        # for it rather than implying it is live.
        "taken-at": _rfc3339(version.created_at),
    }
    if include_contents:
        attrs["hosts"] = dict(version.hosts or {})
        attrs["groups"] = dict(version.groups or {})
        # The shape a configure hands to ansible, rendered from the normalised
        # one rather than stored twice.
        attrs["ansible-inventory"] = to_ansible_inventory(
            ResolvedInventory(hosts=dict(version.hosts or {}), groups=dict(version.groups or {}))
        )
    return {
        "id": ver_id,
        "type": "inventory-versions",
        "attributes": attrs,
        "relationships": {
            "inventory": {
                "data": {"id": f"inv-{version.inventory_id}", "type": "inventories"},
            },
        },
        "links": {"self": f"/api/v1/inventory-versions/{ver_id}"},
    }


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


async def _get_item(item_id: str, db: AsyncSession) -> InventoryItem:
    item_uuid = parse_id_for(item_id, "inventory-items", detail="Inventory item not found")
    item = await inv.get_item(db, item_uuid)
    if item is None:
        raise HTTPException(status_code=404, detail="Inventory item not found")
    return item


async def _get_inventory(inventory_id: str, db: AsyncSession) -> Inventory:
    inv_uuid = parse_id_for(inventory_id, "inventories", detail="Inventory not found")
    inventory = await inv.get_inventory(db, inv_uuid)
    if inventory is None:
        raise HTTPException(status_code=404, detail="Inventory not found")
    return inventory


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


def _as_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise HTTPException(status_code=422, detail=f"{field} must be a list of strings")
    return list(value)


def _as_mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail=f"{field} must be an object")
    return dict(value)


# ── Declared items (#1968) ───────────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/inventory-items")
async def list_inventory_items(
    workspace_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The hosts this workspace declares.

    Unphased for a runner token: a plan reads inventory to diff it.
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    items = await inv.list_items(db, ws.id)
    page_items, meta = paginate([_item_json(i) for i in items], request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/workspaces/{workspace_id}/inventory-items", status_code=201)
async def create_inventory_item(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Declare a host. Bound to the apply phase for a runner token."""
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    attrs = _attrs(body)
    name = attrs.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="name is required")

    existing = await inv.count_items(db, ws.id)
    if existing >= MAX_ITEMS_PER_WORKSPACE:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum of {MAX_ITEMS_PER_WORKSPACE} inventory items per workspace",
        )

    try:
        item = await inv.create_item(
            db,
            ws.id,
            name=name,
            address=attrs.get("address") or "",
            groups=_as_str_list(attrs.get("groups"), "groups"),
            host_vars=_as_mapping(attrs.get("vars"), "vars"),
        )
    except InventoryValidationError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise _item_integrity_error(exc, name) from exc

    await db.commit()
    logger.info("Inventory item declared", workspace=ws.name, host=name, by=user.email)
    return JSONResponse(content={"data": _item_json(item)}, status_code=201)


def _item_integrity_error(exc: IntegrityError, name: str) -> HTTPException:
    """Translate an item write's `IntegrityError` into the status it deserves.

    A duplicate host name is the caller's input, not a server fault. Two applies
    racing, or simply a name already declared, both answered 500 before this
    existed. The constraint name is not echoed back: it is an internal detail
    and the caller does not need it to fix either case.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == _UNIQUE_VIOLATION:
        return HTTPException(
            status_code=409, detail=f"A host named {name!r} is already declared in this workspace"
        )
    return HTTPException(status_code=500, detail="Could not write the inventory item")


@router.get("/inventory-items/{item_id}")
async def show_inventory_item(
    item_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    item = await _get_item(item_id, db)
    ws = await _get_workspace(f"ws-{item.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    return JSONResponse(content={"data": _item_json(item)})


@router.patch("/inventory-items/{item_id}")
async def update_inventory_item(
    item_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Partial update. An absent attribute is left alone; an empty list clears.

    The distinction matters to the provider: omitting `groups` and sending
    `groups: []` are different requests, and collapsing them would make a
    cleared list impossible to express.
    """
    item = await _get_item(item_id, db)
    ws = await _get_workspace(f"ws-{item.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    attrs = _attrs(body)
    name = attrs.get("name")
    if "name" in attrs and (not isinstance(name, str) or not name):
        raise HTTPException(status_code=422, detail="name must be a non-empty string")

    try:
        item = await inv.update_item(
            db,
            item,
            name=name if "name" in attrs else None,
            address=attrs.get("address") if "address" in attrs else None,
            groups=_as_str_list(attrs.get("groups"), "groups") if "groups" in attrs else None,
            host_vars=_as_mapping(attrs.get("vars"), "vars") if "vars" in attrs else None,
        )
    except InventoryValidationError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise _item_integrity_error(exc, str(name or item.name)) from exc

    await db.commit()
    return JSONResponse(content={"data": _item_json(item)})


@router.delete("/inventory-items/{item_id}", status_code=204)
async def delete_inventory_item(
    item_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a declared host.

    This is what `terraform destroy` does to an inventory item, one resource at
    a time, because each host is its own resource in state. The consequence is
    worth knowing rather than discovering: destroying a workspace's inventory
    empties the target set of every configure definition reading it.
    """
    item = await _get_item(item_id, db)
    ws = await _get_workspace(f"ws-{item.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    host = item.name
    await inv.delete_item(db, item)
    await db.commit()
    logger.info("Inventory item removed", workspace=ws.name, host=host, by=user.email)


# ── The inventory object (#1967) ─────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/inventories")
async def list_inventories(
    workspace_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """This workspace's inventories.

    Empty for a workspace that has never declared a host: nothing is created
    until something uses it, which is how a terraform/tofu-only deployment pays
    nothing for this (#1986).
    """
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    inventories = await inv.list_inventories(db, ws.id)
    payload = [_inventory_json(i, await inv.list_sources(db, i.id)) for i in inventories]
    page_items, meta = paginate(payload, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/workspaces/{workspace_id}/inventories", status_code=201)
async def create_inventory(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    ws = await _get_workspace(workspace_id, db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    attrs = _attrs(body)
    name = attrs.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="name is required")

    try:
        inventory = await inv.create_inventory(
            db, ws.id, name=name, description=attrs.get("description") or ""
        )
    except IntegrityError as exc:
        await db.rollback()
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == _UNIQUE_VIOLATION:
            raise HTTPException(
                status_code=409,
                detail=f"An inventory named {name!r} already exists in this workspace",
            ) from exc
        raise HTTPException(status_code=500, detail="Could not create the inventory") from exc

    sources = await inv.list_sources(db, inventory.id)
    await db.commit()
    return JSONResponse(content={"data": _inventory_json(inventory, sources)}, status_code=201)


@router.get("/inventories/{inventory_id}")
async def show_inventory(
    inventory_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    inventory = await _get_inventory(inventory_id, db)
    ws = await _get_workspace(f"ws-{inventory.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)
    sources = await inv.list_sources(db, inventory.id)
    return JSONResponse(content={"data": _inventory_json(inventory, sources)})


@router.delete("/inventories/{inventory_id}", status_code=204)
async def delete_inventory(
    inventory_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an inventory and its snapshots. Declared items are not touched.

    An item belongs to the workspace, not to any one inventory, and is owned by
    the Terraform that declares it. An inventory is a view over them, so
    removing the view cannot remove the hosts.
    """
    inventory = await _get_inventory(inventory_id, db)
    ws = await _get_workspace(f"ws-{inventory.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    await inv.delete_inventory(db, inventory)
    await db.commit()


# ── Resolution and snapshots ─────────────────────────────────────────────────


@router.get("/inventories/{inventory_id}/resolved")
async def show_resolved_inventory(
    inventory_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """What this inventory resolves to: hosts, groups, and when it was taken.

    Serves the newest snapshot. When there is none and every source is one the
    API owns, it resolves and records one first, so a workspace that has just
    declared its hosts can see them without waiting for a configure to exist
    (#1971, #1972) -- which is what #1967's observability scope requires.

    When a source needs ansible, it answers **409** rather than resolving what
    it can: a partial resolution is a target set that is silently too small.
    """
    inventory = await _get_inventory(inventory_id, db)
    ws = await _get_workspace(f"ws-{inventory.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    version = await inv.latest_version(db, inventory.id)
    if version is None:
        sources = await inv.list_sources(db, inventory.id)
        if not inv.api_can_resolve(sources):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This inventory has never been resolved and contains a source the API "
                    "cannot resolve. A configure or a resolve operation in a runner has to "
                    "produce the first snapshot."
                ),
            )
        _, version = await inv.resolve_and_snapshot(db, inventory)
        await db.commit()

    return JSONResponse(content={"data": _version_json(version, include_contents=True)})


@router.post("/inventories/{inventory_id}/actions/resolve")
async def resolve_inventory(
    inventory_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Refresh the snapshot now, for an inventory the API can resolve.

    The "way to refresh it" the freshness surface needs. Requires write, because
    it replaces what a reader is shown.
    """
    inventory = await _get_inventory(inventory_id, db)
    ws = await _get_workspace(f"ws-{inventory.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db, phase="apply")

    sources = await inv.list_sources(db, inventory.id)
    if not inv.api_can_resolve(sources):
        offending = sorted(
            {s.kind for s in sources if s.kind not in InventorySource.API_RESOLVABLE_KINDS}
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Sources {offending} need ansible to parse, and ansible is installed only "
                f"in the runner. A configure or a resolve operation has to refresh this "
                f"inventory; the API will not resolve the rest of it, because a partial "
                f"resolution is a target set that is silently too small."
            ),
        )

    _, version = await inv.resolve_and_snapshot(db, inventory)
    await db.commit()
    return JSONResponse(content={"data": _version_json(version, include_contents=True)})


@router.get("/inventories/{inventory_id}/versions")
async def list_inventory_versions(
    inventory_id: str = Path(...),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Snapshot history, newest first. Contents omitted -- read one to get them."""
    inventory = await _get_inventory(inventory_id, db)
    ws = await _get_workspace(f"ws-{inventory.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    versions = await inv.list_versions(db, inventory.id)
    payload = [_version_json(v, include_contents=False) for v in versions]
    page_items, meta = paginate(payload, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/inventories/{inventory_id}/versions", status_code=201)
async def record_inventory_version(
    inventory_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """A runner posts the snapshot it resolved.

    The write path for everything the API cannot resolve itself. Built now so
    the configure phase (#1972) has somewhere to post on its first day rather
    than needing an endpoint added alongside it.

    Runner-token only: this is the runner protocol, and a snapshot is the
    targeting basis a retry depends on (#1973), so it is not something a person
    hand-posts.
    """
    inventory = await _get_inventory(inventory_id, db)
    ws = await _get_workspace(f"ws-{inventory.workspace_id}", db)

    if user.auth_method != "runner_token":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Runner token required: a snapshot records what a resolve actually found, "
                "so it is posted by the thing that ran it. Use "
                "POST /inventories/{id}/actions/resolve to refresh an inventory the API "
                "can resolve."
            ),
        )
    await _authorize(ws, required=cap.INVENTORY_WRITE, user=user, db=db)

    attrs = _attrs(body)
    hosts = _as_mapping(attrs.get("hosts"), "hosts")
    groups_raw = _as_mapping(attrs.get("groups"), "groups")

    groups: dict[str, list[str]] = {}
    for group, members in groups_raw.items():
        groups[group] = _as_str_list(members, f"groups[{group}]")

    for host_vars in hosts.values():
        if not isinstance(host_vars, dict):
            raise HTTPException(
                status_code=422, detail="each entry in hosts must map a host name to an object"
            )

    resolved = ResolvedInventory(hosts=hosts, groups=groups)
    version = await inv.record_snapshot(
        db,
        inventory,
        resolved,
        produced_by=InventoryVersion.SOURCE_RUNNER,
        produced_by_ref=strip_id_prefix(user.run_id or "", "run-"),
    )
    await db.commit()
    return JSONResponse(
        content={"data": _version_json(version, include_contents=False)}, status_code=201
    )


@router.post("/inventories/{inventory_id}/actions/preview-limit")
async def preview_limit(
    inventory_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Which hosts a `--limit` pattern would select, against the last snapshot.

    #1967's scope says the resolution has to be observable, and that matters
    more than usual here: auto-configure is deliberately broad, so **visibility
    is the control rather than prevention**. An operator has to be able to ask
    "what would this target" before anything runs.

    Read-only and read-gated. It expands the forms an operator writes -- names,
    groups, `all`/`*`, comma or colon separated terms, `!` and `&` -- and
    **refuses a `~regex` term** rather than quietly matching nothing, because an
    empty target set for a pattern ansible would have expanded is the wrong
    answer dressed as an answer. The authoritative expansion is always
    `ansible-inventory --list --limit` taken in the runner.
    """
    inventory = await _get_inventory(inventory_id, db)
    ws = await _get_workspace(f"ws-{inventory.workspace_id}", db)
    await _authorize(ws, required=cap.INVENTORY_READ, user=user, db=db)

    version = await inv.latest_version(db, inventory.id)
    if version is None:
        raise HTTPException(
            status_code=409,
            detail="This inventory has no snapshot yet, so there is nothing to limit against",
        )

    pattern = _attrs(body).get("limit") or ""
    if not isinstance(pattern, str):
        raise HTTPException(status_code=422, detail="limit must be a string")

    resolved = ResolvedInventory(hosts=dict(version.hosts or {}), groups=dict(version.groups or {}))
    try:
        matched = limit_matches(resolved, pattern)
    except InventoryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return JSONResponse(
        content={
            "data": {
                "id": f"invver-{version.id}",
                "type": "inventory-limit-previews",
                "attributes": {
                    "limit": pattern,
                    "hosts": matched,
                    "host-count": len(matched),
                    "of-host-count": version.host_count,
                    "taken-at": _rfc3339(version.created_at),
                },
            }
        }
    )
