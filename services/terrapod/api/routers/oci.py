"""OCI Distribution v2 read path (#1408).

Mounted at ``/v2/`` on the **root**, because the spec mandates that prefix and a
container client will not look anywhere else. The prefix was free — the only
other root-level routes are ``/api``, ``/oauth``, ``/v1``, ``/.well-known``,
``/health``, ``/metrics`` and ``/ready``.

Three things here differ from every other Terrapod router, all because an
unchangeable client dictates them:

* **Basic auth** (:mod:`terrapod.services.oci.auth`), since Kubernetes
  ``imagePullSecrets`` carry nothing else.
* **The spec's error envelope**, not Terrapod's ``{"detail": ...}``. A client
  that receives the wrong shape reports "unknown error" and leaves the operator
  with nothing.
* **Repository names contain slashes**, so routes use ``{name:path}``. Starlette's
  greedy match backtracks to the *last* suffix, which is the correct reading —
  verified against a repository literally named ``org/manifests/thing``.

Absent capability is answered with ``NAME_UNKNOWN`` (404) rather than ``DENIED``
(403), matching how the module registry declines to confirm that a resource
someone cannot see exists at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_db
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.services.oci import registry_service
from terrapod.services.oci.auth import BASIC_CHALLENGE, authenticate_oci
from terrapod.services.oci.errors import (
    BLOB_UNKNOWN,
    MANIFEST_UNKNOWN,
    NAME_INVALID,
    NAME_UNKNOWN,
    UNAUTHORIZED,
    OCIError,
    oci_error_response,
)
from terrapod.services.oci.names import (
    InvalidName,
    parse_digest,
    parse_reference,
    validate_repository,
)
from terrapod.services.registry_rbac_service import resolve_registry_capabilities_for
from terrapod.storage import ObjectStore, get_storage

router = APIRouter(tags=["oci"])

#: Clients check this header to confirm they are talking to a v2 registry.
API_VERSION_HEADER = {"Docker-Distribution-API-Version": "registry/2.0"}


async def oci_error_handler(request: Request, exc: Exception) -> Response:
    """Render :class:`OCIError` in the shape container clients parse.

    Registered on the app rather than caught per-route so no handler can forget
    it and silently answer in the house envelope.
    """
    assert isinstance(exc, OCIError)
    headers = dict(API_VERSION_HEADER)
    if exc.error is UNAUTHORIZED:
        headers["WWW-Authenticate"] = BASIC_CHALLENGE
    return oci_error_response(exc.error, detail=exc.detail, message=exc.message, headers=headers)


async def _authorised_repository(
    db: AsyncSession, user: AuthenticatedUser, name: str, capability: str
):
    """Validate the name, find the repository, and check the capability.

    Every route begins this way, and each of the three failures is deliberately
    indistinguishable to a caller without access: an invalid name is the only
    one that reports differently, because it cannot leak anything.
    """
    try:
        validate_repository(name)
    except InvalidName as exc:
        raise OCIError(NAME_INVALID, message=str(exc)) from exc

    repository = await registry_service.get_repository(db, name)
    if repository is None:
        raise OCIError(NAME_UNKNOWN, detail={"name": name})

    caps = await resolve_registry_capabilities_for(
        db, user, repository.name, repository.labels or {}, repository.owner_email
    )
    if not has_capability(caps, capability):
        # 404, not 403 — do not confirm the existence of something the caller
        # may not see.
        raise OCIError(NAME_UNKNOWN, detail={"name": name})
    return repository


@router.get("/v2/")
async def version_check(user: AuthenticatedUser = Depends(authenticate_oci)) -> Response:
    """The spec's entry point: proves this is a v2 registry and that credentials work.

    Authenticated deliberately. An anonymous 200 here would tell a client its
    credentials were accepted when they had not been looked at, and the failure
    would surface later as a confusing 401 mid-push.
    """
    return JSONResponse(content={}, headers=dict(API_VERSION_HEADER))


@router.get("/v2/{name:path}/tags/list")
async def list_tags(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
) -> Response:
    """List a repository's tags, with the spec's cursor pagination."""
    repository = await _authorised_repository(db, user, name, cap.REGISTRY_READ)

    raw_n = request.query_params.get("n")
    limit: int | None = None
    if raw_n is not None:
        try:
            limit = max(0, int(raw_n))
        except ValueError:
            # The spec does not define behaviour for a non-numeric n; ignoring
            # it is friendlier than failing a pull over a malformed query.
            limit = None

    tags = await registry_service.list_tags(
        db, repository, limit=limit, last=request.query_params.get("last")
    )
    return JSONResponse(
        content={"name": repository.name, "tags": tags}, headers=dict(API_VERSION_HEADER)
    )


@router.api_route("/v2/{name:path}/manifests/{reference}", methods=["GET", "HEAD"])
async def get_manifest(
    name: str,
    reference: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Fetch a manifest by tag or digest.

    GET and HEAD share an implementation because they must agree on every
    header — a client HEADs to decide whether it needs the body, so a
    disagreement about ``Docker-Content-Digest`` or ``Content-Type`` sends it
    down the wrong path.
    """
    repository = await _authorised_repository(db, user, name, cap.REGISTRY_READ)

    try:
        parsed = parse_reference(reference)
    except InvalidName as exc:
        raise OCIError(MANIFEST_UNKNOWN, message=str(exc)) from exc

    manifest = await registry_service.resolve_manifest(db, repository, parsed)
    if manifest is None:
        raise OCIError(MANIFEST_UNKNOWN, detail={"reference": reference})

    headers = {
        **API_VERSION_HEADER,
        # Required: how a client learns the immutable identity of what a mutable
        # tag currently resolves to.
        "Docker-Content-Digest": manifest.digest,
        "Content-Length": str(manifest.size),
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type=manifest.media_type, headers=headers)

    body = await storage.get(manifest.storage_key)
    return Response(content=body, media_type=manifest.media_type, headers=headers)


@router.api_route("/v2/{name:path}/blobs/{digest}", methods=["GET", "HEAD"])
async def get_blob(
    name: str,
    digest: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Fetch a blob, by redirect rather than by proxying its bytes.

    A layer is hundreds of MB, so a 307 to a presigned URL keeps it off the API
    and off the BFF entirely — the same pattern the binary cache uses, and what
    the spec explicitly permits.
    """
    repository = await _authorised_repository(db, user, name, cap.REGISTRY_READ)

    try:
        parsed = parse_digest(digest)
    except InvalidName as exc:
        raise OCIError(BLOB_UNKNOWN, message=str(exc)) from exc

    blob = await registry_service.get_repository_blob(db, repository, str(parsed))
    if blob is None:
        raise OCIError(BLOB_UNKNOWN, detail={"digest": digest})

    headers = {
        **API_VERSION_HEADER,
        "Docker-Content-Digest": blob.digest,
        "Content-Length": str(blob.size),
    }
    if request.method == "HEAD":
        # No redirect on HEAD: the client is asking whether the blob exists and
        # how big it is, and following a redirect to find out would cost a
        # round trip to object storage for information we already hold.
        return Response(status_code=200, headers=headers)

    url = await storage.presigned_get_url(blob.storage_key)
    return RedirectResponse(url=str(url.url), status_code=307, headers=dict(API_VERSION_HEADER))
