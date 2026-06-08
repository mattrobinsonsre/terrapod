"""Tests for the h1: dirhash computation in provider_cache_service.

We verify the algorithm mechanics — sorted entries, sha256 per file,
correct prefix — and that two archives with identical content but
different timestamps produce identical h1 (zip metadata doesn't
contribute). The real proof of correctness is the live Tilt smoke
against `tofu init` accepting the lock file we produce.

The path-based variant (`_compute_h1_from_zip_path`) is the one used
by production hot paths — it reads entries in 1 MB chunks so a 500 MB
provider archive doesn't OOM the API. The parity tests below confirm
it produces identical output to the bytes variant.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
import zipfile

from terrapod.services.provider_cache_service import (
    _compute_h1_from_zip_bytes,
    _compute_h1_from_zip_path,
)


def _make_zip(files: dict[str, bytes], *, date_time: tuple = (2026, 1, 1, 0, 0, 0)) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            info = zipfile.ZipInfo(filename=name, date_time=date_time)
            zf.writestr(info, content)
    return buf.getvalue()


class TestH1Algorithm:
    def test_starts_with_h1_prefix(self) -> None:
        data = _make_zip({"a.txt": b"hello"})
        h1 = _compute_h1_from_zip_bytes(data)
        assert h1.startswith("h1:")

    def test_payload_is_base64_of_sha256(self) -> None:
        data = _make_zip({"a.txt": b"hello"})
        h1 = _compute_h1_from_zip_bytes(data)
        # 32-byte sha256 → base64 = 44 chars (with padding).
        payload = h1.removeprefix("h1:")
        decoded = base64.standard_b64decode(payload)
        assert len(decoded) == 32

    def test_deterministic_across_archives_with_same_content(self) -> None:
        a = _make_zip({"a.txt": b"hello", "b.txt": b"world"}, date_time=(2026, 1, 1, 0, 0, 0))
        b = _make_zip(
            {"a.txt": b"hello", "b.txt": b"world"},
            date_time=(2026, 6, 1, 12, 0, 0),
        )
        assert _compute_h1_from_zip_bytes(a) == _compute_h1_from_zip_bytes(b)

    def test_order_independent(self) -> None:
        # zip member insertion order is irrelevant — the algorithm
        # sorts by name before hashing.
        a = _make_zip({"a.txt": b"hi", "z.txt": b"bye"})
        b = _make_zip({"z.txt": b"bye", "a.txt": b"hi"})
        assert _compute_h1_from_zip_bytes(a) == _compute_h1_from_zip_bytes(b)

    def test_differs_when_content_differs(self) -> None:
        a = _make_zip({"a.txt": b"hello"})
        b = _make_zip({"a.txt": b"goodbye"})
        assert _compute_h1_from_zip_bytes(a) != _compute_h1_from_zip_bytes(b)

    def test_matches_manual_recomputation(self) -> None:
        """Re-implement the algorithm in the test and confirm parity."""
        files = {"main.tf": b"resource\n", "extra.tf": b"output\n"}
        data = _make_zip(files)
        h1 = _compute_h1_from_zip_bytes(data)

        # Manual recomputation: sort entries, hex(sha256) + "  " + name + "\n".
        h = hashlib.sha256()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in sorted(zf.namelist()):
                with zf.open(name) as fh:
                    content_hash = hashlib.sha256(fh.read()).hexdigest()
                h.update(f"{content_hash}  {name}\n".encode())
        expected = "h1:" + base64.standard_b64encode(h.digest()).decode()
        assert h1 == expected


class TestH1PathParity:
    """`_compute_h1_from_zip_path` must produce the same h1 as
    `_compute_h1_from_zip_bytes` for the same archive. The path
    variant is what the production hot paths use; if it diverged
    from the bytes variant the lock-extender's spliced h1 would
    silently mismatch `tofu init`'s recomputation and runs would
    fail with a lock-mismatch error at init time."""

    def _bytes_and_path(self, data: bytes):
        fd, path = tempfile.mkstemp(suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return _compute_h1_from_zip_bytes(data), _compute_h1_from_zip_path(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_single_entry(self) -> None:
        data = _make_zip({"a.txt": b"hello"})
        a, b = self._bytes_and_path(data)
        assert a == b

    def test_multiple_entries(self) -> None:
        data = _make_zip({"a.txt": b"hello", "b.txt": b"world", "z.txt": b"!"})
        a, b = self._bytes_and_path(data)
        assert a == b

    def test_entry_larger_than_chunk_size(self) -> None:
        """The path variant reads each entry in 1 MB chunks. An entry
        that spans multiple chunks must hash the same as the bytes
        variant (which uses one read per entry). Use 3 MB of repeating
        bytes — large enough to cross at least two chunk boundaries
        but cheap enough for unit tests."""
        big = (b"x" * 1024 * 1024) + (b"y" * 1024 * 1024) + (b"z" * 1024 * 1024)
        data = _make_zip({"big.bin": big, "small.txt": b"hi"})
        a, b = self._bytes_and_path(data)
        assert a == b


class TestH1PathConstantMemory:
    """Sanity-check that the path variant doesn't load entries whole.

    Construct a zip with an entry whose decompressed content would
    fit in a single read but whose hash is computed across multiple
    chunks. We can't directly assert `peak_memory_kb` in a unit
    test, but we can confirm the function tolerates an entry larger
    than the chunk size and that the result matches an authoritative
    hash computed by the test (not by the bytes variant — to keep
    this test independent if the bytes variant is ever removed)."""

    def test_authoritative_hash_for_chunked_entry(self) -> None:
        big = b"a" * (3 * 1024 * 1024 + 17)  # not aligned to chunk size
        data = _make_zip({"big.bin": big})

        fd, path = tempfile.mkstemp(suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)

            h = hashlib.sha256()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in sorted(zf.namelist()):
                    entry_h = hashlib.sha256()
                    with zf.open(name) as fh:
                        entry_h.update(fh.read())
                    h.update(f"{entry_h.hexdigest()}  {name}\n".encode())
            expected = "h1:" + base64.standard_b64encode(h.digest()).decode()

            assert _compute_h1_from_zip_path(path) == expected
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
