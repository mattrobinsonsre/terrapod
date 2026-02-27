"""
Shared fixtures for storage tests.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from terrapod.storage.filesystem import FilesystemStore


@pytest_asyncio.fixture
async def fs_store() -> AsyncGenerator[FilesystemStore]:
    """Create a FilesystemStore with a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FilesystemStore(
            root_dir=tmpdir,
            hmac_secret="test-secret-key-for-hmac-signing",
            base_url="http://localhost:8000",
            presigned_url_expiry_seconds=3600,
        )
        yield store
        await store.close()


@pytest.fixture
def localstack_available() -> bool:
    """Check if LocalStack is available for S3 integration tests."""
    return os.environ.get("LOCALSTACK_ENDPOINT", "") != ""


@pytest.fixture
def localstack_endpoint() -> str:
    """Return the LocalStack endpoint URL."""
    return os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4566")


@pytest.fixture
def s3_test_bucket() -> str:
    """Return the S3 test bucket name."""
    return os.environ.get("S3_TEST_BUCKET", "terrapod-test")
