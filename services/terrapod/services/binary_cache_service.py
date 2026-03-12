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

# Redis key prefix and TTL for version resolution cache
_VERSION_CACHE_PREFIX = "tp:version_resolve"
_VERSION_CACHE_TTL = 3600  # 1 hour


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


# --- Available Versions ---


async def list_available_versions(tool: str) -> list[str]:
    """List available stable versions for a tool, newest first.

    Fetches from upstream and caches in Redis for 1 hour.
    Returns both exact versions and major.minor shortcuts.
    """
    if tool not in VALID_TOOLS:
        raise ValueError(f"Invalid tool: {tool}. Must be one of {VALID_TOOLS}")

    # Check Redis cache
    cache_key = f"tp:versions:{tool}"
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        cached = await redis.get(cache_key)
        if cached:
            import json

            raw = cached.decode() if isinstance(cached, bytes) else cached
            return json.loads(raw)
    except Exception:
        pass

    if tool == "terraform":
        versions = await _fetch_terraform_versions()
    else:
        versions = await _fetch_tofu_versions()

    # Sort descending
    versions.sort(key=lambda v: [int(x) for x in v.split(".")], reverse=True)

    # Add major.minor shortcuts (deduplicated, in order)
    shortcuts: list[str] = []
    seen: set[str] = set()
    for v in versions:
        parts = v.split(".")
        if len(parts) >= 2:
            shortcut = f"{parts[0]}.{parts[1]}"
            if shortcut not in seen:
                seen.add(shortcut)
                shortcuts.append(shortcut)

    result = shortcuts + versions

    # Cache in Redis
    try:
        import json

        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        await redis.setex(cache_key, 3600, json.dumps(result))
    except Exception:
        pass

    return result


async def _fetch_terraform_versions() -> list[str]:
    """Fetch stable terraform versions from releases.hashicorp.com."""
    url = "https://releases.hashicorp.com/terraform/index.json"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    versions = []
    for v in data.get("versions", {}):
        if any(tag in v for tag in ("-alpha", "-beta", "-rc", "-dev")):
            continue
        parts = v.split(".")
        if len(parts) >= 3:
            versions.append(v)
    return versions


async def _fetch_tofu_versions() -> list[str]:
    """Fetch stable tofu versions from GitHub releases."""
    url = "https://api.github.com/repos/opentofu/opentofu/releases"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params={"per_page": 100})
        resp.raise_for_status()
        releases = resp.json()

    versions = []
    for release in releases:
        if release.get("prerelease", False):
            continue
        tag = release.get("tag_name", "")
        version = tag.lstrip("v")
        if any(t in version for t in ("-alpha", "-beta", "-rc")):
            continue
        parts = version.split(".")
        if len(parts) >= 3:
            versions.append(version)
    return versions


# --- Version Resolution ---


async def resolve_version(tool: str, partial_version: str) -> str:
    """Resolve a partial version (e.g. '1.9') to the latest exact version (e.g. '1.9.8').

    Handles:
    - Empty/None/"latest" → latest stable version
    - Two-component (x.y) → latest x.y.z patch
    - Three-component (x.y.z) → returned as-is

    Results are cached in Redis for 1 hour.
    """
    # Normalize empty, None, or "latest" to the latest stable release
    if not partial_version or partial_version.strip().lower() == "latest":
        partial_version = "latest"
        versions = await list_available_versions(tool)
        # list_available_versions returns shortcuts first, then full versions
        # Find the first full x.y.z version
        for v in versions:
            if len(v.split(".")) >= 3:
                return v
        raise ValueError(f"No stable versions found for {tool}")

    parts = partial_version.split(".")
    if len(parts) >= 3:
        return partial_version  # Already exact

    # Check Redis cache
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        cache_key = f"{_VERSION_CACHE_PREFIX}:{tool}:{partial_version}"
        cached = await redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        pass  # Redis unavailable — resolve without cache

    if tool == "terraform":
        resolved = await _resolve_terraform_version(partial_version)
    elif tool == "tofu":
        resolved = await _resolve_tofu_version(partial_version)
    else:
        return partial_version

    # Cache the result
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        await redis.setex(cache_key, _VERSION_CACHE_TTL, resolved)
    except Exception:
        pass

    logger.info(
        "Resolved partial version",
        tool=tool,
        partial=partial_version,
        resolved=resolved,
    )
    return resolved


async def _resolve_terraform_version(partial: str) -> str:
    """Resolve partial terraform version via releases.hashicorp.com index."""
    url = "https://releases.hashicorp.com/terraform/index.json"
    prefix = f"{partial}."

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    versions = data.get("versions", {})
    matching = []
    for v in versions:
        if v.startswith(prefix):
            # Skip pre-release versions
            if any(tag in v for tag in ("-alpha", "-beta", "-rc", "-dev")):
                continue
            matching.append(v)

    if not matching:
        logger.warning("No matching terraform version found", partial=partial)
        return partial

    # Sort by version parts and return the latest
    matching.sort(key=lambda v: [int(x) for x in v.split(".")])
    return matching[-1]


async def _resolve_tofu_version(partial: str) -> str:
    """Resolve partial tofu version via GitHub releases API."""
    url = "https://api.github.com/repos/opentofu/opentofu/releases"
    prefix = f"v{partial}."

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params={"per_page": 100})
        resp.raise_for_status()
        releases = resp.json()

    matching = []
    for release in releases:
        tag = release.get("tag_name", "")
        if tag.startswith(prefix) and not release.get("prerelease", False):
            # Strip the 'v' prefix
            version = tag.lstrip("v")
            if any(t in version for t in ("-alpha", "-beta", "-rc")):
                continue
            matching.append(version)

    if not matching:
        logger.warning("No matching tofu version found", partial=partial)
        return partial

    matching.sort(key=lambda v: [int(x) for x in v.split(".")])
    return matching[-1]


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
