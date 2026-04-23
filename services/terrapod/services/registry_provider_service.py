"""Service layer for private provider registry operations.

Handles CRUD for registry providers, versions, and platforms, with presigned
URL generation for binary upload/download via object storage.
"""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import (
    GPGKey,
    RegistryProvider,
    RegistryProviderPlatform,
    RegistryProviderVersion,
)
from terrapod.logging_config import get_logger
from terrapod.storage.keys import (
    provider_binary_key,
    provider_shasums_key,
    provider_shasums_sig_key,
)
from terrapod.storage.protocol import ObjectStore, PresignedURL

logger = get_logger(__name__)


# --- Provider CRUD ---


async def create_provider(
    db: AsyncSession,
    namespace: str,
    name: str,
) -> RegistryProvider:
    """Create a new registry provider."""
    provider = RegistryProvider(
        namespace=namespace,
        name=name,
    )
    db.add(provider)
    await db.flush()
    return provider


async def list_providers(
    db: AsyncSession,
) -> list[RegistryProvider]:
    """List all registry providers."""
    result = await db.execute(
        select(RegistryProvider)
        .options(
            selectinload(RegistryProvider.versions).selectinload(RegistryProviderVersion.platforms)
        )
        .order_by(RegistryProvider.name)
    )
    return list(result.scalars().all())


async def get_provider(
    db: AsyncSession,
    namespace: str,
    name: str,
) -> RegistryProvider | None:
    """Get a registry provider by its identifying tuple."""
    result = await db.execute(
        select(RegistryProvider)
        .where(
            RegistryProvider.namespace == namespace,
            RegistryProvider.name == name,
        )
        .options(
            selectinload(RegistryProvider.versions).selectinload(RegistryProviderVersion.platforms)
        )
    )
    return result.scalars().first()


async def delete_provider(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
) -> bool:
    """Delete a provider and all its versions/platforms. Returns True if found."""
    provider = await get_provider(db, namespace, name)
    if provider is None:
        return False

    # Clean up storage for all versions and platforms
    for version in provider.versions:
        await _delete_version_storage(storage, namespace, name, version)

    await db.delete(provider)
    await db.flush()
    return True


# --- Version CRUD ---


async def create_provider_version(
    db: AsyncSession,
    storage: ObjectStore,
    provider_id: uuid.UUID,
    version: str,
    gpg_key_id: uuid.UUID | None = None,
    protocols: list[str] | None = None,
) -> tuple[RegistryProviderVersion, PresignedURL, PresignedURL]:
    """Create a provider version and return upload URLs for shasums + sig."""
    result = await db.execute(select(RegistryProvider).where(RegistryProvider.id == provider_id))
    provider = result.scalars().first()
    if provider is None:
        raise ValueError(f"Provider {provider_id} not found")

    prov_version = RegistryProviderVersion(
        provider_id=provider_id,
        version=version,
        gpg_key_id=gpg_key_id,
        protocols=protocols or ["5.0"],
    )
    db.add(prov_version)
    await db.flush()

    # Generate presigned upload URLs for shasums files
    shasums_key = provider_shasums_key(provider.namespace, provider.name, version)
    sig_key = provider_shasums_sig_key(provider.namespace, provider.name, version)
    shasums_url = await storage.presigned_put_url(shasums_key, content_type="text/plain")
    sig_url = await storage.presigned_put_url(sig_key, content_type="application/octet-stream")

    return prov_version, shasums_url, sig_url


async def list_provider_versions(
    db: AsyncSession,
    provider_id: uuid.UUID,
) -> list[RegistryProviderVersion]:
    """List all versions for a provider."""
    result = await db.execute(
        select(RegistryProviderVersion)
        .where(RegistryProviderVersion.provider_id == provider_id)
        .options(selectinload(RegistryProviderVersion.platforms))
        .order_by(RegistryProviderVersion.created_at.desc())
    )
    return list(result.scalars().all())


async def get_provider_version(
    db: AsyncSession,
    provider_id: uuid.UUID,
    version: str,
) -> RegistryProviderVersion | None:
    """Get a specific provider version."""
    result = await db.execute(
        select(RegistryProviderVersion)
        .where(
            RegistryProviderVersion.provider_id == provider_id,
            RegistryProviderVersion.version == version,
        )
        .options(selectinload(RegistryProviderVersion.platforms))
    )
    return result.scalars().first()


async def delete_provider_version(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: str,
) -> bool:
    """Delete a provider version and its platforms."""
    provider = await get_provider(db, namespace, name)
    if provider is None:
        return False

    prov_version = await get_provider_version(db, provider.id, version)
    if prov_version is None:
        return False

    await _delete_version_storage(storage, namespace, name, prov_version)
    await db.delete(prov_version)
    await db.flush()
    return True


# --- Platform CRUD ---


