"""Provider network mirror protocol endpoints.

Implements the Terraform provider network mirror protocol for caching
upstream provider binaries. This allows `terraform init` to use Terrapod
as a provider mirror, enabling air-gapped and bandwidth-constrained
environments.

Endpoints:
    GET  /v1/providers/{hostname}/{namespace}/{type}/index.json       — version list
    GET  /v1/providers/{hostname}/{namespace}/{type}/{version}.json   — platform archives
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.provider_cache_service import (
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
    On cache miss with warm_on_first_request enabled, fetches version list
    from the upstream registry.
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
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Get platform archives for a provider version (network mirror protocol).

    Returns the {version}.json format with download URLs and hashes.
    On cache miss with warm_on_first_request enabled, fetches and caches
    all platform binaries from the upstream registry.
    """
    if not settings.registry.provider_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider cache is disabled",
        )

    result = await get_or_fetch_platforms(
        db, storage, hostname, namespace, type, version
    )
    await db.commit()
    return JSONResponse(content=result)
