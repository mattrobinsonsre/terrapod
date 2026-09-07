"""Peer replication: the outbox, the class registry, and applying changes.

Part of #960 phase 3 (#1110). The shape of the thing:

**The follower pulls.** The leader records what changed and otherwise does
nothing; the follower asks for events since its cursor and applies them. This is
what makes "a peer outage must never block a healthy leader" true by
construction rather than by careful coding — a dead follower simply stops
asking, and the leader never knows or cares.

**An event is a notification, not a snapshot** (see ``ReplicationEvent``). The
follower fetches current state when it applies, so replay is idempotent and a
backlog collapses to one read per distinct row rather than a queue of stale
copies to apply in order.

**Backfill is not the exception, it is the common case.** Adding a second node
to a running install has no deltas at all — it is pure backfill. It is also the
fallback whenever the follower's cursor falls off the end of the retained event
window, which is what lets the leader purge old events freely instead of
choosing between unbounded growth and blocking on a dead peer.

**Encryption is per-node.** Serialisation reads through the ORM, so an
``EncryptedText`` column arrives here already decrypted, travels the
authenticated peer link as plaintext, and is re-encrypted under the receiving
node's own key on write. Neither node needs the other's key.
"""

import base64
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import and_, delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from terrapod.db.models import ReplicationEvent

logger = structlog.get_logger(__name__)

UPSERT = "upsert"
DELETE = "delete"

#: Cursor row that tracks the shared event stream rather than a class backfill.
EVENT_STREAM = "*"

#: Rows removed per statement during backfill reconciliation.
_DELETE_CHUNK = 500


@dataclass(frozen=True)
class ReplicatedClass:
    """One entity class in the replication scope.

    There is no conflict-resolution here, deliberately. In a leader/follower
    pair only the leader writes, so **the peer's row is authoritative** —
    applying it is the whole rule. An earlier active-active design needed
    per-field merge semantics; that design was dropped from #960 before this
    code existed, and the machinery it implied went with it (#1124).
    """

    name: str
    model: type
    #: Primary-key columns, in order. A tuple because several replicated
    #: classes are junction tables — role assignments are keyed by
    #: (provider_name, email), and a promoted node with users and roles but no
    #: mapping between them has nobody with any permissions (#1119).
    pk_attrs: tuple[str, ...] = ("id",)
    #: Columns never sent (server-managed, or meaningless on the other node).
    exclude: frozenset[str] = frozenset()
    #: Optional per-class overrides for entities the generic path cannot handle.
    serialize: Callable[[Any], dict] | None = None
    deserialize: Callable[[dict], dict] | None = None


_REGISTRY: dict[str, ReplicatedClass] = {}
#: Models to watch, resolved once so the flush hook stays a dict lookup.
_BY_MODEL: dict[type, ReplicatedClass] = {}


def _ensure_loaded() -> None:
    """Import the registry module so the scope is populated.

    Registration happens as an import side effect, which means the scope is
    empty until something imports `replication_registry`. Relying on a caller to
    do that would make replication silently a no-op the day somebody tidies up
    what looks like an unused import — so every entry point resolves it here
    instead. The registry is a fixed module in this repo, not a plugin system,
    so there is nothing to discover and no ordering to get wrong.
    """
    # No once-guard: Python already memoises imports, so a second call is a
    # `sys.modules` dict lookup. A hand-rolled flag would only be another thing
    # able to be wrong.
    from terrapod.services import replication_registry  # noqa: F401


def register(spec: ReplicatedClass) -> ReplicatedClass:
    """Add a class to the replication scope.

    Registration is the whole opt-in: the flush hook picks the class up
    automatically, so a write path cannot forget to emit an event. It also makes
    the class visible to the CI gate that requires a backfill path and the full
    per-class test matrix.
    """
    _REGISTRY[spec.name] = spec
    _BY_MODEL[spec.model] = spec
    return spec


def registered() -> dict[str, ReplicatedClass]:
    _ensure_loaded()
    return dict(_REGISTRY)


def get(name: str) -> ReplicatedClass | None:
    _ensure_loaded()
    return _REGISTRY.get(name)


# --------------------------------------------------------------------------
# Entity ids
# --------------------------------------------------------------------------


