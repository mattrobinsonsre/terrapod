"""Tests for the cost-estimation pricesheet cache service (#871).

Exercises the real download→gunzip→store path (with a fake httpx client and an
AsyncMock storage that consumes the streamed chunks), plus the pull-through
``ensure_pricesheet`` freshness logic (missing / fresh / stale / stale-fallback).
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import cost_pricesheet_service as svc

_YAML = (
    b"schema: terrapod-pricesheet/v1\n"
    b"currency: USD\n"
    b"products:\n"
    b"- service: AmazonEC2\n"
    b"  family: Compute\n"
    b"  match: type=aws_instance\n"
    b"  pricing: region=us-east-1\n"
    b"  price: '0.10'\n"
    b"  price_type: t\n"
)


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self, chunk_size: int | None = None):
        mid = len(self._data) // 2
        yield self._data[:mid]
        yield self._data[mid:]


class _FakeStreamCtx:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _FakeResp:
        return _FakeResp(self._data)

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeClient:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, method: str, url: str, timeout: float | None = None) -> _FakeStreamCtx:
        return _FakeStreamCtx(self._data)


def _storage_capturing(captured: dict) -> AsyncMock:
    async def fake_put_stream(key, chunks, content_type="application/octet-stream", metadata=None):
        data = b""
        async for chunk in chunks:
            data += chunk
        captured["key"] = key
        captured["data"] = data
        captured["content_type"] = content_type
        return MagicMock()

    storage = AsyncMock()
    storage.put_stream = fake_put_stream
    return storage


def _meta(age: timedelta) -> MagicMock:
    m = MagicMock()
    m.last_modified = datetime.now(UTC) - age
    return m


# --- refresh (real gunzip + streamed store) --------------------------------


async def test_refresh_downloads_builds_sqlite_index_and_stores():
    # refresh now gunzips → builds a SQLite index → stores the .sqlite (#1034).
    gz = gzip.compress(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)

    with patch.object(svc.httpx, "AsyncClient", lambda **kw: _FakeClient(gz)):
        size = await svc.refresh_pricesheet(storage)

    assert captured["key"] == "cache/cost/prices.sqlite"
    assert captured["content_type"] == "application/x-sqlite3"
    assert size == len(captured["data"]) > 0
    assert captured["data"][:16].startswith(b"SQLite format 3")  # real sqlite file

    # the stored index is queryable and has the sheet's product
    import os
    import tempfile

    from terrapod.services.cost.pricesheet_db import PricesheetIndex

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(captured["data"])
        idx = PricesheetIndex.open(path)
        cands = list(idx.candidates("aws_instance", "us-east-1"))
        assert len(cands) == 1 and cands[0].price.value == 0.10
        idx.close()
    finally:
        os.unlink(path)


async def test_pricesheet_available_and_download_url():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    presigned = MagicMock()
    presigned.url = "https://example/presigned/prices.sqlite"
    storage.presigned_get_url = AsyncMock(return_value=presigned)

    assert await svc.pricesheet_available(storage) is True
    assert await svc.pricesheet_download_url(storage) == "https://example/presigned/prices.sqlite"
    storage.exists.assert_awaited_with("cache/cost/prices.sqlite")


# --- pull-through ensure_pricesheet ----------------------------------------


async def test_ensure_fetches_when_missing():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=False)
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        assert await svc.ensure_pricesheet(storage) is True
        refresh.assert_awaited_once()


async def test_ensure_skips_when_fresh():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    storage.head = AsyncMock(return_value=_meta(timedelta(hours=1)))  # fresh
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        assert await svc.ensure_pricesheet(storage) is True
        refresh.assert_not_awaited()


async def test_ensure_refetches_when_stale():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    storage.head = AsyncMock(return_value=_meta(timedelta(days=2)))  # stale
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        assert await svc.ensure_pricesheet(storage) is True
        refresh.assert_awaited_once()


async def test_ensure_serves_stale_copy_when_refresh_fails():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    storage.head = AsyncMock(return_value=_meta(timedelta(days=2)))  # stale
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        refresh.side_effect = RuntimeError("upstream down")
        # A stale copy exists → best-effort serves it (True), no raise.
        assert await svc.ensure_pricesheet(storage) is True


async def test_ensure_returns_false_when_no_copy_and_fetch_fails():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=False)
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        refresh.side_effect = RuntimeError("upstream down")
        assert await svc.ensure_pricesheet(storage) is False
