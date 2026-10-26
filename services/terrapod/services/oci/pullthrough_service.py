"""Pull-through mirroring for the OCI registry (#1408).

A client asks for an image Terrapod does not hold; Terrapod fetches it from the
upstream registry, stores it, and serves it. Every subsequent pull — including
from a cluster with no route to the internet — is served locally.

**Upstreams are an allow-list, not a convenience.** A client names the upstream
in the first path component (``terrapod.example.com/quay.io/ansible/awx-ee``
mirrors ``quay.io/ansible/awx-ee``), so without the allow-list any caller could
name any host and make Terrapod issue a request to it — a server-side request
forgery primitive with the API's own network position. Nothing outside
``registry.oci.upstreams`` is ever contacted.

**``registry.cache_only`` seals this shut**, as it does every other cache. In a
sealed deployment a miss is an error, never an upstream request — the guarantee
being that an air-gapped install cannot reach out even by accident.

**A local repository always wins.** Resolution consults the database first, so a
repository someone has pushed to shadows a mirror of the same name rather than
being silently overwritten by upstream content.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import structlog

from terrapod.config import settings
from terrapod.db.models import OCIRepository
from terrapod.http_retry import DEFAULT_RETRIES, arequest_with_retry, retry_delay_seconds
from terrapod.services.oci.names import Digest

logger = structlog.get_logger("oci.pullthrough")

#: Media types a manifest request advertises. Without these an upstream serves a
#: v2 schema-1 manifest, or refuses a manifest list — so a multi-arch image
#: would silently resolve to one architecture, or not at all.
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)

_STREAM_CHUNK = 1024 * 1024


class UpstreamUnavailable(Exception):
    """The upstream could not be reached or refused the request.

    Distinct from "not found" on purpose: a client should be able to tell a
    missing image from a registry that is down, and the router turns this into a
    502-shaped error rather than a 404 that would look like the image simply
    does not exist.
    """


class _RetryableUpstreamStatus(Exception):
    """A transient status received while a streamed OCI blob was being fetched."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"upstream returned {status_code}")


class _RetryableUpstreamTransport(Exception):
    """A transport failure from the upstream side of a storage stream."""


