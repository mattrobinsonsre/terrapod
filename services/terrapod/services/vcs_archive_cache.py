"""Single-flight VCS tarball cache for poll cycles.

Why this exists
---------------
Without coordination, every workspace that polls a VCS repo at a given
commit SHA does its own GitHub/GitLab tarball download — even when the
SHA is identical across workspaces (typical with multiple workspaces in
one mono-repo). With N workspaces tracking a single mono-repo at the
same SHA, that means N simultaneous downloads + N in-memory buffers per
poll cycle. For non-trivial repos (hundreds of MB), the api pod runs
out of memory and gets OOM-killed.

The cache solves this with two layers:

1. **In-process single-flight** — concurrent workspace polls within ONE
   replica's poll cycle coalesce on a single download. First requestor
   takes a per-key `asyncio.Lock`; the others wait, then either pull from
   the in-memory dict or read the now-populated storage cache.

2. **Object storage cache** — stripped tarballs persisted at
   `vcs_archives/{conn_id}/{owner}/{repo}/{sha}.tar.gz`. Survives across
   poll cycles and replicas. The artifact-retention sweeper evicts entries
   older than `vcs.archive_cache_retention_days`.

Multi-replica safety
--------------------
- The distributed scheduler guarantees only one replica runs `poll_cycle`
  per interval, so the in-process lock layer is sufficient for that path.
- The runs.py UI vcs-ref override path runs in request handlers on any
  replica. The in-process lock won't dedup cross-replica requests there,
  but the storage `head()` check plus the partial-failure cleanup in
  `_download_strip_upload` keeps the cache consistent: cloud backends
  (S3 multipart-complete, Azure commit-block-list, GCS resumable finalize)
  are atomic on success, and the filesystem backend's non-atomic write is
  caught by the try/except and the partial entry deleted. Worst case: two
  replicas do the same download once and overwrite the same content-
  addressed key with byte-identical content.

Memory profile
--------------
Bounded by per-chunk buffers (1 MiB stream chunk + tarfile per-member
buffer). A 500 MB tarball stays under ~10 MB of process memory at any
moment; the rest lives on the api pod's ephemeral PVC.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time as time_mod
from contextlib import asynccontextmanager

from terrapod.config import settings
from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service
from terrapod.services.archive_utils import strip_archive_top_level_dir_file_async
from terrapod.storage import get_storage
from terrapod.storage.keys import vcs_archive_key
from terrapod.storage.protocol import ObjectNotFoundError

logger = get_logger(__name__)

# Orphan temp files newer than this are presumed to belong to an in-flight
# operation in another coroutine — never delete them. 5 minutes covers the
# longest plausible streaming download/upload (200+ MB tarball over a slow
# link) plus headroom for a stuck operation that hasn't yet errored out.
_ORPHAN_AGE_SECONDS = 300


def _resolve_tmpdir() -> str | None:
    """Return the configured VCS tmpdir if it exists, else None.

    None falls back to the system tempdir at the call site — appropriate
    for tests and local dev without an ephemeral PVC mount.
    """
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


def _free_bytes(path: str) -> int:
    """Return free bytes on the filesystem containing `path`."""
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def _ensure_tmpdir_space(tmpdir: str | None) -> None:
    """Best-effort: free up space in `tmpdir` if free space is below threshold.

    Scans for orphan tarball-like temp files older than `_ORPHAN_AGE_SECONDS`
    (anything actively being used should be younger), deletes oldest first
    until we hit `vcs.tmpdir_min_free_bytes` free or run out of candidates.

    A previous pod crash can leave NamedTemporaryFile orphans behind even
    though the file descriptors went away — those files keep their disk
    blocks until something deletes them. Without this sweep the PVC fills
    up over time and every subsequent download fails with ENOSPC.

    Best-effort: failures here are logged and swallowed. The actual
    download will surface a real ENOSPC if there's still no space.
    """
    if tmpdir is None:
        # System tempdir — assume the OS handles its own cleanup
        return
    try:
        free = _free_bytes(tmpdir)
    except OSError as e:
        logger.warning("statvfs failed on tmpdir; skipping space check", path=tmpdir, error=str(e))
        return
    threshold = settings.vcs.tmpdir_min_free_bytes
    if free >= threshold:
        return

    logger.warning(
        "VCS tmpdir below free-space threshold; sweeping orphan tarballs",
        path=tmpdir,
        free_bytes=free,
        threshold_bytes=threshold,
    )

    cutoff = time_mod.time() - _ORPHAN_AGE_SECONDS
    candidates: list[tuple[float, str]] = []
    try:
        for entry in os.scandir(tmpdir):
            if not entry.is_file(follow_symlinks=False):
                continue
            name = entry.name
            # Only sweep files that look like our tarballs — avoid nuking
            # anything else mounted into this directory by accident.
            if not name.endswith(".tar.gz"):
                continue
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                candidates.append((mtime, entry.path))
    except OSError as e:
        logger.warning("scandir failed on tmpdir", path=tmpdir, error=str(e))
        return

    candidates.sort()  # oldest first
    deleted = 0
    bytes_freed = 0
    for _mtime, path in candidates:
        try:
            sz = os.path.getsize(path)
            os.unlink(path)
            deleted += 1
            bytes_freed += sz
        except OSError:
            continue
        try:
            free = _free_bytes(tmpdir)
        except OSError:
            break
        if free >= threshold:
            break

    logger.info(
        "VCS tmpdir sweep complete",
        path=tmpdir,
        deleted_files=deleted,
        bytes_freed=bytes_freed,
        free_bytes_after=free,
    )


async def _stream_download_to_file(
    conn: VCSConnection, owner: str, repo: str, sha: str, dest_path: str
) -> int:
    """Dispatch to the appropriate provider's streaming download."""
    if conn.provider == "gitlab":
        return await gitlab_service.download_archive_to_file(conn, owner, repo, sha, dest_path)
    return await github_service.download_repo_archive_to_file(conn, owner, repo, sha, dest_path)


