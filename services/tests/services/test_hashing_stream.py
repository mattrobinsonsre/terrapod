"""Tests for the HashingStream wrapper."""

import hashlib

from terrapod.services.hashing_stream import HashingStream


class _MockResponse:
    """Fake httpx streaming response for testing."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size: int = 256 * 1024):
        for chunk in self._chunks:
            yield chunk


class TestHashingStream:
    async def test_sha256_and_size(self) -> None:
        data_chunks = [b"hello ", b"world", b"!"]
        resp = _MockResponse(data_chunks)
        stream = HashingStream(resp)

        collected = b""
        async for chunk in stream:
            collected += chunk

        assert collected == b"hello world!"
        assert stream.size == 12
        expected_sha = hashlib.sha256(b"hello world!").hexdigest()
        assert stream.sha256_hex == expected_sha

    async def test_empty_stream(self) -> None:
        resp = _MockResponse([])
        stream = HashingStream(resp)

        collected = b""
        async for chunk in stream:
            collected += chunk

        assert collected == b""
        assert stream.size == 0
        expected_sha = hashlib.sha256(b"").hexdigest()
        assert stream.sha256_hex == expected_sha

    async def test_single_chunk(self) -> None:
        data = b"all in one chunk"
        resp = _MockResponse([data])
        stream = HashingStream(resp)

        chunks = []
        async for chunk in stream:
            chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0] == data
        assert stream.size == len(data)
        assert stream.sha256_hex == hashlib.sha256(data).hexdigest()
