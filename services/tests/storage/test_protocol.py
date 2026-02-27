"""
Tests for the ObjectStore protocol and types.
"""

from datetime import UTC, datetime

from terrapod.storage.filesystem import FilesystemStore
from terrapod.storage.protocol import (
    ObjectMeta,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectStorePermissionError,
    PresignedURL,
)


class TestObjectMeta:
    def test_creation(self) -> None:
        meta = ObjectMeta(
            key="test/key.txt",
            size_bytes=100,
            content_type="text/plain",
            etag="abc123",
            last_modified=datetime.now(UTC),
        )
        assert meta.key == "test/key.txt"
        assert meta.size_bytes == 100
        assert meta.metadata == {}

    def test_with_metadata(self) -> None:
        meta = ObjectMeta(
            key="test/key.txt",
            size_bytes=100,
            content_type="text/plain",
            etag="abc123",
            last_modified=datetime.now(UTC),
            metadata={"workspace": "ws-123"},
        )
        assert meta.metadata["workspace"] == "ws-123"

    def test_frozen(self) -> None:
        meta = ObjectMeta(
            key="test/key.txt",
            size_bytes=100,
            content_type="text/plain",
            etag="abc123",
            last_modified=datetime.now(UTC),
        )
        try:
            meta.key = "other"  # type: ignore[misc]
            raise AssertionError("Should not be able to set attributes on frozen dataclass")
        except AttributeError:
            pass


class TestPresignedURL:
    def test_creation(self) -> None:
        url = PresignedURL(
            url="https://example.com/obj?sig=abc",
            expires_at=datetime.now(UTC),
        )
        assert url.url.startswith("https://")
        assert url.headers == {}

    def test_with_headers(self) -> None:
        url = PresignedURL(
            url="https://example.com/obj?sig=abc",
            expires_at=datetime.now(UTC),
            headers={"x-ms-blob-type": "BlockBlob"},
        )
        assert url.headers["x-ms-blob-type"] == "BlockBlob"


class TestExceptions:
    def test_object_not_found_error(self) -> None:
        err = ObjectNotFoundError("my/key")
        assert err.key == "my/key"
        assert "my/key" in str(err)
        assert isinstance(err, ObjectStoreError)

    def test_permission_error(self) -> None:
        err = ObjectStorePermissionError("access denied")
        assert isinstance(err, ObjectStoreError)


class TestProtocolCompliance:
    def test_filesystem_store_satisfies_protocol(self) -> None:
        """FilesystemStore should satisfy the ObjectStore protocol via structural typing."""
        assert isinstance(FilesystemStore, type)
        # Runtime check via Protocol
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = FilesystemStore(root_dir=tmpdir)
            assert isinstance(store, ObjectStore)
