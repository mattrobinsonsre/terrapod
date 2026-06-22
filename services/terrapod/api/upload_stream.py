"""Shared helpers for streaming request bodies to the ephemeral PVC.

CLAUDE.md hard requirement #13 (no sync work in async handlers) and #14
(substantial tempfiles MUST land on the CSP-attached PVC, not the RAM-backed
`/tmp` emptyDir). Upload handlers that must *parse* the body — state metadata
(serial/lineage/md5), plan-JSON summarisation, module interface extraction —
cannot use the pure pass-through `storage.put_stream(request.stream())` form,
because they need the bytes available to read back. This module streams the
body to a *capped* tempfile on the PVC (`settings.vcs.tmpdir`) so the raw
upload never accumulates in the worker's heap, then exposes the file as a
constant-memory async chunk iterator for `storage.put_stream`.

Pure pass-through uploads (logs, plan/lock files, config tarballs) do NOT need
this — they stream `request.stream()` straight into `storage.put_stream`.
"""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request

from terrapod.config import settings

# Real-world terraform states and plan-JSON rarely exceed ~50 MB; 256 MiB is a
# generous upper bound that still leaves headroom on a small API pod. Matches
# the cap on the CLI state-content path (`tfe_v2.upload_state_content`). Bigger
# states/plans should be split — terraform itself struggles past multi-GB.
DEFAULT_UPLOAD_MAX_BYTES = 256 * 1024 * 1024


def resolve_ephemeral_tmpdir() -> str | None:
    """Resolve the API pod's ephemeral-storage PVC mount, or None.

    Matches `run_artifacts._resolve_ephemeral_tmpdir` /
    `cv_diff_service._resolve_tmpdir` / `vcs_archive_cache._resolve_tmpdir`.
    On the API pod `/tmp` is a RAM-backed `emptyDir{}`; tempfiles that can
    plausibly grow to tens of MB MUST land on the dedicated PVC at
    `settings.vcs.tmpdir`. Returning None falls back to the system default
    for local dev and tests.
    """
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


async def stream_to_tempfile(
    request: Request,
    *,
    suffix: str,
    max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES,
) -> tuple[str, int]:
    """Stream the request body to a capped tempfile on the ephemeral PVC.

    Returns ``(path, size_bytes)``. On success the *caller* owns the tempfile
    and MUST unlink it (use a ``try/finally``). On any failure — cap exceeded
    (HTTP 413) or client disconnect — the tempfile is closed and unlinked here
    so the PVC never leaks a partial upload.

    The cap is pre-checked against ``Content-Length`` (so an oversized client
    is refused before a tempfile is opened) and re-enforced while streaming
    (clients may lie about or omit Content-Length under chunked encoding). All
    writes happen in worker threads — the event loop is never blocked
    (CLAUDE.md #13).
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"upload too large: {declared} bytes > {max_bytes}-byte cap",
                )
        except ValueError:
            pass  # Malformed Content-Length — fall through to streamed enforcement.

    tmpdir = resolve_ephemeral_tmpdir()
    fd, tmp_path = await asyncio.to_thread(tempfile.mkstemp, suffix=suffix, dir=tmpdir)
    f = await asyncio.to_thread(os.fdopen, fd, "wb")
    received = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"upload exceeded the {max_bytes}-byte cap after {received} bytes",
                )
            await asyncio.to_thread(f.write, chunk)
        await asyncio.to_thread(f.flush)
        await asyncio.to_thread(f.close)
        return tmp_path, received
    except BaseException:
        # Cap exceeded / client disconnect / cancellation: never leak the
        # PVC tempfile, since the caller hasn't received the path to clean up.
        if not f.closed:
            try:
                await asyncio.to_thread(f.close)
            except OSError:
                pass
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass
        raise


async def file_chunks(path: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """Yield a file's bytes in bounded chunks for ``storage.put_stream``.

    Reads happen in worker threads (CLAUDE.md #13) so a large file never
    blocks the event loop. Constant memory: only ``chunk_size`` bytes are
    held at a time.
    """
    with open(path, "rb") as src:  # noqa: ASYNC230 -- bounded reads in a thread
        while True:
            buf = await asyncio.to_thread(src.read, chunk_size)
            if not buf:
                break
            yield buf
