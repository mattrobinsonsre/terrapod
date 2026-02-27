"""
AWS S3 storage backend for Terrapod.

Uses aioboto3 for async I/O. Presigned URLs use SigV4 local signature
generation (no API call). Auth relies on the SDK credential chain
(IRSA in K8s, env vars or profile locally).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aioboto3

from terrapod.logging_config import get_logger
from terrapod.storage.protocol import (
    ObjectMeta,
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectStorePermissionError,
    PresignedURL,
)

logger = get_logger(__name__)


class S3Store:
    """Object store backed by AWS S3."""

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        prefix: str = "",
        endpoint_url: str = "",
        presigned_url_expiry_seconds: int = 3600,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._prefix = prefix.strip("/")
        self._endpoint_url = endpoint_url or None
        self._default_expiry = presigned_url_expiry_seconds

        if self._default_expiry > 3600:
            logger.warning(
                "Presigned URL expiry exceeds 1 hour — may fail with IRSA credentials",
                expiry_seconds=self._default_expiry,
            )

        self._session = aioboto3.Session()
        self._client: Any = None

    def _full_key(self, key: str) -> str:
        """Prepend the configured prefix to a key."""
        if self._prefix:
            return f"{self._prefix}/{key}"
        return key

    def _strip_prefix(self, full_key: str) -> str:
        """Remove the configured prefix from a full key."""
        if self._prefix and full_key.startswith(self._prefix + "/"):
            return full_key[len(self._prefix) + 1 :]
        return full_key

    async def _get_client(self) -> Any:
        if self._client is None:
            self._client = await self._session.client(
                "s3",
                region_name=self._region,
                endpoint_url=self._endpoint_url,
            ).__aenter__()
            logger.info(
                "S3 client initialized",
                bucket=self._bucket,
                region=self._region,
            )
        return self._client

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        client = await self._get_client()
        full_key = self._full_key(key)

        put_kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": full_key,
            "Body": data,
            "ContentType": content_type,
        }
        if metadata:
            put_kwargs["Metadata"] = metadata

        try:
            response = await client.put_object(**put_kwargs)
        except client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("AccessDenied", "403"):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

        etag = response.get("ETag", "").strip('"')

        return ObjectMeta(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            etag=etag,
            last_modified=datetime.now(UTC),
            metadata=metadata or {},
        )

    async def get(self, key: str) -> bytes:
        client = await self._get_client()
        full_key = self._full_key(key)

        try:
            response = await client.get_object(Bucket=self._bucket, Key=full_key)
            return await response["Body"].read()
        except client.exceptions.NoSuchKey as e:
            raise ObjectNotFoundError(key) from e
        except client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchKey":
                raise ObjectNotFoundError(key) from e
            if error_code in ("AccessDenied", "403"):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

    async def delete(self, key: str) -> None:
        client = await self._get_client()
        full_key = self._full_key(key)

        try:
            await client.delete_object(Bucket=self._bucket, Key=full_key)
        except client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("AccessDenied", "403"):
                raise ObjectStorePermissionError(str(e)) from e
            raise ObjectStoreError(str(e)) from e

    async def exists(self, key: str) -> bool:
        client = await self._get_client()
        full_key = self._full_key(key)

        try:
            await client.head_object(Bucket=self._bucket, Key=full_key)
            return True
        except client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            raise ObjectStoreError(str(e)) from e

    async def head(self, key: str) -> ObjectMeta:
        client = await self._get_client()
        full_key = self._full_key(key)

        try:
            response = await client.head_object(Bucket=self._bucket, Key=full_key)
        except client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise ObjectNotFoundError(key) from e
            raise ObjectStoreError(str(e)) from e

        return ObjectMeta(
            key=key,
            size_bytes=response.get("ContentLength", 0),
            content_type=response.get("ContentType", "application/octet-stream"),
            etag=response.get("ETag", "").strip('"'),
            last_modified=response.get("LastModified", datetime.now(UTC)),
            metadata=response.get("Metadata", {}),
        )

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        client = await self._get_client()
        full_prefix = self._full_key(prefix)
        results: list[ObjectMeta] = []

        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                key = self._strip_prefix(obj["Key"])
                results.append(
                    ObjectMeta(
                        key=key,
                        size_bytes=obj.get("Size", 0),
                        content_type="application/octet-stream",
                        etag=obj.get("ETag", "").strip('"'),
                        last_modified=obj.get("LastModified", datetime.now(UTC)),
                    )
                )

        return results

    async def presigned_get_url(
        self,
        key: str,
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        client = await self._get_client()
        full_key = self._full_key(key)
        expiry = expiry_seconds or self._default_expiry

        url = await client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": full_key},
            ExpiresIn=expiry,
        )

        expires_at = datetime.fromtimestamp(datetime.now(UTC).timestamp() + expiry, tz=UTC)

        return PresignedURL(url=url, expires_at=expires_at)

    async def presigned_put_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        client = await self._get_client()
        full_key = self._full_key(key)
        expiry = expiry_seconds or self._default_expiry

        url = await client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self._bucket,
                "Key": full_key,
                "ContentType": content_type,
            },
            ExpiresIn=expiry,
        )

        expires_at = datetime.fromtimestamp(datetime.now(UTC).timestamp() + expiry, tz=UTC)

        return PresignedURL(
            url=url,
            expires_at=expires_at,
            headers={"Content-Type": content_type},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
            logger.info("S3 client closed")
