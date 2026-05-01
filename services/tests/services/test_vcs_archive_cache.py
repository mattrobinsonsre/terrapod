"""Tests for VCSArchiveCache — single-flight + storage caching of VCS tarballs.

Today's cache delegates the actual VCS fetch to
``git_fetch.sparse_archive_to_storage``. These tests mock that call —
the dulwich-against-real-providers integration test happens in Tilt.
"""

import asyncio
import io
import os
import tarfile
import time as time_mod
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.archive_utils import strip_archive_top_level_dir_file
from terrapod.services.vcs_archive_cache import (
    _CLONE_DIR_PREFIX,
    VCSArchiveCache,
    _ensure_tmpdir_space,
    materialize_archive,
)
from terrapod.storage.protocol import ObjectNotFoundError


def _mock_conn(provider="github", id_=None):
    conn = MagicMock()
    conn.id = id_ or uuid.uuid4()
    conn.provider = provider
    conn.status = "active"
    return conn


def _make_tarball(members: dict[str, bytes]) -> bytes:
    """Build a gzipped tar with the given {path: content} members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in members.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _list_tarball_members(data: bytes) -> dict[str, bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            f = tf.extractfile(member)
            out[member.name] = f.read() if f else b""
    return out


# ── strip_archive_top_level_dir_file (registry path, still used) ───────


class TestStripArchiveTopLevelDirFile:
    """The strip helper is no longer on the VCS hot path but is still
    used by the registry-module download flow. Keep coverage to detect
    regressions in that path.
    """

    def test_strips_top_level_directory(self, tmp_path):
        src = tmp_path / "in.tar.gz"
        dst = tmp_path / "out.tar.gz"
        src.write_bytes(
            _make_tarball(
                {
                    "wrapper-abc123/": b"",
                    "wrapper-abc123/main.tf": b"resource {}\n",
                    "wrapper-abc123/sub/vars.tf": b"variable {}\n",
                }
            )
        )

        strip_archive_top_level_dir_file(str(src), str(dst))
        members = _list_tarball_members(dst.read_bytes())

        assert "wrapper-abc123" not in members
        assert "main.tf" in members
        assert members["main.tf"] == b"resource {}\n"
        assert "sub/vars.tf" in members

    def test_handles_empty_archive(self, tmp_path):
        src = tmp_path / "in.tar.gz"
        dst = tmp_path / "out.tar.gz"
        src.write_bytes(_make_tarball({}))

        strip_archive_top_level_dir_file(str(src), str(dst))
        assert _list_tarball_members(dst.read_bytes()) == {}


# ── VCSArchiveCache: storage cache hit ─────────────────────────────────


class TestVCSArchiveCacheStorageHit:
    @pytest.mark.asyncio
    async def test_storage_cache_hit_skips_fetch(self):
        """If head() returns OK, we don't invoke the dulwich fetch."""
        cache = VCSArchiveCache()
        conn = _mock_conn()

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache.VCSArchiveCache._fetch_and_upload",
                new_callable=AsyncMock,
            ) as mock_fetch,
        ):
            key = await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        assert key.startswith("vcs_archives/")
        assert "abc123" in key
        # Default paths_hash is "full" for whole-repo
        assert key.endswith("-full.tar.gz")
        mock_storage.head.assert_awaited_once()
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_process_dict_caches_across_calls(self):
        """A second call for the same (conn, sha, paths) skips even head()."""
        cache = VCSArchiveCache()
        conn = _mock_conn()

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(return_value=MagicMock())

        with patch(
            "terrapod.services.vcs_archive_cache.get_storage",
            return_value=mock_storage,
        ):
            key1 = await cache.get_or_fetch(conn, "owner", "repo", "abc123")
            key2 = await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        assert key1 == key2
        mock_storage.head.assert_awaited_once()


# ── VCSArchiveCache: cache miss → fetch → upload ───────────────────────