def encode_entity_id(spec: ReplicatedClass, values: list[Any]) -> str:
    """Render a row's key as one string.

    Single-key classes keep their plain id — unchanged on the wire and in the
    outbox, so nothing already written needs re-encoding.

    Composite keys are base64url(JSON), which survives the outbox column, a URL
    path segment, and a set comparison without escaping. Naive concatenation
    would not: an email or provider name can contain almost any separator, so
    two distinct assignments could alias to the same id and one would silently
    overwrite the other.
    """
    if len(spec.pk_attrs) == 1:
        return str(values[0])
    raw = json.dumps([str(v) for v in values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_entity_id(spec: ReplicatedClass, entity_id: str) -> list[str] | None:
    """Recover the key components, or None if the id is unusable."""
    if len(spec.pk_attrs) == 1:
        return [entity_id]
    try:
        padded = entity_id + "=" * (-len(entity_id) % 4)
        parts = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError):
        return None
    if not isinstance(parts, list) or len(parts) != len(spec.pk_attrs):
        return None
    return [str(p) for p in parts]


def _pk_columns(spec: ReplicatedClass) -> list[Any]:
    return [getattr(spec.model, name) for name in spec.pk_attrs]


def _coerce_pk(column: Any, raw: str) -> Any:
    """Turn one wire key component into the column's native type."""
    if column.expression.type.python_type is uuid.UUID:
        return uuid.UUID(raw)
    return raw


def _pk_filter(spec: ReplicatedClass, parts: list[str]) -> Any:
    """An AND over every key component."""
    conditions = [
        col == _coerce_pk(col, part) for col, part in zip(_pk_columns(spec), parts, strict=True)
    ]
    return and_(*conditions)


def row_entity_id(spec: ReplicatedClass, obj: Any) -> str | None:
    values = [getattr(obj, name, None) for name in spec.pk_attrs]
    if any(v is None for v in values):
        return None
    return encode_entity_id(spec, values)


def payload_entity_id(spec: ReplicatedClass, payload: dict) -> str | None:
    values = [payload.get(name) for name in spec.pk_attrs]
    if any(v is None for v in values):
        return None
    return encode_entity_id(spec, values)


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return float(value)
    return value


def serialize_row(spec: ReplicatedClass, obj: Any) -> dict:
    """Dump a row to a JSON-safe dict of column values."""
    if spec.serialize is not None:
        return spec.serialize(obj)
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(spec.model)
    return {
        col.key: _json_safe(getattr(obj, col.key))
        for col in mapper.column_attrs
        if col.key not in spec.exclude
    }


def _column_python_type(column_type: Any) -> type | None:
    """The Python type a column carries, or None when it declines to say.

    Only two answers change anything below — UUID and datetime need reviving
    from their string form. A type that cannot name itself is therefore never
    one that needs coercion, so "don't know" and "nothing to do" are the same
    answer here, and refusing to guess is safe.

    SQLAlchemy's base `TypeEngine.python_type` *raises* rather than returning
    None, and `TypeDecorator` does not forward the call to its impl. Letting
    that propagate would take replication down on any custom column type — the
    first one being `EncryptedText`, i.e. precisely when a credential is in
    flight.
    """
    try:
        return column_type.python_type  # type: ignore[no-any-return]
    except NotImplementedError:
        return None


def _coerce(spec: ReplicatedClass, payload: dict) -> dict:
    """Turn a wire payload back into column values for this model."""
    if spec.deserialize is not None:
        return spec.deserialize(payload)
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(spec.model)
    out: dict[str, Any] = {}
    for col in mapper.column_attrs:
        if col.key in spec.exclude or col.key not in payload:
            continue
        raw = payload[col.key]
        target = _column_python_type(col.expression.type) if raw is not None else None
        if raw is not None and target is uuid.UUID and isinstance(raw, str):
            raw = uuid.UUID(raw)
        elif raw is not None and target is datetime and isinstance(raw, str):
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        out[col.key] = raw
    return out


# --------------------------------------------------------------------------
# The outbox
# --------------------------------------------------------------------------


#: Set once `install_outbox_hooks` has attached its listeners to the global
#: Session class, so a second call cannot double every event.
_hooks_installed = False


def _pending_event(spec: ReplicatedClass, obj: Any, op: str, origin: str) -> dict | None:
    """The outbox row for one write, or None when the entity has no id yet."""
    entity_id = row_entity_id(spec, obj)
    if entity_id is None:
        return None
    return {
        "entity_class": spec.name,
        "entity_id": entity_id,
        "op": op,
        "occurred_at": datetime.now(UTC),
        "origin_node": origin,
    }


def install_outbox_hooks() -> None:
    """Emit an outbox row for every ORM write to a registered class.

    **Two hooks, because the two things this needs are true at different
    moments.** Neither point alone is sufficient, and using only one was the bug
    (#1173):

    - ``before_flush`` is the only place that knows *what is being written*.
      ``session.deleted`` is populated here, and ``is_modified`` still has the
      attribute history it needs — the flush consumes that history, so asking
      afterwards reports nothing as modified and every UPDATE goes unrecorded.
    - ``after_flush`` is the only place that knows *which row it was*. A pending
      INSERT has no primary key beforehand: almost every model takes its id from
      a column ``default=`` (``generate_uuid7``), evaluated during the flush. So
      ``row_entity_id`` returned None and the event was silently dropped —
      creating a workspace replicated **nothing**. The one class that appeared to
      work, ``api_tokens``, only did so because its creation path assigns the id
      itself, which is exactly why the gap looked like it did not exist.

    That failure is invisible from inside a single node: the row lands, the API
    returns 201, and the only symptom is a promoted follower that has never heard
    of the workspace. It survived CI because the tests here introspect this
    function's source rather than flushing a real row.

    So: decide in ``before_flush``, stash on the session, emit in ``after_flush``
    once the ids exist. The rows go in with a Core ``insert()`` rather than
    ``session.add`` — the ORM unit of work for this flush is already built, so a
    new ORM object would not be picked up, but Core SQL on the session's own
    connection joins the same transaction and commits or rolls back atomically
    with the write it describes.

    **Known gap, deliberate:** bulk ``update()``/``delete()`` statements bypass
    ORM events entirely and will not produce an event. Every current write path
    for a registered class goes through the ORM; the CI gate in slice 2 is what
    keeps that true.

    Calling this more than once is a no-op. The listeners attach to the global
    ``Session`` class, so a second call would register a second pair and every
    write would be recorded twice — a duplicate event is not harmful downstream
    (apply is an idempotent upsert) but it doubles the outbox and makes the
    "how far behind" figure a lie.
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True

    from sqlalchemy import event, insert

    from terrapod.config import settings

    _ensure_loaded()

    #: Key under which the pending (spec, obj, op) triples ride on session.info
    #: between the two hooks.
    stash = "_terrapod_replication_pending"

    @event.listens_for(Session, "before_flush")
    def _before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
        if not _BY_MODEL:
            return
        pending: list[tuple[ReplicatedClass, Any, str]] = session.info.setdefault(stash, [])
        for obj in session.new:
            spec = _BY_MODEL.get(type(obj))
            if spec is not None:
                pending.append((spec, obj, UPSERT))
        for obj in session.dirty:
            if not session.is_modified(obj, include_collections=False):
                continue
            spec = _BY_MODEL.get(type(obj))
            if spec is not None:
                pending.append((spec, obj, UPSERT))
        for obj in session.deleted:
            spec = _BY_MODEL.get(type(obj))
            if spec is not None:
                pending.append((spec, obj, DELETE))

    @event.listens_for(Session, "after_flush")
    def _after_flush(session: Session, _flush_context: Any) -> None:
        pending = session.info.pop(stash, None)
        if not pending:
            return
        origin = settings.ha.node_name
        rows = [
            row
            for spec, obj, op in pending
            if (row := _pending_event(spec, obj, op, origin)) is not None
        ]
        if rows:
            session.execute(insert(ReplicationEvent), rows)


# --------------------------------------------------------------------------
# Reading (leader side)
# --------------------------------------------------------------------------


@dataclass
class EventPage:
    events: list[dict] = field(default_factory=list)
    cursor: int = 0
    #: True when the requested cursor predates the retained window, so the
    #: caller has a gap it cannot close by replaying and must backfill.
    stale_cursor: bool = False
    #: The newest event id on this node, so the caller can say how far behind
    #: it is rather than only how long ago it last succeeded (#1165). One
    #: `max(id)` on an indexed primary key — cheap enough to send every cycle.
    latest_id: int = 0
    #: When the oldest event in THIS page was recorded — which, at the moment
    #: the page is built, is the oldest change the caller has not yet applied.
    #: Turns a count into a duration. None when the caller is already caught up.
    oldest_unapplied_at: datetime | None = None


async def read_events(db: AsyncSession, after: int, limit: int = 500) -> EventPage:
    """Return events after ``after``, oldest first."""
    oldest = await db.scalar(select(ReplicationEvent.id).order_by(ReplicationEvent.id).limit(1))
    # `after` is the last id consumed, so the next expected id is after+1. A
    # gap means events between the two were purged and are unrecoverable.
    stale = oldest is not None and after > 0 and oldest > after + 1
    rows = (
        (
            await db.execute(
                select(ReplicationEvent)
                .where(ReplicationEvent.id > after)
                .order_by(ReplicationEvent.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    latest_id = await db.scalar(select(func.max(ReplicationEvent.id))) or 0

    return EventPage(
        events=[
            {
                "id": r.id,
                "entity-class": r.entity_class,
                "entity-id": r.entity_id,
                "op": r.op,
                "occurred-at": _json_safe(r.occurred_at),
                "origin-node": r.origin_node,
            }
            for r in rows
        ],
        cursor=rows[-1].id if rows else after,
        stale_cursor=stale,
        latest_id=latest_id,
        oldest_unapplied_at=rows[0].occurred_at if rows else None,
    )


async def read_entity(db: AsyncSession, spec: ReplicatedClass, entity_id: str) -> dict | None:
    """Fetch one row's current state, or None if it no longer exists."""
    parts = decode_entity_id(spec, entity_id)
    if parts is None:
        return None
    try:
        obj = await db.scalar(select(spec.model).where(_pk_filter(spec, parts)))
    except ValueError:
        return None  # a component that will not coerce is not a row we have
    return serialize_row(spec, obj) if obj is not None else None


async def read_backfill(
    db: AsyncSession, spec: ReplicatedClass, after: str = "", limit: int = 200
) -> list[dict]:
    """A page of a class's rows, ordered by primary key so it is resumable."""
    cols = _pk_columns(spec)
    stmt = select(spec.model).order_by(*cols).limit(limit)
    if after:
        parts = decode_entity_id(spec, after)
        if parts is None:
            return []
        try:
            values = [_coerce_pk(col, part) for col, part in zip(cols, parts, strict=True)]
        except ValueError:
            return []
        # Row-value comparison so a composite key pages in the same order it is
        # sorted by; comparing only the first column would skip or repeat rows
        # that share it.
        stmt = stmt.where(tuple_(*cols) > tuple_(*values) if len(cols) > 1 else cols[0] > values[0])
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_row(spec, r) for r in rows]


