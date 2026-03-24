"""
Object storage protocol and types for Terrapod.

Defines the ObjectStore Protocol that all storage backends must satisfy,
along with shared data types, exceptions, and an instrumented wrapper
that emits Prometheus metrics for all operations.
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

# --- Data Types ---


@dataclass(frozen=True)
class ObjectMeta:
    """Metadata about a stored object."""

    key: str
    size_bytes: int
    content_type: str
    etag: str
    last_modified: datetime
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PresignedURL:
    """A presigned URL for direct client upload/download.

    The `headers` dict tells the client what headers to send with the request.
    For example, Azure PUT requires `{"x-ms-blob-type": "BlockBlob"}`.
    """

    url: str
    expires_at: datetime
    headers: dict[str, str] = field(default_factory=dict)


# --- Exceptions ---


class ObjectStoreError(Exception):
    """Base exception for object store operations."""


class ObjectNotFoundError(ObjectStoreError):
    """Raised when a requested object does not exist."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Object not found: {key}")


class ObjectStorePermissionError(ObjectStoreError):
    """Raised when the caller lacks permission for the operation."""


# --- Protocol ---


@runtime_checkable
class ObjectStore(Protocol):
    """Protocol defining the object storage interface.

    All methods are async. Implementations must satisfy this interface
    structurally (duck typing) — no inheritance required.
    """

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        """Store an object.

        Args:
            key: Object key (path).
            data: Object content.
            content_type: MIME type.
            metadata: Optional user-defined metadata.

        Returns:
            Metadata of the stored object.
        """
        ...

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        """Store an object by streaming chunks directly to the backend.

        Avoids loading the full payload into memory.

        Args:
            key: Object key (path).
            chunks: Async iterator of byte chunks.
            content_type: MIME type.
            metadata: Optional user-defined metadata.

        Returns:
            Metadata of the stored object.
        """
        ...

    async def get(self, key: str) -> bytes:
        """Retrieve an object's content.

        Args:
            key: Object key.

        Returns:
            Object content as bytes.

        Raises:
            ObjectNotFoundError: If the object does not exist.
        """
        ...

    async def get_stream(
        self,
        key: str,
        chunk_size: int = 256 * 1024,
    ) -> AsyncIterator[bytes]:
        """Stream an object's content in chunks.

        Avoids loading the full object into memory.

        Args:
            key: Object key.
            chunk_size: Size of each chunk in bytes.

        Returns:
            Async iterator of byte chunks.

        Raises:
            ObjectNotFoundError: If the object does not exist.
        """
        ...
        # This yield is needed to make the type checker recognize this as an
        # async generator in the Protocol definition.
        yield b""  # pragma: no cover

    async def delete(self, key: str) -> None:
        """Delete an object.

        Idempotent — does not raise if the object does not exist.

        Args:
            key: Object key.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Check if an object exists.

        Args:
            key: Object key.

        Returns:
            True if the object exists.
        """
        ...

    async def head(self, key: str) -> ObjectMeta:
        """Get object metadata without downloading the content.

        Args:
            key: Object key.

        Returns:
            Object metadata.

        Raises:
            ObjectNotFoundError: If the object does not exist.
        """
        ...

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        """List objects matching a key prefix.

        Args:
            prefix: Key prefix to filter by.

        Returns:
            List of object metadata entries matching the prefix.
        """
        ...

    async def presigned_get_url(
        self,
        key: str,
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        """Generate a presigned URL for downloading an object.

        Args:
            key: Object key.
            expiry_seconds: URL validity in seconds. Uses backend default if None.

        Returns:
            Presigned URL with expiry and any required headers.
        """
        ...

    async def presigned_put_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        """Generate a presigned URL for uploading an object.

        Args:
            key: Object key.
            content_type: Expected content type of the upload.
            expiry_seconds: URL validity in seconds. Uses backend default if None.

        Returns:
            Presigned URL with expiry and any required headers.
        """
        ...

    async def close(self) -> None:
        """Release any resources held by the backend."""
        ...


# --- Instrumented Wrapper ---


class InstrumentedStore:
    """Wrapper that emits Prometheus metrics for all storage operations.

    Delegates to a concrete ObjectStore implementation and records
    operation count, duration, and errors.
    """

    def __init__(self, inner: ObjectStore) -> None:
        self._inner = inner

    def _record(self, operation: str, start: float, error: bool = False) -> None:
        from terrapod.api.metrics import (
            STORAGE_ERRORS,
            STORAGE_OPERATION_DURATION,
            STORAGE_OPERATIONS,
        )

        duration = time.monotonic() - start
        status = "error" if error else "ok"
        STORAGE_OPERATIONS.labels(operation=operation, status=status).inc()
        STORAGE_OPERATION_DURATION.labels(operation=operation).observe(duration)
        if error:
            STORAGE_ERRORS.labels(operation=operation).inc()

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        start = time.monotonic()
        try:
            result = await self._inner.put(key, data, content_type, metadata)
            self._record("put", start)
            return result
        except Exception:
            self._record("put", start, error=True)
            raise

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        start = time.monotonic()
        try:
            result = await self._inner.put_stream(key, chunks, content_type, metadata)
            self._record("put_stream", start)
            return result
        except Exception:
            self._record("put_stream", start, error=True)
            raise

    async def get(self, key: str) -> bytes:
        start = time.monotonic()
        try:
            result = await self._inner.get(key)
            self._record("get", start)
            return result
        except ObjectNotFoundError:
            self._record("get", start)  # not an error, just not found
            raise
        except Exception:
            self._record("get", start, error=True)
            raise

    async def get_stream(
        self,
        key: str,
        chunk_size: int = 256 * 1024,
    ) -> AsyncIterator[bytes]:
        start = time.monotonic()
        try:
            stream = self._inner.get_stream(key, chunk_size)
            self._record("get_stream", start)
            async for chunk in stream:
                yield chunk
        except ObjectNotFoundError:
            self._record("get_stream", start)
            raise
        except Exception:
            self._record("get_stream", start, error=True)
            raise

    async def delete(self, key: str) -> None:
        start = time.monotonic()
        try:
            await self._inner.delete(key)
            self._record("delete", start)
        except Exception:
            self._record("delete", start, error=True)
            raise

    async def exists(self, key: str) -> bool:
        start = time.monotonic()
        try:
            result = await self._inner.exists(key)
            self._record("exists", start)
            return result
        except Exception:
            self._record("exists", start, error=True)
            raise

    async def head(self, key: str) -> ObjectMeta:
        start = time.monotonic()
        try:
            result = await self._inner.head(key)
            self._record("head", start)
            return result
        except ObjectNotFoundError:
            self._record("head", start)
            raise
        except Exception:
            self._record("head", start, error=True)
            raise

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        start = time.monotonic()
        try:
            result = await self._inner.list_prefix(prefix)
            self._record("list_prefix", start)
            return result
        except Exception:
            self._record("list_prefix", start, error=True)
            raise

    async def presigned_get_url(
        self,
        key: str,
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        start = time.monotonic()
        try:
            result = await self._inner.presigned_get_url(key, expiry_seconds)
            self._record("presigned_get_url", start)
            return result
        except Exception:
            self._record("presigned_get_url", start, error=True)
            raise

    async def presigned_put_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        start = time.monotonic()
        try:
            result = await self._inner.presigned_put_url(key, content_type, expiry_seconds)
            self._record("presigned_put_url", start)
            return result
        except Exception:
            self._record("presigned_put_url", start, error=True)
            raise

    async def close(self) -> None:
        await self._inner.close()
