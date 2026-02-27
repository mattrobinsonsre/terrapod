"""Service layer for provider binary caching (network mirror protocol).

Pull-through cache for upstream provider registries. On first request,
fetches version/platform metadata and binary from the upstream registry,
caches in object storage, and serves from cache on subsequent requests.
"""

import hashlib

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import CachedProviderPackage
from terrapod.logging_config import get_logger
from terrapod.storage.keys import provider_cache_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)


async def get_or_fetch_versions(
    db: AsyncSession,
    hostname: str,
    namespace: str,
    type_: str,
) -> dict:
    """Get cached version list or fetch from upstream.

    Returns the index.json-shaped dict for the network mirror protocol.
    """
    # Check if we have any cached entries for this provider
    result = await db.execute(
        select(CachedProviderPackage.version)
        .where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
        )
        .distinct()
    )
    cached_versions = [row[0] for row in result.all()]

    if cached_versions:
        return {
            "versions": {v: {} for v in sorted(cached_versions)},
        }

    # Fetch from upstream if warm_on_first_request
    cfg = settings.registry.provider_cache
    if not cfg.warm_on_first_request:
        return {"versions": {}}

    if hostname not in cfg.upstream_registries:
        return {"versions": {}}

    upstream_versions = await _fetch_upstream_versions(hostname, namespace, type_)
    return {
        "versions": {v: {} for v in upstream_versions},
    }


async def get_or_fetch_platforms(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> dict:
    """Get cached platform info or fetch from upstream.

    Returns the {version}.json-shaped dict for the network mirror protocol.
    """
    result = await db.execute(
        select(CachedProviderPackage).where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
            CachedProviderPackage.version == version,
        )
    )
    cached = list(result.scalars().all())

    if cached:
        archives = {}
        for entry in cached:
            key = provider_cache_key(hostname, namespace, type_, version, entry.filename)
            presigned = await storage.presigned_get_url(key)
            platform_key = f"{entry.os}_{entry.arch}"
            archive: dict = {
                "url": presigned.url,
                "hashes": [f"zh:{entry.shasum}"],
            }
            if entry.h1_hash:
                archive["hashes"].append(f"h1:{entry.h1_hash}")
            archives[platform_key] = archive
        return {"archives": archives}

    # Cache miss — fetch from upstream
    cfg = settings.registry.provider_cache
    if not cfg.warm_on_first_request:
        return {"archives": {}}

    if hostname not in cfg.upstream_registries:
        return {"archives": {}}

    return await _fetch_and_cache_platforms(
        db, storage, hostname, namespace, type_, version
    )


async def get_or_cache_binary(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
) -> str | None:
    """Get a cached provider binary or fetch from upstream.

    Returns presigned download URL, or None if not found.
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
    if cached is not None:
        key = provider_cache_key(hostname, namespace, type_, version, cached.filename)
        presigned = await storage.presigned_get_url(key)
        return presigned.url

    return None


# --- Internal helpers ---


async def _fetch_upstream_versions(
    hostname: str, namespace: str, type_: str
) -> list[str]:
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


async def _fetch_and_cache_platforms(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> dict:
    """Fetch platform info from upstream, cache binaries, return mirror response."""
    archives: dict = {}

    # Fetch version details from upstream
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # Get the list of platforms for this version
        url = f"https://{hostname}/v1/providers/{namespace}/{type_}/versions"
        resp = await client.get(url)
        if resp.status_code != 200:
            return {"archives": {}}

        data = resp.json()
        target_version = None
        for v in data.get("versions", []):
            if v["version"] == version:
                target_version = v
                break

        if target_version is None:
            return {"archives": {}}

        # Fetch each platform
        for platform in target_version.get("platforms", []):
            os_ = platform["os"]
            arch = platform["arch"]

            try:
                download_info = await _fetch_platform_download(
                    client, hostname, namespace, type_, version, os_, arch
                )
                if download_info is None:
                    continue

                # Download the binary
                binary_resp = await client.get(download_info["download_url"], timeout=120.0)
                binary_resp.raise_for_status()
                binary_data = binary_resp.content

                filename = download_info["filename"]
                shasum = hashlib.sha256(binary_data).hexdigest()

                # Store in object storage
                key = provider_cache_key(hostname, namespace, type_, version, filename)
                await storage.put(key, binary_data, content_type="application/zip")

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

                # Build mirror response
                presigned = await storage.presigned_get_url(key)
                platform_key = f"{os_}_{arch}"
                archives[platform_key] = {
                    "url": presigned.url,
                    "hashes": [f"zh:{shasum}"],
                }

                logger.info(
                    "Provider binary cached",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    platform=platform_key,
                    size_bytes=len(binary_data),
                )
            except Exception:
                logger.exception(
                    "Failed to cache provider platform",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    os=os_,
                    arch=arch,
                )

    await db.flush()
    return {"archives": archives}


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
    url = (
        f"https://{hostname}/v1/providers/{namespace}/{type_}/"
        f"{version}/download/{os_}/{arch}"
    )
    resp = await client.get(url)
    if resp.status_code != 200:
        return None
    return resp.json()
