"""Service layer for provider binary caching (network mirror protocol).

Pull-through cache for upstream provider registries. On first request for a
provider version, fetches metadata (versions + platform shasums) from upstream,
caches it in Redis, and returns mirror-protocol JSON with proxy download URLs.
Individual platform binaries are cached on-demand in object storage when a
runner actually downloads them — only the requested os/arch is fetched.

Cache layers:
  - Redis: upstream platform metadata (shasum, filename, download_url) with 24h TTL.
    Allows {version}.json to respond without hitting upstream on subsequent requests.
  - Postgres + Object Storage: actual binary files, persisted until purged.
    Created when a runner downloads a specific platform via the proxy endpoint.
"""

import json

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import CachedProviderPackage
from terrapod.logging_config import get_logger
from terrapod.services.hashing_stream import HashingStream
from terrapod.storage.keys import provider_cache_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

# Redis key for cached upstream platform metadata (24h TTL)
_META_KEY_PREFIX = "tp:provider_meta"
_META_TTL = 86400  # 24 hours


def _meta_redis_key(hostname: str, namespace: str, type_: str, version: str) -> str:
    return f"{_META_KEY_PREFIX}:{hostname}:{namespace}:{type_}:{version}"


# Redis key for cached upstream version index (24h TTL)
_INDEX_KEY_PREFIX = "tp:provider_index"


def _index_redis_key(hostname: str, namespace: str, type_: str) -> str:
    return f"{_INDEX_KEY_PREFIX}:{hostname}:{namespace}:{type_}"


async def get_or_fetch_versions(
    db: AsyncSession,
    hostname: str,
    namespace: str,
    type_: str,
) -> dict:
    """Get version list from upstream (Redis-cached, 24h TTL).

    Returns the index.json-shaped dict for the network mirror protocol.
    The version index must reflect ALL upstream versions — not just the subset
    we've cached binaries for — so that terraform/tofu version constraint
    resolution works correctly.
    """
    cfg = settings.registry.provider_cache
    if hostname not in cfg.upstream_registries:
        return {"versions": {}}

    # Check Redis cache first
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    cache_key = _index_redis_key(hostname, namespace, type_)
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    # Fetch from upstream
    upstream_versions = await _fetch_upstream_versions(hostname, namespace, type_)
    if not upstream_versions:
        return {"versions": {}}

    result = {
        "versions": {v: {} for v in upstream_versions},
    }

    # Cache in Redis (24h TTL)
    await redis.set(cache_key, json.dumps(result), ex=_META_TTL)

    return result


async def get_or_fetch_platforms(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> dict:
    """Get cached platform info or fetch metadata from upstream.

    Returns the {version}.json-shaped dict for the network mirror protocol.

    Three-tier lookup:
    1. Postgres (binary cached): presigned URLs to stored binaries.
    2. Redis (metadata cached): proxy URLs with upstream shasums, no upstream call.
    3. Upstream fetch: fetches metadata JSON, caches in Redis, returns proxy URLs.

    For a mix of cached and uncached platforms, cached platforms get presigned
    URLs and uncached platforms get proxy URLs (from Redis metadata).
    """
    # --- Tier 1: check Postgres for cached binaries ---
    result = await db.execute(
        select(CachedProviderPackage).where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
            CachedProviderPackage.version == version,
        )
    )
    cached = list(result.scalars().all())

    archives: dict = {}
    cached_platforms: set[str] = set()

    if cached:
        stale_ids = []
        for entry in cached:
            key = provider_cache_key(hostname, namespace, type_, version, entry.filename)
            # Verify the object actually exists in storage
            if not await storage.exists(key):
                logger.warning(
                    "Stale provider cache record (object missing from storage)",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    platform=f"{entry.os}_{entry.arch}",
                )
                stale_ids.append(entry.id)
                continue

            presigned = await storage.presigned_get_url(key)
            platform_key = f"{entry.os}_{entry.arch}"
            archive: dict = {
                "url": presigned.url,
                "hashes": [f"zh:{entry.shasum}"],
            }
            if entry.h1_hash:
                archive["hashes"].append(f"h1:{entry.h1_hash}")
            archives[platform_key] = archive
            cached_platforms.add(platform_key)

        # Clean up stale DB records
        if stale_ids:
            await db.execute(
                delete(CachedProviderPackage).where(CachedProviderPackage.id.in_(stale_ids))
            )
            await db.flush()

    # --- Tier 2: check Redis for upstream metadata ---
    meta = await _get_cached_metadata(hostname, namespace, type_, version)

    if meta is None:
        # --- Tier 3: fetch from upstream and cache in Redis ---
        cfg = settings.registry.provider_cache
        if not cfg.warm_on_first_request:
            return {"archives": archives} if archives else {"archives": {}}

        if hostname not in cfg.upstream_registries:
            return {"archives": archives} if archives else {"archives": {}}

        meta = await _fetch_and_cache_upstream_metadata(hostname, namespace, type_, version)

    if meta is None:
        return {"archives": archives} if archives else {"archives": {}}

    # For uncached platforms: eagerly cache platforms matching the configured
    # filter (returning presigned storage URLs), and return upstream direct
    # download URLs for all others (no auth needed — public registries).
    configured_platforms = {
        f"{p['os']}_{p['arch']}" for p in settings.registry.provider_cache.platforms
    }

    for platform_key, platform_meta in meta.items():
        if platform_key in cached_platforms:
            continue  # Already have presigned URL from tier 1

        if platform_key in configured_platforms:
            # Eagerly cache and return presigned URL
            os_, arch = platform_key.split("_", 1)
            try:
                url = await fetch_and_cache_single_platform(
                    db, storage, hostname, namespace, type_, version, os_, arch
                )
                archives[platform_key] = {
                    "url": url,
                    "hashes": [f"zh:{platform_meta['shasum']}"],
                }
            except Exception:
                logger.warning(
                    "Failed to eagerly cache platform, falling back to upstream URL",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    platform=platform_key,
                    exc_info=True,
                )
                archives[platform_key] = {
                    "url": platform_meta["download_url"],
                    "hashes": [f"zh:{platform_meta['shasum']}"],
                }
        else:
            # Not in filter — upstream direct URL (public, no auth needed)
            archives[platform_key] = {
                "url": platform_meta["download_url"],
                "hashes": [f"zh:{platform_meta['shasum']}"],
            }

    return {"archives": archives}


