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
    org: str,
    namespace: str,
    name: str,
    provider: str,
) -> RegistryModule:
    """Create a new registry module."""
    module = RegistryModule(
        org_name=org,
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
    org: str,
) -> list[RegistryModule]:
    """List all registry modules for an organization."""
    result = await db.execute(
        select(RegistryModule)
        .where(RegistryModule.org_name == org)
        .options(selectinload(RegistryModule.versions))
        .order_by(RegistryModule.name)
    )
    return list(result.scalars().all())


async def get_module(
    db: AsyncSession,
    org: str,
    namespace: str,
    name: str,
    provider: str,
) -> RegistryModule | None:
    """Get a registry module by its identifying tuple."""
    result = await db.execute(
        select(RegistryModule)
        .where(
            RegistryModule.org_name == org,
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
    org: str,
    namespace: str,
    name: str,
    provider: str,
) -> bool:
    """Delete a module and all its versions. Returns True if found."""
    module = await get_module(db, org, namespace, name, provider)
    if module is None:
        return False

    # Clean up storage for all versions
    for version in module.versions:
        key = module_tarball_key(org, namespace, name, provider, version.version)
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
    result = await db.execute(
        select(RegistryModule).where(RegistryModule.id == module_id)
    )
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
    key = module_tarball_key(
        module.org_name, module.namespace, module.name, module.provider, version
    )
    upload_url = await storage.presigned_put_url(key, content_type="application/gzip")

    # Update module status
    module.status = "setup_complete"
    await db.flush()

    return mod_version, upload_url


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
    org: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> bool:
    """Delete a specific module version."""
    module = await get_module(db, org, namespace, name, provider)
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

    key = module_tarball_key(org, namespace, name, provider, version)
    await storage.delete(key)
    await db.delete(mod_version)
    await db.flush()
    return True


async def get_module_download_url(
    db: AsyncSession,
    storage: ObjectStore,
    org: str,
    namespace: str,
    name: str,
    provider: str,
    version: str,
) -> str | None:
    """Get a presigned download URL for a module version tarball."""
    module = await get_module(db, org, namespace, name, provider)
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

    key = module_tarball_key(org, namespace, name, provider, version)
    presigned = await storage.presigned_get_url(key)
    return presigned.url
