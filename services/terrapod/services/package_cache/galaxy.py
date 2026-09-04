"""Ansible Galaxy v3, proxied (#1482).

The endpoints here are the ones `ansible-galaxy` actually calls, captured from a
real client rather than read from documentation — see
`docs/galaxy-cli-surface.md`, which records three places the documentation would
have led us to build the wrong thing.

**Every URL the client might follow is rewritten to point at us.** That is the
whole feature: an install that resolves through Terrapod and then downloads from
`galaxy.ansible.com` has not been proxied at all, and the failure is invisible
until someone tries it with no route out. So `download_url`, `versions_url` and
every `href` are rewritten, and the tests assert that no upstream host survives
in a served document.

**`href` is load-bearing, not decorative.** Omitting it from a detail response
aborts the install with a bare `KeyError` reported as a suspected client bug. It
is rewritten rather than dropped.

**The version list is mutable; a collection artifact at a version is not.** Per
the cache-expiry rule, the artifact needs no TTL — re-fetching returns identical
bytes — while the list of what versions exist has to be bounded, or a newly
published version can never be installed. Sealed, the list is restricted to what
we actually hold, so it never advertises something the node cannot serve.
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

ECOSYSTEM = "galaxy"

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

#: Galaxy namespaces and names are `^[a-z0-9_]+$` by the collection spec, so
#: there is no normalisation ambiguity of the kind PEP 503 has. The pattern is
#: enforced on the way in anyway: these segments are interpolated into an
#: upstream URL, and anything that is not a plain name has no business there.
_SEGMENT = re.compile(r"^[a-z0-9_]+$")

#: A version string as it appears in a path. Deliberately permissive about the
#: semver tail (`1.0.0-rc1+build`) and deliberately not permissive about
#: anything that could traverse or escape the path.
_VERSION = re.compile(r"^[A-Za-z0-9._+-]+$")

#: The document filenames a collection's cached index is stored under. They are
#: not real upstream filenames — they are stable keys for the substrate, which
#: addresses everything as (ecosystem, name, filename).
COLLECTION_DOC = "collection.json"
VERSIONS_DOC = "versions.json"


def valid_segment(value: str) -> bool:
    """Whether a namespace or collection name is one we will pass upstream."""
    return bool(_SEGMENT.match(value))


def valid_version(value: str) -> bool:
    return bool(_VERSION.match(value))


def collection_key(namespace: str, name: str) -> str:
    """The substrate's `name` for a collection.

    One key per collection rather than per namespace, so `cached_filenames` for
    it returns that collection's artifacts and nothing else — which is what the
    sealed version list is built from.
    """
    return f"{namespace}.{name}"


def version_doc(version: str) -> str:
    return f"version-{version}.json"


def upstream_base() -> str:
    return settings.registry.package_cache.galaxy.upstream.rstrip("/")


async def _get_json(url: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch and parse one upstream document.

    Distinguishes "no such collection" from "upstream is unreachable": the first
    is a 404 the client should see, the second a 502 worth retrying, and
    collapsing them tells an operator to go and look at their network over a
    typo.
    """
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach {url}: {exc}") from exc
    finally:
        if owns:
            await client.aclose()

    if response.status_code == 404:
        raise NotFoundUpstream(url)
    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned {response.status_code} for {url}")
    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamError(f"{url} did not return JSON") from exc


async def fetch_collection(
    namespace: str, name: str, *, client: httpx.AsyncClient | None = None
) -> dict:
    """Upstream's collection detail document."""
    url = f"{upstream_base()}/api/v3/collections/{namespace}/{name}/"
    return await _get_json(url, client=client)


async def fetch_versions(
    namespace: str, name: str, *, client: httpx.AsyncClient | None = None
) -> dict:
    """Every version of a collection, as one document.

    Upstream paginates. We follow `links.next` and hand the client a single page,
    because the alternative is proxying someone else's cursors — which means
    either leaking upstream URLs into a response we have promised to rewrite, or
    inventing a cursor scheme of our own for no gain. A collection has tens of
    versions, not thousands.

    A malformed or self-referential `next` would otherwise loop forever, so the
    walk is bounded and stops rather than following a link twice.
    """
    url = f"{upstream_base()}/api/v3/collections/{namespace}/{name}/versions/?limit=100"
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        collected: list[dict] = []
        seen: set[str] = set()
        while url and url not in seen and len(seen) < 50:
            seen.add(url)
            page = await _get_json(url, client=client)
            data = page.get("data")
            if isinstance(data, list):
                collected.extend(entry for entry in data if isinstance(entry, dict))
            nxt = (page.get("links") or {}).get("next")
            url = _absolute_next(nxt)
    finally:
        if owns:
            await client.aclose()

    return {
        "meta": {"count": len(collected)},
        "links": {"first": None, "previous": None, "next": None, "last": None},
        "data": collected,
    }


