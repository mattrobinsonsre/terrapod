"""Obtaining a platform tool the **API** itself runs (#1208, #2589).

Two tools are executed by the API rather than by a runner: `opa`, to validate
Rego when a policy set is written, and `syft`, to inventory a cached artifact
into an SBOM at admission. They want identical machinery and had none in common
until this module -- `api_opa` grew it first, and copying it for syft would have
meant two places to fix the next thing found in it.

Four properties, each of which cost something to learn:

**Fetched through the binary cache, never from upstream directly.** The cache is
the one thing that can reach upstream on a deployment where nothing else can, it
stores the artifact so a second API pod and every restart are served from object
storage, and an install sealed later still holds what it already fetched. It
also means verification happens in exactly one place (`verify_platform_tool`),
rather than here as well.

**In-process, not over the API's own HTTP surface.** `get_or_cache_binary` is an
ordinary async function, so there is no loopback request and no credential the
API would have to mint for itself.

**A sealed node does not fetch** (GHSA-rfxh-5gwg-px2g). Checked *after* the
on-disk check, so a binary already on the PVC is still used: sealing stops the
fetch, not the cache.

**The PVC, not `/tmp`.** `/tmp` on the API pod is RAM-backed and these binaries
are tens of megabytes, which is what rule 14 exists to keep out of memory.
"""

from __future__ import annotations

import asyncio
import os
import platform
import stat
import tempfile
from pathlib import Path

import structlog

from terrapod.config import settings
from terrapod.services.platform_tools import SPECS, configured_version

logger = structlog.get_logger(__name__)

#: One lock and one memo per tool. Per tool rather than one shared lock so a
#: slow syft fetch cannot block a policy write waiting on opa.
_locks: dict[str, asyncio.Lock] = {}
_cached: dict[str, str] = {}


def _lock_for(tool: str) -> asyncio.Lock:
    lock = _locks.get(tool)
    if lock is None:
        lock = _locks[tool] = asyncio.Lock()
    return lock


def tool_dir() -> Path:
    """Where to keep the binaries.

    The attached ephemeral PVC when one is configured, falling back to the system
    default for local dev and tests, where there is no PVC and nothing at stake.
    """
    configured = settings.vcs.tmpdir
    base = configured if configured and os.path.isdir(configured) else tempfile.gettempdir()
    return Path(base) / "terrapod-tools"


def _extract_member(archive: Path, member: str, dest: Path) -> None:
    """Pull one named member out of a tarball onto `dest`.

    Synchronous and called in a thread: `tarfile` is on the no-sync-work-in-async
    blocker list, and these archives are tens of megabytes.

    Extracts by NAME rather than extracting the archive, so a member path in the
    tarball can never decide where a file lands -- the traversal class that
    `extractall` is repeatedly patched for cannot arise.
    """
    import tarfile

    with tarfile.open(archive, "r:gz") as tar:
        try:
            source = tar.extractfile(member)
        except KeyError:
            source = None
        if source is None:
            raise FileNotFoundError(f"{archive.name} has no member {member!r}")
        with source, dest.open("wb") as fh:
            while chunk := source.read(1024 * 1024):
                fh.write(chunk)


async def _fetch(tool: str, version: str, dest: Path) -> None:
    """Put `tool` on the PVC, via the binary cache, unpacking if it needs it."""
    from terrapod.db.session import get_db_session
    from terrapod.services.binary_cache_service import cached_binary_key, get_or_cache_binary
    from terrapod.storage import get_storage

    arch = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
    storage = get_storage()

    # Populates the cache on a miss and verifies the checksum; a hit touches
    # `last_accessed_at` so retention does not reap a binary this pod relies on.
    # Its own short-lived session: nothing is held open across the transfer.
    async with get_db_session() as db:
        await get_or_cache_binary(db, storage, tool, version, "linux", arch)
        # The row's recorded key, never a derived one: an operator-uploaded tool
        # lives under `uploads/` and deriving would look under `cache/` and find
        # nothing -- reporting the tool unavailable on exactly the sealed
        # deployment that uploaded it because it could not fetch it (#2599).
        key = await cached_binary_key(db, tool, version, "linux", arch)
    if key is None:
        raise FileNotFoundError(f"{tool} was cached but no row names its object")

    # A UNIQUE scratch file per fetch, not a fixed `<dest>.partial`. Two
    # concurrent fetchers would otherwise stream into the same path, and the
    # first `replace(dest)` renames it out from under the second -- which then
    # dies on `stat()` with ENOENT despite having just downloaded it
    # successfully. `mkstemp` in the destination directory keeps the final
    # `replace` on one filesystem, so it stays atomic.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f"{dest.name}.", suffix=".partial")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tmp.open("wb") as fh:
            async for chunk in storage.get_stream(key):
                await asyncio.to_thread(fh.write, chunk)

        spec = SPECS[tool]
        if spec.archive == "targz":
            # The cached object is the publisher's archive; the executable is one
            # member of it.
            unpacked = Path(f"{tmp_name}.bin")
            try:
                await asyncio.to_thread(_extract_member, tmp, spec.member, unpacked)
                unpacked.chmod(unpacked.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                unpacked.replace(dest)
            except BaseException:
                unpacked.unlink(missing_ok=True)
                raise
        elif spec.archive == "raw":
            tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            # Last writer wins, and every writer wrote a binary the cache
            # verified, so whichever lands is correct.
            tmp.replace(dest)
        else:
            raise NotImplementedError(
                f"{tool} is packaged as {spec.archive!r}, which this module cannot unpack. "
                "Only the API-side tools need to be handled here."
            )
    finally:
        tmp.unlink(missing_ok=True)


async def tool_binary(tool: str, *, sealed_note: str, failure_note: str) -> str | None:
    """Path to a usable `tool`, or None if it could not be obtained.

    **None is a normal outcome**, not an exception: every caller degrades rather
    than failing the request, and the notes are how each one says what that
    degradation means in its own terms.
    """
    if cached := _cached.get(tool):
        return cached

    async with _lock_for(tool):
        if cached := _cached.get(tool):  # another waiter won while we queued
            return cached

        version = configured_version(tool)
        dest = tool_dir() / f"{tool}-{version}"
        if dest.exists():
            _cached[tool] = str(dest)
            return _cached[tool]

        if settings.registry.cache_only:
            logger.info(sealed_note, tool=tool, version=version)
            return None

        try:
            await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
            await _fetch(tool, version, dest)
        except Exception as exc:  # noqa: BLE001 - every failure degrades the same way
            logger.warning(failure_note, tool=tool, version=version, error=str(exc))
            return None

        logger.info("Platform tool ready", tool=tool, version=version, path=str(dest))
        _cached[tool] = str(dest)
        return _cached[tool]


def _reset_for_tests() -> None:
    _cached.clear()
    _locks.clear()
