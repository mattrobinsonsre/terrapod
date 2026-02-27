"""
Tests for the Azure Blob Storage backend.

Unit tests with mocked Azure SDK. No live Azure integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.storage.azure import AzureStore
from terrapod.storage.protocol import ObjectNotFoundError


def _make_mock_container() -> MagicMock:
    """Create a mock container client with a blob client that has async methods."""
    mock_container = MagicMock()
    mock_blob_client = MagicMock()
    # Azure blob client methods are async
    mock_blob_client.upload_blob = AsyncMock()
    mock_blob_client.download_blob = AsyncMock()
    mock_blob_client.delete_blob = AsyncMock()
    mock_blob_client.get_blob_properties = AsyncMock()
    mock_container.get_blob_client.return_value = mock_blob_client
    return mock_container


class TestAzureStoreUnit:
    @pytest.fixture
    def store(self) -> AzureStore:
        return AzureStore(
            account_name="testaccount",
            container_name="testcontainer",
            prefix="terrapod",
        )

    def test_full_key_with_prefix(self, store: AzureStore) -> None:
        assert store._full_key("state/ws1/v1.tfstate") == "terrapod/state/ws1/v1.tfstate"

    def test_full_key_without_prefix(self) -> None:
        store = AzureStore(account_name="test", container_name="test", prefix="")
        assert store._full_key("state/ws1/v1.tfstate") == "state/ws1/v1.tfstate"

    def test_strip_prefix(self, store: AzureStore) -> None:
        assert store._strip_prefix("terrapod/state/ws1/v1.tfstate") == "state/ws1/v1.tfstate"

    async def test_put_calls_upload_blob(self, store: AzureStore) -> None:
        mock_container = _make_mock_container()
        mock_blob_client = mock_container.get_blob_client.return_value

        store._container_client = mock_container
        meta = await store.put("test.txt", b"hello", content_type="text/plain")
        assert meta.key == "test.txt"
        assert meta.size_bytes == 5
        mock_blob_client.upload_blob.assert_called_once()

    async def test_get_calls_download_blob(self, store: AzureStore) -> None:
        mock_container = _make_mock_container()
        mock_blob_client = mock_container.get_blob_client.return_value
        mock_stream = AsyncMock()
        mock_stream.readall.return_value = b"hello"
        mock_blob_client.download_blob.return_value = mock_stream

        store._container_client = mock_container
        result = await store.get("test.txt")
        assert result == b"hello"

    async def test_get_not_found_raises(self, store: AzureStore) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        mock_container = _make_mock_container()
        mock_blob_client = mock_container.get_blob_client.return_value
        mock_blob_client.download_blob.side_effect = ResourceNotFoundError("not found")

        store._container_client = mock_container
        with pytest.raises(ObjectNotFoundError):
            await store.get("nonexistent")

    async def test_delete_is_idempotent(self, store: AzureStore) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        mock_container = _make_mock_container()
        mock_blob_client = mock_container.get_blob_client.return_value
        mock_blob_client.delete_blob.side_effect = ResourceNotFoundError("not found")

        store._container_client = mock_container
        await store.delete("nonexistent")  # Should not raise

    async def test_exists_true(self, store: AzureStore) -> None:
        mock_container = _make_mock_container()
        mock_blob_client = mock_container.get_blob_client.return_value
        mock_blob_client.get_blob_properties.return_value = MagicMock()

        store._container_client = mock_container
        assert await store.exists("test.txt")

    async def test_exists_false(self, store: AzureStore) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        mock_container = _make_mock_container()
        mock_blob_client = mock_container.get_blob_client.return_value
        mock_blob_client.get_blob_properties.side_effect = ResourceNotFoundError("not found")

        store._container_client = mock_container
        assert not await store.exists("nonexistent")

    async def test_head_returns_metadata(self, store: AzureStore) -> None:
        mock_container = _make_mock_container()
        mock_blob_client = mock_container.get_blob_client.return_value
        mock_props = MagicMock()
        mock_props.size = 100
        mock_props.content_settings.content_type = "text/plain"
        mock_props.etag = '"abc123"'
        mock_props.last_modified = datetime.now(UTC)
        mock_props.metadata = {"key": "value"}
        mock_blob_client.get_blob_properties.return_value = mock_props

        store._container_client = mock_container
        meta = await store.head("test.txt")
        assert meta.size_bytes == 100
        assert meta.content_type == "text/plain"
        assert meta.metadata["key"] == "value"

    async def test_presigned_put_url_includes_blob_type_header(self, store: AzureStore) -> None:
        mock_delegation_key = MagicMock()
        store._delegation_key = mock_delegation_key
        store._delegation_key_expiry = datetime.now(UTC).replace(year=2099)

        with patch("azure.storage.blob.generate_blob_sas", return_value="sas=token"):
            url = await store.presigned_put_url("test.txt", content_type="text/plain")
            assert url.headers["x-ms-blob-type"] == "BlockBlob"
            assert "sas=token" in url.url
