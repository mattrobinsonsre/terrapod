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

# Marks a 502/504 that Terrapod returned because an UPSTREAM provider failed,
# as distinct from the BFF's own bare 502 when it briefly cannot reach the API.
# Both reach the browser as a 502, so the status alone cannot tell them apart —
# and they want opposite handling: retrying a provider outage multiplies load on
# something already down, while a BFF blip is exactly what a retry is for. The
# API is the only layer that knows which happened, so it says so.
UPSTREAM_FAILURE_HEADER = "X-Terrapod-Upstream-Failure"


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
    possible message available, because it cannot be told apart from a Terrapod
    bug or from the operator's own misconfiguration. During the GitHub incident
    of 2026-08-17, an operator queueing a run saw only "Internal server error".

    The diagnosis is delegated to `describe_vcs_error`, which the poller already
    uses for the workspace's `vcs_last_error` and its health banner. Two reasons
    that matters more than saving a few lines: the operator reads the SAME
    sentence in the banner and in the failed request rather than two
    descriptions of one outage, and it already separates a **rate-limit 403**
    (reading `x-ratelimit-remaining` / `retry-after`, so it can say how long
    until the window resets) from a **provider simply returning 403** — which is
    the distinction that decides whether the operator polls less or waits for
    someone else's incident to end.

    What is added here is the request's own context: which repo and ref, and
    that nothing was created. A 404 is deliberately mapped to 422 — a missing
    repo or branch is the operator's configuration to fix, and sending them to a
    status page over their own typo would be worse than the 500 was.
    """
    import httpx
    from fastapi import HTTPException

    from terrapod.services.vcs_poller import describe_vcs_error

    provider = (
        str(getattr(conn, "provider", "") or "")
        .replace("github", "GitHub")
        .replace("gitlab", "GitLab")
    ) or "the VCS provider"
    where = f"{repo}@{ref}" if ref else repo

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
        return HTTPException(
            status_code=422,
            detail=(
                f"{provider} has no {where} — check the repository URL and branch, and "
                f"that the connection still has access to it."
            ),
        )

    status = 504 if isinstance(exc, httpx.TimeoutException) else 502
    return HTTPException(
        status_code=status,
        detail=f"Could not read {where} from {provider} — {describe_vcs_error(exc)}. Nothing was created.",
        headers={UPSTREAM_FAILURE_HEADER: "vcs"},
    )
