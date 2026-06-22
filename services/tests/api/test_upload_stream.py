"""Unit tests for the shared upload-streaming helpers (CLAUDE.md #13/#14).

These back the parse-requiring upload handlers (runner state, manual state,
plan-JSON, module tarball) that stream the request body to a capped tempfile
on the ephemeral PVC instead of buffering it in the worker heap.
"""

import os

import pytest
from fastapi import HTTPException

from terrapod.api.upload_stream import (
    DEFAULT_UPLOAD_MAX_BYTES,
    file_chunks,
    stream_to_tempfile,
)


class _FakeRequest:
    """Minimal stand-in for starlette.Request: headers + an async stream()."""

    def __init__(self, chunks: list[bytes], content_length: str | None = None):
        self._chunks = chunks
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length

    async def stream(self):
        for c in self._chunks:
            yield c


@pytest.mark.asyncio
async def test_streams_body_to_tempfile_and_reports_size():
    body = b'{"serial": 1, "lineage": "abc"}'
    req = _FakeRequest([body[:10], body[10:]], content_length=str(len(body)))

    path, size = await stream_to_tempfile(req, suffix=".state.json")
    try:
        assert size == len(body)
        assert os.path.isfile(path)
        with open(path, "rb") as fh:
            assert fh.read() == body
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_content_length_precheck_rejects_oversized():
    req = _FakeRequest([b"x"], content_length=str(DEFAULT_UPLOAD_MAX_BYTES + 1))
    with pytest.raises(HTTPException) as exc:
        await stream_to_tempfile(req, suffix=".bin")
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_streamed_cap_rejects_and_cleans_up_when_content_length_lies():
    # No Content-Length header (chunked-encoding style) → the pre-check can't
    # fire; the streamed enforcement must catch it AND unlink the tempfile so
    # the PVC never leaks a partial upload.
    captured: dict[str, str] = {}

    real_mkstemp = __import__("tempfile").mkstemp

    def _spy_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        captured["path"] = path
        return fd, path

    import tempfile as _tempfile

    orig = _tempfile.mkstemp
    _tempfile.mkstemp = _spy_mkstemp
    try:
        req = _FakeRequest([b"a" * 1024, b"b" * 1024])
        with pytest.raises(HTTPException) as exc:
            await stream_to_tempfile(req, suffix=".bin", max_bytes=1500)
        assert exc.value.status_code == 413
    finally:
        _tempfile.mkstemp = orig

    # The tempfile that was opened mid-stream must have been removed.
    assert "path" in captured
    assert not os.path.exists(captured["path"])


@pytest.mark.asyncio
async def test_empty_body_yields_zero_size_tempfile():
    req = _FakeRequest([], content_length="0")
    path, size = await stream_to_tempfile(req, suffix=".bin")
    try:
        assert size == 0
        assert os.path.getsize(path) == 0
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_file_chunks_yields_full_content_in_order():
    body = b"0123456789" * 300_000  # ~3 MB, multiple 1 MiB reads
    req = _FakeRequest([body])
    path, _ = await stream_to_tempfile(req, suffix=".bin")
    try:
        out = bytearray()
        async for chunk in file_chunks(path, chunk_size=1024 * 1024):
            out.extend(chunk)
        assert bytes(out) == body
    finally:
        os.unlink(path)
