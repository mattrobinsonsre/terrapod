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

**Verification splits on who the publisher is (#2589).** `get.pulumi.com`
publishes nothing beside a tarball -- measured rather than assumed: `.sha256` and
`SHA256SUMS` are both 403 -- so a Pulumi-published plugin is cached and recorded
`none-published`, honestly rather than as though it had been checked. Terrapod's
own bridged plugin is the exception, because *we* are the publisher: its release
carries a `SHA256SUMS` manifest beside the tarballs, so for that one a digest is
REQUIRED and an absent or incomplete manifest refuses the fetch. Which plugin
gets which treatment is decided by one predicate, `is_first_party`, for the
reason recorded on it.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping

import httpx

from terrapod.config import settings
from terrapod.http_retry import arequest_with_retry
from terrapod.logging_config import get_logger
from terrapod.services.package_cache.substrate import (
    Artifact,
    CoordinateError,
    Coordinates,
    DigestResolver,
    UpstreamError,
)

logger = get_logger(__name__)

ECOSYSTEM = "pulumi"

#: The artifact name the CLI builds, whose template is literally
#: `pulumi-%s-%s-v%s-%s-%s.tar.gz` in the binary. The name may contain hyphens
#: (`aws-native`), so it is non-greedy up to the `-v<digit>` that starts the
#: version; OS and architecture are plain tokens at the end.
#: Terrapod's own bridged Pulumi provider (#1987). A deployment cannot rename
#: it -- the name is baked into the plugin binary and into every program that
#: declares the provider -- so it is a constant rather than a config key, and
#: only WHERE it is published is operator-settable.
FIRST_PARTY_PLUGIN = "terrapod"

#: Matched alongside the name so a hypothetical `pulumi-language-terrapod` or
#: `pulumi-converter-terrapod` does not silently resolve against the release
#: base for an asset that was never published there.
FIRST_PARTY_KIND = "resource"

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


def first_party_base() -> str:
    return settings.registry.package_cache.pulumi.first_party_url.rstrip("/")


def is_first_party(parts: Mapping[str, str]) -> bool:
    """Whether a parsed filename names TERRAPOD's own plugin.

    One producer, because two separate things key on it and they must not
    diverge: where the tarball is fetched from (ours is not published where
    Pulumi's are) and whether a digest is REQUIRED (ours is the only plugin whose
    publisher we are, so it is the only one whose checksums we can insist on).

    Written twice they would eventually disagree, and the damaging direction is a
    resolver attached to a Pulumi-published plugin: it would look for a manifest
    on OUR release for an asset that was never published there, and refuse
    `random` and `aws` for everybody.
    """
    return parts.get("name") == FIRST_PARTY_PLUGIN and parts.get("kind") == FIRST_PARTY_KIND


def first_party_checksums_url(version: str) -> str:
    """Where the first-party release publishes its checksums manifest.

    Built from `pulumi-provider/.goreleaser.yml`'s `checksum.name_template`, and
    note the TWO SPELLINGS OF THE VERSION in one URL: the release is tagged
    `v1.2.3` so its assets live under that path segment, while the manifest
    inside it is named `..._1.2.3_SHA256SUMS`, because GoReleaser's `{{ .Version }}`
    strips the `v`. The same trap is recorded against the Pulumi CLI's own
    manifest in `platform_tools.py`.

    Getting it wrong 404s, and a 404 here REFUSES the plugin rather than reading
    as a publisher that ships no digest -- so the typo fails closed and loudly
    instead of quietly downgrading a verifiable artifact to an unverified one.
    """
    project = f"pulumi-{FIRST_PARTY_KIND}-{FIRST_PARTY_PLUGIN}"
    return f"{first_party_base()}/v{version}/{project}_{version}_SHA256SUMS"


#: A manifest line's digest field. GoReleaser writes lowercase sha256, and
#: anything else in that column is a manifest we cannot read rather than a
#: weaker digest -- so it is refused rather than passed on to be refused later
#: with a message that names no manifest.
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


async def resolve_first_party_digest(version: str, asset: str, client: httpx.AsyncClient) -> str:
    """The sha256 our own release published for one plugin asset, as `sha256:<hex>`.

    **Best effort, and that distinction is deliberate.** An ABSENT manifest returns
    `""`, which routes the tarball into the `none-published` branch: admitted, and
    recorded honestly as unverified. A manifest that IS there and disagrees with the
    bytes is a different matter entirely and refuses the fetch, in `substrate`.

    The case that decides this is an operator who has pointed `first_party_url` at an
    internal mirror holding only the tarballs -- a supported, documented thing to do
    for a restricted network. Requiring the manifest would refuse those plugins on
    upgrade, under configuration they already have, with no warning. Breaking a
    working install to tighten a control on one plugin is the wrong trade: the
    integrity check that matters is "do these bytes match what was published", and
    that one is unaffected by admitting an absence.

    So an absence is logged at WARNING rather than swallowed, because for a release
    this project publishes it does suggest something is wrong -- it is just not worth
    refusing an artifact over.

    `pulumi` is deliberately NOT added to `substrate._DIGEST_REQUIRED` for this.
    That set matches on `Artifact.ecosystem`, so joining it would demand a digest
    for `random` and `aws-native` too and break every Pulumi-published plugin.
    The requirement belongs to one plugin, so it is enforced here, beside the
    predicate that identifies that plugin.

    Called ONLY on the miss path -- see `Artifact.digest_resolver`, whose
    placement is what keeps a sealed node's cache hits free of egress.
    """
    url = first_party_checksums_url(version)
    try:
        response = await arequest_with_retry(client, "GET", url)
    except httpx.HTTPError as exc:
        logger.warning(
            "Could not reach the first-party checksums manifest; caching the plugin "
            "unverified and recording it as such",
            url=url,
            asset=asset,
            error=str(exc),
        )
        return ""

    if response.status_code >= 400:
        logger.warning(
            "The first-party release published no readable checksums manifest; caching "
            "the plugin unverified and recording it as such. An internal mirror that "
            "carries only the tarballs is the expected reason.",
            url=url,
            asset=asset,
            status=response.status_code,
        )
        return ""

    for line in response.text.splitlines():
        fields = line.split()
        # `<hex>  <filename>`, and the leading `*` some checksum tools write for
        # binary mode is stripped the way the platform-tool manifests strip it.
        if len(fields) >= 2 and fields[1].lstrip("*") == asset:
            digest = fields[0].strip().lower()
            if not _SHA256_HEX.match(digest):
                # Still a refusal. The manifest is present and IS making a claim
                # about this asset; one we cannot parse is a broken statement rather
                # than an absent one, and admitting it would mean ignoring a digest
                # that exists.
                raise UpstreamError(
                    f"the checksums manifest at {url} lists {asset} against "
                    f"{digest!r}, which is not a sha256"
                )
            return f"sha256:{digest}"

    logger.warning(
        "The asset is not listed in the first-party checksums manifest; caching the "
        "plugin unverified and recording it as such",
        url=url,
        asset=asset,
    )
    return ""


def artifact_for(filename: str, parts: dict[str, str], *, base: str | None = None) -> Artifact:
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

    # Terrapod's OWN plugin is not published where Pulumi's are (#1987), so it
    # resolves against its own base. `upstream` keeps meaning what it means for
    # every Pulumi-published plugin -- overriding it wholesale to reach ours
    # would send every other plugin to a host that does not have it.
    #
    # The version is a path segment rather than part of the filename, because a
    # GitHub release asset lives under its tag. That is the same shape
    # `registry.platform_tools.pulumi_mirror_url` already assembles for the
    # Pulumi CLI itself, so this is the established pattern rather than a new one.
    #
    # A warm `base` override replaces BOTH, because a mirror an operator points a
    # warm at is a mirror of whatever that plugin is -- honouring the first-party
    # split under an override would warm Terrapod's own plugin from GitHub and
    # everyone else's from the mirror, which is neither thing anyone meant.
    #
    # The digest resolver rides the SAME branch, deliberately, rather than being
    # decided again below: a resolver pointed at our release base while the
    # tarball comes from somewhere else would look for an asset that was never
    # published there. So there is exactly one place where "this is ours" is
    # decided, and `is_first_party` is it.
    resolver: DigestResolver | None = None
    if base:
        upstream_url = f"{base.rstrip('/')}/{rebuilt}"
    elif is_first_party(parts):
        upstream_url = f"{first_party_base()}/v{parts['version']}/{rebuilt}"
        resolver = functools.partial(resolve_first_party_digest, parts["version"], rebuilt)
    else:
        upstream_url = f"{upstream_base()}/{rebuilt}"

    return Artifact(
        ecosystem=ECOSYSTEM,
        name=plugin,
        version=parts["version"],
        filename=rebuilt,
        upstream_url=upstream_url,
        # Never carried on the Artifact, for either plugin. `get.pulumi.com`
        # publishes no digest beside the tarball, so for a Pulumi-published
        # plugin there is nothing to record and the row says `none-published` --
        # a declaration about that upstream rather than an absence of
        # information about the row.
        #
        # The first-party plugin's digest is resolved on the MISS path instead
        # (`resolver` above). It is not carried here because an Artifact is
        # constructed on EVERY request including a cache hit, and fetching a
        # manifest during construction would put an upstream round-trip on the
        # hit path and break `cache_only` outright.
        digest="",
        digest_resolver=resolver,
        content_type="application/gzip",
    )


def upload_coordinates(kind: str, name: str, version: str, os_: str, arch: str) -> Coordinates:
    """Where an operator-uploaded plugin tarball is served from.

    The filename is **built and then parsed back** through `_FILENAME` rather
    than validated field by field. That one regex is already the authority for
    what a plugin filename is — the download route resolves nothing else — so
    re-deriving the rules here would be a second spelling of them, and the two
    would eventually disagree about which names this cache can address.

    **Not checked against get.pulumi.com** (#2599): a plugin an operator built
    from a bridged provider of their own is exactly what gets uploaded here, and
    `pulumi` has no index to consult anyway — it asks for the file by name.
    """
    kind = (kind or "").strip()
    name = (name or "").strip()
    version = (version or "").strip().lstrip("v")
    os_ = (os_ or "").strip()
    arch = (arch or "").strip()
    filename = f"pulumi-{kind}-{name}-v{version}-{os_}-{arch}.tar.gz"
    parts = parse_filename(filename)
    if parts is None:
        raise CoordinateError(
            f"{filename!r} is not a plugin filename. A plugin is addressed as "
            "kind, name, version, os and arch — e.g. resource, aws, 6.0.0, "
            "linux, amd64."
        )
    return Coordinates(
        name=f"pulumi-{parts['kind']}-{parts['name']}",
        version=parts["version"],
        filename=filename,
        content_type="application/gzip",
    )
