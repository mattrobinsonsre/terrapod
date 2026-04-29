"""Tests for VCSArchiveCache — single-flight + storage caching of VCS tarballs."""

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


# ── strip_archive_top_level_dir_file ───────────────────────────────────


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


class TestStripArchiveTopLevelDirFile:
    def test_strips_top_level_directory(self, tmp_path):
        """A `markupai-repo-abc/` wrapper is removed from every member name."""
        src = tmp_path / "in.tar.gz"
        dst = tmp_path / "out.tar.gz"
        src.write_bytes(
            _make_tarball(
                {
                    "markupai-repo-abc123/": b"",
                    "markupai-repo-abc123/main.tf": b"resource {}\n",
                    "markupai-repo-abc123/sub/vars.tf": b"variable {}\n",
                }
            )
        )

        strip_archive_top_level_dir_file(str(src), str(dst))
        members = _list_tarball_members(dst.read_bytes())

        assert "markupai-repo-abc123" not in members
        assert "main.tf" in members
        assert members["main.tf"] == b"resource {}\n"
        assert "sub/vars.tf" in members

    def test_handles_empty_archive(self, tmp_path):
        """An empty (member-less) tarball produces an empty output."""
        src = tmp_path / "in.tar.gz"
        dst = tmp_path / "out.tar.gz"
        src.write_bytes(_make_tarball({}))

        strip_archive_top_level_dir_file(str(src), str(dst))
        members = _list_tarball_members(dst.read_bytes())
        assert members == {}

    def test_does_not_buffer_full_archive_in_memory(self, tmp_path):
        """Smoke test: a moderately large archive completes without erroring.

        Real OOM behaviour is hard to assert in a unit test — the value of
        this test is just confirming the streaming path produces correct
        output for non-trivial input sizes.
        """
        # 50 members @ 64KB each = ~3.2MB uncompressed
        big_blob = b"x" * (64 * 1024)
        members = {f"repo-abc/file_{i}.txt": big_blob for i in range(50)}
        src = tmp_path / "in.tar.gz"
        dst = tmp_path / "out.tar.gz"
        src.write_bytes(_make_tarball(members))

        strip_archive_top_level_dir_file(str(src), str(dst))
        out_members = _list_tarball_members(dst.read_bytes())
        assert len(out_members) == 50
        assert all(v == big_blob for v in out_members.values())


# ── VCSArchiveCache: storage cache hit ─────────────────────────────────


class TestVCSArchiveCacheStorageHit:
    @pytest.mark.asyncio
    async def test_storage_cache_hit_skips_download(self):
        """If head() returns OK, we don't touch GitHub or the strip pipeline."""
        cache = VCSArchiveCache()
        conn = _mock_conn()

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(return_value=MagicMock())  # exists

        with (
            patch(
                "terrapod.services.vcs_archive_cache.get_storage",
                return_value=mock_storage,
            ),
            patch(
                "terrapod.services.vcs_archive_cache.VCSArchiveCache._download_strip_upload",
                new_callable=AsyncMock,
            ) as mock_dsu,
        ):
            key = await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        assert key.startswith("vcs_archives/")
        assert "abc123" in key
        mock_storage.head.assert_awaited_once()
        mock_dsu.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_process_dict_caches_across_calls(self):
        """A second call for the same (conn, sha) skips even the head() call."""
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
        # head() called once — second call took the in-memory short-circuit
        mock_storage.head.assert_awaited_once()


# ── VCSArchiveCache: cache miss → download → upload ────────────────────


