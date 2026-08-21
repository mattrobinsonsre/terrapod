"""Blob upload sessions for the OCI registry (#1408).

The push half of the spec, and the part most likely to be got wrong in a way
that only shows up in production.

**Sessions live in Postgres and chunks in object storage — never in memory or
on local disk.** The API runs N replicas with no session affinity, so a chunked
push opens with `POST` on one replica and may deliver each `PATCH` to a
different one. In-process state would pass every test, work perfectly on a
single-replica dev stack, and fail the moment the deployment scales. That is
principle 11 applied to a new surface.

It also rules out the ephemeral PVC, which the provider registry uses for
streamed uploads: a PVC is per-pod, so a partial written by one replica is
invisible to the next. Chunks therefore go to object storage as individually
numbered objects and are concatenated on completion — no object store supports
append, so this is the only portable shape.

**Nothing is buffered whole.** A layer is hundreds of MB, so completion streams
chunk by chunk, hashing as it goes, and hands the same stream to the backend.
Reading a blob into memory to digest it would defeat rule 14 on the largest
objects Terrapod handles.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import OCIBlob, OCIRepository, OCIRepositoryBlob, OCIUploadSession, now_utc
from terrapod.logging_config import get_logger
from terrapod.services.oci.names import Digest
from terrapod.storage import keys
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: Sessions reaped per cycle. Bounded so a large backlog drains over several
#: cycles instead of one transaction holding every stale row at once.
_REAP_BATCH = 500

#: Streamed in 1 MiB reads when concatenating. Large enough that a 500 MB layer
#: is a few hundred iterations rather than tens of thousands, small enough that
#: the resident set stays flat regardless of blob size.
_STREAM_CHUNK = 1024 * 1024


async def open_session(db: AsyncSession, repository_name: str) -> OCIUploadSession:
    """Begin an upload. The returned id is what the client's `Location` points at."""
    session = OCIUploadSession(repository_name=repository_name, offset=0, chunk_count=0)
    db.add(session)
    await db.flush()
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> OCIUploadSession | None:
    result = await db.execute(select(OCIUploadSession).where(OCIUploadSession.id == session_id))
    return result.scalar_one_or_none()


async def append_chunk(
    db: AsyncSession,
    storage: ObjectStore,
    session: OCIUploadSession,
    chunks: AsyncIterator[bytes],
) -> int:
    """Append one chunk, returning the new total offset.

    The chunk is written under the session's *current* `chunk_count`, so a
    lexicographic listing of the prefix is also the concatenation order. That
    removes any second source of truth about ordering — every backend lists
    lexicographically, and the zero-padded sequence in the key makes that sort
    numerically correct.

    Both counters advance in the same transaction as the write is acknowledged,
    so a replica that dies mid-`PATCH` leaves the session exactly where it was
    rather than claiming bytes it never stored.
    """
    key = keys.oci_upload_chunk_key(str(session.id), session.chunk_count)

    written = 0

    async def _counting() -> AsyncIterator[bytes]:
        nonlocal written
        async for chunk in chunks:
            written += len(chunk)
            yield chunk

    await storage.put_stream(key, _counting())

    session.offset += written
    session.chunk_count += 1
    await db.flush()
    return session.offset


async def complete_session(
    db: AsyncSession,
    storage: ObjectStore,
    session: OCIUploadSession,
    repository: OCIRepository,
    expected: Digest,
) -> OCIBlob | None:
    """Concatenate, verify the digest, and register the blob.

    Returns ``None`` when the content does not hash to ``expected`` — the caller
    turns that into ``DIGEST_INVALID``. **This is the trust boundary**: the
    client asserts a digest and the registry believes only the bytes, because
    content-addressed storage whose addresses are taken on trust is not
    content-addressed at all.

    Streams twice rather than buffering: once to hash, once to store. Two passes
    over object storage costs less than holding half a gigabyte resident, and
    keeps the memory profile flat no matter how large the layer.
    """
    prefix = keys.oci_upload_prefix(str(session.id))
    # list_prefix guarantees lexicographic key order as part of its contract, and
    # the chunk sequence is zero-padded, so this listing *is* the concatenation
    # order. No re-sorting, and no second source of truth about ordering.
    chunk_keys = [entry.key for entry in await storage.list_prefix(prefix)]

    hasher = hashlib.new(expected.algorithm)
    size = 0
    for key in chunk_keys:
        async for data in storage.get_stream(key, chunk_size=_STREAM_CHUNK):
            hasher.update(data)
            size += len(data)

    if hasher.hexdigest() != expected.encoded:
        return None

    async def _concatenated() -> AsyncIterator[bytes]:
        for key in chunk_keys:
            async for data in storage.get_stream(key, chunk_size=_STREAM_CHUNK):
                yield data

    blob_key = keys.oci_blob_key(expected.storage_segment)
    await storage.put_stream(blob_key, _concatenated())

    blob = await _upsert_blob(db, str(expected), size, blob_key)
    await link_blob(db, repository, blob)

    await discard_session(db, storage, session)
    return blob


