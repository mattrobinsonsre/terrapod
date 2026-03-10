"""
Object storage protocol and types for Terrapod.

Defines the ObjectStore Protocol that all storage backends must satisfy,
along with shared data types and exceptions.
"""

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
