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

    # Deliberately not `setup_complete` yet: the tarball has not arrived. The
    # module used to report complete here while its only version was unusable
    # (#1707). It becomes complete when the upload is seen -- see
    # `finalize_presigned_uploads`.
    await db.flush()

    return mod_version, upload_url


async def finalize_presigned_uploads(
    db: AsyncSession, module: RegistryModule, storage: ObjectStore | None = None
) -> None:
    """Mark a version uploaded once its presigned upload has landed (#1707).

    The documented publishing flow creates a version, then PUTs the tarball to
    the version's presigned URL. That PUT goes straight to object storage, which
    cannot tell the API it happened -- so the version stayed `pending` forever,
    the CLI listing (which serves only `uploaded` versions) stayed empty, and
    every response along the way reported success.

    Called on the read paths that care, this finds pending versions whose
    tarball now exists and finishes what the direct upload endpoint does: marks
    them uploaded, parses the interface, marks the module complete, and queues
    runs on linked workspaces. A version whose tarball has not arrived is left
    pending. Cheap when there is nothing to do, which is the usual case.
    """
    pending = [v for v in module.versions if v.upload_status == "pending"]
    if not pending:
        return

    # A read path must not write on an HA follower: its database is a replica,
    # and it cannot create runs anyway. The leader finalizes on its next read.
    from terrapod.services import ha_role

    if not await ha_role.is_leader():
        return

    if storage is None:
        # Resolved only when there is something to check, so the common read
        # of a fully-published module touches nothing new.
        from terrapod.storage import get_storage

        storage = get_storage()

    for mod_version in pending:
        key = module_tarball_key(
            module.namespace, module.name, module.provider, mod_version.version
        )
        try:
            if not await storage.exists(key):
                continue
        except Exception:
            logger.warning("Could not check a pending module upload", key=key, exc_info=True)
            continue

        # Claim the transition. Two reads arriving together -- two `tofu init`s,
        # or an init and a UI view, on one replica or two -- both see `pending`.
        # Lock the row, skipping it if another read already holds the lock, and
        # re-check it is still pending: only one read goes on, so linked-workspace
        # runs are queued once. Through the ORM rather than a bulk UPDATE, so the
        # change reaches the replication outbox.
        claimed = (
            await db.execute(
                select(RegistryModuleVersion)
                .where(
                    RegistryModuleVersion.id == mod_version.id,
                    RegistryModuleVersion.upload_status == "pending",
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if claimed is None:
            continue
        mod_version = claimed

        mod_version.upload_status = "uploaded"
        module.status = "setup_complete"
        await _parse_interface_from_storage(storage, key, mod_version)
        await db.flush()
        logger.info(
            "Finalized a presigned module upload",
            module=module.name,
            provider=module.provider,
            version=mod_version.version,
        )

        # In a savepoint: a database error while creating runs must not leave the
        # session failed, or the read that triggered this -- the CLI's version
        # listing -- would fail at commit.
        try:
            from terrapod.services.module_impact_service import trigger_linked_workspace_runs

            async with db.begin_nested():
                await trigger_linked_workspace_runs(db, module, mod_version.version)
        except Exception:
            logger.warning(
                "Failed to trigger linked workspace runs after a presigned upload",
                module=module.name,
                version=mod_version.version,
                exc_info=True,
            )


async def _parse_interface_from_storage(
    storage: ObjectStore, key: str, mod_version: RegistryModuleVersion
) -> None:
    """Parse a stored tarball's interface onto the version, if enabled.

    The tarball is streamed to a tempfile on the ephemeral PVC and parsed from
    disk in a worker thread (CLAUDE.md #13, #14); it is never held in memory.
    """
    from terrapod.config import settings

    if not settings.registry.module_interface.enabled:
        return

    import asyncio
    import os
    import tempfile

    from terrapod.services.module_hcl_parser import extract_module_interface_result_from_file

    configured = settings.vcs.tmpdir
    tmpdir = configured if configured and os.path.isdir(configured) else None
    fd, path = tempfile.mkstemp(suffix=".tar.gz", dir=tmpdir)
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in storage.get_stream(key):
                await asyncio.to_thread(fh.write, chunk)
        interface = await asyncio.to_thread(extract_module_interface_result_from_file, path)
        mod_version.inputs = interface["inputs"]
        mod_version.outputs = interface["outputs"]
        # Set on failure, cleared on success, as every other writer does (#1707).
        mod_version.interface_error = interface["error"]
    except Exception:
        logger.warning("Failed to extract module interface after upload", key=key, exc_info=True)
        mod_version.interface_error = "The module interface could not be read."
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


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
    tarball_path: str,
) -> RegistryModuleVersion:
    """Upload a module tarball directly. Upserts version, stores tarball.

    `tarball_path` is a file on the API pod's ephemeral PVC (the caller
    streams the request body to it). The tarball is streamed into storage
    and parsed from disk — never buffered in the worker heap (CLAUDE.md #14).
    """
    from terrapod.api.upload_stream import file_chunks

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
    await storage.put_stream(key, file_chunks(tarball_path), content_type="application/gzip")

    mod_version.upload_status = "uploaded"

    from terrapod.config import settings

    if settings.registry.module_interface.enabled:
        import asyncio

        from terrapod.services.module_hcl_parser import (
            extract_module_interface_result_from_file,
        )

        try:
            interface = await asyncio.to_thread(
                extract_module_interface_result_from_file, tarball_path
            )
            mod_version.inputs = interface["inputs"]
            mod_version.outputs = interface["outputs"]
            # Set on failure and cleared on success (#1707), so a re-upload
            # that fixes the module also clears the warning.
            mod_version.interface_error = interface["error"]
        except Exception:
            logger.warning("Failed to extract module interface on upload", exc_info=True)
            mod_version.interface_error = "The module interface could not be read."

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
