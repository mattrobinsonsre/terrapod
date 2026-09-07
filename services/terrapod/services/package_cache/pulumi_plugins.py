"""Pulumi plugin downloads, proxied (#1483).

The smallest of the ecosystem proxies, because the protocol is one request. The
CLI already knows the plugin's kind, name, version, OS and architecture, so it
asks for a single well-known filename under whatever base
`PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES` points it at:

    GET {base}/pulumi-resource-random-v4.16.3-linux-amd64.tar.gz

There is no index and no metadata call — captured from a real client, not read
from documentation (`scripts/pulumi-capture.py`). Which means, per the
cache-expiry rule, there is **nothing here that needs a TTL**: a plugin at a
version is an immutable artifact, and this proxy serves nothing else. No version
list exists to go stale, because the client never asks for one.

**The filename is parsed rather than passed through.** It is client input that
would otherwise be interpolated straight into an upstream URL and a storage key,
so it is matched against the exact shape the CLI emits and refused otherwise.
That check is the whole of the request-forgery surface here.
"""

from __future__ import annotations

import re

from terrapod.config import settings
from terrapod.services.package_cache.substrate import Artifact

ECOSYSTEM = "pulumi"

#: The artifact name the CLI builds, whose template is literally
#: `pulumi-%s-%s-v%s-%s-%s.tar.gz` in the binary. The name may contain hyphens
#: (`aws-native`), so it is non-greedy up to the `-v<digit>` that starts the
#: version; OS and architecture are plain tokens at the end.
_FILENAME = re.compile(
    r"^pulumi-(?P<kind>[a-z]+)-(?P<name>[a-z0-9][a-z0-9._-]*?)"
    r"-v(?P<version>[0-9][A-Za-z0-9._+-]*)"
    r"-(?P<os>[a-z0-9]+)-(?P<arch>[a-z0-9]+)\.tar\.gz$"
)


def parse_filename(filename: str) -> dict[str, str] | None:
    """The parts of a plugin filename, or None if it is not one.

    Returning None rather than raising leaves the caller to decide the status
    code; every caller so far answers 404, because a filename that is not a
    plugin names nothing this proxy has.
    """
    match = _FILENAME.match(filename)
    return match.groupdict() if match else None


def upstream_base() -> str:
    return settings.registry.package_cache.pulumi.upstream.rstrip("/")


def artifact_for(filename: str, parts: dict[str, str]) -> Artifact:
    """Turn a parsed filename into something the substrate can fetch.

    `name` groups a plugin's platforms together — `pulumi-resource-aws` rather
    than one entry per OS and architecture — so `cached_filenames` for it lists
    the platforms held, matching how the other ecosystems key their artifacts.

    The upstream URL is rebuilt from the *parsed* parts against the configured
    base, so a filename that survived the pattern still cannot smuggle a path
    into the request.
    """
    plugin = f"pulumi-{parts['kind']}-{parts['name']}"
    rebuilt = f"{plugin}-v{parts['version']}-{parts['os']}-{parts['arch']}.tar.gz"
    return Artifact(
        ecosystem=ECOSYSTEM,
        name=plugin,
        version=parts["version"],
        filename=rebuilt,
        upstream_url=f"{upstream_base()}/{rebuilt}",
        # Upstream publishes no digest alongside the tarball, so there is none
        # to record. The CLI verifies the plugin by unpacking and running it,
        # which is a weaker guarantee than pip's or npm's and worth being plain
        # about rather than implying an integrity check we do not perform.
        digest="",
        content_type="application/gzip",
    )
