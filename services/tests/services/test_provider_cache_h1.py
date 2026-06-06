"""Tests for the h1: dirhash computation in provider_cache_service.

We verify the algorithm mechanics — sorted entries, sha256 per file,
correct prefix — and that two archives with identical content but
different timestamps produce identical h1 (zip metadata doesn't
contribute). The real proof of correctness is the live Tilt smoke
against `tofu init` accepting the lock file we produce.
"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile

from terrapod.services.provider_cache_service import _compute_h1_from_zip_bytes


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
