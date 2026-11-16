"""The NuGet V3 protocol, proxied (#1484).

Three requests, captured from a real `dotnet restore`
(`scripts/nuget-capture.py`):

    /index.json                                   the service index   MUTABLE
    /flat/{id}/index.json                         what versions exist MUTABLE
    /flat/{id}/{version}/{id}.{version}.nupkg     the package         immutable

Only the two listings need a bound; a package at a version cannot change.

**The service index is the part that has to be built per request, not stored.**
It advertises absolute URLs which the client then follows verbatim — so they
must carry this deployment's own external base *and* the path prefix this proxy
is mounted under. Getting that wrong is not a subtle failure: the capture showed
`dotnet restore` dutifully following an advertised URL to a host it could not
reach and giving up, which is exactly what a hardcoded root path would cause.

Package ids are **lowercased** in every path. The client does it before asking,
and doing it here too means `Newtonsoft.Json` and `newtonsoft.json` are one
cache entry rather than two copies of identical bytes.
"""

from __future__ import annotations

import re

import httpx

from terrapod.config import settings
from terrapod.http_retry import arequest_with_retry
from terrapod.services.package_cache.substrate import (
    Artifact,
    CoordinateError,
    Coordinates,
    NotFoundUpstream,
    UpstreamError,
)

ECOSYSTEM = "nuget"

_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)

#: A package id. NuGet allows letters, digits, and `.`, `-`, `_`.
_ID = re.compile(r"^[A-Za-z0-9._-]+$")

#: A NuGet version: `1.0.0`, `2.1.0-beta.1`, `1.0.0+meta`, and the 4-part form.
_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+-]*$")

#: The versions document, stored under this name so the substrate can hold it.
VERSIONS_DOC = "index.json"


def normalise(package_id: str) -> str:
    """Lowercase, which is how the protocol addresses a package."""
    return package_id.lower()


def valid_id(package_id: str) -> bool:
    return bool(_ID.match(package_id)) and ".." not in package_id


def valid_version(version: str) -> bool:
    return bool(_VERSION.match(version))


def upstream_base() -> str:
    return settings.registry.package_cache.nuget.upstream.rstrip("/")


def service_index(base: str) -> dict:
    """The service index, built for the caller that asked for it.

    `base` is this deployment's external URL **including this proxy's path
    prefix**, resolved per request. It is not stored, because a stored index
    would pin whatever host happened to fetch it first and hand that to
    everyone else.

    Only `PackageBaseAddress` is advertised. It is what `restore` uses to
    resolve versions and download packages, and advertising a resource we do
    not serve would send the client somewhere that 404s at a point it has
    already committed.
    """
    return {
        "version": "3.0.0",
        "resources": [
            {
                "@id": f"{base}/flat/",
                "@type": "PackageBaseAddress/3.0.0",
                "comment": "Base URL of where NuGet packages are stored.",
            }
        ],
    }


async def fetch_versions(
    package_id: str, *, client: httpx.AsyncClient | None = None, base: str | None = None
) -> bytes:
    """The upstream versions document for a package, as raw bytes.

    Not parsed: it is `{"versions": [...]}` and nothing here inspects it, so
    passing the bytes through means a future field cannot be lost in a
    re-serialisation.
    """
    lower = normalise(package_id)
    url = f"{(base or upstream_base()).rstrip('/')}/{lower}/index.json"
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await arequest_with_retry(client, "GET", url)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach {url}: {exc}") from exc
    finally:
        if owns:
            await client.aclose()

    if response.status_code == 404:
        raise NotFoundUpstream(package_id)
    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned {response.status_code} for {url}")
    return response.content


def versions_artifact(package_id: str) -> Artifact:
    """The cache entry for a versions document, so a sealed node still resolves."""
    lower = normalise(package_id)
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=lower,
        version="",
        filename=VERSIONS_DOC,
        upstream_url="",
        content_type="application/json",
    )


def index_coordinates(package_id: str) -> Coordinates:
    """Where an operator-uploaded NuGet versions index is served from.

    A sealed node answers the flat container's version list from this document, so an
    uploaded `.nupkg` without one is present and unresolvable -- `dotnet restore` is
    told the package is not cached.

    The name is normalised the same way the serve path normalises it, because NuGet
    addresses packages case-insensitively and a document stored under a different
    case is one no restore ever asks for.
    """
    lower = normalise(package_id or "")
    if not valid_id(lower):
        raise CoordinateError(f"package id {package_id!r} is not one NuGet would address")
    return Coordinates(
        name=lower, version="", filename=VERSIONS_DOC, content_type="application/json"
    )


