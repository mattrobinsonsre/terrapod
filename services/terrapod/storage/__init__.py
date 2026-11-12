"""
Object storage abstraction layer for Terrapod.

Provides init_storage() / close_storage() for app lifespan and
get_storage() as a FastAPI dependency.
"""

from __future__ import annotations

from terrapod.config import StorageBackend, StorageConfig, StorageLocationConfig, settings
from terrapod.logging_config import get_logger
from terrapod.storage.protocol import InstrumentedStore, ObjectStore

logger = get_logger(__name__)

# Module-level storage instance
_store: ObjectStore | None = None


def _instrumented(store: ObjectStore) -> ObjectStore:
    """Wrap with metrics instrumentation when enabled."""
    return InstrumentedStore(store) if settings.metrics.enabled else store  # type: ignore[return-value]


def _build_cloud_store(
    cfg: StorageConfig | StorageLocationConfig, **log_fields: str
) -> ObjectStore:
    """Build an s3, azure or gcs store from a backend definition."""
    store: ObjectStore
    match cfg.backend:
        case StorageBackend.S3:
            from terrapod.storage.s3 import S3Store

            store = S3Store(
                bucket=cfg.s3.bucket,
                region=cfg.s3.region,
                prefix=cfg.s3.prefix,
                endpoint_url=cfg.s3.endpoint_url,
                presigned_url_expiry_seconds=cfg.s3.presigned_url_expiry_seconds,
            )
            logger.info("Storage initialized", backend="s3", bucket=cfg.s3.bucket, **log_fields)

        case StorageBackend.AZURE:
            from terrapod.storage.azure import AzureStore

            store = AzureStore(
                account_name=cfg.azure.account_name,
                container_name=cfg.azure.container_name,
                prefix=cfg.azure.prefix,
                presigned_url_expiry_seconds=cfg.azure.presigned_url_expiry_seconds,
            )
            logger.info(
                "Storage initialized", backend="azure", account=cfg.azure.account_name, **log_fields
            )

        case StorageBackend.GCS:
            from terrapod.storage.gcs import GCSStore

            store = GCSStore(
                bucket=cfg.gcs.bucket,
                prefix=cfg.gcs.prefix,
                project_id=cfg.gcs.project_id,
                service_account_email=cfg.gcs.service_account_email,
                presigned_url_expiry_seconds=cfg.gcs.presigned_url_expiry_seconds,
            )
            logger.info("Storage initialized", backend="gcs", bucket=cfg.gcs.bucket, **log_fields)

        case _:
            raise ValueError(f"unsupported storage backend: {cfg.backend}")
    return store


async def init_storage() -> None:
    """Initialize the storage backend based on configuration.

    Called during app startup (lifespan).
    """
    global _store  # noqa: PLW0603
    cfg = settings.storage

    if cfg.backend == StorageBackend.FILESYSTEM:
        from terrapod.storage.filesystem import FilesystemStore
        from terrapod.storage.filesystem_routes import set_filesystem_store

        store = FilesystemStore(
            root_dir=cfg.filesystem.root_dir,
            hmac_secret=cfg.filesystem.hmac_secret,
            base_url=cfg.filesystem.base_url,
            presigned_url_expiry_seconds=cfg.filesystem.presigned_url_expiry_seconds,
        )
        set_filesystem_store(store)
        logger.info("Storage initialized", backend="filesystem", root_dir=cfg.filesystem.root_dir)
        default = _instrumented(store)
    else:
        default = _instrumented(_build_cloud_store(cfg))

    if not cfg.locations:
        _store = default
        return

    from terrapod.storage.routing import RoutingStore, StaticDefaultResolver

    locations = {
        name: _instrumented(_build_cloud_store(location, location=name))
        for name, location in cfg.locations.items()
    }
    _store = RoutingStore(default, locations, StaticDefaultResolver())
    logger.warning(
        "Storage locations are configured but no workspace is assigned to one yet",
        locations=sorted(locations),
    )


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
