"""Pricesheet cache service for cost estimation (#871).

Mirrors the configured pricesheet (``cost_estimation.prices_url``, a gzipped
sheet) into object storage so the runner (plan path) and the API (state/workspace
path) read a cached copy — no per-run egress. It defaults to Terrapod's own
self-generated, multi-region pricesheet (#893/#1025), and also accepts an
OpenInfraQuote-compatible CSV; the reader auto-detects the format by content, so
this cache is format-agnostic (it stores the decompressed bytes verbatim). This
is a **pull-through** cache, exactly like Terrapod's binary/provider caches: the
sheet is fetched **on demand** when it's missing or stale, not on a schedule. No
operator scheduling, no interval to tune. Air-gapped deployments pre-seed the
cached object or point ``cost_estimation.prices_url`` at an internal mirror.

Staleness comes straight from object storage (the cached object's
``last_modified``), so there's no separate timestamp store. Refresh is
best-effort: if an upstream fetch fails but a cached copy exists, the stale copy
keeps serving — a transient outage never breaks a run.

The pricesheet is ~1.8 MB gzipped / ~20 MB decompressed, so download, gunzip,
and storage streaming are kept off the event loop (rule 13) and land on the
attached PVC, never ``/tmp`` (rule 14) — the same discipline as
``provider_cache_service``.

The engine that reads this CSV is :mod:`terrapod.services.cost` (a native
OpenInfraQuote-compatible port); this module only manages the cached data.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from terrapod.config import settings
from terrapod.services.cost import pricesheet_db
from terrapod.storage.keys import cost_pricesheet_db_key
from terrapod.storage.protocol import ObjectStore

logger = structlog.get_logger(__name__)

_DOWNLOAD_TIMEOUT = 300.0
_CHUNK = 1024 * 1024  # 1 MiB
# How long a cached pricesheet is considered fresh before an on-demand refetch.
# Cloud prices move slowly; a day-old sheet is fine and keeps egress rare.
_STALE_AFTER = timedelta(hours=24)


def _resolve_ephemeral_tmpdir() -> str | None:
    """Path to a writable mount on attached storage (PVC), or ``None``.

    Mirrors ``provider_cache_service._resolve_ephemeral_tmpdir`` /
    ``cv_diff_service._resolve_tmpdir``: the Helm chart mounts a per-pod
    ephemeral PVC at ``settings.vcs.tmpdir`` (default ``/var/lib/terrapod/tmp``).
    ``None`` falls back to the system default for local dev and tests.
    """
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


def _append_chunk(path: str, chunk: bytes) -> None:
    with open(path, "ab") as f:
        f.write(chunk)


def _gunzip_file(gz_path: str, out_path: str) -> None:
    with gzip.open(gz_path, "rb") as src, open(out_path, "wb") as dst:
        shutil.copyfileobj(src, dst, length=_CHUNK)


async def _file_chunks(path: str) -> AsyncIterator[bytes]:
    """Yield a file's bytes in chunks, reading off the event loop (rule 13)."""
    fh = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(fh.read, _CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        await asyncio.to_thread(fh.close)


def _build_db(sheet_path: str, db_path: str) -> int:
    """Stream a decompressed sheet file into a SQLite index (sync, off-loop)."""
    with open(sheet_path) as fp:
        return pricesheet_db.build_index(fp, db_path)


async def refresh_pricesheet(storage: ObjectStore) -> int:
    """Fetch the configured pricesheet, build a SQLite index, and cache it (#1034).

    Streams ``cost_estimation.prices_url`` (gzipped CSV / Terrapod YAML) to a PVC
    tempfile, gunzips it off-loop, **streams it into a SQLite index**
    (:mod:`pricesheet_db` — bounded memory even for the ~260k-product multi-region
    sheet), then stores the ``.sqlite`` in object storage at
    :func:`cost_pricesheet_db_key`. Both the API and runner query that file off
    disk instead of parsing the whole sheet. Returns the stored DB size in bytes.
    Raises on any download/build failure (the store only happens on success, so a
    previously cached copy is left intact).
    """
    url = settings.cost_estimation.prices_url
    tmpdir = _resolve_ephemeral_tmpdir()

    gz_fd, gz_path = await asyncio.to_thread(tempfile.mkstemp, suffix=".gz", dir=tmpdir)
    await asyncio.to_thread(os.close, gz_fd)
    sheet_fd, sheet_path = await asyncio.to_thread(tempfile.mkstemp, suffix=".sheet", dir=tmpdir)
    await asyncio.to_thread(os.close, sheet_fd)
    db_fd, db_path = await asyncio.to_thread(tempfile.mkstemp, suffix=".sqlite", dir=tmpdir)
    await asyncio.to_thread(os.close, db_fd)
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # trust_env (httpx default) routes via the configured proxy/CA (#592).
            async with client.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(_CHUNK):
                    await asyncio.to_thread(_append_chunk, gz_path, chunk)

        await asyncio.to_thread(_gunzip_file, gz_path, sheet_path)
        # SQLite can't be built into via a stream, so build to the temp file then
        # (over)write the cache atomically at the DB key.
        await asyncio.to_thread(os.unlink, db_path)  # build_index creates fresh
        rows = await asyncio.to_thread(_build_db, sheet_path, db_path)
        size = await asyncio.to_thread(os.path.getsize, db_path)

        await storage.put_stream(
            cost_pricesheet_db_key(),
            _file_chunks(db_path),
            content_type="application/x-sqlite3",
        )
        logger.info("cost_pricesheet_refreshed", url=url, rows=rows, size_bytes=size)
        return size
    finally:
        for path in (gz_path, sheet_path, db_path):
            try:
                await asyncio.to_thread(os.unlink, path)
            except OSError:
                pass


async def _is_fresh(storage: ObjectStore) -> bool:
    """True when a cached pricesheet exists and is younger than the TTL."""
    if not await storage.exists(cost_pricesheet_db_key()):
        return False
    try:
        meta = await storage.head(cost_pricesheet_db_key())
    except Exception:  # noqa: BLE001 - any head failure => treat as stale
        return False
    modified = meta.last_modified
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    return datetime.now(UTC) - modified < _STALE_AFTER


async def ensure_pricesheet(storage: ObjectStore) -> bool:
    """Pull-through: ensure a usable cached pricesheet, refetching if stale.

    Returns ``True`` if a (possibly stale) sheet is available to serve. Fetches
    on demand when the cached object is missing or older than the TTL. If a
    refresh fails but a cached copy exists, the stale copy is served (best-effort
    freshness — a transient upstream outage never breaks a run); only returns
    ``False`` when nothing is cached and the fetch failed.
    """
    if await _is_fresh(storage):
        return True
    had_copy = await storage.exists(cost_pricesheet_db_key())
    try:
        await refresh_pricesheet(storage)
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort; fall back to any stale copy
        logger.warning("cost_pricesheet_refresh_failed", error=str(exc), served_stale=had_copy)
        return had_copy


async def pricesheet_available(storage: ObjectStore) -> bool:
    """Whether a cached pricesheet currently exists (no refetch)."""
    return await storage.exists(cost_pricesheet_db_key())


async def pricesheet_download_url(storage: ObjectStore) -> str:
    """Presigned download URL for the cached pricesheet (302 target for runners)."""
    presigned = await storage.presigned_get_url(cost_pricesheet_db_key())
    return presigned.url


def pricesheet_stream(storage: ObjectStore) -> AsyncIterator[bytes]:
    """Stream the cached pricesheet's bytes (for the API state/workspace path)."""
    return storage.get_stream(cost_pricesheet_db_key())


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def download_cached_to_file(storage: ObjectStore) -> str:
    """Stream the cached pricesheet CSV to a PVC tempfile; return its path.

    For the API's state/workspace cost path: the native engine reads the sheet
    as a local text file, and it's ~20 MB decompressed, so it lands on the
    attached PVC (rule 14), streamed off the event loop (rule 13). The caller
    owns the returned path and MUST unlink it. Raises ``ObjectNotFoundError`` if
    no sheet is cached (call :func:`ensure_pricesheet` first).
    """
    tmpdir = _resolve_ephemeral_tmpdir()
    fd, path = await asyncio.to_thread(tempfile.mkstemp, suffix=".sqlite", dir=tmpdir)
    await asyncio.to_thread(os.close, fd)
    try:
        async for chunk in storage.get_stream(cost_pricesheet_db_key()):
            await asyncio.to_thread(_append_chunk, path, chunk)
    except BaseException:
        await asyncio.to_thread(_safe_unlink, path)
        raise
    return path
