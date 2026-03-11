"""Private provider registry endpoints.

Two API surfaces:
1. CLI-facing protocol — what `terraform init` speaks for private registry providers
2. TFE V2 management — JSON:API CRUD for managing providers, versions, platforms

UX CONTRACT: Management endpoints are consumed by the web frontend:
  - web/src/app/registry/providers/page.tsx (provider list, create)
  - web/src/app/registry/providers/[name]/page.tsx (provider detail)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to those frontend pages.

CLI Protocol:
    GET  /api/v2/registry/providers/{namespace}/{name}/versions
    GET  /api/v2/registry/providers/{namespace}/{name}/{version}/download/{os}/{arch}

TFE V2 Management:
    POST   /api/v2/organizations/default/registry-providers
    GET    /api/v2/organizations/default/registry-providers
    GET    /api/v2/organizations/default/registry-providers/private/default/{name}
    DELETE /api/v2/organizations/default/registry-providers/private/default/{name}
    POST   .../private/default/{name}/versions
    GET    .../private/default/{name}/versions
    DELETE .../private/default/{name}/versions/{ver}
    POST   .../private/default/{name}/versions/{ver}/platforms
    GET    .../private/default/{name}/versions/{ver}/platforms
    DELETE .../private/default/{name}/versions/{ver}/platforms/{os}/{arch}
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.registry_provider_service import (
    create_provider,
    create_provider_platform,
    create_provider_version,
    delete_provider,
    delete_provider_platform,
    delete_provider_version,
    get_provider,
    get_provider_download_info,
    get_provider_version,
    list_provider_platforms,
    list_provider_versions,
    list_providers,
    upload_provider_binary,
)
from terrapod.services.platform_provider_service import (
    get_download_info as platform_get_download_info,
    get_version_list as platform_get_version_list,
)
from terrapod.services.registry_rbac_service import (
    REGISTRY_PERMISSION_HIERARCHY,
    has_registry_permission,
    resolve_registry_permission,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(tags=["registry-providers"])
logger = get_logger(__name__)


# --- Request Models ---


class CreateProviderRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            name: str
            labels: dict = {}

        type: str = "registry-providers"
        attributes: Attributes

    data: Data


class CreateProviderVersionRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            version: str
            key_id: str = ""
            protocols: list[str] = ["5.0"]

        type: str = "registry-provider-versions"
        attributes: Attributes

    data: Data


class CreateProviderPlatformRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            os: str
            arch: str
            filename: str

        type: str = "registry-provider-platforms"
        attributes: Attributes

    data: Data


# --- JSON:API serialization ---


def _provider_to_jsonapi(provider, effective_permission: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    perm = effective_permission
    return {
        "id": str(provider.id),
        "type": "registry-providers",
        "attributes": {
            "name": provider.name,
            "namespace": provider.namespace,
            "labels": provider.labels or {},
            "owner-email": provider.owner_email,
            "created-at": provider.created_at.isoformat() if provider.created_at else None,
            "updated-at": provider.updated_at.isoformat() if provider.updated_at else None,
            "permissions": {
                "can-update": has_registry_permission(perm, "admin"),
                "can-destroy": has_registry_permission(perm, "admin"),
                "can-create-version": has_registry_permission(perm, "write"),
            },
        },
    }


def _version_to_jsonapi(version, upload_links: dict | None = None) -> dict:  # type: ignore[no-untyped-def]
    platforms = [
        {"os": p.os, "arch": p.arch, "filename": p.filename} for p in (version.platforms or [])
    ]
    result: dict = {
        "id": str(version.id),
        "type": "registry-provider-versions",
        "attributes": {
            "version": version.version,
            "protocols": version.protocols,
            "shasums-uploaded": version.shasums_uploaded,
            "shasums-sig-uploaded": version.shasums_sig_uploaded,
            "platforms": platforms,
            "created-at": version.created_at.isoformat() if version.created_at else None,
        },
    }
    if upload_links:
        result["links"] = upload_links
    return result


def _platform_to_jsonapi(platform, upload_link: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    result: dict = {
        "id": str(platform.id),
        "type": "registry-provider-platforms",
        "attributes": {
            "os": platform.os,
            "arch": platform.arch,
            "filename": platform.filename,
            "shasum": platform.shasum,
            "upload-status": platform.upload_status,
        },
    }
    if upload_link:
        result["links"] = {"provider-binary-upload": upload_link}
    return result


# --- Helper ---


async def _require_provider_permission(
    db: AsyncSession,
    user: AuthenticatedUser,
    provider,
    required: str,
) -> None:
    """Check registry permission on a provider or raise 403."""
    perm = await resolve_registry_permission(
        db, user.email, user.roles, provider.name, provider.labels or {}, provider.owner_email
    )
    if not has_registry_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on provider",
        )


# --- CLI Protocol Endpoints ---


@router.get("/api/v2/registry/providers/{namespace}/{name}/versions")
async def list_provider_versions_cli(
    namespace: str,
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List available versions for a provider (CLI protocol). Requires read.

    Special case: namespace=default, name=terrapod serves the built-in
    platform provider from the running instance version (pull-through cache
    from GitHub Releases).
    """
    # Built-in platform provider — no DB lookup needed
    if namespace == "default" and name == "terrapod":
        return JSONResponse(content=await platform_get_version_list())

    provider = await get_provider(db, namespace, name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, provider.name, provider.labels or {}, provider.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Provider not found")

    versions = []
    for v in provider.versions:
        platforms = [
            {"os": p.os, "arch": p.arch} for p in v.platforms if p.upload_status == "uploaded"
        ]
        versions.append(
            {
                "version": v.version,
                "protocols": v.protocols,
                "platforms": platforms,
            }
        )

    return JSONResponse(content={"versions": versions})


@router.get("/api/v2/registry/providers/{namespace}/{name}/{version}/download/{os}/{arch}")
async def download_provider_cli(
    namespace: str,
    name: str,
    version: str,
    os: str,
    arch: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Get download info for a provider version (CLI protocol). Requires read.

    Special case: namespace=default, name=terrapod delegates to the
    platform provider service (pull-through cache from GitHub Releases).
    """
    # Built-in platform provider
    if namespace == "default" and name == "terrapod":
        try:
            info = await platform_get_download_info(storage, version, os, arch)
            return JSONResponse(content=info)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))

    provider = await get_provider(db, namespace, name)
    if provider is not None:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, provider.name, provider.labels or {}, provider.owner_email
        )
        if not has_registry_permission(perm, "read"):
            raise HTTPException(status_code=404, detail="Provider platform not found")

    info = await get_provider_download_info(db, storage, namespace, name, version, os, arch)
    if info is None:
        raise HTTPException(status_code=404, detail="Provider platform not found")

    return JSONResponse(content=info)


# --- TFE V2 Management Endpoints ---

_ORG_PREFIX = "/api/v2/organizations/default/registry-providers"


@router.post(_ORG_PREFIX)
async def create_provider_endpoint(
    body: CreateProviderRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new registry provider. Any authenticated user; creator becomes owner."""
    attrs = body.data.attributes

    provider = await create_provider(db, "default", attrs.name)
    provider.owner_email = user.email
    provider.labels = attrs.labels
    await db.commit()

    logger.info("Registry provider created", name=attrs.name, owner=user.email)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _provider_to_jsonapi(provider)},
    )


