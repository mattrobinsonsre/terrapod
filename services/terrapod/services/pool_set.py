"""The workspace agent-pool set (#1085 / #960 phase 0).

A workspace routes its runs to a **set** of agent pools rather than a single
one. The set is **flat**: every pool in it is equally eligible to claim a run.
There is no primary, no rank, no preference and no rotation — a queued run is
offered to all of them at once and whichever pool has a listener that claims it
first runs it.

The set is stored across two columns, and that split is a *backward-compatibility
device rather than a hierarchy*:

* ``agent_pool_id`` predates multi-pool and is still read by clients that have
  not been upgraded (the Terraform provider, go-terrapod, MCP, ``tfci``), so it
  keeps holding element 0 and is what the legacy ``agent-pool-id`` attribute
  resolves to.
* ``agent_pool_extra_ids`` holds the remainder.

Keeping the pre-existing column authoritative for element 0 is what makes a
rolling upgrade safe: an API replica still running the previous release writes
only ``agent_pool_id``, and that write stays meaningful. Had the whole set moved
into one new column, the old replica's write would be silently ignored and runs
would dispatch to pools the operator had just removed.

Every caller resolves the set through this module rather than reading either
column directly, so the storage split never leaks out as a preference order.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol


class _HasPoolSet(Protocol):
    """Structural type for the two row shapes that carry a pool set."""

    agent_pool_id: uuid.UUID | None
    agent_pool_extra_ids: list[Any]


def _coerce(value: Any) -> uuid.UUID | None:
    """Best-effort UUID coercion, tolerating the ``apool-`` id prefix."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).removeprefix("apool-"))
    except (ValueError, AttributeError):
        return None


def normalise(pool_ids: list[Any] | None) -> list[uuid.UUID]:
    """Coerce a raw list to UUIDs, dropping blanks and de-duplicating.

    Order is preserved because it is what the operator typed and what the UI
    displays back — it carries no dispatch meaning.
    """
    seen: list[uuid.UUID] = []
    for raw in pool_ids or []:
        parsed = _coerce(raw)
        if parsed is not None and parsed not in seen:
            seen.append(parsed)
    return seen


def workspace_pool_ids(workspace: _HasPoolSet) -> list[uuid.UUID]:
    """The workspace's full pool set, element 0 first.

    Empty when the workspace has no pool assigned at all.
    """
    return normalise([workspace.agent_pool_id, *(workspace.agent_pool_extra_ids or [])])


def run_pool_ids(run: Any) -> list[uuid.UUID]:
    """The candidate pools a run may be claimed by, snapshotted at creation.

    ``run.pool_id`` is included because a claim rewrites it to the pool that
    actually took the run — so after a claim this returns the executing pool
    first, and before one it returns element 0 first.
    """
    return normalise([run.pool_id, *(getattr(run, "pool_extra_ids", None) or [])])


def split(pool_ids: list[uuid.UUID]) -> tuple[uuid.UUID | None, list[str]]:
    """Split a resolved set into the ``(element 0, remainder)`` storage form.

    The remainder is returned as strings because it lands in a JSONB column.
    """
    if not pool_ids:
        return None, []
    return pool_ids[0], [str(p) for p in pool_ids[1:]]
