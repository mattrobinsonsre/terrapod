"""Inventory persistence: the eight structures and their rows (#1967, #1968).

Name validation is `inventory_resolution` -- pure, no database. This module is
the database half, and it is deliberately thin: eight structures, each with
create / read / list / delete, and update only where a row has a mutable field.

## One inventory per workspace

There is no named inventory object. Disjoint targeting is what groups and
`--limit` are for, which is ansible's own answer, so nothing has to choose
between several and `inventory_settings` is a 1:1 row that may simply be absent.

## Why the counts are queries rather than columns

A host's group count and variable count are what a list view shows, and they are
`SELECT count()` per parent rather than denormalised columns. A column would
have to be maintained by every writer of the join and variable tables -- the
API, the provider, the UI and whatever comes next -- and one that forgot would
leave a count that disagrees with the rows while looking authoritative.

## Deletion is the database's job

Every link and variable table cascades from its parent, and every table cascades
from the workspace, so deleting a host removes its memberships and its variables
without this module enumerating them. That is also what makes a cross-workspace
link impossible rather than merely unlikely: the foreign keys are composite
`(workspace_id, <parent>_id)`, so the integrity is structural.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    InventoryGlobalVar,
    InventoryGroup,
    InventoryGroupChild,
    InventoryGroupVar,
    InventoryHost,
    InventoryHostGroup,
    InventoryHostVar,
    InventorySettings,
)
from terrapod.services.inventory_resolution import (
    InventoryValidationError,
    validate_group_name,
    validate_host_name,
    validate_var_key,
)

#: Defensive ceilings, per workspace. Generous: a few hundred hosts is an
#: ordinary fleet and `for_each` over them is the documented shape. They exist
#: so a runaway `for_each` fails with a message naming the limit instead of
#: filling a table one API call at a time.
MAX_HOSTS_PER_WORKSPACE = 5000
MAX_GROUPS_PER_WORKSPACE = 1000
#: Per host and per group respectively. A host with a thousand variables is not
#: an inventory problem, it is a different design.
MAX_VARS_PER_PARENT = 500
#: Across the workspace, for `group_vars/all`.
MAX_GLOBAL_VARS_PER_WORKSPACE = 500
#: A group may legitimately hold every host, so this is the product ceiling
#: rather than a per-group one.
MAX_MEMBERSHIPS_PER_WORKSPACE = 20000
MAX_GROUP_CHILDREN_PER_WORKSPACE = 2000


class InventoryLimitExceeded(Exception):
    """A ceiling was reached. Translated to HTTP 422, not 409.

    422 because the request is well-formed and refused on policy: nothing
    conflicts, and a 409 would send a caller looking for the row it collided
    with. The message names the limit so the caller knows what to change.
    """


# ── Settings ─────────────────────────────────────────────────────────────────


async def get_settings(db: AsyncSession, workspace_id: uuid.UUID) -> InventorySettings | None:
    """The workspace's inventory settings, or None when it has none.

    None is a real and common state -- it means "no VCS source, resolve the
    declared rows alone" -- so callers treat it as a default rather than an
    error.
    """
    result = await db.execute(
        select(InventorySettings).where(InventorySettings.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def put_settings(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    include_platform: bool = True,
    vcs_connection_id: uuid.UUID | None = None,
    repo_url: str = "",
    branch: str = "",
    working_directory: str = "",
    ignore_paths: list[str] | None = None,
) -> InventorySettings:
    """Create or replace the settings row.

    A full replace rather than a patch, because the route is a `PUT` on a
    singleton: there is one of these per workspace, so "the settings are now
    this" is the only coherent write. A caller wanting to change one field reads
    first -- which it has to do anyway to know what the others are.
    """
    if repo_url and vcs_connection_id is None:
        # The CHECK constraint says this too; refusing here names the field
        # instead of surfacing a constraint name.
        raise InventoryValidationError(
            "a repository needs a VCS connection to fetch it with: set "
            "vcs-connection-id, or clear repo-url to resolve the declared rows alone"
        )

    settings = await get_settings(db, workspace_id)
    if settings is None:
        settings = InventorySettings(workspace_id=workspace_id)
        db.add(settings)

    settings.include_platform = include_platform
    settings.vcs_connection_id = vcs_connection_id
    settings.repo_url = repo_url
    settings.branch = branch
    settings.working_directory = working_directory
    settings.ignore_paths = list(ignore_paths or [])
    await db.flush()
    return settings


async def patch_settings(
    db: AsyncSession,
    settings: InventorySettings,
    *,
    include_platform: bool | None = None,
    vcs_connection_id: uuid.UUID | None = None,
    clear_vcs_connection: bool = False,
    repo_url: str | None = None,
    branch: str | None = None,
    working_directory: str | None = None,
    ignore_paths: list[str] | None = None,
) -> InventorySettings:
    """Change some of the settings and leave the rest alone.

    Alongside `put_settings` rather than instead of it, because the two answer
    different questions and a caller should not have to read a row to change one
    field. A `PUT` says "the settings are now this"; a `PATCH` says "change this
    much". Terraform wants the first, a person or a script usually wants the
    second, and offering only one makes the other party do extra work for no
    benefit.

    `clear_vcs_connection` exists because `None` already means "not supplied"
    here, so there would otherwise be no way to express "remove the binding" --
    the same reason the provider distinguishes an omitted attribute from an
    explicit null.
    """
    if include_platform is not None:
        settings.include_platform = include_platform
    if clear_vcs_connection:
        settings.vcs_connection_id = None
    elif vcs_connection_id is not None:
        settings.vcs_connection_id = vcs_connection_id
    if repo_url is not None:
        settings.repo_url = repo_url
    if branch is not None:
        settings.branch = branch
    if working_directory is not None:
        settings.working_directory = working_directory
    if ignore_paths is not None:
        settings.ignore_paths = list(ignore_paths)

    # Re-checked against the MERGED state, not against what was supplied: a
    # patch that clears the connection while leaving a repo behind is the same
    # incoherent row as setting both at once, and only the merge can see it.
    if settings.repo_url and settings.vcs_connection_id is None:
        raise InventoryValidationError(
            "a repository needs a VCS connection to fetch it with: these settings would "
            "be left with a repo-url and no connection"
        )

    await db.flush()
    return settings


async def delete_settings(db: AsyncSession, settings: InventorySettings) -> None:
    """Remove the settings row, which clears the VCS binding.

    It does not touch a single declared row: the rows belong to the workspace,
    not to the settings, so clearing the binding leaves the inventory intact
    with only its declared half.
    """
    await db.delete(settings)
    await db.flush()


# ── Hosts and groups ─────────────────────────────────────────────────────────


async def list_hosts(db: AsyncSession, workspace_id: uuid.UUID) -> list[InventoryHost]:
    result = await db.execute(
        select(InventoryHost)
        .where(InventoryHost.workspace_id == workspace_id)
        .order_by(InventoryHost.name)
    )
    return list(result.scalars().all())


async def get_host(db: AsyncSession, host_id: uuid.UUID) -> InventoryHost | None:
    result = await db.execute(select(InventoryHost).where(InventoryHost.id == host_id))
    return result.scalar_one_or_none()


async def create_host(db: AsyncSession, workspace_id: uuid.UUID, *, name: str) -> InventoryHost:
    validate_host_name(name)
    await _check_ceiling(
        db, InventoryHost, workspace_id, MAX_HOSTS_PER_WORKSPACE, "hosts per workspace"
    )
    host = InventoryHost(workspace_id=workspace_id, name=name)
    db.add(host)
    await db.flush()
    return host


async def rename_host(db: AsyncSession, host: InventoryHost, *, name: str) -> InventoryHost:
    validate_host_name(name)
    host.name = name
    await db.flush()
    return host


async def delete_host(db: AsyncSession, host: InventoryHost) -> None:
    """Remove a host. Its memberships and variables cascade."""
    await db.delete(host)
    await db.flush()


async def list_groups(db: AsyncSession, workspace_id: uuid.UUID) -> list[InventoryGroup]:
    result = await db.execute(
        select(InventoryGroup)
        .where(InventoryGroup.workspace_id == workspace_id)
        .order_by(InventoryGroup.name)
    )
    return list(result.scalars().all())


async def get_group(db: AsyncSession, group_id: uuid.UUID) -> InventoryGroup | None:
    result = await db.execute(select(InventoryGroup).where(InventoryGroup.id == group_id))
    return result.scalar_one_or_none()


async def create_group(db: AsyncSession, workspace_id: uuid.UUID, *, name: str) -> InventoryGroup:
    validate_group_name(name)
    await _check_ceiling(
        db, InventoryGroup, workspace_id, MAX_GROUPS_PER_WORKSPACE, "groups per workspace"
    )
    group = InventoryGroup(workspace_id=workspace_id, name=name)
    db.add(group)
    await db.flush()
    return group


async def rename_group(db: AsyncSession, group: InventoryGroup, *, name: str) -> InventoryGroup:
    validate_group_name(name)
    group.name = name
    await db.flush()
    return group


async def delete_group(db: AsyncSession, group: InventoryGroup) -> None:
    """Remove a group. Its memberships, nestings and variables cascade.

    The hosts are untouched: a host belongs to the workspace, so deleting a
    group ungroups its members rather than deleting them.
    """
    await db.delete(group)
    await db.flush()


# ── Membership and nesting ───────────────────────────────────────────────────


async def list_host_groups(
    db: AsyncSession, *, host_id: uuid.UUID | None = None, group_id: uuid.UUID | None = None
) -> list[InventoryHostGroup]:
    """Memberships, filtered by whichever side the caller asked about."""
    stmt = select(InventoryHostGroup)
    if host_id is not None:
        stmt = stmt.where(InventoryHostGroup.host_id == host_id)
    if group_id is not None:
        stmt = stmt.where(InventoryHostGroup.group_id == group_id)
    result = await db.execute(stmt.order_by(InventoryHostGroup.created_at))
    return list(result.scalars().all())


async def get_host_group(db: AsyncSession, link_id: uuid.UUID) -> InventoryHostGroup | None:
    result = await db.execute(select(InventoryHostGroup).where(InventoryHostGroup.id == link_id))
    return result.scalar_one_or_none()


async def create_host_group(
    db: AsyncSession, *, workspace_id: uuid.UUID, host_id: uuid.UUID, group_id: uuid.UUID
) -> InventoryHostGroup:
    """Put a host in a group.

    No check that either exists, and none that they share the workspace: the
    composite foreign keys answer both, and a `SELECT` first would be a
    check-then-act race that two concurrent applies could pass together.
    """
    await _check_ceiling(
        db,
        InventoryHostGroup,
        workspace_id,
        MAX_MEMBERSHIPS_PER_WORKSPACE,
        "group memberships per workspace",
    )
    link = InventoryHostGroup(workspace_id=workspace_id, host_id=host_id, group_id=group_id)
    db.add(link)
    await db.flush()
    return link


async def delete_host_group(db: AsyncSession, link: InventoryHostGroup) -> None:
    await db.delete(link)
    await db.flush()


async def list_group_children(
    db: AsyncSession,
    *,
    parent_group_id: uuid.UUID | None = None,
    child_group_id: uuid.UUID | None = None,
) -> list[InventoryGroupChild]:
    stmt = select(InventoryGroupChild)
    if parent_group_id is not None:
        stmt = stmt.where(InventoryGroupChild.parent_group_id == parent_group_id)
    if child_group_id is not None:
        stmt = stmt.where(InventoryGroupChild.child_group_id == child_group_id)
    result = await db.execute(stmt.order_by(InventoryGroupChild.created_at))
    return list(result.scalars().all())


async def get_group_child(db: AsyncSession, link_id: uuid.UUID) -> InventoryGroupChild | None:
    result = await db.execute(select(InventoryGroupChild).where(InventoryGroupChild.id == link_id))
    return result.scalar_one_or_none()


async def create_group_child(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    parent_group_id: uuid.UUID,
    child_group_id: uuid.UUID,
) -> InventoryGroupChild:
    """Nest one group inside another.

    The one-step cycle is a CHECK constraint. A longer one is refused here,
    because a CHECK cannot walk a graph -- see `_would_cycle`.
    """
    if await _would_cycle(db, workspace_id, parent_group_id, child_group_id):
        raise InventoryValidationError(
            "that nesting would make a cycle: the parent group is already reachable from "
            "the child, so ansible would have a group that contains itself"
        )
    await _check_ceiling(
        db,
        InventoryGroupChild,
        workspace_id,
        MAX_GROUP_CHILDREN_PER_WORKSPACE,
        "group nestings per workspace",
    )
    link = InventoryGroupChild(
        workspace_id=workspace_id,
        parent_group_id=parent_group_id,
        child_group_id=child_group_id,
    )
    db.add(link)
    await db.flush()
    return link


async def delete_group_child(db: AsyncSession, link: InventoryGroupChild) -> None:
    await db.delete(link)
    await db.flush()


async def _would_cycle(
    db: AsyncSession, workspace_id: uuid.UUID, parent_id: uuid.UUID, child_id: uuid.UUID
) -> bool:
    """Whether making `child_id` a child of `parent_id` closes a cycle.

    Walks DOWN from the proposed child looking for the proposed parent: if the
    parent is already somewhere beneath the child, the new edge closes a loop.

    Terrapod does not resolve the group graph -- ansible does -- so this is not
    protecting any traversal of ours. It is refused because ansible's own
    behaviour on a cyclic inventory is not something an operator should have to
    discover, and the write is the only place we can say so with the two group
    names in hand.
    """
    rows = await db.execute(
        select(InventoryGroupChild.parent_group_id, InventoryGroupChild.child_group_id).where(
            InventoryGroupChild.workspace_id == workspace_id
        )
    )
    edges: dict[uuid.UUID, list[uuid.UUID]] = {}
    for parent, child in rows.all():
        edges.setdefault(parent, []).append(child)

    seen: set[uuid.UUID] = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


# ── Variables ────────────────────────────────────────────────────────────────
#
# Three near-identical surfaces, kept as three because their parents differ and
# the parent is what the composite foreign key is for. The helpers below take
# the model and the parent column so the shared logic is written once without
# pretending the tables are one.


async def list_host_vars(db: AsyncSession, host_id: uuid.UUID) -> list[InventoryHostVar]:
    result = await db.execute(
        select(InventoryHostVar)
        .where(InventoryHostVar.host_id == host_id)
        .order_by(InventoryHostVar.key)
    )
    return list(result.scalars().all())


async def get_host_var(db: AsyncSession, var_id: uuid.UUID) -> InventoryHostVar | None:
    result = await db.execute(select(InventoryHostVar).where(InventoryHostVar.id == var_id))
    return result.scalar_one_or_none()


async def create_host_var(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    host_id: uuid.UUID,
    key: str,
    value: str = "",
    structured: bool = False,
    sensitive: bool = False,
) -> InventoryHostVar:
    validate_var_key(key)
    await _check_var_ceiling(db, InventoryHostVar, InventoryHostVar.host_id, host_id, "host")
    var = InventoryHostVar(
        workspace_id=workspace_id,
        host_id=host_id,
        key=key,
        value=value,
        structured=structured,
        sensitive=sensitive,
    )
    db.add(var)
    await db.flush()
    return var


async def list_group_vars(db: AsyncSession, group_id: uuid.UUID) -> list[InventoryGroupVar]:
    result = await db.execute(
        select(InventoryGroupVar)
        .where(InventoryGroupVar.group_id == group_id)
        .order_by(InventoryGroupVar.key)
    )
    return list(result.scalars().all())


async def get_group_var(db: AsyncSession, var_id: uuid.UUID) -> InventoryGroupVar | None:
    result = await db.execute(select(InventoryGroupVar).where(InventoryGroupVar.id == var_id))
    return result.scalar_one_or_none()


async def create_group_var(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    group_id: uuid.UUID,
    key: str,
    value: str = "",
    structured: bool = False,
    sensitive: bool = False,
) -> InventoryGroupVar:
    validate_var_key(key)
    await _check_var_ceiling(db, InventoryGroupVar, InventoryGroupVar.group_id, group_id, "group")
    var = InventoryGroupVar(
        workspace_id=workspace_id,
        group_id=group_id,
        key=key,
        value=value,
        structured=structured,
        sensitive=sensitive,
    )
    db.add(var)
    await db.flush()
    return var


async def list_global_vars(db: AsyncSession, workspace_id: uuid.UUID) -> list[InventoryGlobalVar]:
    result = await db.execute(
        select(InventoryGlobalVar)
        .where(InventoryGlobalVar.workspace_id == workspace_id)
        .order_by(InventoryGlobalVar.key)
    )
    return list(result.scalars().all())


async def get_global_var(db: AsyncSession, var_id: uuid.UUID) -> InventoryGlobalVar | None:
    result = await db.execute(select(InventoryGlobalVar).where(InventoryGlobalVar.id == var_id))
    return result.scalar_one_or_none()


async def create_global_var(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    key: str,
    value: str = "",
    structured: bool = False,
    sensitive: bool = False,
) -> InventoryGlobalVar:
    """One entry in `group_vars/all`.

    Parented on the workspace rather than on a group row, because `all` is
    refused as a group name -- see `inventory_resolution.DERIVED_GROUPS`.
    """
    validate_var_key(key)
    await _check_ceiling(
        db,
        InventoryGlobalVar,
        workspace_id,
        MAX_GLOBAL_VARS_PER_WORKSPACE,
        "inventory-wide variables per workspace",
    )
    var = InventoryGlobalVar(
        workspace_id=workspace_id,
        key=key,
        value=value,
        structured=structured,
        sensitive=sensitive,
    )
    db.add(var)
    await db.flush()
    return var


async def update_var(
    db: AsyncSession,
    var: InventoryHostVar | InventoryGroupVar | InventoryGlobalVar,
    *,
    key: str | None = None,
    value: str | None = None,
    structured: bool | None = None,
    sensitive: bool | None = None,
) -> InventoryHostVar | InventoryGroupVar | InventoryGlobalVar:
    """Partial update, shared by all three variable surfaces.

    An absent argument leaves the field alone, which is what lets a caller set
    `sensitive` without having to resend a value it may have read back masked.

    **`key` is renameable.** The unique constraint on `(parent, key)` still
    protects correctness, so a collision is a 409 either way -- and refusing the
    rename would only mean a caller deletes and recreates to get the same
    result, losing the row id for no gain. The Terraform provider still declares
    `key` as `RequiresReplace`, because replacing is the right behaviour for a
    configuration that changed; that is the provider's choice about its own
    lifecycle and not a reason for the API to forbid the operation to everyone
    else.
    """
    if key is not None:
        validate_var_key(key)
        var.key = key
    if value is not None:
        var.value = value
    if structured is not None:
        var.structured = structured
    if sensitive is not None:
        var.sensitive = sensitive
    await db.flush()
    return var


async def delete_var(
    db: AsyncSession, var: InventoryHostVar | InventoryGroupVar | InventoryGlobalVar
) -> None:
    await db.delete(var)
    await db.flush()


# ── Counts, for list views and the data gate ─────────────────────────────────


async def count_hosts(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    return await _count(db, InventoryHost, InventoryHost.workspace_id, workspace_id)


async def count_groups(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    return await _count(db, InventoryGroup, InventoryGroup.workspace_id, workspace_id)


async def has_anything(db: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """Whether this workspace has an inventory at all.

    What a data-gated surface keys on (#1986): no rows and no settings means no
    tab and no nav entry, so a terraform/tofu-only deployment sees nothing. It
    is deliberately three cheap counts rather than a cached flag -- a flag would
    need maintaining by every writer, and one that drifted would hide a real
    inventory or show an empty tab.
    """
    if await count_hosts(db, workspace_id):
        return True
    if await count_groups(db, workspace_id):
        return True
    return await get_settings(db, workspace_id) is not None


async def group_member_counts(db: AsyncSession, workspace_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """group id -> member count, for every group in the workspace.

    One grouped query rather than a count per group: a list view needs all of
    them, and the per-row version is the N+1 that makes a hundred-group
    workspace slow in a way nothing in the response explains.
    """
    result = await db.execute(
        select(InventoryHostGroup.group_id, func.count())
        .where(InventoryHostGroup.workspace_id == workspace_id)
        .group_by(InventoryHostGroup.group_id)
    )
    return {gid: int(n) for gid, n in result.all()}


async def host_group_counts(db: AsyncSession, workspace_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """host id -> the number of groups it is in."""
    result = await db.execute(
        select(InventoryHostGroup.host_id, func.count())
        .where(InventoryHostGroup.workspace_id == workspace_id)
        .group_by(InventoryHostGroup.host_id)
    )
    return {hid: int(n) for hid, n in result.all()}


async def host_var_counts(db: AsyncSession, workspace_id: uuid.UUID) -> dict[uuid.UUID, int]:
    result = await db.execute(
        select(InventoryHostVar.host_id, func.count())
        .where(InventoryHostVar.workspace_id == workspace_id)
        .group_by(InventoryHostVar.host_id)
    )
    return {hid: int(n) for hid, n in result.all()}


async def group_var_counts(db: AsyncSession, workspace_id: uuid.UUID) -> dict[uuid.UUID, int]:
    result = await db.execute(
        select(InventoryGroupVar.group_id, func.count())
        .where(InventoryGroupVar.workspace_id == workspace_id)
        .group_by(InventoryGroupVar.group_id)
    )
    return {gid: int(n) for gid, n in result.all()}


async def group_child_counts(db: AsyncSession, workspace_id: uuid.UUID) -> dict[uuid.UUID, int]:
    result = await db.execute(
        select(InventoryGroupChild.parent_group_id, func.count())
        .where(InventoryGroupChild.workspace_id == workspace_id)
        .group_by(InventoryGroupChild.parent_group_id)
    )
    return {gid: int(n) for gid, n in result.all()}


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _count(db: AsyncSession, model: Any, column: Any, value: Any) -> int:
    result = await db.execute(select(func.count()).select_from(model).where(column == value))
    return int(result.scalar() or 0)


async def _check_ceiling(
    db: AsyncSession, model: Any, workspace_id: uuid.UUID, limit: int, what: str
) -> None:
    existing = await _count(db, model, model.workspace_id, workspace_id)
    if existing >= limit:
        raise InventoryLimitExceeded(f"Maximum of {limit} {what}")


async def _check_var_ceiling(
    db: AsyncSession, model: Any, column: Any, parent_id: uuid.UUID, what: str
) -> None:
    existing = await _count(db, model, column, parent_id)
    if existing >= MAX_VARS_PER_PARENT:
        raise InventoryLimitExceeded(f"Maximum of {MAX_VARS_PER_PARENT} variables per {what}")


__all__ = [
    "MAX_GLOBAL_VARS_PER_WORKSPACE",
    "MAX_GROUPS_PER_WORKSPACE",
    "MAX_GROUP_CHILDREN_PER_WORKSPACE",
    "MAX_HOSTS_PER_WORKSPACE",
    "MAX_MEMBERSHIPS_PER_WORKSPACE",
    "MAX_VARS_PER_PARENT",
    "InventoryLimitExceeded",
    "InventoryValidationError",
    "count_groups",
    "count_hosts",
    "create_global_var",
    "create_group",
    "create_group_child",
    "create_group_var",
    "create_host",
    "create_host_group",
    "create_host_var",
    "delete_group",
    "delete_group_child",
    "delete_host",
    "delete_host_group",
    "delete_settings",
    "delete_var",
    "get_global_var",
    "get_group",
    "get_group_child",
    "get_group_var",
    "get_host",
    "get_host_group",
    "get_host_var",
    "get_settings",
    "group_child_counts",
    "group_member_counts",
    "group_var_counts",
    "has_anything",
    "host_group_counts",
    "host_var_counts",
    "list_global_vars",
    "list_group_children",
    "list_group_vars",
    "list_groups",
    "list_host_groups",
    "list_host_vars",
    "list_hosts",
    "put_settings",
    "rename_group",
    "rename_host",
    "update_var",
]
