"""The follower's pull loop (#960 phase 3, #1110).

Runs as a periodic task on the follower. Each cycle: authenticate to the peer if
the cached token has expired, ask for events since the durable cursor, apply
them, advance. If the peer says the cursor has fallen off the end of the
retained window, backfill the affected classes instead — that fallback is what
makes a bounded outbox safe.

**Nothing here ever blocks the leader.** The leader records events and serves
reads; a follower that is down, slow, or wedged simply stops asking, which is
why "a peer outage must not block a healthy leader" holds by construction
rather than by careful coding.

**Applying is not gated on leadership**, unlike every other write. The leader
write-gate exists to stop a follower originating changes; this path is the
follower faithfully copying changes the leader already made, and gating it would
mean a follower could never converge — the one thing it exists to do.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import ReplicationCursor
from terrapod.http_retry import arequest_with_retry
from terrapod.services import replication

logger = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0)
#: Renew a little early rather than discovering expiry mid-cycle.
_TOKEN_SKEW = timedelta(minutes=2)


@dataclass
class _CachedToken:
    value: str
    expires_at: datetime


_token: _CachedToken | None = None


def reset_token_cache() -> None:
    """Drop the cached peer token (tests, and after an auth failure)."""
    global _token
    _token = None


async def _peer_token(client: httpx.AsyncClient) -> str:
    """Fetch (and cache) an access token for the peer via client_credentials."""
    global _token
    now = datetime.now(UTC)
    if _token is not None and _token.expires_at - _TOKEN_SKEW > now:
        return _token.value

    peer = settings.ha.peer
    resp = await arequest_with_retry(
        client,
        "POST",
        f"{peer.url.rstrip('/')}/oauth/token",
        idempotent=True,
        data={
            "grant_type": "client_credentials",
            "client_id": peer.client_id,
            "client_secret": peer.client_secret,
        },
    )
    resp.raise_for_status()
    body = resp.json()
    _token = _CachedToken(
        value=body["access_token"],
        expires_at=now + timedelta(seconds=int(body.get("expires_in", 3600))),
    )
    return _token.value


# --------------------------------------------------------------------------
# Cursors
# --------------------------------------------------------------------------


async def _get_cursor(db: AsyncSession, entity_class: str) -> ReplicationCursor:
    row = await db.scalar(
        select(ReplicationCursor).where(ReplicationCursor.entity_class == entity_class)
    )
    if row is None:
        row = ReplicationCursor(entity_class=entity_class, position="", backfilling=False)
        db.add(row)
        await db.flush()
    return row


async def _set_cursor(
    db: AsyncSession, entity_class: str, position: str, *, backfilling: bool = False
) -> None:
    row = await _get_cursor(db, entity_class)
    row.position = position
    row.backfilling = backfilling
    row.updated_at = datetime.now(UTC)


def _record_lag(row, meta: dict) -> None:
    """Keep what the peer said about the end of its stream (#1165).

    Written from the SAME response the cursor advances from, so the pair is
    always consistent: `peer_latest_position` minus the cursor is how far behind
    this node was at the moment of that pull, and it is honest about being a
    statement about that moment rather than a live measurement.

    A peer that does not send the fields (older code) leaves them untouched
    rather than zeroed — "unknown" and "caught up" must not look the same.
    """
    latest = meta.get("latest-id")
    if latest is not None:
        row.peer_latest_position = str(latest)
    if "oldest-unapplied-at" in meta:
        raw = meta["oldest-unapplied-at"]
        row.oldest_unapplied_at = (
            datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
        )


# --------------------------------------------------------------------------
# The cycle
# --------------------------------------------------------------------------


async def _apply_event(
    db: AsyncSession, client: httpx.AsyncClient, token: str, event: dict
) -> None:
    entity_class = event["entity-class"]
    entity_id = event["entity-id"]
    spec = replication.get(entity_class)
    if spec is None:
        # The peer replicates a class this node does not know — a newer peer
        # across a version skew. Skipping is correct and additive-safe; failing
        # would wedge the whole stream on one unknown row.
        logger.debug("Skipping unknown replication class", entity_class=entity_class)
        return

    if event["op"] == replication.DELETE:
        await replication.apply_delete(db, spec, entity_id)
        return

    base = settings.ha.peer.url.rstrip("/")
    resp = await arequest_with_retry(
        client,
        "GET",
        f"{base}/api/terrapod/v1/ha/replication/entities/{entity_class}/{entity_id}",
        idempotent=True,
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code == 404:
        # Deleted between the event and this read. The later delete event
        # settles it; resurrecting a stale copy here would be worse.
        return
    resp.raise_for_status()
    await replication.apply_upsert(db, spec, resp.json()["data"]["attributes"])


async def _try_apply(db: AsyncSession, client: httpx.AsyncClient, token: str, event: dict) -> bool:
    """Apply one event inside its own savepoint. False when it could not land.

    One row must never be able to stop the stream. Before this, an apply that
    raised aborted the whole cycle and left the cursor unmoved, so the next
    cycle re-fetched the same row and failed identically — replication for
    EVERY class stopped, silently, until someone noticed at a failover (#1180).

    `_apply_event` already reasons this way for a class the peer knows and this
    node does not ("failing would wedge the whole stream on one unknown row").
    This is the same argument applied to the rows themselves.
    """
    try:
        async with db.begin_nested():
            await _apply_event(db, client, token, event)
        return True
    except Exception:
        logger.warning(
            "Could not apply a replicated row; deferring it",
            entity_class=event["entity-class"],
            entity_id=event["entity-id"],
            op=event["op"],
            exc_info=True,
        )
        return False


async def _try_upsert(db: AsyncSession, spec, attributes: dict) -> bool:
    """Backfill's equivalent of `_try_apply` — one row, one savepoint."""
    try:
        async with db.begin_nested():
            await replication.apply_upsert(db, spec, attributes)
        return True
    except Exception:
        logger.warning(
            "Could not apply a backfilled row; deferring it",
            entity_class=spec.name,
            exc_info=True,
        )
        return False


async def backfill_class(
    db: AsyncSession, client: httpx.AsyncClient, token: str, entity_class: str
) -> int:
    """Pull a whole class from the peer, resuming from the stored position.

    On a **complete, error-free** pass this also reconciles deletions (#1115):
    rows this node holds that the peer does not are removed, because backfill
    otherwise cannot converge a delete that happened while this node was beyond
    the retained event window — and a revoked API token surviving that gap
    works again after a failover.

    Reconciliation is deliberately gated on completion. A pass that raised
    part-way has a truncated view of the peer's rows, and acting on it would
    read as mass deletion.
    """
    spec = replication.get(entity_class)
    if spec is None:
        return 0

    base = settings.ha.peer.url.rstrip("/")
    cursor = await _get_cursor(db, entity_class)
    after = cursor.position
    applied = 0
    # Only meaningful when the pass starts from the beginning; a resumed
    # backfill has already-applied pages it never sees again, so it cannot
    # judge what is missing.
    from_scratch = not after
    seen: set[str] = set()
    deferred: list[dict] = []

    # Claim the class as in-progress before the first page. Completion clears
    # it, so `backfilling` is an unambiguous "this pass never finished" — which
    # is what `backfill_pending_classes` retries on, and is why a pod restart
    # part-way through a large class now resumes instead of being forgotten.
    await _set_cursor(db, entity_class, after or "", backfilling=True)
    await db.commit()

    while True:
        resp = await arequest_with_retry(
            client,
            "GET",
            f"{base}/api/terrapod/v1/ha/replication/backfill/{entity_class}",
            idempotent=True,
            params={"after": after, "limit": 200},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        for row in body["data"]:
            if await _try_upsert(db, spec, row["attributes"]):
                seen.add(str(row["id"]))
                applied += 1
            else:
                deferred.append(row)
        after = body["meta"]["cursor"]
        # Persist per page so an interrupted backfill resumes rather than
        # restarting a large class from the beginning.
        await _set_cursor(db, entity_class, after, backfilling=not body["meta"]["complete"])
        await db.commit()
        if body["meta"]["complete"]:
            break

    # One retry pass over the rows that would not land. Ordering within a class
    # is the usual reason (a row referencing another row of the same class that
    # had not arrived yet), and retrying resolves it without a dependency graph.
    if deferred:
        still: list[dict] = []
        for row in deferred:
            if await _try_upsert(db, spec, row["attributes"]):
                seen.add(str(row["id"]))
                applied += 1
            else:
                still.append(row)
        deferred = still
        await db.commit()

    if deferred:
        # An incomplete pass must not be recorded as finished: leaving
        # `backfilling` set is what makes the next cycle try this class again.
        # Reconciliation is skipped for the same reason it is skipped on a
        # resumed pass — `seen` is not the peer's full set, and acting on a
        # partial view reads as mass deletion.
        logger.warning(
            "Backfill left rows unapplied; the class stays pending",
            entity_class=entity_class,
            unapplied=len(deferred),
            applied=applied,
        )
        return applied

    removed = 0
    if from_scratch:
        removed = len(await replication.reconcile_deletions(db, spec, seen))

    # Clear the resume point now the pass is done. The class cursor is a
    # position WITHIN an in-progress backfill, not a high-water mark: leaving
    # it set means the next backfill of this class resumes past every row it
    # already saw and re-syncs almost nothing — so a node that ages out a
    # second time would never recover. Clearing it also makes the next pass
    # eligible to reconcile.
    await _set_cursor(db, entity_class, "", backfilling=False)
    await db.commit()

    logger.info(
        "Backfilled replication class",
        entity_class=entity_class,
        rows=applied,
        removed=removed,
        reconciled=from_scratch,
    )
    return applied


async def backfill_pending_classes(
    db: AsyncSession, client: httpx.AsyncClient, token: str
) -> list[str]:
    """Backfill any class that has never completed a pull, and say which.

    A class that is newly REGISTERED has the same problem as a newly ADDED
    node: there are no deltas to carry it. Everything that existed before the
    upgrade predates the event stream this node is following, and anything
    written during the rolling upgrade — while this node still had the older
    code — was skipped as an unknown class and its cursor advanced past it.
    Deltas never replay, so without this the class stays empty on the follower
    indefinitely, and the gap only shows at a promotion (#1175).

    "Never completed" covers two states, and both need the same treatment: no
    cursor row at all (never started), and a cursor still marked `backfilling`
    (started, never finished — a pod restart part-way, or a pass that could not
    land every row). Keying on those rather than on a version number means it
    needs nothing declared and cannot be forgotten when the next class is
    registered: registering one is still the whole opt-in.

    Cheap in the steady state — one indexed cursor lookup per class per cycle,
    and nothing to do once each has completed once.
    """
    started: list[str] = []
    for entity_class in replication.registered():
        row = await db.scalar(
            select(ReplicationCursor).where(ReplicationCursor.entity_class == entity_class)
        )
        if row is not None and not row.backfilling:
            continue
        logger.info(
            "Backfilling a class that has never completed a pull",
            entity_class=entity_class,
            resuming=row is not None,
        )
        await backfill_class(db, client, token, entity_class)
        started.append(entity_class)
    return started


async def backfill_all(db: AsyncSession, client: httpx.AsyncClient, token: str) -> None:
    """Backfill every class, in registration (dependency) order.

    Order matters: a join token cannot be inserted before its pool exists.
    """
    for entity_class in replication.registered():
        await backfill_class(db, client, token, entity_class)


async def sync_cycle() -> None:
    """One pull. Registered as a periodic task; safe to call concurrently-ish
    because the scheduler's claim already serialises it across replicas."""
    cfg = settings.ha.replication
    if not cfg.enabled:
        return

    from terrapod.db.session import get_db_session

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            token = await _peer_token(client)
        except Exception as exc:
            # A peer that is down must not turn into a follower that crashes.
            #
            # Only drop the cached token when the peer actually REJECTED the
            # credential. A 429 or a 5xx says nothing about whether it is valid,
            # and discarding it there guarantees another mint next cycle — which
            # is how a transient rate limit became permanent on a live pair
            # (#960): reset, retry, get limited, reset again.
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                reset_token_cache()
            logger.warning("Could not authenticate to peer; will retry", exc_info=True)
            return

        base = settings.ha.peer.url.rstrip("/")
        async with get_db_session() as db:
            cursor = await _get_cursor(db, replication.EVENT_STREAM)
            after = int(cursor.position or 0)

            resp = await arequest_with_retry(
                client,
                "GET",
                f"{base}/api/terrapod/v1/ha/replication/events",
                idempotent=True,
                params={"after": after, "limit": cfg.batch_size},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            body = resp.json()

            if body["meta"]["stale-cursor"]:
                # The gap is unrecoverable by replay: those events were purged.
                # Backfilling is not a fallback for a broken stream, it is the
                # designed recovery — and it is why the window can be bounded.
                logger.info("Replication cursor is stale; backfilling", after=after)
                await backfill_all(db, client, token)
                await _set_cursor(db, replication.EVENT_STREAM, str(body["meta"]["cursor"]))
                _record_lag(await _get_cursor(db, replication.EVENT_STREAM), body["meta"])
                await db.commit()
                return

            own = settings.ha.node_name
            applied = 0
            deferred: list[dict] = []
            # The cursor may only advance across a contiguous run of settled
            # events: the first deferred one caps it, so it is re-fetched next
            # cycle rather than lost. Re-applying the events after it costs
            # nothing — an event is a notification, not a snapshot, so every
            # apply is an idempotent upsert of the peer's current state.
            safe_cursor: int | None = None
            blocked = False

            for event in body["data"]:
                # Origin tags stop the pair echoing changes at each other: a row
                # this node originated must not be applied back onto itself.
                if event["origin-node"] and event["origin-node"] == own:
                    if not blocked:
                        safe_cursor = event["id"]
                    continue
                if await _try_apply(db, client, token, event):
                    applied += 1
                    if not blocked:
                        safe_cursor = event["id"]
                else:
                    deferred.append(event)
                    blocked = True

            # A child whose parent arrives later in the same batch fails on the
            # first pass and lands on this one, which is the common ordering
            # case and needs no dependency graph to resolve.
            if deferred:
                still: list[dict] = []
                for event in deferred:
                    if await _try_apply(db, client, token, event):
                        applied += 1
                    else:
                        still.append(event)
                deferred = still
                if not deferred:
                    safe_cursor = body["meta"]["cursor"]

            if safe_cursor is not None:
                await _set_cursor(db, replication.EVENT_STREAM, str(safe_cursor))
            _record_lag(await _get_cursor(db, replication.EVENT_STREAM), body["meta"])

            # AFTER the deltas, not before. A class registered since this node
            # last ran has no deltas to carry it, but its rows routinely point
            # at parents in ESTABLISHED classes whose own rows are still sitting
            # unapplied in this batch — backfilling first pulled a configuration
            # version whose workspace did not exist yet (#1180).
            await backfill_pending_classes(db, client, token)
            await db.commit()

            if deferred:
                # Holding the cursor is also what makes this visible without a
                # new signal: the lag recorded above is peer-latest minus the
                # cursor, so a row that never lands shows as replication lag
                # that climbs and does not recover — which the HA page and the
                # existing gauges already report.
                logger.warning(
                    "Replication rows could not be applied; holding the cursor",
                    unapplied=len(deferred),
                    classes=sorted({e["entity-class"] for e in deferred}),
                    cursor_held_at=safe_cursor,
                )

            if applied:
                logger.info(
                    "Applied replication events",
                    applied=applied,
                    cursor=body["meta"]["cursor"],
                )


async def purge_cycle() -> None:
    """Trim the outbox, and refresh the replication gauges.

    The gauges ride this task rather than getting their own: both nodes of a
    pair run it, the numbers are cheap, and an hourly refresh matches what they
    describe — retention margin moves in days, and "seconds since last sync" is
    computed from a timestamp rather than sampled.
    """
    from terrapod.db.session import get_db_session

    async with get_db_session() as db:
        await replication.purge_old_events(db, settings.ha.replication.retention_days)
        await _refresh_metrics(db)


async def _refresh_metrics(db: AsyncSession) -> None:
    from terrapod.api.metrics import (
        REPLICATION_BACKFILLING_CLASSES,
        REPLICATION_EVENTS_RETAINED,
        REPLICATION_OLDEST_EVENT_AGE,
        REPLICATION_SECONDS_SINCE_SYNC,
    )

    state = await replication.read_status(db)
    now = datetime.now(UTC)

    REPLICATION_BACKFILLING_CLASSES.set(len(state.backfilling))
    REPLICATION_EVENTS_RETAINED.set(state.events_retained)
    if state.last_sync_at:
        REPLICATION_SECONDS_SINCE_SYNC.set(
            (now - state.last_sync_at.astimezone(UTC)).total_seconds()
        )
    if state.oldest_event_at:
        REPLICATION_OLDEST_EVENT_AGE.set(
            (now - state.oldest_event_at.astimezone(UTC)).total_seconds()
        )


__all__ = [
    "backfill_all",
    "backfill_class",
    "purge_cycle",
    "reset_token_cache",
    "sync_cycle",
]
