"""Admin list, totals and eviction for the two caches that had none (#2474).

The CLI-binary and provider-binary caches have had admin routes since they
existed; they stay in `binary_cache.py`, where they are, because moving them
would be churn with no reader. These are the other two.

**The totals are computed over the whole cache, not over a page.** A total summed
from a listing is wrong by however much the page left out, and wrong in the
reassuring direction -- an operator looking for what is filling a bucket would be
shown a number smaller than the problem. For the package cache that is a `GROUP
BY` in the database; for the archive cache there is nothing to group, so the walk
carries a ceiling and the response says when it hit it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.api.pagination import paginate
from terrapod.api.serialization import rfc3339
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import cache_inventory
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

router = APIRouter(tags=["cache-admin"])


class PackageEvictRequest(BaseModel):
    """What a bulk package eviction targets.

    `extra="forbid"` so a misspelled filter is a 422 rather than an eviction
    wider than the caller meant -- which on this endpoint is the difference
    between clearing one ecosystem and clearing the cache.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ecosystem: str = Field(default="", description="galaxy | pypi | npm | go | nuget | pulumi")
    older_than_days: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Evict artifacts not ACCESSED in this many days. Access-based, never "
            "write-based: an artifact every run pulls would otherwise be evicted "
            "merely for being old and re-fetched immediately."
        ),
    )


class ArchiveEvictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    connection_id: str = Field(default="", description="Narrow to one VCS connection")
    older_than_days: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Evict archives written more than this many days ago. Write-based here, "
            "because an archive is immutable once written and there is no access "
            "column to prefer."
        ),
    )


def _sealed_note() -> dict[str, object]:
    """What an eviction has to say on a node that cannot re-fetch (#2474).

    The scheduled retention sweep SKIPS these caches entirely when
    `registry.cache_only` is set, and its own comment gives the reason: "an
    un-refetchable artifact would be lost permanently". These routes are the
    manual path to the same deletion, so on a sealed node an eviction here is
    not an eviction at all -- it is a deletion with nothing able to restore it.

    It WARNS rather than refusing, and the 409 the warm route answers does NOT
    transfer. Warming is an upstream fetch by definition, so a sealed node
    genuinely cannot do it; eviction can, and refusing would leave an operator
    with no way to remove a bad artifact from exactly the deployment where a bad
    artifact is hardest to work around. The rule is the project's own: refuse
    only what cannot work, and warn about what works and cannot be undone.

    So the response carries the fact, and every consumer -- the admin page's
    confirmation, the SDK, an MCP tool -- can surface it without each one having
    to know what `cache_only` implies.
    """
    if not settings.registry.cache_only:
        return {"sealed": False}
    return {
        "sealed": True,
        "warning": (
            "This node is sealed (registry.cache_only), so nothing evicted here can "
            "be fetched again. The scheduled retention sweep skips these caches for "
            "that reason; a manual eviction is a permanent deletion."
        ),
    }


# ── package cache ──────────────────────────────────────────────────────────