def upload_coordinates(package_id: str, version: str) -> Coordinates:
    """Where an operator-uploaded `.nupkg` is served from.

    The filename is derived the same way `package_artifact` derives it, so an
    upload and a fetch of the same package land on the same row. Validated with
    this module's own `valid_id` / `valid_version` and **never against the
    upstream flat container** (#2599): an internal package is the normal case
    for an uploaded NuGet artifact.
    """
    package_id = (package_id or "").strip()
    version = (version or "").strip()
    if not valid_id(package_id):
        raise CoordinateError(
            f"package id {package_id!r} is not a NuGet id (letters, digits, '.', '-' and '_')"
        )
    if not valid_version(version):
        raise CoordinateError(f"version {version!r} is not a NuGet version")
    lower = normalise(package_id)
    return Coordinates(
        name=lower,
        version=version,
        filename=f"{lower}.{version}.nupkg",
    )


def registration_base() -> str:
    return settings.registry.package_cache.nuget.registration_upstream.rstrip("/")


async def resolve_digest(package_id: str, version: str, client: httpx.AsyncClient) -> str:
    """Upstream's `packageHash` for one package, as an SRI-style digest.

    Two requests, because NuGet puts the digest two hops from the download. The
    registration leaf names a `catalogEntry`, and the digest is on that; the leaf
    may also inline the entry, so both shapes are handled rather than assumed.

    Returned as ``<algo>-<base64>`` because `packageHash` IS base64 and that is
    the form `substrate._parse_digest` already reads for npm's SRI. The algorithm
    comes from `packageHashAlgorithm` rather than being assumed to be SHA-512: it
    is upstream's statement about its own digest, and a wrong guess here would
    compare a SHA-512 against a SHA-256 computation and refuse every package.

    Raises `UpstreamError` on anything it cannot read. Failing closed is the point
    -- the caller treats an empty digest as "nothing published", which for NuGet
    is never true and would cache the package unverified.
    """
    lower = normalise(package_id)
    leaf = f"{registration_base()}/{lower}/{version}.json"
    try:
        response = await arequest_with_retry(client, "GET", leaf)
        if response.status_code == 404:
            raise NotFoundUpstream(f"{package_id} {version}")
        if response.status_code >= 400:
            raise UpstreamError(f"upstream returned {response.status_code} for {leaf}")
        entry = response.json().get("catalogEntry")

        # A URL in the gz-semver2 registrations, inline in some others.
        if isinstance(entry, str):
            response = await arequest_with_retry(client, "GET", entry)
            if response.status_code >= 400:
                raise UpstreamError(f"upstream returned {response.status_code} for {entry}")
            entry = response.json()
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach the NuGet registration for {lower}: {exc}") from exc

    if not isinstance(entry, dict):
        raise UpstreamError(f"NuGet registration for {lower} {version} has no catalogEntry")

    digest = entry.get("packageHash")
    algorithm = entry.get("packageHashAlgorithm")
    if not isinstance(digest, str) or not digest:
        raise UpstreamError(f"NuGet published no packageHash for {lower} {version}")
    if not isinstance(algorithm, str) or not algorithm:
        raise UpstreamError(f"NuGet published no packageHashAlgorithm for {lower} {version}")
    return f"{algorithm.lower()}-{digest}"


def package_artifact(package_id: str, version: str, *, base: str | None = None) -> Artifact:
    """One `.nupkg`, addressed exactly as the client asks for it.

    `base` overrides the configured upstream for ONE call. Warming uses it to
    pull from a named mirror (`registry.package_cache.<eco>.mirrors`); serving
    never passes it, so the proxy's own request-forgery surface is unchanged.
    """
    lower = normalise(package_id)
    filename = f"{lower}.{version}.nupkg"
    root = (base or upstream_base()).rstrip("/")
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=lower,
        version=version,
        filename=filename,
        upstream_url=f"{root}/{lower}/{version}/{filename}",
        # The flat container publishes no digest beside the package, so there is
        # nothing to carry here. `digest_resolver` fetches it from the registration
        # index on a MISS -- never during construction, which happens on every
        # request including a cache hit.
        digest="",
        digest_resolver=lambda client: resolve_digest(package_id, version, client),
        content_type="application/octet-stream",
    )
