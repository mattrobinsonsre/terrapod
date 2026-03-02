"""
Conformance test suite for storage backends.

Runs identical tests against every available backend. Always runs against
filesystem. Optionally runs against S3 via LocalStack when available.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from terrapod.storage.filesystem import FilesystemStore
from terrapod.storage.protocol import ObjectNotFoundError, ObjectStore


async def _conformance_put_get(store: ObjectStore) -> None:
    """Test: put/get roundtrip produces identical data."""
    data = b"conformance test data \x00\x01\x02"
    meta = await store.put(
        "conformance/roundtrip.bin", data, content_type="application/octet-stream"
    )
    assert meta.key == "conformance/roundtrip.bin"
    assert meta.size_bytes == len(data)
    assert meta.content_type == "application/octet-stream"

    result = await store.get("conformance/roundtrip.bin")
    assert result == data


async def _conformance_delete_idempotent(store: ObjectStore) -> None:
    """Test: delete is idempotent — does not raise for nonexistent keys."""
    await store.put("conformance/to-delete.txt", b"data")
    await store.delete("conformance/to-delete.txt")
    assert not await store.exists("conformance/to-delete.txt")

    # Second delete should not raise
    await store.delete("conformance/to-delete.txt")


async def _conformance_head_metadata(store: ObjectStore) -> None:
    """Test: head returns correct metadata."""
    data = b"metadata test content"
    await store.put(
        "conformance/head.txt",
        data,
        content_type="text/plain",
        metadata={"env": "test"},
    )

    meta = await store.head("conformance/head.txt")
    assert meta.key == "conformance/head.txt"
    assert meta.size_bytes == len(data)
    assert meta.content_type == "text/plain"
    assert meta.etag
    assert meta.last_modified


async def _conformance_list_prefix(store: ObjectStore) -> None:
    """Test: list_prefix returns only matching objects."""
    await store.put("conformance/list/alpha.txt", b"a")
    await store.put("conformance/list/beta.txt", b"b")
    await store.put("conformance/other/gamma.txt", b"c")

    results = await store.list_prefix("conformance/list/")
    keys = {m.key for m in results}
    assert "conformance/list/alpha.txt" in keys
    assert "conformance/list/beta.txt" in keys
    assert "conformance/other/gamma.txt" not in keys


async def _conformance_presigned_urls(store: ObjectStore) -> None:
    """Test: presigned URL generation succeeds."""
    get_url = await store.presigned_get_url("conformance/presigned.txt")
    assert get_url.url
    assert get_url.expires_at

    put_url = await store.presigned_put_url("conformance/presigned.txt", content_type="text/plain")
    assert put_url.url
    assert put_url.expires_at


async def _conformance_nonexistent_key_errors(store: ObjectStore) -> None:
    """Test: get and head raise ObjectNotFoundError for missing keys."""
    with pytest.raises(ObjectNotFoundError):
        await store.get("conformance/does-not-exist")

    with pytest.raises(ObjectNotFoundError):
        await store.head("conformance/does-not-exist")


# --- Filesystem conformance ---


@pytest_asyncio.fixture
async def conformance_fs_store() -> AsyncGenerator[FilesystemStore]:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FilesystemStore(
            root_dir=tmpdir,
            hmac_secret="conformance-test-secret",
            base_url="http://localhost:8000",
        )
        yield store
        await store.close()


class TestFilesystemConformance:
    async def test_put_get(self, conformance_fs_store: FilesystemStore) -> None:
        await _conformance_put_get(conformance_fs_store)

    async def test_delete_idempotent(self, conformance_fs_store: FilesystemStore) -> None:
        await _conformance_delete_idempotent(conformance_fs_store)

    async def test_head_metadata(self, conformance_fs_store: FilesystemStore) -> None:
        await _conformance_head_metadata(conformance_fs_store)

    async def test_list_prefix(self, conformance_fs_store: FilesystemStore) -> None:
        await _conformance_list_prefix(conformance_fs_store)

    async def test_presigned_urls(self, conformance_fs_store: FilesystemStore) -> None:
        await _conformance_presigned_urls(conformance_fs_store)

    async def test_nonexistent_key_errors(self, conformance_fs_store: FilesystemStore) -> None:
        await _conformance_nonexistent_key_errors(conformance_fs_store)


# --- S3 conformance (LocalStack) ---


@pytest_asyncio.fixture
async def conformance_s3_store() -> AsyncGenerator[None]:
    endpoint = os.environ.get("LOCALSTACK_ENDPOINT", "")
    if not endpoint:
        pytest.skip("LocalStack not available (set LOCALSTACK_ENDPOINT)")
        return

    import urllib.request

    try:
        req = urllib.request.Request(f"{endpoint}/_localstack/health", method="GET")
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception:
        pytest.skip("LocalStack not reachable")
        return

    from terrapod.storage.s3 import S3Store

    bucket = os.environ.get("S3_TEST_BUCKET", "terrapod-conformance")
    store = S3Store(
        bucket=bucket,
        region="us-east-1",
        endpoint_url=endpoint,
        prefix="conformance",
    )

    # Create the test bucket
    client = await store._get_client()
    try:
        await client.create_bucket(Bucket=bucket)
    except Exception:
        pass

    yield store  # type: ignore[misc]
    await store.close()


class TestS3Conformance:
    async def test_put_get(self, conformance_s3_store: object) -> None:
        await _conformance_put_get(conformance_s3_store)  # type: ignore[arg-type]

    async def test_delete_idempotent(self, conformance_s3_store: object) -> None:
        await _conformance_delete_idempotent(conformance_s3_store)  # type: ignore[arg-type]

    async def test_head_metadata(self, conformance_s3_store: object) -> None:
        await _conformance_head_metadata(conformance_s3_store)  # type: ignore[arg-type]

    async def test_list_prefix(self, conformance_s3_store: object) -> None:
        await _conformance_list_prefix(conformance_s3_store)  # type: ignore[arg-type]

    async def test_presigned_urls(self, conformance_s3_store: object) -> None:
        await _conformance_presigned_urls(conformance_s3_store)  # type: ignore[arg-type]

    async def test_nonexistent_key_errors(self, conformance_s3_store: object) -> None:
        await _conformance_nonexistent_key_errors(conformance_s3_store)  # type: ignore[arg-type]
