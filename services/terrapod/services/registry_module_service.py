"""Service layer for private module registry operations.

Handles CRUD for registry modules and versions, with presigned URL
generation for tarball upload/download via object storage.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import RegistryModule, RegistryModuleVersion
from terrapod.logging_config import get_logger
from terrapod.storage.keys import module_tarball_key
from terrapod.storage.protocol import ObjectStore, PresignedURL

logger = get_logger(__name__)


async def create_module(
    db: AsyncSession,
    namespace: str,
    name: str,
    provider: str,
) -> RegistryModule:
    """Create a new registry module."""
    module = RegistryModule(
        namespace=namespace,
        name=name,
        provider=provider,
        status="pending",
    )
    db.add(module)
    await db.flush()
    return module


async def list_modules(
    db: AsyncSession,
) -> list[RegistryModule]:
    """List all registry modules."""
    result = await db.execute(
        select(RegistryModule)
        .options(selectinload(RegistryModule.versions))
        .order_by(RegistryModule.name)
    )
    return list(result.scalars().all())


async def get_module(
    db: AsyncSession,
    namespace: str,
    name: str,
    provider: str,
) -> RegistryModule | None:
    """Get a registry module by its identifying tuple."""
    result = await db.execute(
        select(RegistryModule)
        .where(
            RegistryModule.namespace == namespace,
            RegistryModule.name == name,
            RegistryModule.provider == provider,
        )
        .options(selectinload(RegistryModule.versions))
    )
    return result.scalars().first()


async def delete_module(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    provider: str,
) -> bool:
    """Delete a module and all its versions. Returns True if found."""
    module = await get_module(db, namespace, name, provider)
    if module is None:
        return False

    # Clean up storage for all versions
    for version in module.versions:
        key = module_tarball_key(namespace, name, provider, version.version)
        await storage.delete(key)

    await db.delete(module)
    await db.flush()
    return True


async def create_module_version(
    db: AsyncSession,
    storage: ObjectStore,
    module_id: uuid.UUID,
    version: str,
) -> tuple[RegistryModuleVersion, PresignedURL]:
    """Create a new module version and return an upload URL for the tarball."""
    # Get the module to build the storage key
    result = await db.execute(select(RegistryModule).where(RegistryModule.id == module_id))
    module = result.scalars().first()
    if module is None:
        raise ValueError(f"Module {module_id} not found")

    mod_version = RegistryModuleVersion(
        module_id=module_id,
        version=version,
        upload_status="pending",
    )
    db.add(mod_version)
    await db.flush()

    # Generate presigned upload URL
    key = module_tarball_key(module.namespace, module.name, module.provider, version)
    upload_url = await storage.presigned_put_url(key, content_type="application/gzip")

    # Update module status
    module.status = "setup_complete"
    await db.flush()

    return mod_version, upload_url


async def upsert_module_version(
    db: AsyncSession,
    module_id: uuid.UUID,
    version: str,
) -> RegistryModuleVersion:
    """Get or create a module version record."""
    result = await db.execute(
        select(RegistryModuleVersion).where(
            RegistryModuleVersion.module_id == module_id,
            RegistryModuleVersion.version == version,
        )
    )
    mod_version = result.scalars().first()
    if mod_version is not None:
        return mod_version

    mod_version = RegistryModuleVersion(
        module_id=module_id,
        version=version,
        upload_status="pending",
    )
    db.add(mod_version)
    await db.flush()
    return mod_version


async def upload_module_tarball(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    provider: str,
    version: str,
    data: bytes,
) -> RegistryModuleVersion:
    """Upload a module tarball directly. Upserts version, stores tarball."""
    module = await get_module(db, namespace, name, provider)
    if module is None:
        raise ValueError(f"Module {namespace}/{name}/{provider} not found")

    is_new = (
        await db.execute(
            select(RegistryModuleVersion).where(
                RegistryModuleVersion.module_id == module.id,
                RegistryModuleVersion.version == version,
            )
        )
    ).scalars().first() is None

    mod_version = await upsert_module_version(db, module.id, version)

    key = module_tarball_key(namespace, name, provider, version)
    await storage.put(key, data, "application/gzip")

    mod_version.upload_status = "uploaded"
    module.status = "setup_complete"
    await db.flush()

    # Trigger runs on linked workspaces for new versions
    if is_new:
        try:
            from terrapod.services.module_impact_service import trigger_linked_workspace_runs

            await trigger_linked_workspace_runs(db, module, version)
        except Exception:
            logger.warning(
                "Failed to trigger linked workspace runs on upload",
                module=name,
                version=version,
                exc_info=True,
            )

    return mod_version


async def confirm_module_upload(
    db: AsyncSession,
    storage: ObjectStore,
    version_id: uuid.UUID,
) -> RegistryModuleVersion | None:
    """Confirm a module version upload is complete."""
    result = await db.execute(
        select(RegistryModuleVersion).where(RegistryModuleVersion.id == version_id)
    )
    mod_version = result.scalars().first()
    if mod_version is None:
        return None

    mod_version.upload_status = "uploaded"
    await db.flush()
    return mod_version


async def delete_module_version(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> bool:
    """Delete a specific module version."""
    module = await get_module(db, namespace, name, provider)
    if module is None:
        return False

    result = await db.execute(
        select(RegistryModuleVersion).where(
            RegistryModuleVersion.module_id == module.id,
            RegistryModuleVersion.version == version,
        )
    )
    mod_version = result.scalars().first()
    if mod_version is None:
        return False

    key = module_tarball_key(namespace, name, provider, version)
    await storage.delete(key)
    await db.delete(mod_version)
    await db.flush()
    return True


async def get_module_download_url(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    provider: str,
    version: str,
    run_id: str | None = None,
) -> str | None:
    """Get a presigned download URL for a module version tarball.

    If run_id is provided and the run has module_overrides for this module,
    the override tarball is returned instead of the published version.
    """
    # Check for module override first (module impact analysis)
    if run_id:
        from terrapod.db.models import Run

        try:
            run = await db.get(Run, uuid.UUID(run_id))
        except (ValueError, AttributeError):
            run = None
        if run and run.module_overrides:
            coord = f"{namespace}/{name}/{provider}"
            override_path = run.module_overrides.get(coord)
            if override_path:
                presigned = await storage.presigned_get_url(override_path)
                return presigned.url

    # Normal path: look up published version
    module = await get_module(db, namespace, name, provider)
    if module is None:
        return None

    result = await db.execute(
        select(RegistryModuleVersion).where(
            RegistryModuleVersion.module_id == module.id,
            RegistryModuleVersion.version == version,
        )
    )
    mod_version = result.scalars().first()
    if mod_version is None:
        return None

    key = module_tarball_key(namespace, name, provider, version)
    presigned = await storage.presigned_get_url(key)
    return presigned.url
