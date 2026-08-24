"""npm: the packument, proxied (#1417).

An npm client fetches a *packument* — one JSON document describing every version
of a package — and then fetches each version's tarball from the `dist.tarball`
URL inside it. Only that URL is rewritten.

**`dist.integrity` is left exactly as published**, and that is what makes the
proxy safe rather than merely convenient: npm checks the tarball it receives
against upstream's own sha512 SRI, so a substituted artifact fails in the client.
We are a cache, not a trust boundary.

**The abbreviated form matters.** With `Accept: application/vnd.npm.install-v1+json`
a registry returns a much smaller document — `react`'s full packument is several
megabytes, the abbreviated one a fraction of that. The client's Accept is passed
straight upstream so an install stays on the small path, and both forms carry
`versions.{v}.dist.tarball`, so one rewrite covers them.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from terrapod.config import settings
from terrapod.services.package_cache.substrate import (
    Artifact,
    NotFoundUpstream,
    UpstreamError,
)

ECOSYSTEM = "npm"

#: The abbreviated packument. Sent when a client expressed no preference of its
#: own, because it is smaller and sufficient for installing.
ABBREVIATED = "application/vnd.npm.install-v1+json"

#: Packuments are JSON but can be several MB, so parsing goes through a thread
#: above this size (rule 13). Below it the thread hop costs more than the parse.
_PARSE_IN_THREAD_OVER = 256 * 1024

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)


def upstream_base() -> str:
    return settings.registry.package_cache.npm.upstream.rstrip("/")


def normalise(name: str) -> str:
    """The package name as npm addresses it.

    Scoped names keep their `@scope/name` shape — npm URL-encodes the slash on
    the wire but the name itself contains it, and it is the natural grouping in
    object storage too. Case is left alone: npm has enforced lowercase for new
    packages for years but older mixed-case names exist and are distinct.
    """
    return name.strip()


def tarball_filename(name: str, version: str) -> str:
    """The conventional tarball filename for a version.

    npm's own registry serves `{unscoped-name}-{version}.tgz`, dropping the
    scope. Matching that convention keeps our rewritten URLs indistinguishable in
    shape from a real registry's, which matters for the tools that parse them.
    """
    unscoped = name.rsplit("/", 1)[-1]
    return f"{unscoped}-{version}.tgz"


async def fetch_packument(
    name: str, *, accept: str | None = None, client: httpx.AsyncClient | None = None
) -> dict:
    """The upstream packument for a package."""
    url = f"{upstream_base()}/{name}"
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await client.get(url, headers={"Accept": accept or ABBREVIATED})
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach {url}: {exc}") from exc
    finally:
        if owns:
            await client.aclose()

    if response.status_code == 404:
        raise NotFoundUpstream(name)
    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned {response.status_code} for {url}")

    body = response.content
    if len(body) > _PARSE_IN_THREAD_OVER:
        return await asyncio.to_thread(json.loads, body)
    return json.loads(body)


def find_version(packument: dict, filename: str) -> tuple[str, dict] | None:
    """The version entry whose tarball is *filename*, or None.

    Matched on the filename we would have generated rather than on upstream's
    URL, so a registry that serves tarballs from a different host or path still
    resolves. Returns `(version, entry)`.
    """
    name = packument.get("name", "")
    for version, entry in (packument.get("versions") or {}).items():
        if not isinstance(entry, dict):
            continue
        if tarball_filename(name, version) == filename:
            return version, entry
    return None


def artifact_for(name: str, version: str, entry: dict) -> Artifact:
    """Turn a packument version entry into something the substrate can fetch."""
    dist = entry.get("dist") or {}
    upstream_url = dist.get("tarball")
    if not isinstance(upstream_url, str) or not upstream_url:
        raise UpstreamError(f"{name}@{version} has no dist.tarball")
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=normalise(name),
        version=version,
        filename=tarball_filename(name, version),
        upstream_url=upstream_url,
        # SRI as published ("sha512-...."), falling back to the legacy sha1
        # `shasum` for packages published before integrity existed.
        digest=dist.get("integrity") or (f"sha1:{dist['shasum']}" if dist.get("shasum") else ""),
        content_type="application/octet-stream",
    )


def rewrite(packument: dict, tarball_url_base: str) -> dict:
    """The packument with every `dist.tarball` pointed at us.

    Everything else — `dist.integrity`, `dist.shasum`, dependency ranges,
    `dist-tags` — is passed through untouched. Rewriting a dependency range or
    dropping an integrity hash would silently change what a client installs.
    """
    name = packument.get("name", "")
    versions = packument.get("versions")
    if not isinstance(versions, dict):
        return packument

    rewritten_versions = {}
    for version, entry in versions.items():
        if not isinstance(entry, dict):
            rewritten_versions[version] = entry
            continue
        dist = entry.get("dist")
        if isinstance(dist, dict) and dist.get("tarball"):
            filename = tarball_filename(name, version)
            entry = {
                **entry,
                "dist": {**dist, "tarball": f"{tarball_url_base}/{name}/-/{filename}"},
            }
        rewritten_versions[version] = entry

    return {**packument, "versions": rewritten_versions}


#: Where a cached packument is kept, as an artifact alongside the tarballs it
#: describes. A sealed node has no other way to obtain one, and cannot serve an
#: install without it: dependency ranges live in the packument and nowhere else.
PACKUMENT_FILENAME = "packument.json"


def packument_artifact(name: str) -> Artifact:
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=normalise(name),
        version="",
        filename=PACKUMENT_FILENAME,
        upstream_url="",
        content_type="application/json",
    )


def restrict_to_cached(packument: dict, cached_filenames: set[str]) -> dict:
    """The packument with versions we cannot serve removed (#1417).

    What a sealed node serves. Leaving an uncached version in would let npm
    resolve to it and then fail fetching its tarball — and npm reports that as a
    network error, which sends an operator to look at their firewall rather than
    at whatever they forgot to warm.

    `dist-tags` are pruned to surviving versions for the same reason: a `latest`
    pointing at a version the document no longer lists makes npm ask for
    something that is not there.
    """
    versions = packument.get("versions")
    if not isinstance(versions, dict):
        return packument

    name = packument.get("name", "")
    kept = {
        version: entry
        for version, entry in versions.items()
        if tarball_filename(name, version) in cached_filenames
    }

    tags = packument.get("dist-tags")
    pruned: dict = {}
    if isinstance(tags, dict):
        pruned = {tag: version for tag, version in tags.items() if version in kept}
    if "latest" not in pruned and kept:
        # npm needs a `latest`. Lexical order is not semver order, but this is a
        # fallback for when the real `latest` is not cached, not the main path.
        pruned["latest"] = sorted(kept)[-1]

    return {**packument, "versions": kept, "dist-tags": pruned}
