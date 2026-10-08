"""Inventory persistence and resolution (#1967, #1968).

The merge itself is `inventory_resolution` -- pure, no database. This module is
the database half: the rows, the lazy `default` inventory, and turning declared
items into a source resolution.

## A read resolves live, and writes nothing

There is no snapshot table and no equality token to compare against. #1970
closed `NOT_PLANNED`, which bans dynamic inventory, so **every source is
static** -- the `platform` source is the `inventory_items` rows Terrapod already
holds, and resolving it is a query with nothing to fetch, parse or time out.
#1967 says so directly: those rows "are already rows Terrapod owns".

A read that is already live buys a reader nothing by recording a row, and it
costs something real: a bounded history that a dashboard left open could evict.
So a resolved read resolves, returns, and persists nothing.

That is what makes #1967's observability scope deliverable now -- "an operator
has to be able to see what a configure would target before running it" -- while
configure runs do not exist yet (#1971, #1972).

The target set a *configure* runs against is a different artifact with a
different owner: it belongs to the run, is written when the run is created, and
is what a partial-configure retry subtracts against (#1973). Not to this
module, and not to the inventory.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    Inventory,
    InventoryItem,
    InventorySource,
)
from terrapod.services.inventory_resolution import (
    HostEntry,
    InventoryValidationError,
    ResolvedInventory,
    SourceResolution,
    merge,
    validate_declared_vars,
    validate_group_name,
    validate_host_name,
)

logger = structlog.get_logger(__name__)

#: The name of the inventory created lazily when a workspace first declares a
#: host. Not reserved -- an operator may create others alongside it -- but it is
#: what `terrapod_inventory_item` feeds and what the resolved view reads, since
#: the resource carries no `inventory` attribute (#1968).
DEFAULT_INVENTORY_NAME = "default"

#: The host variable `InventoryItem.address` populates. An explicit
#: `ansible_host` in the item's own `vars` wins: an operator writing the raw
#: variable is deliberately reaching past the convenience field, and silently
#: overriding them would make `vars` a lie.
ADDRESS_VAR = "ansible_host"


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
    """Create an inventory with its one `platform` source at position 0.

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
            kind=InventorySource.KIND_PLATFORM,
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
    """Delete an inventory and its sources.

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
    validate_declared_vars(host_vars)


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
    """Resolve one source into the hosts it contributes.

    No refusal branch: `platform` is the only kind, and it is a query over rows
    this deployment already holds. Dynamic inventory -- the one shape that would
    have needed something the API cannot do -- was declined outright (#1970).
    """
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


__all__ = [
    "ADDRESS_VAR",
    "DEFAULT_INVENTORY_NAME",
    "InventoryValidationError",
    "count_items",
    "create_inventory",
    "create_item",
    "delete_inventory",
    "delete_item",
    "find_inventory",
    "get_inventory",
    "get_item",
    "get_or_create_default_inventory",
    "list_inventories",
    "list_items",
    "list_sources",
    "resolve",
    "resolve_source",
    "update_item",
    "validate_item_fields",
]