# --------------------------------------------------------------------------
# Applying (follower side)
# --------------------------------------------------------------------------


async def apply_upsert(db: AsyncSession, spec: ReplicatedClass, payload: dict) -> None:
    """Insert or update a row from a peer, preserving its identity."""
    values = _coerce(spec, payload)
    if any(values.get(name) is None for name in spec.pk_attrs):
        return
    parts = [str(values[name]) for name in spec.pk_attrs]
    existing = await db.scalar(select(spec.model).where(_pk_filter(spec, parts)))
    if existing is None:
        db.add(spec.model(**values))
        return
    # The peer's row is authoritative — there is nothing to reconcile, because
    # a follower originates nothing (#1124).
    for key, value in values.items():
        if key not in spec.pk_attrs:
            setattr(existing, key, value)


async def reconcile_deletions(
    db: AsyncSession, spec: ReplicatedClass, seen_ids: set[str]
) -> list[str]:
    """Remove local rows of this class that the peer no longer has (#1115).

    Backfill upserts what the peer holds; without this it can never converge a
    **deletion**. That matters because a delete may have happened while this
    node was beyond the retained event window — the case backfill exists to
    recover from. The concrete failure is a revoked API token surviving on the
    follower and working again after a failover.

    Tombstones would be the obvious alternative and are worse: they need their
    own retention (so the window problem simply recurs), and they must be
    written on every delete path including bulk statements that bypass the ORM.
    The cheaper property is already true — everything replicated originates on
    the leader and a follower originates nothing, so **a row the follower holds
    and the peer does not is a row the peer deleted.**

    Returns the ids removed. The caller MUST only invoke this after a complete,
    error-free pass: a truncated `seen_ids` would read as mass deletion.
    """
    cols = _pk_columns(spec)
    rows = (await db.execute(select(*cols))).all()
    local = {encode_entity_id(spec, list(row)): list(row) for row in rows}
    extra = [row_id for row_id in local if row_id not in seen_ids]
    if not extra:
        return []

    # Delete by explicit key rather than a NOT IN over the whole class, so the
    # statement size tracks what is being removed, not how much there is.
    for chunk_start in range(0, len(extra), _DELETE_CHUNK):
        chunk = extra[chunk_start : chunk_start + _DELETE_CHUNK]
        await db.execute(
            delete(spec.model).where(
                or_(*[_pk_filter(spec, [str(v) for v in local[row_id]]) for row_id in chunk])
            )
        )

    # This is the one place replication removes data that was never deleted
    # locally. It must never be silent — an operator failing back needs to see
    # exactly what was discarded to reach convergence.
    logger.warning(
        "Backfill removed rows the peer no longer has",
        entity_class=spec.name,
        removed=len(extra),
        ids=extra[:20],
        truncated=len(extra) > 20,
    )
    return extra


