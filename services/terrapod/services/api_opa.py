"""Obtain OPA for the API's write-time Rego check (#1208).

The API image no longer bakes OPA in. It fetches it through the same binary
cache the runner uses and keeps it on the ephemeral PVC, so the version is an
operator-set Helm value rather than something frozen into an image.

**This path degrades; it does not fail closed.** That is the opposite of the
runner's policy gate, and deliberately so — the two uses are not the same thing:

    runner   `opa eval`   the gate itself         a run that cannot be evaluated
                                                  must not proceed
    API      `opa check`  write-time Rego syntax  a policy saved without its
                                                  syntax checked still fails
                                                  closed later, at eval

So when OPA is unavailable here the policy-set write reports that validation is
unavailable and proceeds. The cost is a syntax error surfacing at the next run
instead of at save time; the cost of the alternative is an operator unable to
edit any policy because an unrelated fetch failed.

Fetched lazily on first use rather than at startup: an install that never writes
a policy set never pays for it, and a slow or unreachable upstream cannot delay
the API becoming ready.
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
from terrapod.services.platform_tools import (
    configured_version,
)

logger = structlog.get_logger(__name__)

#: Serialises concurrent first-uses so two policy writes don't both download.
_lock = asyncio.Lock()
_cached_path: str | None = None


def _tool_dir() -> Path:
    """Where to keep the binary.

    The attached ephemeral PVC when one is configured — `/tmp` on the API pod is
    RAM-backed and OPA is ~50MB, which is exactly the kind of thing rule 14
    exists to keep out of memory. Falls back to the system default for local dev
    and tests, where there is no PVC and nothing at stake.
    """
    configured = settings.vcs.tmpdir
    base = configured if configured and os.path.isdir(configured) else tempfile.gettempdir()
    return Path(base) / "terrapod-tools"


async def _download(version: str, dest: Path) -> None:
    """Put OPA on the PVC, fetched **through the binary cache**.

    Not from upstream directly, which is what this did before. Every fetch goes
    through the cache, and that is not a preference: the cache is the one thing
    that can reach upstream on a deployment where nothing else can, it stores
    the artifact so a second API pod and every restart are served from object
    storage rather than GitHub, and an install that is sealed later still holds
    what it already fetched.

    **In-process, not over the API's own HTTP surface.** `get_or_cache_binary`
    is an ordinary async function, so the cache is driven by calling it and the
    bytes are then read from object storage by key -- no loopback request, and
    no credential the API would have to mint for itself.

    It also removes a duplicated upstream path. The cache applies the same
    checksum gate this function used to apply itself (`verify_platform_tool`,
    via `artifact_verification`), so verification is no longer repeated here --
    there is exactly one place that decides whether an OPA binary is
    trustworthy, and it is the same one the runner's fetch goes through.

    Streamed to disk in a worker thread -- never held in memory, and never
    blocking the event loop on the write (rule 13).
    """
    from terrapod.db.session import get_db_session
    from terrapod.services.binary_cache_service import get_or_cache_binary
    from terrapod.storage import get_storage
    from terrapod.storage.keys import binary_cache_key

    arch = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
    storage = get_storage()

    # Populates the cache on a miss and verifies the checksum; a hit touches
    # `last_accessed_at` so retention does not reap a binary this pod relies on.
    # Its own short-lived session: nothing is held open across the transfer.
    async with get_db_session() as db:
        await get_or_cache_binary(db, storage, "opa", version, "linux", arch)

    key = binary_cache_key("opa", version, "linux", arch)

    # A UNIQUE scratch file per fetch, not a fixed `<dest>.partial`. Two
    # concurrent fetchers (two policy-set writes arriving together, or two
    # pytest-xdist workers) would otherwise stream into the same path, and the
    # first `replace(dest)` renames it out from under the second — which then
    # dies on `stat()` with ENOENT and reports OPA unavailable despite having
    # just downloaded it successfully. `mkstemp` in the destination directory
    # keeps the final `replace` on one filesystem, so it stays atomic.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f"{dest.name}.", suffix=".partial")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tmp.open("wb") as fh:
            async for chunk in storage.get_stream(key):
                await asyncio.to_thread(fh.write, chunk)

        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # Last writer wins, and every writer wrote a binary the cache verified,
        # so whichever lands is correct.
        tmp.replace(dest)
    except BaseException:
        # Don't leave scratch files behind on the PVC when a fetch fails.
        tmp.unlink(missing_ok=True)
        raise


async def opa_binary() -> str | None:
    """Path to a usable `opa`, or None if it could not be obtained.

    None is a normal outcome the caller is expected to handle by reporting that
    validation is unavailable — see the module docstring.
    """
    global _cached_path
    if _cached_path:
        return _cached_path

    async with _lock:
        if _cached_path:  # another waiter won while we queued
            return _cached_path

        version = configured_version("opa")
        dest = _tool_dir() / f"opa-{version}"
        if dest.exists():
            _cached_path = str(dest)
            return _cached_path

        # GHSA-rfxh-5gwg-px2g. A sealed node answers a cache miss from what it
        # already holds and never reaches upstream -- `docs/deployment-network-
        # isolation.md` calls that "a hard guarantee that the caches never reach
        # upstream". This module was the one cache that did not honour it, so a
        # policy-set write on an air-gapped deployment made an outbound request.
        #
        # Checked AFTER `dest.exists()` above, so a binary already on the PVC is
        # still used: sealing stops the fetch, not the cache.
        if settings.registry.cache_only:
            logger.info(
                "OPA is not cached and this node is sealed (registry.cache_only), so "
                "write-time Rego validation is unavailable rather than fetched from "
                "upstream. Rego is still checked at evaluation time on the runner, "
                "which fails closed.",
                version=version,
            )
            return None

        try:
            await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
            await _download(version, dest)
        except Exception as exc:  # noqa: BLE001 — every failure degrades the same way
            logger.warning(
                "could not obtain OPA for write-time Rego validation — policy-set "
                "writes will report validation as unavailable and proceed. Rego is "
                "still checked at evaluation time on the runner, which fails closed.",
                version=version,
                error=str(exc),
            )
            return None

        logger.info("OPA ready for Rego validation", version=version, path=str(dest))
        _cached_path = str(dest)
        return _cached_path


def _reset_for_tests() -> None:
    """Clear the memoised path. Tests only."""
    global _cached_path
    _cached_path = None
