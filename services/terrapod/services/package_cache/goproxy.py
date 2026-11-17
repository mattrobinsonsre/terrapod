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

**Nothing here is verified at fetch time, deliberately (#2589).** The reason is
recorded in full on `artifact_for`, and the short version is that the consumer
already verifies what it has PINNED: the hashes in a program's committed
`go.sum` are enforced by the `go` command, which is a stronger control than ours
would be because the program's author pinned them. So a row cached here records
`none-published` rather than being left to look verified.

A module ABSENT from that `go.sum` is not covered, and `artifact_for` records the
measurement -- with `GOSUMDB=off` the `go` command writes our bytes' hash rather
than refusing. Closing that is #2589 Gap A.

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
from urllib.parse import urlparse

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


def artifact_for(module: str, version: str, suffix: str, *, base: str | None = None) -> Artifact:
    """One immutable file of a module version — `.info`, `.mod` or `.zip`.

    `base` overrides the configured upstream for ONE call. Warming uses it to
    pull from a named mirror (`registry.package_cache.<eco>.mirrors`); serving
    never passes it, so the proxy's own request-forgery surface is unchanged.
    The URL is BUILT from the base here rather than read out of an index, because
    the proxy protocol publishes no document carrying it — so unlike pypi, npm
    and galaxy this one has to be told.
    """
    root = (base or upstream_base()).rstrip("/")
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=cache_name(module),
        version=version,
        filename=f"{version}{suffix}",
        upstream_url=f"{root}/{module}/@v/{version}{suffix}",
        # No digest HERE, and no longer because nobody can check: the proxy
        # protocol publishes none alongside these files (`.ziphash` and
        # `.zip.sha256` are both 404 -- measured), so the authoritative hash
        # lives in the Go checksum database and nowhere else.
        #
        # **That database is now MIRRORED through this cache** (#2589). It used
        # to be a third upstream a sealed deployment could not reach, which is
        # why the runner ran with `GOSUMDB=off`; the module-proxy protocol
        # carries it under `/sumdb/<host>/`, so it is cached content like
        # everything else here and the check is back on by default.
        #
        # Why that matters, and why the old reason for leaving it off was wrong:
        # the `go` command enforces the hashes in a program's committed `go.sum`,
        # which covers every module the author has PINNED. For anything absent
        # from that file it does not -- measured against go1.27.3 with a cold
        # module cache, `go mod download` with `GOSUMDB=off` and no `go.sum`
        # entry exits 0 and WRITES the hash of whatever the proxy served. The
        # runner runs exactly that command against exactly this cache, so an
        # incomplete `go.sum` recorded our own bytes as authoritative. The old
        # wording -- "enforced against every module it downloads" -- was true of
        # the pinned case and false of this one, which is the direction that
        # reads as there being nothing left to do.
        #
        # The row still records `none-published`, and that is now a statement
        # about THIS artifact rather than about the ecosystem: the bytes carry no
        # publisher digest, and what verifies them is the transparency log the
        # toolchain checks them against, not a digest we could have stored.
        digest="",
        content_type=_CONTENT_TYPES[suffix],
    )


#: The documents an operator may upload. `@v/list` is what `go list -m -versions`
#: and an unpinned `go get` read; `@latest` is what `go get module` with no version
#: resolves through.
INDEX_DOCUMENTS = {"list": LIST_DOC, "latest": LATEST_DOC}


def index_coordinates(module: str, document: str) -> Coordinates:
    """Where an operator-uploaded module index document is served from.

    Unlike the other three this is not required for a PINNED install: `go mod
    download` of an exact version asks for `.info`/`.mod`/`.zip` directly and never
    consults the list. It is required for anything that has to DISCOVER a version --
    `go get module` with no version, or `go list -m -versions`.

    Served as `text/plain` for `@v/list`, which is a newline-separated list rather
    than JSON, and as JSON for `@latest`.
    """
    name = cache_name(module or "")
    if not valid_module(module or ""):
        raise CoordinateError(f"module {module!r} is not a module path the proxy addresses")
    key = (document or "").strip().lower()
    filename = INDEX_DOCUMENTS.get(key)
    if filename is None:
        raise CoordinateError(
            f"document {document!r} is not a module index document; "
            f"valid values are {', '.join(sorted(INDEX_DOCUMENTS))}"
        )
    return Coordinates(
        name=name,
        version="",
        filename=filename,
        content_type="text/plain; charset=UTF-8" if key == "list" else "application/json",
    )


