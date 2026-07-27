"""JSON:API error-envelope helpers (#1063).

Terrapod's house style is JSON:API: an error body is
``{"errors": [{"detail": …, "status": "404"}]}``. Historically the native
surface leaned on FastAPI's default ``HTTPException`` handler, which emits a
bare ``{"detail": …}`` — the shape go-terrapod's error extractor does NOT
understand, so it fell through and returned the raw body verbatim.

The fix is **purely additive**: a shared handler emits BOTH keys —

    {"errors": [{"detail": "...", "status": "404"}], "detail": "..."}

Old clients keep reading the top-level ``detail``; JSON:API clients (and
go-terrapod) read ``errors``. No existing response byte is removed, so the
route/attribute/wire contract snapshots are untouched.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def jsonapi_error_content(detail: Any, status_code: int) -> dict[str, Any]:
    """Build the dual-key error body: a JSON:API ``errors`` array plus the
    legacy top-level ``detail`` (verbatim, for back-compat).

    ``detail`` is usually a string, but FastAPI's request-validation errors pass
    a list of per-field dicts. In that case each entry becomes its own
    ``errors[]`` member (with a JSON-Pointer ``source``), and the original list
    is still echoed at the top level unchanged.
    """
    status = str(status_code)
    if isinstance(detail, list):
        errors = []
        for item in detail:
            if isinstance(item, dict):
                loc = item.get("loc") or []
                pointer = "/" + "/".join(str(p) for p in loc)
                errors.append(
                    {
                        "detail": item.get("msg", ""),
                        "status": status,
                        "source": {"pointer": pointer},
                    }
                )
            else:
                errors.append({"detail": str(item), "status": status})
        if not errors:
            errors = [{"detail": "Validation error", "status": status}]
    else:
        errors = [{"detail": detail if isinstance(detail, str) else str(detail), "status": status}]
    # Keep the original `detail` verbatim alongside the new `errors` array.
    return {"errors": errors, "detail": detail}


def jsonapi_error_response(
    detail: Any, status_code: int, headers: dict[str, str] | None = None
) -> JSONResponse:
    """A ``JSONResponse`` carrying the dual-key error envelope, preserving any
    headers the original error set (e.g. ``WWW-Authenticate`` on a 401)."""
    return JSONResponse(
        status_code=status_code,
        content=jsonapi_error_content(detail, status_code),
        headers=headers,
    )
