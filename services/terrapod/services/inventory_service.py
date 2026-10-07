"""Inventory persistence, resolution and snapshots (#1967, #1968).

The merge itself is `inventory_resolution` -- pure, no database. This module is
the database half: the rows, the lazy `default` inventory, turning declared
items into a source resolution, and writing the `InventoryVersion` snapshot.

## Who resolves, and why the API is allowed to

`runner/phases/ansible_env.py` records the settled position (#2010): **the API
does not install ansible**, because every fetch has to go through the
pull-through cache or an air-gapped deployment cannot work, and reaching the
cache would mean the API authenticating to its own HTTP surface with a
credential it had minted for itself. So anything needing ansible to parse is
resolved in a runner.

**The `terraform` source needs none of it.** #1967 says so directly: the
declared items "are already rows Terrapod owns: resolving that source is a
query, with nothing to fetch, parse or time out". So the constraint is
ansible-in-the-API, not resolution-in-the-API, and this module resolves sources
the API owns.

That is what makes #1967's observability scope deliverable at all right now --
"an operator has to be able to see what a configure would target before running
it" -- because configure runs do not exist yet (#1971, #1972). Nothing else in
this increment could answer it.

**And it fails closed.** `resolve` refuses outright when any source is not in
`InventorySource.API_RESOLVABLE_KINDS`, naming the source. It does not partially
resolve, because a partial resolution of an inventory is a silently shrinking
host set, which is the failure mode the snapshot exists to prevent. When git
(#1929) or UI-edited YAML (#1969) lands, the refusal is already in place and the
runner path is forced rather than remembered.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    Inventory,
    InventoryItem,
    InventorySource,
    InventoryVersion,
)
from terrapod.services.inventory_resolution import (
    HostEntry,
    InventoryValidationError,
    ResolvedInventory,
    SourceResolution,
    merge,
    validate_group_name,
    validate_host_name,
    validate_var_names,
)

logger = structlog.get_logger(__name__)

#: The name of the inventory created lazily when a workspace first declares a
#: host. Not reserved -- an operator may create others alongside it -- but it is
#: what `terrapod_inventory_item` feeds and what the resolved view reads, since
#: the resource carries no `inventory` attribute (#1968).
DEFAULT_INVENTORY_NAME = "default"

#: How many snapshots to keep per inventory. Bounded because a resolve writes
#: one every time and nothing would otherwise remove them.
#:
#: **The obligation this creates is recorded on #1973**, not only here: once a
#: configure pins the snapshot it ran against, this prune must exclude referenced
#: rows or a retry loses the target set it needs. There is no referencing table
#: yet, so today there is nothing to exclude.
MAX_VERSIONS_PER_INVENTORY = 20

#: The host variable `InventoryItem.address` populates. An explicit
#: `ansible_host` in the item's own `vars` wins: an operator writing the raw
#: variable is deliberately reaching past the convenience field, and silently
#: overriding them would make `vars` a lie.
ADDRESS_VAR = "ansible_host"


class InventoryResolutionUnavailable(RuntimeError):
    """A source in this inventory cannot be resolved by the API.

    Raised rather than resolving what it can. See the module docstring: a
    partial resolution is a silently shrinking host set.
    """


# ── Inventories ──────────────────────────────────────────────────────────────


async def list_inventories(db: AsyncSession, workspace_id: uuid.UUID) -> list[Inventory]:
    result = await db.execute(
        select(Inventory).where(Inventory.workspace_id == workspace_id).order_by(Inventory.name)
    )
    return list(result.scalars().all())


async def get_inventory(db: AsyncSession, inventory_id: uuid.UUID) -> Inventory | None:
    return await db.get(Inventory, inventory_id)


async def find_inventory(db: AsyncSession, workspace_id: uuid.UUID, name: str) -> Inventory | None:
    result = await db.execute(
        select(Inventory).where(
            Inventory.workspace_id == workspace_id,
            Inventory.name == name,
        )
    )
    return result.scalar_one_or_none()


async def create_inventory(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    name: str,
    description: str = "",
) -> Inventory:
    """Create an inventory with its one `terraform` source at position 0.

    Every inventory starts with the declared-items source because that is the
    one source Terrapod owns and the one that needs no configuration. Others are
    appended after it, which is also their `-i` order.
    """
    inventory = Inventory(workspace_id=workspace_id, name=name, description=description)
    db.add(inventory)
    await db.flush()

    db.add(
        InventorySource(
            inventory_id=inventory.id,
            position=0,
            kind=InventorySource.KIND_TERRAFORM,
            config={},
        )
    )
    await db.flush()
    return inventory


async def get_or_create_default_inventory(db: AsyncSession, workspace_id: uuid.UUID) -> Inventory:
    """The workspace's `default` inventory, created on first use.

    Lazy on purpose: a workspace that never declares a host carries no rows at
    all, which is how "terraform/tofu users pay nothing" is delivered by data
    rather than by a flag (#1986).
    """
    existing = await find_inventory(db, workspace_id, DEFAULT_INVENTORY_NAME)
    if existing is not None:
        return existing
    return await create_inventory(db, workspace_id, name=DEFAULT_INVENTORY_NAME)


async def delete_inventory(db: AsyncSession, inventory: Inventory) -> None:
    """Delete an inventory, its sources and its snapshots.

    Declared items are **not** deleted: they belong to the workspace, not to any
    one inventory, and they are owned by the Terraform that declares them. An
    inventory is a view over them.
    """
    await db.delete(inventory)
    await db.flush()


async def list_sources(db: AsyncSession, inventory_id: uuid.UUID) -> list[InventorySource]:
    result = await db.execute(
        select(InventorySource)
        .where(InventorySource.inventory_id == inventory_id)
        .order_by(InventorySource.position)
    )
    return list(result.scalars().all())


# ── Declared items (#1968) ───────────────────────────────────────────────────


async def list_items(db: AsyncSession, workspace_id: uuid.UUID) -> list[InventoryItem]:
    result = await db.execute(
        select(InventoryItem)
        .where(InventoryItem.workspace_id == workspace_id)
        .order_by(InventoryItem.name)
    )
    return list(result.scalars().all())


async def get_item(db: AsyncSession, item_id: uuid.UUID) -> InventoryItem | None:
    return await db.get(InventoryItem, item_id)


def validate_item_fields(*, name: str, groups: list[str], host_vars: dict[str, Any]) -> None:
    """Validate what ansible will actually accept, or raise.

    Called before any write. The rules are in `inventory_resolution`, which owns
    them because they are properties of an ansible inventory rather than of this
    table -- `--limit`'s operators, and the groups ansible derives.
    """
    validate_host_name(name)
    for group in groups:
        validate_group_name(group)
    validate_var_names(host_vars)


async def create_item(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    name: str,
    address: str = "",
    groups: list[str] | None = None,
    host_vars: dict[str, Any] | None = None,
) -> InventoryItem:
    groups = list(groups or [])
    host_vars = dict(host_vars or {})
    validate_item_fields(name=name, groups=groups, host_vars=host_vars)

    item = InventoryItem(
        workspace_id=workspace_id,
        name=name,
        address=address or "",
        groups=groups,
        vars=host_vars,
    )
    db.add(item)
    await db.flush()

    # The inventory a configure will read has to exist for the item to be
    # visible in the resolved view, and the resource carries no `inventory`
    # attribute to name one.
    await get_or_create_default_inventory(db, workspace_id)
    return item


async def update_item(
    db: AsyncSession,
    item: InventoryItem,
    *,
    name: str | None = None,
    address: str | None = None,
    groups: list[str] | None = None,
    host_vars: dict[str, Any] | None = None,
) -> InventoryItem:
    """Partial update: only what the caller supplied is changed.

    `None` means "leave alone" and is why these are not plain defaults -- an
    empty list and an absent list are different requests, the first clearing the
    groups and the second not mentioning them.
    """
    validate_item_fields(
        name=name if name is not None else item.name,
        groups=groups if groups is not None else list(item.groups),
        host_vars=host_vars if host_vars is not None else dict(item.vars),
    )

    if name is not None:
        item.name = name
    if address is not None:
        item.address = address
    if groups is not None:
        item.groups = list(groups)
    if host_vars is not None:
        item.vars = dict(host_vars)

    await db.flush()
    return item


async def delete_item(db: AsyncSession, item: InventoryItem) -> None:
    await db.delete(item)
    await db.flush()


# ── Resolution ───────────────────────────────────────────────────────────────


def _host_entry(item: InventoryItem) -> HostEntry:
    """One declared item as a resolution entry.

    `address` is folded into `ansible_host` here rather than at write time, so
    the stored row keeps saying what the operator declared and the derived
    variable cannot drift from it.
    """
    host_vars = dict(item.vars or {})
    if item.address and ADDRESS_VAR not in host_vars:
        host_vars[ADDRESS_VAR] = item.address
    return HostEntry(name=item.name, vars=host_vars, groups=tuple(item.groups or ()))


async def resolve_source(
    db: AsyncSession, source: InventorySource, *, workspace_id: uuid.UUID
) -> SourceResolution:
    """Resolve one source, or raise if the API cannot.

    Only `terraform` is API-resolvable today; see the module docstring.
    """
    if source.kind not in InventorySource.API_RESOLVABLE_KINDS:
        raise InventoryResolutionUnavailable(
            f"the {source.kind!r} source at position {source.position} needs ansible to "
            f"parse, and ansible is installed only in the runner (#2010). This inventory "
            f"can only be resolved by a configure or a resolve operation; the API will not "
            f"resolve the rest of it, because a partial resolution is a target set that is "
            f"silently too small."
        )

    items = await list_items(db, workspace_id)
    return SourceResolution(
        label=f"{source.kind}:{source.position}",
        hosts=tuple(_host_entry(item) for item in items),
    )


async def resolve(db: AsyncSession, inventory: Inventory) -> ResolvedInventory:
    """Resolve every source in `-i` order into one host/group set."""
    sources = await list_sources(db, inventory.id)
    resolutions = [
        await resolve_source(db, source, workspace_id=inventory.workspace_id) for source in sources
    ]
    return merge(resolutions)


def api_can_resolve(sources: list[InventorySource]) -> bool:
    """Whether every source is one the API owns."""
    return all(s.kind in InventorySource.API_RESOLVABLE_KINDS for s in sources)


# ── Snapshots ────────────────────────────────────────────────────────────────


async def record_snapshot(
    db: AsyncSession,
    inventory: Inventory,
    resolved: ResolvedInventory,
    *,
    produced_by: str,
    produced_by_ref: str = "",
) -> InventoryVersion:
    """Write an `InventoryVersion` and prune the history behind it."""
    version = InventoryVersion(
        inventory_id=inventory.id,
        hosts=resolved.hosts,
        groups=resolved.groups,
        host_count=resolved.host_count,
        group_count=resolved.group_count,
        produced_by=produced_by,
        produced_by_ref=produced_by_ref,
    )
    db.add(version)
    await db.flush()

    await _prune_versions(db, inventory.id)

    logger.info(
        "inventory snapshot recorded",
        inventory_id=str(inventory.id),
        hosts=resolved.host_count,
        groups=resolved.group_count,
        produced_by=produced_by,
    )
    return version


async def _prune_versions(db: AsyncSession, inventory_id: uuid.UUID) -> int:
    """Keep the newest MAX_VERSIONS_PER_INVENTORY snapshots; return how many went.

    See the constant for the obligation this creates once a configure pins the
    snapshot it ran against (#1973).
    """
    keep = await db.execute(
        select(InventoryVersion.id)
        .where(InventoryVersion.inventory_id == inventory_id)
        .order_by(InventoryVersion.created_at.desc(), InventoryVersion.id.desc())
        .limit(MAX_VERSIONS_PER_INVENTORY)
    )
    keep_ids = [row[0] for row in keep.all()]
    if len(keep_ids) < MAX_VERSIONS_PER_INVENTORY:
        return 0

    result = await db.execute(
        delete(InventoryVersion).where(
            InventoryVersion.inventory_id == inventory_id,
            InventoryVersion.id.not_in(keep_ids),
        )
    )
    await db.flush()
    return int(result.rowcount or 0)


async def latest_version(db: AsyncSession, inventory_id: uuid.UUID) -> InventoryVersion | None:
    """The newest snapshot, or None if this inventory has never been resolved."""
    result = await db.execute(
        select(InventoryVersion)
        .where(InventoryVersion.inventory_id == inventory_id)
        .order_by(InventoryVersion.created_at.desc(), InventoryVersion.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_versions(
    db: AsyncSession, inventory_id: uuid.UUID, *, limit: int = MAX_VERSIONS_PER_INVENTORY
) -> list[InventoryVersion]:
    result = await db.execute(
        select(InventoryVersion)
        .where(InventoryVersion.inventory_id == inventory_id)
        .order_by(InventoryVersion.created_at.desc(), InventoryVersion.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_items(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    """How many hosts this workspace declares.

    Used by read surfaces to decide whether there is anything to show at all --
    the data a tab keys on rather than a flag (#1986).
    """
    result = await db.execute(
        select(func.count())
        .select_from(InventoryItem)
        .where(InventoryItem.workspace_id == workspace_id)
    )
    return int(result.scalar() or 0)


async def resolve_and_snapshot(
    db: AsyncSession, inventory: Inventory
) -> tuple[ResolvedInventory, InventoryVersion]:
    """Resolve now and record the result. Raises if the API cannot resolve.

    The whole of the API-side refresh: what `POST .../actions/resolve` does and
    what a read of the resolved view does when there is no snapshot yet.
    """
    resolved = await resolve(db, inventory)
    version = await record_snapshot(
        db, inventory, resolved, produced_by=InventoryVersion.SOURCE_API
    )
    return resolved, version


__all__ = [
    "ADDRESS_VAR",
    "DEFAULT_INVENTORY_NAME",
    "MAX_VERSIONS_PER_INVENTORY",
    "InventoryResolutionUnavailable",
    "InventoryValidationError",
    "api_can_resolve",
    "count_items",
    "create_inventory",
    "create_item",
    "delete_inventory",
    "delete_item",
    "find_inventory",
    "get_inventory",
    "get_item",
    "get_or_create_default_inventory",
    "latest_version",
    "list_inventories",
    "list_items",
    "list_sources",
    "list_versions",
    "record_snapshot",
    "resolve",
    "resolve_and_snapshot",
    "resolve_source",
    "update_item",
    "validate_item_fields",
]
