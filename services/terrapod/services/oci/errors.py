"""The OCI Distribution error envelope (#1408).

The spec defines its own error shape, and container clients parse it. This is
therefore *not* one of the surfaces that follows Terrapod's house JSON:API
convention — the same reasoning that exempts the runner protocol and the OAuth
token endpoints: an unchangeable client dictates the shape.

    {"errors": [{"code": "BLOB_UNKNOWN", "message": "...", "detail": {...}}]}

Getting this right is worth the small module. A client that receives Terrapod's
usual ``{"detail": ...}`` where it expects this will report something unhelpful —
``docker push`` in particular will surface a bare "unknown error" and leave the
operator with nothing to go on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import status
from fastapi.responses import JSONResponse


@dataclass(frozen=True, slots=True)
class OCIErrorCode:
    """A spec error code and the HTTP status it is served with.

    Pairing them here stops the two drifting apart: the spec is specific about
    which status accompanies which code, and clients do branch on both.
    """

    code: str
    status_code: int
    message: str


# The codes Terrapod actually emits. Deliberately not the entire spec list —
# an unused code is one nobody has thought about the semantics of.
BLOB_UNKNOWN = OCIErrorCode("BLOB_UNKNOWN", status.HTTP_404_NOT_FOUND, "blob unknown to registry")
BLOB_UPLOAD_INVALID = OCIErrorCode(
    "BLOB_UPLOAD_INVALID", status.HTTP_400_BAD_REQUEST, "blob upload invalid"
)
#: A chunk that does not begin where the last one ended. Distinct from
#: BLOB_UPLOAD_INVALID because the spec requires **416**, not 400: the client is
#: being told its byte range is wrong and where to resume, which is a different
#: conversation from "this upload is malformed".
BLOB_UPLOAD_OUT_OF_ORDER = OCIErrorCode(
    "BLOB_UPLOAD_INVALID",
    416,  # Range Not Satisfiable — the literal; Starlette renamed its constant
    "chunk does not begin at the expected offset",
)
BLOB_UPLOAD_UNKNOWN = OCIErrorCode(
    "BLOB_UPLOAD_UNKNOWN", status.HTTP_404_NOT_FOUND, "blob upload unknown to registry"
)
DIGEST_INVALID = OCIErrorCode(
    "DIGEST_INVALID", status.HTTP_400_BAD_REQUEST, "provided digest did not match uploaded content"
)
MANIFEST_BLOB_UNKNOWN = OCIErrorCode(
    "MANIFEST_BLOB_UNKNOWN",
    status.HTTP_404_NOT_FOUND,
    "manifest references a manifest or blob unknown to registry",
)
MANIFEST_INVALID = OCIErrorCode("MANIFEST_INVALID", status.HTTP_400_BAD_REQUEST, "manifest invalid")
MANIFEST_UNKNOWN = OCIErrorCode(
    "MANIFEST_UNKNOWN", status.HTTP_404_NOT_FOUND, "manifest unknown to registry"
)
NAME_INVALID = OCIErrorCode("NAME_INVALID", status.HTTP_400_BAD_REQUEST, "invalid repository name")
NAME_UNKNOWN = OCIErrorCode(
    "NAME_UNKNOWN", status.HTTP_404_NOT_FOUND, "repository name not known to registry"
)
SIZE_INVALID = OCIErrorCode(
    "SIZE_INVALID", status.HTTP_400_BAD_REQUEST, "provided length did not match content length"
)
UNAUTHORIZED = OCIErrorCode("UNAUTHORIZED", status.HTTP_401_UNAUTHORIZED, "authentication required")
DENIED = OCIErrorCode(
    "DENIED", status.HTTP_403_FORBIDDEN, "requested access to the resource is denied"
)
#: The same spec code at 405, for an operation this registry declines outright
#: rather than one it rejected for its arguments. Split for the same reason as
#: BLOB_UPLOAD_OUT_OF_ORDER above: the code says what went wrong, the status says
#: what the client should do about it, and 400 would invite a retry that can
#: never succeed.
NOT_ALLOWED = OCIErrorCode(
    "UNSUPPORTED", status.HTTP_405_METHOD_NOT_ALLOWED, "the operation is not supported"
)
UNSUPPORTED = OCIErrorCode(
    "UNSUPPORTED", status.HTTP_400_BAD_REQUEST, "the operation is unsupported"
)


class OCIError(Exception):
    """Raised inside the registry surface; rendered by the router's handler.

    Carries the code rather than a status so the two can never be mismatched at
    the raise site.
    """

    def __init__(
        self,
        error: OCIErrorCode,
        detail: Any | None = None,
        message: str | None = None,
    ) -> None:
        self.error = error
        self.detail = detail
        # An overridable message because the spec's stock wording ("blob unknown
        # to registry") is correct but tells an operator nothing about *which*
        # blob; `detail` is free-form and clients display it.
        self.message = message or error.message
        super().__init__(f"{error.code}: {self.message}")


def oci_error_body(
    error: OCIErrorCode, detail: Any | None = None, message: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Build the spec's error envelope."""
    entry: dict[str, Any] = {"code": error.code, "message": message or error.message}
    if detail is not None:
        entry["detail"] = detail
    return {"errors": [entry]}


def oci_error_response(
    error: OCIErrorCode,
    detail: Any | None = None,
    message: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render an error the way a container client expects to receive it."""
    return JSONResponse(
        status_code=error.status_code,
        content=oci_error_body(error, detail=detail, message=message),
        headers=headers,
    )
