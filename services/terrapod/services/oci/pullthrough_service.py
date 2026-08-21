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

import hashlib

import httpx
import structlog

from terrapod.config import settings
from terrapod.db.models import OCIRepository
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


async def fetch_manifest(host: str, repository: str, reference: str) -> tuple[bytes, str, str]:
    """Fetch a manifest from upstream, returning (body, media type, digest).

    The digest is computed from the bytes rather than read from the upstream's
    ``Docker-Content-Digest`` header. An upstream is not more trustworthy than a
    client, and content-addressed storage that takes addresses on trust is not
    content-addressed.
    """
    url = f"{_base_url(host)}/v2/{repository}/manifests/{reference}"
    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, auth=_auth(host)
        ) as client:
            response = await client.get(url, headers={"Accept": MANIFEST_ACCEPT})
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(f"{host}: {exc}") from exc

    if response.status_code == 404:
        raise UpstreamUnavailable(f"{host} has no manifest {repository}:{reference}")
    if response.status_code >= 400:
        raise UpstreamUnavailable(f"{host} returned {response.status_code}")

    body = response.content
    media_type = response.headers.get("content-type", "application/vnd.oci.image.manifest.v1+json")
    return body, media_type, f"sha256:{hashlib.sha256(body).hexdigest()}"


async def fetch_blob(host: str, repository: str, digest: Digest, storage, storage_key: str) -> int:
    """Stream a blob from upstream into storage, verifying it as it passes.

    Returns its size. Streamed rather than buffered because a layer is hundreds
    of MB, and verified in the same pass so a corrupt or substituted blob is
    caught before anything is recorded — the alternative is discovering it when
    a container fails to start, with nothing pointing at the cause.
    """
    url = f"{_base_url(host)}/v2/{repository}/blobs/{digest}"
    hasher = hashlib.new(digest.algorithm)
    size = 0

    async def _verified():
        nonlocal size
        async with httpx.AsyncClient(
            timeout=300.0, follow_redirects=True, auth=_auth(host)
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise UpstreamUnavailable(f"{host} returned {response.status_code}")
                async for chunk in response.aiter_bytes(_STREAM_CHUNK):
                    hasher.update(chunk)
                    size += len(chunk)
                    yield chunk

    try:
        await storage.put_stream(storage_key, _verified())
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(f"{host}: {exc}") from exc

    if hasher.hexdigest() != digest.encoded:
        # The stored object is already written; the caller must not record it.
        # Left for the reaper rather than deleted inline, because a delete that
        # fails here would mask the far more interesting integrity failure.
        raise UpstreamUnavailable(f"{host} served {repository}@{digest} with a mismatched digest")
    return size


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