class TestVCSArchiveCacheMiss:
    @pytest.mark.asyncio
    async def test_miss_invokes_sparse_archive(self, tmp_path):
        """On head() miss, the cache calls git_fetch.sparse_archive_to_storage."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))

        sparse_mock = AsyncMock(return_value=12345)

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache._resolve_tmpdir",
                return_value=str(tmp_path),
            ),
            patch(
                "terrapod.services.git_fetch.sparse_archive_to_storage",
                sparse_mock,
            ),
        ):
            key = await cache.get_or_fetch(conn, "owner", "repo", "abc123", paths=["infra/eks"])

        assert key.startswith("vcs_archives/")
        assert "abc123" in key
        assert not key.endswith("-full.tar.gz")  # narrowed
        sparse_mock.assert_awaited_once()
        # paths is positional arg 4 (conn, owner, repo, sha, paths, storage_key, clone_dir=...)
        call_args = sparse_mock.call_args
        assert call_args.args[4] == ["infra/eks"]

    @pytest.mark.asyncio
    async def test_clone_dir_cleaned_up_on_success(self, tmp_path):
        """The vcs-clone-* parent dir is rmtree'd after a successful fetch."""
        cache = VCSArchiveCache()
        conn = _mock_conn()

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))

        async def fake_sparse(_c, _o, _r, _s, _p, _k, *, clone_dir):
            # Verify the clone_dir was actually created with the prefix
            assert os.path.isdir(clone_dir)
            assert os.path.basename(clone_dir).startswith(_CLONE_DIR_PREFIX)
            return 42

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache._resolve_tmpdir",
                return_value=str(tmp_path),
            ),
            patch(
                "terrapod.services.git_fetch.sparse_archive_to_storage",
                side_effect=fake_sparse,
            ),
        ):
            await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        # Check no leftover clone dirs in tmp_path
        leftover = [n for n in os.listdir(tmp_path) if n.startswith(_CLONE_DIR_PREFIX)]
        assert leftover == []


# ── VCSArchiveCache: paths hashing & cache key ─────────────────────────


class TestVCSArchiveCachePathsHashing:
    @pytest.mark.asyncio
    async def test_different_paths_produce_different_keys(self):
        """Two callers with different path sets get different cache entries."""
        cache = VCSArchiveCache()
        conn = _mock_conn(id_=uuid.uuid4())

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(return_value=MagicMock())

        with patch(
            "terrapod.services.vcs_archive_cache.get_storage",
            return_value=mock_storage,
        ):
            k1 = await cache.get_or_fetch(conn, "o", "r", "sha", paths=["a"])
            k2 = await cache.get_or_fetch(conn, "o", "r", "sha", paths=["b"])
            k_full = await cache.get_or_fetch(conn, "o", "r", "sha")

        assert k1 != k2
        assert k1 != k_full
        assert k2 != k_full
        assert k_full.endswith("-full.tar.gz")

    @pytest.mark.asyncio
    async def test_same_paths_share_cache_entry_regardless_of_order(self):
        """Path order and duplicates don't affect the cache key — `normalize_paths`
        sorts and dedupes before hashing."""
        cache = VCSArchiveCache()
        conn = _mock_conn(id_=uuid.uuid4())

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(return_value=MagicMock())

        with patch(
            "terrapod.services.vcs_archive_cache.get_storage",
            return_value=mock_storage,
        ):
            k1 = await cache.get_or_fetch(conn, "o", "r", "sha", paths=["b", "a", "a"])
            k2 = await cache.get_or_fetch(conn, "o", "r", "sha", paths=["a", "b"])

        assert k1 == k2
        # head should fire only once — the second call hits the in-memory
        # short-circuit on equal cache keys.
        mock_storage.head.assert_awaited_once()


# ── VCSArchiveCache: single-flight under concurrency ───────────────────