@router.get(_ORG_PREFIX)
async def list_providers_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all registry providers (filtered by permissions)."""
    providers = await list_providers(db)
    visible = []
    for p in providers:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, p.name, p.labels or {}, p.owner_email
        )
        if perm is not None:
            visible.append(_provider_to_jsonapi(p, perm))
    return JSONResponse(content={"data": visible})


@router.get(_ORG_PREFIX + "/private/default/{name}")
async def show_provider_endpoint(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a specific registry provider. Requires read."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, provider.name, provider.labels or {}, provider.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Provider not found")

    return JSONResponse(content={"data": _provider_to_jsonapi(provider, perm)})


@router.delete(_ORG_PREFIX + "/private/default/{name}")
async def delete_provider_endpoint(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Delete a registry provider and all its versions. Requires admin on provider."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    await _require_provider_permission(db, user, provider, "admin")

    deleted = await delete_provider(db, storage, "default", name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(_ORG_PREFIX + "/private/default/{name}")
async def update_provider_endpoint(
    name: str,
    body: dict,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a registry provider's labels and/or owner. Requires admin on provider."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, provider.name, provider.labels or {}, provider.owner_email
    )
    if not has_registry_permission(perm, "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin permission on provider",
        )

    attrs = body.get("data", {}).get("attributes", {})

    if "owner-email" in attrs:
        if "admin" not in user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform admins can change owner",
            )
        provider.owner_email = attrs["owner-email"]

    if "labels" in attrs:
        new_labels = attrs["labels"]
        # Self-lockout check: warn if label change would reduce user's access
        if (
            new_labels != (provider.labels or {})
            and not attrs.get("force")
            and "admin" not in user.roles
            and provider.owner_email != user.email
        ):
            new_perm = await resolve_registry_permission(
                db, user.email, user.roles, provider.name, new_labels, provider.owner_email
            )
            if new_perm is None or REGISTRY_PERMISSION_HIERARCHY.get(
                new_perm, -1
            ) < REGISTRY_PERMISSION_HIERARCHY.get(perm, -1):
                new_level = new_perm or "none"
                return JSONResponse(
                    status_code=409,
                    content={
                        "errors": [
                            {
                                "status": "409",
                                "title": "Label change would reduce your access",
                                "detail": (
                                    f"This label change would reduce your access from "
                                    f"{perm} to {new_level} on this provider. "
                                    f'Re-submit with "force": true to confirm.'
                                ),
                            }
                        ]
                    },
                )
        provider.labels = new_labels

    await db.commit()
    await db.refresh(provider)
    return JSONResponse(content={"data": _provider_to_jsonapi(provider, perm)})


# --- Version Management ---


@router.post(_ORG_PREFIX + "/private/default/{name}/versions")
async def create_provider_version_endpoint(
    name: str,
    body: CreateProviderVersionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Create a provider version and get upload URLs. Requires write."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    await _require_provider_permission(db, user, provider, "write")

    attrs = body.data.attributes

    # Resolve GPG key if key_id provided
    gpg_key_uuid: uuid.UUID | None = None
    if attrs.key_id:
        from terrapod.services.gpg_key_service import get_gpg_key_by_key_id

        gpg_key = await get_gpg_key_by_key_id(db, attrs.key_id)
        if gpg_key is not None:
            gpg_key_uuid = gpg_key.id

    prov_version, shasums_url, sig_url = await create_provider_version(
        db, storage, provider.id, attrs.version, gpg_key_uuid, attrs.protocols
    )
    await db.commit()

    logger.info(
        "Provider version created",
        provider_id=str(provider.id),
        version=attrs.version,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "data": _version_to_jsonapi(
                prov_version,
                upload_links={
                    "shasums-upload": shasums_url.url,
                    "shasums-sig-upload": sig_url.url,
                },
            )
        },
    )


@router.get(_ORG_PREFIX + "/private/default/{name}/versions")
async def list_provider_versions_endpoint(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all versions for a provider. Requires read."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, provider.name, provider.labels or {}, provider.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Provider not found")

    versions = await list_provider_versions(db, provider.id)
    return JSONResponse(
        content={"data": [_version_to_jsonapi(v) for v in versions]},
    )


@router.delete(_ORG_PREFIX + "/private/default/{name}/versions/{version}")
async def delete_provider_version_endpoint(
    name: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Delete a provider version and its platforms. Requires admin."""
    provider = await get_provider(db, "default", name)
    if provider is not None:
        await _require_provider_permission(db, user, provider, "admin")

    deleted = await delete_provider_version(db, storage, "default", name, version)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider version not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Platform Management ---


