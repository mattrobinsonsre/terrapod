"""Private module registry endpoints.

Two API surfaces:
1. CLI-facing protocol — what `terraform init` speaks for private registry modules
2. TFE V2 management — JSON:API CRUD for managing modules

CLI Protocol:
    GET  /api/v2/registry/modules/{org}/{name}/{provider}/versions
    GET  /api/v2/registry/modules/{org}/{name}/{provider}/{version}/download

TFE V2 Management:
    POST   /api/v2/organizations/{org}/registry-modules
    GET    /api/v2/organizations/{org}/registry-modules
    GET    /api/v2/organizations/{org}/registry-modules/private/{ns}/{name}/{prov}
    DELETE /api/v2/organizations/{org}/registry-modules/private/{ns}/{name}/{prov}
    POST   /api/v2/organizations/{org}/registry-modules/private/{ns}/{name}/{prov}/versions
    DELETE /api/v2/organizations/{org}/registry-modules/private/{ns}/{name}/{prov}/{ver}
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.registry_module_service import (
    confirm_module_upload,
    create_module,
    create_module_version,
    delete_module,
    delete_module_version,
    get_module,
    get_module_download_url,
    list_modules,
)
from terrapod.services.registry_rbac_service import (
    has_registry_permission,
    resolve_registry_permission,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(tags=["registry-modules"])
logger = get_logger(__name__)


# --- Pydantic Request Models ---


class CreateModuleRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            name: str
            provider: str
            namespace: str = ""
            labels: dict = {}

        type: str = "registry-modules"
        attributes: Attributes

    data: Data


class CreateModuleVersionRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            version: str

        type: str = "registry-module-versions"
        attributes: Attributes

    data: Data


# --- JSON:API serialization ---


def _module_to_jsonapi(module) -> dict:  # type: ignore[no-untyped-def]
    versions = [
        {"version": v.version, "status": v.upload_status}
        for v in (module.versions or [])
    ]
    return {
        "id": str(module.id),
        "type": "registry-modules",
        "attributes": {
            "name": module.name,
            "namespace": module.namespace,
            "provider": module.provider,
            "status": module.status,
            "labels": module.labels or {},
            "owner-email": module.owner_email,
            "version-statuses": versions,
            "created-at": module.created_at.isoformat() if module.created_at else None,
            "updated-at": module.updated_at.isoformat() if module.updated_at else None,
        },
    }


# --- CLI Protocol Endpoints ---


@router.get("/api/v2/registry/modules/{org}/{name}/{provider}/versions")
async def list_module_versions_cli(
    org: str,
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List available versions for a module (CLI protocol). Requires read."""
    module = await get_module(db, org, org, name, provider)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Module not found")

    versions = [
        {"version": v.version}
        for v in module.versions
        if v.upload_status == "uploaded"
    ]
    return JSONResponse(
        content={
            "modules": [{"versions": versions}],
        }
    )


@router.get("/api/v2/registry/modules/{org}/{name}/{provider}/{version}/download")
async def download_module_cli(
    org: str,
    name: str,
    provider: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Get download URL for a module version (CLI protocol). Requires read."""
    module = await get_module(db, org, org, name, provider)
    if module is not None:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
        )
        if not has_registry_permission(perm, "read"):
            raise HTTPException(status_code=404, detail="Module version not found")

    url = await get_module_download_url(db, storage, org, org, name, provider, version)
    if url is None:
        raise HTTPException(status_code=404, detail="Module version not found")

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Terraform-Get": url},
    )


# --- TFE V2 Management Endpoints ---


@router.post("/api/v2/organizations/{org}/registry-modules")
async def create_module_endpoint(
    org: str,
    body: CreateModuleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new registry module. Any authenticated user; creator becomes owner."""
    attrs = body.data.attributes
    namespace = attrs.namespace or org

    module = await create_module(db, org, namespace, attrs.name, attrs.provider)
    module.owner_email = user.email
    module.labels = attrs.labels
    await db.commit()

    logger.info(
        "Registry module created",
        org=org,
        name=attrs.name,
        provider=attrs.provider,
        owner=user.email,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _module_to_jsonapi(module)},
    )


@router.get("/api/v2/organizations/{org}/registry-modules")
async def list_modules_endpoint(
    org: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all registry modules for an organization (filtered by permissions)."""
    modules = await list_modules(db, org)
    visible = []
    for m in modules:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, m.name, m.labels or {}, m.owner_email
        )
        if perm is not None:
            visible.append(_module_to_jsonapi(m))
    return JSONResponse(content={"data": visible})


@router.get("/api/v2/organizations/{org}/registry-modules/private/{namespace}/{name}/{provider}")
async def show_module_endpoint(
    org: str,
    namespace: str,
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a specific registry module. Requires read."""
    module = await get_module(db, org, namespace, name, provider)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Module not found")

    return JSONResponse(content={"data": _module_to_jsonapi(module)})


@router.delete("/api/v2/organizations/{org}/registry-modules/private/{namespace}/{name}/{provider}")
async def delete_module_endpoint(
    org: str,
    namespace: str,
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Delete a registry module and all its versions. Requires admin on module."""
    module = await get_module(db, org, namespace, name, provider)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
    )
    if not has_registry_permission(perm, "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin permission on module",
        )

    deleted = await delete_module(db, storage, org, namespace, name, provider)
    if not deleted:
        raise HTTPException(status_code=404, detail="Module not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/api/v2/organizations/{org}/registry-modules/private/{namespace}/{name}/{provider}/versions"
)
async def create_module_version_endpoint(
    org: str,
    namespace: str,
    name: str,
    provider: str,
    body: CreateModuleVersionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Create a new module version and get an upload URL. Requires write."""
    module = await get_module(db, org, namespace, name, provider)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
    )
    if not has_registry_permission(perm, "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires write permission on module",
        )

    version_str = body.data.attributes.version
    mod_version, upload_url = await create_module_version(
        db, storage, module.id, version_str
    )
    await db.commit()

    logger.info(
        "Module version created",
        module_id=str(module.id),
        version=version_str,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "data": {
                "id": str(mod_version.id),
                "type": "registry-module-versions",
                "attributes": {
                    "version": mod_version.version,
                    "status": mod_version.upload_status,
                    "created-at": mod_version.created_at.isoformat()
                    if mod_version.created_at
                    else None,
                },
                "links": {
                    "upload": upload_url.url,
                },
            }
        },
    )


@router.delete(
    "/api/v2/organizations/{org}/registry-modules/private/{namespace}/{name}/{provider}/{version}"
)
async def delete_module_version_endpoint(
    org: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Delete a specific module version. Requires admin on module."""
    module = await get_module(db, org, namespace, name, provider)
    if module is not None:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
        )
        if not has_registry_permission(perm, "admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires admin permission on module",
            )

    deleted = await delete_module_version(db, storage, org, namespace, name, provider, version)
    if not deleted:
        raise HTTPException(status_code=404, detail="Module version not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