def _caused_by(error: BaseException, original: BaseException) -> bool:
    """Whether a storage backend wrapped an error raised by the input stream."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if current is original:
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def resolve_upstream(name: str) -> tuple[str, str] | None:
    """Split a repository name into (upstream host, upstream repository).

    Returns ``None`` when the first path component is not a configured upstream,
    which is the ordinary case for a locally pushed repository.
    """
    host, _, remainder = name.partition("/")
    if not remainder:
        return None
    for upstream in settings.registry.oci.upstreams:
        if upstream.host == host:
            return host, remainder
    return None


def _upstream(host: str):
    for upstream in settings.registry.oci.upstreams:
        if upstream.host == host:
            return upstream
    return None


def _auth(host: str) -> tuple[str, str] | None:
    """Basic-auth credentials for an upstream, or None for an anonymous pull.

    The password comes from the environment rather than configuration (see
    ``OCIUpstreamConfig``), so a private upstream needs a Secret rather than a
    credential in a ConfigMap. A username with no password is treated as
    anonymous rather than as half a credential — sending an empty password
    produces a confusing 401 from the upstream instead of the anonymous access
    that was probably intended.
    """
    upstream = _upstream(host)
    if upstream is None or not upstream.username:
        return None
    password = upstream.password
    if not password:
        logger.warning(
            "oci_upstream_username_without_password",
            host=host,
            env_var=upstream.password_env_var,
        )
        return None
    return upstream.username, password


def _base_url(host: str) -> str:
    for upstream in settings.registry.oci.upstreams:
        if upstream.host == host:
            return (upstream.api_url or f"https://{host}").rstrip("/")
    # Unreachable via resolve_upstream, but never construct a URL for an
    # unconfigured host even by accident.
    raise UpstreamUnavailable(f"{host} is not a configured upstream")


def mirroring_allowed() -> bool:
    """Whether an upstream fetch may happen at all."""
    return bool(settings.registry.oci.upstreams) and not settings.registry.cache_only


async def fetch_manifest(
    host: str,
    repository: str,
    reference: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str, str]:
    """Fetch a manifest from upstream, returning (body, media type, digest).

    The digest is computed from the bytes rather than read from the upstream's
    ``Docker-Content-Digest`` header. An upstream is not more trustworthy than a
    client, and content-addressed storage that takes addresses on trust is not
    content-addressed.
    """
    url = f"{_base_url(host)}/v2/{repository}/manifests/{reference}"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True, auth=_auth(host))
    try:
        response = await arequest_with_retry(
            client,
            "GET",
            url,
            headers={"Accept": MANIFEST_ACCEPT},
            auth=_auth(host),
            follow_redirects=True,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(f"{host}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code == 404:
        raise UpstreamUnavailable(f"{host} has no manifest {repository}:{reference}")
    if response.status_code >= 400:
        raise UpstreamUnavailable(f"{host} returned {response.status_code}")

    body = response.content
    media_type = response.headers.get("content-type", "application/vnd.oci.image.manifest.v1+json")
    return body, media_type, f"sha256:{hashlib.sha256(body).hexdigest()}"


async def fetch_blob(
    host: str,
    repository: str,
    digest: Digest,
    storage,
    storage_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Stream a blob from upstream into storage, verifying it as it passes.

    Returns its size. Streamed rather than buffered because a layer is hundreds
    of MB, and verified in the same pass so a corrupt or substituted blob is
    caught before anything is recorded — the alternative is discovering it when
    a container fails to start, with nothing pointing at the cause.
    """
    url = f"{_base_url(host)}/v2/{repository}/blobs/{digest}"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=300.0, follow_redirects=True, auth=_auth(host))
    try:
        for attempt in range(DEFAULT_RETRIES + 1):
            hasher = hashlib.new(digest.algorithm)
            size = 0
            upstream_failure: Exception | None = None

            async def _verified(hasher=hasher):
                nonlocal size, upstream_failure
                try:
                    async with client.stream(
                        "GET",
                        url,
                        auth=_auth(host),
                        follow_redirects=True,
                        timeout=300.0,
                    ) as response:
                        if response.status_code == 404:
                            raise UpstreamUnavailable(f"{host} has no blob {repository}@{digest}")
                        if response.status_code >= 500:
                            raise _RetryableUpstreamStatus(response.status_code)
                        if response.status_code >= 400:
                            raise UpstreamUnavailable(f"{host} returned {response.status_code}")
                        async for chunk in response.aiter_bytes(_STREAM_CHUNK):
                            hasher.update(chunk)
                            size += len(chunk)
                            yield chunk
                except (_RetryableUpstreamStatus, UpstreamUnavailable) as exc:
                    upstream_failure = exc
                    raise
                except httpx.RequestError as exc:
                    failure = _RetryableUpstreamTransport(str(exc))
                    upstream_failure = failure
                    raise failure from exc

            try:
                await storage.put_stream(storage_key, _verified())
            except Exception as exc:
                # Some storage drivers wrap exceptions from the source iterator
                # (Azure, for example). Retry only when the cause chain proves
                # the failure came from this upstream response, not the store.
                if upstream_failure is None or not _caused_by(exc, upstream_failure):
                    raise
                if isinstance(upstream_failure, UpstreamUnavailable):
                    if exc is upstream_failure:
                        raise
                    raise upstream_failure from exc

                if attempt == DEFAULT_RETRIES:
                    if isinstance(upstream_failure, _RetryableUpstreamStatus):
                        detail = f"{host} returned {upstream_failure.status_code}"
                    else:
                        detail = f"{host}: {upstream_failure.__cause__ or upstream_failure}"
                    raise UpstreamUnavailable(detail) from exc

                logger.warning(
                    "Retrying OCI blob after transient upstream failure",
                    host=host,
                    repository=repository,
                    attempt=attempt + 1,
                    retries=DEFAULT_RETRIES,
                    status=(
                        upstream_failure.status_code
                        if isinstance(upstream_failure, _RetryableUpstreamStatus)
                        else None
                    ),
                    error=str(upstream_failure),
                )
                await asyncio.sleep(retry_delay_seconds(attempt))
                continue

            if hasher.hexdigest() != digest.encoded:
                # The stored object is already written; the caller must not record it.
                # Left for the reaper rather than deleted inline, because a delete that
                # fails here would mask the far more interesting integrity failure.
                raise UpstreamUnavailable(
                    f"{host} served {repository}@{digest} with a mismatched digest"
                )
            return size
        raise AssertionError("retry loop exited without a result")
    finally:
        if owns_client:
            await client.aclose()


