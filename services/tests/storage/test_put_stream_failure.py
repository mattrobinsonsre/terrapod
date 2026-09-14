"""put_stream stores an object whole or not at all (#1600).

A stream that fails part-way must leave nothing at the key — otherwise a
truncated archive is later read back as if it were complete. S3 aborts its
multipart upload and Azure never commits its block list, and the filesystem
backend writes through `_write_atomically` (#1418), so those were already
all-or-nothing; these tests pin the filesystem behaviour, including that a
listing taken mid-write does not show the temporary file. The GCS backend ended
its resumable upload normally when the stream failed, which finalized whatever
had been sent.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from terrapod.storage.filesystem import FilesystemStore
from terrapod.storage.gcs import GCSStore
from terrapod.storage.protocol import ObjectNotFoundError


class _SourceFailed(Exception):
    pass


async def _fails_after(n: int):
    for _ in range(n):
        yield b"x" * 1024
    raise _SourceFailed("source stream failed")


async def _once(data: bytes):
    yield data


class TestFilesystemPutStream:
    async def test_a_failed_stream_leaves_nothing_at_the_key(self, fs_store: FilesystemStore):
        with pytest.raises(_SourceFailed):
            await fs_store.put_stream("a/b.tar.gz", _fails_after(3))

        with pytest.raises(ObjectNotFoundError):
            await fs_store.head("a/b.tar.gz")
        # Nor a temporary file.
        assert list(fs_store._full_path("a/b.tar.gz").parent.iterdir()) == []

    async def test_a_failed_stream_keeps_the_previous_object(self, fs_store: FilesystemStore):
        await fs_store.put_stream("k", _once(b"old"))
        with pytest.raises(_SourceFailed):
            await fs_store.put_stream("k", _fails_after(2))
        assert await fs_store.get("k") == b"old"

    async def test_the_object_is_not_visible_until_the_stream_ends(self, fs_store: FilesystemStore):
        release = asyncio.Event()

        async def slow():
            yield b"part one, "
            await release.wait()
            yield b"part two"

        task = asyncio.create_task(fs_store.put_stream("k2", slow()))
        await asyncio.sleep(0.05)
        with pytest.raises(ObjectNotFoundError):
            await fs_store.head("k2")
        # A listing taken mid-write does not show the temporary file.
        assert [m.key for m in await fs_store.list_prefix("")] == []

        release.set()
        await task
        assert await fs_store.get("k2") == b"part one, part two"


class TestGCSPutStream:
    @staticmethod
    def _client(finalized: list[bytes]) -> MagicMock:
        """A client whose upload, like a resumable upload, completes when the
        reader reports end of stream."""

        def upload_from_file(reader, content_type=None, num_retries=None):  # noqa: ARG001
            data = b""
            while piece := reader.read(512):
                data += piece
            finalized.append(data)

        blob = MagicMock()
        blob.upload_from_file = upload_from_file
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        return client

    async def test_a_failed_stream_is_never_finalized(self):
        store = GCSStore(bucket="test-bucket", prefix="")
        finalized: list[bytes] = []
        with patch.object(store, "_get_sync_client", return_value=self._client(finalized)):
            with pytest.raises(_SourceFailed):
                await store.put_stream("k", _fails_after(20))
        assert finalized == [], "a truncated object was finalized"

    async def test_a_whole_stream_is_uploaded(self):
        store = GCSStore(bucket="test-bucket", prefix="")
        finalized: list[bytes] = []
        with patch.object(store, "_get_sync_client", return_value=self._client(finalized)):
            meta = await store.put_stream("k", _once(b"all of it"))
        assert finalized == [b"all of it"]
        assert meta.size_bytes == len(b"all of it")
