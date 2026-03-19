"""Provider network mirror protocol endpoints.

Implements the Terraform provider network mirror protocol for caching
upstream provider binaries. This allows `terraform init` to use Terrapod
as a provider mirror, enabling air-gapped and bandwidth-constrained
environments.

On first request for a provider version, upstream metadata (shasums, filenames)
is fetched and cached in Redis. Individual binaries are cached on-demand when
a runner downloads a specific platform via the proxy endpoint.

Endpoints:
    GET  /v1/providers/{hostname}/{namespace}/{type}/index.json                  — version list
    GET  /v1/providers/{hostname}/{namespace}/{type}/{version}.json              — platform archives
    GET  /v1/providers/{hostname}/{namespace}/{type}/{version}/download/{os}/{arch} — binary download proxy
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.provider_cache_service import (
    fetch_and_cache_single_platform,
    get_cached_platform,
    get_or_fetch_platforms,
    get_or_fetch_versions,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(tags=["provider-mirror"])
logger = get_logger(__name__)


@router.get("/v1/providers/{hostname}/{namespace}/{type}/index.json")
async def provider_versions_mirror(
    hostname: str,
    namespace: str,
    type: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List cached versions for a provider (network mirror protocol).

    Returns the index.json format expected by terraform's network mirror.
    Requires authentication (runner token, API token, or session).
    """
    if not settings.registry.provider_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider cache is disabled",
        )

    result = await get_or_fetch_versions(db, hostname, namespace, type)
    return JSONResponse(content=result)


@router.get("/v1/providers/{hostname}/{namespace}/{type}/{version}.json")
async def provider_platforms_mirror(
    hostname: str,
    namespace: str,
    type: str,
    version: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Get platform archives for a provider version (network mirror protocol).

    Returns the {version}.json format with download URLs and hashes.
    Cached platforms get presigned storage URLs; uncached platforms get
    proxy download URLs that trigger on-demand caching.
    Requires authentication (runner token, API token, or session).
    """
    if not settings.registry.provider_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider cache is disabled",
        )

    result = await get_or_fetch_platforms(
        db, storage, hostname, namespace, type, version, request=request
    )
    await db.commit()
    return JSONResponse(content=result)


@router.get(
    "/v1/providers/{hostname}/{namespace}/{type}/{version}/download/{os}/{arch}",
    name="provider_download_proxy",
)
async def provider_download_proxy(
    hostname: str,
    namespace: str,
    type: str,
    version: str,
    os: str,
    arch: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> RedirectResponse:
    """Download a provider binary, caching on-demand from upstream.

    Cache hit: 302 redirect to presigned storage URL.
    Cache miss: fetches binary from upstream, stores in object storage,
    creates DB record, then 302 redirects to presigned URL.

    Requires authentication (runner token, API token, or session).
    """
    if not settings.registry.provider_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider cache is disabled",
        )

    # Check for cached binary (with stale record cleanup)
    url = await get_cached_platform(db, storage, hostname, namespace, type, version, os, arch)

    if url is None:
        # Cache miss — fetch from upstream and cache
        try:
            url = await fetch_and_cache_single_platform(
                db, storage, hostname, namespace, type, version, os, arch
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
        except Exception as e:
            logger.error(
                "Failed to cache provider binary",
                hostname=hostname,
                provider=f"{namespace}/{type}",
                version=version,
                platform=f"{os}_{arch}",
                error=str(e),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to fetch provider binary: {e}",
            ) from e

    await db.commit()
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
