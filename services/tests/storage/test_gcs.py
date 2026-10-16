"""
Tests for the GCS storage backend.

Unit tests with a mocked GCS SDK, or with the real SDK over a fake HTTP
session. No live GCS integration tests.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import google_crc32c
import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from google.cloud.storage import blob as blob_module
from google.cloud.storage.blob import Blob

from terrapod.storage.gcs import GCSStore
from terrapod.storage.protocol import ObjectNotFoundError, ObjectStoreError


class TestGCSLibraryContract:
    """The upload_from_file call is checked against the installed library.

    A fake accepts whatever it was written to accept, so the `num_retries`
    keyword removed in 3.x went unnoticed (#2106). Reading the keywords from
    `gcs.py` itself means nothing has to be kept in step by hand.
    """

    def test_upload_from_file_accepts_the_keywords_gcs_py_passes(self) -> None:
        source = Path(inspect.getsourcefile(GCSStore))
        calls = [
            node
            for node in ast.walk(ast.parse(source.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "upload_from_file"
        ]
        assert calls, "no upload_from_file call found in gcs.py"

        parameters = inspect.signature(Blob.upload_from_file).parameters.values()
        # A **kwargs parameter would accept any keyword and make this vacuous.
        assert all(p.kind is not inspect.Parameter.VAR_KEYWORD for p in parameters)
        accepted = {p.name for p in parameters}
        for call in calls:
            passed = {kw.arg for kw in call.keywords if kw.arg is not None}
            assert passed <= accepted, (
                f"gcs.py:{call.lineno} passes {sorted(passed - accepted)}, "
                "which Blob.upload_from_file does not accept"
            )


class _FakeResumableGCS(AuthorizedSession):
    """Answers a resumable upload the way GCS does: 200 and a Location to start,
    308 and a Range for each intermediate chunk, 200 and the object for the last.
    Only `request` is replaced; the rest is the real session the client expects."""

    def __init__(self) -> None:
        super().__init__(AnonymousCredentials())
        self.received = b""
        self.finalized: list[bytes] = []

    def request(self, method, url, data=None, headers=None, **kwargs):  # noqa: ARG002
        response = requests.Response()
        response.url = url
        response._content = b""
        if method == "POST" and "uploadType=resumable" in url:
            response.status_code = 200
            response.headers["location"] = "https://upload.test/session?upload_id=1"
        elif method == "PUT" and "upload_id=1" in url:
            self.received += data
            if headers["content-range"].endswith("/*"):
                response.status_code = 308
                response.headers["range"] = f"bytes=0-{len(self.received) - 1}"
            else:
                self.finalized.append(self.received)
                crc32c = google_crc32c.Checksum(self.received).digest()
                response.status_code = 200
                response._content = json.dumps(
                    {
                        "name": "k",
                        "size": str(len(self.received)),
                        "crc32c": base64.b64encode(crc32c).decode(),
                    }
                ).encode()
        else:
            raise AssertionError(f"unexpected request: {method} {url}")
        return response


class TestGCSPutStreamThroughTheLibrary:
    """put_stream driven through google-cloud-storage's real resumable upload.

    Only the HTTP session is fake, so the library's own code decides what it
    needs from the stream. A fake `upload_from_file` cannot see that it calls
    `tell()` on it, which `_QueueReader` did not support (#2106). The client's
    `_http` argument and `_DEFAULT_CHUNKSIZE` are private to the library, so a
    library upgrade may need these tests adjusting.
    """

    @staticmethod
    async def _pieces(n: int):
        for i in range(n):
            yield bytes([i]) * 64 * 1024

    # 64 KiB pieces against a 256 KiB chunk: 1 and 3 pieces fit in the queue and
    # one chunk; 20 pieces overrun the queue and span five chunks, each of which
    # takes its byte range from tell().
    @pytest.mark.parametrize("pieces", [1, 3, 20])
    @pytest.mark.usefixtures("gcs_queue_puts_are_bounded")
    async def test_a_stream_is_uploaded_whole(self, monkeypatch, pieces: int) -> None:
        # The smallest chunk size the library accepts, so a small stream still
        # goes through the multi-chunk path.
        monkeypatch.setattr(blob_module, "_DEFAULT_CHUNKSIZE", 256 * 1024)
        session = _FakeResumableGCS()
        client = storage.Client(project="test", credentials=AnonymousCredentials(), _http=session)
        store = GCSStore(bucket="test-bucket", prefix="")

        with patch.object(store, "_get_sync_client", return_value=client):
            meta = await asyncio.wait_for(store.put_stream("k", self._pieces(pieces)), timeout=10)

        expected = b"".join([bytes([i]) * 64 * 1024 for i in range(pieces)])
        assert session.finalized == [expected]
        assert meta.size_bytes == len(expected)


class TestGCSStoreUnit:
    @pytest.fixture
    def store(self) -> GCSStore:
        return GCSStore(
            bucket="test-bucket",
            prefix="terrapod",
            project_id="test-project",
        )

    def test_full_key_with_prefix(self, store: GCSStore) -> None:
        assert store._full_key("state/ws1/v1.tfstate") == "terrapod/state/ws1/v1.tfstate"

    def test_full_key_without_prefix(self) -> None:
        store = GCSStore(bucket="test-bucket", prefix="")
        assert store._full_key("state/ws1/v1.tfstate") == "state/ws1/v1.tfstate"

    def test_strip_prefix(self, store: GCSStore) -> None:
        assert store._strip_prefix("terrapod/state/ws1/v1.tfstate") == "state/ws1/v1.tfstate"

    async def test_put_calls_upload(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            meta = await store.put("test.txt", b"hello", content_type="text/plain")
            assert meta.key == "test.txt"
            assert meta.size_bytes == 5
            mock_storage.upload.assert_called_once()

    async def test_get_calls_download(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.download.return_value = b"hello"
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            result = await store.get("test.txt")
            assert result == b"hello"

    async def test_get_not_found_raises(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.download.side_effect = Exception("404 Not Found")
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            with pytest.raises(ObjectNotFoundError):
                await store.get("nonexistent")

    async def test_delete_is_idempotent(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.delete.side_effect = Exception("404 Not Found")
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            await store.delete("nonexistent")  # Should not raise

    async def test_exists_true(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.download_metadata.return_value = {"size": "100"}
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            assert await store.exists("test.txt")

    async def test_exists_false(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.download_metadata.side_effect = Exception("404 Not Found")
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            assert not await store.exists("nonexistent")

    async def test_head_returns_metadata(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.download_metadata.return_value = {
            "size": "100",
            "contentType": "text/plain",
            "etag": "abc123",
            "updated": "2025-01-01T00:00:00Z",
            "metadata": {"key": "value"},
        }
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            meta = await store.head("test.txt")
            assert meta.size_bytes == 100
            assert meta.content_type == "text/plain"
            assert meta.metadata["key"] == "value"

    async def test_head_not_found_raises(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.download_metadata.side_effect = Exception("404 Not Found")
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            with pytest.raises(ObjectNotFoundError):
                await store.head("nonexistent")

    async def test_list_prefix(self, store: GCSStore) -> None:
        mock_storage = AsyncMock()
        mock_storage.list_objects.return_value = {
            "items": [
                {
                    "name": "terrapod/logs/a.txt",
                    "size": "10",
                    "contentType": "text/plain",
                    "etag": "abc",
                    "updated": "2025-01-01T00:00:00Z",
                },
                {
                    "name": "terrapod/logs/b.txt",
                    "size": "20",
                    "contentType": "text/plain",
                    "etag": "def",
                    "updated": "2025-01-01T00:00:00Z",
                },
            ]
        }
        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            results = await store.list_prefix("logs/")
            assert len(results) == 2
            assert results[0].key == "logs/a.txt"
            assert results[1].key == "logs/b.txt"

    async def test_list_prefix_follows_every_page(self, store: GCSStore) -> None:
        """The bug this fixes (#1155).

        The GCS JSON API caps a response at `maxResults` (1000 by default) and
        hands back a `nextPageToken`. The previous implementation called
        `list_objects` once and ignored the token, so **any prefix holding more
        than a thousand objects returned a partial list with nothing to say it
        was partial** — including `state/`, which the break-glass recovery index
        is built from, and `vcs_archives/`, which the retention sweep walks.
        """

        def page(names: list[str], token: str | None) -> dict:
            body: dict = {
                "items": [
                    {
                        "name": n,
                        "size": "1",
                        "contentType": "text/plain",
                        "etag": "e",
                        "updated": "2025-01-01T00:00:00Z",
                    }
                    for n in names
                ]
            }
            if token:
                body["nextPageToken"] = token
            return body

        mock_storage = AsyncMock()
        mock_storage.list_objects.side_effect = [
            page(["terrapod/logs/a.txt", "terrapod/logs/b.txt"], "tok-1"),
            page(["terrapod/logs/c.txt"], "tok-2"),
            page(["terrapod/logs/d.txt"], None),
        ]

        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            results = await store.list_prefix("logs/")

        assert [m.key for m in results] == [
            "logs/a.txt",
            "logs/b.txt",
            "logs/c.txt",
            "logs/d.txt",
        ]
        assert mock_storage.list_objects.await_count == 3
        # Each continuation carries the token the previous page returned.
        tokens = [
            call.kwargs["params"].get("pageToken")
            for call in mock_storage.list_objects.await_args_list
        ]
        assert tokens == [None, "tok-1", "tok-2"]

    async def test_list_prefix_narrows_server_side_with_start_offset(self, store: GCSStore) -> None:
        """`startOffset` keeps the cursor server-side rather than filtering a
        full listing locally — and because it is *inclusive*, the boundary key
        still has to be dropped to honour strictly-greater."""
        mock_storage = AsyncMock()
        mock_storage.list_objects.return_value = {
            "items": [
                {
                    "name": f"terrapod/logs/{n}",
                    "size": "1",
                    "contentType": "text/plain",
                    "etag": "e",
                    "updated": "2025-01-01T00:00:00Z",
                }
                for n in ("a.txt", "b.txt")
            ]
        }

        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            results = await store.list_prefix("logs/", after="logs/a.txt")

        params = mock_storage.list_objects.await_args.kwargs["params"]
        assert params["startOffset"] == "terrapod/logs/a.txt"
        assert [m.key for m in results] == ["logs/b.txt"], (
            "startOffset is inclusive, so the boundary key must be dropped"
        )

    async def test_list_prefix_stops_at_the_limit_across_pages(self, store: GCSStore) -> None:
        """`maxResults` bounds a page; the walk has to bound itself."""
        mock_storage = AsyncMock()
        mock_storage.list_objects.return_value = {
            "items": [
                {
                    "name": f"terrapod/logs/{n}.txt",
                    "size": "1",
                    "contentType": "text/plain",
                    "etag": "e",
                    "updated": "2025-01-01T00:00:00Z",
                }
                for n in ("a", "b", "c")
            ],
            # A token that would go on forever if the limit were not honoured.
            "nextPageToken": "more",
        }

        with patch.object(store, "_get_aio_storage", return_value=mock_storage):
            results = await store.list_prefix("logs/", limit=2)

        assert [m.key for m in results] == ["logs/a.txt", "logs/b.txt"]
        assert mock_storage.list_objects.await_count == 1
        assert mock_storage.list_objects.await_args.kwargs["params"]["maxResults"] == "2"

    async def test_put_stream_uses_sync_client(self, store: GCSStore) -> None:
        mock_blob = MagicMock()
        # Like the real upload, read the stream to its end.
        mock_blob.upload_from_file = MagicMock(side_effect=lambda reader, **_: reader.read())
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch.object(store, "_get_sync_client", return_value=mock_client):

            async def _chunks():
                yield b"hello"
                yield b"world"

            meta = await store.put_stream(
                "test/stream.bin", _chunks(), content_type="application/zip"
            )
            assert meta.key == "test/stream.bin"
            assert meta.size_bytes == 10
            mock_blob.upload_from_file.assert_called_once()

    async def test_get_stream(self, store: GCSStore) -> None:
        import io

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.open.return_value = io.BytesIO(b"hello world")
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch.object(store, "_get_sync_client", return_value=mock_client):
            result = b""
            async for chunk in store.get_stream("test/stream.bin", chunk_size=5):
                result += chunk
            assert result == b"hello world"

    async def test_get_stream_not_found(self, store: GCSStore) -> None:
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        async def read_all() -> None:
            async for _ in store.get_stream("test/missing.bin"):
                pass

        with patch.object(store, "_get_sync_client", return_value=mock_client):
            with pytest.raises(ObjectNotFoundError):
                await asyncio.wait_for(read_all(), timeout=10)

    async def test_get_stream_delivers_what_was_read_before_an_error(self, store: GCSStore) -> None:
        import io

        class _Reader(io.BytesIO):
            def read(self, n: int | None = -1) -> bytes:
                data = super().read(n)
                if not data:
                    raise OSError("connection reset")
                return data

        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.open.return_value = _Reader(b"helloworld")
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        chunks: list[bytes] = []

        async def read_all() -> None:
            async for chunk in store.get_stream("test/stream.bin", chunk_size=5):
                chunks.append(chunk)

        with patch.object(store, "_get_sync_client", return_value=mock_client):
            with pytest.raises(ObjectStoreError, match="connection reset"):
                await asyncio.wait_for(read_all(), timeout=10)
        assert chunks == [b"hello", b"world"]

    async def test_presigned_put_url_has_content_type_header(self, store: GCSStore) -> None:
        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed"
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket

        with patch.object(store, "_get_sync_client", return_value=mock_client):
            url = await store.presigned_put_url("test.txt", content_type="text/plain")
            assert url.headers["Content-Type"] == "text/plain"
            mock_blob.generate_signed_url.assert_called_once()
