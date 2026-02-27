"""
Filesystem storage backend for Terrapod.

Uses aiofiles for async I/O against a local directory. Presigned URLs are
HMAC-SHA256 signed tokens that point back at the API server's own endpoints.
This is the default backend — zero external dependencies for dev/CI.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import aiofiles
import aiofiles.os

from terrapod.logging_config import get_logger
from terrapod.storage.protocol import (
    ObjectMeta,
    ObjectNotFoundError,
    ObjectStoreError,
    PresignedURL,
)

logger = get_logger(__name__)


class FilesystemStore:
    """Object store backed by the local filesystem."""

    def __init__(
        self,
        root_dir: str,
        hmac_secret: str = "",
        base_url: str = "http://localhost:8000",
        presigned_url_expiry_seconds: int = 3600,
    ) -> None:
        self._root = Path(root_dir)
        self._hmac_secret = hmac_secret or secrets.token_hex(32)
        self._base_url = base_url.rstrip("/")
        self._default_expiry = presigned_url_expiry_seconds

        # Ensure root directory exists
        self._root.mkdir(parents=True, exist_ok=True)
        logger.info("Filesystem store initialized", root_dir=str(self._root))

    def _full_path(self, key: str) -> Path:
        """Resolve key to a full filesystem path, preventing path traversal."""
        # Normalize and reject absolute or traversal keys
        clean = Path(key)
        if clean.is_absolute() or ".." in clean.parts:
            raise ObjectStoreError(f"Invalid key: {key}")
        return self._root / clean

    def _key_from_path(self, path: Path) -> str:
        """Convert a filesystem path back to a key."""
        return str(path.relative_to(self._root))

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        path = self._full_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with aiofiles.open(path, "wb") as f:
            await f.write(data)

        # Store content type in a sidecar file
        meta_path = Path(str(path) + ".meta")
        meta_content = content_type
        if metadata:
            meta_content += "\n" + "\n".join(f"{k}={v}" for k, v in metadata.items())
        async with aiofiles.open(meta_path, "w") as f:
            await f.write(meta_content)

        stat = await aiofiles.os.stat(path)
        etag = hashlib.md5(data).hexdigest()  # noqa: S324

        return ObjectMeta(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            etag=etag,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            metadata=metadata or {},
        )

    async def get(self, key: str) -> bytes:
        path = self._full_path(key)
        if not path.exists():
            raise ObjectNotFoundError(key)

        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def delete(self, key: str) -> None:
        path = self._full_path(key)
        meta_path = Path(str(path) + ".meta")

        if path.exists():
            await aiofiles.os.remove(path)
        if meta_path.exists():
            await aiofiles.os.remove(meta_path)

    async def exists(self, key: str) -> bool:
        return self._full_path(key).exists()

    async def head(self, key: str) -> ObjectMeta:
        path = self._full_path(key)
        if not path.exists():
            raise ObjectNotFoundError(key)

        stat = await aiofiles.os.stat(path)

        # Read content type and metadata from sidecar
        content_type = "application/octet-stream"
        metadata: dict[str, str] = {}
        meta_path = Path(str(path) + ".meta")
        if meta_path.exists():
            async with aiofiles.open(meta_path) as f:
                lines = (await f.read()).strip().split("\n")
                if lines:
                    content_type = lines[0]
                for line in lines[1:]:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        metadata[k] = v

        # Compute etag from file content
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        etag = hashlib.md5(data).hexdigest()  # noqa: S324

        return ObjectMeta(
            key=key,
            size_bytes=stat.st_size,
            content_type=content_type,
            etag=etag,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            metadata=metadata,
        )

    async def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        prefix_path = self._full_path(prefix) if prefix else self._root
        results: list[ObjectMeta] = []

        # Walk the directory tree
        search_dir = prefix_path if prefix_path.is_dir() else prefix_path.parent
        if not search_dir.exists():
            return results

        prefix_str = prefix if prefix else ""
        for path in sorted(search_dir.rglob("*")):
            if path.is_file() and not path.name.endswith(".meta"):
                key = self._key_from_path(path)
                if key.startswith(prefix_str):
                    meta = await self.head(key)
                    results.append(meta)

        return results

    def _sign(self, operation: str, key: str, expires: int) -> str:
        """Create an HMAC-SHA256 signature for a presigned URL."""
        message = f"{operation}:{key}:{expires}"
        return hmac.new(
            self._hmac_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

    def verify_signature(self, operation: str, key: str, expires: str, signature: str) -> bool:
        """Verify an HMAC-SHA256 signature from a presigned URL."""
        try:
            expires_int = int(expires)
        except ValueError:
            return False

        if time.time() > expires_int:
            return False

        expected = self._sign(operation, key, expires_int)
        return hmac.compare_digest(expected, signature)

    async def presigned_get_url(
        self,
        key: str,
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        expiry = expiry_seconds or self._default_expiry
        expires = int(time.time()) + expiry
        sig = self._sign("GET", key, expires)

        encoded_key = urllib.parse.quote(key, safe="")
        url = f"{self._base_url}/api/v2/storage/get/{encoded_key}?expires={expires}&sig={sig}"

        return PresignedURL(
            url=url,
            expires_at=datetime.fromtimestamp(expires, tz=UTC),
        )

    async def presigned_put_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        expiry = expiry_seconds or self._default_expiry
        expires = int(time.time()) + expiry
        sig = self._sign("PUT", key, expires)

        encoded_key = urllib.parse.quote(key, safe="")
        url = (
            f"{self._base_url}/api/v2/storage/put/{encoded_key}"
            f"?expires={expires}&sig={sig}&content_type={urllib.parse.quote(content_type)}"
        )

        return PresignedURL(
            url=url,
            expires_at=datetime.fromtimestamp(expires, tz=UTC),
            headers={"Content-Type": content_type},
        )

    async def close(self) -> None:
        """No resources to release for filesystem backend."""

    @property
    def root_dir(self) -> Path:
        """The root directory for stored objects."""
        return self._root

    @property
    def hmac_secret(self) -> str:
        """The HMAC secret used for URL signing."""
        return self._hmac_secret

    @classmethod
    def from_env(cls, base_url: str = "http://localhost:8000") -> FilesystemStore:
        """Create a FilesystemStore from environment defaults."""
        root = os.environ.get("TERRAPOD_STORAGE__FILESYSTEM__ROOT_DIR", "/tmp/terrapod-storage")
        return cls(root_dir=root, base_url=base_url)
