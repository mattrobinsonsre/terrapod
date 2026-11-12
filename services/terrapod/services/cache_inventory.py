"""Enumerating and evicting the two caches that could not be inspected (#2474).

Terrapod keeps four caches. Two of them — CLI binaries and provider binaries —
have had admin list and delete routes since they existed, and the other two did
not: the **package cache** (galaxy, pypi, npm, go, nuget, pulumi) had only
`POST /admin/package-cache/warm`, and the **VCS archive cache** had nothing at
all. So an operator could warm a cache and never see what was in it, how big it
had grown, or evict one bad artifact without going to object storage by hand.

The two are enumerated by different means, and that difference is not an
inconsistency to tidy away:

* the package cache has a row per artifact (`cached_package_files`), so it is a
  database query -- filterable, countable, and able to answer "how many bytes of
  npm" without touching storage at all;
* the VCS archive cache has **no rows**. An archive is addressed entirely by its
  key, `vcs_archives/{connection}/{owner}/{repo}/{sha}-{paths_hash}.tar.gz`, and
  that was a deliberate choice: the tarball is content-addressed by commit and
  path set, so the key *is* the record and a table would be a second copy of it
  to keep in step. Listing it therefore walks storage, which is a paged
  operation on S3, Azure and GCS -- hence a key cursor rather than an offset,
  and a bounded page rather than "everything".

`vcs_metadata_cache` is deliberately absent from both: it lives in memory for
the length of one poll cycle, so there is nothing to enumerate and nothing an
eviction could mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import CachedPackageFile, now_utc
from terrapod.logging_config import get_logger
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: The prefix every cached VCS archive key starts with. One producer: a second
#: spelling of it is how a listing and an eviction come to disagree about what
#: they are addressing.
VCS_ARCHIVE_PREFIX = "vcs_archives/"

#: The most keys one storage walk will fetch. A ceiling rather than a default,
#: because the caller's `limit` is what decides the page -- this only stops an
#: unbounded request from pulling a six-figure estate into one response.
MAX_ARCHIVE_SCAN = 5000

#: The most objects ONE bulk eviction call will remove, for either cache.
#:
#: A bound on the WORK rather than on what matches, and the result says when it
#: stopped early and where to resume -- so a large cache is purged by repeating
#: the call rather than by one request that outlives its gateway. Before this,
#: `evict_package_files` deleted every matching object sequentially inside one
#: handler: 50,000 artifacts at ~30 ms each is about 25 minutes, so the gateway
#: timed out, the transaction rolled back, and the objects already deleted left
#: rows pointing at keys that no longer existed (#2492).
#:
#: Separate from `MAX_ARCHIVE_SCAN`, which bounds a READ -- how much of the
#: estate a listing shows or a total aggregates. Merging them is the obvious
#: tidy and would be wrong in both directions: a read can afford a wide window
#: because it only costs one list call, and a destructive loop cannot because
#: every entry in the window is a round trip.
#:
#: Deliberately NOT a Helm value. The right bound is a property of the gateway
#: in front of the API, not of the estate behind it, and the unattended path is
#: already configurable: the scheduled retention sweep evicts both these caches
#: with `artifact_retention.batch_size`. This constant bounds the ATTENDED path,
#: where an operator who has just published a bad artifact wants it gone now and
#: can see from the response that more remain.
#:
#: Deliberately NOT implemented as a batch delete either, which is the other
#: obvious answer: `ObjectStore` has no such method, adding one means four
#: backends with four different hard limits -- and S3's `DeleteObjects` fails
#: the WHOLE call over 1000 keys, which looks like it worked. A batch delete
#: would make this loop faster and leave it just as unbounded, and the defect is
#: the unboundedness.
MAX_EVICT_BATCH = 1000


# ── the package cache ──────────────────────────────────────────────────────


async def list_package_files(
    db: AsyncSession,
    *,
    ecosystem: str | None = None,
    name: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[CachedPackageFile], int]:
    """One page of cached package artifacts, newest-accessed first, and the
    total matching the filters.

    Ordered by `last_accessed_at` rather than `cached_at` because that is the
    column retention reads: an operator deciding what to evict wants to see what
    nothing has touched, and sorting by when it arrived buries exactly that.

    **The page is sliced in SQL, and the total is counted in SQL.** It used to
    return every matching row and let the router slice the list in Python, so
    every page of a 60,000-artifact cache materialised all 60,000 ORM rows --
    and the admin panel walked every page, which made one filter keystroke
    hundreds of requests each paying for the whole table (#2492). `limit` of
    `None` still returns everything, because that is the documented non-paged
    contract (`page[size]` absent means the full list) and an SDK that sends no
    paging params relies on it; what changed is that asking for a page now costs
    a page.

    A secondary `id` sort breaks ties, which `last_accessed_at` alone does not:
    artifacts cached by one request share a timestamp to the microsecond, and
    without a tiebreak Postgres may order them differently per query -- so a row
    could appear on two consecutive pages, or on neither.
    """
    stmt = select(CachedPackageFile)
    count_stmt = select(func.count()).select_from(CachedPackageFile)
    if ecosystem:
        stmt = stmt.where(CachedPackageFile.ecosystem == ecosystem)
        count_stmt = count_stmt.where(CachedPackageFile.ecosystem == ecosystem)
    if name:
        # A prefix match, not equality: a practitioner looking for "community."
        # wants the collection family, and the exact name is the degenerate
        # case of the same query.
        stmt = stmt.where(CachedPackageFile.name.startswith(name))
        count_stmt = count_stmt.where(CachedPackageFile.name.startswith(name))

    total = int((await db.execute(count_stmt)).scalar() or 0)

    stmt = stmt.order_by(CachedPackageFile.last_accessed_at.desc(), CachedPackageFile.id.desc())
    if limit is not None:
        stmt = stmt.offset(max(offset, 0)).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all()), total


@dataclass(frozen=True)
class EcosystemTotal:
    """What one ecosystem is holding."""

    ecosystem: str
    files: int
    bytes: int


async def package_totals(db: AsyncSession) -> list[EcosystemTotal]:
    """Count and bytes per ecosystem, aggregated in the database.

    In SQL rather than by summing a listing, because the listing is paged: a
    total computed from one page is wrong by however much the page left out, and
    wrong in the reassuring direction.
    """
    result = await db.execute(
        select(
            CachedPackageFile.ecosystem,
            func.count().label("files"),
            func.coalesce(func.sum(CachedPackageFile.size), 0).label("bytes"),
        )
        .group_by(CachedPackageFile.ecosystem)
        .order_by(CachedPackageFile.ecosystem)
    )
    return [
        EcosystemTotal(ecosystem=r.ecosystem, files=r.files, bytes=int(r.bytes)) for r in result
    ]


async def delete_package_file(db: AsyncSession, storage: ObjectStore, file_id: str) -> bool:
    """Evict one artifact. Returns False when the row is already gone.

    The object is deleted before the row, deliberately. The other order can
    leave an object with no row pointing at it -- invisible to this page, still
    paying for storage, and re-cached on the next request under a *new* row
    rather than reusing it. A row whose object is already absent is the
    recoverable direction: the next fetch simply re-populates it.
    """
    entry = await db.get(CachedPackageFile, file_id)
    if entry is None:
        return False
    await storage.delete(entry.storage_key)
    await db.delete(entry)
    await db.flush()
    logger.info(
        "cached package artifact evicted",
        ecosystem=entry.ecosystem,
        name=entry.name,
        filename=entry.filename,
        size=entry.size,
    )
    return True


@dataclass(frozen=True)
class EvictionResult:
    """What a bulk eviction removed, **and whether that was all of it**.

    `truncated` is the honest half, and the reason this dataclass exists rather
    than a bare pair of counts. Both evictions are bounded (`MAX_EVICT_BATCH`),
    so the answer can be partial -- and a partial answer reported as a complete
    one is worse than a partial result. The observed sequence was: an operator
    posts `{"older_than_days": 30}` against 20,000 archives, gets back
    `{"status": "evicted", "archives": 400}`, reloads the totals, sees the same
    size, and reads it as "the purge did nothing" rather than "the purge is
    bounded" (#2491).

    `cursor` is where a repeat call should resume, and it is **empty for the
    package cache** rather than missing. That asymmetry is the two caches being
    different things rather than an inconsistency to tidy:

    * the package eviction selects the rows it then deletes, so the rows that
      matched are gone and the next call's own `LIMIT` picks up where this one
      stopped. A cursor would be an offset into a shrinking set, which skips
      rows;
    * the archive eviction walks keys that are mostly NOT deleted -- an age
      filter may match a handful in a window of a thousand -- so without a
      cursor the walk restarts at the same place every time. That is what made
      a large archive cache unpurgeable no matter how often the route was run,
      and in the degenerate case where nothing in the first window matches, the
      window never advanced at all.
    """

    files: int
    bytes: int
    #: True when more matched than this call removed. Read it: the alternative
    #: is presenting a bounded sweep as a complete one.
    truncated: bool = False
    #: The key to resume an archive sweep from, or `""` when there is nothing
    #: to resume -- either because the sweep finished or because this is the
    #: package cache, which resumes by itself. See the class docstring.
    cursor: str = ""


async def evict_package_files(
    db: AsyncSession,
    storage: ObjectStore,
    *,
    ecosystem: str | None = None,
    older_than_days: int | None = None,
) -> EvictionResult:
    """Evict every artifact matching the filters, by **last access**.

    Access-based, never write-based, for the reason the model records: an
    artifact every run pulls would be evicted merely for being old and re-fetched
    immediately, which costs the bandwidth the cache exists to save.

    **Neither filter means everything**, and that is intentional rather than a
    missing guard -- the caller is an admin asking to empty a cache, which is a
    thing an operator legitimately wants after a bad publish. Nothing is lost
    either way: every artifact here is re-fetchable from the registry it came
    from, which is what makes this cache evictable at all.
    """
    stmt = select(CachedPackageFile)
    if ecosystem:
        stmt = stmt.where(CachedPackageFile.ecosystem == ecosystem)
    if older_than_days is not None:
        cutoff = now_utc() - timedelta(days=older_than_days)
        stmt = stmt.where(CachedPackageFile.last_accessed_at < cutoff)

    # STALEST FIRST, which matters only because the call is bounded: if a sweep
    # can take a thousand of fifty thousand, the thousand worth taking are the
    # ones nothing has touched for longest. The scheduled sweep orders its own
    # batch the same way and says so.
    #
    # ...and ONE ROW PAST THE BOUND, which is what makes `truncated` exact.
    # Asking for exactly `MAX_EVICT_BATCH` cannot tell "this many matched" from
    # "more matched and we stopped": both come back as a full page, so `>=`
    # would report a cache of precisely that many as truncated and tell an
    # operator more remained when nothing did. `archive_totals` asks for one
    # past its own ceiling for the same reason.
    stmt = stmt.order_by(CachedPackageFile.last_accessed_at.asc()).limit(MAX_EVICT_BATCH + 1)
    matched = list((await db.execute(stmt)).scalars().all())
    truncated = len(matched) > MAX_EVICT_BATCH
    entries = matched[:MAX_EVICT_BATCH]
    if not entries:
        return EvictionResult(files=0, bytes=0, truncated=False)

    total = 0
    for entry in entries:
        await storage.delete(entry.storage_key)
        total += entry.size

    await db.execute(
        delete(CachedPackageFile).where(CachedPackageFile.id.in_([e.id for e in entries]))
    )
    await db.flush()
    logger.info(
        "package cache evicted",
        ecosystem=ecosystem or "all",
        older_than_days=older_than_days,
        files=len(entries),
        bytes=total,
        truncated=truncated,
    )
    return EvictionResult(files=len(entries), bytes=total, truncated=truncated)


# ── the VCS archive cache ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchiveEntry:
    """One cached archive, as its key describes it.

    Every field but `size` and `last_modified` is READ OUT OF THE KEY. There is
    no row to read instead, so an entry whose key does not parse is reported with
    the parts it could resolve rather than skipped: a key this module cannot read
    is exactly the one an operator most needs to see, since nothing else will
    ever show it to them.
    """

    key: str
    connection_id: str
    owner: str
    repo: str
    sha: str
    paths_hash: str
    size: int
    last_modified: str


def parse_archive_key(key: str) -> ArchiveEntry | None:
    """Read an archive key back into its parts.

    `vcs_archives/{connection}/{owner}/{repo}/{sha}-{paths_hash}.tar.gz`, split
    on the FIRST hyphen of the last segment: a commit sha is hex and carries
    none, where `paths_hash` may. Returns None for a key that does not fit,
    which the caller surfaces rather than drops.
    """
    if not key.startswith(VCS_ARCHIVE_PREFIX) or not key.endswith(".tar.gz"):
        return None
    rest = key[len(VCS_ARCHIVE_PREFIX) : -len(".tar.gz")]
    parts = rest.split("/")
    if len(parts) != 4:
        return None
    connection_id, owner, repo, tail = parts
    sha, _, paths_hash = tail.partition("-")
    if not sha or not paths_hash:
        return None
    return ArchiveEntry(
        key=key,
        connection_id=connection_id,
        owner=owner,
        repo=repo,
        sha=sha,
        paths_hash=paths_hash,
        size=0,
        last_modified="",
    )


async def list_archives(
    storage: ObjectStore,
    *,
    connection_id: str | None = None,
    after: str = "",
    limit: int = 100,
) -> tuple[list[ArchiveEntry], str]:
    """One page of cached archives, plus the cursor to resume from.

    A KEY cursor rather than an offset, because `list_prefix` is ordered by key
    on every backend and that ordering is part of its contract -- an offset into
    a paged remote listing would re-walk the whole prefix per page.

    Narrowing by connection narrows the PREFIX rather than filtering a page, so
    an operator inspecting one connection does not pay for the whole estate.
    """
    prefix = VCS_ARCHIVE_PREFIX
    if connection_id:
        prefix = f"{VCS_ARCHIVE_PREFIX}{connection_id}/"

    bounded = min(max(limit, 1), MAX_ARCHIVE_SCAN)
    objects = await storage.list_prefix(prefix, after=after, limit=bounded)

    out: list[ArchiveEntry] = []
    for obj in objects:
        parsed = parse_archive_key(obj.key)
        if parsed is None:
            # Surfaced, not skipped -- see ArchiveEntry's docstring.
            out.append(
                ArchiveEntry(
                    key=obj.key,
                    connection_id="",
                    owner="",
                    repo="",
                    sha="",
                    paths_hash="",
                    size=obj.size_bytes,
                    last_modified=_iso(obj),
                )
            )
            continue
        out.append(
            ArchiveEntry(
                key=parsed.key,
                connection_id=parsed.connection_id,
                owner=parsed.owner,
                repo=parsed.repo,
                sha=parsed.sha,
                paths_hash=parsed.paths_hash,
                size=obj.size_bytes,
                last_modified=_iso(obj),
            )
        )

    # The cursor is the last key of THIS page, so a caller that got fewer
    # entries than it asked for knows it has reached the end by the page being
    # short rather than by the cursor being empty.
    cursor = out[-1].key if len(out) == bounded else ""
    return out, cursor


@dataclass(frozen=True)
class RepoTotal:
    """What one repository's archives are holding."""

    connection_id: str
    owner: str
    repo: str
    archives: int
    bytes: int


async def archive_totals(storage: ObjectStore) -> tuple[list[RepoTotal], bool]:
    """Count and bytes per repository, and whether the walk was TRUNCATED.

    The flag is the honest half. There are no rows to aggregate, so this walks
    storage, and a walk has to stop somewhere; a total that silently omitted
    whatever lay past the ceiling would be wrong in the reassuring direction --
    an operator looking for what is filling a bucket would be shown a number
    smaller than the problem. So the ceiling is reported and the caller says so.
    """
    # ONE OBJECT PAST THE CEILING, which is what makes the flag exact.
    #
    # Asking for exactly `MAX_ARCHIVE_SCAN` cannot tell "the cache holds exactly
    # that many" from "it holds more and the walk stopped": both come back as a
    # full page. `>=` therefore reported a cache of precisely 5000 archives as
    # truncated and rendered "these numbers understate the problem" over figures
    # that were exact -- and `>` alone, with the same limit, is WORSE: the store
    # never returns more than it was asked for, so nothing could ever exceed the
    # ceiling and the flag would be dead. Ask for one more than we will use, and
    # the comparison means what it says.
    objects = await storage.list_prefix(VCS_ARCHIVE_PREFIX, limit=MAX_ARCHIVE_SCAN + 1)
    truncated = len(objects) > MAX_ARCHIVE_SCAN
    objects = objects[:MAX_ARCHIVE_SCAN]

    buckets: dict[tuple[str, str, str], RepoTotal] = {}
    for obj in objects:
        parsed = parse_archive_key(obj.key)
        if parsed is None:
            continue
        k = (parsed.connection_id, parsed.owner, parsed.repo)
        prev = buckets.get(k)
        if prev is None:
            buckets[k] = RepoTotal(
                connection_id=parsed.connection_id,
                owner=parsed.owner,
                repo=parsed.repo,
                archives=1,
                bytes=obj.size_bytes,
            )
        else:
            buckets[k] = RepoTotal(
                connection_id=prev.connection_id,
                owner=prev.owner,
                repo=prev.repo,
                archives=prev.archives + 1,
                bytes=prev.bytes + obj.size_bytes,
            )

    totals = sorted(buckets.values(), key=lambda t: (-t.bytes, t.owner, t.repo))
    return totals, truncated


async def delete_archive(storage: ObjectStore, key: str) -> bool:
    """Evict one archive by key.

    The key is REFUSED unless it is under the archive prefix. Not defensive
    tidiness: this endpoint takes a storage key from a request, so without the
    check an admin typo -- or anything that reached an admin token -- could
    delete a state version, a plan artifact or a provider binary through a route
    whose name says "cache".
    """
    if not key.startswith(VCS_ARCHIVE_PREFIX) or ".." in key:
        return False
    await storage.delete(key)
    logger.info("cached vcs archive evicted", key=key)
    return True


async def evict_archives(
    storage: ObjectStore,
    *,
    connection_id: str | None = None,
    older_than_days: int | None = None,
    after: str = "",
) -> EvictionResult:
    """Evict archives by connection and/or age, **one bounded window at a time**.

    Age comes from the object's own `last_modified`, because there is nothing
    else: an archive is immutable once written, so its write time IS its age,
    and unlike a package artifact there is no access column to prefer. That
    makes this eviction cheaper to reason about and slightly blunter -- an
    archive a run pulled this morning is evicted if its commit is old enough,
    and the next run re-fetches it.

    **The window is `MAX_EVICT_BATCH` keys and the result says where it stopped.**
    This used to walk `MAX_ARCHIVE_SCAN` keys from the START of the prefix every
    time and report only a count, which made a cache larger than the ceiling
    unpurgeable: the walk always began at the same place, so repeating the call
    re-examined the same window. With an age filter matching a small fraction of
    it, the window crept forward by only as many keys as the call deleted; with
    one matching none of it, the window never moved at all and the rest of the
    estate was unreachable for ever (#2491).

    **The cursor is the last key EXAMINED, not the last key deleted**, and that
    is the whole mechanism. Resuming past a key we looked at and kept is what
    makes the next call reach keys this one never saw; resuming past the last
    DELETION would re-walk every non-matching key in between, which is the
    stuck-window behaviour in a smaller costume.

    A caller resuming MUST pass the same filters back, or the second window is
    a different question from the first.
    """
    prefix = f"{VCS_ARCHIVE_PREFIX}{connection_id}/" if connection_id else VCS_ARCHIVE_PREFIX
    # One key past the window, so `truncated` is exact rather than "the window
    # happened to be full" -- the same reason `archive_totals` asks for one more
    # than it uses.
    objects = await storage.list_prefix(prefix, after=after, limit=MAX_EVICT_BATCH + 1)
    truncated = len(objects) > MAX_EVICT_BATCH
    objects = objects[:MAX_EVICT_BATCH]

    cutoff = None
    if older_than_days is not None:
        cutoff = now_utc() - timedelta(days=older_than_days)

    files = 0
    total = 0
    last_examined = ""
    for obj in objects:
        # Recorded FIRST, before either guard, so the cursor means exactly what
        # its name says: the last key this call LOOKED AT. A key we examined and
        # kept still has to advance it -- recording it only on a delete is the
        # stuck-window defect in a smaller costume, and skipping it on the
        # prefix guard would leave a window of nothing but off-prefix keys
        # reporting `truncated` with no cursor, which a caller looping on the
        # cursor reads as the end.
        last_examined = obj.key
        if not obj.key.startswith(VCS_ARCHIVE_PREFIX):
            continue
        if cutoff is not None:
            when = getattr(obj, "last_modified", None)
            if when is None or when >= cutoff:
                continue
        await storage.delete(obj.key)
        files += 1
        total += obj.size_bytes

    cursor = last_examined if truncated else ""
    logger.info(
        "vcs archive cache evicted",
        connection_id=connection_id or "all",
        older_than_days=older_than_days,
        archives=files,
        bytes=total,
        truncated=truncated,
    )
    return EvictionResult(files=files, bytes=total, truncated=truncated, cursor=cursor)


def _iso(obj: object) -> str:
    """An object's last-modified time as RFC3339, or `""` when the backend did
    not report one -- the filesystem backend does, the others do, and a future
    one that does not must not crash a listing."""
    when = getattr(obj, "last_modified", None)
    if when is None:
        return ""
    from terrapod.api.serialization import rfc3339

    return rfc3339(when)


__all__ = [
    "MAX_ARCHIVE_SCAN",
    "MAX_EVICT_BATCH",
    "VCS_ARCHIVE_PREFIX",
    "ArchiveEntry",
    "EcosystemTotal",
    "EvictionResult",
    "RepoTotal",
    "archive_totals",
    "delete_archive",
    "delete_package_file",
    "evict_archives",
    "evict_package_files",
    "list_archives",
    "list_package_files",
    "package_totals",
    "parse_archive_key",
]
