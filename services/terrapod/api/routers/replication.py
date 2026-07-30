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

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import PeerIdentity, get_peer_identity
from terrapod.db.session import get_db
from terrapod.services import blob_classes, replication


def _iso(value: datetime | None) -> str | None:
    """RFC3339 with a trailing Z, per the house rule — never `+00:00`."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


router = APIRouter(prefix="/ha/replication", tags=["ha"])


@router.get("/classes")
async def list_classes(
    _peer: PeerIdentity = Depends(get_peer_identity),
) -> dict:
    """The replication scope, in dependency order.

    The follower reads this rather than carrying its own copy, so a version-skew
    pair converges on what the *sender* actually knows how to serve instead of
    failing on a class one side has never heard of.

    No per-class merge semantics are advertised, because there are none: the
    peer's row is authoritative (#1124).
    """
    return {
        "data": [{"type": "replication-classes", "id": name} for name in replication.registered()]
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

    ``meta.latest-id`` and ``meta.oldest-unapplied-at`` let the caller say how
    far behind it is (#1165), which it cannot work out alone: the page it
    receives is capped at ``limit``, so a full page means "there is more" and
    nothing about how much more. Both are additive.
    """
    page = await replication.read_events(db, after=after, limit=limit)
    return {
        "data": page.events,
        "meta": {
            "cursor": page.cursor,
            "stale-cursor": page.stale_cursor,
            "latest-id": page.latest_id,
            "oldest-unapplied-at": _iso(page.oldest_unapplied_at),
        },
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


# ---------------------------------------------------------------------------
# Object store (#960 phase 4, #1159)
#
# The database half of replication moves rows; these two move the objects those
# rows name. Same direction — the follower pulls — and the same peer-token gate.
# ---------------------------------------------------------------------------


@router.get("/blobs/{blob_class}")
async def list_blobs(
    blob_class: str,
    after: str = Query(default="", description="Last key already consumed"),
    limit: int = Query(default=500, ge=1, le=2000),
    _peer: PeerIdentity = Depends(get_peer_identity),
) -> dict:
    """A page of the class's objects, in key order.

    Size and etag come back with the key so the follower can decide what it needs
    without fetching anything — the diff is the cheap half, and keeping it cheap
    is what lets the copy be throttled.

    Ordered by key, so it resumes: pass the last key back as `after`. That rides
    the storage cursor from #1155, so a page costs a page rather than a full
    listing sliced afterwards.

    Keys owned by a **more specific** class are excluded. `state/index.yaml` lives
    under `state/` but is its own class, and returning it here would have the
    follower copy it twice and count it twice. That filter is why `cursor` and
    `complete` are derived from **what the store returned**, not from what
    survived it: a page thinned by the filter is a short page, not the end of the
    class, and reporting it as the end would silently stop a backfill part-way.
    """
    try:
        cls = blob_classes.get(blob_class)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown blob class"
        ) from None

    # Every registered class has exactly one prefix, and a single `after` cursor
    # only has a meaning for one. A multi-prefix class would need a cursor per
    # prefix; refusing here beats paging one prefix and silently dropping the
    # rest, which is what a naive loop over `cls.prefixes` would do.
    if len(cls.prefixes) != 1:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                f"Blob class {blob_class!r} spans {len(cls.prefixes)} prefixes; "
                "paging needs a per-prefix cursor"
            ),
        )

    from terrapod.storage import get_storage

    store = get_storage()
    raw = await store.list_prefix(cls.prefixes[0], after=after, limit=limit)
    entries = [
        {
            "type": "replication-blobs",
            "id": meta.key,
            "attributes": {
                "key": meta.key,
                "size-bytes": meta.size_bytes,
                "etag": meta.etag,
            },
        }
        for meta in raw
        if blob_classes.owns(cls, meta.key)
    ]

    return {
        "data": entries,
        "meta": {
            # The last key the STORE returned, so the next page resumes past the
            # filtered-out ones rather than fetching them again forever.
            "cursor": raw[-1].key if raw else after,
            "complete": len(raw) < limit,
        },
    }


@router.get("/blobs/{blob_class}/content")
async def read_blob(
    blob_class: str,
    key: str = Query(description="Object key, as returned by the listing"),
    _peer: PeerIdentity = Depends(get_peer_identity),
) -> StreamingResponse:
    """Stream one object's bytes.

    **The key is checked against the class that owns it before a byte is
    served.** A peer token already reads more than a user's, so this must not
    become a way to read any key in the store: a prefix test alone would admit a
    crafted key, and `blob_classes.owns` resolves the owning class instead — the
    key is served under exactly the class that owns it, or refused.

    Streamed rather than read (rule 14): these are the large objects, and a state
    file or provider zip must cross the link without either side holding it.
    """
    try:
        cls = blob_classes.get(blob_class)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown blob class"
        ) from None

    if not blob_classes.owns(cls, key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Key is not owned by blob class {blob_class!r}",
        )

    from terrapod.storage import get_storage
    from terrapod.storage.protocol import ObjectNotFoundError

    store = get_storage()
    try:
        meta = await store.head(key)
    except ObjectNotFoundError:
        # A normal answer, like the entity endpoint's 404: the object was deleted
        # between the listing and the fetch. The follower skips it.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Object not found"
        ) from None

    return StreamingResponse(
        store.get_stream(key),
        media_type="application/octet-stream",
        headers={"Content-Length": str(meta.size_bytes)},
    )
