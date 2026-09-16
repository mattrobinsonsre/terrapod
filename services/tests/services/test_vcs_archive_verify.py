"""A damaged VCS archive never becomes a config version (#1600).

verify_archive reads an archive through to the gzip trailer. The poller runs it
before copying a cached archive into a config version, and removes a damaged
cache entry so the next attempt rebuilds it instead of every run for the commit
inheriting the same broken bytes.
"""

import io
import os
import tarfile
import uuid
from unittest.mock import patch

import pytest

from terrapod.services import vcs_poller
from terrapod.services.vcs_archive_cache import CorruptArchiveError, verify_archive
from terrapod.storage.keys import config_version_key


def _tar_gz(n: int = 10) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(n):
            body = os.urandom(3000)
            info = tarfile.TarInfo(f"infra/f{i}.tf")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture
def archive(tmp_path):
    def write(data: bytes) -> str:
        p = tmp_path / "a.tar.gz"
        p.write_bytes(data)
        return str(p)

    return write


class TestVerifyArchive:
    def test_a_whole_archive_passes(self, archive):
        verify_archive(archive(_tar_gz()))

    def test_cut_inside_a_file(self, archive):
        data = _tar_gz()
        with pytest.raises(CorruptArchiveError):
            verify_archive(archive(data[: len(data) // 2]))

    def test_cut_in_the_gzip_trailer(self, archive):
        # Every file is intact; only the gzip length field is missing. Walking
        # the members stops at the end-of-archive block, so this is caught only
        # by reading on to the trailer.
        data = _tar_gz()
        with pytest.raises(CorruptArchiveError):
            verify_archive(archive(data[:-4]))

    def test_not_gzip(self, archive):
        with pytest.raises(CorruptArchiveError):
            verify_archive(archive(b"<html>Service Unavailable</html>"))

    def test_empty(self, archive):
        with pytest.raises(CorruptArchiveError):
            verify_archive(archive(b""))


class _Store:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.puts: list[str] = []

    async def get_stream(self, key):
        data = self.objects[key]
        for i in range(0, len(data), 1024):
            yield data[i : i + 1024]

    async def put_stream(self, key, chunks, content_type=None, metadata=None):  # noqa: ARG002
        self.objects[key] = b"".join([c async for c in chunks])
        self.puts.append(key)

    async def delete(self, key):
        self.objects.pop(key, None)


def _patched(store: _Store):
    return (
        patch.object(vcs_poller, "get_storage", return_value=store),
        patch("terrapod.services.vcs_archive_cache.get_storage", return_value=store),
    )


class TestConfigVersionUploadChecksTheArchive:
    @pytest.mark.asyncio
    async def test_a_damaged_cached_archive_is_removed_and_nothing_uploaded(self):
        data = _tar_gz()
        store = _Store({"vcs/k": data[: len(data) // 2]})
        p1, p2 = _patched(store)
        with p1, p2, pytest.raises(CorruptArchiveError):
            await vcs_poller._stream_cv_upload_from_cache("vcs/k", uuid.uuid4(), uuid.uuid4())

        assert store.puts == [], "a damaged archive was copied into a config version"
        assert "vcs/k" not in store.objects, "the damaged cache entry was left for the next run"

    @pytest.mark.asyncio
    async def test_a_whole_archive_is_copied_to_the_config_version(self):
        data = _tar_gz()
        store = _Store({"vcs/k": data})
        ws, cv = uuid.uuid4(), uuid.uuid4()
        p1, p2 = _patched(store)
        with p1, p2:
            await vcs_poller._stream_cv_upload_from_cache("vcs/k", ws, cv)

        key = config_version_key(str(ws), str(cv))
        assert store.puts == [key]
        assert store.objects[key] == data
        assert "vcs/k" in store.objects
