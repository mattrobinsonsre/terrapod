"""
Shared fixtures for storage tests.
"""

from __future__ import annotations

import os
import queue
import tempfile
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terrapod.storage import gcs
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


class _BoundedPutQueue(queue.Queue):
    """A queue whose untimed blocking put gives up instead of waiting forever."""

    def put(self, item, block=True, timeout=None):  # type: ignore[no-untyped-def]
        if block and timeout is None:
            timeout = 5.0
        super().put(item, block, timeout)


@pytest.fixture
def gcs_queue_puts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound every untimed put on the GCS backend's queues.

    A put that nothing will drain blocks an executor thread for good, and
    the event loop waits for that thread at teardown, so a regression in
    put_stream would stall the run rather than fail it. With this, it fails
    with queue.Full (#2106).
    """
    monkeypatch.setattr(gcs, "queue", SimpleNamespace(Queue=_BoundedPutQueue, Full=queue.Full))


@pytest.fixture
def localstack_available() -> bool:
    """Check if LocalStack is available for S3 integration tests.

    Verifies both that the env var is set AND that the service is actually
    reachable, avoiding failures when the env var is set (e.g. in
    docker-compose) but LocalStack hasn't finished starting.
    """
    endpoint = os.environ.get("LOCALSTACK_ENDPOINT", "")
    if not endpoint:
        return False

    import urllib.request

    try:
        req = urllib.request.Request(f"{endpoint}/_localstack/health", method="GET")
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture
def localstack_endpoint() -> str:
    """Return the LocalStack endpoint URL."""
    return os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4566")


@pytest.fixture
def s3_test_bucket() -> str:
    """Return the S3 test bucket name."""
    return os.environ.get("S3_TEST_BUCKET", "terrapod-test")
