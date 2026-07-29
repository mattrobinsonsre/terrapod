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
            await replication.apply_upsert(db, spec, row["attributes"])
            seen.add(str(row["id"]))
            applied += 1
        after = body["meta"]["cursor"]
        # Persist per page so an interrupted backfill resumes rather than
        # restarting a large class from the beginning.
        await _set_cursor(db, entity_class, after, backfilling=not body["meta"]["complete"])
        await db.commit()
        if body["meta"]["complete"]:
            break

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
        except Exception:
            # A peer that is down must not turn into a follower that crashes.
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
                await db.commit()
                return

            own = settings.ha.node_name
            applied = 0
            for event in body["data"]:
                # Origin tags stop the pair echoing changes at each other: a row
                # this node originated must not be applied back onto itself.
                if event["origin-node"] and event["origin-node"] == own:
                    continue
                await _apply_event(db, client, token, event)
                applied += 1

            await _set_cursor(db, replication.EVENT_STREAM, str(body["meta"]["cursor"]))
            await db.commit()

            if applied:
                logger.info(
                    "Applied replication events",
                    applied=applied,
                    cursor=body["meta"]["cursor"],
                )


async def purge_cycle() -> None:
    """Trim the outbox on the node that produces events."""
    from terrapod.db.session import get_db_session

    async with get_db_session() as db:
        await replication.purge_old_events(db, settings.ha.replication.retention_days)


__all__ = [
    "backfill_all",
    "backfill_class",
    "purge_cycle",
    "reset_token_cache",
    "sync_cycle",
]
