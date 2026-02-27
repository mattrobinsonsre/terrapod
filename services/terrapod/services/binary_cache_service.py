"""Service layer for terraform/tofu CLI binary caching.

Pull-through cache: on first request, downloads the binary from upstream
(releases.hashicorp.com for terraform, GitHub releases for tofu),
stores it in object storage, and returns a presigned download URL.
Subsequent requests serve from cache.
"""

import hashlib

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import CachedBinary
from terrapod.logging_config import get_logger
from terrapod.storage.keys import binary_cache_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

VALID_TOOLS = {"terraform", "tofu"}
VALID_OS = {"linux", "darwin", "windows", "freebsd", "openbsd", "solaris"}
VALID_ARCH = {"amd64", "arm64", "arm", "386"}


async def get_or_cache_binary(
    db: AsyncSession,
    storage: ObjectStore,
    tool: str,
    version: str,
    os_: str,
    arch: str,
) -> str:
    """Get a cached binary or fetch from upstream on cache miss.

    Returns a presigned download URL.
    """
    if tool not in VALID_TOOLS:
        raise ValueError(f"Invalid tool: {tool}. Must be one of {VALID_TOOLS}")

    # Check cache
    cached = await _get_cached(db, tool, version, os_, arch)
    if cached is not None:
        key = binary_cache_key(tool, version, os_, arch)
        presigned = await storage.presigned_get_url(key)
        return presigned.url

    # Cache miss — fetch from upstream
    logger.info(
        "Binary cache miss, fetching from upstream",
        tool=tool,
        version=version,
        os=os_,
        arch=arch,
    )

    if tool == "terraform":
        data, download_url = await _fetch_terraform_binary(version, os_, arch)
    else:
        data, download_url = await _fetch_tofu_binary(version, os_, arch)

    # Store in object storage
    key = binary_cache_key(tool, version, os_, arch)
    await storage.put(key, data, content_type="application/zip")

    # Record in database
    shasum = hashlib.sha256(data).hexdigest()
    entry = CachedBinary(
        tool=tool,
        version=version,
        os=os_,
        arch=arch,
        shasum=shasum,
        download_url=download_url,
    )
    db.add(entry)
    await db.flush()

    logger.info(
        "Binary cached",
        tool=tool,
        version=version,
        os=os_,
        arch=arch,
        size_bytes=len(data),
    )

    presigned = await storage.presigned_get_url(key)
    return presigned.url


async def list_cached_binaries(
    db: AsyncSession,
    tool: str | None = None,
) -> list[CachedBinary]:
    """List cached binaries, optionally filtered by tool."""
    stmt = select(CachedBinary).order_by(CachedBinary.cached_at.desc())
    if tool is not None:
        stmt = stmt.where(CachedBinary.tool == tool)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def purge_binary(
    db: AsyncSession,
    storage: ObjectStore,
    tool: str,
    version: str,
) -> int:
    """Purge all cached binaries for a tool+version. Returns count deleted."""
    result = await db.execute(
        select(CachedBinary).where(
            CachedBinary.tool == tool,
            CachedBinary.version == version,
        )
    )
    entries = list(result.scalars().all())
    for entry in entries:
        key = binary_cache_key(tool, version, entry.os, entry.arch)
        await storage.delete(key)
        await db.delete(entry)

    await db.flush()
    return len(entries)


async def warm_binary(
    db: AsyncSession,
    storage: ObjectStore,
    tool: str,
    version: str,
    os_: str,
    arch: str,
) -> str:
    """Pre-warm a binary into the cache. Returns presigned URL."""
    return await get_or_cache_binary(db, storage, tool, version, os_, arch)


# --- Internal helpers ---


async def _get_cached(
    db: AsyncSession,
    tool: str,
    version: str,
    os_: str,
    arch: str,
) -> CachedBinary | None:
    result = await db.execute(
        select(CachedBinary).where(
            CachedBinary.tool == tool,
            CachedBinary.version == version,
            CachedBinary.os == os_,
            CachedBinary.arch == arch,
        )
    )
    return result.scalars().first()


async def _fetch_terraform_binary(version: str, os_: str, arch: str) -> tuple[bytes, str]:
    """Download terraform binary from releases.hashicorp.com."""
    cfg = settings.registry.binary_cache
    filename = f"terraform_{version}_{os_}_{arch}.zip"
    url = f"{cfg.terraform_mirror_url}/{version}/{filename}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    return resp.content, url


async def _fetch_tofu_binary(version: str, os_: str, arch: str) -> tuple[bytes, str]:
    """Download tofu binary from GitHub releases."""
    cfg = settings.registry.binary_cache
    filename = f"tofu_{version}_{os_}_{arch}.zip"
    url = f"{cfg.tofu_mirror_url}/v{version}/{filename}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    return resp.content, url