class TestVCSArchiveCacheSingleFlight:
    @pytest.mark.asyncio
    async def test_concurrent_get_or_fetch_coalesce_one_fetch(self, tmp_path):
        """Five concurrent callers for the same (conn, sha, paths) trigger one fetch."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))

        fetch_calls = 0
        fetch_started = asyncio.Event()
        fetch_can_finish = asyncio.Event()

        async def slow_fetch(*_args, **_kwargs):
            nonlocal fetch_calls
            fetch_calls += 1
            fetch_started.set()
            await fetch_can_finish.wait()
            return 100

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache._resolve_tmpdir",
                return_value=str(tmp_path),
            ),
            patch(
                "terrapod.services.git_fetch.sparse_archive_to_storage",
                side_effect=slow_fetch,
            ),
        ):
            tasks = [
                asyncio.create_task(cache.get_or_fetch(conn, "owner", "repo", "abc123"))
                for _ in range(5)
            ]
            await fetch_started.wait()
            await asyncio.sleep(0.05)
            fetch_can_finish.set()
            keys = await asyncio.gather(*tasks)

        assert fetch_calls == 1
        assert len(set(keys)) == 1


# ── VCSArchiveCache: partial-failure cleanup ───────────────────────────


class TestVCSArchiveCachePartialUploadCleanup:
    @pytest.mark.asyncio
    async def test_fetch_failure_deletes_partial_cache_entry(self, tmp_path):
        """If sparse_archive_to_storage raises mid-upload, the cache key is
        deleted so a future head() won't return OK on a truncated tarball."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.delete = AsyncMock()

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache._resolve_tmpdir",
                return_value=str(tmp_path),
            ),
            patch(
                "terrapod.services.git_fetch.sparse_archive_to_storage",
                side_effect=RuntimeError("upload died mid-stream"),
            ),
        ):
            with pytest.raises(RuntimeError, match="upload died"):
                await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        mock_storage.delete.assert_awaited_once()
        deleted_key = mock_storage.delete.call_args.args[0]
        assert deleted_key.startswith("vcs_archives/")
        assert "abc123" in deleted_key

    @pytest.mark.asyncio
    async def test_failure_does_not_pollute_in_memory_cache(self, tmp_path):
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.delete = AsyncMock()

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache._resolve_tmpdir",
                return_value=str(tmp_path),
            ),
            patch(
                "terrapod.services.git_fetch.sparse_archive_to_storage",
                side_effect=RuntimeError("boom"),
            ),
        ):
            with pytest.raises(RuntimeError):
                await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        # Whatever specific cache_key was written must NOT be in the dict.
        assert all("abc123" not in k for k in cache._known)


# ── _ensure_tmpdir_space ───────────────────────────────────────────────


