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


def vcs_unavailable(conn: Any, repo: str, ref: str, exc: Exception) -> HTTPException:  # noqa: F821
    """Turn a provider failure into an error the operator can act on.

    A provider call raises rather than returning a falsy value, so a handler's
    own "could not determine X" guard never sees an outage — the exception sails
    past it into the catch-all and the caller gets a bare 500. That is the worst
    possible message here, because it cannot be told apart from a Terrapod bug
    or the operator's own misconfiguration. During a GitHub incident that
    returned 403 on every call, an operator queueing a run saw only "Internal
    server error".

    So name the provider, the repo, the ref and the upstream status, and say
    plainly that nothing was created. A 404 is deliberately kept as a 422: a
    missing repo or branch is the operator's configuration to fix, whereas any
    other failure is the provider's and not theirs.
    """
    import httpx
    from fastapi import HTTPException

    provider = (
        str(getattr(conn, "provider", "") or "the VCS provider")
        .replace("github", "GitHub")
        .replace("gitlab", "GitLab")
    )
    where = f"{repo}@{ref}" if ref else repo

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 404:
            return HTTPException(
                status_code=422,
                detail=(
                    f"{provider} has no {where} — check the repository URL and branch, "
                    f"and that the connection still has access to it."
                ),
            )
        return HTTPException(
            status_code=502,
            detail=(
                f"Could not read {where} from {provider} (HTTP {code}). Nothing was "
                f"created. If {provider} is healthy, check this connection's saturation "
                f"and its access to the repository."
            ),
        )
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(
            status_code=504,
            detail=f"{provider} timed out reading {where}. Nothing was created.",
        )
    return HTTPException(
        status_code=502,
        detail=f"Could not reach {provider} to read {where}. Nothing was created.",
    )
