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

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.credentials import extract_credential
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.engine_gating import capability_enabled
from terrapod.services.package_cache import npm, pypi
from terrapod.services.package_cache.substrate import (
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
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

#: Split by ecosystem so each can be left unmounted independently. See
#: `build_router` — a disabled ecosystem is not registered at all.
pypi_router = APIRouter()
npm_router = APIRouter()
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

    token = extract_credential(request)
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
    return aggregate if mounted else None