async def _upsert_blob(db: AsyncSession, digest: str, size: int, storage_key: str) -> OCIBlob:
    """Register the blob, tolerating one that already exists.

    Two repositories pushing identical content — a shared base layer, most
    obviously — is normal rather than exceptional, and the bytes are identical
    by definition because the digest matched. So an existing row is reused
    rather than treated as a conflict.
    """
    result = await db.execute(select(OCIBlob).where(OCIBlob.digest == digest))
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    blob = OCIBlob(digest=digest, size=size, storage_key=storage_key)
    db.add(blob)
    await db.flush()
    return blob


async def link_blob(db: AsyncSession, repository: OCIRepository, blob: OCIBlob) -> None:
    """Grant a repository the right to serve a blob.

    Idempotent: re-pushing content a repository already holds is a no-op rather
    than a unique-constraint violation, which is what a client retrying an
    interrupted push will do.
    """
    result = await db.execute(
        select(OCIRepositoryBlob).where(
            OCIRepositoryBlob.repository_id == repository.id,
            OCIRepositoryBlob.blob_id == blob.id,
        )
    )
    if result.scalar_one_or_none() is not None:
        return
    db.add(OCIRepositoryBlob(repository_id=repository.id, blob_id=blob.id))
    await db.flush()


async def mount_blob(
    db: AsyncSession, source: OCIRepository, target: OCIRepository, digest: str
) -> OCIBlob | None:
    """Cross-repository mount: link an existing blob instead of re-uploading it.

    The spec's optimisation for the common case of a shared base layer, and the
    reason blobs are stored globally rather than per repository. Returns
    ``None`` when the source does not hold the blob, which the caller turns into
    an ordinary upload session — the spec requires the fallback, because a
    client must not have to know in advance whether a mount will succeed.
    """
    result = await db.execute(
        select(OCIBlob)
        .join(OCIRepositoryBlob, OCIRepositoryBlob.blob_id == OCIBlob.id)
        .where(OCIRepositoryBlob.repository_id == source.id, OCIBlob.digest == digest)
    )
    blob = result.scalar_one_or_none()
    if blob is None:
        return None
    await link_blob(db, target, blob)
    return blob


async def discard_session(
    db: AsyncSession, storage: ObjectStore, session: OCIUploadSession
) -> None:
    """Delete a session and its chunks.

    Best-effort on the storage side: a chunk that fails to delete is orphaned
    rather than allowed to fail the push, because the client has already been
    told the blob is stored. Orphans are what the session reaper exists for.
    """
    for entry in await storage.list_prefix(keys.oci_upload_prefix(str(session.id))):
        try:
            await storage.delete(entry.key)
        except Exception:  # noqa: BLE001 — see docstring
            pass
    await db.delete(session)
    await db.flush()


async def reap_abandoned_sessions() -> int:
    """Delete upload sessions that have sat untouched past the timeout.

    The spec asks a server to "eventually timeout unfinished uploads", and
    without this an abandoned push leaks permanently: the session row stays, and
    every chunk it wrote stays in object storage with nothing referencing it. A
    client only needs push access to repeat that until the disk is full, so this
    is a availability control as much as it is housekeeping.

    Registered as a periodic task on the distributed scheduler, so exactly one
    replica runs a cycle — never `asyncio.create_task` (principle 11).

    Rows are claimed with ``FOR UPDATE SKIP LOCKED`` — the same primitive the run
    dispatcher uses — so two overlapping cycles divide the backlog rather than
    contend over it. The scheduler's mutual exclusion is a claim, not a lock, and
    a cycle that overran can overlap the next one, so this path has to be correct
    under concurrency in its own right.

    Returns the number of sessions reaped, for the caller's log line.
    """
    from datetime import timedelta

    from terrapod.config import settings
    from terrapod.db.session import get_db_session
    from terrapod.storage import get_storage

    cutoff = now_utc() - timedelta(hours=settings.registry.oci.upload_session_timeout_hours)
    storage = get_storage()
    reaped = 0

    async with get_db_session() as db:
        stale = (
            (
                await db.execute(
                    select(OCIUploadSession)
                    .where(OCIUploadSession.updated_at < cutoff)
                    # Bounded per cycle: a large backlog is drained over several
                    # cycles rather than in one long transaction holding rows.
                    .limit(_REAP_BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for session in stale:
            await discard_session(db, storage, session)
            reaped += 1

    if reaped:
        logger.info("Reaped abandoned OCI upload sessions", count=reaped)
    return reaped
