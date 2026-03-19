"""Async iterator wrapper that computes SHA256 on the fly.

Wraps an httpx streaming response (or any async byte iterator) to hash
content incrementally as chunks pass through. Peak memory: one chunk.
"""

import hashlib
from collections.abc import AsyncIterator


class HashingStream:
    """Wraps an httpx streaming response to compute SHA256 on the fly."""

    def __init__(self, response: object, chunk_size: int = 256 * 1024) -> None:
        self._response = response
        self._chunk_size = chunk_size
        self._hasher = hashlib.sha256()
        self._size = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_bytes(self._chunk_size):  # type: ignore[union-attr]
            self._hasher.update(chunk)
            self._size += len(chunk)
            yield chunk

    @property
    def sha256_hex(self) -> str:
        """Return the hex digest of all data streamed so far."""
        return self._hasher.hexdigest()

    @property
    def size(self) -> int:
        """Return the total number of bytes streamed so far."""
        return self._size
