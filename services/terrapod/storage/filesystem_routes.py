"""
FastAPI endpoints for filesystem presigned URL handling.

These endpoints validate HMAC-signed tokens and perform the actual I/O
for the filesystem storage backend. They maintain the same client-side
upload/download pattern as cloud backends — the Terraform CLI doesn't
know the difference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiofiles
from fastapi import APIRouter, HTTPException, Request, Response, status
from starlette.responses import StreamingResponse

from terrapod.logging_config import get_logger

if TYPE_CHECKING:
    from terrapod.storage.filesystem import FilesystemStore

router = APIRouter(tags=["storage"])
logger = get_logger(__name__)

# Set by storage init — the filesystem store instance
_store: FilesystemStore | None = None


def set_filesystem_store(store: FilesystemStore) -> None:
    """Register the filesystem store instance for route handlers."""
    global _store  # noqa: PLW0603
    _store = store


def _get_store() -> FilesystemStore:
    if _store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Filesystem storage not initialized",
        )
    return _store


@router.put("/storage/put/{key:path}")
async def storage_put(key: str, request: Request) -> Response:
    """Handle a presigned PUT — validate signature and store the object."""
    store = _get_store()

    expires = request.query_params.get("expires", "")
    sig = request.query_params.get("sig", "")
    content_type = request.query_params.get("content_type", "application/octet-stream")

    if not store.verify_signature("PUT", key, expires, sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired signature",
        )

    await store.put_stream(key, request.stream(), content_type=content_type)
    logger.info("Object stored via presigned URL", key=key)

    return Response(status_code=status.HTTP_201_CREATED)


@router.get("/storage/get/{key:path}")
async def storage_get(key: str, request: Request) -> Response:
    """Handle a presigned GET — validate signature and return the object."""
    store = _get_store()

    expires = request.query_params.get("expires", "")
    sig = request.query_params.get("sig", "")

    if not store.verify_signature("GET", key, expires, sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired signature",
        )

    from terrapod.storage.protocol import ObjectNotFoundError

    try:
        meta = await store.head(key)
    except ObjectNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Object not found: {key}",
        ) from e

    # Content-Length and Range are not optional niceties here. The OCI
    # distribution conformance suite requires both on a blob GET, and this route
    # is where a filesystem-backed presigned URL lands — cloud backends redirect
    # straight to S3/Azure/GCS, which provide them natively, so without this the
    # filesystem backend is the only one that cannot serve a conformant
    # registry. Everything else presigned from the filesystem — binary cache,
    # provider cache, module tarballs — gets resumable downloads out of it too.
    common = {"ETag": meta.etag, "Accept-Ranges": "bytes"}

    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, meta.size_bytes)
        if parsed is None:
            # Unsatisfiable: the spec wants 416 with the true size, so a client
            # can correct itself rather than guess.
            return Response(
                status_code=416,  # Range Not Satisfiable — the literal; Starlette renamed its constant
                headers={**common, "Content-Range": f"bytes */{meta.size_bytes}"},
            )
        start, end = parsed
        length = end - start + 1
        return StreamingResponse(
            _ranged_stream(store, key, start, length),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=meta.content_type,
            headers={
                **common,
                "Content-Length": str(length),
                "Content-Range": f"bytes {start}-{end}/{meta.size_bytes}",
            },
        )

    return StreamingResponse(
        store.get_stream(key),
        media_type=meta.content_type,
        headers={**common, "Content-Length": str(meta.size_bytes)},
    )


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range `Range: bytes=…`, or None if unsatisfiable.

    Only the single-range forms are honoured — `a-b`, `a-`, `-n`. Multi-range
    responses need multipart/byteranges, which no registry client asks for, so
    an unsupported form is treated as no range at all rather than answered
    incorrectly.
    """
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].strip()
    if "," in spec:  # multi-range: decline rather than answer wrongly
        return None

    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            # Suffix form: the last N bytes.
            suffix = int(end_text)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            return start, size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return None

    if start >= size or start < 0 or end < start:
        return None
    # A range that runs past the end is clamped, not refused — RFC 9110, and it
    # is what a client resuming a download will send.
    return start, min(end, size - 1)


async def _ranged_stream(store: FilesystemStore, key: str, start: int, length: int):
    """Stream `length` bytes from `start` by seeking, not by discarding.

    Reading from the beginning and throwing away the prefix would make a range
    request on a large layer as expensive as a full download, which is the
    opposite of why clients use them.
    """
    path = store._full_path(key)  # noqa: SLF001 — this route is filesystem-specific
    chunk_size = 256 * 1024
    async with aiofiles.open(path, "rb") as handle:
        await handle.seek(start)
        remaining = length
        while remaining > 0:
            data = await handle.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
