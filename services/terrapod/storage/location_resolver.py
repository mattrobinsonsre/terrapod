"""Resolves a workspace's storage location from its database row."""

from __future__ import annotations

import uuid
import weakref
from collections import OrderedDict
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from terrapod.config import DEFAULT_STORAGE_LOCATION, settings
from terrapod.db.models import Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.storage.protocol import ObjectStoreError

logger = get_logger(__name__)

_resolvers: weakref.WeakSet[DbLocationResolver] = weakref.WeakSet()


class DbLocationResolver:
    """Reads `Workspace.storage_location`; known workspaces are cached, as the value never changes."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max_entries = max_entries
        self._known: OrderedDict[str, str] = OrderedDict()
        _resolvers.add(self)
        if not event.contains(Session, "after_flush", remember_flushed_workspaces):
            event.listen(Session, "after_flush", remember_flushed_workspaces)

    def remember(self, workspace_id: str, location: str | None) -> None:
        self._known[workspace_id] = location or DEFAULT_STORAGE_LOCATION
        self._known.move_to_end(workspace_id)
        while len(self._known) > self._max_entries:
            self._known.popitem(last=False)

    async def location_for(self, workspace_id: str) -> str | None:
        if (known := self._known.get(workspace_id)) is not None:
            self._known.move_to_end(workspace_id)
            return known
        try:
            parsed = uuid.UUID(workspace_id)
        except ValueError:
            return None
        if str(parsed) != workspace_id:
            return None
        try:
            async with get_db_session() as db:
                row = (
                    await db.execute(
                        select(Workspace.storage_location).where(Workspace.id == parsed)
                    )
                ).one_or_none()
        except Exception as exc:
            raise ObjectStoreError(
                f"could not resolve the storage location of workspace {workspace_id}"
            ) from exc
        if row is None:
            return None
        location = row[0] or DEFAULT_STORAGE_LOCATION
        self.remember(workspace_id, location)
        return location


def remember_flushed_workspaces(session: Any, _flush_context: Any) -> None:
    """Cache new workspaces at flush, so writes made before the commit can be routed."""
    for obj in session.new:
        if isinstance(obj, Workspace) and obj.id is not None:
            for resolver in list(_resolvers):
                resolver.remember(str(obj.id), obj.storage_location)


#: Postgres sqlstates for a schema the storage-location migration has not reached yet.
_PRE_MIGRATION = frozenset({"42703", "42P01"})

#: Why the storage location check could not run; None once one has run (or none was tried).
_unchecked: str | None = None


def _is_pre_migration(exc: BaseException) -> bool:
    return getattr(getattr(exc, "orig", None), "sqlstate", None) in _PRE_MIGRATION


async def _unconfigured_locations() -> dict[str, int]:
    """Workspaces per storage location their rows name but this node does not configure."""
    column = Workspace.storage_location
    try:
        async with get_db_session() as db:
            rows = await db.execute(
                select(column, func.count())
                .where(column.is_not(None), column != DEFAULT_STORAGE_LOCATION)
                .group_by(column)
            )
            return {
                name: count for name, count in rows.all() if name not in settings.storage.locations
            }
    except Exception as exc:
        if _is_pre_migration(exc):
            return {}
        raise


def _refusal(missing: dict[str, int]) -> str:
    names = ", ".join(sorted(missing))
    if settings.storage.locations:
        problem = f"workspaces reference storage locations that are not configured: {names}"
    else:
        problem = f"workspaces reference storage locations but none are configured: {names}"
    return (
        f"{problem}; re-add them to the configured storage locations (api.config.storage.locations)"
    )


async def check_locations_at_startup() -> None:
    """Refuse to start when workspace rows name storage locations this node does not configure."""
    global _unchecked  # noqa: PLW0603
    try:
        missing = await _unconfigured_locations()
    except Exception as exc:
        _unchecked = type(exc).__name__
        logger.warning("Storage location check failed; not ready until it passes", error=str(exc))
        return
    _unchecked = None
    if missing:
        problem = _refusal(missing)
        logger.error("Refusing to start: " + problem, locations=missing)
        raise RuntimeError(problem)


async def locations_readiness() -> str:
    """`healthy`, or why not: a startup check that could not run is retried until one passes."""
    global _unchecked  # noqa: PLW0603
    if _unchecked is None:
        return "healthy"
    try:
        missing = await _unconfigured_locations()
    except Exception as exc:
        _unchecked = type(exc).__name__
        return f"unchecked: {_unchecked}"
    if missing:
        problem = _refusal(missing)
        logger.error("Not ready: " + problem, locations=missing)
        return problem
    _unchecked = None
    return "healthy"
