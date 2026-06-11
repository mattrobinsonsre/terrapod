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

TFE V2 Management + client-signed publish (no presigned URLs, no finalize):
    POST   /api/terrapod/v1/registry-providers
    GET    /api/terrapod/v1/registry-providers
    GET    /api/terrapod/v1/registry-providers/private/default/{name}
    DELETE /api/terrapod/v1/registry-providers/private/default/{name}
    GET    .../private/default/{name}/versions
    DELETE .../private/default/{name}/versions/{ver}
    PUT    .../private/default/{name}/versions/{ver}/shasums         (client manifest)
    PUT    .../private/default/{name}/versions/{ver}/shasums.sig     (signature; trust gate)
    PUT    .../private/default/{name}/versions/{ver}/platforms/{os}/{arch}  (streamed zip)
    GET    .../private/default/{name}/versions/{ver}/platforms
    DELETE .../private/default/{name}/versions/{ver}/platforms/{os}/{arch}

Publishing is client-signed and CLI-only (terrapod-publish): the browser UI
is read-only for versions. The old presigned create-version / create-platform
POSTs and the server-signed `/upload` PUT were removed.
"""

import asyncio
import hashlib
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_non_runner
from terrapod.api.labels import validate_labels
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.platform_provider_service import (
    get_download_info as platform_get_download_info,
)
from terrapod.services.platform_provider_service import (
    get_version_list as platform_get_version_list,
)
from terrapod.services.registry_provider_service import (
    PublishValidationError,
    create_provider,
    delete_provider,
    delete_provider_platform,
    delete_provider_version,
    get_provider,
    get_provider_download_info,
    get_provider_version,
    list_provider_platforms,
    list_provider_versions,
    list_providers,
    record_provider_binary,
    store_and_verify_provider_sig,
    store_provider_shasums,
)
from terrapod.services.registry_rbac_service import (
    REGISTRY_PERMISSION_HIERARCHY,
    has_registry_permission,
    resolve_registry_permission,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(prefix="/api/v2", tags=["registry-providers"])

# Terrapod-native provider-registry management — org-scoped CRUD on
# private providers + their versions + per-platform binaries. The CLI
# only uses the /api/v2/registry/providers/... download protocol on
# `router`. Dual-mounted at /api/terrapod/v1 (canonical) and /api/v2
# (deprecated alias removed in v0.24.0 — see #278).
management_router = APIRouter(tags=["registry-providers-management"])
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
        db,
        user.email,
        user.roles,
        provider.name,
        provider.labels or {},
        provider.owner_email,
        auth_method=user.auth_method,
    )
    if not has_registry_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on provider",
        )


# --- CLI Protocol Endpoints ---


@router.get("/registry/providers/{namespace}/{name}/versions")
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
        db,
        user.email,
        user.roles,
        provider.name,
        provider.labels or {},
        provider.owner_email,
        auth_method=user.auth_method,
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


@router.get("/registry/providers/{namespace}/{name}/{version}/download/{os}/{arch}")
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
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    provider = await get_provider(db, namespace, name)
    if provider is not None:
        perm = await resolve_registry_permission(
            db,
            user.email,
            user.roles,
            provider.name,
            provider.labels or {},
            provider.owner_email,
            auth_method=user.auth_method,
        )
        if not has_registry_permission(perm, "read"):
            raise HTTPException(status_code=404, detail="Provider platform not found")

    info = await get_provider_download_info(db, storage, namespace, name, version, os, arch)
    if info is None:
        raise HTTPException(status_code=404, detail="Provider platform not found")

    return JSONResponse(content=info)


# --- Management Endpoints (Terrapod-native) ---

_BASE = "/registry-providers"


@management_router.post(_BASE)
async def create_provider_endpoint(
    body: CreateProviderRequest,
    user: AuthenticatedUser = Depends(require_non_runner),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new registry provider. Any authenticated user; creator becomes owner."""
    attrs = body.data.attributes

    provider = await create_provider(db, "default", attrs.name)
    provider.owner_email = user.email
    provider.labels = validate_labels(attrs.labels)
    await db.commit()

    logger.info("Registry provider created", name=attrs.name, owner=user.email)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _provider_to_jsonapi(provider)},
    )


