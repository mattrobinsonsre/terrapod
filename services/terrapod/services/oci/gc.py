"""Reclaiming storage in the OCI registry (#1419).

The registry could not free space at all. Nothing expired, and a delete endpoint
would not have helped: deleting a manifest reclaims nothing on its own, because
its layers are usually shared with other images. Reclaiming space means finding
blobs that no manifest references any more — mark and sweep.

**Two kinds of content, and the distinction is the whole design.**

*Pushed* content is not re-fetchable. An operator's execution-environment image
exists only here, so expiring it by age would destroy it permanently; a registry
that silently eats the images you pushed to it is not a registry. Pushed
manifests are therefore **never** expired by age — only their genuinely
unreferenced blobs are ever collected, and a blob is unreferenced only when no
manifest points at it.

*Mirrored* content came from an upstream and can be fetched again, exactly like
the provider and binary caches, so it expires on last access — and not at all on
a sealed node, where re-fetching is precisely what is forbidden.

**The dangerous case is a push in flight.** Blobs are uploaded *before* the
manifest that references them, so a sweep landing between the two deletes the
layers of a push that is still happening; the client then fails its manifest PUT
with `MANIFEST_BLOB_UNKNOWN` for content it just successfully uploaded. Docker's
own registry answers this by requiring read-only mode during collection. A grace
period is better: a blob younger than `gc_grace_hours` is never a candidate, so an
in-flight push cannot be one. It costs nothing, needs no downtime, and errs
toward keeping bytes — which is the right way for a garbage collector to be
wrong.

**Signatures are not garbage.** A referrer — a signature, an SBOM, an attestation
— usually carries no tag, so a naive "untagged means unreachable" sweep destroys
provenance while leaving the image it describes. Referrers of a reachable subject
are roots here, which matters more for Terrapod than for most registries:
provenance that travels with the content is the reason an air-gapped estate can
check anything at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import (
    OCIBlob,
    OCIManifest,
    OCIRepository,
    OCIRepositoryBlob,
    OCITag,
)
from terrapod.logging_config import get_logger
from terrapod.services.oci.registry_service import referenced_digests
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: Manifests read per mark pass. Bounded so a very large repository does not hold
#: one enormous transaction open.
_BATCH = 500


@dataclass
class SweepResult:
    """What a cycle did, for the log line and the metrics."""

    blobs_deleted: int = 0
    bytes_reclaimed: int = 0
    manifests_expired: int = 0
    repositories: int = 0
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)


def _config():
    return settings.registry.oci.gc


async def _manifest_document(storage: ObjectStore, manifest: OCIManifest) -> dict | None:
    """A manifest's parsed body, or None if it cannot be read.

    An unreadable manifest makes the mark phase incomplete, and an incomplete
    mark phase deletes live data. Callers treat None as a reason to abandon the
    repository rather than to carry on with a partial reachability set.
    """
    try:
        raw = await storage.get(manifest.storage_key)
    except Exception:
        logger.warning(
            "Manifest unreadable during GC; skipping its repository",
            digest=manifest.digest,
            key=manifest.storage_key,
        )
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        logger.warning("Manifest is not JSON during GC", digest=manifest.digest)
        return None
    return document if isinstance(document, dict) else None


async def _reachable_digests(
    db: AsyncSession, storage: ObjectStore, repository: OCIRepository
) -> set[str] | None:
    """Every digest reachable from this repository's roots, or None if unsure.

    Roots are the tagged manifests, plus every referrer of a manifest already
    reached — a signature carries no tag of its own and would otherwise look like
    garbage.

    Returns None when any manifest could not be read. **That is the safety
    property**: a partial reachability set marks live blobs as unreferenced, so
    the only correct response to not knowing is to collect nothing here.
    """
    tagged = await db.execute(
        select(OCIManifest)
        .join(OCITag, OCITag.manifest_id == OCIManifest.id)
        .where(OCIManifest.repository_id == repository.id)
    )
    frontier = list({manifest.id: manifest for manifest in tagged.scalars().all()}.values())

    reachable: set[str] = set()
    seen_manifests: set[str] = set()

    while frontier:
        manifest = frontier.pop()
        if manifest.digest in seen_manifests:
            continue
        seen_manifests.add(manifest.digest)
        reachable.add(manifest.digest)

        document = await _manifest_document(storage, manifest)
        if document is None:
            return None

        for digest in referenced_digests(document):
            reachable.add(digest)

        # An index's entries are manifests; follow them so their layers are
        # marked too. Walking only `layers` would collect every architecture of
        # a multi-arch image.
        children = await db.execute(
            select(OCIManifest).where(
                OCIManifest.repository_id == repository.id,
                OCIManifest.digest.in_(referenced_digests(document)),
            )
        )
        frontier.extend(children.scalars().all())

        # Referrers of anything reached are reached: destroying an image's
        # signature while keeping the image is worse than keeping both.
        referrers = await db.execute(
            select(OCIManifest).where(
                OCIManifest.repository_id == repository.id,
                OCIManifest.subject_digest == manifest.digest,
            )
        )
        frontier.extend(referrers.scalars().all())

    return reachable


async def _expire_mirrored_manifests(
    db: AsyncSession, repository: OCIRepository, retention_days: int
) -> int:
    """Drop mirrored manifests not accessed within the retention window.

    Only mirrors. A pushed manifest exists nowhere else, so age is never a reason
    to delete it — the operator's own images are not a cache.

    Untagged is not the test either: clients legitimately pull by digest, so an
    untagged manifest may be in daily use. Last access is the honest signal, and
    it is the same one the provider and binary caches use.
    """
    if repository.upstream is None or retention_days <= 0:
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    stale = await db.execute(
        select(OCIManifest)
        .where(
            OCIManifest.repository_id == repository.id,
            OCIManifest.last_accessed_at < cutoff,
        )
        .limit(_BATCH)
    )
    expired = 0
    for manifest in stale.scalars().all():
        # A tag still pointing here means someone can still ask for it by name.
        # The tag goes with the manifest; the blobs follow in the sweep below
        # once nothing references them.
        await db.execute(delete(OCITag).where(OCITag.manifest_id == manifest.id))
        await db.delete(manifest)
        expired += 1
    if expired:
        await db.flush()
    return expired


async def _sweep_repository(
    db: AsyncSession, storage: ObjectStore, repository: OCIRepository, grace: timedelta
) -> SweepResult:
    """Unlink this repository's unreferenced blobs. Returns what it did."""
    result = SweepResult(repositories=1)

    reachable = await _reachable_digests(db, storage, repository)
    if reachable is None:
        result.errors.append(f"{repository.name}: reachability incomplete, collected nothing")
        return result

    cutoff = datetime.now(UTC) - grace
    linked = await db.execute(
        select(OCIRepositoryBlob, OCIBlob)
        .join(OCIBlob, OCIRepositoryBlob.blob_id == OCIBlob.id)
        .where(OCIRepositoryBlob.repository_id == repository.id)
    )

    for link, blob in linked.all():
        if blob.digest in reachable:
            continue
        # The in-flight push guard. Blobs land before the manifest that
        # references them, so anything recent is presumed to belong to a push
        # that has not finished rather than to no one.
        if blob.cached_at > cutoff:
            continue
        await db.delete(link)
        result.blobs_deleted += 1
        result.bytes_reclaimed += blob.size or 0

    await db.flush()
    return result