class TestEnsureTmpdirSpace:
    """`_ensure_tmpdir_space` evicts old orphans (tarballs + clone dirs)
    when free space is below threshold."""

    def test_no_sweep_when_above_threshold(self, tmp_path):
        keep = tmp_path / "ancient.raw.tar.gz"
        keep.write_bytes(b"x" * 1024)
        old_mtime = time_mod.time() - 10_000
        os.utime(keep, (old_mtime, old_mtime))

        with patch("terrapod.services.vcs_archive_cache._free_bytes", return_value=10 * 1024**3):
            _ensure_tmpdir_space(str(tmp_path))

        assert keep.exists()

    def test_sweeps_oldest_orphan_tarballs(self, tmp_path):
        old1 = tmp_path / "old1.raw.tar.gz"
        old2 = tmp_path / "old2.stripped.tar.gz"
        new_in_use = tmp_path / "fresh.raw.tar.gz"
        unrelated = tmp_path / "DO_NOT_TOUCH.txt"

        for p in (old1, old2, new_in_use, unrelated):
            p.write_bytes(b"x" * 1024)

        cutoff = time_mod.time() - 600
        os.utime(old1, (cutoff, cutoff))
        os.utime(old2, (cutoff + 1, cutoff + 1))

        with patch("terrapod.services.vcs_archive_cache._free_bytes", return_value=1024):
            _ensure_tmpdir_space(str(tmp_path))

        assert not old1.exists()
        assert not old2.exists()
        assert new_in_use.exists()
        assert unrelated.exists()

    def test_sweeps_orphan_clone_dirs(self, tmp_path):
        """Clone directories left behind by an aborted fetch get rmtree'd."""
        old_clone = tmp_path / f"{_CLONE_DIR_PREFIX}deadbeef"
        old_clone.mkdir()
        (old_clone / "objects").mkdir()
        (old_clone / "objects" / "pack.idx").write_bytes(b"x" * 1024)

        # Mark old (past orphan age)
        cutoff = time_mod.time() - 600
        os.utime(old_clone, (cutoff, cutoff))

        # Also drop an unrelated dir that must NOT be touched
        unrelated_dir = tmp_path / "some-other-thing"
        unrelated_dir.mkdir()
        (unrelated_dir / "f").write_bytes(b"x")
        os.utime(unrelated_dir, (cutoff, cutoff))

        with patch("terrapod.services.vcs_archive_cache._free_bytes", return_value=1024):
            _ensure_tmpdir_space(str(tmp_path))

        assert not old_clone.exists()
        assert unrelated_dir.exists()

    def test_skips_when_tmpdir_is_none(self):
        with patch("terrapod.services.vcs_archive_cache._free_bytes") as mock_free:
            _ensure_tmpdir_space(None)
        mock_free.assert_not_called()

    def test_does_not_delete_files_younger_than_orphan_age(self, tmp_path):
        recent = tmp_path / "recent.raw.tar.gz"
        recent.write_bytes(b"x" * 1024)

        with patch("terrapod.services.vcs_archive_cache._free_bytes", return_value=1024):
            _ensure_tmpdir_space(str(tmp_path))

        assert recent.exists()


# ── materialize_archive ─────────────────────────────────────────────────


class TestMaterializeArchive:
    @pytest.mark.asyncio
    async def test_streams_storage_key_to_local_temp_file(self, tmp_path):
        """The yielded path contains exactly the bytes streamed from storage.

        Production storage backends implement `get_stream` as an async
        generator; the mock matches that shape (NOT AsyncMock(return_value=...)).
        """
        payload = _make_tarball({"a.tf": b"a", "b.tf": b"bb"})

        async def get_stream(_key):
            yield payload[: len(payload) // 2]
            yield payload[len(payload) // 2 :]

        mock_storage = MagicMock()
        mock_storage.get_stream = get_stream

        observed: dict[str, bytes | bool | str] = {}

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache._resolve_tmpdir",
                return_value=str(tmp_path),
            ),
        ):
            async with materialize_archive("vcs_archives/foo.tar.gz") as path:
                with open(path, "rb") as f:
                    observed["bytes"] = f.read()
                observed["existed"] = os.path.exists(path)
                observed["path"] = path

        assert observed["existed"]
        assert observed["bytes"] == payload
        assert not os.path.exists(observed["path"])

    @pytest.mark.asyncio
    async def test_unlinks_temp_file_even_when_consumer_raises(self, tmp_path):
        async def get_stream(_key):
            yield b"some bytes"

        mock_storage = MagicMock()
        mock_storage.get_stream = get_stream

        observed_path: str | None = None
        observed_error: BaseException | None = None

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache._resolve_tmpdir",
                return_value=str(tmp_path),
            ),
        ):
            try:
                async with materialize_archive("vcs_archives/foo.tar.gz") as path:
                    observed_path = path
                    raise RuntimeError("caller blew up")
            except RuntimeError as e:
                observed_error = e

        assert observed_path is not None
        assert observed_error is not None
        assert "caller blew up" in str(observed_error)
        assert not os.path.exists(observed_path)
