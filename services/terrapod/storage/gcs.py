"""
Google Cloud Storage backend for Terrapod.

Hybrid approach: gcloud-aio-storage for async data I/O, google-cloud-storage
via asyncio.to_thread for signed URL generation (requires IAM signBlob API).
Auth via Application Default Credentials (Workload Identity Federation on GKE).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from terrapod.logging_config import get_logger
from terrapod.storage.protocol import (
    ObjectMeta,
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectStorePermissionError,
    PresignedURL,
)

logger = get_logger(__name__)


class GCSStore:
    """Object store backed by Google Cloud Storage."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        project_id: str = "",
        service_account_email: str = "",
        presigned_url_expiry_seconds: int = 3600,
    ) -> None:
        self._bucket_name = bucket
        self._prefix = prefix.strip("/")
        self._project_id = project_id or None
        self._service_account_email = service_account_email or None
        self._default_expiry = presigned_url_expiry_seconds

        # Async client (gcloud-aio-storage)
        self._aio_storage: Any = None
        # Sync client (google-cloud-storage) — for signed URL generation
        self._sync_client: Any = None

    def _full_key(self, key: str) -> str:
        if self._prefix:
            return f"{self._prefix}/{key}"
        return key

    def _strip_prefix(self, full_key: str) -> str:
        if self._prefix and full_key.startswith(self._prefix + "/"):
            return full_key[len(self._prefix) + 1 :]
        return full_key

    async def _get_aio_storage(self) -> Any:
        if self._aio_storage is None:
            from gcloud.aio.storage import Storage

            self._aio_storage = Storage()
            logger.info("GCS async client initialized", bucket=self._bucket_name)
        return self._aio_storage

    def _get_sync_client(self) -> Any:
        if self._sync_client is None:
            from google.cloud import storage as gcs_storage

            self._sync_client = gcs_storage.Client(project=self._project_id)
            logger.info("GCS sync client initialized (for signing)")
        return self._sync_client

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        storage = await self._get_aio_storage()
        blob_name = self._full_key(key)

        try:
            await storage.upload(
                self._bucket_name,
                blob_name,
                data,
                headers={"Content-Type": content_type},
                metadata=metadata,
            )
        except Exception as e:
            if "403" in str(e):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

        etag = hashlib.md5(data).hexdigest()  # noqa: S324

        return ObjectMeta(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            etag=etag,
            last_modified=datetime.now(UTC),
            metadata=metadata or {},
        )

    async def get(self, key: str) -> bytes:
        storage = await self._get_aio_storage()
        blob_name = self._full_key(key)

        try:
            return await storage.download(self._bucket_name, blob_name)
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                raise ObjectNotFoundError(key) from e
            if "403" in str(e):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

    async def delete(self, key: str) -> None:
        storage = await self._get_aio_storage()
        blob_name = self._full_key(key)

        try:
            await storage.delete(self._bucket_name, blob_name)
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                return  # Idempotent delete
            if "403" in str(e):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

    async def exists(self, key: str) -> bool:
        storage = await self._get_aio_storage()
        blob_name = self._full_key(key)

        try:
            metadata = await storage.download_metadata(self._bucket_name, blob_name)
            return metadata is not None
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                return False
            raise ObjectStoreError(str(e)) from e

    async def head(self, key: str) -> ObjectMeta:
        storage = await self._get_aio_storage()
        blob_name = self._full_key(key)

        try:
            metadata = await storage.download_metadata(self._bucket_name, blob_name)
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                raise ObjectNotFoundError(key) from e
            raise ObjectStoreError(str(e)) from e

        if metadata is None:
            raise ObjectNotFoundError(key)

        size = int(metadata.get("size", 0))
        content_type = metadata.get("contentType", "application/octet-stream")
        etag = metadata.get("etag", "").strip('"')
        updated = metadata.get("updated", "")
        last_modified = datetime.now(UTC)
        if updated:
            try:
                last_modified = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                pass

        user_metadata = metadata.get("metadata", {})
        if not isinstance(user_metadata, dict):
            user_metadata = {}

        return ObjectMeta(
            key=key,
            size_bytes=size,
            content_type=content_type,
            etag=etag,
            last_modified=last_modified,
            metadata=user_metadata,
        )

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        storage = await self._get_aio_storage()
        full_prefix = self._full_key(prefix)
        results: list[ObjectMeta] = []

        try:
            blobs = await storage.list_objects(self._bucket_name, params={"prefix": full_prefix})
        except Exception as e:
            raise ObjectStoreError(str(e)) from e

        for item in blobs.get("items", []):
            key = self._strip_prefix(item.get("name", ""))
            size = int(item.get("size", 0))
            content_type = item.get("contentType", "application/octet-stream")
            etag = item.get("etag", "").strip('"')
            updated = item.get("updated", "")
            last_modified = datetime.now(UTC)
            if updated:
                try:
                    last_modified = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except ValueError:
                    pass

            results.append(
                ObjectMeta(
                    key=key,
                    size_bytes=size,
                    content_type=content_type,
                    etag=etag,
                    last_modified=last_modified,
                )
            )

        return results

    async def presigned_get_url(
        self,
        key: str,
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        expiry = expiry_seconds or self._default_expiry
        blob_name = self._full_key(key)

        def _sign() -> str:
            client = self._get_sync_client()
            bucket = client.bucket(self._bucket_name)
            blob = bucket.blob(blob_name)
            kwargs: dict[str, Any] = {
                "version": "v4",
                "expiration": timedelta(seconds=expiry),
                "method": "GET",
            }
            if self._service_account_email:
                kwargs["service_account_email"] = self._service_account_email
            return blob.generate_signed_url(**kwargs)

        url = await asyncio.to_thread(_sign)
        expires_at = datetime.now(UTC) + timedelta(seconds=expiry)

        return PresignedURL(url=url, expires_at=expires_at)

    async def presigned_put_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        expiry = expiry_seconds or self._default_expiry
        blob_name = self._full_key(key)

        def _sign() -> str:
            client = self._get_sync_client()
            bucket = client.bucket(self._bucket_name)
            blob = bucket.blob(blob_name)
            kwargs: dict[str, Any] = {
                "version": "v4",
                "expiration": timedelta(seconds=expiry),
                "method": "PUT",
                "content_type": content_type,
            }
            if self._service_account_email:
                kwargs["service_account_email"] = self._service_account_email
            return blob.generate_signed_url(**kwargs)

        url = await asyncio.to_thread(_sign)
        expires_at = datetime.now(UTC) + timedelta(seconds=expiry)

        return PresignedURL(
            url=url,
            expires_at=expires_at,
            headers={"Content-Type": content_type},
        )

    async def close(self) -> None:
        if self._aio_storage is not None:
            await self._aio_storage.close()
            self._aio_storage = None
        self._sync_client = None
        logger.info("GCS clients closed")
