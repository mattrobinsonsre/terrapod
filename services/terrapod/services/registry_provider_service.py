"""Service layer for private provider registry operations.

Handles CRUD for registry providers plus the client-signed publish path:
store the SHA256SUMS manifest, verify its detached signature against a
registered GPG key (the trust gate), then accept per-platform binaries that
match the signed manifest. The server never re-signs; downloads are served
via presigned GET URLs.
"""

import asyncio
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
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)


class PublishValidationError(Exception):
    """A client-signed publish step failed validation.

    The router maps this to HTTP 422 so a bad / untrusted / mismatched
    upload is rejected loudly at publish time rather than silently at
    `tofu init`.
    """


def _parse_shasums(data: bytes) -> dict[str, str]:
    """Parse a SHA256SUMS manifest into ``{filename: lowercase-sha}``."""
    out: dict[str, str] = {}
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[-1]] = parts[0].lower()
    return out


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


async def store_provider_shasums(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: str,
    data: bytes,
) -> RegistryProviderVersion:
    """Store a client-supplied SHA256SUMS manifest (first publish step).

    Upserts the version row and persists the manifest verbatim. The
    manifest is NOT trusted until its detached signature is uploaded and
    verified by store_and_verify_provider_sig — binary uploads are gated
    on that.
    """
    provider = await get_provider(db, namespace, name)
    if provider is None:
        raise ValueError(f"Provider {namespace}/{name} not found")

    prov_version = await upsert_provider_version(db, provider.id, version)
    key = provider_shasums_key(namespace, name, version)
    await storage.put(key, data, "text/plain")
    prov_version.shasums_uploaded = True
    await db.flush()
    return prov_version


async def store_and_verify_provider_sig(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: str,
    sig_bytes: bytes,
) -> RegistryProviderVersion:
    """Verify + store the detached SHA256SUMS signature — the trust gate.

    Reads back the previously-uploaded SHA256SUMS, confirms the detached
    signature was produced by a *registered* GPG key, and verifies it
    cryptographically over the manifest bytes. On success the signing key
    is linked to the version so the CLI download response advertises it as
    a signing key. Raises PublishValidationError (-> HTTP 422) on any
    failure, so binaries are never accepted under an untrusted manifest.
    The server never re-signs — the client owns the signature.
    """
    from terrapod.services.gpg_key_service import (
        extract_signature_key_id,
        get_gpg_key_by_key_id,
        verify_detached_signature,
    )

    provider = await get_provider(db, namespace, name)
    if provider is None:
        raise ValueError(f"Provider {namespace}/{name} not found")
    prov_version = await get_provider_version(db, provider.id, version)
    if prov_version is None:
        raise PublishValidationError("upload SHA256SUMS before its signature")

    shasums_key = provider_shasums_key(namespace, name, version)
    try:
        shasums_bytes = await storage.get(shasums_key)
    except Exception as exc:
        raise PublishValidationError("SHA256SUMS must be uploaded before its signature") from exc

    key_id = await extract_signature_key_id(sig_bytes)
    if not key_id:
        raise PublishValidationError("could not parse a key ID from the signature")

    gpg_key = await get_gpg_key_by_key_id(db, key_id)
    if gpg_key is None:
        raise PublishValidationError(
            f"signature key {key_id} is not registered; add it via /api/terrapod/v1/gpg-keys first"
        )

    if not await verify_detached_signature(gpg_key.ascii_armor, shasums_bytes, sig_bytes):
        raise PublishValidationError(
            f"SHA256SUMS signature failed verification against registered key {key_id}"
        )

    sig_key = provider_shasums_sig_key(namespace, name, version)
    await storage.put(sig_key, sig_bytes, "application/pgp-signature")
    prov_version.gpg_key_id = gpg_key.id
    prov_version.shasums_sig_uploaded = True
    await db.flush()
    return prov_version


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


async def record_provider_binary(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: str,
    os_: str,
    arch: str,
    *,
    sha256: str,
    filename: str,
    tmp_path: str,
) -> RegistryProviderPlatform:
    """Validate a streamed provider binary against the signed manifest, then store it.

    Preconditions (PublishValidationError -> HTTP 422):
      - the version's SHA256SUMS.sig is already verified (trust established);
      - the uploaded zip's filename appears in SHA256SUMS;
      - the uploaded zip's sha matches the signed manifest entry.

    Only then is the validated tempfile streamed from the PVC into object
    storage and the platform row marked uploaded. The server never re-signs
    — the client owns the manifest and signature.
    """
    provider = await get_provider(db, namespace, name)
    if provider is None:
        raise ValueError(f"Provider {namespace}/{name} not found")
    prov_version = await get_provider_version(db, provider.id, version)
    if prov_version is None:
        raise PublishValidationError(
            "upload and verify SHA256SUMS + SHA256SUMS.sig before uploading binaries"
        )
    if not prov_version.shasums_sig_uploaded:
        raise PublishValidationError(
            "upload and verify SHA256SUMS + SHA256SUMS.sig before uploading binaries"
        )

    shasums_bytes = await storage.get(provider_shasums_key(namespace, name, version))
    sums = _parse_shasums(shasums_bytes)
    expected = sums.get(filename)
    if expected is None:
        raise PublishValidationError(f"{filename} is not listed in the signed SHA256SUMS")
    if expected != sha256.lower():
        raise PublishValidationError(
            f"{filename}: uploaded sha {sha256.lower()} does not match signed manifest {expected}"
        )

    # Stream the validated tempfile into object storage (constant memory).
    key = provider_binary_key(namespace, name, version, os_, arch)

    async def _chunks():
        with open(tmp_path, "rb") as src:  # noqa: ASYNC230 -- bounded reads off the PVC
            while True:
                buf = await asyncio.to_thread(src.read, 1024 * 1024)
                if not buf:
                    break
                yield buf

    await storage.put_stream(key, _chunks(), content_type="application/zip")

    platform = await upsert_provider_platform(db, prov_version.id, os_, arch)
    platform.shasum = sha256.lower()
    platform.filename = filename
    platform.upload_status = "uploaded"
    # The terraform h1 dirhash is computed lazily by the mirror on first
    # read (memory-safe for large archives); not eagerly computed here.
    await db.flush()
    return platform


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
    if platform is None or platform.upload_status != "uploaded":
        # Hide platforms whose binary hasn't been validated + stored yet, so
        # the CLI download path agrees with the version-list path (which also
        # filters on upload_status) — never serve a half-published platform.
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
