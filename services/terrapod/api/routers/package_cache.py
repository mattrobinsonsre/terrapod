"""Language package registry proxies — PyPI and npm (#1417).

Point a runner at these with `PIP_INDEX_URL` and `NPM_CONFIG_REGISTRY` and its
dependency closure resolves without touching the internet.

**Why these live under `/api/terrapod/v1/` and the OCI registry does not.** The
distribution spec mandates `/v2/` at the root; nothing here is mandated, because
both clients take their registry URL as configuration. Staying on the native
surface means the BFF's existing `/api/*` route already proxies them — no new
prefix, no new ingress rule, and none of the trailing-slash handling that the
mandated prefix cost us in #1408.

**Index URLs are rewritten; integrity metadata is not.** A client checks our
bytes against the digest upstream published, which is the whole security model
(see :mod:`terrapod.services.package_cache`).

**No client-supplied URL is ever fetched.** A cold artifact is resolved by
re-reading the project's index from the *configured* upstream and matching the
filename. Encoding the upstream URL into the rewritten link would save that
request and turn this into a server-side request forgery primitive.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.credentials import extract_credential
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import registry_collection_service as collections
from terrapod.services.engine_gating import capability_enabled
from terrapod.services.package_cache import galaxy, npm, pypi
from terrapod.services.package_cache.substrate import (
    Artifact,
    NotFoundUpstream,
    SealedError,
    UpstreamError,
    cached_filenames,
    cached_files,
    get_or_fetch,
    load_document,
    lookup_present,
    sealed,
    store_document,
)
from terrapod.storage import get_storage, keys
from terrapod.storage.protocol import ObjectStore

#: Split by ecosystem so each can be left unmounted independently. See
#: `build_router` — a disabled ecosystem is not registered at all.
pypi_router = APIRouter()
npm_router = APIRouter()
galaxy_router = APIRouter()
logger = get_logger(__name__)

#: pip has no bearer option — credentials come from the index URL or `.netrc` —
#: so the challenge has to name Basic or pip will not retry with them.
_CHALLENGE = 'Basic realm="terrapod"'


async def authenticate_package_request(request: Request) -> AuthenticatedUser:
    """Authenticate a package-cache request, accepting Basic or Bearer.

    pip sends Basic and npm sends Bearer, so both are accepted here for the same
    credential set as everywhere else. Its own short-lived DB session, so nothing
    is held open across an artifact transfer.
    """
    from terrapod.api.dependencies import PEER_KIND, _resolve_user_roles, validate_api_token
    from terrapod.auth.runner_tokens import verify_runner_token
    from terrapod.auth.sessions import get_session
    from terrapod.db.session import get_db_session

    def _unauthorised() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": _CHALLENGE},
        )

    # ansible-galaxy sends `Token <value>`; pip sends Basic and npm Bearer. All
    # three carry the same credential, so all three are accepted here — and only
    # here, since the OCI surface deliberately does not honour `Token` (#1482).
    token = extract_credential(request, allow_token_scheme=True)
    if not token:
        raise _unauthorised()

    if token.startswith("runtok:"):
        run_id = verify_runner_token(token)
        if run_id is not None:
            request.state.user_email = "runner"
            return AuthenticatedUser(
                email="runner",
                display_name="Runner Job",
                roles=["everyone"],
                provider_name="runner_token",
                auth_method="runner_token",
                run_id=run_id,
            )

    async with get_db_session() as db:
        api_token = await validate_api_token(db, token)
        if api_token is not None:
            # A replication peer is not a user and must not act as one here.
            if api_token.kind == PEER_KIND:
                raise _unauthorised()
            email = api_token.bound_to or ""
            roles = await _resolve_user_roles(db, email) if email else []
            request.state.user_email = email
            return AuthenticatedUser(
                email=email,
                display_name=None,
                roles=roles,
                provider_name="api_token",
                auth_method="api_token",
                kind=api_token.kind,
                pinned_roles=api_token.pinned_roles,
            )

    session = await get_session(token)
    if session is not None:
        request.state.user_email = session.email
        return AuthenticatedUser(
            email=session.email,
            display_name=session.display_name,
            roles=session.roles,
            provider_name=session.provider_name,
            auth_method="session",
        )

    raise _unauthorised()


def _upstream_failure(exc: Exception) -> HTTPException:
    """Map a fetch failure onto a status a client can act on.

    Sealing is a 503 with the operator's own configuration named in it, not a
    404: the package may well exist, and telling a developer it does not sends
    them to look in entirely the wrong place.
    """
    if isinstance(exc, SealedError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, NotFoundUpstream):
        return HTTPException(status_code=404, detail="Not found")
    return HTTPException(status_code=502, detail=str(exc))


# ── PyPI ────────────────────────────────────────────────────────────────────
#
# Files are served from *under* the index path, so the rewritten link can be the
# bare filename. PEP 691 and PEP 503 both resolve a relative URL against the
# index page, which means the proxy needs no knowledge of its own external URL —
# and therefore cannot be misconfigured into emitting links nobody can reach.


@pypi_router.get("/pypi/simple/{project}/")
async def pypi_index(
    project: str,
    request: Request,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The simple index for a project, with file links pointed at us."""
    normalised = pypi.normalise(project)

    if sealed():
        # A sealed node cannot ask upstream what versions exist, so it answers
        # with what it holds. Anything else makes an air-gapped install
        # impossible even for packages warmed for exactly that purpose.
        files = await cached_files(db, pypi.ECOSYSTEM, normalised)
        if not files:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{project} is not cached and this node is sealed "
                    f"(registry.cache_only). Warm it before sealing."
                ),
            )
        document = pypi.index_from_cache(files)
        document["name"] = normalised
    else:
        try:
            upstream = await pypi.fetch_index(project)
        except (NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc
        document = pypi.rewrite_json(upstream, project, file_url_base="")
        # Relative to the index page: just the filename.
        for entry in document["files"]:
            entry["url"] = entry["url"].lstrip("/").split("/")[-1]

    accept = request.headers.get("accept", "")
    if "json" in accept or not accept or "*/*" in accept:
        return JSONResponse(
            content=document,
            media_type="application/vnd.pypi.simple.v1+json",
        )
    return Response(content=pypi.render_html(document), media_type="text/html")


@pypi_router.get("/pypi/simple/{project}/{filename}")
async def pypi_file(
    project: str,
    filename: str,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Serve an artifact, caching it from upstream on a miss."""
    normalised = pypi.normalise(project)

    record = await lookup_present(db, storage, pypi.ECOSYSTEM, normalised, filename)
    if record is None:
        # Resolve through the configured upstream's index — never through
        # anything the caller handed us.
        try:
            index = await pypi.fetch_index(project)
        except (NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc

        # A PEP 658 sidecar describes a file in the index rather than being one,
        # so it is resolved from that file's entry.
        base_filename, wants_metadata = pypi.is_metadata_request(filename)
        entry = pypi.find_file(index, base_filename)
        if entry is None:
            raise HTTPException(status_code=404, detail="Not found")
        artifact = (
            pypi.metadata_artifact_for(project, entry)
            if wants_metadata
            else pypi.artifact_for(project, entry)
        )
        try:
            record = await get_or_fetch(db, storage, artifact)
        except (SealedError, NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc

    return await _redirect_to_object(storage, record.storage_key)


# ── npm ─────────────────────────────────────────────────────────────────────
#
# `dist.tarball` must be absolute — npm does not resolve it relative to the
# packument — so this half does need to know its own external URL.


def _npm_base(request: Request) -> str:
    """The absolute base URL for rewritten tarball links.

    npm does not resolve `dist.tarball` relative to the packument, so unlike the
    PyPI half this one has to know its own external address. Three sources, in
    descending order of how much they can be trusted:

    1. **`external_url`** — the operator's explicit answer, correct however many
       proxies sit in front. Set it; everything else is a fallback.
    2. **`X-Forwarded-Host` / `-Proto`** — what the BFF and any ingress report the
       client asked for. This is how Verdaccio and other registries solve the same
       problem, and it makes a deployment that has not set `external_url` work
       rather than emit links to an internal hostname nobody can resolve.
    3. **The request's own base URL** — direct access, no proxy.

    On (2): a client can forge `X-Forwarded-Host` and receive a packument pointing
    at a host of its choosing. That is tolerable here because the packument is not
    cached and is returned only to the caller that sent the header — they can
    mislead themselves and no one else — and because (1) takes precedence, so a
    deployment that sets `external_url` is not exposed to it at all.
    """
    configured = (settings.external_url or "").strip().rstrip("/")
    if configured:
        base = configured
    else:
        host = request.headers.get("x-forwarded-host")
        if host:
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            base = f"{proto}://{host}"
        else:
            base = str(request.base_url).rstrip("/")
    return f"{base.rstrip('/')}/api/terrapod/v1/package-cache/npm"


@npm_router.get("/npm/{package:path}/-/{filename}")
async def npm_tarball(
    package: str,
    filename: str,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Serve a package tarball, caching it from upstream on a miss.

    Declared before the packument route: `{package:path}` is greedy and
    backtracks to the last matching suffix, so `@scope/pkg/-/pkg-1.0.0.tgz`
    resolves to package `@scope/pkg` and this filename.
    """
    name = npm.normalise(package)

    record = await lookup_present(db, storage, npm.ECOSYSTEM, name, filename)
    if record is None:
        try:
            packument = await npm.fetch_packument(name)
        except (NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc
        found = npm.find_version(packument, filename)
        if found is None:
            raise HTTPException(status_code=404, detail="Not found")
        version, entry = found
        try:
            record = await get_or_fetch(db, storage, npm.artifact_for(name, version, entry))
        except (SealedError, NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc

    return await _redirect_to_object(storage, record.storage_key)


@npm_router.get("/npm/{package:path}")
async def npm_packument(
    package: str,
    request: Request,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """The packument for a package, with tarball URLs pointed at us."""
    name = npm.normalise(package)

    if sealed():
        # The packument is cached alongside the tarballs precisely for this: a
        # sealed node cannot fetch one, and without it npm has no dependency
        # ranges and cannot resolve anything at all.
        raw = await load_document(db, storage, npm.ECOSYSTEM, name, npm.PACKUMENT_FILENAME)
        if raw is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{package} is not cached and this node is sealed "
                    f"(registry.cache_only). Warm it before sealing."
                ),
            )
        held = set(await cached_filenames(db, npm.ECOSYSTEM, name))
        packument = npm.restrict_to_cached(json.loads(raw), held)
        return JSONResponse(content=npm.rewrite(packument, _npm_base(request)))

    # Pass the client's own Accept through, so an install stays on the small
    # abbreviated document rather than pulling megabytes it will not read.
    accept = request.headers.get("accept")
    try:
        packument = await npm.fetch_packument(
            name, accept=accept if accept and "npm.install" in accept else None
        )
    except (NotFoundUpstream, UpstreamError) as exc:
        raise _upstream_failure(exc) from exc

    # Keep a copy so this package stays installable once the node is sealed.
    # Best-effort: failing to cache the document must not fail a request that is
    # otherwise about to succeed.
    try:
        await store_document(
            db, storage, npm.packument_artifact(name), json.dumps(packument).encode()
        )
    except Exception:
        logger.warning("Could not cache packument", package=name, exc_info=True)

    return JSONResponse(content=npm.rewrite(packument, _npm_base(request)))


# ── Ansible Galaxy ──────────────────────────────────────────────────────────


def _galaxy_base(request: Request) -> str:
    """The absolute base URL for rewritten Galaxy links.

    Same three sources, same precedence and same caveat as :func:`_npm_base` —
    `external_url` first, then the forwarded headers, then the request's own base
    — because ansible-galaxy resolves nothing relative to the document either.

    Unlike npm's packument these documents ARE cached, so a forged
    `X-Forwarded-Host` could in principle be stored. It cannot: only upstream's
    document is cached, and rewriting happens on the way out, per request.
    """
    configured = (settings.external_url or "").strip().rstrip("/")
    if configured:
        base = configured
    else:
        host = request.headers.get("x-forwarded-host")
        if host:
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            base = f"{proto}://{host}"
        else:
            base = str(request.base_url).rstrip("/")
    return f"{base.rstrip('/')}/api/terrapod/v1/package-cache/galaxy"


def _galaxy_names(namespace: str, name: str) -> None:
    """Reject anything that is not a plain collection name segment.

    These are interpolated into an upstream URL, so the check is a boundary and
    not a nicety: `..` or a scheme here would let a caller choose where we fetch
    from, which is the request-forgery hole the single configured upstream exists
    to close.
    """
    if not galaxy.valid_segment(namespace) or not galaxy.valid_segment(name):
        raise HTTPException(status_code=404, detail="Not found")


@galaxy_router.get("/galaxy/")
async def galaxy_root(
    user: AuthenticatedUser = Depends(authenticate_package_request),
) -> Response:
    """Version discovery — the first request ansible-galaxy makes.

    Served rather than proxied: it advertises which API versions *this* server
    offers, and we offer v3 whatever upstream happens to say.
    """
    return JSONResponse(content={"description": "Terrapod", "available_versions": {"v3": "v3/"}})


@galaxy_router.get("/galaxy/v3/collections/{namespace}/{name}/")
async def galaxy_collection(
    namespace: str,
    name: str,
    request: Request,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Collection detail, with every link pointed back at us."""
    _galaxy_names(namespace, name)
    key = galaxy.collection_key(namespace, name)
    base = _galaxy_base(request)

    # A published collection is answered from our own registry and never proxied.
    # Checking first also means a private name cannot be shadowed by an upstream
    # one that happens to match.
    published = await collections.list_versions(db, namespace, name)
    if published:
        newest = published[-1].version
        return JSONResponse(
            content={
                "href": f"{base}/v3/collections/{namespace}/{name}/",
                "namespace": namespace,
                "name": name,
                "deprecated": False,
                "versions_url": f"{base}/v3/collections/{namespace}/{name}/versions/",
                "highest_version": {
                    "version": newest,
                    "href": f"{base}/v3/collections/{namespace}/{name}/versions/{newest}/",
                },
            }
        )

    if sealed():
        raw = await load_document(db, storage, galaxy.ECOSYSTEM, key, galaxy.COLLECTION_DOC)
        if raw is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{key} is not cached and this node is sealed "
                    f"(registry.cache_only). Warm it before sealing."
                ),
            )
        document = json.loads(raw)
    else:
        try:
            document = await galaxy.fetch_collection(namespace, name)
        except (NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc
        # Keep a copy so the collection stays installable once sealed.
        # Best-effort: failing to cache must not fail a request about to succeed.
        try:
            await store_document(
                db,
                storage,
                Artifact(
                    ecosystem=galaxy.ECOSYSTEM,
                    name=key,
                    version="",
                    filename=galaxy.COLLECTION_DOC,
                    upstream_url="",
                    content_type="application/json",
                ),
                json.dumps(document).encode(),
            )
        except Exception:
            logger.warning("Could not cache collection detail", collection=key, exc_info=True)

    return JSONResponse(content=galaxy.rewrite_collection(document, base, namespace, name))


@galaxy_router.get("/galaxy/v3/collections/{namespace}/{name}/versions/")
async def galaxy_versions(
    namespace: str,
    name: str,
    request: Request,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Every version of a collection, as one page.

    Sealed, the list is narrowed to versions whose artifact we actually hold: a
    node that advertises a version it cannot serve sends the client down a path
    that 404s after it has already resolved, when it could have been offered the
    newest version that does exist here.
    """
    _galaxy_names(namespace, name)
    key = galaxy.collection_key(namespace, name)
    base = _galaxy_base(request)

    published = await collections.list_versions(db, namespace, name)
    if published:
        prefix = f"{base}/v3/collections/{namespace}/{name}/versions"
        return JSONResponse(
            content={
                "meta": {"count": len(published)},
                "links": {
                    "first": f"{prefix}/",
                    "previous": None,
                    "next": None,
                    "last": f"{prefix}/",
                },
                "data": [
                    {"version": row.version, "href": f"{prefix}/{row.version}/"}
                    for row in published
                ],
            }
        )

    if sealed():
        raw = await load_document(db, storage, galaxy.ECOSYSTEM, key, galaxy.VERSIONS_DOC)
        if raw is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{key} is not cached and this node is sealed "
                    f"(registry.cache_only). Warm it before sealing."
                ),
            )
        held = galaxy.versions_held(
            await cached_filenames(db, galaxy.ECOSYSTEM, key), namespace, name
        )
        document = galaxy.restrict_to_cached(json.loads(raw), held)
    else:
        try:
            document = await galaxy.fetch_versions(namespace, name)
        except (NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc
        try:
            await store_document(
                db,
                storage,
                Artifact(
                    ecosystem=galaxy.ECOSYSTEM,
                    name=key,
                    version="",
                    filename=galaxy.VERSIONS_DOC,
                    upstream_url="",
                    content_type="application/json",
                ),
                json.dumps(document).encode(),
            )
        except Exception:
            logger.warning("Could not cache version list", collection=key, exc_info=True)

    return JSONResponse(content=galaxy.rewrite_versions(document, base, namespace, name))


@galaxy_router.get("/galaxy/v3/collections/{namespace}/{name}/versions/{version}/")
async def galaxy_version(
    namespace: str,
    name: str,
    version: str,
    request: Request,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Version detail — where the client learns how to fetch and how to check.

    `download_url` is rewritten to us. Leaving it as upstream's would produce an
    install that resolves through Terrapod and then downloads from the internet,
    which looks identical to working until someone has no route out.
    """
    _galaxy_names(namespace, name)
    if not galaxy.valid_version(version):
        raise HTTPException(status_code=404, detail="Not found")
    key = galaxy.collection_key(namespace, name)
    base = _galaxy_base(request)

    row = await collections.get_version(db, namespace, name, version)
    if row is not None:
        return JSONResponse(content=_published_version_json(row, base, namespace, name, version))

    if sealed():
        raw = await load_document(db, storage, galaxy.ECOSYSTEM, key, galaxy.version_doc(version))
        if raw is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{key}:{version} is not cached and this node is sealed "
                    f"(registry.cache_only). Warm it before sealing."
                ),
            )
        document = json.loads(raw)
    else:
        try:
            document = await galaxy.fetch_version(namespace, name, version)
        except (NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc
        try:
            await store_document(
                db,
                storage,
                Artifact(
                    ecosystem=galaxy.ECOSYSTEM,
                    name=key,
                    version=version,
                    filename=galaxy.version_doc(version),
                    upstream_url="",
                    content_type="application/json",
                ),
                json.dumps(document).encode(),
            )
        except Exception:
            logger.warning(
                "Could not cache version detail", collection=key, version=version, exc_info=True
            )

    return JSONResponse(content=galaxy.rewrite_version(document, base, namespace, name, version))


@galaxy_router.get(
    "/galaxy/v3/collections/{namespace}/{name}/versions/{version}/download/{filename}"
)
async def galaxy_download(
    namespace: str,
    name: str,
    version: str,
    filename: str,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """The collection artifact.

    The filename in the path is checked against the one this coordinate implies
    rather than trusted — it is decoration for the client's benefit, and treating
    it as an input would let it name a file other than the collection requested.

    A cold artifact is resolved by re-reading the version detail from the
    *configured* upstream and using the `download_url` it gives, never a URL a
    client supplied.
    """
    _galaxy_names(namespace, name)
    if not galaxy.valid_version(version):
        raise HTTPException(status_code=404, detail="Not found")
    if filename != galaxy.artifact_filename(namespace, name, version):
        raise HTTPException(status_code=404, detail="Not found")

    published = await collections.get_version(db, namespace, name, version)
    if published is not None:
        return await _redirect_to_object(
            storage, keys.collection_tarball_key(namespace, name, version)
        )

    key = galaxy.collection_key(namespace, name)
    record = await lookup_present(db, storage, galaxy.ECOSYSTEM, key, filename)
    if record is None:
        if sealed():
            raise _upstream_failure(
                SealedError(
                    f"{key}:{version} is not cached and this node is sealed "
                    f"(registry.cache_only). Warm it before sealing."
                )
            )
        try:
            detail = await galaxy.fetch_version(namespace, name, version)
            record = await get_or_fetch(
                db, storage, galaxy.artifact_for(namespace, name, version, detail)
            )
        except (SealedError, NotFoundUpstream, UpstreamError) as exc:
            raise _upstream_failure(exc) from exc

    return await _redirect_to_object(storage, record.storage_key)


# ── Ansible Galaxy: publishing ──────────────────────────────────────────────


def _published_version_json(row, base: str, namespace: str, name: str, version: str) -> dict:
    """A published version rendered in the shape the client expects.

    Built to the same contract as a proxied one — `href`, `download_url` and
    `artifact.sha256` all present and all ours — so the installer cannot tell
    the two apart, which is the point: a private collection installs exactly
    like a public one.
    """
    manifest = row.manifest or {}
    return {
        "version": version,
        "href": f"{base}/v3/collections/{namespace}/{name}/versions/{version}/",
        "namespace": {"name": namespace},
        "collection": {"name": name},
        "artifact": {
            "filename": galaxy.artifact_filename(namespace, name, version),
            "sha256": row.artifact_sha256,
            "size": row.size,
        },
        "download_url": (
            f"{base}/v3/collections/{namespace}/{name}/versions/{version}/download/"
            f"{galaxy.artifact_filename(namespace, name, version)}"
        ),
        # Straight from the archive's MANIFEST.json. Dependency resolution reads
        # this, so a published collection whose dependencies were dropped would
        # resolve as though it had none — a wrong answer rather than an error.
        "metadata": {
            "dependencies": manifest.get("dependencies") or {},
            "tags": manifest.get("tags") or [],
            "description": manifest.get("description") or "",
        },
        "requires_ansible": manifest.get("requires_ansible") or "",
        # Advertised only once a signature has been verified against a
        # registered key. An empty list is the honest answer for an unsigned
        # collection — the client then installs it unless `--keyring` demands
        # otherwise, rather than failing against a signature we cannot vouch for.
        "signatures": (
            [
                {
                    "signature": row.signature,
                    "pubkey_fingerprint": row.signing_key_id,
                    "signing_service": None,
                }
            ]
            if row.signature
            else []
        ),
    }


@galaxy_router.post("/galaxy/v3/artifacts/collections/", status_code=202)
async def galaxy_publish(
    request: Request,
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Accept `ansible-galaxy collection publish`.

    The client sends one multipart POST carrying only the tarball, so every fact
    about the collection is read out of the archive rather than taken from the
    request — there is nowhere else for it to come from, and trusting a
    client-supplied coordinate would let one publisher write into another's
    namespace.

    The part is re-streamed to the ephemeral PVC rather than read into memory:
    Starlette rolls a large multipart part over to the RAM-backed `/tmp`, which
    is what rule 14 exists to avoid.

    Responds 202 with a task URL. The client does not follow that URL — it takes
    its last path segment as an import id and polls a fixed path (see
    `docs/galaxy-cli-surface.md`), so the segment is the version's own id.
    """
    from terrapod.api.upload_stream import stream_upload_to_tempfile

    tmp_path, size = await stream_upload_to_tempfile(file, suffix=".collection.tar.gz")
    try:
        if size == 0:
            raise HTTPException(status_code=400, detail="Empty upload")
        try:
            row = await collections.publish(db, storage, tmp_path, owner_email=user.email or "")
        except collections.PublishError as exc:
            # 400 with the archive's own problem named: the client prints this,
            # and "invalid request" would send someone to look at their command
            # rather than their tarball.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.commit()
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    base = _galaxy_base(request)
    return JSONResponse(
        status_code=202,
        content={"task": f"{base}/v3/imports/collections/{row.id}/"},
    )


@galaxy_router.put("/galaxy/v3/collections/{namespace}/{name}/versions/{version}/signature")
async def galaxy_attach_signature(
    namespace: str,
    name: str,
    version: str,
    request: Request,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Attach a detached signature to a published collection. Terrapod-native.

    Not part of the surface `ansible-galaxy` drives: `collection publish` sends
    the tarball and nothing else, so there is nowhere in that protocol to put a
    signature (see `docs/galaxy-cli-surface.md`). Signing is therefore a second,
    deliberate step here — as it is in Galaxy NG, where signatures come from a
    signing service rather than the publish call.

    The body is the ASCII-armored detached signature over the collection's
    `MANIFEST.json`. It is verified against a key already registered with this
    platform and refused with 422 otherwise; the server never re-signs.
    """
    _galaxy_names(namespace, name)
    if not galaxy.valid_version(version):
        raise HTTPException(status_code=404, detail="Not found")

    sig_bytes = await request.body()
    if not sig_bytes:
        raise HTTPException(status_code=400, detail="Empty signature")

    try:
        row = await collections.attach_signature(db, storage, namespace, name, version, sig_bytes)
    except collections.SignatureError as exc:
        # 422, matching the provider registry: the request was well-formed and
        # the content is what we decline to vouch for.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()

    return JSONResponse(
        content={
            "namespace": namespace,
            "name": name,
            "version": version,
            "pubkey_fingerprint": row.signing_key_id,
        }
    )


@galaxy_router.get("/galaxy/v3/imports/collections/{import_id}/")
async def galaxy_import_status(
    import_id: str,
    user: AuthenticatedUser = Depends(authenticate_package_request),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The import poll `ansible-galaxy` makes after publishing.

    Publishing here is synchronous — the archive is validated and stored inside
    the POST — so by the time this is reachable the import has either happened
    or the POST already failed. The row existing IS the completed state, which
    is why nothing extra is stored or expired to answer this.
    """
    row = await collections.get_version_by_id(db, import_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(
        content={
            "state": "completed",
            "finished_at": row.created_at.isoformat().replace("+00:00", "Z"),
        }
    )


async def _redirect_to_object(storage: ObjectStore, key: str) -> Response:
    """302 to a presigned URL for the stored artifact.

    Redirecting rather than proxying the bytes keeps a multi-hundred-megabyte
    wheel off the API pod entirely — the same thing the binary cache and provider
    mirror do, and the reason a large install does not scale with API replicas.
    """
    presigned = await storage.presigned_get_url(key)
    return RedirectResponse(url=presigned.url, status_code=302)


def build_router() -> APIRouter | None:
    """The package-cache routes for the ecosystems this deployment offers (#1429).

    Returns None when none of them are, so the application mounts nothing at all —
    the routes do not exist, rather than existing and refusing. That is the
    difference between a surface an operator has switched off and one that answers
    every request with a 404 while still appearing in the schema, and it is what
    keeps a terraform-only install free of endpoints it has no use for.

    A factory rather than mutating a module-level router, so building the
    application twice in one process (which the tests do constantly) cannot
    accumulate duplicate routes.
    """
    aggregate = APIRouter(prefix="/package-cache", tags=["package-cache"])
    mounted = False
    if capability_enabled("pypi"):
        aggregate.include_router(pypi_router)
        mounted = True
    if capability_enabled("npm"):
        aggregate.include_router(npm_router)
        mounted = True
    if capability_enabled("galaxy"):
        aggregate.include_router(galaxy_router)
        mounted = True
    return aggregate if mounted else None
