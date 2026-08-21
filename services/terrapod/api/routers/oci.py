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

import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_db
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import OCIRepository
from terrapod.services.oci import registry_service, upload_service
from terrapod.services.oci.auth import BASIC_CHALLENGE, authenticate_oci
from terrapod.services.oci.errors import (
    BLOB_UNKNOWN,
    BLOB_UPLOAD_INVALID,
    BLOB_UPLOAD_UNKNOWN,
    DIGEST_INVALID,
    MANIFEST_BLOB_UNKNOWN,
    MANIFEST_INVALID,
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
    db: AsyncSession,
    user: AuthenticatedUser,
    name: str,
    capability: str,
    *,
    create: bool = False,
):
    """Validate the name, find the repository, and check the capability.

    Every route begins this way, and each of the three failures is deliberately
    indistinguishable to a caller without access: an invalid name is the only
    one that reports differently, because it cannot leak anything.

    ``create`` is for the push path, where the repository often does not exist
    yet — ``docker push`` never asks for one to be created first. The new row is
    owned by the pushing user, which matches how Terrapod already treats
    workspaces and registry modules: any authenticated user may create one, and
    the creator becomes its owner.
    """
    try:
        validate_repository(name)
    except InvalidName as exc:
        raise OCIError(NAME_INVALID, message=str(exc)) from exc

    repository = await registry_service.get_repository(db, name)
    if repository is None:
        if not create:
            raise OCIError(NAME_UNKNOWN, detail={"name": name})
        repository = OCIRepository(name=name, owner_email=user.email or None, labels={})
        db.add(repository)
        await db.flush()
        # Freshly created and owned by this user, so the capability check below
        # will pass — but it is left to run rather than short-circuited, so
        # there is exactly one place that decides access.

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


# ── push ───────────────────────────────────────────────────────────────────
#
# The upload dance is three calls in the common case — POST to open, PATCH per
# chunk, PUT to finish — plus two shortcuts the spec defines because they save
# an entire layer transfer: monolithic upload (POST with a body and a digest)
# and cross-repository mount (POST with ?mount=&from=).


def _upload_location(name: str, session_id) -> str:
    """Where the client sends the next chunk.

    Absolute path rather than a full URL: the client resolves it against the
    registry it is already talking to, which keeps this correct behind the BFF,
    behind an ingress, and under whatever hostname the operator has chosen.
    """
    return f"/v2/{name}/blobs/uploads/{session_id}"


