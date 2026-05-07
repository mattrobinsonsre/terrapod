"""Diff two configuration version tarballs.

Used by `/api/v2/configuration-versions/diff` (Terrapod-only endpoint
backing the workspace UI's "compare two versions" view). Pure I/O +
text diffing; no async networking on the hot path.

Approach
--------
* Stream both tarballs from object storage to ephemeral on-disk temp
  files (same PVC the VCS-archive cache uses; bounded by per-blob
  buffering, never the whole tarball in memory).
* Walk each tarball, build {path → bytes} maps. Skip directories
  and non-regular files (symlinks, devices, etc.) — they're noise
  for a Terraform-config diff.
* Cap per-file size and per-pair total size. A CV is supposed to be
  HCL + small support files; a multi-hundred-MB tarball is almost
  certainly a misuse and we'd rather refuse than OOM the API pod.
* For binary files (heuristic: contains a NUL byte in the first
  ~8 KiB), report only `binary-changed` rather than rendering
  meaningless text diffs.
* Text files run through `difflib.unified_diff` with three lines of
  context — same default git uses.

The output shape is one entry per changed file, with the per-file
unified diff text. The caller (UI) renders.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import tarfile
import tempfile
from typing import Any

from terrapod.config import settings
from terrapod.logging_config import get_logger
from terrapod.storage import get_storage

logger = get_logger(__name__)

# Per-file ceiling — anything larger we report as "too large to diff"
# rather than reading into memory. 1 MiB covers any realistic .tf file.
_MAX_FILE_BYTES = 1 << 20

# Per-pair ceiling on total bytes loaded across both sides. Stops a
# pathologically large CV from blowing the api pod's resident memory.
# At 32 MiB, even a fully-disjoint diff stays comfortably below the
# default 512 MiB pod limit.
_MAX_TOTAL_BYTES = 32 << 20

# How many context lines to include in the unified diff. Same default
# `git diff` uses.
_DIFF_CONTEXT = 3


class CVTarballMissing(Exception):
    """Raised when a CV's stored tarball can't be retrieved.

    Typical cause: retention swept the bytes but the row stayed
    referenced by a run. The diff endpoint surfaces this as a 410
    so the UI can render a clear "this version is no longer
    downloadable" state instead of a generic 500.
    """


class DiffTooLarge(Exception):
    """Raised when the combined uncompressed size exceeds `_MAX_TOTAL_BYTES`.

    The endpoint surfaces this as a 413 (Payload Too Large) so the UI
    can show "this diff is too big to render in the browser" rather
    than silently truncating.
    """


def _resolve_tmpdir() -> str | None:
    """Reuse the VCS tmpdir setting — same PVC, same sweep behaviour."""
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


async def _stream_to_tempfile(storage_key: str) -> str:
    """Materialise a stored tarball into an ephemeral temp file.

    Caller owns the path and must delete it. Uses `mkstemp` + buffered
    `os.fdopen` so chunk writes loop internally for short writes.
    """
    storage = get_storage()
    tmpdir = _resolve_tmpdir()
    fd, path = await asyncio.to_thread(tempfile.mkstemp, suffix=".cv.tar.gz", dir=tmpdir)
    f = await asyncio.to_thread(os.fdopen, fd, "wb")
    try:
        try:
            async for chunk in storage.get_stream(storage_key):
                if not chunk:
                    continue
                await asyncio.to_thread(f.write, chunk)
        finally:
            await asyncio.to_thread(f.close)
    except Exception:
        try:
            await asyncio.to_thread(os.unlink, path)
        except FileNotFoundError:
            pass
        raise
    return path


def _looks_binary(b: bytes) -> bool:
    """Heuristic: NUL byte in the first ~8 KiB → binary.

    Same heuristic git uses (`git diff` falls back to "Binary files differ").
    Cheap, correct in practice for HCL / .tfvars / .json / .yaml /
    .lock.hcl source.
    """
    return b"\x00" in b[:8192]


def _read_tarball(path: str) -> tuple[dict[str, bytes], list[str], int]:
    """Walk the tarball at `path`, return (files, oversized_paths, total_bytes).

    `files`: regular-file path → bytes. Skips dirs/symlinks/devices.
    `oversized_paths`: paths exceeding `_MAX_FILE_BYTES`. Reported in
    the diff response so the UI can show "(too large to diff)".
    `total_bytes`: sum of every file's bytes (for the per-pair ceiling).
    """
    files: dict[str, bytes] = {}
    oversized: list[str] = []
    total = 0
    with tarfile.open(path, mode="r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                # Directories and symlinks aren't useful in a config
                # diff; skip without a fuss.
                continue
            if member.size > _MAX_FILE_BYTES:
                oversized.append(member.name)
                total += member.size
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            data = f.read(_MAX_FILE_BYTES + 1)
            if len(data) > _MAX_FILE_BYTES:
                oversized.append(member.name)
                total += member.size
                continue
            files[member.name] = data
            total += len(data)
    return files, oversized, total


async def diff_tarballs(from_key: str, to_key: str) -> dict[str, Any]:
    """Materialise + diff two stored tarballs.

    Returns a dict the API serialises directly:

        {
          "files": [
            {"path": "main.tf", "type": "modified", "diff": "..."},
            {"path": "vars.tf", "type": "added", "diff": "..."},
            {"path": "old.tf", "type": "removed", "diff": "..."},
            {"path": "image.png", "type": "binary-changed"},
            ...
          ],
          "oversized": ["modules/big.zip"],   # files skipped per-file cap
          "total-files-changed": 4,
        }

    Raises `DiffTooLarge` if combined bytes exceed `_MAX_TOTAL_BYTES`,
    or `CVTarballMissing` (via the storage exception bubbling up the
    caller) if either side's bytes are gone.
    """
    # Fetch both sides in parallel — they're independent network reads.
    from_path, to_path = await asyncio.gather(
        _stream_to_tempfile(from_key),
        _stream_to_tempfile(to_key),
    )
    try:
        from_files, from_oversized, from_total = await asyncio.to_thread(_read_tarball, from_path)
        to_files, to_oversized, to_total = await asyncio.to_thread(_read_tarball, to_path)

        if from_total + to_total > _MAX_TOTAL_BYTES:
            raise DiffTooLarge(
                f"combined uncompressed size {from_total + to_total} bytes exceeds "
                f"{_MAX_TOTAL_BYTES} byte limit"
            )

        all_paths = sorted(set(from_files) | set(to_files))
        results: list[dict[str, Any]] = []
        for path in all_paths:
            from_bytes = from_files.get(path)
            to_bytes = to_files.get(path)

            if from_bytes is None:
                kind = "added"
                old, new = b"", to_bytes or b""
            elif to_bytes is None:
                kind = "removed"
                old, new = from_bytes, b""
            elif from_bytes == to_bytes:
                continue  # unchanged — skip
            else:
                kind = "modified"
                old, new = from_bytes, to_bytes

            if _looks_binary(old) or _looks_binary(new):
                results.append({"path": path, "type": "binary-changed"})
                continue

            try:
                old_text = old.decode("utf-8")
                new_text = new.decode("utf-8")
            except UnicodeDecodeError:
                results.append({"path": path, "type": "binary-changed"})
                continue

            diff_lines = list(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=path,
                    tofile=path,
                    n=_DIFF_CONTEXT,
                )
            )
            results.append({"path": path, "type": kind, "diff": "".join(diff_lines)})

        return {
            "files": results,
            "oversized": sorted(set(from_oversized) | set(to_oversized)),
            "total-files-changed": len(results),
        }
    finally:
        for p in (from_path, to_path):
            try:
                await asyncio.to_thread(os.unlink, p)
            except FileNotFoundError:
                pass
