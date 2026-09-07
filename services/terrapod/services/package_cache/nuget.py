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
from terrapod.services.package_cache.substrate import (
    Artifact,
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


async def fetch_versions(package_id: str, *, client: httpx.AsyncClient | None = None) -> bytes:
    """The upstream versions document for a package, as raw bytes.

    Not parsed: it is `{"versions": [...]}` and nothing here inspects it, so
    passing the bytes through means a future field cannot be lost in a
    re-serialisation.
    """
    lower = normalise(package_id)
    url = f"{upstream_base()}/{lower}/index.json"
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await client.get(url)
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


def package_artifact(package_id: str, version: str) -> Artifact:
    """One `.nupkg`, addressed exactly as the client asks for it."""
    lower = normalise(package_id)
    filename = f"{lower}.{version}.nupkg"
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=lower,
        version=version,
        filename=filename,
        upstream_url=f"{upstream_base()}/{lower}/{version}/{filename}",
        # The flat container publishes no digest beside the package; the client
        # verifies against the versions document and its own signature checks.
        digest="",
        content_type="application/octet-stream",
    )