def _absolute_next(nxt: object) -> str:
    """The next page URL, or empty when there is not one.

    Upstream may give a relative path. It is resolved against the *configured*
    upstream rather than followed as given, so a `next` pointing at another host
    cannot walk us off the operator's chosen registry — the same reasoning as
    never fetching a client-supplied URL.
    """
    if not isinstance(nxt, str) or not nxt:
        return ""
    if nxt.startswith("/"):
        return f"{upstream_base()}{nxt}"
    base = upstream_base()
    return nxt if nxt.startswith(base) else ""


async def fetch_version(
    namespace: str, name: str, version: str, *, client: httpx.AsyncClient | None = None
) -> dict:
    """Upstream's version detail — where `download_url` and the digest live."""
    url = f"{upstream_base()}/api/v3/collections/{namespace}/{name}/versions/{version}/"
    return await _get_json(url, client=client)


def artifact_filename(namespace: str, name: str, version: str) -> str:
    """The conventional collection artifact name.

    Derived rather than taken from `artifact.filename`, because it is
    interpolated into a storage key and a URL path. Upstream's value is used to
    cross-check, not to build paths from.
    """
    return f"{namespace}-{name}-{version}.tar.gz"


def artifact_for(namespace: str, name: str, version: str, detail: dict) -> Artifact:
    """Turn a version detail document into something the substrate can fetch."""
    artifact = detail.get("artifact") or {}
    sha256 = artifact.get("sha256")
    digest = f"sha256:{sha256}" if isinstance(sha256, str) and sha256 else ""
    download = detail.get("download_url")
    if not isinstance(download, str) or not download:
        raise UpstreamError(
            f"{namespace}.{name}:{version} has no download_url; "
            "upstream did not describe how to fetch it"
        )
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=collection_key(namespace, name),
        version=version,
        filename=artifact_filename(namespace, name, version),
        upstream_url=download,
        digest=digest,
        content_type="application/octet-stream",
    )


def rewrite_collection(document: dict, base: str, namespace: str, name: str) -> dict:
    """Point a collection detail at us.

    `href` and `versions_url` both have to be ours, and `href` has to be present
    at all — see the module docstring.
    """
    out = dict(document)
    out["href"] = f"{base}/v3/collections/{namespace}/{name}/"
    out["versions_url"] = f"{base}/v3/collections/{namespace}/{name}/versions/"
    highest = out.get("highest_version")
    if isinstance(highest, dict) and isinstance(highest.get("version"), str):
        out["highest_version"] = {
            **highest,
            "href": f"{base}/v3/collections/{namespace}/{name}/versions/{highest['version']}/",
        }
    return out


def rewrite_versions(document: dict, base: str, namespace: str, name: str) -> dict:
    """Point every entry in a version list at us, and drop upstream's cursors."""
    prefix = f"{base}/v3/collections/{namespace}/{name}/versions"
    entries = []
    for entry in document.get("data") or []:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version")
        if not isinstance(version, str):
            continue
        entries.append({**entry, "href": f"{prefix}/{version}/"})
    return {
        "meta": {"count": len(entries)},
        # Ours is a single page. Advertising upstream's cursors would hand the
        # client a URL we have not rewritten, which is the one thing this module
        # exists to prevent.
        "links": {"first": f"{prefix}/", "previous": None, "next": None, "last": f"{prefix}/"},
        "data": entries,
    }


def rewrite_version(document: dict, base: str, namespace: str, name: str, version: str) -> dict:
    """Point a version detail at us — `href` and, critically, `download_url`.

    If `download_url` is ever left as upstream's, an install resolves through
    Terrapod and then fetches from the internet, which looks identical to
    working right up until someone has no route out.
    """
    out = dict(document)
    out["href"] = f"{base}/v3/collections/{namespace}/{name}/versions/{version}/"
    out["download_url"] = (
        f"{base}/v3/collections/{namespace}/{name}/versions/{version}/download/"
        f"{artifact_filename(namespace, name, version)}"
    )
    return out


def restrict_to_cached(document: dict, held_versions: set[str]) -> dict:
    """A version list narrowed to the versions whose artifact we actually hold.

    A sealed node must not advertise a version it cannot serve: the client would
    resolve to it, ask for the artifact, and get a 404 at the point it has
    already committed. Better to offer the newest version we can deliver.
    """
    kept = [
        entry
        for entry in document.get("data") or []
        if isinstance(entry, dict) and entry.get("version") in held_versions
    ]
    return {**document, "meta": {"count": len(kept)}, "data": kept}


def versions_held(filenames: list[str], namespace: str, name: str) -> set[str]:
    """The versions we hold an artifact for, from the substrate's filenames."""
    prefix = f"{namespace}-{name}-"
    suffix = ".tar.gz"
    return {
        f[len(prefix) : -len(suffix)]
        for f in filenames
        if f.startswith(prefix) and f.endswith(suffix)
    }