@management_router.get(_BASE)
async def list_providers_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all registry providers (filtered by permissions)."""
    providers = await list_providers(db)
    visible = []
    for p in providers:
        perm = await resolve_registry_permission(
            db,
            user.email,
            user.roles,
            p.name,
            p.labels or {},
            p.owner_email,
            auth_method=user.auth_method,
        )
        if perm is not None:
            visible.append(_provider_to_jsonapi(p, perm))
    return JSONResponse(content={"data": visible})


@management_router.get(_BASE + "/private/default/{name}")
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
        db,
        user.email,
        user.roles,
        provider.name,
        provider.labels or {},
        provider.owner_email,
        auth_method=user.auth_method,
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Provider not found")

    return JSONResponse(content={"data": _provider_to_jsonapi(provider, perm)})


@management_router.delete(_BASE + "/private/default/{name}")
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


@management_router.patch(_BASE + "/private/default/{name}")
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
        db,
        user.email,
        user.roles,
        provider.name,
        provider.labels or {},
        provider.owner_email,
        auth_method=user.auth_method,
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
        # Validate up-front (size limits + reserved-key check). Raises 422
        # before any self-lockout logic.
        new_labels = validate_labels(attrs["labels"])
        # Self-lockout check: warn if label change would reduce user's access
        if (
            new_labels != (provider.labels or {})
            and not attrs.get("force")
            and "admin" not in user.roles
            and provider.owner_email != user.email
        ):
            new_perm = await resolve_registry_permission(
                db,
                user.email,
                user.roles,
                provider.name,
                new_labels,
                provider.owner_email,
                auth_method=user.auth_method,
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


# Per-platform provider zips can be large; stream them to the PVC. Manifest +
# signature are tiny and read into memory.
_MAX_PROVIDER_BINARY_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB hard cap per platform zip
_MAX_SHASUMS_BYTES = 1 * 1024 * 1024  # 1 MiB — SHA256SUMS / .sig are tiny


def _resolve_ephemeral_tmpdir() -> str | None:
    """Resolve the API pod's ephemeral-storage PVC mount for large tempfiles.

    Matches `run_artifacts._resolve_ephemeral_tmpdir`. On the API pod `/tmp`
    is a RAM-backed emptyDir; provider zips (tens to hundreds of MB) MUST land
    on the dedicated PVC at `settings.vcs.tmpdir`. None falls back to the
    system default for local dev and tests.
    """
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


@management_router.put(_BASE + "/private/default/{name}/versions/{version}/shasums")
async def upload_provider_shasums_endpoint(
    name: str,
    version: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload the client-built SHA256SUMS manifest (first publish step). Requires write.

    Upserts the version. The manifest is not yet trusted — the detached
    signature must be uploaded next and verify against a registered key.
    """
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await _require_provider_permission(db, user, provider, "write")

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty SHA256SUMS body")
    if len(data) > _MAX_SHASUMS_BYTES:
        raise HTTPException(status_code=413, detail="SHA256SUMS too large")

    prov_version = await store_provider_shasums(db, storage, "default", name, version, data)
    await db.commit()
    await db.refresh(prov_version, attribute_names=["platforms"])
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"data": _version_to_jsonapi(prov_version)},
    )


@management_router.put(_BASE + "/private/default/{name}/versions/{version}/shasums.sig")
async def upload_provider_shasums_sig_endpoint(
    name: str,
    version: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload + verify the detached SHA256SUMS signature — the trust gate. Requires write.

    Verifies the signature against a *registered* GPG key over the previously
    uploaded manifest; 422 on any failure. On success the signing key is linked
    so the CLI download advertises it and binary uploads are unblocked.
    """
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await _require_provider_permission(db, user, provider, "write")

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty signature body")
    if len(data) > _MAX_SHASUMS_BYTES:
        raise HTTPException(status_code=413, detail="Signature too large")

    try:
        prov_version = await store_and_verify_provider_sig(
            db, storage, "default", name, version, data
        )
    except PublishValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(prov_version, attribute_names=["platforms"])
    logger.info("Provider SHA256SUMS signature verified", provider=name, version=version)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"data": _version_to_jsonapi(prov_version)},
    )


@management_router.get(_BASE + "/private/default/{name}/versions")
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
        db,
        user.email,
        user.roles,
        provider.name,
        provider.labels or {},
        provider.owner_email,
        auth_method=user.auth_method,
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Provider not found")

    versions = await list_provider_versions(db, provider.id)
    return JSONResponse(
        content={"data": [_version_to_jsonapi(v) for v in versions]},
    )


@management_router.delete(_BASE + "/private/default/{name}/versions/{version}")
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


@management_router.get(_BASE + "/private/default/{name}/versions/{version}/platforms")
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
        db,
        user.email,
        user.roles,
        provider.name,
        provider.labels or {},
        provider.owner_email,
        auth_method=user.auth_method,
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


@management_router.put(_BASE + "/private/default/{name}/versions/{version}/platforms/{os}/{arch}")
async def upload_provider_platform_endpoint(
    name: str,
    version: str,
    request: Request,
    os_: str = Path(alias="os"),
    arch: str = Path(),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Stream + validate a provider platform zip, then store it. Requires write. Idempotent.

    The version's SHA256SUMS and its verified signature must already be
    uploaded (trust gate) — else 422. The body is streamed to the ephemeral
    PVC (never buffered in RAM), its sha-256 computed on the fly, and checked
    against the signed manifest before the file is committed to storage. The
    server never re-signs.
    """
    provider = await get_provider(db, "default", name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await _require_provider_permission(db, user, provider, "write")

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_PROVIDER_BINARY_BYTES:
                raise HTTPException(status_code=413, detail="provider binary too large")
        except ValueError:
            pass

    tmpdir = _resolve_ephemeral_tmpdir()
    fd, tmp_path = await asyncio.to_thread(tempfile.mkstemp, suffix=".provider.zip", dir=tmpdir)
    f = await asyncio.to_thread(os.fdopen, fd, "wb")
    hasher = hashlib.sha256()
    received = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > _MAX_PROVIDER_BINARY_BYTES:
                raise HTTPException(status_code=413, detail="provider binary exceeded size cap")
            hasher.update(chunk)
            await asyncio.to_thread(f.write, chunk)
        await asyncio.to_thread(f.flush)
        await asyncio.to_thread(f.close)
        if received == 0:
            raise HTTPException(status_code=400, detail="Empty request body")

        filename = f"terraform-provider-{name}_{version}_{os_}_{arch}.zip"
        try:
            platform = await record_provider_binary(
                db,
                storage,
                "default",
                name,
                version,
                os_,
                arch,
                sha256=hasher.hexdigest(),
                filename=filename,
                tmp_path=tmp_path,
            )
        except PublishValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await db.commit()
    finally:
        if not f.closed:
            try:
                await asyncio.to_thread(f.close)
            except OSError:
                pass
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    logger.info(
        "Provider binary uploaded",
        provider=name,
        version=version,
        os=os_,
        arch=arch,
        size=received,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"data": _platform_to_jsonapi(platform)},
    )


@management_router.delete(
    _BASE + "/private/default/{name}/versions/{version}/platforms/{os}/{arch}"
)
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
