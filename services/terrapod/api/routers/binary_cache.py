"""Terraform/tofu CLI binary cache endpoints.

Provides pull-through caching for terraform and tofu CLI binaries.
Runner Jobs fetch their binary from here at startup instead of using
images with a baked-in binary version.

Endpoints:
    GET    /api/v2/binary-cache/{tool}/{version}/{os}/{arch}    — download (redirect)
    GET    /api/v2/admin/binary-cache                           — list cached
    POST   /api/v2/admin/binary-cache/warm                      — pre-warm
    DELETE /api/v2/admin/binary-cache/{tool}/{version}           — purge
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.binary_cache_service import (
    get_or_cache_binary,
    list_cached_binaries,
    purge_binary,
    warm_binary,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(tags=["binary-cache"])
logger = get_logger(__name__)


class WarmBinaryRequest(BaseModel):
    tool: str = "terraform"
    version: str
    os: str = "linux"
    arch: str = "amd64"


# --- Runner-facing endpoint ---


@router.get("/api/v2/binary-cache/{tool}/{version}/{os}/{arch}")
async def download_binary(
    tool: str,
    version: str,
    os: str,
    arch: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> RedirectResponse:
    """Get a terraform/tofu binary, pulling through from upstream on cache miss.

    Returns a redirect to a presigned download URL. Used by runner Jobs
    to fetch the exact binary version needed for a workspace.
    """
    if not settings.registry.binary_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Binary cache is disabled",
        )

    try:
        url = await get_or_cache_binary(db, storage, tool, version, os, arch)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        logger.error(
            "Failed to fetch binary",
            tool=tool,
            version=version,
            os=os,
            arch=arch,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch {tool} {version}: {e}",
        ) from e

    await db.commit()
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


# --- Admin endpoints ---


@router.get("/api/v2/admin/binary-cache")
async def list_cached_binaries_endpoint(
    tool: str | None = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all cached binaries."""
    entries = await list_cached_binaries(db, tool=tool)
    return JSONResponse(
        content={
            "data": [
                {
                    "id": str(e.id),
                    "type": "cached-binaries",
                    "attributes": {
                        "tool": e.tool,
                        "version": e.version,
                        "os": e.os,
                        "arch": e.arch,
                        "shasum": e.shasum,
                        "download-url": e.download_url,
                        "cached-at": e.cached_at.isoformat() if e.cached_at else None,
                    },
                }
                for e in entries
            ]
        }
    )


@router.post("/api/v2/admin/binary-cache/warm")
async def warm_binary_endpoint(
    body: WarmBinaryRequest,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Pre-warm a specific binary version into the cache."""
    if not settings.registry.binary_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Binary cache is disabled",
        )

    try:
        url = await warm_binary(db, storage, body.tool, body.version, body.os, body.arch)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to warm binary: {e}",
        ) from e

    await db.commit()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "cached", "download_url": url},
    )


@router.delete("/api/v2/admin/binary-cache/{tool}/{version}")
async def purge_binary_endpoint(
    tool: str,
    version: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Purge all cached binaries for a tool+version."""
    count = await purge_binary(db, storage, tool, version)
    await db.commit()
    return JSONResponse(
        content={"status": "purged", "count": count},
    )
