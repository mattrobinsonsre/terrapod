"""
Object storage abstraction layer for Terrapod.

Provides init_storage() / close_storage() for app lifespan and
get_storage() as a FastAPI dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from terrapod.config import StorageBackend, StorageConfig, StorageLocationConfig, settings
from terrapod.logging_config import get_logger
from terrapod.storage.protocol import InstrumentedStore, ObjectStore

if TYPE_CHECKING:
    from terrapod.storage.routing import LocationResolver

logger = get_logger(__name__)

# Module-level storage instance
_store: ObjectStore | None = None


def _instrumented(store: ObjectStore) -> ObjectStore:
    """Wrap with metrics instrumentation when enabled."""
    return InstrumentedStore(store) if settings.metrics.enabled else store  # type: ignore[return-value]


def _federated(cfg: StorageConfig | StorageLocationConfig, name: str | None) -> object | None:
    """This location's federated identity, or None.

    `None` for the deployment-wide backend whatever it is configured as: a
    `StorageConfig` has no `credentials` block at all, because the pod's own
    identity is the only thing it could federate AS. Federation is a per-location
    property by design -- the whole point is reaching an account the pod's identity
    cannot (#2590).
    """
    if name is None or not isinstance(cfg, StorageLocationConfig):
        return None
    from terrapod.storage.credentials import federated_location

    return federated_location(name, cfg)


def _build_cloud_store(
    cfg: StorageConfig | StorageLocationConfig, name: str | None = None
) -> ObjectStore:
    """Build an s3, azure or gcs store from a backend definition.

    `name` is the storage location's name, or None for the deployment-wide backend.
    It was a `**log_fields` bag until federation needed it as a value: a token is
    minted FOR a location, so the name is part of the credential rather than
    decoration on a log line.
    """
    log_fields = {} if name is None else {"location": name}
    federated = _federated(cfg, name)
    match cfg.backend:
        case StorageBackend.S3:
            from terrapod.storage.s3 import S3Store

            store = S3Store(
                bucket=cfg.s3.bucket,
                region=cfg.s3.region,
                prefix=cfg.s3.prefix,
                endpoint_url=cfg.s3.endpoint_url,
                presigned_url_expiry_seconds=cfg.s3.presigned_url_expiry_seconds,
                federated=federated,
            )
            logger.info("Storage initialized", backend="s3", bucket=cfg.s3.bucket, **log_fields)
            return store

        case StorageBackend.AZURE:
            from terrapod.storage.azure import AzureStore

            store = AzureStore(
                account_name=cfg.azure.account_name,
                container_name=cfg.azure.container_name,
                prefix=cfg.azure.prefix,
                presigned_url_expiry_seconds=cfg.azure.presigned_url_expiry_seconds,
                federated=federated,
            )
            logger.info(
                "Storage initialized", backend="azure", account=cfg.azure.account_name, **log_fields
            )
            return store

        case StorageBackend.GCS:
            from terrapod.storage.gcs import GCSStore

            store = GCSStore(
                bucket=cfg.gcs.bucket,
                prefix=cfg.gcs.prefix,
                project_id=cfg.gcs.project_id,
                service_account_email=cfg.gcs.service_account_email,
                presigned_url_expiry_seconds=cfg.gcs.presigned_url_expiry_seconds,
                federated=federated,
            )
            logger.info("Storage initialized", backend="gcs", bucket=cfg.gcs.bucket, **log_fields)
            return store

        case _:
            raise ValueError(f"unsupported storage backend: {cfg.backend}")


async def init_storage(resolver: LocationResolver | None = None) -> None:
    """Initialize the storage backend based on configuration.

    Called during app startup (lifespan). `resolver` overrides the database-backed one.
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

    if resolver is None:
        from terrapod.storage.location_resolver import DbLocationResolver

        resolver = DbLocationResolver()
    locations = {
        name: _instrumented(_build_cloud_store(location, name=name))
        for name, location in cfg.locations.items()
    }
    _store = RoutingStore(default, locations, resolver)
    if isinstance(resolver, StaticDefaultResolver):
        logger.warning(
            "Storage locations are configured but no workspace is assigned to one yet; "
            "a listing whose prefix can hold workspace-scoped keys still reads every location",
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