@router.post(_ORG_PREFIX + "/private/default/{name}/versions/{version}/platforms")
async def create_provider_platform_endpoint(
    name: str,
    version: str,
    body: CreateProviderPlatformRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Create a platform entry and get an upload URL. Requires write."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    await _require_provider_permission(db, user, provider, "write")

    prov_version = await get_provider_version(db, provider.id, version)
    if prov_version is None:
        raise HTTPException(status_code=404, detail="Provider version not found")

    attrs = body.data.attributes
    platform, upload_url = await create_provider_platform(
        db, storage, prov_version.id, attrs.os, attrs.arch, attrs.filename
    )
    await db.commit()

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _platform_to_jsonapi(platform, upload_link=upload_url.url)},
    )


@router.get(_ORG_PREFIX + "/private/default/{name}/versions/{version}/platforms")
async def list_provider_platforms_endpoint(
    name: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all platforms for a provider version. Requires read."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, provider.name, provider.labels or {}, provider.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Provider not found")

    prov_version = await get_provider_version(db, provider.id, version)
    if prov_version is None:
        raise HTTPException(status_code=404, detail="Provider version not found")

    platforms = await list_provider_platforms(db, prov_version.id)
    return JSONResponse(
        content={"data": [_platform_to_jsonapi(p) for p in platforms]},
    )


@router.put(_ORG_PREFIX + "/private/default/{name}/versions/{version}/platforms/{os}/{arch}/upload")
async def upload_provider_binary_endpoint(
    name: str,
    version: str,
    os: str,
    arch: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload a provider binary directly. Requires write. Idempotent."""
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    await _require_provider_permission(db, user, provider, "write")

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")

    platform = await upload_provider_binary(db, storage, "default", name, version, os, arch, data)
    await db.commit()

    logger.info(
        "Provider binary uploaded",
        provider=name,
        version=version,
        os=os,
        arch=arch,
        size=len(data),
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"data": _platform_to_jsonapi(platform)},
    )


@router.delete(_ORG_PREFIX + "/private/default/{name}/versions/{version}/platforms/{os}/{arch}")
async def delete_provider_platform_endpoint(
    name: str,
    version: str,
    os: str,
    arch: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Delete a specific provider platform binary. Requires admin."""
    provider = await get_provider(db, "default", name)
    if provider is not None:
        await _require_provider_permission(db, user, provider, "admin")

    deleted = await delete_provider_platform(db, storage, "default", name, version, os, arch)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider platform not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