async def apply_delete(db: AsyncSession, spec: ReplicatedClass, entity_id: str) -> None:
    parts = decode_entity_id(spec, entity_id)
    if parts is None:
        return
    try:
        await db.execute(delete(spec.model).where(_pk_filter(spec, parts)))
    except ValueError:
        return  # a component that will not coerce is not a row we have


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


async def purge_old_events(db: AsyncSession, retention_days: int) -> int:
    """Drop events beyond the retained window.

    Safe to do unconditionally because a follower that falls off the end
    detects the gap and backfills rather than silently skipping the missed
    rows — which is the whole reason the window can be bounded at all.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await db.execute(delete(ReplicationEvent).where(ReplicationEvent.occurred_at < cutoff))
    await db.commit()
    count = result.rowcount or 0
    if count:
        logger.info("Purged replication events", count=count, retention_days=retention_days)
    return count


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


@dataclass
class ReplicationStatus:
    """What this node can say about replication without asking the peer.

    Deliberately answerable locally: the status path stays fast, and it still
    works when the peer is the thing that has broken — which is precisely when
    an operator is looking at it.
    """

    #: Follower side: when the last pull completed, and whether any class is
    #: still mid-backfill. A node backfilling is NOT in sync, however recent
    #: its last cycle.
    last_sync_at: datetime | None = None
    backfilling: list[str] = field(default_factory=list)
    #: Leader side: how much margin the follower has before its cursor falls
    #: off the retained window and it has to backfill from scratch.
    events_retained: int = 0
    oldest_event_at: datetime | None = None
    #: Follower side: how far behind this node was at the last successful pull
    #: (#1165). None means unknown — never pulled, or a peer that does not
    #: report it. Deliberately distinct from 0, which means caught up.
    events_behind: int | None = None
    #: When the oldest change this node had not applied was recorded, as of that
    #: same pull. None when caught up (or unknown).
    oldest_unapplied_at: datetime | None = None


async def read_status(db: AsyncSession) -> ReplicationStatus:
    from terrapod.db.models import ReplicationCursor

    cursors = (await db.execute(select(ReplicationCursor))).scalars().all()
    stream = next((c for c in cursors if c.entity_class == EVENT_STREAM), None)

    behind: int | None = None
    if stream is not None and stream.peer_latest_position:
        # Clamped at zero: the peer's newest id is from the last pull, so a
        # local cursor that has since moved past it would otherwise produce a
        # negative "behind", which is not a thing.
        behind = max(0, int(stream.peer_latest_position) - int(stream.position or 0))

    return ReplicationStatus(
        last_sync_at=stream.updated_at if stream else None,
        events_behind=behind,
        oldest_unapplied_at=stream.oldest_unapplied_at if stream else None,
        backfilling=sorted(
            c.entity_class for c in cursors if c.backfilling and c.entity_class != EVENT_STREAM
        ),
        events_retained=await db.scalar(select(func.count()).select_from(ReplicationEvent)) or 0,
        oldest_event_at=await db.scalar(select(func.min(ReplicationEvent.occurred_at))),
    )