def upload_coordinates(module: str, version: str, suffix: str) -> Coordinates:
    """Where one operator-uploaded module file is served from.

    A module version is **three** files to the proxy protocol — `.info`, `.mod`
    and `.zip` — and they are separate artifacts with separate bytes, so each is
    uploaded in its own right exactly as each is fetched in its own right. An
    upload of the `.zip` alone will not satisfy `go mod download`.

    Validated with this module's own `valid_module` / `valid_version`, which
    already refuse a path that climbs, and **never against the upstream proxy**
    (#2599): a module from an internal host is the normal case here.
    """
    module = (module or "").strip()
    version = (version or "").strip()
    suffix = (suffix or "").strip()
    if not valid_module(module):
        raise CoordinateError(
            f"module {module!r} is not a module path the proxy protocol addresses"
        )
    if not valid_version(version):
        raise CoordinateError(f"version {version!r} is not a module version (it starts 'v')")
    if suffix not in _CONTENT_TYPES:
        raise CoordinateError(
            f"suffix {suffix!r} is not one of the files a module version has: "
            f"{', '.join(sorted(_CONTENT_TYPES))}"
        )
    return Coordinates(
        name=cache_name(module),
        version=version,
        filename=f"{version}{suffix}",
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
    module: str, name: str, *, client: httpx.AsyncClient | None = None, base: str | None = None
) -> bytes:
    """Fetch one of the mutable documents — `@v/list` or `@latest`.

    Returns raw bytes rather than a parsed document: `@v/list` is a newline
    separated list and `@latest` is JSON, and neither is inspected here. Passing
    the bytes through means a future addition to either format cannot be lost in
    a re-serialisation.
    """
    path = "@v/list" if name == LIST_DOC else "@latest"
    url = f"{(base or upstream_base()).rstrip('/')}/{module}/{path}"
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await arequest_with_retry(client, "GET", url)
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


# --- The checksum database (#2589) ---------------------------------------
#
# The module-proxy protocol carries the Go checksum database under
# `/sumdb/<host>/...`, which is what makes the database mirrorable rather than a
# second upstream. Measured against go1.27.3 through a logging proxy, a cold
# `go mod tidy` asks for, in order:
#
#     /sumdb/sum.golang.org/supported            <- proxy-side; 200 means "I serve this"
#     /sumdb/sum.golang.org/tile/8/0/x274/929.p/152
#     /sumdb/sum.golang.org/lookup/rsc.io/quote@v1.5.2
#     ... more tiles, interleaved with the module files
#
# `supported` exists ONLY on the proxy -- the real sum.golang.org answers 404 for
# it -- so answering it is how we tell the toolchain to route the database here.
# Everything else is passed through and cached: a tile is immutable content at a
# fixed position in the log, and a lookup is an append-only entry, so neither
# needs an expiry.

SUMDB_PREFIX = "sumdb"


def sumdb_host() -> str:
    """The checksum database host this cache mirrors, or "" when it serves none."""
    raw = (settings.registry.package_cache.go.sumdb_upstream or "").strip()
    if not raw:
        return ""
    return urlparse(raw if "://" in raw else f"https://{raw}").hostname or ""


def sumdb_enabled() -> bool:
    return bool(sumdb_host())


def valid_sumdb_path(path: str) -> bool:
    """Whether a sumdb path is one we will fetch and store.

    Refuses anything that could climb out of the cache prefix. The path becomes
    part of an object key (`package_cache_key` leaves slashes intact, by design,
    so the hierarchy is the grouping), which is exactly why `..` and an absolute
    path have to be refused here rather than relied on being harmless later.
    """
    path = (path or "").strip()
    if not path or path.startswith("/") or path.endswith("/"):
        return False
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        return False
    # The toolchain asks only for `lookup/...` and `tile/...`. Refusing the rest
    # keeps this from becoming a general proxy to the sumdb host.
    return path.startswith(("lookup/", "tile/"))


def sumdb_artifact(path: str) -> Artifact:
    """The cache entry for one sumdb response."""
    host = sumdb_host()
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=f"{SUMDB_PREFIX}/{host}",
        version="",
        filename=path,
        upstream_url=f"{(settings.registry.package_cache.go.sumdb_upstream or '').rstrip('/')}/{path}",
        content_type="text/plain; charset=UTF-8",
    )


async def fetch_sumdb(path: str) -> bytes:
    """Fetch one sumdb response from the configured database."""
    base = (settings.registry.package_cache.go.sumdb_upstream or "").rstrip("/")
    url = f"{base}/{path}"
    client = httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await arequest_with_retry(client, "GET", url)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach {url}: {exc}") from exc
    finally:
        await client.aclose()

    if response.status_code in (404, 410):
        # A lookup for a module the database has never seen. Normal, and the
        # toolchain treats it as "this module is not in the log" rather than as
        # a fault, so it must not become a 5xx.
        raise NotFoundUpstream(path)
    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned {response.status_code} for {url}")
    return response.content