async def fetch_and_cache_single_platform(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
) -> str:
    """Fetch a single platform binary from upstream, cache it, return presigned URL.

    Called by the download proxy endpoint when a runner requests a specific
    platform that hasn't been cached yet.

    Tries Redis metadata first for the download URL, falls back to upstream.
    """
    # Check Redis metadata for download info (avoids extra upstream call)
    platform_key = f"{os_}_{arch}"
    meta = await _get_cached_metadata(hostname, namespace, type_, version)
    download_url = None
    filename = None
    if meta and platform_key in meta:
        download_url = meta[platform_key].get("download_url")
        filename = meta[platform_key].get("filename")

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # If we don't have download info from Redis, fetch from upstream
        if not download_url or not filename:
            download_info = await _fetch_platform_download(
                client, hostname, namespace, type_, version, os_, arch
            )
            if download_info is None:
                raise ValueError(
                    f"Platform {os_}/{arch} not found upstream for "
                    f"{hostname}/{namespace}/{type_} v{version}"
                )
            download_url = download_info["download_url"]
            filename = download_info["filename"]

        key = provider_cache_key(hostname, namespace, type_, version, filename)

        # Stream binary directly to object storage
        async with client.stream("GET", download_url, timeout=300.0) as resp:
            resp.raise_for_status()
            stream = HashingStream(resp)
            await storage.put_stream(key, stream, content_type="application/zip")
            shasum = stream.sha256_hex
            size_bytes = stream.size

    # Record in database
    entry = CachedProviderPackage(
        hostname=hostname,
        namespace=namespace,
        type=type_,
        version=version,
        os=os_,
        arch=arch,
        filename=filename,
        shasum=shasum,
    )
    db.add(entry)
    await db.flush()

    logger.info(
        "Provider binary cached (on-demand)",
        hostname=hostname,
        provider=f"{namespace}/{type_}",
        version=version,
        platform=platform_key,
        size_bytes=size_bytes,
    )

    presigned = await storage.presigned_get_url(key)
    return presigned.url


async def get_cached_platform(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
) -> str | None:
    """Get a cached provider binary presigned URL, or None if not cached.

    Also cleans up stale DB records (object missing from storage).
    """
    result = await db.execute(
        select(CachedProviderPackage).where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
            CachedProviderPackage.version == version,
            CachedProviderPackage.os == os_,
            CachedProviderPackage.arch == arch,
        )
    )
    cached = result.scalars().first()
    if cached is None:
        return None

    key = provider_cache_key(hostname, namespace, type_, version, cached.filename)
    if not await storage.exists(key):
        logger.warning(
            "Stale provider cache record (object missing from storage)",
            hostname=hostname,
            provider=f"{namespace}/{type_}",
            version=version,
            platform=f"{os_}_{arch}",
        )
        await db.delete(cached)
        await db.flush()
        return None

    presigned = await storage.presigned_get_url(key)
    return presigned.url


