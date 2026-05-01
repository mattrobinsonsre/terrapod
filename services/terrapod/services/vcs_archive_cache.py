"""Single-flight VCS tarball cache for poll cycles.

Why this exists
---------------
Without coordination, every workspace that polls a VCS repo at a given
commit SHA + path set does its own git fetch — even when the
(SHA, paths) is identical across workspaces (typical with multiple
workspaces in one monorepo sharing the same `working_directory` /
`trigger_prefixes` union).

The cache solves this with two layers:

1. **In-process single-flight** — concurrent workspace polls within ONE
   replica's poll cycle coalesce on a single fetch. First requestor
   takes a per-key `asyncio.Lock`; the others wait, then either pull from
   the in-memory dict or read the now-populated storage cache.

2. **Object storage cache** — narrowed tarballs persisted at
   `vcs_archives/{conn_id}/{owner}/{repo}/{sha}-{paths_hash}.tar.gz`.
   Survives across poll cycles and replicas. The artifact-retention
   sweeper evicts entries older than `vcs.archive_cache_retention_days`.

Path narrowing
--------------
Each fetch is scoped to a path set — typically the union of every
workspace's `working_directory ∪ trigger_prefixes` for the same
`(connection, repo)` in the cycle. Only blobs under those paths are
fetched (via dulwich partial clone + sparse selection in
`git_fetch.py`). Different path sets produce different cache keys; the
caller is responsible for stable path-set computation per cycle.

Multi-replica safety
--------------------
- The distributed scheduler guarantees only one replica runs `poll_cycle`
  per interval, so the in-process lock layer is sufficient for that path.
- The runs.py UI vcs-ref override path runs in request handlers on any
  replica. The in-process lock won't dedup cross-replica requests there,
  but the storage `head()` check plus the partial-failure cleanup in
  `_fetch_and_upload` keeps the cache consistent: cloud backends
  (S3 multipart-complete, Azure commit-block-list, GCS resumable finalize)
  are atomic on success, and the filesystem backend's non-atomic write is
  caught by the try/except and the partial entry deleted. Worst case: two
  replicas do the same fetch once and overwrite the same content-
  addressed key with byte-identical content.

Memory profile
--------------
The git CLI does the fetch on disk (clone dir on the api pod's
ephemeral PVC; no in-memory pack buffering). Tar production reads
files one at a time via `tarfile.add` and streams gzip output
through an `os.pipe` to `storage.put_stream` — kernel pipe buffer
provides backpressure. Peak memory at any moment:

* one blob in flight (Python's `tarfile.add` reads the source file
  through a buffered reader, so the file's bytes don't all land in
  memory at once — but the gzip writer's internal block buffer can
  hold up to ~64 KiB of output)
* one 64 KiB pipe chunk being consumed
* zlib state for the gzip stream (small, kilobytes)

For a repo of small terraform files this stays well under 10 MB. A
single very large file (e.g. a multi-hundred-MB binary asset) would
be streamed through the gzip writer in 64 KiB chunks — the file
itself is never fully resident.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time as time_mod
from collections.abc import Iterable
from contextlib import asynccontextmanager

from terrapod.config import settings
from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger
from terrapod.services import git_fetch
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


_CLONE_DIR_PREFIX = "vcs-clone-"


def _dir_size_bytes(path: str) -> int:
    """Best-effort recursive size for a directory; 0 on errors."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _ensure_tmpdir_space(tmpdir: str | None) -> None:
    """Best-effort: free up space in `tmpdir` if free space is below threshold.

    Scans for orphans older than `_ORPHAN_AGE_SECONDS` and deletes oldest
    first until we hit `vcs.tmpdir_min_free_bytes` free or run out of
    candidates. Two orphan classes:

    * Tarball-shaped files (`*.tar.gz`) left behind by NamedTemporaryFile
      after a pod crash before the context exit cleanup ran.
    * Clone directories (`vcs-clone-*`) left behind by an aborted
      git_fetch — TemporaryDirectory normally cleans them up, but a hard
      kill (OOM, SIGKILL) skips the cleanup hook.

    Without this sweep the PVC fills up over time and every subsequent
    fetch fails with ENOSPC.

    Best-effort: failures here are logged and swallowed. The actual
    fetch will surface a real ENOSPC if there's still no space.
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
        "VCS tmpdir below free-space threshold; sweeping orphans",
        path=tmpdir,
        free_bytes=free,
        threshold_bytes=threshold,
    )

    cutoff = time_mod.time() - _ORPHAN_AGE_SECONDS
    # `kind` ∈ {"file", "dir"} — we delete with different syscalls.
    candidates: list[tuple[float, str, str]] = []
    try:
        for entry in os.scandir(tmpdir):
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            name = entry.name
            if entry.is_file(follow_symlinks=False) and name.endswith(".tar.gz"):
                candidates.append((mtime, entry.path, "file"))
            elif entry.is_dir(follow_symlinks=False) and name.startswith(_CLONE_DIR_PREFIX):
                candidates.append((mtime, entry.path, "dir"))
    except OSError as e:
        logger.warning("scandir failed on tmpdir", path=tmpdir, error=str(e))
        return

    candidates.sort()  # oldest first
    deleted = 0
    bytes_freed = 0
    for _mtime, path, kind in candidates:
        try:
            if kind == "file":
                sz = os.path.getsize(path)
                os.unlink(path)
            else:
                sz = _dir_size_bytes(path)
                shutil.rmtree(path, ignore_errors=True)
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
        deleted_orphans=deleted,
        bytes_freed=bytes_freed,
        free_bytes_after=free,
    )


async def _file_chunks(path: str, chunk_size: int = 1 << 20):
    """Async-iterate a file's contents in `chunk_size` chunks.

    Used by the cv-upload-from-cache path in `vcs_poller`: a workspace's
    config-version is uploaded to its own storage key by streaming bytes
    from the materialised cache file rather than re-fetching.

    Read I/O happens in a thread so the event loop isn't blocked on each
    read. The file handle lives in this generator's frame for the lifetime
    of the consumer — if the consumer aborts mid-iteration the handle
    closes via the finally block on GC.
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
        paths: Iterable[str] | None = None,
    ) -> str:
        """Ensure the narrowed tarball for (conn, owner, repo, sha, paths) is in storage.

        Returns the storage key. Concurrent calls for the same
        (conn, sha, paths) coalesce — exactly one performs the fetch,
        the others wait and re-use the result.

        `paths` is the union of repo-rooted paths (working_directory ∪
        trigger_prefixes) the caller wants in the tarball. None or empty
        means "whole repo" (full clone, all blobs). Two callers must
        agree on the same path set to share a cache entry — typically
        achieved by pre-computing the union at the poll-cycle level.
        """
        ph = git_fetch.paths_hash(paths)
        cache_key = f"{conn.id}:{owner}/{repo}@{sha}#{ph}"
        if cache_key in self._known:
            return self._known[cache_key]

        lock = self._key_lock(cache_key)
        async with lock:
            # Re-check after acquiring the lock — peer may have populated.
            if cache_key in self._known:
                return self._known[cache_key]

            storage_key = vcs_archive_key(str(conn.id), owner, repo, sha, ph)
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
                    paths_hash=ph,
                )
                return storage_key
            except ObjectNotFoundError:
                pass

            # Miss → fetch + upload.
            await self._fetch_and_upload(conn, owner, repo, sha, paths, storage_key)
            self._known[cache_key] = storage_key
            return storage_key

    async def _fetch_and_upload(
        self,
        conn: VCSConnection,
        owner: str,
        repo: str,
        sha: str,
        paths: Iterable[str] | None,
        storage_key: str,
    ) -> None:
        """Sparse-fetch from VCS via dulwich and stream-upload to object storage.

        Wraps the clone in a TemporaryDirectory so the on-disk dulwich
        object store is cleaned up regardless of success. The clone dir
        name is prefixed `vcs-clone-` so `_ensure_tmpdir_space` can
        identify and sweep stragglers from prior pod crashes.

        Transactional w.r.t. the storage cache: if upload raises after
        partially writing, we best-effort `delete(storage_key)` so a
        future `head()` won't return OK on a truncated tarball.
        """
        storage = get_storage()
        tmpdir = _resolve_tmpdir()
        # Sweep stale orphans before reserving more disk. statvfs is cheap;
        # the sweep only does real work when we're below the threshold.
        await asyncio.to_thread(_ensure_tmpdir_space, tmpdir)

        clone_parent = await asyncio.to_thread(
            tempfile.mkdtemp, prefix=_CLONE_DIR_PREFIX, dir=tmpdir
        )
        try:
            try:
                bytes_uploaded = await git_fetch.sparse_archive_to_storage(
                    conn,
                    owner,
                    repo,
                    sha,
                    paths,
                    storage_key,
                    clone_dir=clone_parent,
                )
            except Exception as e:
                # Partial upload: a future head() might return OK on a
                # truncated key. Best-effort delete so the cache stays
                # consistent. We swallow secondary errors — re-raise the
                # original.
                logger.warning(
                    "VCS archive fetch/upload failed; deleting partial cache entry",
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
                bytes_uploaded=bytes_uploaded,
                storage_key=storage_key,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, clone_parent, True)


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
