"""
Tests for the filesystem storage backend.

Includes presigned URL endpoint tests via FastAPI test client.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI

from terrapod.storage.filesystem import FilesystemStore
from terrapod.storage.filesystem_routes import router, set_filesystem_store
from terrapod.storage.protocol import ObjectNotFoundError, ObjectStoreError


class TestFilesystemStore:
    async def test_put_and_get(self, fs_store: FilesystemStore) -> None:
        data = b"hello world"
        meta = await fs_store.put("test/file.txt", data, content_type="text/plain")
        assert meta.key == "test/file.txt"
        assert meta.size_bytes == len(data)
        assert meta.content_type == "text/plain"

        result = await fs_store.get("test/file.txt")
        assert result == data

    async def test_get_nonexistent_raises(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectNotFoundError):
            await fs_store.get("nonexistent/key")

    async def test_delete_existing(self, fs_store: FilesystemStore) -> None:
        await fs_store.put("to-delete.txt", b"data")
        assert await fs_store.exists("to-delete.txt")

        await fs_store.delete("to-delete.txt")
        assert not await fs_store.exists("to-delete.txt")

    async def test_delete_nonexistent_is_idempotent(self, fs_store: FilesystemStore) -> None:
        await fs_store.delete("never-existed.txt")  # Should not raise

    async def test_exists(self, fs_store: FilesystemStore) -> None:
        assert not await fs_store.exists("nope")
        await fs_store.put("yep", b"data")
        assert await fs_store.exists("yep")

    async def test_head(self, fs_store: FilesystemStore) -> None:
        await fs_store.put(
            "meta-test.bin", b"\x00\x01\x02", content_type="application/octet-stream"
        )
        meta = await fs_store.head("meta-test.bin")
        assert meta.size_bytes == 3
        assert meta.content_type == "application/octet-stream"
        assert meta.etag

    async def test_head_nonexistent_raises(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectNotFoundError):
            await fs_store.head("nonexistent")

    async def test_list_prefix(self, fs_store: FilesystemStore) -> None:
        await fs_store.put("logs/ws1/plan.log", b"plan1")
        await fs_store.put("logs/ws1/apply.log", b"apply1")
        await fs_store.put("logs/ws2/plan.log", b"plan2")
        await fs_store.put("state/ws1/v1.tfstate", b"state")

        results = await fs_store.list_prefix("logs/ws1/")
        keys = [m.key for m in results]
        assert len(keys) == 2
        assert "logs/ws1/plan.log" in keys
        assert "logs/ws1/apply.log" in keys

    async def test_list_prefix_empty(self, fs_store: FilesystemStore) -> None:
        results = await fs_store.list_prefix("nonexistent/")
        assert results == []

    async def test_put_with_metadata(self, fs_store: FilesystemStore) -> None:
        metadata = {"workspace": "ws-123", "run": "run-456"}
        await fs_store.put("with-meta.txt", b"data", metadata=metadata)
        meta = await fs_store.head("with-meta.txt")
        assert meta.metadata["workspace"] == "ws-123"
        assert meta.metadata["run"] == "run-456"

    async def test_path_traversal_rejected(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectStoreError):
            await fs_store.put("../escape.txt", b"nope")

    async def test_absolute_path_rejected(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectStoreError):
            await fs_store.put("/etc/passwd", b"nope")


class TestFilesystemPresignedURLs:
    async def test_presigned_get_url(self, fs_store: FilesystemStore) -> None:
        url = await fs_store.presigned_get_url("test/key")
        assert "sig=" in url.url
        assert "expires=" in url.url
        assert url.expires_at

    async def test_presigned_put_url(self, fs_store: FilesystemStore) -> None:
        url = await fs_store.presigned_put_url("test/key", content_type="text/plain")
        assert "sig=" in url.url
        assert "content_type=" in url.url
        assert url.headers["Content-Type"] == "text/plain"

    async def test_signature_verification(self, fs_store: FilesystemStore) -> None:
        expires = int(time.time()) + 3600
        sig = fs_store._sign("GET", "test/key", expires)
        assert fs_store.verify_signature("GET", "test/key", str(expires), sig)

    async def test_expired_signature_rejected(self, fs_store: FilesystemStore) -> None:
        expires = int(time.time()) - 10  # Already expired
        sig = fs_store._sign("GET", "test/key", expires)
        assert not fs_store.verify_signature("GET", "test/key", str(expires), sig)

    async def test_wrong_operation_rejected(self, fs_store: FilesystemStore) -> None:
        expires = int(time.time()) + 3600
        sig = fs_store._sign("GET", "test/key", expires)
        assert not fs_store.verify_signature("PUT", "test/key", str(expires), sig)


class TestFilesystemRoutes:
    """Test the presigned URL FastAPI endpoints."""

    @pytest.fixture
    def app(self, fs_store: FilesystemStore) -> FastAPI:
        """Create a test FastAPI app with filesystem routes."""
        test_app = FastAPI()
        set_filesystem_store(fs_store)
        test_app.include_router(router, prefix="/api/v2")
        return test_app

    async def test_put_and_get_via_routes(self, app: FastAPI, fs_store: FilesystemStore) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Get a presigned PUT URL
            put_url = await fs_store.presigned_put_url("route-test.txt", content_type="text/plain")
            # Extract path + query from the URL
            from urllib.parse import urlparse

            parsed = urlparse(put_url.url)
            path = parsed.path + "?" + parsed.query

            # PUT the data
            resp = await client.put(path, content=b"hello from route test")
            assert resp.status_code == 201

            # Get a presigned GET URL
            get_url = await fs_store.presigned_get_url("route-test.txt")
            parsed = urlparse(get_url.url)
            path = parsed.path + "?" + parsed.query

            # GET the data
            resp = await client.get(path)
            assert resp.status_code == 200
            assert resp.content == b"hello from route test"

    async def test_get_invalid_signature(self, app: FastAPI) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/storage/get/test.txt?expires=9999999999&sig=invalid")
            assert resp.status_code == 403

    async def test_get_nonexistent_object(self, app: FastAPI, fs_store: FilesystemStore) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            get_url = await fs_store.presigned_get_url("does-not-exist.txt")
            from urllib.parse import urlparse

            parsed = urlparse(get_url.url)
            path = parsed.path + "?" + parsed.query

            resp = await client.get(path)
            assert resp.status_code == 404
