"""Private module registry endpoints.

Two API surfaces:
1. CLI-facing protocol — what `terraform init` speaks for private registry modules
2. TFE V2 management — JSON:API CRUD for managing modules

UX CONTRACT: Management endpoints are consumed by the web frontend:
  - web/src/app/registry/modules/page.tsx (module list, create)
  - web/src/app/registry/modules/[name]/[provider]/page.tsx (module detail)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to those frontend pages.

CLI Protocol:
    GET  /api/v2/registry/modules/{namespace}/{name}/{provider}/versions
    GET  /api/v2/registry/modules/{namespace}/{name}/{provider}/{version}/download

TFE V2 Management:
    POST   /api/v2/organizations/default/registry-modules
    GET    /api/v2/organizations/default/registry-modules
    GET    /api/v2/organizations/default/registry-modules/private/default/{name}/{prov}
    DELETE /api/v2/organizations/default/registry-modules/private/default/{name}/{prov}
    POST   /api/v2/organizations/default/registry-modules/private/default/{name}/{prov}/versions
    DELETE /api/v2/organizations/default/registry-modules/private/default/{name}/{prov}/{ver}
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.registry_module_service import (
    create_module,
    create_module_version,
    delete_module,
    delete_module_version,
    get_module,
    get_module_download_url,
    list_modules,
    upload_module_tarball,
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
    versions = [{"version": v.version, "status": v.upload_status} for v in (module.versions or [])]
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
            "source": module.source,
            "vcs-connection-id": str(module.vcs_connection_id)
            if module.vcs_connection_id
            else None,
            "vcs-repo-url": module.vcs_repo_url,
            "vcs-branch": module.vcs_branch,
            "vcs-tag-pattern": module.vcs_tag_pattern,
            "vcs-last-tag": module.vcs_last_tag,
            "version-statuses": versions,
            "created-at": module.created_at.isoformat() if module.created_at else None,
            "updated-at": module.updated_at.isoformat() if module.updated_at else None,
        },
    }


# --- CLI Protocol Endpoints ---


@router.get("/api/v2/registry/modules/{namespace}/{name}/{provider}/versions")
async def list_module_versions_cli(
    namespace: str,
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List available versions for a module (CLI protocol). Requires read."""
    module = await get_module(db, namespace, name, provider)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Module not found")

    versions = [{"version": v.version} for v in module.versions if v.upload_status == "uploaded"]
    return JSONResponse(
        content={
            "modules": [{"versions": versions}],
        }
    )


@router.get("/api/v2/registry/modules/{namespace}/{name}/{provider}/{version}/download")
async def download_module_cli(
    namespace: str,
    name: str,
    provider: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Get download URL for a module version (CLI protocol). Requires read."""
    module = await get_module(db, namespace, name, provider)
    if module is not None:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
        )
        if not has_registry_permission(perm, "read"):
            raise HTTPException(status_code=404, detail="Module version not found")

    url = await get_module_download_url(db, storage, namespace, name, provider, version)
    if url is None:
        raise HTTPException(status_code=404, detail="Module version not found")

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Terraform-Get": url},
    )


# --- TFE V2 Management Endpoints ---


@router.post("/api/v2/organizations/default/registry-modules")
async def create_module_endpoint(
    body: CreateModuleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new registry module. Any authenticated user; creator becomes owner."""
    attrs = body.data.attributes

    module = await create_module(db, "default", attrs.name, attrs.provider)
    module.owner_email = user.email
    module.labels = attrs.labels
    await db.commit()
    await db.refresh(module, attribute_names=["versions"])

    logger.info(
        "Registry module created",
        name=attrs.name,
        provider=attrs.provider,
        owner=user.email,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _module_to_jsonapi(module)},
    )


@router.get("/api/v2/organizations/default/registry-modules")
async def list_modules_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all registry modules (filtered by permissions)."""
    modules = await list_modules(db)
    visible = []
    for m in modules:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, m.name, m.labels or {}, m.owner_email
        )
        if perm is not None:
            visible.append(_module_to_jsonapi(m))
    return JSONResponse(content={"data": visible})


@router.get("/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}")
async def show_module_endpoint(
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a specific registry module. Requires read."""
    module = await get_module(db, "default", name, provider)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Module not found")

    return JSONResponse(content={"data": _module_to_jsonapi(module)})


@router.delete("/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}")
async def delete_module_endpoint(
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Delete a registry module and all its versions. Requires admin on module."""
    module = await get_module(db, "default", name, provider)
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

    deleted = await delete_module(db, storage, "default", name, provider)
    if not deleted:
        raise HTTPException(status_code=404, detail="Module not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/versions"
)
async def create_module_version_endpoint(
    name: str,
    provider: str,
    body: CreateModuleVersionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Create a new module version and get an upload URL. Requires write."""
    module = await get_module(db, "default", name, provider)
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
    mod_version, upload_url = await create_module_version(db, storage, module.id, version_str)
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
    "/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/{version}"
)
async def delete_module_version_endpoint(
    name: str,
    provider: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Delete a specific module version. Requires admin on module."""
    module = await get_module(db, "default", name, provider)
    if module is not None:
        perm = await resolve_registry_permission(
            db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
        )
        if not has_registry_permission(perm, "admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires admin permission on module",
            )

    deleted = await delete_module_version(db, storage, "default", name, provider, version)
    if not deleted:
        raise HTTPException(status_code=404, detail="Module version not found")

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Direct Upload ---


@router.put(
    "/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/versions/{version}/upload"
)
async def upload_module_version_endpoint(
    name: str,
    provider: str,
    version: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload a module tarball directly. Requires write. Idempotent."""
    module = await get_module(db, "default", name, provider)
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

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")

    mod_version = await upload_module_tarball(db, storage, "default", name, provider, version, data)
    await db.commit()

    logger.info(
        "Module tarball uploaded",
        module=name,
        provider=provider,
        version=version,
        size=len(data),
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
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
            }
        },
    )


# --- VCS Configuration ---


class UpdateModuleVCSRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            source: str = "vcs"
            vcs_connection_id: str = ""
            vcs_repo_url: str = ""
            vcs_branch: str = ""
            vcs_tag_pattern: str = "v*"

        type: str = "registry-modules"
        attributes: Attributes

    data: Data


@router.patch(
    "/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/vcs"
)
async def update_module_vcs_endpoint(
    name: str,
    provider: str,
    body: UpdateModuleVCSRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Configure VCS source for a module. Requires admin."""
    module = await get_module(db, "default", name, provider)
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

    attrs = body.data.attributes

    # Validate VCS connection exists if provided
    if attrs.vcs_connection_id:
        import uuid as _uuid

        from sqlalchemy import select as sa_select

        from terrapod.db.models import VCSConnection

        try:
            conn_id = _uuid.UUID(attrs.vcs_connection_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid vcs_connection_id") from exc

        result = await db.execute(sa_select(VCSConnection).where(VCSConnection.id == conn_id))
        if result.scalars().first() is None:
            raise HTTPException(status_code=400, detail="VCS connection not found")
        module.vcs_connection_id = conn_id
    else:
        module.vcs_connection_id = None

    module.source = attrs.source
    module.vcs_repo_url = attrs.vcs_repo_url
    module.vcs_branch = attrs.vcs_branch
    module.vcs_tag_pattern = attrs.vcs_tag_pattern or "v*"
    await db.commit()
    await db.refresh(module, attribute_names=["versions"])

    logger.info(
        "Module VCS configuration updated",
        module=name,
        provider=provider,
        source=attrs.source,
    )

    return JSONResponse(content={"data": _module_to_jsonapi(module)})
