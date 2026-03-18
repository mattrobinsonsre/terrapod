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

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_non_runner
from terrapod.db.models import ModuleWorkspaceLink, Workspace
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
    REGISTRY_PERMISSION_HIERARCHY,
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
            vcs_connection_id: str = ""
            vcs_repo_url: str = ""
            vcs_branch: str = ""
            vcs_tag_pattern: str = ""

            model_config = {
                "alias_generator": lambda f: f.replace("_", "-"),
                "populate_by_name": True,
            }

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


def _semver_sort_key(version_str: str) -> tuple[int, ...]:
    """Parse a version string into a tuple of ints for sorting."""
    parts: list[int] = []
    for p in version_str.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _module_to_jsonapi(module, effective_permission: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    sorted_versions = sorted(
        module.versions or [], key=lambda v: _semver_sort_key(v.version), reverse=True
    )
    versions = [
        {
            "version": v.version,
            "status": v.upload_status,
            "vcs-commit-sha": v.vcs_commit_sha or "",
            "vcs-tag": v.vcs_tag or "",
        }
        for v in sorted_versions
    ]
    perm = effective_permission
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
            "vcs-connection-id": f"vcs-{module.vcs_connection_id}"
            if module.vcs_connection_id
            else None,
            "vcs-repo-url": module.vcs_repo_url,
            "vcs-branch": module.vcs_branch,
            "vcs-tag-pattern": module.vcs_tag_pattern,
            "vcs-last-tag": module.vcs_last_tag,
            "version-statuses": versions,
            "created-at": module.created_at.isoformat() if module.created_at else None,
            "updated-at": module.updated_at.isoformat() if module.updated_at else None,
            "permissions": {
                "can-update": has_registry_permission(perm, "admin"),
                "can-destroy": has_registry_permission(perm, "admin"),
                "can-create-version": has_registry_permission(perm, "write"),
            },
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

    versions = sorted(
        [{"version": v.version} for v in module.versions if v.upload_status == "uploaded"],
        key=lambda v: _semver_sort_key(v["version"]),
        reverse=True,
    )
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

    url = await get_module_download_url(
        db,
        storage,
        namespace,
        name,
        provider,
        version,
        run_id=getattr(user, "run_id", None),
    )
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
    user: AuthenticatedUser = Depends(require_non_runner),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new registry module. Any authenticated user; creator becomes owner."""
    attrs = body.data.attributes

    module = await create_module(db, "default", attrs.name, attrs.provider)
    module.owner_email = user.email
    module.labels = attrs.labels

    # Apply VCS fields if provided
    if attrs.vcs_connection_id:
        import uuid as _uuid

        from sqlalchemy import select as sa_select

        from terrapod.db.models import VCSConnection

        try:
            conn_id = _uuid.UUID(attrs.vcs_connection_id.removeprefix("vcs-"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid vcs_connection_id") from exc

        result = await db.execute(sa_select(VCSConnection).where(VCSConnection.id == conn_id))
        if result.scalars().first() is None:
            raise HTTPException(status_code=400, detail="VCS connection not found")
        module.vcs_connection_id = conn_id
        module.source = "vcs"
    if attrs.vcs_repo_url:
        module.vcs_repo_url = attrs.vcs_repo_url
    if attrs.vcs_branch:
        module.vcs_branch = attrs.vcs_branch
    if attrs.vcs_tag_pattern:
        module.vcs_tag_pattern = attrs.vcs_tag_pattern

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
            visible.append(_module_to_jsonapi(m, perm))
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

    return JSONResponse(content={"data": _module_to_jsonapi(module, perm)})


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


@router.patch("/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}")
async def update_module_endpoint(
    name: str,
    provider: str,
    body: dict,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a registry module's labels and/or owner. Requires admin on module."""
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

    attrs = body.get("data", {}).get("attributes", {})

    if "owner-email" in attrs:
        if "admin" not in user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform admins can change owner",
            )
        module.owner_email = attrs["owner-email"]

    if "labels" in attrs:
        new_labels = attrs["labels"]
        # Self-lockout check: warn if label change would reduce user's access
        if (
            new_labels != (module.labels or {})
            and not attrs.get("force")
            and "admin" not in user.roles
            and module.owner_email != user.email
        ):
            new_perm = await resolve_registry_permission(
                db, user.email, user.roles, module.name, new_labels, module.owner_email
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
                                    f"{perm} to {new_level} on this module. "
                                    f'Re-submit with "force": true to confirm.'
                                ),
                            }
                        ]
                    },
                )
        module.labels = new_labels

    # VCS fields
    if "vcs-connection-id" in attrs:
        vcs_conn_val = attrs["vcs-connection-id"]
        if vcs_conn_val:
            import uuid as _uuid

            from sqlalchemy import select as sa_select

            from terrapod.db.models import VCSConnection

            try:
                conn_id = _uuid.UUID(str(vcs_conn_val).removeprefix("vcs-"))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid vcs_connection_id") from exc

            result = await db.execute(sa_select(VCSConnection).where(VCSConnection.id == conn_id))
            if result.scalars().first() is None:
                raise HTTPException(status_code=400, detail="VCS connection not found")
            module.vcs_connection_id = conn_id
            module.source = "vcs"
        else:
            module.vcs_connection_id = None
    if "vcs-repo-url" in attrs:
        module.vcs_repo_url = attrs["vcs-repo-url"] or ""
    if "vcs-branch" in attrs:
        module.vcs_branch = attrs["vcs-branch"] or ""
    if "vcs-tag-pattern" in attrs:
        module.vcs_tag_pattern = attrs["vcs-tag-pattern"] or "v*"

    await db.commit()
    await db.refresh(module)
    return JSONResponse(content={"data": _module_to_jsonapi(module, perm)})


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
            conn_id = _uuid.UUID(attrs.vcs_connection_id.removeprefix("vcs-"))
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


