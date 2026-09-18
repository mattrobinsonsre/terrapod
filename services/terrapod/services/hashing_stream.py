"""Async iterator wrapper that computes SHA-256 and SHA-512 on the fly.

Wraps an httpx streaming response (or any async byte iterator) to hash
content incrementally as chunks pass through. Peak memory: one chunk.
"""

import hashlib
from collections.abc import AsyncIterator


class HashingStream:
    """Wraps an httpx streaming response to compute its digests on the fly.

    Both algorithms, because publishers disagree about which they publish and
    the artifact passes through once: HashiCorp, OpenTofu, Node, Go and Pulumi
    all state a SHA-256, and .NET states only a SHA-512 (#1566). Hashing the same
    bytes twice costs nothing next to the network, and the alternative -- a
    second pass, or a second download -- costs a great deal.
    """

    def __init__(self, response: object, chunk_size: int = 256 * 1024) -> None:
        self._response = response
        self._chunk_size = chunk_size
        self._hasher = hashlib.sha256()
        self._hasher512 = hashlib.sha512()
        self._size = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_bytes(self._chunk_size):  # type: ignore[union-attr]
            self._hasher.update(chunk)
            self._hasher512.update(chunk)
            self._size += len(chunk)
            yield chunk

    @property
    def sha256_hex(self) -> str:
        """Return the hex digest of all data streamed so far."""
        return self._hasher.hexdigest()

    @property
    def sha512_hex(self) -> str:
        """The SHA-512 hex digest of all data streamed so far."""
        return self._hasher512.hexdigest()

    @property
    def size(self) -> int:
        """Return the total number of bytes streamed so far."""
        return self._size
