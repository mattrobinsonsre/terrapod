"""Resolves a workspace's storage location from its database row."""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy import inspect as sa_inspect
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
        #: Created on first use: there is no running loop at import.
        self._gate: asyncio.Semaphore | None = None
        _resolvers.add(self)
        if not event.contains(Session, "after_flush", remember_flushed_workspaces):
            event.listen(Session, "after_flush", remember_flushed_workspaces)
        if not event.contains(Workspace, "load", remember_loaded_workspace):
            event.listen(Workspace, "load", remember_loaded_workspace)

    def _resolution_gate(self) -> asyncio.Semaphore:
        """Bound how much of the connection pool a cache miss can hold.

        This runs inside a request that ALREADY holds a session, so resolving
        checks a SECOND connection out of the same pool. Warm, the cache answers
        and no connection is taken at all; the cold window right after a rollout
        is the one that bites, because every workspace is a first touch at once
        and each in-flight request wants two connections (#2621).

        Without a bound that is a race for the pool between requests and the
        resolutions those same requests are waiting on, which is the shape that
        stalls rather than merely slowing down. With one, the surplus waits here
        -- in process, microseconds, no connection held -- instead of on
        `pool_timeout`.

        Half the persistent pool, so a resolution storm can never take more than
        the requests themselves can hold. Derived rather than a constant: an
        operator who raises `pool_size` for exactly this reason should get the
        benefit without editing code.
        """
        if self._gate is None:
            self._gate = asyncio.Semaphore(max(2, settings.database.pool_size // 2))
        return self._gate

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
            async with self._resolution_gate():
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


def remember_loaded_workspace(target: Workspace, _context: Any) -> None:
    """Cache a workspace's location whenever its row is loaded, from the caller's own session.

    This is what keeps `location_for` off the database on the request path.
    Resolving a miss there opens a SECOND connection inside a request that
    already holds one, and `_resolution_gate` bounds that race without being
    able to remove it: SQLAlchemy's pool cannot reserve capacity, so when the
    pool is genuinely exhausted the surplus still waits on `pool_timeout`. The
    cold window right after a rollout is exactly when every workspace is a first
    touch at once (#2623).

    So the answer is to not need the second connection. Nearly every request
    that reaches storage has already loaded its workspace -- RBAC needs the row
    -- and `storage_location` is on it, undeferred. Taking it from there costs
    one dict write and no query at all, and a list page warms every row it shows.
    This is `remember_flushed_workspaces`' pattern, which already seeds the
    resolver from the caller's own session for a workspace being created; this is
    the same move for one being read.

    Guarded on the attribute being LOADED. A query using `load_only()` without
    this column would otherwise have the access here trigger a lazy refresh --
    one query per row, which is worse than the miss it is avoiding. Nothing in
    the tree does that today; the guard is what keeps that true cheaply rather
    than by convention.
    """
    if target.id is None or "storage_location" in sa_inspect(target).unloaded:
        return
    for resolver in list(_resolvers):
        resolver.remember(str(target.id), target.storage_location)


#: Postgres sqlstates for a schema the storage-location migration has not reached yet.
_PRE_MIGRATION = frozenset({"42703", "42P01"})

#: The reachability probe lists one key under a prefix every location already serves.
_PROBE_PREFIX = "state/"
_PROBE_TIMEOUT_SECONDS = 5.0
#: How long a probe result answers `/ready` before the locations are probed again.
_PROBE_TTL_SECONDS = 30.0


@dataclass
class _NodeView:
    """This node's view of its own storage locations, as one object.

    These were five module globals, which is what they are -- process-wide state
    written by one call and read by another, deliberately. The cost was that every
    writer needed a `global` statement, each carrying a `# noqa: PLW0603`, and
    CodeQL read the rebinds as unused globals because no single function both
    writes and reads one (`py/unused-global-variable`, alert 680). Both complaints
    are false here and both would keep coming back: a dismissal is bound to one
    alert number, so the next edit to this file raises a fresh one.

    Holding them on one instance settles it structurally rather than by argument.
    Assigning an attribute is not a global rebind, so there is nothing left for
    either tool to flag, and a reader can see the whole of this module's mutable
    state in one place (#2623).
    """

    #: Why the storage location check could not run; None once one has run (or none was tried).
    unchecked: str | None = None
    #: Configured locations whose last probe failed, with the reason.
    unreachable: dict[str, str] = field(default_factory=dict)
    #: When the locations were last probed (monotonic seconds); None before the first probe.
    probed_at: float | None = None
    #: When the unconfigured-rows check last RAN (monotonic seconds), which is a different
    #: clock from `probed_at`: that one times a reachability probe of the locations this node
    #: has, and this one times a question about rows it may not have seen yet.
    #:
    #: A standby configured with NO locations is exactly the node that needs the second clock.
    #: Its startup check passes (no row named a location yet), `unchecked` goes to None, and
    #: the readiness path below used to re-query only when a PREVIOUS check had failed -- so
    #: rows replicated afterwards were never noticed, and a promotion without a restart wrote
    #: a located workspace's objects to the default store (#2621).
    counted_at: float | None = None
    #: The one in-flight background refresh; held here so it is not garbage-collected.
    refresh_task: asyncio.Task[None] | None = None


_node = _NodeView()


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
        if reason and name not in _node.unreachable:
            logger.error("Storage location unreachable", location=name, reason=reason)
        elif not reason and name in _node.unreachable:
            logger.info("Storage location reachable again", location=name)
        if reason:
            _node.unreachable[name] = reason
        else:
            _node.unreachable.pop(name, None)


async def _refresh() -> None:
    """Probe the locations and stamp the result; a failure is logged and keeps the old result."""
    try:
        await _probe_locations()
    except Exception as exc:
        logger.warning("Storage location probe refresh failed", error=str(exc))
    _node.probed_at = time.monotonic()


def _refresh_in_background() -> None:
    """Start one refresh when the last result is older than the TTL; never waits for it."""
    if not settings.storage.locations or _node.probed_at is None:
        return
    if time.monotonic() - _node.probed_at < _PROBE_TTL_SECONDS:
        return
    if _node.refresh_task is None or _node.refresh_task.done():
        # Per-process reachability: a scheduler task runs on one replica, so cannot carry it.
        refresh = _refresh()
        _node.refresh_task = asyncio.create_task(
            refresh
        )  # nosemgrep: terrapod-no-raw-background-tasks


async def stop_probing() -> None:
    """Cancel a refresh still in flight; called on shutdown."""
    task, _node.refresh_task = _node.refresh_task, None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _record_unchecked(value: str | None) -> str | None:
    """Set the readiness flag and return what it now holds.

    One producer for a value written in five places across two functions, each
    with its own `global` (#2621). It also makes the write READABLE: both
    writers set it for a LATER call of `locations_readiness` to read, which no
    static analysis can follow, so `py/unused-global-variable` fired on the two
    writes whose value nothing local reads. Returning the global rather than the
    parameter is deliberate -- that is what makes the assignment used.
    """
    _node.unchecked = value
    return _node.unchecked


async def check_locations_at_startup() -> None:
    """Refuse to start on rows naming unconfigured locations; report unreachable locations."""
    await _probe_locations()
    _node.probed_at = time.monotonic()
    # Stamped around the count rather than after it, so a count that RAISED still
    # marks the attempt: `_node.unchecked` then holds the reason and the readiness path
    # re-runs on `pending`, not on a staleness that would fire on every call.
    _node.counted_at = time.monotonic()
    try:
        counts = await _location_counts()
    except Exception as exc:
        _record_unchecked(type(exc).__name__)
        logger.warning("Storage location check failed; not ready until it passes", error=str(exc))
        return
    if missing := _missing(counts):
        problem = _refusal(missing)
        # NOT fatal (#2621). This used to raise, which is a CrashLoopBackOff for
        # every API replica -- including every workspace on the default location,
        # which has nothing to do with the misconfigured row that triggered it.
        # The trigger is a DATABASE ROW, so the blast radius is the whole
        # deployment by construction.
        #
        # Worse, a crash-looping pod serves neither /ready nor /health, so the
        # `storage_locations: <refusal>` detail below is unreachable exactly when
        # an operator needs it. Holding readiness achieves the same safety -- the
        # pod is drained and serves no writes -- and stays inspectable. It is
        # also what `lifespan` already does for a schema behind the app, twelve
        # lines above where this is called from.
        #
        # `_node.unchecked` is assigned HERE rather than cleared above, so the
        # readiness path cannot report healthy on a deployment this check has
        # refused. Clearing it first and relying on the raise was the ordering
        # that made a swallowed raise look fine.
        _record_unchecked(problem)
        logger.error("Not ready: " + problem, locations=missing)
        return
    _record_unchecked(None)


async def locations_readiness() -> tuple[bool, str]:
    """(ready, detail): a failed check or unconfigured rows hold readiness; unreachable is detail.

    The probe result is the last one recorded, never waited for; an old one is refreshed behind.
    """
    _refresh_in_background()
    pending = _node.unchecked
    # `None` is NOT stale, deliberately: this is a REFRESH of the count
    # `check_locations_at_startup` already did, so an unstamped process is one
    # where startup has not run (a test, or a process still booting) and owes no
    # query. Reading it as stale made every first readiness call hit the
    # database, which on a pod whose startup had not run is a RuntimeError
    # reported as `unchecked` -- readiness refused for the absence of a check
    # rather than for its result (#2621).
    stale = (
        _node.counted_at is not None and (time.monotonic() - _node.counted_at) >= _PROBE_TTL_SECONDS
    )
    if pending is not None or stale:
        _node.counted_at = time.monotonic()
        try:
            counts = await _location_counts()
        except Exception as exc:
            return False, f"unchecked: {_record_unchecked(type(exc).__name__)}"
        if missing := _missing(counts):
            problem = _refusal(missing)
            _record_unchecked(problem)
            logger.error("Not ready: " + problem, locations=missing)
            return False, problem
        _record_unchecked(None)
        if pending is not None:
            # Only on RECOVERY. The block above now also runs on the TTL for a
            # node that is perfectly healthy, and logging a pass every thirty
            # seconds per replica is how an operator learns to filter the line
            # that matters.
            logger.info("Storage location check passed", earlier=pending)
    if _node.unreachable:
        return True, f"unreachable: {', '.join(sorted(_node.unreachable))}"
    return True, "healthy"
