"""Utilities for repacking VCS archive tarballs.

GitHub and GitLab archive downloads wrap all files in a top-level directory
(e.g. ``owner-repo-sha/``).  Terraform/tofu expects module tarballs and
workspace configs to have .tf files at the root.  The helpers here strip
that wrapper so downloaded archives are usable as-is.

Two interfaces:
- bytes-in/bytes-out (legacy, used by the registry path) — buffers the full
  tarball in memory, only safe for small archives.
- file-in/file-out (preferred for VCS poll cycles) — uses sequential streaming
  tarfile mode (``r|gz`` / ``w|gz``) so a multi-hundred-MB monorepo tarball
  never lands in process memory.
"""

import asyncio
import io
import tarfile


async def strip_archive_top_level_dir_async(archive: bytes) -> bytes:
    """Async wrapper that runs the sync repack in a thread.

    Callers on an asyncio event loop (request handlers, scheduler tasks)
    must use this form — the sync variant does CPU-heavy tarfile work and
    will starve the loop on multi-MB archives.
    """
    return await asyncio.to_thread(strip_archive_top_level_dir, archive)


def strip_archive_top_level_dir(archive: bytes) -> bytes:
    """Repack a gzipped tarball, stripping the single top-level directory.

    ``owner-repo-sha/variables.tf`` becomes ``variables.tf``, etc.
    If the archive has no common top-level directory, it is returned unchanged.
    """
    in_buf = io.BytesIO(archive)
    out_buf = io.BytesIO()

    with (
        tarfile.open(fileobj=in_buf, mode="r:gz") as src,
        tarfile.open(fileobj=out_buf, mode="w:gz") as dst,
    ):
        for member in src.getmembers():
            # Strip first path component: "owner-repo-sha/file" -> "file"
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue  # skip the top-level directory entry itself
            member.name = parts[1]
            if member.isfile():
                f = src.extractfile(member)
                if f:
                    dst.addfile(member, f)
            else:
                dst.addfile(member)

    return out_buf.getvalue()


async def strip_archive_top_level_dir_file_async(src_path: str, dst_path: str) -> None:
    """Async wrapper around the file-to-file streaming strip.

    Use this for VCS-poll-cycle archives where the input may be hundreds of
    MB. The sync helper runs in a thread so the asyncio loop isn't blocked.
    """
    await asyncio.to_thread(strip_archive_top_level_dir_file, src_path, dst_path)


def strip_archive_top_level_dir_file(src_path: str, dst_path: str) -> None:
    """Repack a gzipped tarball file-to-file, stripping the single top-level directory.

    Uses tarfile streaming mode (``r|gz`` for read, ``w|gz`` for write) which
    processes entries sequentially without building an in-memory index. Memory
    use is bounded by the per-entry buffer (one file at a time), so a 300 MB
    monorepo tarball costs single-digit MB of RAM rather than 300+ MB.

    Same path-rewrite contract as ``strip_archive_top_level_dir``: the first
    path component (``owner-repo-sha/``) is dropped from every member name.
    """
    with (
        open(src_path, "rb") as src_f,
        open(dst_path, "wb") as dst_f,
        tarfile.open(fileobj=src_f, mode="r|gz") as src,
        tarfile.open(fileobj=dst_f, mode="w|gz") as dst,
    ):
        for member in src:
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue  # skip the top-level directory entry itself
            member.name = parts[1]
            if member.isfile():
                f = src.extractfile(member)
                if f:
                    dst.addfile(member, f)
            else:
                dst.addfile(member)
