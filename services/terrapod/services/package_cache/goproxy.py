"""The Go module proxy protocol, proxied (#1484).

Five paths, and the split between them is the whole design:

    @v/list                 what versions exist      MUTABLE
    @latest                 the newest one           MUTABLE
    @v/{version}.info       metadata for one         immutable
    @v/{version}.mod        its go.mod               immutable
    @v/{version}.zip        its source               immutable

Per the cache-expiry rule, only the first two need a bound — a module version's
bytes cannot change, which the Go checksum database exists to guarantee, so the
artifacts need no TTL at all and only `last_accessed_at` for retention.

Two properties captured from a real toolchain (`scripts/goproxy-capture.py`),
either of which breaks common modules if guessed:

* **Uppercase is escaped.** `github.com/BurntSushi/toml` is requested as
  `github.com/!burnt!sushi/toml`. The escaped form is what upstream expects
  too, so it is forwarded verbatim rather than decoded and re-encoded.
* **The toolchain probes parent prefixes.** Resolving `example.com/a/b` asks
  about `example.com`, `example.com/a` and `example.com/a/b` in turn, to find
  where the module root is. Most of those are misses by design, so a miss must
  be a clean 404 — anything else turns normal resolution into a failure.
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

ECOSYSTEM = "go"

_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)

#: A module path in its escaped form. Deliberately allows `!` (the case escape)
#: and the usual host/path characters, and deliberately not `..` or anything
#: that could climb out of the path it is interpolated into.
_MODULE = re.compile(r"^[A-Za-z0-9!._~/-]+$")

#: `v1.2.3`, `v0.0.0-20240101120000-abcdef123456`, `v2.1.0-rc1+meta`.
_VERSION = re.compile(r"^v[A-Za-z0-9._+-]+$")

#: The two mutable documents, stored under these names so the substrate can
#: hold them the way it holds an npm packument.
LIST_DOC = "@v-list"
LATEST_DOC = "@latest"


def valid_module(module: str) -> bool:
    """Whether a module path is one we will pass upstream.

    `..` is rejected outright rather than normalised: a path that tries to climb
    is not a module anyone meant to fetch, and normalising it would decide on
    the caller's behalf what they meant.
    """
    if not _MODULE.match(module):
        return False
    return ".." not in module.split("/")


def valid_version(version: str) -> bool:
    return bool(_VERSION.match(version))


def upstream_base() -> str:
    return settings.registry.package_cache.go.upstream.rstrip("/")


def cache_name(module: str) -> str:
    """The substrate's `name` for a module.

    The escaped module path, so every version of one module groups together and
    `cached_filenames` lists what is held for it.
    """
    return module


def artifact_for(module: str, version: str, suffix: str) -> Artifact:
    """One immutable file of a module version — `.info`, `.mod` or `.zip`."""
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=cache_name(module),
        version=version,
        filename=f"{version}{suffix}",
        upstream_url=f"{upstream_base()}/{module}/@v/{version}{suffix}",
        # The proxy protocol publishes no digest alongside these; integrity
        # comes from the client's own checksum database, which it consults
        # independently of us. Claiming one here would imply a check we do not
        # perform.
        digest="",
        content_type=_CONTENT_TYPES[suffix],
    )


#: What each immutable file is. `.mod` and `.info` are small text/JSON; a `.zip`
#: is the module's whole source and can be large, which is why everything here
#: goes through the streaming substrate rather than being buffered.
_CONTENT_TYPES = {
    ".info": "application/json",
    ".mod": "text/plain; charset=UTF-8",
    ".zip": "application/zip",
}

SUFFIXES = tuple(_CONTENT_TYPES)


async def fetch_document(
    module: str, name: str, *, client: httpx.AsyncClient | None = None
) -> bytes:
    """Fetch one of the mutable documents — `@v/list` or `@latest`.

    Returns raw bytes rather than a parsed document: `@v/list` is a newline
    separated list and `@latest` is JSON, and neither is inspected here. Passing
    the bytes through means a future addition to either format cannot be lost in
    a re-serialisation.
    """
    path = "@v/list" if name == LIST_DOC else "@latest"
    url = f"{upstream_base()}/{module}/{path}"
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach {url}: {exc}") from exc
    finally:
        if owns:
            await client.aclose()

    if response.status_code in (404, 410):
        # Expected and frequent: the toolchain asks about parent prefixes that
        # are not modules. A miss here is normal resolution, not a fault.
        raise NotFoundUpstream(module)
    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned {response.status_code} for {url}")
    return response.content


def document_artifact(module: str, name: str) -> Artifact:
    """The cache entry for a mutable document, so a sealed node still resolves."""
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=cache_name(module),
        version="",
        filename=name,
        upstream_url="",
        content_type="text/plain; charset=UTF-8",
    )
