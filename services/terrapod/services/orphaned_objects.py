"""Recording and sweeping objects that no row points at any more (#2599).

Every retention sweep in the tree is **row-driven**: it walks rows and deletes
the object each one names. That is correct while a row's key never changes, and
#2599 made it change -- replacing a pulled artifact with an operator upload
moves the row from `cache/` to `uploads/`, leaving the `cache/` object
referenced by nothing and therefore invisible to all of them.

Recorded rather than deleted inline. The upload paths change the row, flush, and
only then stream the bytes, deliberately, so a failed put rolls back cleanly --
but the caller's transaction can still roll back *after* a successful put, which
reverts the row to the old key. Deleting the old object at that point would be
data loss rather than a leak. A row written here lives in the same transaction
as the key change, so a rollback takes it too, which is the honest outcome: the
old object is referenced again.
"""

from __future__ import annotations

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    CachedBinary,
    CachedPackageFile,
    CachedProviderPackage,
    OrphanedObject,
)
from terrapod.storage.protocol import ObjectStore

logger = structlog.get_logger(__name__)

#: Everything that names an object by a recorded key. The sweep re-checks each
#: of these before deleting, so a stale row cannot destroy a live object.
#:
#: This is a LIST, not a derivation, and its own comment claimed otherwise until
#: the beta.6 review -- "derived from the models rather than listed in prose, so
#: a fourth table with a `storage_key` is covered the day it is added". It is
#: not, and six models carry a `storage_key`: the three here, `OrphanedObject`
#: itself, and `OCIManifest`/`OCIBlob`.
#:
#: It cannot be naively derived, which is why the claim was tempting and why the
#: honest version is a list with a gate over it. `OrphanedObject` is the sweep's
#: OWN table, so including it would make every key "still referenced" and
#: disable the sweep entirely -- the one entry that must never be added.
#:
#: The two OCI tables are a DELIBERATE omission and a latent hazard rather than
#: a live one: all three callers of `record()` above name a key belonging to one
#: of the three tables here, so no OCI key reaches the sweep today. The day one
#: does, this tuple must gain them first, or the sweep deletes a live blob out
#: of the container registry. `test_orphan_sweep_key_holders.py` is what forces
#: that decision rather than leaving it to whoever adds the fourth recorder.
_KEY_HOLDERS = (CachedPackageFile, CachedBinary, CachedProviderPackage)


async def record(db: AsyncSession, storage_key: str, *, reason: str) -> None:
    """Note that nothing points at `storage_key` any more.

    Idempotent on the key: re-uploading over the same artifact twice orphans the
    same `cache/` object, and the second attempt must not fail the upload. The
    first record's `reason` and `recorded_at` are kept, because the first is
    when it actually became unreferenced.

    Does NOT flush. The caller's transaction decides, which is the whole point.
    """
    if not storage_key:
        return
    await db.execute(
        pg_insert(OrphanedObject)
        .values(storage_key=storage_key, reason=reason[:255])
        .on_conflict_do_nothing(index_elements=["storage_key"])
    )


async def _still_referenced(db: AsyncSession, storage_key: str) -> bool:
    """Whether any row names this key.

    The guard that makes the sweep safe against its own records going stale: a
    key can legitimately come back into use -- an operator uploads over an
    artifact, then re-uploads the original bytes, and the pull-through key is
    live again. Deleting it then would destroy a served object.
    """
    for model in _KEY_HOLDERS:
        found = await db.scalar(
            sa.select(sa.literal(1)).where(model.storage_key == storage_key).limit(1)
        )
        if found:
            return True
    return False


async def sweep(db: AsyncSession, storage: ObjectStore, *, limit: int = 500) -> tuple[int, int]:
    """Delete the objects nothing points at. Returns (deleted, skipped).

    Bounded per pass so one enormous backlog cannot hold a transaction open for
    minutes; the next pass takes the rest.
    """
    rows = (
        (
            await db.execute(
                sa.select(OrphanedObject).order_by(OrphanedObject.recorded_at).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    deleted = skipped = 0
    for row in rows:
        if await _still_referenced(db, row.storage_key):
            # Back in use. Drop the record, keep the object.
            await db.delete(row)
            skipped += 1
            logger.info(
                "An orphaned object came back into use; keeping it",
                storage_key=row.storage_key,
            )
            continue
        try:
            await storage.delete(row.storage_key)
        except Exception:
            # Left recorded on purpose: a transient storage failure should be
            # retried next pass, not forgotten. Never fatal -- this is a
            # space-reclaiming sweep, and failing the cycle would stop the
            # others behind it.
            logger.warning(
                "Could not delete an orphaned object; will retry",
                storage_key=row.storage_key,
                exc_info=True,
            )
            continue
        await db.delete(row)
        deleted += 1

    if deleted or skipped:
        logger.info("Swept orphaned objects", deleted=deleted, skipped=skipped)
    return deleted, skipped
