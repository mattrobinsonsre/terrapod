"""
FastAPI endpoints for filesystem presigned URL handling.

These endpoints validate HMAC-signed tokens and perform the actual I/O
for the filesystem storage backend. They maintain the same client-side
upload/download pattern as cloud backends — the Terraform CLI doesn't
know the difference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response, status

from terrapod.logging_config import get_logger

if TYPE_CHECKING:
    from terrapod.storage.filesystem import FilesystemStore

router = APIRouter(tags=["storage"])
logger = get_logger(__name__)

# Set by storage init — the filesystem store instance
_store: FilesystemStore | None = None


def set_filesystem_store(store: FilesystemStore) -> None:
    """Register the filesystem store instance for route handlers."""
    global _store  # noqa: PLW0603
    _store = store


def _get_store() -> FilesystemStore:
    if _store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Filesystem storage not initialized",
        )
    return _store


@router.put("/storage/put/{key:path}")
async def storage_put(key: str, request: Request) -> Response:
    """Handle a presigned PUT — validate signature and store the object."""
    store = _get_store()

    expires = request.query_params.get("expires", "")
    sig = request.query_params.get("sig", "")
    content_type = request.query_params.get("content_type", "application/octet-stream")

    if not store.verify_signature("PUT", key, expires, sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired signature",
        )

    body = await request.body()
    await store.put(key, body, content_type=content_type)
    logger.info("Object stored via presigned URL", key=key, size=len(body))

    return Response(status_code=status.HTTP_201_CREATED)


@router.get("/storage/get/{key:path}")
async def storage_get(key: str, request: Request) -> Response:
    """Handle a presigned GET — validate signature and return the object."""
    store = _get_store()

    expires = request.query_params.get("expires", "")
    sig = request.query_params.get("sig", "")

    if not store.verify_signature("GET", key, expires, sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired signature",
        )

    from terrapod.storage.protocol import ObjectNotFoundError

    try:
        data = await store.get(key)
    except ObjectNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Object not found: {key}",
        ) from e

    # Read content type from metadata
    meta = await store.head(key)

    return Response(
        content=data,
        media_type=meta.content_type,
        headers={"ETag": meta.etag},
    )
