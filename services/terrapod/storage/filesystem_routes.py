"""
FastAPI endpoints for filesystem presigned URL handling.

These endpoints validate HMAC-signed tokens and perform the actual I/O
for the filesystem storage backend. They maintain the same client-side
upload/download pattern as cloud backends — the Terraform CLI doesn't
know the difference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response, status
from starlette.responses import StreamingResponse

from terrapod.logging_config import get_logger

if TYPE_CHECKING:
    from terrapod.storage.filesystem import FilesystemStore

router = APIRouter(tags=["storage"])
logger = get_logger(__name__)

# Content types a presigned PUT is allowed to declare.
#
# The declared value is persisted to the object's sidecar and served straight
# back as the GET's `Content-Type` — from the deployment's own origin, and with
# no credential, because the signature *is* the credential. Left unconstrained
# that is a stored-XSS primitive on the API's hostname: `text/html` goes in with
# the upload and comes back out as a document the browser renders.
#
# It is clamped here rather than signed. Extending `_sign` to cover the
# parameter would invalidate every presigned URL already in flight — PUT and GET
# alike, since the two share one signing function — breaking runner artifact
# uploads, registry pushes and provider-cache fetches mid-flight.
#
# The set is derived from what Terrapod actually stores (`storage/keys.py` and
# every `put`/`put_stream`/`presigned_put_url` call site), not from a general
# notion of "safe": a list that is too narrow silently mis-serves a legitimate
# artifact, which is its own bug. Nothing a browser executes or renders as a
# document is on it — no `text/html`, no `image/svg+xml`, no
# `application/xhtml+xml`.
_DEFAULT_CONTENT_TYPE = "application/octet-stream"
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        _DEFAULT_CONTENT_TYPE,  # state, CLI binaries, pypi/npm/nuget artifacts
        "application/json",  # plan JSON, cost estimates, cache indexes, markers
        "application/gzip",  # module + collection tarballs, catalog wrappers
        "application/x-tar",  # config versions, plan artifacts, VCS archives
        "application/zip",  # provider binaries
        "application/pgp-signature",  # detached SHA256SUMS signatures
        "application/x-sqlite3",  # the cost pricesheet index
        "text/plain",  # SHA256SUMS manifests, onboarding config + imports
    }
)


def _safe_content_type(declared: str) -> str:
    """Clamp a client-declared content type to one we are willing to serve back.

    Parameters are dropped rather than echoed: the only caller that declares a
    type passes a bare one, so keeping them buys nothing and would put another
    attacker-controlled string into a response header.
    """
    bare = declared.split(";", 1)[0].strip().lower()
    if bare in _ALLOWED_CONTENT_TYPES:
        return bare
    # Either an attack or a new artifact class nobody added above — both are
    # worth finding in the logs.
    logger.warning("Presigned PUT content type clamped", declared=declared[:120])
    return _DEFAULT_CONTENT_TYPE


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
    content_type = _safe_content_type(
        request.query_params.get("content_type", _DEFAULT_CONTENT_TYPE)
    )

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

    return StreamingResponse(
        store.get_stream(key),
        media_type=meta.content_type,
        headers={"ETag": meta.etag},
    )