# --- Admin functions ---


async def list_cached_providers(
    db: AsyncSession,
    hostname: str | None = None,
) -> list[CachedProviderPackage]:
    """List cached provider packages, optionally filtered by hostname."""
    stmt = select(CachedProviderPackage).order_by(CachedProviderPackage.cached_at.desc())
    if hostname is not None:
        stmt = stmt.where(CachedProviderPackage.hostname == hostname)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def purge_cached_provider(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> int:
    """Purge all cached platforms for a provider version. Returns count deleted."""
    result = await db.execute(
        select(CachedProviderPackage).where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
            CachedProviderPackage.version == version,
        )
    )
    entries = list(result.scalars().all())
    for entry in entries:
        key = provider_cache_key(hostname, namespace, type_, version, entry.filename)
        await storage.delete(key)
        await db.delete(entry)

    await db.flush()
    return len(entries)


# --- Redis metadata cache ---


async def _get_cached_metadata(
    hostname: str, namespace: str, type_: str, version: str
) -> dict | None:
    """Get cached upstream platform metadata from Redis.

    Returns dict mapping platform_key (e.g. "linux_amd64") to
    {"shasum": ..., "filename": ..., "download_url": ...}, or None.
    """
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        key = _meta_redis_key(hostname, namespace, type_, version)
        raw = await redis.get(key)
        if raw:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        logger.debug("Redis metadata cache miss or error", exc_info=True)
    return None


async def _cache_metadata(
    hostname: str, namespace: str, type_: str, version: str, meta: dict
) -> None:
    """Cache upstream platform metadata in Redis with TTL."""
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        key = _meta_redis_key(hostname, namespace, type_, version)
        await redis.setex(key, _META_TTL, json.dumps(meta))
    except Exception:
        logger.debug("Failed to cache metadata in Redis", exc_info=True)


# --- Internal helpers ---


async def _fetch_upstream_versions(hostname: str, namespace: str, type_: str) -> list[str]:
    """Fetch available versions from upstream registry."""
    url = f"https://{hostname}/v1/providers/{namespace}/{type_}/versions"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                "Upstream version fetch failed",
                hostname=hostname,
                namespace=namespace,
                type=type_,
                status=resp.status_code,
            )
            return []
        data = resp.json()

    return [v["version"] for v in data.get("versions", [])]


async def _fetch_and_cache_upstream_metadata(
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> dict | None:
    """Fetch platform metadata from upstream and cache in Redis.

    Returns dict mapping platform_key to {shasum, filename, download_url},
    or None on failure.
    """
    meta: dict = {}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # Get the list of platforms for this version
        url = f"https://{hostname}/v1/providers/{namespace}/{type_}/versions"
        resp = await client.get(url)
        if resp.status_code != 200:
            return None

        data = resp.json()
        target_version = None
        for v in data.get("versions", []):
            if v["version"] == version:
                target_version = v
                break

        if target_version is None:
            return None

        # Fetch metadata for each platform (JSON only, no binary downloads)
        for platform in target_version.get("platforms", []):
            os_ = platform["os"]
            arch = platform["arch"]

            try:
                download_info = await _fetch_platform_download(
                    client, hostname, namespace, type_, version, os_, arch
                )
                if download_info is None:
                    continue

                platform_key = f"{os_}_{arch}"
                meta[platform_key] = {
                    "shasum": download_info["shasum"],
                    "filename": download_info["filename"],
                    "download_url": download_info["download_url"],
                }
            except Exception:
                logger.exception(
                    "Failed to fetch platform metadata",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    os=os_,
                    arch=arch,
                )

    if meta:
        await _cache_metadata(hostname, namespace, type_, version, meta)
        logger.info(
            "Upstream provider metadata cached in Redis",
            hostname=hostname,
            provider=f"{namespace}/{type_}",
            version=version,
            platforms=len(meta),
        )

    return meta


async def _fetch_platform_download(
    client: httpx.AsyncClient,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
) -> dict | None:
    """Fetch download info for a specific platform from upstream."""
    url = f"https://{hostname}/v1/providers/{namespace}/{type_}/{version}/download/{os_}/{arch}"
    resp = await client.get(url)
    if resp.status_code != 200:
        return None
    return resp.json()