async def _file_chunks(path: str, chunk_size: int = 1 << 20):
    """Async-iterate a file's contents in `chunk_size` chunks.

    Read I/O happens in a thread so the event loop isn't blocked on each
    read. The file handle lives in this generator's frame for the lifetime
    of the consumer — if the consumer aborts mid-iteration the handle
    closes via the generator's `with` block on GC.
    """

    def _read(f):  # noqa: ANN001
        return f.read(chunk_size)

    f = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(_read, f)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(f.close)


class VCSArchiveCache:
    """Single-flight cache for VCS archive tarballs.

    Construct one instance per logical work unit (poll cycle / immediate-poll
    trigger / one UI request). DO NOT reuse across cycles — `_known` grows
    without bound, and stale entries can mask cross-replica cache
    invalidations (e.g. an admin manually deleting a `vcs_archives/...` key
    via the artifact-retention sweeper).
    """

    def __init__(self) -> None:
        # Maps cache_key → storage_key (where the stripped tarball lives).
        self._known: dict[str, str] = {}
        # Per-cache-key locks for single-flight. We never delete entries
        # from this dict — the lock object itself is cheap and sticking
        # around for the cache lifetime is fine.
        self._locks: dict[str, asyncio.Lock] = {}

    def _key_lock(self, cache_key: str) -> asyncio.Lock:
        """Get-or-create the per-key lock.

        `dict.setdefault` is atomic under the GIL, and asyncio coroutines
        don't preempt at non-await points, so this is safe to call without
        a meta-lock.
        """
        return self._locks.setdefault(cache_key, asyncio.Lock())

    async def get_or_fetch(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        sha: str,
    ) -> str:
        """Ensure the stripped tarball for (conn, owner, repo, sha) is in storage.

        Returns the storage key. Concurrent calls for the same (conn, sha)
        coalesce — exactly one performs the download, the others wait and
        re-use the result.
        """
        cache_key = f"{conn.id}:{owner}/{repo}@{sha}"
        if cache_key in self._known:
            return self._known[cache_key]

        lock = self._key_lock(cache_key)
        async with lock:
            # Re-check after acquiring the lock — peer may have populated.
            if cache_key in self._known:
                return self._known[cache_key]

            storage_key = vcs_archive_key(str(conn.id), owner, repo, sha)
            storage = get_storage()

            # Storage cache hit (previous cycle or another replica wrote it).
            try:
                await storage.head(storage_key)
                self._known[cache_key] = storage_key
                logger.debug(
                    "VCS archive storage-cache hit",
                    connection_id=str(conn.id),
                    owner=owner,
                    repo=repo,
                    sha=sha[:8],
                )
                return storage_key
            except ObjectNotFoundError:
                pass

            # Miss → download, strip, upload.
            await self._download_strip_upload(conn, owner, repo, sha, storage_key)
            self._known[cache_key] = storage_key
            return storage_key

    async def _download_strip_upload(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        sha: str,
        storage_key: str,
    ) -> None:
        """Stream from VCS → strip → upload to object storage. No memory buffer.

        Transactional w.r.t. the storage cache: if `put_stream` raises after
        partially uploading, we best-effort `delete(storage_key)` so a future
        `head()` won't return OK on a truncated tarball. Without this, a
        corrupt entry would be silently served to subsequent workspaces and
        the runner would fail with a cryptic tarball error far from the
        upload site.
        """
        storage = get_storage()
        tmpdir = _resolve_tmpdir()
        # Sweep stale orphans before reserving more disk. statvfs is cheap;
        # the sweep only does real work when we're below the threshold.
        await asyncio.to_thread(_ensure_tmpdir_space, tmpdir)

        with (
            tempfile.NamedTemporaryFile(suffix=".raw.tar.gz", dir=tmpdir) as raw_f,
            tempfile.NamedTemporaryFile(suffix=".stripped.tar.gz", dir=tmpdir) as stripped_f,
        ):
            raw_path = raw_f.name
            stripped_path = stripped_f.name

            bytes_in = await _stream_download_to_file(conn, owner, repo, sha, raw_path)
            await strip_archive_top_level_dir_file_async(raw_path, stripped_path)
            bytes_out = await asyncio.to_thread(os.path.getsize, stripped_path)

            try:
                await storage.put_stream(
                    storage_key,
                    _file_chunks(stripped_path),
                    content_type="application/x-tar",
                )
            except Exception as e:
                # Partial upload: a future head() might return OK on a
                # truncated key. Best-effort delete so the cache stays
                # consistent. We swallow secondary errors — re-raise the
                # original.
                logger.warning(
                    "VCS archive upload failed; deleting partial cache entry",
                    storage_key=storage_key,
                    error=str(e),
                )
                try:
                    await storage.delete(storage_key)
                except Exception:
                    logger.warning(
                        "Best-effort cache delete also failed; cache may hold corrupt entry until TTL",
                        storage_key=storage_key,
                        exc_info=True,
                    )
                raise

            logger.info(
                "Cached VCS archive",
                connection_id=str(conn.id),
                owner=owner,
                repo=repo,
                sha=sha[:8],
                raw_bytes=bytes_in,
                stripped_bytes=bytes_out,
                storage_key=storage_key,
            )


