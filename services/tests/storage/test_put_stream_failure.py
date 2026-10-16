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
import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from terrapod.storage import gcs
from terrapod.storage.filesystem import FilesystemStore
from terrapod.storage.gcs import GCSStore
from terrapod.storage.protocol import (
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectStorePermissionError,
)


class _SourceFailed(Exception):
    pass


async def _fails_after(n: int):
    for _ in range(n):
        yield b"x" * 1024
    raise _SourceFailed("source stream failed")


async def _pieces(n: int):
    for _ in range(n):
        yield b"x" * 1024


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

        def upload_from_file(reader, **_):
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

    @staticmethod
    def _stopping_client(
        reads: int, error: Exception | None, wait_for: threading.Event | None = None
    ) -> MagicMock:
        """A client whose upload stops after `reads` reads, raising `error` if
        given. With `wait_for`, it waits for that event before stopping."""

        def upload_from_file(reader, **_):
            for _ in range(reads):
                reader.read(512)
            if wait_for is not None:
                wait_for.wait(5)
            if error is not None:
                raise error

        blob = MagicMock()
        blob.upload_from_file = upload_from_file
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        return client

    @staticmethod
    def _watch_full_puts(
        monkeypatch: pytest.MonkeyPatch, *, end_marker_only: bool = False
    ) -> threading.Event:
        """Replace the backend's queue so that a put finding it full sets the
        returned event (only the end marker's, with `end_marker_only`). An
        untimed put that cannot complete fails instead of blocking for good."""
        put_waiting = threading.Event()

        class _Queue(queue.Queue):
            def put(self, item, block=True, timeout=None):  # type: ignore[no-untyped-def]
                if block and self.full() and (item is None or not end_marker_only):
                    put_waiting.set()
                if block and timeout is None:
                    try:
                        super().put(item, True, 5.0)
                    except queue.Full:
                        raise AssertionError("an untimed put blocked on a full queue") from None
                else:
                    super().put(item, block, timeout)

        monkeypatch.setattr(gcs, "queue", SimpleNamespace(Queue=_Queue, Full=queue.Full))
        return put_waiting

    @pytest.mark.parametrize("reads", [0, 3])
    @pytest.mark.usefixtures("gcs_queue_puts_are_bounded")
    async def test_a_dead_upload_thread_fails_the_stream_promptly(self, reads: int):
        """Once the upload thread is gone nothing drains the queue, so a stream
        longer than the queue used to block on `put` forever (#2106)."""
        store = GCSStore(bucket="test-bucket", prefix="")
        client = self._stopping_client(reads, RuntimeError("upload failed"))
        with patch.object(store, "_get_sync_client", return_value=client):
            with pytest.raises(ObjectStoreError, match="upload failed"):
                await asyncio.wait_for(store.put_stream("k", _pieces(100)), timeout=10)

    @pytest.mark.usefixtures("gcs_queue_puts_are_bounded")
    async def test_a_dead_upload_thread_keeps_its_permission_error(self):
        """A 403 from a dead upload thread still maps to ObjectStorePermissionError."""
        store = GCSStore(bucket="test-bucket", prefix="")
        client = self._stopping_client(0, RuntimeError("403 Forbidden"))
        with patch.object(store, "_get_sync_client", return_value=client):
            with pytest.raises(ObjectStorePermissionError, match="403"):
                await asyncio.wait_for(store.put_stream("k", _pieces(100)), timeout=10)

    @pytest.mark.usefixtures("gcs_queue_puts_are_bounded")
    async def test_an_upload_thread_that_stops_early_is_not_a_success(self):
        """An upload that returns before the stream ends must not be reported
        as a complete object."""
        store = GCSStore(bucket="test-bucket", prefix="")
        client = self._stopping_client(3, None)
        with patch.object(store, "_get_sync_client", return_value=client):
            with pytest.raises(ObjectStoreError, match="stopped before the stream ended"):
                await asyncio.wait_for(store.put_stream("k", _pieces(100)), timeout=10)

    async def test_an_upload_thread_that_dies_while_a_put_waits_fails_the_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The thread dies while the stream is blocked on a full queue, as when
        GCS refuses an upload part-way. A put without a timeout would wait for
        it forever (#2106)."""
        put_waiting = self._watch_full_puts(monkeypatch)
        store = GCSStore(bucket="test-bucket", prefix="")
        client = self._stopping_client(0, RuntimeError("upload failed"), wait_for=put_waiting)
        with patch.object(store, "_get_sync_client", return_value=client):
            with pytest.raises(ObjectStoreError, match="upload failed"):
                await asyncio.wait_for(store.put_stream("k", _pieces(100)), timeout=10)

    async def test_an_upload_thread_that_returns_while_the_end_marker_waits_is_not_a_success(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Four pieces fill the queue (maxsize=4), so the end marker is the put
        left waiting when the thread returns without an error. An end marker
        that was never taken means the stream was not stored whole."""
        put_waiting = self._watch_full_puts(monkeypatch, end_marker_only=True)
        store = GCSStore(bucket="test-bucket", prefix="")
        client = self._stopping_client(0, None, wait_for=put_waiting)
        with patch.object(store, "_get_sync_client", return_value=client):
            with pytest.raises(ObjectStoreError, match="stopped before the stream ended"):
                await asyncio.wait_for(store.put_stream("k", _pieces(4)), timeout=10)