async def ensure_mirror_repository(db, name: str, host: str) -> OCIRepository:
    """Get or create the local row standing in for an upstream repository."""
    from terrapod.services.oci import registry_service

    existing = await registry_service.get_repository(db, name)
    if existing is not None:
        return existing
    repository = OCIRepository(name=name, upstream=host, labels={}, owner_email=None)
    db.add(repository)
    await db.flush()
    logger.info("oci_mirror_created", repository=name, upstream=host)
    return repository


async def current_upstream_digest(
    host: str,
    repository: str,
    tag: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """What upstream says this tag points at now, or None if it cannot be reached.

    A `HEAD` returns `Docker-Content-Digest` without the body, so confirming an
    unchanged tag — the common case by a wide margin — costs one small request
    rather than a manifest download.

    Returns None rather than raising on any failure. The caller's answer to "I
    could not check" is to serve what it already has, so an unreachable upstream
    is a normal outcome here rather than an error: a cache that stops answering
    when the internet is unavailable has failed at its one job.

    Deliberately short timeouts. This is an optimisation on the read path, and a
    slow upstream must not make a pull that could be served locally slow too.
    """
    url = f"{_base_url(host)}/v2/{repository}/manifests/{tag}"
    timeout = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0)
    owns_client = client is None
    client = client or httpx.AsyncClient(
        follow_redirects=True,
        auth=_auth(host),
        timeout=timeout,
    )

    try:
        # `auth=` rather than an Authorization header: `_auth` returns a
        # (username, password) tuple, which httpx turns into basic auth here and
        # refuses as a header value -- `TypeError: Header value must be str or
        # bytes`. That is not an `httpx.HTTPError`, so it would escape the
        # handler below, break this function's documented "returns None on any
        # failure" contract, and surface as a 500 on an ordinary image pull. It
        # only ever fired for an upstream with credentials, which is why it sat
        # here unmet while every anonymous mirror worked.
        response = await arequest_with_retry(
            client,
            "HEAD",
            url,
            headers={"Accept": MANIFEST_ACCEPT},
            auth=_auth(host),
            follow_redirects=True,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        logger.info(
            "Could not confirm mirrored tag with upstream; serving the cached image",
            host=host,
            repository=repository,
            tag=tag,
            error=str(exc),
        )
        return None
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code >= 400:
        # Includes the tag having been deleted upstream (404). Keeping what we
        # have is still right: it is the only copy on this side of the boundary,
        # and disappearing content is not an improvement.
        logger.info(
            "Upstream did not confirm mirrored tag; serving the cached image",
            host=host,
            repository=repository,
            tag=tag,
            status=response.status_code,
        )
        return None

    digest = response.headers.get("Docker-Content-Digest", "").strip()
    return digest or None


#: How many `tags/list` pages a single listing will follow. A registry chooses
#: its own page size, so the only way to be sure a listing is complete is to
#: walk to the end -- and the only way to be sure the walk terminates is a cap.
_MAX_TAG_PAGES = 20


async def upstream_tags(
    host: str,
    repository: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[str] | None:
    """Every tag upstream holds for a repository, or None if it cannot be known.

    **None means "cannot tell", and a caller must not read it as "empty".** That
    distinction is the whole point of the return type here. The execution-
    environment availability read (#2154) refuses a workspace write naming a
    line it cannot find, so an empty list and an unknown list have opposite
    consequences: empty is grounds to refuse, unknown is grounds to accept. A
    function that collapsed the two would turn an upstream hiccup into a refused
    workspace edit.

    Which is also why a **truncated** listing returns None rather than what it
    managed to collect. The spec paginates `tags/list` with a `Link` header, so a
    registry with a small page size would otherwise hand back a partial set that
    looks authoritative -- and a tag missing from a partial set is
    indistinguishable from a tag that does not exist. Hitting `_MAX_TAG_PAGES`
    is therefore "cannot tell", not "here is most of it".

    Unlike `current_upstream_digest` this has no cached answer to fall back on,
    so the timeouts are a little more generous -- but still bounded, because this
    sits behind a read an operator is waiting on in a form.
    """
    return await list_tags_at(
        _base_url(host), repository, auth=_auth(host), label=host, client=client
    )


async def list_tags_at(
    base_url: str,
    repository: str,
    *,
    auth: tuple[str, str] | None = None,
    label: str = "",
    client: httpx.AsyncClient | None = None,
) -> list[str] | None:
    """`upstream_tags` for a registry that is not a configured pull-through upstream.

    Separate because the two have different reasons to exist, and conflating
    them would weaken one of them. `registry.oci.upstreams` is an allow-list
    against a **client** naming a host -- that is an SSRF control and must stay
    closed by default. A base URL an operator put in their own Helm values is
    not that: it is the same shape as the eight `*_mirror_url` settings the
    binary cache already fetches from, and refusing to read it would mean a
    deployment could not discover its own execution environments without also
    opening a client-facing mirror it may not want.

    So the caller supplies the base URL and owns having obtained it from
    configuration rather than from a request. `registry.cache_only` still seals
    this shut, as it does every other outbound read.
    """
    url: str | None = f"{base_url.rstrip('/')}/v2/{repository}/tags/list?n=100"
    collected: list[str] = []
    host = label or base_url
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
    owns_client = client is None
    client = client or httpx.AsyncClient(
        follow_redirects=True,
        auth=auth,
        timeout=timeout,
    )

    try:
        for _ in range(_MAX_TAG_PAGES):
            response = await arequest_with_retry(
                client,
                "GET",
                url,
                headers={"Accept": "application/json"},
                auth=auth,
                follow_redirects=True,
                timeout=timeout,
            )
            if response.status_code == 404:
                # The repository genuinely has no tags upstream. Distinct
                # from unreachable: this one IS an answer.
                return []
            if response.status_code >= 400:
                logger.info(
                    "Upstream refused a tag listing",
                    host=host,
                    repository=repository,
                    status=response.status_code,
                )
                return None

            body = response.json()
            tags = body.get("tags") if isinstance(body, dict) else None
            # `tags: null` is legal in the spec for an empty repository, and
            # is not a malformed response to warn about.
            if isinstance(tags, list):
                collected.extend(t for t in tags if isinstance(t, str))

            url = _next_page(response, base_url.rstrip("/"))
            if url is None:
                return collected
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers a non-JSON body: a proxy error page is a failure to
        # reach the registry, not a repository with no tags.
        logger.info(
            "Could not list tags upstream",
            host=host,
            repository=repository,
            error=str(exc),
        )
        return None
    finally:
        if owns_client:
            await client.aclose()

    logger.warning(
        "Upstream tag listing exceeded the page cap; treating it as unknown",
        host=host,
        repository=repository,
        pages=_MAX_TAG_PAGES,
        collected=len(collected),
    )
    return None


def _next_page(response: httpx.Response, base: str) -> str | None:
    """The `Link: <...>; rel="next"` target of a paginated listing, absolutised.

    The spec says the `Link` value is opaque and must be used as given, so the
    query string is never reconstructed from the tags we received -- a registry
    is free to use a cursor that has nothing to do with tag names.

    A relative target is resolved against the configured upstream base rather
    than against the response's own URL, so a registry that returned an absolute
    `Link` pointing somewhere else cannot redirect the walk off-host.
    """
    link = response.headers.get("Link", "")
    for part in link.split(","):
        segments = part.split(";")
        if not any('rel="next"' in s.replace(" ", "") or "rel=next" in s for s in segments[1:]):
            continue
        target = segments[0].strip().strip("<>")
        if not target:
            return None
        if target.startswith(base):
            return target
        if target.startswith("/"):
            return f"{base}{target}"
        # Absolute and pointing elsewhere: refuse to follow it.
        logger.warning("Ignoring an off-host Link in a tag listing", base=base, target=target)
        return None
    return None