@router.get("/admin/package-cache")
async def list_package_cache(
    ecosystem: str | None = None,
    name: str | None = None,
    request: Request = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Cached package artifacts, newest-ACCESSED first.

    `name` is a prefix match: a practitioner looking for `community.` wants the
    collection family, and an exact name is the degenerate case of that query.
    """
    entries = await cache_inventory.list_package_files(db, ecosystem=ecosystem, name=name)
    items = [
        {
            "id": str(e.id),
            "type": "cached-package-files",
            "attributes": {
                "ecosystem": e.ecosystem,
                "name": e.name,
                "version": e.version,
                "filename": e.filename,
                "size": e.size,
                # Upstream's own digest, as the ecosystem expresses it -- not
                # one of ours, so an operator can match it against what the
                # registry published rather than against what we stored.
                "digest": e.digest,
                "cached-at": rfc3339(e.cached_at),
                "last-accessed-at": rfc3339(e.last_accessed_at),
            },
        }
        for e in entries
    ]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.get("/admin/package-cache/totals")
async def package_cache_totals(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Count and bytes per ecosystem, aggregated in the database."""
    totals = await cache_inventory.package_totals(db)
    return JSONResponse(
        content={
            "data": [
                {
                    "type": "package-cache-totals",
                    "id": t.ecosystem,
                    "attributes": {"ecosystem": t.ecosystem, "files": t.files, "bytes": t.bytes},
                }
                for t in totals
            ],
            "meta": {
                "files": sum(t.files for t in totals),
                "bytes": sum(t.bytes for t in totals),
                # Reported HERE as well as on the eviction responses, because a
                # client needs it before it asks rather than after: a
                # confirmation that cannot say "these bytes will not come back"
                # is the one place this fact matters most. A panel loads the
                # totals anyway, so it costs nothing.
                **_sealed_note(),
            },
        }
    )


@router.delete("/admin/package-cache/{file_id}")
async def delete_package_cache_file(
    file_id: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict one artifact, by row id.

    By id rather than by `(ecosystem, name, filename)`: that tuple is the unique
    constraint, so either would address exactly one row, and an id cannot be
    mangled by a filename carrying a slash or a `+` the way a path segment can.
    """
    try:
        removed = await cache_inventory.delete_package_file(db, storage, file_id)
    except Exception as exc:  # a malformed uuid reaches the driver, not us
        raise HTTPException(status_code=404, detail="no such cached artifact") from exc
    if not removed:
        raise HTTPException(status_code=404, detail="no such cached artifact")
    await db.commit()
    return JSONResponse(content={"status": "evicted", **_sealed_note()})


@router.post("/admin/package-cache/evict")
async def evict_package_cache(
    body: PackageEvictRequest,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict in bulk by ecosystem and/or age.

    Neither filter means the whole cache, which is a thing an operator
    legitimately wants after a bad publish. Nothing is lost: every artifact here
    is re-fetchable from the registry it came from, which is what makes this
    cache evictable rather than a store.
    """
    result = await cache_inventory.evict_package_files(
        db,
        storage,
        ecosystem=body.ecosystem or None,
        older_than_days=body.older_than_days,
    )
    await db.commit()
    return JSONResponse(
        content={
            "status": "evicted",
            "files": result.files,
            "bytes": result.bytes,
            **_sealed_note(),
        }
    )


# ── VCS archive cache ──────────────────────────────────────────────────────


@router.get("/admin/vcs-archive-cache")
async def list_vcs_archive_cache(
    connection_id: str | None = None,
    after: str = "",
    limit: int = 100,
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """One page of cached archives, with a KEY cursor to resume from.

    A key cursor rather than `page[number]`, and so NOT the shared `paginate`
    helper: there are no rows here, so a page comes from a storage walk, and
    `list_prefix` is ordered by key on every backend precisely so a cursor is
    meaningful. An offset would re-walk the whole prefix per page.

    `meta.next-cursor` is empty when this is the last page.
    """
    entries, cursor = await cache_inventory.list_archives(
        storage, connection_id=connection_id, after=after, limit=limit
    )
    return JSONResponse(
        content={
            "data": [
                {
                    "type": "cached-vcs-archives",
                    "id": e.key,
                    "attributes": {
                        "key": e.key,
                        "connection-id": e.connection_id,
                        "owner": e.owner,
                        "repo": e.repo,
                        "sha": e.sha,
                        # "full" for a whole-repo archive, otherwise the hash of
                        # the path-narrowing set the workspace asked for.
                        "paths-hash": e.paths_hash,
                        "size": e.size,
                        "last-modified": e.last_modified,
                    },
                }
                for e in entries
            ],
            "meta": {"next-cursor": cursor, "page-size": len(entries)},
        }
    )


@router.get("/admin/vcs-archive-cache/totals")
async def vcs_archive_cache_totals(
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Count and bytes per repository, largest first.

    `meta.truncated` is the honest half: there is nothing to aggregate in the
    database, so this walks storage and a walk has to stop somewhere. A total
    that silently omitted whatever lay past the ceiling would understate exactly
    the thing an operator came here to find.
    """
    totals, truncated = await cache_inventory.archive_totals(storage)
    return JSONResponse(
        content={
            "data": [
                {
                    "type": "vcs-archive-cache-totals",
                    "id": f"{t.connection_id}/{t.owner}/{t.repo}",
                    "attributes": {
                        "connection-id": t.connection_id,
                        "owner": t.owner,
                        "repo": t.repo,
                        "archives": t.archives,
                        "bytes": t.bytes,
                    },
                }
                for t in totals
            ],
            "meta": {
                "archives": sum(t.archives for t in totals),
                "bytes": sum(t.bytes for t in totals),
                "truncated": truncated,
                "scan-limit": cache_inventory.MAX_ARCHIVE_SCAN,
                # See the package totals above: before, not after.
                **_sealed_note(),
            },
        }
    )


@router.delete("/admin/vcs-archive-cache")
async def delete_vcs_archive(
    key: str,
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict one archive by its storage key.

    The key arrives as a QUERY parameter rather than a path, because it contains
    slashes and a path would have to encode them. It is refused unless it sits
    under the archive prefix -- this route takes a storage key from a request, so
    without that check an admin typo could delete a state version or a plan
    artifact through an endpoint whose name says "cache".
    """
    if not await cache_inventory.delete_archive(storage, key):
        raise HTTPException(
            status_code=422,
            detail=(
                "key must be a cached VCS archive -- it has to start with "
                f"'{cache_inventory.VCS_ARCHIVE_PREFIX}' and contain no '..'"
            ),
        )
    return JSONResponse(content={"status": "evicted", **_sealed_note()})


@router.post("/admin/vcs-archive-cache/evict")
async def evict_vcs_archive_cache(
    body: ArchiveEvictRequest,
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict archives in bulk by connection and/or age."""
    result = await cache_inventory.evict_archives(
        storage,
        connection_id=body.connection_id or None,
        older_than_days=body.older_than_days,
    )
    return JSONResponse(
        content={
            "status": "evicted",
            "archives": result.files,
            "bytes": result.bytes,
            **_sealed_note(),
        }
    )
