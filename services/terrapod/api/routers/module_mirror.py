"""Module registry proxy endpoints for upstream module caching.

Implements the Terraform module registry protocol for caching upstream
modules. This allows runners to resolve public modules through Terrapod,
enabling air-gapped and bandwidth-constrained environments.

Endpoints:
    GET  /v1/modules/{hostname}/{namespace}/{name}/{provider}/versions        - version list
    GET  /v1/modules/{hostname}/{namespace}/{name}/{provider}/{version}/download - download redirect
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.module_cache_service import (
    get_or_fetch_download_url,
    get_or_fetch_versions,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(tags=["module-mirror"])
logger = get_logger(__name__)


@router.get("/v1/modules/{hostname}/{namespace}/{name}/{provider}/versions")
async def module_versions_mirror(
    hostname: str,
    namespace: str,
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List cached versions for a module (registry protocol).

    Returns the modules.v1 versions response shape expected by terraform.
    On cache miss with warm_on_first_request enabled, fetches version list
    from the upstream registry.
    """
    if not settings.registry.module_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Module cache is disabled",
        )

    result = await get_or_fetch_versions(db, hostname, namespace, name, provider)
    return JSONResponse(content=result)


@router.get(
    "/v1/modules/{hostname}/{namespace}/{name}/{provider}/{version}/download"
)
async def module_download_mirror(
    hostname: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Get download URL for a module version (registry protocol).

    Returns 204 with X-Terraform-Get header pointing to the cached tarball.
    On cache miss with warm_on_first_request enabled, fetches and caches
    the tarball from the upstream registry.
    """
    if not settings.registry.module_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Module cache is disabled",
        )

    download_url = await get_or_fetch_download_url(
        db, storage, hostname, namespace, name, provider, version
    )
    await db.commit()

    if download_url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Module {namespace}/{name}/{provider} version {version} not found",
        )

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Terraform-Get": download_url},
    )
