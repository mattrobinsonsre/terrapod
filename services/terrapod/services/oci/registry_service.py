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

from sqlalchemy import func, select
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
    db: AsyncSession,
    repository: OCIRepository,
    name: str,
    manifest: OCIManifest,
    *,
    from_upstream: bool = False,
) -> OCITag:
    """Point a tag at a manifest, moving it if it already exists.

    Tags are mutable by design — ``latest`` moving is the whole point — so this
    updates in place rather than inserting, which also keeps the unique
    constraint on (repository, name) from turning a normal re-tag into a 500.

    ``from_upstream`` records that this tag's content came from a mirror fetch,
    by stamping ``revalidated_at``. It carries more weight than it looks: that
    timestamp is what separates a mirrored tag from one a user pushed, and only a
    mirrored tag is ever revalidated against upstream (#1425). A push into a
    mirror repository leaves it NULL and is therefore left alone — otherwise
    upstream's ``v1`` would quietly overwrite the ``v1`` someone deliberately
    pushed here.

    Stamping it at fetch time also starts the freshness window at the moment the
    content was actually confirmed, rather than at the next read.
    """
    result = await db.execute(
        select(OCITag).where(OCITag.repository_id == repository.id, OCITag.name == name)
    )
    tag = result.scalar_one_or_none()
    if tag is not None:
        tag.manifest_id = manifest.id
        if from_upstream:
            tag.revalidated_at = datetime.now(UTC)
        await db.flush()
        return tag

    tag = OCITag(
        repository_id=repository.id,
        name=name,
        manifest_id=manifest.id,
        revalidated_at=datetime.now(UTC) if from_upstream else None,
    )
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


async def get_tag(db: AsyncSession, repository: OCIRepository, name: str) -> OCITag | None:
    """The tag row itself, rather than the manifest it points at.

    `resolve_manifest` joins straight through to the manifest, which is what a
    pull needs. Revalidation needs the tag: when it was last confirmed against
    upstream lives there, not on the manifest, because several tags can point at
    one manifest and each is checked on its own schedule.
    """
    result = await db.execute(
        select(OCITag).where(OCITag.repository_id == repository.id, OCITag.name == name)
    )
    return result.scalar_one_or_none()


async def delete_tag(db: AsyncSession, repository: OCIRepository, name: str) -> bool:
    """Remove a tag, leaving the manifest it pointed at addressable by digest.

    That asymmetry is the spec's, and it is the right one: a tag is a name, and
    dropping a name should not destroy the thing named. The manifest becomes
    untagged, which is a state the operator can see and act on rather than a
    deletion they did not ask for.

    Returns False when there was no such tag, so the caller can 404 rather than
    reporting a success that deleted nothing.
    """
    result = await db.execute(
        select(OCITag).where(OCITag.repository_id == repository.id, OCITag.name == name)
    )
    tag = result.scalar_one_or_none()
    if tag is None:
        return False
    await db.delete(tag)
    await db.flush()
    return True


async def delete_manifest(db: AsyncSession, repository: OCIRepository, digest: str) -> bool:
    """Remove a manifest and any tags that pointed at it.

    The tags go because a tag pointing at a manifest that no longer exists is a
    pull that 404s from a name the registry still advertises — worse than the tag
    being gone.

    **Referrers are deliberately not cascaded.** A signature or SBOM attached via
    `subject` survives its subject's deletion and stays addressable by digest.
    Deleting an image should not silently destroy the record that it was ever
    signed — that record is evidence, and may exist nowhere else. It becomes an
    untagged manifest, which the untagged listing surfaces so an operator can
    remove it deliberately if they want to.

    No blob is touched here. Layers are shared, so what a manifest's removal makes
    reclaimable is decided by whether anything *else* still references each blob —
    which is the collector's job, on its own cycle, and not something a delete
    request can answer correctly on its own.
    """
    result = await db.execute(
        select(OCIManifest).where(
            OCIManifest.repository_id == repository.id, OCIManifest.digest == digest
        )
    )
    manifest = result.scalar_one_or_none()
    if manifest is None:
        return False

    # The tags go with it via `oci_tags.manifest_id ON DELETE CASCADE`, rather
    # than by iterating them here — one statement the database applies atomically,
    # with no window in which a tag points at a manifest that has already gone.
    await db.delete(manifest)
    await db.flush()
    return True


async def list_untagged_manifests(db: AsyncSession, repository: OCIRepository) -> list[OCIManifest]:
    """Manifests in this repository that no tag points at.

    The case that quietly fills a registry: re-pushing `:latest` leaves the
    previous manifest untagged and still holding its layers. Nothing collects it,
    because it is a manifest rather than an orphaned blob — and an operator cannot
    delete what they cannot see, so this is what makes that space accountable.

    Referrers appear here too, which is intended: an SBOM whose subject has been
    deleted is exactly something an operator should be shown.
    """
    tagged = select(OCITag.manifest_id).where(OCITag.repository_id == repository.id)
    result = await db.execute(
        select(OCIManifest)
        .where(OCIManifest.repository_id == repository.id, OCIManifest.id.not_in(tagged))
        .order_by(OCIManifest.created_at)
    )
    return list(result.scalars().all())


async def delete_repository(db: AsyncSession, repository: OCIRepository) -> dict[str, int]:
    """Remove a repository, its tags, its manifests and its blob links.

    What an operator actually reaches for when clearing out a project, and not in
    the distribution spec — which is why it lives on the native surface rather
    than under `/v2/`.

    Blob *rows* survive: they are content-addressed and shared across
    repositories, so deleting them here would take content another repository is
    still serving. Dropping this repository's links is what makes its blobs
    reclaimable, and only for those nothing else references.
    """
    # Counted before the delete, because after it there is nothing left to count —
    # every child FK is ON DELETE CASCADE, so removing the repository row takes its
    # tags, manifests and blob links with it in one statement. Deleting them
    # individually first would be the same outcome by a longer route, and would
    # leave a window where the repository still exists with its contents gone.
    counts = {}
    for label, model, column in (
        ("tags", OCITag, OCITag.repository_id),
        ("manifests", OCIManifest, OCIManifest.repository_id),
        ("blob_links", OCIRepositoryBlob, OCIRepositoryBlob.repository_id),
    ):
        result = await db.execute(
            select(func.count()).select_from(model).where(column == repository.id)
        )
        counts[label] = int(result.scalar_one())

    await db.delete(repository)
    await db.flush()
    return counts
