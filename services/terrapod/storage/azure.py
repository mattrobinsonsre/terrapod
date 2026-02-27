"""
Azure Blob Storage backend for Terrapod.

Uses azure.storage.blob.aio for async I/O. Auth via DefaultAzureCredential
(picks up Workload Identity automatically on AKS). Presigned URLs use
User Delegation SAS — requires a cached delegation key to avoid per-request
API calls.
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


class AzureStore:
    """Object store backed by Azure Blob Storage."""

    def __init__(
        self,
        account_name: str,
        container_name: str,
        prefix: str = "",
        presigned_url_expiry_seconds: int = 3600,
    ) -> None:
        self._account_name = account_name
        self._container_name = container_name
        self._prefix = prefix.strip("/")
        self._default_expiry = presigned_url_expiry_seconds

        self._container_client: Any = None
        self._credential: Any = None
        self._delegation_key: Any = None
        self._delegation_key_expiry: datetime | None = None
        self._delegation_key_lock = asyncio.Lock()

    def _full_key(self, key: str) -> str:
        if self._prefix:
            return f"{self._prefix}/{key}"
        return key

    def _strip_prefix(self, full_key: str) -> str:
        if self._prefix and full_key.startswith(self._prefix + "/"):
            return full_key[len(self._prefix) + 1 :]
        return full_key

    async def _get_container_client(self) -> Any:
        if self._container_client is None:
            from azure.identity.aio import DefaultAzureCredential
            from azure.storage.blob.aio import ContainerClient

            self._credential = DefaultAzureCredential()
            account_url = f"https://{self._account_name}.blob.core.windows.net"
            self._container_client = ContainerClient(
                account_url=account_url,
                container_name=self._container_name,
                credential=self._credential,
            )
            logger.info(
                "Azure Blob container client initialized",
                account=self._account_name,
                container=self._container_name,
            )
        return self._container_client

    async def _get_delegation_key(self) -> Any:
        """Get a cached user delegation key for SAS generation.

        Delegation keys are cached for ~23 hours with an asyncio.Lock
        to prevent thundering herd on concurrent requests.
        """
        now = datetime.now(UTC)
        if (
            self._delegation_key
            and self._delegation_key_expiry
            and now < self._delegation_key_expiry
        ):
            return self._delegation_key

        async with self._delegation_key_lock:
            # Double-check after acquiring lock
            now = datetime.now(UTC)
            if (
                self._delegation_key
                and self._delegation_key_expiry
                and now < self._delegation_key_expiry
            ):
                return self._delegation_key

            from azure.storage.blob.aio import BlobServiceClient

            account_url = f"https://{self._account_name}.blob.core.windows.net"
            async with BlobServiceClient(
                account_url=account_url, credential=self._credential
            ) as service_client:
                key_start = now
                key_expiry = now + timedelta(hours=23)
                self._delegation_key = await service_client.get_user_delegation_key(
                    key_start_time=key_start,
                    key_expiry_time=key_expiry,
                )
                self._delegation_key_expiry = key_expiry - timedelta(minutes=5)
                logger.info("User delegation key refreshed", expires=key_expiry.isoformat())

        return self._delegation_key

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        container = await self._get_container_client()
        blob_name = self._full_key(key)
        blob_client = container.get_blob_client(blob_name)

        try:
            await blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=_content_settings(content_type),
                metadata=metadata,
            )
        except Exception as e:
            if _is_permission_error(e):
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
        container = await self._get_container_client()
        blob_name = self._full_key(key)
        blob_client = container.get_blob_client(blob_name)

        try:
            stream = await blob_client.download_blob()
            return await stream.readall()
        except Exception as e:
            if _is_not_found(e):
                raise ObjectNotFoundError(key) from e
            if _is_permission_error(e):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

    async def delete(self, key: str) -> None:
        container = await self._get_container_client()
        blob_name = self._full_key(key)
        blob_client = container.get_blob_client(blob_name)

        try:
            await blob_client.delete_blob()
        except Exception as e:
            if _is_not_found(e):
                return  # Idempotent delete
            if _is_permission_error(e):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

    async def exists(self, key: str) -> bool:
        container = await self._get_container_client()
        blob_name = self._full_key(key)
        blob_client = container.get_blob_client(blob_name)

        try:
            await blob_client.get_blob_properties()
            return True
        except Exception as e:
            if _is_not_found(e):
                return False
            raise ObjectStoreError(str(e)) from e

    async def head(self, key: str) -> ObjectMeta:
        container = await self._get_container_client()
        blob_name = self._full_key(key)
        blob_client = container.get_blob_client(blob_name)

        try:
            props = await blob_client.get_blob_properties()
        except Exception as e:
            if _is_not_found(e):
                raise ObjectNotFoundError(key) from e
            raise ObjectStoreError(str(e)) from e

        return ObjectMeta(
            key=key,
            size_bytes=props.size or 0,
            content_type=props.content_settings.content_type or "application/octet-stream",
            etag=(props.etag or "").strip('"'),
            last_modified=props.last_modified or datetime.now(UTC),
            metadata=dict(props.metadata) if props.metadata else {},
        )

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        container = await self._get_container_client()
        full_prefix = self._full_key(prefix)
        results: list[ObjectMeta] = []

        async for blob in container.list_blobs(name_starts_with=full_prefix):
            key = self._strip_prefix(blob.name)
            results.append(
                ObjectMeta(
                    key=key,
                    size_bytes=blob.size or 0,
                    content_type=(
                        blob.content_settings.content_type
                        if blob.content_settings
                        else "application/octet-stream"
                    ),
                    etag=(blob.etag or "").strip('"'),
                    last_modified=blob.last_modified or datetime.now(UTC),
                    metadata=dict(blob.metadata) if blob.metadata else {},
                )
            )

        return results

    async def presigned_get_url(
        self,
        key: str,
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        delegation_key = await self._get_delegation_key()
        blob_name = self._full_key(key)
        expiry = expiry_seconds or self._default_expiry
        expires_at = datetime.now(UTC) + timedelta(seconds=expiry)

        sas_token = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container_name,
            blob_name=blob_name,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            expiry=expires_at,
        )

        url = (
            f"https://{self._account_name}.blob.core.windows.net/"
            f"{self._container_name}/{blob_name}?{sas_token}"
        )

        return PresignedURL(url=url, expires_at=expires_at)

    async def presigned_put_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        delegation_key = await self._get_delegation_key()
        blob_name = self._full_key(key)
        expiry = expiry_seconds or self._default_expiry
        expires_at = datetime.now(UTC) + timedelta(seconds=expiry)

        sas_token = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container_name,
            blob_name=blob_name,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(write=True, create=True),
            expiry=expires_at,
            content_type=content_type,
        )

        url = (
            f"https://{self._account_name}.blob.core.windows.net/"
            f"{self._container_name}/{blob_name}?{sas_token}"
        )

        return PresignedURL(
            url=url,
            expires_at=expires_at,
            headers={
                "x-ms-blob-type": "BlockBlob",
                "Content-Type": content_type,
            },
        )

    async def close(self) -> None:
        if self._container_client is not None:
            await self._container_client.close()
            self._container_client = None
        if self._credential is not None:
            await self._credential.close()
            self._credential = None
        logger.info("Azure Blob client closed")


def _content_settings(content_type: str) -> Any:
    """Create ContentSettings for Azure Blob upload."""
    from azure.storage.blob import ContentSettings

    return ContentSettings(content_type=content_type)


def _is_not_found(exc: Exception) -> bool:
    """Check if an Azure exception indicates a 404."""
    from azure.core.exceptions import ResourceNotFoundError

    return isinstance(exc, ResourceNotFoundError)


def _is_permission_error(exc: Exception) -> bool:
    """Check if an Azure exception indicates a permission error."""
    from azure.core.exceptions import ClientAuthenticationError, HttpResponseError

    if isinstance(exc, ClientAuthenticationError):
        return True
    if isinstance(exc, HttpResponseError) and exc.status_code == 403:
        return True
    return False