async def _delete_orphan_blobs(db: AsyncSession, storage: ObjectStore) -> tuple[int, int]:
    """Remove blobs no repository links to any more.

    Content is addressed globally and linked per repository, so unlinking is what
    makes a blob collectable and this is where the bytes actually go. Deleting
    the object before the row would leave a row promising content that is not
    there; the reverse merely leaves an object nothing points at, which the next
    cycle will not even see. Rows first is the wrong order — so: object, then row,
    and a failed object delete leaves the row for the next attempt.
    """
    orphans = await db.execute(
        select(OCIBlob)
        .outerjoin(OCIRepositoryBlob, OCIRepositoryBlob.blob_id == OCIBlob.id)
        .where(OCIRepositoryBlob.id.is_(None))
        .limit(_BATCH)
    )

    deleted = 0
    reclaimed = 0
    for blob in orphans.scalars().all():
        try:
            await storage.delete(blob.storage_key)
        except Exception:
            logger.warning(
                "Could not delete orphaned blob object; leaving the row for retry",
                digest=blob.digest,
                key=blob.storage_key,
            )
            continue
        reclaimed += blob.size or 0
        await db.delete(blob)
        deleted += 1

    if deleted:
        await db.flush()
    return deleted, reclaimed


async def collect() -> SweepResult:
    """One garbage-collection cycle. Registered on the distributed scheduler.

    Never `asyncio.create_task`: the API is multi-replica and this deletes data,
    so exactly one replica may run a cycle (principle 11).
    """
    from terrapod.db.session import get_db_session
    from terrapod.storage import get_storage

    cfg = _config()
    if not cfg.enabled:
        return SweepResult(skipped_reason="registry.oci.gc.enabled is false")

    storage = get_storage()
    grace = timedelta(hours=cfg.grace_hours)
    retention = settings.artifact_retention.oci_mirror_retention_days
    # On a sealed node nothing can be re-fetched, so expiring a mirror loses it
    # exactly as permanently as expiring a pushed image would.
    if settings.registry.cache_only:
        retention = 0

    total = SweepResult()
    async with get_db_session() as db:
        repositories = (await db.execute(select(OCIRepository))).scalars().all()
        for repository in repositories:
            total.manifests_expired += await _expire_mirrored_manifests(db, repository, retention)
            swept = await _sweep_repository(db, storage, repository, grace)
            total.blobs_deleted += swept.blobs_deleted
            total.bytes_reclaimed += swept.bytes_reclaimed
            total.repositories += swept.repositories
            total.errors.extend(swept.errors)

        deleted, reclaimed = await _delete_orphan_blobs(db, storage)

    # The unlink count is per repository and the delete count is global, so the
    # bytes actually reclaimed are the ones from deleted objects.
    total.blobs_deleted = deleted
    total.bytes_reclaimed = reclaimed

    if total.blobs_deleted or total.manifests_expired or total.errors:
        logger.info(
            "OCI garbage collection",
            blobs_deleted=total.blobs_deleted,
            bytes_reclaimed=total.bytes_reclaimed,
            manifests_expired=total.manifests_expired,
            repositories=total.repositories,
            errors=len(total.errors),
        )
    return total


async def registry_size(db: AsyncSession) -> tuple[int, int]:
    """(blob count, total bytes) — what an operator wants after a collection."""
    count = await db.scalar(select(func.count()).select_from(OCIBlob)) or 0
    size = await db.scalar(select(func.coalesce(func.sum(OCIBlob.size), 0))) or 0
    return count, size
