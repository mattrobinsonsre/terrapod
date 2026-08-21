"""Read-side queries for the OCI registry (#1408).

Kept separate from the router so the lookups can be tested without a framework,
and so the router stays a thin translation between HTTP and these functions.

One rule runs through all of it: **a blob existing is not permission to serve
it from an arbitrary repository.** Blobs are global and content-addressed, so
every blob lookup goes through the repository link table. Skipping that would
let anyone who learns a digest pull private layers by inventing a repository
name they do have access to.

Reads touch ``last_accessed_at``, because ``artifact_retention_service`` reaps
cache entries on *access* rather than write time — deliberately, since expiring
a heavily used artifact merely for being old evicts it and immediately re-fetches
it. Without the touch, a constantly pulled image would look untouched and be
reaped on schedule. Note the volume differs from the other caches: they are read
a few times per run, where a blob is read once per layer per pull, so this is a
place to look first if write load ever becomes a question.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import OCIBlob, OCIManifest, OCIRepository, OCIRepositoryBlob, OCITag
from terrapod.services.oci.names import Reference

#: Layer types the spec expects to be absent from the registry: they carry
#: `urls` pointing elsewhere, because redistributing them is not permitted.
NON_DISTRIBUTABLE_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.layer.nondistributable.v1.tar",
        "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
        "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd",
        "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
    }
)


def _touch(entity):
    """Mark an entity as accessed, so retention sees it as live.

    Returns its argument so call sites stay one line. A no-op on ``None``,
    which keeps every caller from having to guard.
    """
    if entity is not None:
        entity.last_accessed_at = datetime.now(UTC)
    return entity


async def get_repository(db: AsyncSession, name: str) -> OCIRepository | None:
    """Look up a repository by its full name."""
    result = await db.execute(select(OCIRepository).where(OCIRepository.name == name))
    return result.scalar_one_or_none()


async def resolve_manifest(
    db: AsyncSession, repository: OCIRepository, reference: Reference
) -> OCIManifest | None:
    """Resolve a manifest by tag or by digest.

    The spec puts both in one path position; :class:`Reference` has already
    decided which this is, so the two lookups stay explicit rather than being
    guessed at again here.
    """
    if reference.is_digest:
        result = await db.execute(
            select(OCIManifest).where(
                OCIManifest.repository_id == repository.id,
                OCIManifest.digest == str(reference.digest),
            )
        )
        return _touch(result.scalar_one_or_none())

    # Tag → manifest, joined rather than fetched in two round trips because a
    # manifest GET is on the hot path of every image pull.
    result = await db.execute(
        select(OCIManifest)
        .join(OCITag, OCITag.manifest_id == OCIManifest.id)
        .where(OCITag.repository_id == repository.id, OCITag.name == reference.tag)
    )
    return _touch(result.scalar_one_or_none())


async def get_repository_blob(
    db: AsyncSession, repository: OCIRepository, digest: str
) -> OCIBlob | None:
    """Fetch a blob **only if this repository references it**.

    The join is the access check, not an optimisation. A blob row exists once
    any repository holds it; serving it requires an edge from *this* one.
    """
    result = await db.execute(
        select(OCIBlob)
        .join(OCIRepositoryBlob, OCIRepositoryBlob.blob_id == OCIBlob.id)
        .where(OCIRepositoryBlob.repository_id == repository.id, OCIBlob.digest == digest)
    )
    return _touch(result.scalar_one_or_none())


async def list_tags(
    db: AsyncSession,
    repository: OCIRepository,
    *,
    limit: int | None = None,
    last: str | None = None,
) -> list[str]:
    """Tags in a repository, lexically ordered.

    Ordering is not cosmetic: the spec's pagination is cursor-based on the tag
    name (``last=``), which only works over a stable total order.
    """
    stmt = select(OCITag.name).where(OCITag.repository_id == repository.id)
    if last is not None:
        stmt = stmt.where(OCITag.name > last)
    stmt = stmt.order_by(OCITag.name)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def referenced_digests(document: dict) -> list[str]:
    """Every digest a manifest or manifest list points at.

    Handles both shapes because a multi-arch push sends an index whose entries
    are *manifests*, not blobs — walking only ``layers`` would validate a
    single-architecture image and wave the interesting case straight through.

    Deliberately tolerant of shape: a malformed entry is skipped rather than
    raising, because this feeds a validation check that reports its own error,
    and a `TypeError` escaping here would surface to the client as a 500 where
    ``MANIFEST_INVALID`` is the honest answer.
    """
    digests: list[str] = []

    config = document.get("config")
    if isinstance(config, dict) and isinstance(config.get("digest"), str):
        digests.append(config["digest"])

    for key in ("layers", "manifests"):
        entries = document.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("digest"), str):
                continue
            # Non-distributable (foreign) layers are referenced by digest but
            # deliberately NOT uploaded — they are fetched from the `urls` in the
            # descriptor, typically because their licence forbids redistribution.
            # Demanding their presence would reject every Windows base image.
            if entry.get("mediaType") in NON_DISTRIBUTABLE_MEDIA_TYPES:
                continue
            digests.append(entry["digest"])

    return digests


async def missing_referenced_blobs(
    db: AsyncSession, repository: OCIRepository, document: dict
) -> list[str]:
    """Which of a manifest's referents this repository does not hold.

    A manifest list's entries are other *manifests*, so both tables are
    consulted — checking blobs alone would reject every multi-arch push.
    """
    referenced = referenced_digests(document)
    if not referenced:
        return []

    blob_rows = await db.execute(
        select(OCIBlob.digest)
        .join(OCIRepositoryBlob, OCIRepositoryBlob.blob_id == OCIBlob.id)
        .where(OCIRepositoryBlob.repository_id == repository.id, OCIBlob.digest.in_(referenced))
    )
    manifest_rows = await db.execute(
        select(OCIManifest.digest).where(
            OCIManifest.repository_id == repository.id, OCIManifest.digest.in_(referenced)
        )
    )
    present = set(blob_rows.scalars().all()) | set(manifest_rows.scalars().all())
    return [digest for digest in referenced if digest not in present]


async def store_manifest(
    db: AsyncSession,
    storage,
    repository: OCIRepository,
    digest: str,
    media_type: str,
    body: bytes,
) -> OCIManifest:
    """Persist a manifest, tolerating a re-push of one already held.

    Idempotent because a client retrying an interrupted push re-sends the
    manifest, and because the same image may legitimately be pushed twice. The
    bytes are identical by definition — the digest is computed from them — so an
    existing row is returned rather than treated as a conflict.
    """
    existing = await db.execute(
        select(OCIManifest).where(
            OCIManifest.repository_id == repository.id, OCIManifest.digest == digest
        )
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return found

    from terrapod.services.oci.names import parse_digest
    from terrapod.storage import keys

    storage_key = keys.oci_manifest_key(parse_digest(digest).storage_segment)
    await storage.put(storage_key, body, content_type=media_type)

    subject_digest, artifact_type, annotations = referrer_metadata(body)

    manifest = OCIManifest(
        repository_id=repository.id,
        digest=digest,
        media_type=media_type,
        size=len(body),
        storage_key=storage_key,
        subject_digest=subject_digest,
        artifact_type=artifact_type,
        annotations=annotations,
    )
    db.add(manifest)
    await db.flush()
    return manifest


async def set_tag(
    db: AsyncSession, repository: OCIRepository, name: str, manifest: OCIManifest
) -> OCITag:
    """Point a tag at a manifest, moving it if it already exists.

    Tags are mutable by design — ``latest`` moving is the whole point — so this
    updates in place rather than inserting, which also keeps the unique
    constraint on (repository, name) from turning a normal re-tag into a 500.
    """
    result = await db.execute(
        select(OCITag).where(OCITag.repository_id == repository.id, OCITag.name == name)
    )
    tag = result.scalar_one_or_none()
    if tag is not None:
        tag.manifest_id = manifest.id
        await db.flush()
        return tag

    tag = OCITag(repository_id=repository.id, name=name, manifest_id=manifest.id)
    db.add(tag)
    await db.flush()
    return tag


def referrer_metadata(body: bytes) -> tuple[str | None, str | None, dict | None]:
    """Pull the referrers fields out of a manifest body.

    Denormalised at write time so the referrers API is an indexed lookup rather
    than a scan that parses every manifest in the repository.

    `artifactType` falls back to `config.mediaType`, which the spec directs for
    an image manifest that does not declare one — and which is how most tools
    actually mark an attachment's kind today, so omitting the fallback leaves
    the filter matching almost nothing.

    Never raises: a manifest whose shape is unexpected simply has no referrer
    metadata, and refusing the push over it would reject valid content the
    registry is otherwise happy to store.
    """
    try:
        document = json.loads(body)
    except ValueError:
        return None, None, None
    if not isinstance(document, dict):
        return None, None, None

    subject = document.get("subject")
    subject_digest = None
    if isinstance(subject, dict) and isinstance(subject.get("digest"), str):
        subject_digest = subject["digest"]

    artifact_type = document.get("artifactType")
    if not isinstance(artifact_type, str):
        config = document.get("config")
        artifact_type = config.get("mediaType") if isinstance(config, dict) else None
        if not isinstance(artifact_type, str):
            artifact_type = None

    annotations = document.get("annotations")
    if not isinstance(annotations, dict):
        annotations = None

    return subject_digest, artifact_type, annotations


async def list_referrers(
    db: AsyncSession,
    repository: OCIRepository,
    subject_digest: str,
    artifact_type: str | None = None,
) -> list[OCIManifest]:
    """Manifests in this repository attached to *subject_digest*.

    Ordered by creation so a client paging through attachments sees a stable
    sequence rather than whatever order the planner returns.
    """
    query = select(OCIManifest).where(
        OCIManifest.repository_id == repository.id,
        OCIManifest.subject_digest == subject_digest,
    )
    if artifact_type is not None:
        query = query.where(OCIManifest.artifact_type == artifact_type)
    result = await db.execute(query.order_by(OCIManifest.created_at))
    return list(result.scalars().all())
