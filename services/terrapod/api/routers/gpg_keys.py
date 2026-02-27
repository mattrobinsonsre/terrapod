"""GPG key management endpoints for provider signing.

TFE uses the path prefix /api/registry/private/v2/gpg-keys.

Endpoints:
    POST   /api/registry/private/v2/gpg-keys              — create
    GET    /api/registry/private/v2/gpg-keys               — list
    GET    /api/registry/private/v2/gpg-keys/{key_id}      — show
    DELETE /api/registry/private/v2/gpg-keys/{key_id}      — delete
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.gpg_key_service import (
    create_gpg_key,
    delete_gpg_key,
    get_gpg_key,
    list_gpg_keys,
)

router = APIRouter(tags=["gpg-keys"])
logger = get_logger(__name__)


# --- Request Models ---


class CreateGPGKeyRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            namespace: str
            ascii_armor: str
            source: str = "terrapod"
            source_url: str | None = None

        type: str = "gpg-keys"
        attributes: Attributes

    data: Data


# --- JSON:API serialization ---


def _gpg_key_to_jsonapi(key) -> dict:  # type: ignore[no-untyped-def]
    return {
        "id": str(key.id),
        "type": "gpg-keys",
        "attributes": {
            "key-id": key.key_id,
            "ascii-armor": key.ascii_armor,
            "namespace": key.org_name,
            "source": key.source,
            "source-url": key.source_url,
            "created-at": key.created_at.isoformat() if key.created_at else None,
            "updated-at": key.updated_at.isoformat() if key.updated_at else None,
        },
    }


# --- Endpoints ---


@router.post("/api/registry/private/v2/gpg-keys")
async def create_gpg_key_endpoint(
    body: CreateGPGKeyRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new GPG key. Parses key_id from the ASCII armor block."""
    attrs = body.data.attributes
    try:
        key = await create_gpg_key(
            db,
            org=attrs.namespace,
            ascii_armor=attrs.ascii_armor,
            source=attrs.source,
            source_url=attrs.source_url,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid GPG key: {e}",
        ) from e

    await db.commit()

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _gpg_key_to_jsonapi(key)},
    )


@router.get("/api/registry/private/v2/gpg-keys")
async def list_gpg_keys_endpoint(
    filter_namespace: str | None = Query(None, alias="filter[namespace]"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List GPG keys, optionally filtered by namespace (org)."""
    keys = await list_gpg_keys(db, org=filter_namespace)
    return JSONResponse(
        content={"data": [_gpg_key_to_jsonapi(k) for k in keys]},
    )


@router.get("/api/registry/private/v2/gpg-keys/{key_id}")
async def show_gpg_key_endpoint(
    key_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a specific GPG key by its database ID."""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="GPG key not found")

    key = await get_gpg_key(db, key_uuid)
    if key is None:
        raise HTTPException(status_code=404, detail="GPG key not found")

    return JSONResponse(content={"data": _gpg_key_to_jsonapi(key)})


@router.delete("/api/registry/private/v2/gpg-keys/{key_id}")
async def delete_gpg_key_endpoint(
    key_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a GPG key."""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="GPG key not found")

    deleted = await delete_gpg_key(db, key_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="GPG key not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
