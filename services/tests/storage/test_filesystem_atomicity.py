"""Writes to the filesystem store are atomic (#1417).

Found by running a real `npm install` through the package proxy: npm fans out a
dozen parallel requests, two missed the same tarball at once, both fetched and
wrote the same key while a third read it — and the reader got a partial object.
It surfaced as `RuntimeError: Response content shorter than Content-Length` and
an aborted install, which reads like a client or network fault and is neither.

A reader must see either the previous object or the complete new one.
"""

from __future__ import annotations

import asyncio

import pytest

from terrapod.storage.filesystem import FilesystemStore

pytestmark = pytest.mark.asyncio

SMALL = b"x" * 1024
LARGE = b"y" * (4 * 1024 * 1024)


@pytest.fixture
def store(tmp_path) -> FilesystemStore:
    return FilesystemStore(root_dir=str(tmp_path), hmac_secret="test")


async def _slow_chunks(data: bytes, chunks: int = 32):
    """Feed a write slowly enough that a concurrent read overlaps it."""
    step = max(1, len(data) // chunks)
    for offset in range(0, len(data), step):
        yield data[offset : offset + step]
        await asyncio.sleep(0)  # yield to the reader


class TestAtomicWrites:
    async def test_a_reader_never_sees_a_partial_object(self, store) -> None:
        await store.put("k", SMALL)

        async def write() -> None:
            await store.put_stream("k", _slow_chunks(LARGE))

        async def read_repeatedly() -> list[int]:
            seen = []
            for _ in range(40):
                if await store.exists("k"):
                    seen.append(len(await store.get("k")))
                await asyncio.sleep(0)
            return seen

        _, sizes = await asyncio.gather(write(), read_repeatedly())

        # Every observation is one of the two complete objects, never anything
        # in between — which is what a non-atomic write produces.
        assert sizes, "the reader never observed the object"
        assert set(sizes) <= {len(SMALL), len(LARGE)}, f"partial reads observed: {set(sizes)}"

    async def test_concurrent_writers_leave_one_complete_object(self, store) -> None:
        """Two proxy replicas fetching the same cold artifact at once.

        Both writes are legitimate and identical in practice; what matters is
        that the file left behind is whole rather than interleaved.
        """
        await asyncio.gather(
            store.put_stream("k", _slow_chunks(LARGE)),
            store.put_stream("k", _slow_chunks(LARGE)),
        )
        assert len(await store.get("k")) == len(LARGE)

    async def test_the_reported_size_matches_the_bytes_on_disk(self, store) -> None:
        """The mismatch that aborted the download.

        The storage route sends Content-Length from the recorded size, so a
        stored length that disagrees with the file truncates the response.
        """
        meta = await store.put_stream("k", _slow_chunks(LARGE))
        assert meta.size_bytes == len(await store.get("k"))

    async def test_a_failed_write_leaves_no_temporary_file(self, tmp_path, store) -> None:
        """A partial download must not accumulate as an unfindable disk leak."""

        async def _explodes():
            yield b"partial"
            raise RuntimeError("upstream went away")

        with pytest.raises(RuntimeError):
            await store.put_stream("k", _explodes())

        leftovers = [p.name for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]
        assert leftovers == []

    async def test_a_failed_write_does_not_destroy_the_previous_object(self, store) -> None:
        """The reason to write beside the destination rather than over it."""
        await store.put("k", SMALL)

        async def _explodes():
            yield b"partial"
            raise RuntimeError("upstream went away")

        with pytest.raises(RuntimeError):
            await store.put_stream("k", _explodes())

        assert await store.get("k") == SMALL