async def create_provider_platform(
    db: AsyncSession,
    storage: ObjectStore,
    version_id: uuid.UUID,
    os_: str,
    arch: str,
    filename: str,
) -> tuple[RegistryProviderPlatform, PresignedURL]:
    """Create a platform entry and return an upload URL for the binary."""
    # Look up the version to get provider info for storage key
    result = await db.execute(
        select(RegistryProviderVersion)
        .where(RegistryProviderVersion.id == version_id)
        .options(selectinload(RegistryProviderVersion.provider))
    )
    prov_version = result.scalars().first()
    if prov_version is None:
        raise ValueError(f"Provider version {version_id} not found")

    provider = prov_version.provider

    platform = RegistryProviderPlatform(
        version_id=version_id,
        os=os_,
        arch=arch,
        filename=filename,
        upload_status="pending",
    )
    db.add(platform)
    await db.flush()

    key = provider_binary_key(
        provider.namespace,
        provider.name,
        prov_version.version,
        os_,
        arch,
    )
    upload_url = await storage.presigned_put_url(key, content_type="application/zip")

    return platform, upload_url


async def list_provider_platforms(
    db: AsyncSession,
    version_id: uuid.UUID,
) -> list[RegistryProviderPlatform]:
    """List all platforms for a provider version."""
    result = await db.execute(
        select(RegistryProviderPlatform)
        .where(RegistryProviderPlatform.version_id == version_id)
        .order_by(RegistryProviderPlatform.os, RegistryProviderPlatform.arch)
    )
    return list(result.scalars().all())


async def delete_provider_platform(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: str,
    os_: str,
    arch: str,
) -> bool:
    """Delete a specific provider platform binary."""
    provider = await get_provider(db, namespace, name)
    if provider is None:
        return False

    prov_version = await get_provider_version(db, provider.id, version)
    if prov_version is None:
        return False

    result = await db.execute(
        select(RegistryProviderPlatform).where(
            RegistryProviderPlatform.version_id == prov_version.id,
            RegistryProviderPlatform.os == os_,
            RegistryProviderPlatform.arch == arch,
        )
    )
    platform = result.scalars().first()
    if platform is None:
        return False

    key = provider_binary_key(namespace, name, version, os_, arch)
    await storage.delete(key)
    await db.delete(platform)
    await db.flush()
    return True


# --- Download Info (CLI protocol) ---


async def get_provider_download_info(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: str,
    os_: str,
    arch: str,
) -> dict | None:
    """Assemble the download info response for `terraform init`.

    Returns the dict that the CLI expects with download_url, shasums_url,
    signing_keys, etc. Returns None if not found.
    """
    provider = await get_provider(db, namespace, name)
    if provider is None:
        return None

    prov_version = await get_provider_version(db, provider.id, version)
    if prov_version is None:
        return None

    # Find the platform
    result = await db.execute(
        select(RegistryProviderPlatform).where(
            RegistryProviderPlatform.version_id == prov_version.id,
            RegistryProviderPlatform.os == os_,
            RegistryProviderPlatform.arch == arch,
        )
    )
    platform = result.scalars().first()
    if platform is None:
        return None

    # Generate presigned URLs
    binary_k = provider_binary_key(namespace, name, version, os_, arch)
    shasums_k = provider_shasums_key(namespace, name, version)
    sig_k = provider_shasums_sig_key(namespace, name, version)

    download_url = await storage.presigned_get_url(binary_k)
    shasums_url = await storage.presigned_get_url(shasums_k)
    sig_url = await storage.presigned_get_url(sig_k)

    # Build signing_keys from GPG key
    signing_keys: list[dict] = []
    if prov_version.gpg_key_id is not None:
        gpg_result = await db.execute(select(GPGKey).where(GPGKey.id == prov_version.gpg_key_id))
        gpg_key = gpg_result.scalars().first()
        if gpg_key is not None:
            signing_keys.append(
                {
                    "ascii_armor": gpg_key.ascii_armor,
                    "key_id": gpg_key.key_id,
                    "source": gpg_key.source,
                    "source_url": gpg_key.source_url or "",
                }
            )

    return {
        "protocols": prov_version.protocols,
        "os": os_,
        "arch": arch,
        "filename": platform.filename,
        "download_url": download_url.url,
        "shasums_url": shasums_url.url,
        "shasums_signature_url": sig_url.url,
        "shasum": platform.shasum,
        "signing_keys": {
            "gpg_public_keys": signing_keys,
        },
    }


# --- Upsert helpers (for direct upload flow) ---


