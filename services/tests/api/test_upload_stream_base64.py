"""Multipart parts that arrive base64-encoded (#1482).

`ansible-galaxy collection publish` sends its file part as
`Content-Transfer-Encoding: base64`. Starlette's multipart parser does not
decode that — it hands back the encoded text — so a helper that ignores the
header writes base64 where the caller expects an archive.

This is worth a test of its own because of how it failed: publish was broken for
every real client while `curl -F file=@...` worked perfectly, so nothing in the
suite noticed. The decode also has to be *streamed*, since the whole point of
the helper is that a large upload never accumulates in the worker's heap, and a
chunk boundary lands mid-quad often enough that "it worked on a small file" says
nothing.
"""

from __future__ import annotations

import base64
import io
import os

import pytest
from starlette.datastructures import Headers, UploadFile

from terrapod.api.upload_stream import stream_upload_to_tempfile


def _part(payload: bytes, *, encoding: str | None) -> UploadFile:
    headers = Headers({"content-transfer-encoding": encoding} if encoding else {})
    return UploadFile(file=io.BytesIO(payload), filename="c.tar.gz", headers=headers)


async def _collect(upload: UploadFile) -> bytes:
    path, _ = await stream_upload_to_tempfile(upload, suffix=".test")
    try:
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        os.unlink(path)


class TestBase64Parts:
    async def test_a_declared_base64_part_is_decoded(self) -> None:
        raw = b"\x1f\x8b" + b"a collection tarball's bytes" * 10
        assert await _collect(_part(base64.encodebytes(raw), encoding="base64")) == raw

    async def test_a_part_spanning_many_chunks_round_trips(self) -> None:
        """The streamed path, where a chunk boundary lands mid-quad.

        Several megabytes so the 1 MiB read loop runs repeatedly — the case a
        small fixture would not reach, and the one where a naive per-chunk
        decode corrupts the output.
        """
        raw = os.urandom(3 * 1024 * 1024 + 7)
        assert await _collect(_part(base64.encodebytes(raw), encoding="base64")) == raw

    async def test_the_header_is_matched_case_insensitively(self) -> None:
        """`Content-Transfer-Encoding: BASE64` is the same declaration."""
        raw = b"payload"
        assert await _collect(_part(base64.encodebytes(raw), encoding="BASE64")) == raw

    async def test_surrounding_whitespace_does_not_matter(self) -> None:
        raw = b"payload"
        assert await _collect(_part(base64.encodebytes(raw), encoding="  base64 ")) == raw


class TestUndeclaredPartsAreUntouched:
    """Decoding must be driven by the header, never guessed.

    A binary part that happens to be valid base64 is not base64, and decoding it
    would corrupt an upload that was perfectly well-formed.
    """

    async def test_a_part_with_no_encoding_is_stored_verbatim(self) -> None:
        raw = b"\x1f\x8b\x08\x00 raw gzip bytes"
        assert await _collect(_part(raw, encoding=None)) == raw

    async def test_a_binary_part_that_looks_like_base64_is_not_decoded(self) -> None:
        looks_like = b"YWJjZGVmZ2g="  # decodes cleanly, but was never declared
        assert await _collect(_part(looks_like, encoding=None)) == looks_like

    async def test_another_encoding_is_not_decoded(self) -> None:
        raw = b"plain bytes"
        assert await _collect(_part(raw, encoding="binary")) == raw


class TestTruncatedBase64:
    async def test_it_raises_rather_than_silently_padding(self) -> None:
        """A short final group means the client sent a truncated part.

        Padding it out would store a file that is subtly not what was uploaded,
        and the failure would surface much later as a digest mismatch.
        """
        with pytest.raises(Exception):  # noqa: B017 - binascii.Error, via b64decode
            await _collect(_part(b"YWJjZGVmZ2", encoding="base64"))
