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

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from terrapod.db.models import ReplicationEvent

logger = structlog.get_logger(__name__)

UPSERT = "upsert"
DELETE = "delete"

#: Cursor row that tracks the shared event stream rather than a class backfill.
EVENT_STREAM = "*"


@dataclass(frozen=True)
class ReplicatedClass:
    """One entity class in the replication scope.

    ``monotonic_fields`` is the sharp edge. Most columns converge fine under
    last-write-wins, but a counter does not: both nodes increment
    ``agent_pool_tokens.use_count`` independently (a shared listener fleet
    re-joins against whichever node currently holds the DNS name), so a stale
    copy winning on timestamp would hand a spent token its budget back. Fields
    listed here never decrease — the merge takes the larger value, in both
    directions and during backfill alike. ``one_way_true_fields`` is the boolean
    equivalent: once ``is_revoked`` is true it can never be replicated back to
    false.
    """

    name: str
    model: type
    pk_attr: str = "id"
    #: Columns never sent (server-managed, or meaningless on the other node).
    exclude: frozenset[str] = frozenset()
    #: Counters that may only ever increase.
    monotonic_fields: frozenset[str] = frozenset()
    #: Booleans that may only ever go false -> true.
    one_way_true_fields: frozenset[str] = frozenset()
    #: Optional per-class overrides for entities the generic path cannot handle.
    serialize: Callable[[Any], dict] | None = None
    deserialize: Callable[[dict], dict] | None = None


_REGISTRY: dict[str, ReplicatedClass] = {}
#: Models to watch, resolved once so the flush hook stays a dict lookup.
_BY_MODEL: dict[type, ReplicatedClass] = {}


_loaded = False


def _ensure_loaded() -> None:
    """Import the registry module so the scope is populated.

    Registration happens as an import side effect, which means the scope is
    empty until something imports `replication_registry`. Relying on a caller to
    do that would make replication silently a no-op the day somebody tidies up
    what looks like an unused import — so every entry point resolves it here
    instead. The registry is a fixed module in this repo, not a plugin system,
    so there is nothing to discover and no ordering to get wrong.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
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
        target = col.expression.type.python_type if raw is not None else None
        if raw is not None and target is uuid.UUID and isinstance(raw, str):
            raw = uuid.UUID(raw)
        elif raw is not None and target is datetime and isinstance(raw, str):
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        out[col.key] = raw
    return out


# --------------------------------------------------------------------------
# The outbox
# --------------------------------------------------------------------------


def _record(session: Session, spec: ReplicatedClass, obj: Any, op: str, origin: str) -> None:
    entity_id = getattr(obj, spec.pk_attr, None)
    if entity_id is None:
        return
    session.add(
        ReplicationEvent(
            entity_class=spec.name,
            entity_id=str(entity_id),
            op=op,
            occurred_at=datetime.now(UTC),
            origin_node=origin,
        )
    )


def install_outbox_hooks() -> None:
    """Emit an outbox row for every ORM write to a registered class.

    Hooked on ``before_flush`` rather than ``after_flush`` because the outbox
    rows are themselves ORM objects: ``before_flush`` is the documented point at
    which the session may still be modified, and it is where ``session.deleted``
    is populated.

    **Known gap, deliberate:** bulk ``update()``/``delete()`` statements bypass
    ORM events entirely and will not produce an event. Every current write path
    for a registered class goes through the ORM; the CI gate in slice 2 is what
    keeps that true.
    """
    from sqlalchemy import event

    from terrapod.config import settings

    _ensure_loaded()

    @event.listens_for(Session, "before_flush")
    def _before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
        if not _BY_MODEL:
            return
        origin = settings.ha.node_name
        for obj in session.new:
            spec = _BY_MODEL.get(type(obj))
            if spec is not None:
                _record(session, spec, obj, UPSERT, origin)
        for obj in session.dirty:
            if not session.is_modified(obj, include_collections=False):
                continue
            spec = _BY_MODEL.get(type(obj))
            if spec is not None:
                _record(session, spec, obj, UPSERT, origin)
        for obj in session.deleted:
            spec = _BY_MODEL.get(type(obj))
            if spec is not None:
                _record(session, spec, obj, DELETE, origin)


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
    )


async def read_entity(db: AsyncSession, spec: ReplicatedClass, entity_id: str) -> dict | None:
    """Fetch one row's current state, or None if it no longer exists."""
    pk = getattr(spec.model, spec.pk_attr)
    value: Any = entity_id
    if pk.expression.type.python_type is uuid.UUID:
        try:
            value = uuid.UUID(entity_id)
        except ValueError:
            return None
    obj = await db.scalar(select(spec.model).where(pk == value))
    return serialize_row(spec, obj) if obj is not None else None


async def read_backfill(
    db: AsyncSession, spec: ReplicatedClass, after: str = "", limit: int = 200
) -> list[dict]:
    """A page of a class's rows, ordered by primary key so it is resumable."""
    pk = getattr(spec.model, spec.pk_attr)
    stmt = select(spec.model).order_by(pk).limit(limit)
    if after:
        value: Any = after
        if pk.expression.type.python_type is uuid.UUID:
            try:
                value = uuid.UUID(after)
            except ValueError:
                return []
        stmt = stmt.where(pk > value)
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_row(spec, r) for r in rows]


# --------------------------------------------------------------------------
# Applying (follower side)
# --------------------------------------------------------------------------


def _merge(spec: ReplicatedClass, existing: Any, values: dict) -> dict:
    """Apply the field-level rules that blanket last-write-wins gets wrong."""
    merged = dict(values)
    for name in spec.monotonic_fields:
        if name not in merged:
            continue
        incoming = merged[name] or 0
        current = getattr(existing, name, 0) or 0
        # Losing an increment here would hand a spent token its budget back,
        # so the larger value always wins regardless of which side is newer.
        merged[name] = max(incoming, current)
    for name in spec.one_way_true_fields:
        if name not in merged:
            continue
        if getattr(existing, name, False):
            merged[name] = True
    return merged


async def apply_upsert(db: AsyncSession, spec: ReplicatedClass, payload: dict) -> None:
    """Insert or update a row from a peer, preserving its identity."""
    values = _coerce(spec, payload)
    pk_value = values.get(spec.pk_attr)
    if pk_value is None:
        return
    pk = getattr(spec.model, spec.pk_attr)
    existing = await db.scalar(select(spec.model).where(pk == pk_value))
    if existing is None:
        db.add(spec.model(**values))
        return
    for key, value in _merge(spec, existing, values).items():
        if key != spec.pk_attr:
            setattr(existing, key, value)


async def apply_delete(db: AsyncSession, spec: ReplicatedClass, entity_id: str) -> None:
    pk = getattr(spec.model, spec.pk_attr)
    value: Any = entity_id
    if pk.expression.type.python_type is uuid.UUID:
        try:
            value = uuid.UUID(entity_id)
        except ValueError:
            return
    await db.execute(delete(spec.model).where(pk == value))


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
