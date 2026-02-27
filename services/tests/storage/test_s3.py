"""
Tests for the S3 storage backend.

Integration tests require LocalStack. Set LOCALSTACK_ENDPOINT to enable.
Unit tests use mocks.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from terrapod.storage.s3 import S3Store


class TestS3StoreUnit:
    """Unit tests with mocked boto3 client."""

    @pytest.fixture
    def store(self) -> S3Store:
        return S3Store(
            bucket="test-bucket",
            region="us-east-1",
            prefix="terrapod",
            presigned_url_expiry_seconds=3600,
        )

    async def test_full_key_with_prefix(self, store: S3Store) -> None:
        assert store._full_key("state/ws1/v1.tfstate") == "terrapod/state/ws1/v1.tfstate"

    async def test_full_key_without_prefix(self) -> None:
        store = S3Store(bucket="test-bucket", prefix="")
        assert store._full_key("state/ws1/v1.tfstate") == "state/ws1/v1.tfstate"

    async def test_strip_prefix(self, store: S3Store) -> None:
        assert store._strip_prefix("terrapod/state/ws1/v1.tfstate") == "state/ws1/v1.tfstate"

    async def test_strip_prefix_no_match(self, store: S3Store) -> None:
        assert store._strip_prefix("other/key") == "other/key"

    async def test_warns_on_high_expiry(self) -> None:
        """Should log a warning when expiry exceeds IRSA credential lifetime."""
        with patch("terrapod.storage.s3.logger") as mock_logger:
            S3Store(bucket="test", presigned_url_expiry_seconds=7200)
            mock_logger.warning.assert_called_once()


class TestS3StoreIntegration:
    """Integration tests using LocalStack. Skipped unless LOCALSTACK_ENDPOINT is set."""

    @pytest.fixture
    async def store(
        self, localstack_available: bool, localstack_endpoint: str, s3_test_bucket: str
    ) -> S3Store | None:
        if not localstack_available:
            pytest.skip("LocalStack not available")
        store = S3Store(
            bucket=s3_test_bucket,
            region="us-east-1",
            endpoint_url=localstack_endpoint,
        )
        # Create the test bucket
        client = await store._get_client()
        try:
            await client.create_bucket(Bucket=s3_test_bucket)
        except Exception:
            pass  # Bucket may already exist
        return store

    async def test_put_get_roundtrip(self, store: S3Store | None) -> None:
        if store is None:
            return
        data = b"s3 integration test data"
        meta = await store.put("integration/test.txt", data, content_type="text/plain")
        assert meta.key == "integration/test.txt"
        assert meta.size_bytes == len(data)

        result = await store.get("integration/test.txt")
        assert result == data
        await store.close()

    async def test_delete_and_exists(self, store: S3Store | None) -> None:
        if store is None:
            return
        await store.put("integration/to-delete.txt", b"data")
        assert await store.exists("integration/to-delete.txt")

        await store.delete("integration/to-delete.txt")
        assert not await store.exists("integration/to-delete.txt")
        await store.close()

    async def test_head(self, store: S3Store | None) -> None:
        if store is None:
            return
        await store.put("integration/head-test.txt", b"head data", content_type="text/plain")
        meta = await store.head("integration/head-test.txt")
        assert meta.size_bytes == 9
        assert meta.content_type == "text/plain"
        await store.close()

    async def test_list_prefix(self, store: S3Store | None) -> None:
        if store is None:
            return
        await store.put("integration/list/a.txt", b"a")
        await store.put("integration/list/b.txt", b"b")
        await store.put("integration/other/c.txt", b"c")

        results = await store.list_prefix("integration/list/")
        keys = [m.key for m in results]
        assert len(keys) == 2
        assert "integration/list/a.txt" in keys
        assert "integration/list/b.txt" in keys
        await store.close()
