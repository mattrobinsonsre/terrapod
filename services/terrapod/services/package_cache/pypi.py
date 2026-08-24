"""PyPI: the PEP 503 simple index, proxied (#1417).

**Upstream is always asked for JSON; the client gets whatever it asked for.**
PEP 691 defines a JSON form of the simple index, and requesting that upstream
means there is one parse path here instead of an HTML scraper. Older pip still
sends `Accept: text/html`, so the HTML form (PEP 503) is *rendered* from the
parsed result rather than rewritten with a regex over someone else's markup.

**The file endpoint never fetches a URL the client supplied.** A cold artifact is
resolved by re-reading the project's index from the configured upstream and
finding the filename in it. Accepting an upstream URL as a parameter — encoded
into the rewritten link, say — would be cheaper by one small request and would
turn the endpoint into a server-side request forgery primitive, which is the same
mistake the OCI upstream allow-list exists to prevent.
"""

from __future__ import annotations

import html
import re

import httpx

from terrapod.config import settings
from terrapod.services.package_cache.substrate import (
    Artifact,
    NotFoundUpstream,
    UpstreamError,
)

ECOSYSTEM = "pypi"

#: PEP 691. Asking for the JSON form is what keeps this module free of an HTML
#: parser; `text/html` is the fallback for an upstream that predates the PEP.
_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json, application/vnd.pypi.simple.v1+html;q=0.2"

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)


