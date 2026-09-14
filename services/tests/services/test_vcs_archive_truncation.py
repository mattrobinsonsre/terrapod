"""A failed archive build never leaves a truncated archive in storage (#1600).

The VCS archive is built by a producer thread (tar + gzip) writing into a pipe
that the upload reads from. When the producer died mid-stream, it closed its end
of the pipe, so the upload saw an ordinary end-of-file and finished storing the
bytes it had — a truncated gzip, under the cache key for that commit. Every
config version built from that commit afterwards got the same broken archive,
and retrying a run downloaded it again.

The storage fake here behaves like the object stores: an object appears only
when its stream has been consumed to the end, and an exception raised from the
stream stores nothing. It consumes slowly, so the old behaviour — an upload
left running after the build had already failed — is reproduced reliably.
"""

import asyncio
import gzip
import io
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import git_fetch
from terrapod.services.vcs_archive_cache import VCSArchiveCache
from terrapod.storage.protocol import ObjectNotFoundError

SHA = "a" * 40


class _ObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_stream(self, key, chunks, content_type=None, metadata=None):  # noqa: ARG002
        buf = b""
        async for c in chunks:
            buf += c
            await asyncio.sleep(0.005)
        self.objects[key] = buf

    async def head(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError(key)

    async def delete(self, key):
        self.objects.pop(key, None)


def _conn():
    conn = MagicMock()
    conn.id = uuid.uuid4()
    conn.provider = "github"
    conn.server_url = None
    return conn


def _tarball(n_files: int = 40) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for i in range(n_files):
            body = (f'resource "x" "r{i}" {{}}\n' * 400).encode()
            info = tarfile.TarInfo(name=f"infra/f{i}.tf")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _dies_halfway(fileobj, working_tree):  # noqa: ARG001
    """A tarball writer that emits half a gzip stream, then fails."""
    data = gzip.compress(_tarball())
    half = data[: len(data) // 2]
    for i in range(0, len(half), 4096):
        fileobj.write(half[i : i + 4096])
    raise OSError("simulated failure part-way through the tree")


@pytest.fixture
def store():
    s = _ObjectStore()
    with (
        patch.object(git_fetch, "_run_git", AsyncMock()),
        patch.object(git_fetch, "_resolve_auth", AsyncMock(return_value="Authorization: x")),
        patch.object(git_fetch, "get_storage", return_value=s),
        patch("terrapod.services.vcs_archive_cache.get_storage", return_value=s),
    ):
        yield s


async def _settle():
    # Long enough for an upload the failure left running to finish.
    await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_a_build_that_fails_part_way_stores_nothing(store, tmp_path, monkeypatch):
    monkeypatch.setattr(git_fetch, "_write_tarball_from_dir", _dies_halfway)

    with pytest.raises(OSError, match="part-way"):
        await git_fetch.sparse_archive_to_storage(
            _conn(), "o", "r", SHA, None, "vcs/key", clone_dir=str(tmp_path)
        )
    await _settle()

    assert "vcs/key" not in store.objects, "a truncated archive was stored"


@pytest.mark.asyncio
async def test_the_cache_holds_no_truncated_archive_after_a_failed_build(store, monkeypatch):
    monkeypatch.setattr(git_fetch, "_write_tarball_from_dir", _dies_halfway)
    cache = VCSArchiveCache()

    with pytest.raises(OSError):
        await cache.get_or_fetch(_conn(), "o", "r", SHA)
    await _settle()

    # The next poll must miss and rebuild, not reuse a broken archive.
    assert store.objects == {}, f"left behind: {list(store.objects)}"


@pytest.mark.asyncio
async def test_a_build_that_succeeds_stores_the_whole_archive(store, tmp_path):
    wt = tmp_path / "wt"
    (wt / "infra").mkdir(parents=True)
    for i in range(20):
        (wt / "infra" / f"f{i}.tf").write_text(f'resource "x" "r{i}" {{}}\n' * 200)

    n = await git_fetch.sparse_archive_to_storage(
        _conn(), "o", "r", SHA, None, "vcs/key", clone_dir=str(wt)
    )

    stored = store.objects["vcs/key"]
    assert n == len(stored)
    with tarfile.open(fileobj=io.BytesIO(stored), mode="r:gz") as tf:
        assert len(tf.getmembers()) == 20
