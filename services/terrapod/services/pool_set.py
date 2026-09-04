"""The workspace agent-pool set (#1085 / #1087, #960 phase 0).

A workspace routes its runs to a **set** of agent pools rather than a single
one. The set is **flat**: every pool in it is equally eligible to claim a run.
There is no primary, no rank, no preference and no rotation — a queued run is
offered to all of them at once and whichever pool has a listener that claims it
first runs it.

It is stored as a mapping table (``workspace_agent_pools``) with a real foreign
key on each side, so deleting an agent pool detaches it from every workspace by
``ON DELETE CASCADE`` — no application-side sweeping that a future code path
could forget. Order is display-only: it is what the operator typed and what the
UI and API echo back, and carries no dispatch meaning.

Runs are deliberately different. A run's candidate pools are a **snapshot**
frozen at creation (``pool_id`` plus a ``pool_extra_ids`` JSONB list, the same
shape as the run's other snapshots), because history must not be rewritten by a
pool being deleted mid-flight. After a claim ``run.pool_id`` holds one true
value: the pool actually executing the run.

Every caller resolves the set through this module rather than walking the links
by hand.
"""

from __future__ import annotations

import uuid
from typing import Any

from terrapod.db.models import Workspace, WorkspaceAgentPool


def _coerce(value: Any) -> uuid.UUID | None:
    """Best-effort UUID coercion, tolerating the ``apool-`` id prefix."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).removeprefix("apool-"))
    except ValueError, AttributeError:
        return None


def normalise(pool_ids: list[Any] | None) -> list[uuid.UUID]:
    """Coerce a raw list to UUIDs, dropping blanks and de-duplicating.

    Order is preserved because it is what the operator typed — it carries no
    dispatch meaning.
    """
    seen: list[uuid.UUID] = []
    for raw in pool_ids or []:
        parsed = _coerce(raw)
        if parsed is not None and parsed not in seen:
            seen.append(parsed)
    return seen


def workspace_pool_ids(workspace: Any) -> list[uuid.UUID]:
    """The workspace's pool set, in display order. Empty when none is assigned."""
    links = getattr(workspace, "agent_pool_links", None) or []
    return normalise([link.agent_pool_id for link in links])


def workspace_pool_names(workspace: Any) -> list[str]:
    """Pool names in the same order, for display. Skips any link with no row."""
    links = getattr(workspace, "agent_pool_links", None) or []
    return [link.agent_pool.name for link in links if link.agent_pool is not None]


def set_workspace_pools(workspace: Workspace, pool_ids: list[Any]) -> list[uuid.UUID]:
    """Replace the workspace's pool set, returning the resolved ids.

    Assigning the collection (rather than mutating it) lets SQLAlchemy's
    ``delete-orphan`` cascade remove the rows that are no longer in the set.
    """
    resolved = normalise(pool_ids)
    workspace.agent_pool_links = [
        WorkspaceAgentPool(agent_pool_id=pid, ordinal=i) for i, pid in enumerate(resolved)
    ]
    return resolved


def run_pool_ids(run: Any) -> list[uuid.UUID]:
    """The candidate pools a run may be claimed by, snapshotted at creation.

    ``run.pool_id`` is included because a claim rewrites it to the pool that
    actually took the run — so after a claim this returns the executing pool
    first, and before one it returns the pool the run was created against.
    """
    return normalise([run.pool_id, *(getattr(run, "pool_extra_ids", None) or [])])


def split(pool_ids: list[uuid.UUID]) -> tuple[uuid.UUID | None, list[str]]:
    """Split a resolved set into the run snapshot's ``(pool_id, extras)`` form.

    The extras are strings because they land in a JSONB column.
    """
    if not pool_ids:
        return None, []
    return pool_ids[0], [str(p) for p in pool_ids[1:]]
