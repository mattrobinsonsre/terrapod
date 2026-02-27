"""
Object storage abstraction layer for Terrapod.

Provides init_storage() / close_storage() for app lifespan and
get_storage() as a FastAPI dependency.
"""

from __future__ import annotations

from terrapod.config import StorageBackend, settings
from terrapod.logging_config import get_logger
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

# Module-level storage instance
_store: ObjectStore | None = None


async def init_storage() -> None:
    """Initialize the storage backend based on configuration.

    Called during app startup (lifespan).
    """
    global _store  # noqa: PLW0603
    cfg = settings.storage

    match cfg.backend:
        case StorageBackend.FILESYSTEM:
            from terrapod.storage.filesystem import FilesystemStore
            from terrapod.storage.filesystem_routes import set_filesystem_store

            store = FilesystemStore(
                root_dir=cfg.filesystem.root_dir,
                hmac_secret=cfg.filesystem.hmac_secret,
                base_url=cfg.filesystem.base_url,
                presigned_url_expiry_seconds=cfg.filesystem.presigned_url_expiry_seconds,
            )
            set_filesystem_store(store)
            _store = store
            logger.info(
                "Storage initialized", backend="filesystem", root_dir=cfg.filesystem.root_dir
            )

        case StorageBackend.S3:
            from terrapod.storage.s3 import S3Store

            _store = S3Store(
                bucket=cfg.s3.bucket,
                region=cfg.s3.region,
                prefix=cfg.s3.prefix,
                endpoint_url=cfg.s3.endpoint_url,
                presigned_url_expiry_seconds=cfg.s3.presigned_url_expiry_seconds,
            )
            logger.info("Storage initialized", backend="s3", bucket=cfg.s3.bucket)

        case StorageBackend.AZURE:
            from terrapod.storage.azure import AzureStore

            _store = AzureStore(
                account_name=cfg.azure.account_name,
                container_name=cfg.azure.container_name,
                prefix=cfg.azure.prefix,
                presigned_url_expiry_seconds=cfg.azure.presigned_url_expiry_seconds,
            )
            logger.info("Storage initialized", backend="azure", account=cfg.azure.account_name)

        case StorageBackend.GCS:
            from terrapod.storage.gcs import GCSStore

            _store = GCSStore(
                bucket=cfg.gcs.bucket,
                prefix=cfg.gcs.prefix,
                project_id=cfg.gcs.project_id,
                service_account_email=cfg.gcs.service_account_email,
                presigned_url_expiry_seconds=cfg.gcs.presigned_url_expiry_seconds,
            )
            logger.info("Storage initialized", backend="gcs", bucket=cfg.gcs.bucket)


async def close_storage() -> None:
    """Close the storage backend and release resources.

    Called during app shutdown (lifespan).
    """
    global _store  # noqa: PLW0603
    if _store is not None:
        await _store.close()
        _store = None
        logger.info("Storage closed")


def get_storage() -> ObjectStore:
    """FastAPI dependency that returns the storage backend.

    Raises RuntimeError if storage has not been initialized.
    """
    if _store is None:
        raise RuntimeError("Storage not initialized — call init_storage() first")
    return _store


def get_storage_or_none() -> ObjectStore | None:
    """Return the storage backend if initialized, otherwise None."""
    return _store