# --- Module-Workspace Links (Impact Analysis) ---


class CreateWorkspaceLinkRequest(BaseModel):
    class Data(BaseModel):
        class Attributes(BaseModel):
            workspace_id: str

        type: str = "workspace-links"
        attributes: Attributes

    data: Data


def _link_to_jsonapi(link: ModuleWorkspaceLink) -> dict:
    ws = link.workspace
    return {
        "id": str(link.id),
        "type": "workspace-links",
        "attributes": {
            "workspace-id": str(link.workspace_id),
            "workspace-name": ws.name if ws else "",
            "created-at": link.created_at.isoformat() if link.created_at else None,
            "created-by": link.created_by,
        },
    }


@router.get(
    "/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/workspace-links"
)
async def list_workspace_links(
    name: str,
    provider: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List workspaces linked to this module. Requires read."""
    module = await get_module(db, "default", name, provider)
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    perm = await resolve_registry_permission(
        db, user.email, user.roles, module.name, module.labels or {}, module.owner_email
    )
    if not has_registry_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Module not found")

    result = await db.execute(
        select(ModuleWorkspaceLink)
        .where(ModuleWorkspaceLink.module_id == module.id)
        .options(selectinload(ModuleWorkspaceLink.workspace))
    )
    links = list(result.scalars().all())

    return JSONResponse(content={"data": [_link_to_jsonapi(link) for link in links]})


@router.post(
    "/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/workspace-links"
)
async def create_workspace_link(
    name: str,
    provider: str,
    body: CreateWorkspaceLinkRequest,
    user: AuthenticatedUser = Depends(require_non_runner),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Link a workspace to this module. Requires admin on module."""
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

    ws_id_str = body.data.attributes.workspace_id.removeprefix("ws-")
    try:
        ws_uuid = _uuid.UUID(ws_id_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid workspace ID") from exc

    ws = await db.get(Workspace, ws_uuid)
    if ws is None:
        raise HTTPException(status_code=400, detail="Workspace not found")

    # Check for duplicate
    existing = await db.execute(
        select(ModuleWorkspaceLink).where(
            ModuleWorkspaceLink.module_id == module.id,
            ModuleWorkspaceLink.workspace_id == ws_uuid,
        )
    )
    if existing.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="Workspace already linked")

    link = ModuleWorkspaceLink(
        module_id=module.id,
        workspace_id=ws_uuid,
        created_by=user.email,
    )
    db.add(link)
    await db.flush()
    await db.refresh(link, attribute_names=["workspace"])
    await db.commit()

    logger.info(
        "Module-workspace link created",
        module=name,
        provider=provider,
        workspace=ws.name,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _link_to_jsonapi(link)},
    )


@router.delete(
    "/api/v2/organizations/default/registry-modules/private/default/{name}/{provider}/workspace-links/{link_id}"
)
async def delete_workspace_link(
    name: str,
    provider: str,
    link_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_non_runner),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a workspace link. Requires admin on module."""
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

    try:
        link_uuid = _uuid.UUID(link_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid link ID") from exc

    link = await db.get(ModuleWorkspaceLink, link_uuid)
    if link is None or link.module_id != module.id:
        raise HTTPException(status_code=404, detail="Link not found")

    await db.delete(link)
    await db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
