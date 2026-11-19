"""Resolves a workspace's storage location from its database row."""

from __future__ import annotations

import asyncio
import time
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

#: The reachability probe lists one key under a prefix every location already serves.
_PROBE_PREFIX = "state/"
_PROBE_TIMEOUT_SECONDS = 5.0
#: How long a probe result answers `/ready` before the locations are probed again.
_PROBE_TTL_SECONDS = 30.0

#: Why the storage location check could not run; None once one has run (or none was tried).
_unchecked: str | None = None

#: Configured locations whose last probe failed, with the reason.
_unreachable: dict[str, str] = {}

#: When the locations were last probed (monotonic seconds); None before the first probe.
_probed_at: float | None = None
#: The one in-flight background refresh; held here so it is not garbage-collected.
_refresh_task: asyncio.Task[None] | None = None


def _is_pre_migration(exc: BaseException) -> bool:
    return getattr(getattr(exc, "orig", None), "sqlstate", None) in _PRE_MIGRATION


async def _location_counts() -> dict[str, int]:
    """Workspaces per named storage location; empty on a schema before the migration."""
    column = Workspace.storage_location
    try:
        async with get_db_session() as db:
            rows = await db.execute(
                select(column, func.count())
                .where(column.is_not(None), column != DEFAULT_STORAGE_LOCATION)
                .group_by(column)
            )
            return dict(rows.all())
    except Exception as exc:
        if _is_pre_migration(exc):
            return {}
        raise


def _missing(counts: dict[str, int]) -> dict[str, int]:
    return {name: n for name, n in counts.items() if name not in settings.storage.locations}


def _refusal(missing: dict[str, int]) -> str:
    names = ", ".join(sorted(missing))
    if settings.storage.locations:
        problem = f"workspaces reference storage locations that are not configured: {names}"
    else:
        problem = f"workspaces reference storage locations but none are configured: {names}"
    return (
        f"{problem}; re-add them to the configured storage locations (api.config.storage.locations)"
    )


async def _probe(store: Any) -> str | None:
    """None when a location answers a read-only listing in time, else why it did not."""
    try:
        await asyncio.wait_for(store.list_prefix(_PROBE_PREFIX, limit=1), _PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return f"no answer within {_PROBE_TIMEOUT_SECONDS:g}s"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"[:200]
    return None


async def _probe_locations() -> None:
    """Probe every named location, recording the outcome, the gauge and any change in it."""
    from terrapod.storage import get_storage_or_none
    from terrapod.storage.routing import RoutingStore

    store = get_storage_or_none()
    if not isinstance(store, RoutingStore) or not store.locations:
        return
    from terrapod.api.metrics import STORAGE_LOCATION_REACHABLE

    stores = dict(store.locations)
    reasons = await asyncio.gather(*(_probe(s) for s in stores.values()))
    for name, reason in zip(stores, reasons, strict=True):
        STORAGE_LOCATION_REACHABLE.labels(location=name).set(0 if reason else 1)
        if reason and name not in _unreachable:
            logger.error("Storage location unreachable", location=name, reason=reason)
        elif not reason and name in _unreachable:
            logger.info("Storage location reachable again", location=name)
        if reason:
            _unreachable[name] = reason
        else:
            _unreachable.pop(name, None)


async def _refresh() -> None:
    """Probe the locations and stamp the result; a failure is logged and keeps the old result."""
    global _probed_at  # noqa: PLW0603
    try:
        await _probe_locations()
    except Exception as exc:
        logger.warning("Storage location probe refresh failed", error=str(exc))
    _probed_at = time.monotonic()


def _refresh_in_background() -> None:
    """Start one refresh when the last result is older than the TTL; never waits for it."""
    global _refresh_task  # noqa: PLW0603
    if not settings.storage.locations or _probed_at is None:
        return
    if time.monotonic() - _probed_at < _PROBE_TTL_SECONDS:
        return
    if _refresh_task is None or _refresh_task.done():
        # Per-process reachability: a scheduler task runs on one replica, so cannot carry it.
        refresh = _refresh()
        _refresh_task = asyncio.create_task(refresh)  # nosemgrep: terrapod-no-raw-background-tasks


async def stop_probing() -> None:
    """Cancel a refresh still in flight; called on shutdown."""
    global _refresh_task  # noqa: PLW0603
    task, _refresh_task = _refresh_task, None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def check_locations_at_startup() -> None:
    """Refuse to start on rows naming unconfigured locations; report unreachable locations."""
    global _unchecked, _probed_at  # noqa: PLW0603
    await _probe_locations()
    _probed_at = time.monotonic()
    try:
        counts = await _location_counts()
    except Exception as exc:
        _unchecked = type(exc).__name__
        logger.warning("Storage location check failed; not ready until it passes", error=str(exc))
        return
    _unchecked = None
    if missing := _missing(counts):
        problem = _refusal(missing)
        logger.error("Refusing to start: " + problem, locations=missing)
        raise RuntimeError(problem)


async def locations_readiness() -> tuple[bool, str]:
    """(ready, detail): a failed check or unconfigured rows hold readiness; unreachable is detail.

    The probe result is the last one recorded, never waited for; an old one is refreshed behind.
    """
    global _unchecked  # noqa: PLW0603
    _refresh_in_background()
    pending = _unchecked
    if pending is not None:
        try:
            counts = await _location_counts()
        except Exception as exc:
            _unchecked = type(exc).__name__
            return False, f"unchecked: {_unchecked}"
        if missing := _missing(counts):
            problem = _refusal(missing)
            _unchecked = problem
            logger.error("Not ready: " + problem, locations=missing)
            return False, problem
        _unchecked = None
        logger.info("Storage location check passed", earlier=pending)
    if _unreachable:
        return True, f"unreachable: {', '.join(sorted(_unreachable))}"
    return True, "healthy"
