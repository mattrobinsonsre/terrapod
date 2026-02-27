"""
Tests for the GCS storage backend.

Unit tests with mocked GCS SDK. No live GCS integration tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.storage.gcs import GCSStore
from terrapod.storage.protocol import ObjectNotFoundError


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