async def upsert_provider_version(
    db: AsyncSession,
    provider_id: uuid.UUID,
    version: str,
) -> RegistryProviderVersion:
    """Get or create a provider version record."""
    result = await db.execute(
        select(RegistryProviderVersion)
        .where(
            RegistryProviderVersion.provider_id == provider_id,
            RegistryProviderVersion.version == version,
        )
        .options(selectinload(RegistryProviderVersion.platforms))
    )
    prov_version = result.scalars().first()
    if prov_version is not None:
        return prov_version

    prov_version = RegistryProviderVersion(
        provider_id=provider_id,
        version=version,
        protocols=["5.0"],
    )
    db.add(prov_version)
    await db.flush()
    return prov_version


async def upsert_provider_platform(
    db: AsyncSession,
    version_id: uuid.UUID,
    os_: str,
    arch: str,
) -> RegistryProviderPlatform:
    """Get or create a provider platform record."""
    result = await db.execute(
        select(RegistryProviderPlatform).where(
            RegistryProviderPlatform.version_id == version_id,
            RegistryProviderPlatform.os == os_,
            RegistryProviderPlatform.arch == arch,
        )
    )
    platform = result.scalars().first()
    if platform is not None:
        return platform

    platform = RegistryProviderPlatform(
        version_id=version_id,
        os=os_,
        arch=arch,
    )
    db.add(platform)
    await db.flush()
    return platform


async def regenerate_shasums(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version_id: uuid.UUID,
    version_str: str,
) -> None:
    """Regenerate SHA256SUMS from all uploaded platforms, sign, and link GPG key."""
    from terrapod.services.gpg_key_service import get_or_create_signing_key, sign_data

    result = await db.execute(
        select(RegistryProviderPlatform)
        .where(
            RegistryProviderPlatform.version_id == version_id,
            RegistryProviderPlatform.upload_status == "uploaded",
        )
        .order_by(RegistryProviderPlatform.os, RegistryProviderPlatform.arch)
    )
    platforms = list(result.scalars().all())

    lines = []
    for p in platforms:
        if p.shasum and p.filename:
            lines.append(f"{p.shasum}  {p.filename}")

    content = "\n".join(lines) + "\n" if lines else ""
    content_bytes = content.encode()
    shasums_k = provider_shasums_key(namespace, name, version_str)
    await storage.put(shasums_k, content_bytes, "text/plain")

    # Look up the version record
    ver_result = await db.execute(
        select(RegistryProviderVersion).where(RegistryProviderVersion.id == version_id)
    )
    prov_version = ver_result.scalars().first()
    if prov_version is None:
        return

    prov_version.shasums_uploaded = True

    # Sign SHA256SUMS and store the detached signature
    if content_bytes:
        try:
            gpg_key, private_armor = await get_or_create_signing_key(db)
            sig_bytes = await sign_data(private_armor, content_bytes)
            sig_k = provider_shasums_sig_key(namespace, name, version_str)
            await storage.put(sig_k, sig_bytes, "application/pgp-signature")
            prov_version.shasums_sig_uploaded = True
            prov_version.gpg_key_id = gpg_key.id
            logger.info(
                "SHA256SUMS signed",
                provider=f"{namespace}/{name}",
                version=version_str,
                gpg_key_id=gpg_key.key_id,
            )
        except Exception:
            logger.warning(
                "Failed to sign SHA256SUMS, provider will work without signatures",
                provider=f"{namespace}/{name}",
                version=version_str,
                exc_info=True,
            )

    await db.flush()


async def upload_provider_binary(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version_str: str,
    os_: str,
    arch: str,
    data: bytes,
) -> RegistryProviderPlatform:
    """Upload a provider binary directly. Upserts version + platform, stores binary."""
    provider = await get_provider(db, namespace, name)
    if provider is None:
        raise ValueError(f"Provider {namespace}/{name} not found")

    prov_version = await upsert_provider_version(db, provider.id, version_str)
    platform = await upsert_provider_platform(db, prov_version.id, os_, arch)

    # Compute SHA-256
    sha256 = hashlib.sha256(data).hexdigest()

    # Store binary
    filename = f"terraform-provider-{name}_{version_str}_{os_}_{arch}.zip"
    key = provider_binary_key(namespace, name, version_str, os_, arch)
    await storage.put(key, data, "application/zip")

    # Update platform record
    platform.shasum = sha256
    platform.filename = filename
    platform.upload_status = "uploaded"
    await db.flush()

    # Regenerate SHA256SUMS for this version
    await regenerate_shasums(db, storage, namespace, name, prov_version.id, version_str)

    return platform


# --- Internal helpers ---


async def _delete_version_storage(
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: RegistryProviderVersion,
) -> None:
    """Delete all storage objects for a provider version."""
    # Delete shasums files
    shasums_k = provider_shasums_key(namespace, name, version.version)
    sig_k = provider_shasums_sig_key(namespace, name, version.version)
    await storage.delete(shasums_k)
    await storage.delete(sig_k)

    # Delete platform binaries
    for platform in version.platforms:
        key = provider_binary_key(namespace, name, version.version, platform.os, platform.arch)
        await storage.delete(key)