@asynccontextmanager
async def materialize_archive(storage_key: str):
    """Stream a cached archive from storage into a local temp file.

    Yields the path to a temp file containing the stripped tarball.
    Caller can read it (e.g. for further processing) or stream-upload it
    elsewhere. The temp file is unlinked on context exit regardless of
    whether the caller errored.

    Uses `mkstemp` (single fd we own) rather than `NamedTemporaryFile`
    which opens its own write handle that we'd otherwise ignore. The fd
    is wrapped in a buffered `BufferedWriter` (`os.fdopen`) so chunk
    writes loop internally to handle short writes — the bare `os.write(2)`
    syscall can transfer fewer bytes than requested under disk pressure
    or signal interruption, which would silently truncate the cached
    tarball. All file I/O is dispatched to threads to keep the event
    loop unblocked.

    Used by the per-workspace config-version path: each workspace needs
    the same stripped tarball but at a different storage key, and we
    don't have a server-side copy primitive in our protocol — so we
    materialise once and stream-upload to the target key.
    """
    storage = get_storage()
    tmpdir = _resolve_tmpdir()
    await asyncio.to_thread(_ensure_tmpdir_space, tmpdir)

    fd, path = await asyncio.to_thread(tempfile.mkstemp, suffix=".tar.gz", dir=tmpdir)
    # Wrap the fd in a buffered file object so .write() handles short
    # writes for us. fdopen takes ownership of the fd; closing the
    # BufferedWriter closes the underlying fd.
    f = await asyncio.to_thread(os.fdopen, fd, "wb")
    try:
        try:
            # `storage.get_stream` is an async generator (uses `yield` under
            # the hood across S3/Azure/GCS/filesystem backends), so calling
            # it returns the iterator directly — DO NOT `await` it. Awaiting
            # an async-generator object raises `TypeError: 'async_generator'
            # object can't be awaited`. AsyncMock-based unit tests don't
            # surface this because the mock's `return_value` is itself
            # awaitable; only the real backends bite.
            async for chunk in storage.get_stream(storage_key):
                if not chunk:
                    continue
                await asyncio.to_thread(f.write, chunk)
        finally:
            await asyncio.to_thread(f.close)
        yield path
    finally:
        try:
            await asyncio.to_thread(os.unlink, path)
        except FileNotFoundError:
            pass
