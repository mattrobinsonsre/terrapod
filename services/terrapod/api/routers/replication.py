"""Peer replication endpoints (#960 phase 3, #1110).

Read-only, and answered by whichever node the peer asks — the **follower**
pulls, so in practice these are served by the leader. They are mounted on the
ordinary public API rather than a shadow surface: a peer already has to reach
this node over the network, and a second listener would be a second thing to
secure, expose, and get wrong.

Every route is gated on ``get_peer_identity``, which accepts a ``peer`` token
and nothing else — and which no other endpoint accepts. That containment is the
point: a peer can read entities an ordinary user could not, so the identity is
deliberately not expressible as a set of roles somebody could be granted.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import PeerIdentity, get_peer_identity
from terrapod.db.session import get_db
from terrapod.services import replication

router = APIRouter(prefix="/ha/replication", tags=["ha"])


@router.get("/classes")
async def list_classes(
    _peer: PeerIdentity = Depends(get_peer_identity),
) -> dict:
    """The replication scope, in dependency order.

    The follower reads this rather than carrying its own copy, so a version-skew
    pair converges on what the *sender* actually knows how to serve instead of
    failing on a class one side has never heard of.
    """
    return {
        "data": [
            {
                "type": "replication-classes",
                "id": name,
                "attributes": {
                    "monotonic-fields": sorted(spec.monotonic_fields),
                    "one-way-true-fields": sorted(spec.one_way_true_fields),
                },
            }
            for name, spec in replication.registered().items()
        ]
    }


@router.get("/events")
async def list_events(
    after: int = Query(default=0, ge=0, description="Last event id already consumed"),
    limit: int = Query(default=500, ge=1, le=5000),
    _peer: PeerIdentity = Depends(get_peer_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Events after ``after``, oldest first.

    ``meta.stale-cursor`` is the important part: it says the caller's cursor
    predates the retained window, so replaying will silently skip rows and the
    caller must backfill instead. Reporting it here — rather than returning an
    innocent-looking empty page — is what stops a lagging follower from
    believing it is in sync.
    """
    page = await replication.read_events(db, after=after, limit=limit)
    return {
        "data": page.events,
        "meta": {"cursor": page.cursor, "stale-cursor": page.stale_cursor},
    }


@router.get("/entities/{entity_class}/{entity_id}")
async def read_entity(
    entity_class: str,
    entity_id: str,
    _peer: PeerIdentity = Depends(get_peer_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Current state of one row.

    A 404 is a normal, expected answer: the row was deleted after the event was
    recorded. The caller skips it and lets the later delete event settle it —
    which is exactly why events carry no payload.
    """
    spec = replication.get(entity_class)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown entity class")
    payload = await replication.read_entity(db, spec, entity_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")
    return {"data": {"type": entity_class, "id": entity_id, "attributes": payload}}


@router.get("/backfill/{entity_class}")
async def backfill(
    entity_class: str,
    after: str = Query(default="", description="Last primary key already consumed"),
    limit: int = Query(default=200, ge=1, le=1000),
    _peer: PeerIdentity = Depends(get_peer_identity),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A page of a class's rows, ordered by primary key.

    Ordered by primary key rather than by time so it is resumable: a backfill
    interrupted halfway continues from the last key instead of restarting a
    large class from the beginning.
    """
    spec = replication.get(entity_class)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown entity class")
    rows = await replication.read_backfill(db, spec, after=after, limit=limit)
    ids = [replication.payload_entity_id(spec, row) for row in rows]
    return {
        "data": [
            {"type": entity_class, "id": row_id, "attributes": row}
            for row_id, row in zip(ids, rows, strict=True)
        ],
        # The resume point is the last row's full key, so a composite class
        # pages in the same order it is sorted by.
        "meta": {"cursor": ids[-1] if ids else after, "complete": len(rows) < limit},
    }
