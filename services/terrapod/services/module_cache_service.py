"""Service layer for module caching (pull-through proxy).

Pull-through cache for upstream module registries. On first request,
fetches version metadata and tarball from the upstream registry,
caches in object storage, and serves from cache on subsequent requests.
"""

import hashlib

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import CachedModule
from terrapod.logging_config import get_logger
from terrapod.storage.keys import module_cache_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)


async def get_or_fetch_versions(
    db: AsyncSession,
    hostname: str,
    namespace: str,
    name: str,
    provider: str,
) -> dict:
    """Get cached version list or fetch from upstream.

    Returns the modules.v1 versions response shape.
    """
    result = await db.execute(
        select(CachedModule.version)
        .where(
            CachedModule.hostname == hostname,
            CachedModule.namespace == namespace,
            CachedModule.name == name,
            CachedModule.provider == provider,
        )
        .distinct()
    )
    cached_versions = [row[0] for row in result.all()]

    if cached_versions:
        return {
            "modules": [
                {
                    "versions": [{"version": v} for v in sorted(cached_versions)],
                }
            ],
        }

    # Fetch from upstream if warm_on_first_request
    cfg = settings.registry.module_cache
    if not cfg.warm_on_first_request:
        return {"modules": [{"versions": []}]}

    if hostname not in cfg.upstream_registries:
        return {"modules": [{"versions": []}]}

    upstream_versions = await _fetch_upstream_versions(hostname, namespace, name, provider)
    return {
        "modules": [
            {
                "versions": [{"version": v} for v in upstream_versions],
            }
        ],
    }


async def get_or_fetch_download_url(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> str | None:
    """Get cached module download URL or fetch from upstream.

    Returns presigned download URL, or None if not available.
    """
    result = await db.execute(
        select(CachedModule).where(
            CachedModule.hostname == hostname,
            CachedModule.namespace == namespace,
            CachedModule.name == name,
            CachedModule.provider == provider,
            CachedModule.version == version,
        )
    )
    cached = result.scalars().first()

    if cached is not None:
        key = module_cache_key(hostname, namespace, name, provider, version)
        presigned = await storage.presigned_get_url(key)
        return presigned.url

    # Cache miss — fetch from upstream
    cfg = settings.registry.module_cache
    if not cfg.warm_on_first_request:
        return None

    if hostname not in cfg.upstream_registries:
        return None

    return await _fetch_and_cache_module(db, storage, hostname, namespace, name, provider, version)


# --- Internal helpers ---


async def _fetch_upstream_versions(
    hostname: str, namespace: str, name: str, provider: str
) -> list[str]:
    """Fetch available versions from upstream module registry."""
    url = f"https://{hostname}/v1/modules/{namespace}/{name}/{provider}/versions"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                "Upstream module version fetch failed",
                hostname=hostname,
                namespace=namespace,
                name=name,
                provider=provider,
                status=resp.status_code,
            )
            return []
        data = resp.json()

    modules = data.get("modules", [])
    if not modules:
        return []
    return [v["version"] for v in modules[0].get("versions", [])]


async def _fetch_and_cache_module(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> str | None:
    """Fetch module tarball from upstream, cache it, return presigned URL."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # Get download URL from upstream
        download_url = await _fetch_upstream_download_url(
            client, hostname, namespace, name, provider, version
        )
        if download_url is None:
            return None

        try:
            # Download the tarball
            tarball_resp = await client.get(download_url, timeout=120.0)
            tarball_resp.raise_for_status()
            tarball_data = tarball_resp.content

            shasum = hashlib.sha256(tarball_data).hexdigest()

            # Store in object storage
            key = module_cache_key(hostname, namespace, name, provider, version)
            await storage.put(key, tarball_data, content_type="application/gzip")

            # Record in database
            entry = CachedModule(
                hostname=hostname,
                namespace=namespace,
                name=name,
                provider=provider,
                version=version,
                shasum=shasum,
            )
            db.add(entry)
            await db.flush()

            # Return presigned URL
            presigned = await storage.presigned_get_url(key)

            logger.info(
                "Module cached",
                hostname=hostname,
                module=f"{namespace}/{name}/{provider}",
                version=version,
                size_bytes=len(tarball_data),
            )

            return presigned.url
        except Exception:
            logger.exception(
                "Failed to cache module",
                hostname=hostname,
                module=f"{namespace}/{name}/{provider}",
                version=version,
            )
            return None


async def _fetch_upstream_download_url(
    client: httpx.AsyncClient,
    hostname: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> str | None:
    """Fetch download URL for a specific module version from upstream.

    The upstream registry returns 204 with X-Terraform-Get header containing
    the download URL, or redirects to the download URL.
    """
    url = f"https://{hostname}/v1/modules/{namespace}/{name}/{provider}/{version}/download"
    # Don't follow redirects — we need the X-Terraform-Get header from 204
    resp = await client.get(url, follow_redirects=False)

    if resp.status_code == 204:
        download_url = resp.headers.get("X-Terraform-Get")
        if download_url:
            return download_url

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location")
        if location:
            return location

    logger.warning(
        "Upstream module download URL fetch failed",
        hostname=hostname,
        module=f"{namespace}/{name}/{provider}",
        version=version,
        status=resp.status_code,
    )
    return None
