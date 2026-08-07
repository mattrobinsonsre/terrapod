"""GPG key management endpoints for provider signing.

The CLI doesn't talk to these — `terraform init` reads provider GPG
public keys from the provider download response, not from a separate
admin endpoint. They're Terrapod-native management surface and live at
`/api/terrapod/v1/gpg-keys`.

The historical TFE-shaped path was `/api/registry/private/v2/gpg-keys`;
it was removed in v0.24.0 (see #278) and is no longer routable.

Endpoints (canonical):
    POST   /api/terrapod/v1/gpg-keys                  — create
    GET    /api/terrapod/v1/gpg-keys                   — list
    GET    /api/terrapod/v1/gpg-keys/{key_id}          — show
    POST   /api/terrapod/v1/gpg-keys/{key_id}/revoke   — revoke (#640)
    DELETE /api/terrapod/v1/gpg-keys/{key_id}          — delete
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.pagination import paginate
from terrapod.api.serialization import rfc3339
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.gpg_key_service import (
    create_gpg_key,
    delete_gpg_key,
    get_gpg_key,
    list_gpg_keys,
    revoke_gpg_key,
)
from terrapod.services.registry_rbac_service import (
    resolve_registry_capabilities_for,
)

router = APIRouter(tags=["gpg-keys"])
logger = get_logger(__name__)


# --- Request Models ---


class CreateGPGKeyRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            # JSON:API uses hyphenated attribute names by convention.
            # Pydantic field names are snake_case; aliases below accept
            # either `ascii-armor` / `source-url` (hyphenated, the
            # convention every other Terrapod router follows) or the
            # snake_case form (used by go-tfe + a handful of older
            # clients). populate_by_name lets the alias OR the field
            # name match.
            namespace: str
            ascii_armor: str = Field(..., alias="ascii-armor")
            source: str = "terrapod"
            source_url: str | None = Field(default=None, alias="source-url")

            model_config = {"populate_by_name": True}

        type: str = "gpg-keys"
        attributes: Attributes

    data: Data


class RevokeGPGKeyRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            revocation_certificate: str = Field(..., alias="revocation-certificate")

            model_config = {"populate_by_name": True}

        type: str = "gpg-key-revocations"
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
            "namespace": "default",
            "source": key.source,
            "source-url": key.source_url,
            "created-at": rfc3339(key.created_at),
            "updated-at": rfc3339(key.updated_at),
        },
    }


# --- Endpoints ---


# The GPG key store is the trust anchor for provider signature verification:
# `registry_provider_service._verify_and_store_shasums_signature` reads the
# issuer key id out of a publisher's detached SHA256SUMS.sig and verifies the
# signature against whichever registered GPGKey matches. Being able to add a
# key therefore means being able to have Terrapod accept a signature you made;
# being able to delete one means being able to break verification for a
# publisher who did nothing wrong. Both are registry-administration acts, and
# were previously gated on nothing beyond "is authenticated".
#
# GPGKey carries no owner and no labels — it is one platform-wide list, not a
# per-resource thing — so there is nothing to scope the capability against and
# no name is invented for it. The empty resource means only the unscoped grants
# resolve: platform admin (and audit's read floor). A role's allow_labels
# cannot match, because `matches_labels` requires the label key to be present
# on the resource.
#
# An earlier version of this passed the literal "gpg-keys" as a resource name
# so a registry role could name it in allow_names. That put a magic string into
# the same namespace as real provider names (`_require_provider_capability`
# passes `provider.name`), so a provider called "gpg-keys" would have collided
# with the trust-anchor store in both directions. Not worth the flexibility.


async def _require_gpg_key_capability(
    db: AsyncSession,
    user: AuthenticatedUser,
    required_cap: str,
) -> None:
    """Check the required registry capability on the GPG key store or raise 403."""
    caps = await resolve_registry_capabilities_for(db, user, "", {}, "")
    if not has_capability(caps, required_cap):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required_cap} capability on the GPG key store",
        )


@router.post("/gpg-keys")
async def create_gpg_key_endpoint(
    body: CreateGPGKeyRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new GPG key. Parses key_id from the ASCII armor block."""
    await _require_gpg_key_capability(db, user, cap.REGISTRY_ADMIN)
    attrs = body.data.attributes
    try:
        key = await create_gpg_key(
            db,
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


@router.get("/gpg-keys")
async def list_gpg_keys_endpoint(
    filter_namespace: str | None = Query(None, alias="filter[namespace]"),
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List GPG keys, optionally filtered by namespace (org)."""
    keys = await list_gpg_keys(db)
    items = [_gpg_key_to_jsonapi(k) for k in keys]
    page_items, meta = paginate(items, request)
    return JSONResponse(
        content={"data": page_items, "meta": meta},
    )


@router.get("/gpg-keys/{key_id}")
async def show_gpg_key_endpoint(
    key_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a specific GPG key by its database ID."""
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="GPG key not found") from None

    key = await get_gpg_key(db, key_uuid)
    if key is None:
        raise HTTPException(status_code=404, detail="GPG key not found")

    return JSONResponse(content={"data": _gpg_key_to_jsonapi(key)})


@router.post("/gpg-keys/{key_id}/revoke")
async def revoke_gpg_key_endpoint(
    key_id: str,
    body: RevokeGPGKeyRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Revoke a registered GPG key by applying an owner-issued revocation
    certificate (#640). The key stays registered but all signature verification
    fails closed for it. 422 if the certificate is not a valid self-revocation."""
    await _require_gpg_key_capability(db, user, cap.REGISTRY_ADMIN)
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="GPG key not found") from None

    try:
        key = await revoke_gpg_key(db, key_uuid, body.data.attributes.revocation_certificate)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    if key is None:
        raise HTTPException(status_code=404, detail="GPG key not found")

    await db.commit()
    return JSONResponse(content={"data": _gpg_key_to_jsonapi(key)})


@router.delete("/gpg-keys/{key_id}")
async def delete_gpg_key_endpoint(
    key_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a GPG key."""
    await _require_gpg_key_capability(db, user, cap.REGISTRY_ADMIN)
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="GPG key not found") from None

    deleted = await delete_gpg_key(db, key_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="GPG key not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