def normalise(name: str) -> str:
    """PEP 503 normalisation.

    `Flask`, `flask` and `FLASK` are one project, and so are `zope.interface`,
    `zope-interface` and `zope_interface`. Normalising on the way in makes them
    one cache entry rather than several copies of identical bytes.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def upstream_base() -> str:
    return settings.registry.package_cache.pypi.upstream.rstrip("/")


async def fetch_index(project: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """The upstream PEP 691 index for a project.

    Returns the parsed document. Raises :class:`NotFoundUpstream` for a project
    that does not exist, which the caller turns into a 404 — distinct from an
    upstream that is unreachable, which is a 502 and worth retrying.
    """
    url = f"{upstream_base()}/simple/{normalise(project)}/"
    owns = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)
    try:
        response = await client.get(url, headers={"Accept": _JSON_ACCEPT})
    except httpx.HTTPError as exc:
        raise UpstreamError(f"could not reach {url}: {exc}") from exc
    finally:
        if owns:
            await client.aclose()

    if response.status_code == 404:
        raise NotFoundUpstream(project)
    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned {response.status_code} for {url}")

    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        # An upstream serving only PEP 503 HTML. Rather than grow a parser for a
        # case no mainstream index still exhibits, say so plainly.
        raise UpstreamError(
            f"{url} returned {content_type or 'an unknown content type'}; "
            "the package cache requires a PEP 691 (JSON) simple index"
        )
    return response.json()


def _digest_of(file_entry: dict) -> str:
    """Upstream's digest for a file, as `sha256:...`, or empty.

    Recorded, not enforced — see the package docstring. `hashes` is a mapping of
    algorithm to hex digest; sha256 is what every index publishes and what pip
    checks.
    """
    hashes = file_entry.get("hashes")
    if isinstance(hashes, dict):
        sha256 = hashes.get("sha256")
        if isinstance(sha256, str) and sha256:
            return f"sha256:{sha256}"
    return ""


def find_file(index: dict, filename: str) -> dict | None:
    """The index entry for a filename, or None."""
    for entry in index.get("files", []):
        if isinstance(entry, dict) and entry.get("filename") == filename:
            return entry
    return None


def artifact_for(project: str, entry: dict) -> Artifact:
    """Turn an index entry into something the substrate can fetch."""
    filename = entry["filename"]
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=normalise(project),
        # The index does not carry a version field; the filename does, and
        # deriving it is best-effort because it is only ever displayed.
        version=_version_from_filename(filename),
        filename=filename,
        upstream_url=entry["url"],
        digest=_digest_of(entry),
        content_type="application/octet-stream",
    )


#: PEP 658 sidecar suffix. pip reads a wheel's METADATA from `<file>.metadata`
#: instead of downloading the whole wheel to resolve dependencies, which is the
#: difference between a fast resolve and pulling hundreds of megabytes to read a
#: few kilobytes each time.
METADATA_SUFFIX = ".metadata"


def is_metadata_request(filename: str) -> tuple[str, bool]:
    """Split a requested filename into its base and whether it is a sidecar.

    The index passes upstream's `core-metadata` flag through untouched, so pip
    believes the sidecar exists at our URL — which means we have to serve it. An
    index that advertises a sidecar and then 404s it makes every resolve fail,
    and it fails at the point pip has already committed to the fast path.
    """
    if filename.endswith(METADATA_SUFFIX):
        return filename[: -len(METADATA_SUFFIX)], True
    return filename, False


def metadata_artifact_for(project: str, entry: dict) -> Artifact:
    """The PEP 658 metadata sidecar for an index entry.

    Cached as an artifact in its own right — it is a distinct file with distinct
    bytes, and treating it as one means it expires, replicates and is purged by
    the same machinery as everything else rather than needing its own.
    """
    filename = entry["filename"] + METADATA_SUFFIX
    core = entry.get("core-metadata") or entry.get("dist-info-metadata")
    digest = ""
    if isinstance(core, dict):
        sha256 = core.get("sha256")
        if isinstance(sha256, str) and sha256:
            digest = f"sha256:{sha256}"
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=normalise(project),
        version=_version_from_filename(entry["filename"]),
        filename=filename,
        # PyPI serves the sidecar beside the file it describes.
        upstream_url=entry["url"] + METADATA_SUFFIX,
        digest=digest,
        content_type="application/octet-stream",
    )


def _version_from_filename(filename: str) -> str:
    """Best-effort version out of a wheel or sdist filename.

    Wheels are `{name}-{version}-{python}-{abi}-{platform}.whl` and sdists are
    `{name}-{version}.tar.gz`, so the second hyphen-separated field is the
    version in both. Only ever shown to a human — nothing routes on it, so a
    strange filename degrading to an empty string is not a failure.
    """
    stem = filename
    for suffix in (".tar.gz", ".tar.bz2", ".zip", ".whl", ".egg"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = stem.split("-")
    return parts[1] if len(parts) > 1 else ""


def rewrite_json(index: dict, project: str, file_url_base: str) -> dict:
    """The PEP 691 document with file URLs pointed at us.

    Only the URL changes. `hashes`, `requires-python`, `yanked` and everything
    else is passed through as published, so a client's integrity check is against
    upstream's digest and our bytes — which is the whole security model here.
    """
    normalised = normalise(project)
    files = []
    for entry in index.get("files", []):
        if not isinstance(entry, dict) or "filename" not in entry:
            continue
        rewritten = dict(entry)
        rewritten["url"] = f"{file_url_base}/{normalised}/{entry['filename']}"
        files.append(rewritten)

    return {
        **{k: v for k, v in index.items() if k not in ("files", "name")},
        "name": normalised,
        "files": files,
        "meta": index.get("meta", {"api-version": "1.0"}),
    }


def render_html(document: dict) -> str:
    """The PEP 503 HTML form, for a client that asked for `text/html`.

    Rendered from the parsed document rather than rewritten from upstream's
    markup: generating known-good HTML is safer than editing someone else's, and
    every value is escaped on the way out.
    """
    name = html.escape(document.get("name", ""))
    lines = [
        "<!DOCTYPE html>",
        '<html><head><meta name="pypi:repository-version" content="1.0">',
        f"<title>Links for {name}</title></head><body>",
        f"<h1>Links for {name}</h1>",
    ]
    for entry in document.get("files", []):
        url = entry["url"]
        # PEP 503 carries the digest in the URL fragment, which is what older pip
        # reads for its integrity check.
        digest = _digest_of(entry)
        if digest:
            algorithm, _, value = digest.partition(":")
            url = f"{url}#{algorithm}={value}"
        attributes = ""
        requires_python = entry.get("requires-python")
        if isinstance(requires_python, str) and requires_python:
            attributes += f' data-requires-python="{html.escape(requires_python)}"'
        if entry.get("yanked"):
            reason = entry["yanked"]
            attributes += (
                f' data-yanked="{html.escape(reason)}"'
                if isinstance(reason, str)
                else " data-yanked"
            )
        lines.append(
            f'<a href="{html.escape(url)}"{attributes}>{html.escape(entry["filename"])}</a><br/>'
        )
    lines.append("</body></html>")
    return "\n".join(lines)


def index_from_cache(files: list) -> dict:
    """A PEP 691 index describing only what is cached (#1417).

    What a sealed node serves. Building it from cached rows rather than from a
    stored copy of upstream's index is deliberate: an upstream index lists
    versions whose files we may not hold, and pip would resolve to one of them
    and then fail on a 503 it can do nothing about. An index of what is actually
    here resolves to something installable, every time.

    Sidecars are not distributions, so they do not get their own entry — they are
    advertised as `core-metadata` on the file they describe, which is what makes
    pip use the fast resolve path against a sealed node too.
    """
    sidecars = {record.filename for record in files if record.filename.endswith(METADATA_SUFFIX)}
    entries = []
    for record in files:
        if record.filename.endswith(METADATA_SUFFIX):
            continue
        entry: dict = {"filename": record.filename, "url": record.filename}
        algorithm, _, value = record.digest.partition(":")
        if algorithm == "sha256" and value:
            entry["hashes"] = {"sha256": value}
        else:
            entry["hashes"] = {}
        if record.filename + METADATA_SUFFIX in sidecars:
            entry["core-metadata"] = True
        entries.append(entry)

    return {"meta": {"api-version": "1.0"}, "name": "", "files": entries}
