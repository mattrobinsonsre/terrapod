"""Resolving a path segment that is either a capability or a bare id.

Four endpoints are reached by a client that sends no `Authorization` header,
because the client constructs the request itself rather than going through its
authenticated request builder:

* `/plans/{id}/log` and `/applies/{id}/log` — go-tfe's `LogReader.read` builds a
  bare `http.NewRequest` and copies only `client.headers`, the operator-supplied
  map. The per-request `Authorization` set in `Client.newRequest` never reaches
  it. (Confirmed against go-tfe v1.95.0, the version OpenTofu 1.11.x vendors.)
* `/configuration-versions/{id}/upload` and `/state-versions/{id}/content` —
  go-tfe's foreign-PUT path, likewise unauthenticated by construction.

So those four cannot simply be auth-gated. Until now the path segment was a bare
resource UUID, which is not a secret: it appears in UI links, audit rows,
notification payloads and PR comments, so anyone who learned a run id could read
that run's logs, and anyone who learned a configuration-version id could upload
the Terraform that run would execute.

**A capability replaces the bare id in the same path segment**, so no route
template changes and no route is added. It must be the path rather than a query
parameter: `LogReader.read` rewrites `RawQuery` wholesale on every chunk
(`r.logURL.RawQuery = fmt.Sprintf("limit=%d&offset=%d", ...)`), so a `?ticket=`
would not survive to the first byte.

**A bare id still works, but only with authentication.** That is what keeps the
authenticated consumers whole — the MCP `terrapod_run_logs` tool, go-terrapod's
`UploadStateContent`, terrapod-migrate's configuration upload and the web UI all
send a credential and address these endpoints by plain id. It also means the
change closes the hole without a flag day: the capability is what a credential-less
client is *given*, and the credential is what everything else already sends.

The one thing it does not preserve is a run already in flight when the API is
upgraded: its CLI holds a bare-id log URL and sends no credential, so the log
stream ends with a 401. The run itself is unaffected and re-attaching gets a
fresh capability. That is stated in the release notes rather than papered over
with a grace period, because a grace period is the hole staying open.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from terrapod.api.dependencies import AuthenticatedUser, authenticate_request
from terrapod.auth import capability_urls


async def resolve_capability_or_authenticate(
    segment: str,
    *,
    expect_kind: str,
    request: Request,
    not_found: str,
) -> tuple[str, AuthenticatedUser | None]:
    """Turn a path segment into a resource id, saying how it was authorised.

    Returns `(resource_id, user)`. `user` is `None` when a valid capability
    authorised the request — the caller performs no further permission check,
    because the capability names exactly one resource and nothing else.
    When `user` is set, the segment was a bare id and the caller MUST apply its
    own permission check; returning the user rather than the id alone is what
    makes forgetting that check visible at the call site.

    A segment that *looks* like a capability but does not verify is a 404, never
    a fallthrough to the authenticated path: a forged, tampered or expired
    capability must not be retried as if it were an id, and answering 404 tells
    a prober nothing about which of those it was.
    """
    resource_id = capability_urls.verify(segment, expect_kind=expect_kind)
    if resource_id is not None:
        return resource_id, None
    if capability_urls.looks_like_capability(segment):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found)
    # A bare id. Authenticate before the resource is looked up, so an
    # unauthenticated caller cannot use the 404-vs-401 difference to learn
    # whether an id exists.
    user = await authenticate_request(request)
    return segment, user