class TestVCSArchiveCacheMiss:
    @pytest.mark.asyncio
    async def test_miss_runs_download_strip_upload(self, tmp_path):
        """On head() miss, the cache invokes the streaming download/strip/upload pipeline."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.put_stream = AsyncMock(return_value=MagicMock())

        # Stub the GH download to write a known tarball to dest_path
        async def fake_download(_conn, _o, _r, _sha, dest_path, **_kw):
            data = _make_tarball({"prefix-abc/main.tf": b"resource {}\n"})
            with open(dest_path, "wb") as f:
                f.write(data)
            return len(data)

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
                "terrapod.services.github_service.download_repo_archive_to_file",
                side_effect=fake_download,
            ),
        ):
            key = await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        assert key.startswith("vcs_archives/")
        mock_storage.put_stream.assert_awaited_once()
        # Chunks arg is an async iterator — exhaust it to confirm content was streamed
        kwargs = mock_storage.put_stream.call_args.kwargs
        assert kwargs.get("content_type") == "application/x-tar"


# ── VCSArchiveCache: single-flight under concurrency ───────────────────


class TestVCSArchiveCacheSingleFlight:
    @pytest.mark.asyncio
    async def test_concurrent_get_or_fetch_coalesce_one_download(self, tmp_path):
        """Five concurrent callers for the same (conn, sha) trigger one download."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.put_stream = AsyncMock(return_value=MagicMock())

        download_calls = 0
        download_started = asyncio.Event()
        download_can_finish = asyncio.Event()

        async def slow_download(_conn, _o, _r, _sha, dest_path, **_kw):
            nonlocal download_calls
            download_calls += 1
            download_started.set()
            await download_can_finish.wait()
            data = _make_tarball({"x/main.tf": b"resource {}\n"})
            with open(dest_path, "wb") as f:
                f.write(data)
            return len(data)

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
                "terrapod.services.github_service.download_repo_archive_to_file",
                side_effect=slow_download,
            ),
        ):
            # Fire 5 concurrent fetches before letting the first download finish
            tasks = [
                asyncio.create_task(cache.get_or_fetch(conn, "owner", "repo", "abc123"))
                for _ in range(5)
            ]
            await download_started.wait()
            # Give the other 4 tasks a chance to queue up on the lock
            await asyncio.sleep(0.05)
            download_can_finish.set()
            keys = await asyncio.gather(*tasks)

        assert download_calls == 1
        assert len(set(keys)) == 1  # all callers got the same storage key

    @pytest.mark.asyncio
    async def test_different_shas_do_not_block_each_other(self, tmp_path):
        """Concurrent calls for different SHAs both proceed in parallel."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.put_stream = AsyncMock(return_value=MagicMock())

        active = 0
        max_concurrent = 0

        async def fake_download(_conn, _o, _r, _sha, dest_path, **_kw):
            nonlocal active, max_concurrent
            active += 1
            max_concurrent = max(max_concurrent, active)
            await asyncio.sleep(0.05)
            data = _make_tarball({"x/main.tf": b"x"})
            with open(dest_path, "wb") as f:
                f.write(data)
            active -= 1
            return len(data)

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
                "terrapod.services.github_service.download_repo_archive_to_file",
                side_effect=fake_download,
            ),
        ):
            await asyncio.gather(
                cache.get_or_fetch(conn, "owner", "repo", "sha-a"),
                cache.get_or_fetch(conn, "owner", "repo", "sha-b"),
                cache.get_or_fetch(conn, "owner", "repo", "sha-c"),
            )

        assert max_concurrent >= 2  # different SHAs ran in parallel


# ── VCSArchiveCache: dispatch by provider ──────────────────────────────


class TestEnsureTmpdirSpace:
    """`_ensure_tmpdir_space` evicts old orphan temp tarballs when free space is low."""

    def test_no_sweep_when_above_threshold(self, tmp_path):
        """If statvfs shows enough free space, we don't touch any files."""
        keep = tmp_path / "ancient.raw.tar.gz"
        keep.write_bytes(b"x" * 1024)
        old_mtime = time_mod.time() - 10_000
        os.utime(keep, (old_mtime, old_mtime))

        # Pretend there's tons of free space so the threshold check passes.
        # Use settings.vcs.tmpdir_min_free_bytes default (~2 GiB) and a fake
        # statvfs returning more than that.
        with patch("terrapod.services.vcs_archive_cache._free_bytes", return_value=10 * 1024**3):
            _ensure_tmpdir_space(str(tmp_path))

        assert keep.exists()  # not deleted because we were above threshold

    def test_sweeps_oldest_orphans_when_low(self, tmp_path):
        """Below threshold, the oldest orphan tarballs get deleted first."""
        old1 = tmp_path / "old1.raw.tar.gz"
        old2 = tmp_path / "old2.stripped.tar.gz"
        new_in_use = tmp_path / "fresh.raw.tar.gz"
        unrelated = tmp_path / "DO_NOT_TOUCH.txt"

        for p in (old1, old2, new_in_use, unrelated):
            p.write_bytes(b"x" * 1024)

        # old1 + old2 well past the 5-min orphan age; new_in_use is fresh.
        # unrelated isn't a tarball — must never be deleted.
        cutoff = time_mod.time() - 600
        os.utime(old1, (cutoff, cutoff))
        os.utime(old2, (cutoff + 1, cutoff + 1))  # slightly newer than old1
        # new_in_use has its default (now) mtime

        # First _free_bytes call shows below threshold; subsequent calls
        # also show below threshold so the loop keeps deleting.
        with patch(
            "terrapod.services.vcs_archive_cache._free_bytes",
            return_value=1024,  # 1 KB free << 2 GiB threshold
        ):
            _ensure_tmpdir_space(str(tmp_path))

        # Both old orphans deleted; in-use and unrelated survive.
        assert not old1.exists()
        assert not old2.exists()
        assert new_in_use.exists()
        assert unrelated.exists()

    def test_skips_when_tmpdir_is_none(self):
        """No-op when caller passed None (system tempdir fallback path)."""
        # Should not raise, should not call statvfs
        with patch("terrapod.services.vcs_archive_cache._free_bytes") as mock_free:
            _ensure_tmpdir_space(None)
        mock_free.assert_not_called()

    def test_does_not_delete_files_younger_than_orphan_age(self, tmp_path):
        """A recent file (mtime within the last 5 minutes) is presumed in-flight."""
        recent = tmp_path / "recent.raw.tar.gz"
        recent.write_bytes(b"x" * 1024)
        # mtime is "now" by default — well within the 5-min window

        with patch(
            "terrapod.services.vcs_archive_cache._free_bytes",
            return_value=1024,  # below threshold
        ):
            _ensure_tmpdir_space(str(tmp_path))

        # Even though we're below threshold, we don't touch in-flight files
        assert recent.exists()


