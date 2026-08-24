"""The shared pull-through path: fetch, store, serve, record (#1417).

This is the half every language registry has in common. The ecosystem modules
own their index format; none of them own any of this.

Three constraints shape the code more than the protocol does:

* **Artifacts are large.** A CUDA-flavoured wheel is comfortably several hundred
  megabytes. Everything streams through a file on the ephemeral PVC and is never
  held in the API pod's memory (rule 14), and `/tmp` is not that PVC — it is
  RAM-backed, which is how a previous release OOM-killed both API pods.
* **The event loop is shared.** Hashing and file I/O go through
  `asyncio.to_thread` (rule 13); a 500 MB digest computed inline stalls every
  other request on the replica.
* **Sealing must fail honestly.** In `cache_only` mode a miss cannot be served,
  and the error says which package and what to do about it — a generic 502 sends
  an operator to look at their network.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import CachedPackageFile
from terrapod.logging_config import get_logger
from terrapod.storage import keys
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: Read size for streaming to and from disk. Large enough that a 500 MB artifact
#: is not a million round trips, small enough not to matter to a small one.
_CHUNK = 1024 * 1024

#: Upstream is a public CDN; a slow one is a slow build, not a broken one. The
#: connect timeout is what should be short.
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


class SealedError(RuntimeError):
    """A miss that cannot be filled because the node is sealed."""


class UpstreamError(RuntimeError):
    """Upstream refused or failed. Distinct from "no such package"."""


class NotFoundUpstream(RuntimeError):
    """Upstream says this artifact does not exist."""


@dataclass(frozen=True)
class Artifact:
    """What an ecosystem hands the substrate to fetch and cache.

    `digest` is upstream's own, in the form the ecosystem publishes it, and is
    recorded rather than verified here — the client checks it, and that is the
    point (see the package docstring).
    """

    ecosystem: str
    name: str
    version: str
    filename: str
    upstream_url: str
    digest: str = ""
    content_type: str = "application/octet-stream"


def sealed() -> bool:
    """Whether this node is forbidden from reaching upstream."""
    return bool(settings.registry.cache_only)


def _resolve_tmpdir() -> str | None:
    """The ephemeral PVC, or None for the system default in dev and tests."""
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


def _digest_file(path: str) -> tuple[int, str]:
    """Size and sha256 of a file on disk. Runs in a thread — never inline."""
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()


async def _file_chunks(path: str) -> AsyncIterator[bytes]:
    """Stream a file from disk without reading it into memory."""
    handle = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(handle.read, _CHUNK)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


async def lookup(
    db: AsyncSession, ecosystem: str, name: str, filename: str
) -> CachedPackageFile | None:
    """The cached row for an artifact, or None."""
    result = await db.execute(
        select(CachedPackageFile).where(
            CachedPackageFile.ecosystem == ecosystem,
            CachedPackageFile.name == name,
            CachedPackageFile.filename == filename,
        )
    )
    return result.scalar_one_or_none()


async def touch(db: AsyncSession, record: CachedPackageFile) -> None:
    """Mark an artifact as used, so retention does not evict a hot package.

    Retention compares `last_accessed_at`, so anything cached that is not touched
    on read looks permanently untouched and gets reaped however heavily it is
    used — which then re-fetches it, in a loop, forever.
    """
    record.last_accessed_at = datetime.now(UTC)
    await db.flush()


async def fetch_and_cache(
    db: AsyncSession,
    storage: ObjectStore,
    artifact: Artifact,
    *,
    client: httpx.AsyncClient | None = None,
) -> CachedPackageFile:
    """Fetch an artifact from upstream, store it, and record it.

    Safe to run concurrently for the same artifact. Two replicas racing a cold
    package both fetch — wasteful once, and cheaper than the coordination needed
    to prevent it — but the bytes are identical and the losing writer's unique
    violation resolves to the winner's row rather than a 500 in someone's
    `pip install`.
    """
    if sealed():
        raise SealedError(
            f"{artifact.ecosystem} package {artifact.name} {artifact.filename} is not "
            f"cached, and this node is sealed (registry.cache_only). Warm it before "
            f"sealing, or unset registry.cache_only."
        )

    key = keys.package_cache_key(artifact.ecosystem, artifact.name, artifact.filename)
    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="tp-pkg-", dir=_resolve_tmpdir())
    os.close(tmp_fd)
    try:
        try:
            async with client.stream("GET", artifact.upstream_url) as response:
                if response.status_code == 404:
                    raise NotFoundUpstream(artifact.upstream_url)
                if response.status_code >= 400:
                    raise UpstreamError(
                        f"upstream returned {response.status_code} for {artifact.upstream_url}"
                    )
                handle = await asyncio.to_thread(open, tmp_path, "wb")
                try:
                    async for chunk in response.aiter_bytes(_CHUNK):
                        await asyncio.to_thread(handle.write, chunk)
                finally:
                    await asyncio.to_thread(handle.close)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not reach {artifact.upstream_url}: {exc}") from exc

        size, _sha256 = await asyncio.to_thread(_digest_file, tmp_path)
        await storage.put_stream(key, _file_chunks(tmp_path), content_type=artifact.content_type)
    finally:
        # The PVC is shared and finite; a failed fetch must not leave its partial
        # download behind to be discovered as a disk-full incident later.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if owns_client:
            await client.aclose()

    record = CachedPackageFile(
        ecosystem=artifact.ecosystem,
        name=artifact.name,
        version=artifact.version,
        filename=artifact.filename,
        storage_key=key,
        size=size,
        digest=artifact.digest,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        # Lost the race. The winner stored identical bytes at the identical key,
        # so its row is as good as ours.
        await db.rollback()
        existing = await lookup(db, artifact.ecosystem, artifact.name, artifact.filename)
        if existing is None:  # pragma: no cover — the constraint says otherwise
            raise
        return existing

    logger.info(
        "Cached package artifact",
        ecosystem=artifact.ecosystem,
        name=artifact.name,
        filename=artifact.filename,
        size=size,
    )
    return record


async def lookup_present(
    db: AsyncSession, storage: ObjectStore, ecosystem: str, name: str, filename: str
) -> CachedPackageFile | None:
    """A cached record whose object is really there, or None.

    **The row is not the truth; the store is.** A row whose object has gone —
    a pruned bucket, a recreated container, a partially restored backup — must be
    treated as a miss and re-fetched, never served as a redirect to a 404 that
    nobody can trace back to a database row. That is not hypothetical: it is what
    a stale row produced during this feature's own testing, and it looked exactly
    like a bug in the client.

    Every read path goes through here so the check cannot be skipped by one of
    them, which is precisely how it got skipped the first time.
    """
    record = await lookup(db, ecosystem, name, filename)
    if record is None:
        return None
    if not await storage.exists(record.storage_key):
        logger.warning(
            "Cached package row has no object; treating as a miss",
            ecosystem=ecosystem,
            name=name,
            filename=filename,
        )
        await db.delete(record)
        await db.flush()
        return None
    await touch(db, record)
    return record


async def get_or_fetch(
    db: AsyncSession,
    storage: ObjectStore,
    artifact: Artifact,
    *,
    client: httpx.AsyncClient | None = None,
) -> CachedPackageFile:
    """The cached artifact, fetching it first if we do not have it."""
    existing = await lookup_present(
        db, storage, artifact.ecosystem, artifact.name, artifact.filename
    )
    if existing is not None:
        return existing

    return await fetch_and_cache(db, storage, artifact, client=client)


async def store_document(
    db: AsyncSession, storage: ObjectStore, artifact: Artifact, data: bytes
) -> CachedPackageFile:
    """Cache a small document we already hold in memory.

    Used for index documents rather than distributions — an npm packument, which
    a sealed node has no other way to obtain and cannot serve an install without,
    because the dependency ranges live in it and nowhere else.

    Overwrites any existing copy: the newest fetch is the best answer a sealed
    node will ever have for what versions exist.
    """
    key = keys.package_cache_key(artifact.ecosystem, artifact.name, artifact.filename)
    await storage.put(key, data, content_type="application/json")

    existing = await lookup(db, artifact.ecosystem, artifact.name, artifact.filename)
    if existing is not None:
        existing.size = len(data)
        existing.version = artifact.version
        existing.last_accessed_at = datetime.now(UTC)
        await db.flush()
        return existing

    record = CachedPackageFile(
        ecosystem=artifact.ecosystem,
        name=artifact.name,
        version=artifact.version,
        filename=artifact.filename,
        storage_key=key,
        size=len(data),
        digest=artifact.digest,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        found = await lookup(db, artifact.ecosystem, artifact.name, artifact.filename)
        if found is None:  # pragma: no cover — the constraint says otherwise
            raise
        return found
    return record


async def load_document(
    db: AsyncSession, storage: ObjectStore, ecosystem: str, name: str, filename: str
) -> bytes | None:
    """A cached document's bytes, or None if we do not hold it."""
    record = await lookup(db, ecosystem, name, filename)
    if record is None:
        return None
    try:
        return await storage.get(record.storage_key)
    except Exception:
        # The row promised something the store does not have. Treated as a miss;
        # on a sealed node that becomes an honest "not cached" rather than a 500.
        logger.warning("Cached document missing from storage", key=record.storage_key)
        return None


async def cached_filenames(db: AsyncSession, ecosystem: str, name: str) -> list[str]:
    """Every artifact filename cached for a package.

    What a sealed node can actually serve, which is what its index must describe:
    advertising a version whose bytes are not here sends a client to ask for it
    and get a 503 it cannot do anything about.
    """
    result = await db.execute(
        select(CachedPackageFile.filename).where(
            CachedPackageFile.ecosystem == ecosystem,
            CachedPackageFile.name == name,
        )
    )
    return list(result.scalars().all())


async def cached_files(db: AsyncSession, ecosystem: str, name: str) -> list[CachedPackageFile]:
    """Every cached artifact row for a package."""
    result = await db.execute(
        select(CachedPackageFile)
        .where(
            CachedPackageFile.ecosystem == ecosystem,
            CachedPackageFile.name == name,
        )
        .order_by(CachedPackageFile.filename)
    )
    return list(result.scalars().all())
