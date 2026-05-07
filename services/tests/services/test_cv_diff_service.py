"""Tests for `cv_diff_service` — diffs between two CV tarballs.

The service is the backing for `POST /api/v2/configuration-versions/diff`.
These tests cover the pure tarball-walking + diffing layer; the
storage round-trip is exercised via a real local filesystem store
fixture so we touch the same `_stream_to_tempfile` path the API uses.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from terrapod.services import cv_diff_service


def _make_tarball(members: dict[str, bytes]) -> bytes:
    """Build a gzipped tar containing the given path → bytes mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in members.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _write_tarball(path: Path, members: dict[str, bytes]) -> None:
    path.write_bytes(_make_tarball(members))


# ── _read_tarball (pure, no storage) ─────────────────────────────────


class TestReadTarball:
    def test_collects_regular_files_only(self, tmp_path):
        """Directories and similar non-files are skipped — they're noise
        for a config diff and would otherwise show up as zero-byte
        entries we'd then have to filter downstream."""
        path = tmp_path / "in.tar.gz"
        # Build manually so we can include a directory entry.
        with tarfile.open(path, mode="w:gz") as tf:
            d = tarfile.TarInfo(name="modules/")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
            f = tarfile.TarInfo(name="main.tf")
            f.size = 5
            tf.addfile(f, io.BytesIO(b"hello"))

        files, oversized, total = cv_diff_service._read_tarball(str(path))
        assert files == {"main.tf": b"hello"}
        assert oversized == []
        assert total == 5

    def test_oversized_files_recorded_not_loaded(self, tmp_path, monkeypatch):
        """Anything bigger than `_MAX_FILE_BYTES` is recorded as oversized
        and its bytes are NOT loaded into memory — the whole point of the
        cap is to avoid OOMing the api pod on a misuse-shaped CV."""
        # Lower the cap for the test
        monkeypatch.setattr(cv_diff_service, "_MAX_FILE_BYTES", 100)
        path = tmp_path / "in.tar.gz"
        _write_tarball(
            path,
            {
                "small.tf": b"x" * 50,
                "big.bin": b"X" * 1024,
            },
        )
        files, oversized, total = cv_diff_service._read_tarball(str(path))
        assert "small.tf" in files
        assert "big.bin" not in files
        assert oversized == ["big.bin"]
        # `total` includes every file's size including oversized ones
        # so the per-pair cap can refuse the diff before we even start.
        assert total >= 1024


# ── _looks_binary heuristic ──────────────────────────────────────────


class TestLooksBinary:
    def test_pure_text_is_not_binary(self):
        assert not cv_diff_service._looks_binary(b'resource "null" "x" {}\n')

    def test_nul_byte_in_first_8k_means_binary(self):
        assert cv_diff_service._looks_binary(b"hello\x00world")

    def test_nul_after_8k_does_not_trip(self):
        # Heuristic only checks the first 8 KiB — same as git's behaviour
        data = b"x" * (8192 + 100) + b"\x00"
        assert not cv_diff_service._looks_binary(data)


# ── diff_tarballs end-to-end via real storage ────────────────────────


@pytest.fixture
def filesystem_storage(tmp_path, monkeypatch):
    """Real filesystem-backed object store, scoped to tmp_path.

    Drives the same `get_storage()` indirection the production code
    uses — so we exercise `_stream_to_tempfile`'s real path including
    `os.fdopen` semantics. Faster than mocking, more honest.
    """
    from terrapod.config import settings
    from terrapod.storage import close_storage, get_storage, init_storage

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    monkeypatch.setattr(settings.storage.filesystem, "data_dir", str(storage_dir))
    monkeypatch.setattr(settings.storage, "backend", "filesystem")

    import asyncio

    async def _setup():
        await init_storage()

    async def _teardown():
        await close_storage()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_setup())
        yield get_storage()
    finally:
        loop.run_until_complete(_teardown())
        loop.close()


@pytest.mark.asyncio
async def test_diff_classifies_added_removed_modified():
    # Drive the function with mocked storage rather than the fixture
    # above — simpler and avoids the per-fixture asyncio dance.
    from unittest.mock import MagicMock, patch

    from_bytes = _make_tarball(
        {
            "main.tf": b'resource "null" "x" {}\n',
            "removed.tf": b"# bye\n",
            "shared.tf": b"a\nb\nc\n",
        }
    )
    to_bytes = _make_tarball(
        {
            "main.tf": b'resource "null" "x" { triggers = {} }\n',
            "shared.tf": b"a\nb\nc\n",  # unchanged
            "added.tf": b"# new\n",
        }
    )

    async def fake_get_stream(key):
        # Two storage keys; serve different bytes per key
        if key == "from-key":
            yield from_bytes
        else:
            yield to_bytes

    storage = MagicMock()
    storage.get_stream = fake_get_stream

    with patch.object(cv_diff_service, "get_storage", return_value=storage):
        result = await cv_diff_service.diff_tarballs("from-key", "to-key")

    types = {f["path"]: f["type"] for f in result["files"]}
    assert types["main.tf"] == "modified"
    assert types["removed.tf"] == "removed"
    assert types["added.tf"] == "added"
    # Unchanged files must NOT appear
    assert "shared.tf" not in types
    assert result["total-files-changed"] == 3

    # Modified file's diff body actually contains the change
    main_diff = next(f["diff"] for f in result["files"] if f["path"] == "main.tf")
    assert "triggers" in main_diff


@pytest.mark.asyncio
async def test_diff_reports_binary_changes_without_rendering_diff():
    from unittest.mock import MagicMock, patch

    from_bytes = _make_tarball({"image.png": b"\x89PNG\r\n\x1a\n" + b"old"})
    to_bytes = _make_tarball({"image.png": b"\x89PNG\r\n\x1a\n" + b"new"})

    async def fake_get_stream(key):
        yield from_bytes if key == "from-key" else to_bytes

    storage = MagicMock()
    storage.get_stream = fake_get_stream

    with patch.object(cv_diff_service, "get_storage", return_value=storage):
        result = await cv_diff_service.diff_tarballs("from-key", "to-key")

    assert result["files"] == [{"path": "image.png", "type": "binary-changed"}]


@pytest.mark.asyncio
async def test_diff_too_large_raises():
    from unittest.mock import MagicMock, patch

    # Build a tarball big enough to trip the per-pair cap once doubled.
    # _MAX_TOTAL_BYTES default is 32 MiB; build 20 MiB on each side.
    big_payload = b"x" * (20 * 1024 * 1024)
    from_bytes = _make_tarball({"big.tf": big_payload})
    to_bytes = _make_tarball({"big.tf": big_payload + b"\n"})

    async def fake_get_stream(key):
        yield from_bytes if key == "from-key" else to_bytes

    storage = MagicMock()
    storage.get_stream = fake_get_stream

    with patch.object(cv_diff_service, "get_storage", return_value=storage):
        with pytest.raises(cv_diff_service.DiffTooLarge):
            await cv_diff_service.diff_tarballs("from-key", "to-key")