@router.post("/v2/{name:path}/blobs/uploads/")
async def start_upload(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Open an upload session, mount an existing blob, or accept a whole blob.

    Three behaviours on one route because the spec puts them there — a client
    decides which by query parameter, and must be able to attempt a mount
    without knowing in advance whether it will succeed.
    """
    repository = await _authorised_repository(db, user, name, cap.REGISTRY_WRITE, create=True)

    mount = request.query_params.get("mount")
    source_name = request.query_params.get("from")
    if mount and source_name:
        source = await registry_service.get_repository(db, source_name)
        if source is not None:
            source_caps = await resolve_registry_capabilities_for(
                db, user, source.name, source.labels or {}, source.owner_email
            )
            # Read on the *source* is required: mounting is a read of its
            # content, and without this check a digest would be enough to lift
            # a private layer into a repository the caller controls.
            if has_capability(source_caps, cap.REGISTRY_READ):
                blob = await upload_service.mount_blob(db, source, repository, mount)
                if blob is not None:
                    return Response(
                        status_code=201,
                        headers={
                            **API_VERSION_HEADER,
                            "Location": f"/v2/{name}/blobs/{blob.digest}",
                            "Docker-Content-Digest": blob.digest,
                        },
                    )
        # Fall through to an ordinary upload. The spec requires this: a failed
        # mount is not an error, it is "you will have to send the bytes".

    session = await upload_service.open_session(db, repository.name)

    digest_param = request.query_params.get("digest")
    if digest_param:
        # Monolithic upload: the whole blob arrives with the POST.
        return await _finish_upload(db, storage, session, repository, name, digest_param, request)

    return Response(
        status_code=202,
        headers={
            **API_VERSION_HEADER,
            "Location": _upload_location(name, session.id),
            "Docker-Upload-UUID": str(session.id),
            "Range": "0-0",
        },
    )


@router.patch("/v2/{name:path}/blobs/uploads/{session_id}")
async def upload_chunk(
    name: str,
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Append a chunk to an open session."""
    await _authorised_repository(db, user, name, cap.REGISTRY_WRITE)
    session = await _open_session(db, session_id)

    content_range = request.headers.get("content-range")
    if content_range:
        # The spec requires chunks to arrive in order and to start exactly where
        # the last one ended. Enforced rather than trusted: accepting a gap
        # would produce a blob that silently fails its digest much later, with
        # nothing to point at the chunk that caused it.
        try:
            start_text, _, _ = content_range.partition("-")
            start = int(start_text)
        except ValueError:
            raise OCIError(BLOB_UPLOAD_INVALID, message="malformed Content-Range") from None
        if start != session.offset:
            raise OCIError(
                BLOB_UPLOAD_INVALID,
                message=f"chunk starts at {start}, expected {session.offset}",
                detail={"expected": session.offset, "received": start},
            )

    offset = await upload_service.append_chunk(db, storage, session, request.stream())
    return Response(
        status_code=202,
        headers={
            **API_VERSION_HEADER,
            "Location": _upload_location(name, session.id),
            "Docker-Upload-UUID": str(session.id),
            # Inclusive, and empty uploads have no range to report.
            "Range": f"0-{offset - 1}" if offset else "0-0",
        },
    )


@router.put("/v2/{name:path}/blobs/uploads/{session_id}")
async def finish_upload(
    name: str,
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Complete an upload, optionally with a final chunk in the body."""
    repository = await _authorised_repository(db, user, name, cap.REGISTRY_WRITE)
    session = await _open_session(db, session_id)

    digest_param = request.query_params.get("digest")
    if not digest_param:
        raise OCIError(DIGEST_INVALID, message="digest query parameter is required")

    return await _finish_upload(db, storage, session, repository, name, digest_param, request)


@router.get("/v2/{name:path}/blobs/uploads/{session_id}")
async def upload_status(
    name: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
) -> Response:
    """How far an upload has got — what a client asks after a broken connection."""
    await _authorised_repository(db, user, name, cap.REGISTRY_WRITE)
    session = await _open_session(db, session_id)
    return Response(
        status_code=204,
        headers={
            **API_VERSION_HEADER,
            "Location": _upload_location(name, session.id),
            "Docker-Upload-UUID": str(session.id),
            "Range": f"0-{session.offset - 1}" if session.offset else "0-0",
        },
    )


@router.delete("/v2/{name:path}/blobs/uploads/{session_id}")
async def cancel_upload(
    name: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Abandon an upload and reclaim its chunks."""
    await _authorised_repository(db, user, name, cap.REGISTRY_WRITE)
    session = await _open_session(db, session_id)
    await upload_service.discard_session(db, storage, session)
    return Response(status_code=204, headers=dict(API_VERSION_HEADER))


async def _open_session(db: AsyncSession, session_id: str):
    """Load a session, rejecting an unknown or malformed id identically.

    A client cannot act differently on the two, and distinguishing them would
    say whether an id had ever existed.
    """
    try:
        parsed = uuid.UUID(session_id)
    except ValueError:
        raise OCIError(BLOB_UPLOAD_UNKNOWN, detail={"uuid": session_id}) from None
    session = await upload_service.get_session(db, parsed)
    if session is None:
        raise OCIError(BLOB_UPLOAD_UNKNOWN, detail={"uuid": session_id})
    return session


async def _finish_upload(db, storage, session, repository, name: str, digest_param: str, request):
    """Shared tail of monolithic POST and chunked PUT — both end the same way."""
    try:
        expected = parse_digest(digest_param)
    except InvalidName as exc:
        raise OCIError(DIGEST_INVALID, message=str(exc)) from exc

    # A body here is the last chunk. Absent on a PUT that only finalises.
    await upload_service.append_chunk(db, storage, session, request.stream())

    blob = await upload_service.complete_session(db, storage, session, repository, expected)
    if blob is None:
        # The bytes did not hash to what the client asserted. Content-addressed
        # storage that takes addresses on trust is not content-addressed.
        raise OCIError(
            DIGEST_INVALID,
            message="uploaded content does not match the provided digest",
            detail={"expected": str(expected)},
        )

    return Response(
        status_code=201,
        headers={
            **API_VERSION_HEADER,
            "Location": f"/v2/{name}/blobs/{blob.digest}",
            "Docker-Content-Digest": blob.digest,
        },
    )


@router.put("/v2/{name:path}/manifests/{reference}")
async def put_manifest(
    name: str,
    reference: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(authenticate_oci),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Store a manifest and, if the reference is a tag, point that tag at it.

    The manifest is the last thing a push sends, so this is where an image
    becomes real. Two things are checked before it does.

    **The digest is computed here, never taken from the client.** A manifest's
    identity is the hash of its bytes; accepting an asserted one would let a
    client register content under an address it does not hash to, which is the
    same trust boundary the blob upload enforces.

    **Every referenced blob must already be present in this repository.** The
    spec calls for `MANIFEST_BLOB_UNKNOWN`, and the reason is practical: a
    manifest that names a missing layer is an image that pulls cleanly right up
    to the point someone tries to run it. Failing the push is far kinder than
    storing a broken image.
    """
    repository = await _authorised_repository(db, user, name, cap.REGISTRY_WRITE, create=True)

    body = await request.body()
    media_type = request.headers.get("content-type", "application/vnd.oci.image.manifest.v1+json")

    try:
        parsed_reference = parse_reference(reference)
    except InvalidName as exc:
        raise OCIError(MANIFEST_INVALID, message=str(exc)) from exc

    try:
        document = json.loads(body)
    except ValueError as exc:
        raise OCIError(MANIFEST_INVALID, message="manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise OCIError(MANIFEST_INVALID, message="manifest must be a JSON object")

    computed = f"sha256:{hashlib.sha256(body).hexdigest()}"
    # A digest reference must agree with the content it names, or the client and
    # the registry disagree about what was just pushed.
    if parsed_reference.is_digest and str(parsed_reference.digest) != computed:
        raise OCIError(
            DIGEST_INVALID,
            message="manifest does not match the digest in the reference",
            detail={"computed": computed},
        )

    missing = await registry_service.missing_referenced_blobs(db, repository, document)
    if missing:
        raise OCIError(
            MANIFEST_BLOB_UNKNOWN,
            message="manifest references content not present in this repository",
            detail={"missing": missing},
        )

    manifest = await registry_service.store_manifest(
        db, storage, repository, computed, media_type, body
    )
    if parsed_reference.tag is not None:
        await registry_service.set_tag(db, repository, parsed_reference.tag, manifest)

    return Response(
        status_code=201,
        headers={
            **API_VERSION_HEADER,
            "Location": f"/v2/{name}/manifests/{computed}",
            "Docker-Content-Digest": computed,
        },
    )