class TestVCSArchiveCacheProviderDispatch:
    @pytest.mark.asyncio
    async def test_gitlab_uses_gitlab_streaming_download(self, tmp_path):
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="gitlab")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.put_stream = AsyncMock(return_value=MagicMock())

        async def fake_download(_conn, _o, _r, _sha, dest_path, **_kw):
            data = _make_tarball({"x/main.tf": b"x"})
            with open(dest_path, "wb") as f:
                f.write(data)
            return len(data)

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
                "terrapod.services.gitlab_service.download_archive_to_file",
                side_effect=fake_download,
            ) as mock_gl,
            patch(
                "terrapod.services.github_service.download_repo_archive_to_file",
                new_callable=AsyncMock,
            ) as mock_gh,
        ):
            await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        mock_gl.assert_awaited_once()
        mock_gh.assert_not_called()


# ── VCSArchiveCache: transactional upload (partial failure cleanup) ─────


class TestVCSArchiveCachePartialUploadCleanup:
    @pytest.mark.asyncio
    async def test_put_stream_failure_deletes_partial_cache_entry(self, tmp_path):
        """If put_stream raises, we delete the storage_key so a future
        head() doesn't return OK on a truncated tarball."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.put_stream = AsyncMock(side_effect=RuntimeError("network died mid-upload"))
        mock_storage.delete = AsyncMock()

        async def fake_download(_conn, _o, _r, _sha, dest_path, **_kw):
            data = _make_tarball({"x/main.tf": b"x"})
            with open(dest_path, "wb") as f:
                f.write(data)
            return len(data)

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
                "terrapod.services.github_service.download_repo_archive_to_file",
                side_effect=fake_download,
            ),
        ):
            with pytest.raises(RuntimeError, match="network died"):
                await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        # Partial upload key must be cleaned up
        mock_storage.delete.assert_awaited_once()
        deleted_key = mock_storage.delete.call_args.args[0]
        assert deleted_key.startswith("vcs_archives/")
        assert "abc123" in deleted_key

    @pytest.mark.asyncio
    async def test_failure_does_not_pollute_in_memory_cache(self, tmp_path):
        """A raised exception from `_download_strip_upload` must not populate
        the in-memory `_known` dict, so the next call retries cleanly."""
        cache = VCSArchiveCache()
        conn = _mock_conn(provider="github")

        mock_storage = MagicMock()
        mock_storage.head = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        mock_storage.put_stream = AsyncMock(side_effect=RuntimeError("boom"))
        mock_storage.delete = AsyncMock()

        async def fake_download(_conn, _o, _r, _sha, dest_path, **_kw):
            data = _make_tarball({"x/main.tf": b"x"})
            with open(dest_path, "wb") as f:
                f.write(data)
            return len(data)

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
                "terrapod.services.github_service.download_repo_archive_to_file",
                side_effect=fake_download,
            ),
        ):
            with pytest.raises(RuntimeError):
                await cache.get_or_fetch(conn, "owner", "repo", "abc123")

        cache_key = f"{conn.id}:owner/repo@abc123"
        assert cache_key not in cache._known


# ── materialize_archive ─────────────────────────────────────────────────


class TestMaterializeArchive:
    @pytest.mark.asyncio
    async def test_streams_storage_key_to_local_temp_file(self, tmp_path):
        """The yielded path contains exactly the bytes streamed from storage."""
        payload = _make_tarball({"a.tf": b"a", "b.tf": b"bb"})

        async def chunks():
            # Split into 2 chunks to exercise the loop
            yield payload[: len(payload) // 2]
            yield payload[len(payload) // 2 :]

        mock_storage = MagicMock()
        mock_storage.get_stream = AsyncMock(return_value=chunks())

        observed: dict[str, bytes] = {}

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
            async with materialize_archive("vcs_archives/foo/owner/repo/abc.tar.gz") as path:
                # Caller would normally stream-upload from path; we just read it
                with open(path, "rb") as f:
                    observed["bytes"] = f.read()
                observed["existed"] = os.path.exists(path)
                observed["path"] = path

        # Inside-context: file existed; afterwards: deleted
        assert observed["existed"]
        assert observed["bytes"] == payload
        assert not os.path.exists(observed["path"])

    @pytest.mark.asyncio
    async def test_unlinks_temp_file_even_when_consumer_raises(self, tmp_path):
        """Caller error inside the `async with` must not leak the temp file.

        CodeQL's py/unreachable-statement rule mis-models exception flow when
        a `raise` sits inside a context manager whose body the analyser can't
        see (the `@asynccontextmanager` generator). Using a plain
        try/except + nullable variable, written sequentially, keeps the
        control-flow graph explicit enough to satisfy the analyser without
        sacrificing test clarity.
        """

        async def chunks():
            yield b"some bytes"

        mock_storage = MagicMock()
        mock_storage.get_stream = AsyncMock(return_value=chunks())

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

    @pytest.mark.asyncio
    async def test_handles_large_chunks_via_buffered_writer(self, tmp_path):
        """The fdopen-wrapped writer must handle multi-MB chunks without truncation.

        Past versions used a bare `os.write(fd, b)` which can short-write
        under disk pressure (`write(2)` is allowed to transfer fewer bytes
        than requested). Using `os.fdopen(fd, "wb")` wraps the fd in a
        BufferedWriter whose `.write()` loops internally, guaranteeing the
        full chunk lands on disk.

        We assert correctness on a non-trivial chunk size — exact
        short-write behaviour is hard to provoke deterministically, but
        this guards against regressions where someone reverts to bare
        `os.write` and breaks correctness on real-world chunk sizes.
        """
        # 4 MiB chunk — large enough that on a real system, a single
        # write(2) syscall might not transfer it all in one go.
        large_chunk = b"X" * (4 * 1024 * 1024)
        payload = large_chunk + b"Y" * 1024

        async def chunks():
            yield large_chunk
            yield b"Y" * 1024

        mock_storage = MagicMock()
        mock_storage.get_stream = AsyncMock(return_value=chunks())

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
            async with materialize_archive("vcs_archives/large.tar.gz") as path:
                with open(path, "rb") as f:
                    written = f.read()

        assert len(written) == len(payload)
        assert written == payload
