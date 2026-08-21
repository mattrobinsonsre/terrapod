"""Read-side queries for the OCI registry (#1408).

Kept separate from the router so the lookups can be tested without a framework,
and so the router stays a thin translation between HTTP and these functions.

One rule runs through all of it: **a blob existing is not permission to serve
it from an arbitrary repository.** Blobs are global and content-addressed, so
every blob lookup goes through the repository link table. Skipping that would
let anyone who learns a digest pull private layers by inventing a repository
name they do have access to.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import OCIBlob, OCIManifest, OCIRepository, OCIRepositoryBlob, OCITag
from terrapod.services.oci.names import Reference


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
        return result.scalar_one_or_none()

    # Tag → manifest, joined rather than fetched in two round trips because a
    # manifest GET is on the hot path of every image pull.
    result = await db.execute(
        select(OCIManifest)
        .join(OCITag, OCITag.manifest_id == OCIManifest.id)
        .where(OCITag.repository_id == repository.id, OCITag.name == reference.tag)
    )
    return result.scalar_one_or_none()


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
    return result.scalar_one_or_none()


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
