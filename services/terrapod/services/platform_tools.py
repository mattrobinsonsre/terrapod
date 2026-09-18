"""Platform-scoped third-party tools: where to fetch them and how to check them (#1208).

`opa`, `trivy` and `checkov` used to be baked into Terrapod's published images.
This module is what replaced that: the per-tool knowledge of the upstream asset
layout, so the same pull-through cache that serves terraform/tofu/terragrunt can
serve these too and the version becomes an operator-set Helm value.

Three things differ from the CLI tools in `artifact_verification`:

**No signatures exist.** None of the three publishes a GPG-signed SHA256SUMS, so
there is no `signature` level to offer — only `checksum`. That is not a
regression: the Dockerfiles that used to bake these in checked a checksum and
nothing more. The material each publishes differs in strength and the table below
says so rather than flattening it:

    opa      a sibling <asset>.sha256 next to every asset      publisher-published
    trivy    one trivy_<version>_checksums.txt per release     publisher-published
    checkov  nothing — only the GitHub release API's digest    registry-computed

Checkov's is genuinely weaker: a digest GitHub computed over whatever was
uploaded, not something the publisher signed or even wrote down. It is the only
material that exists, and pretending otherwise would be worse than saying so.

**No partial-version resolution.** These are pinned exactly in Helm, one version
per deployment. There is no `1.19` → `1.19.0` step and no `allow_prerelease`
interaction.

**The artifact shapes differ.** OPA ships a bare executable, Trivy a .tar.gz,
Checkov a zip around a single self-contained binary. The cache stores whatever
upstream served, byte for byte; unpacking is the runner's job (it knows which
tool it asked for), which keeps this side free of archive handling on a
potentially large stream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import httpx
import structlog

from terrapod.config import settings
from terrapod.http_retry import arequest_with_retry
from terrapod.services.artifact_verification import VerificationError

logger = structlog.get_logger(__name__)

PLATFORM_TOOLS = frozenset({"opa", "trivy", "checkov"})

#: Tools whose upstream facts live here but which are NOT platform-scoped.
#:
#: Pulumi was a platform tool until #1559, when its version became a property of
#: the workspace rather than of the deployment. It is a CLI tool now, resolved
#: and cached by `binary_cache_service` like terraform and tofu. What kept it
#: here is the asset layout below -- the `linux-x64` platform spelling, the
#: tar.gz holding the language plugins, the checksum manifest with the version
#: spelled two ways in one URL. None of that is any less true for a per-workspace
#: tool, and moving it would have bought nothing but churn.
_NON_PLATFORM_TOOLS = frozenset({"pulumi", "node", "go"})

#: Every tool this module describes, however it is scoped.
DESCRIBED_TOOLS = PLATFORM_TOOLS | _NON_PLATFORM_TOOLS

#: Node's platform naming. Like Pulumi it says x64 where Go says amd64, and its
#: archive root carries both the version and this spelling --
#: `node-v22.20.0-linux-x64/` -- which is why the runner strips the root rather
#: than trying to name it (#1566).
_NODE_PLATFORM = {
    ("linux", "amd64"): "linux-x64",
    ("linux", "arm64"): "linux-arm64",
    ("darwin", "amd64"): "darwin-x64",
    ("darwin", "arm64"): "darwin-arm64",
}

#: Go's own platform naming -- the one publisher that needs no translation,
#: because Terrapod already speaks Go-style os/arch. Its archive root is a plain
#: `go/`, so the member could be named through it; the runner strips the root
#: anyway, for one rule across all three runtimes (#1566).
_GO_PLATFORM = {
    ("linux", "amd64"): "linux-amd64",
    ("linux", "arm64"): "linux-arm64",
    ("darwin", "amd64"): "darwin-amd64",
    ("darwin", "arm64"): "darwin-arm64",
}

#: Pulumi's own platform naming, which differs from Go's for amd64.
_PULUMI_PLATFORM = {
    ("linux", "amd64"): "linux-x64",
    ("linux", "arm64"): "linux-arm64",
    ("darwin", "amd64"): "darwin-x64",
    ("darwin", "arm64"): "darwin-arm64",
}

#: How each tool names the platform in its asset filenames. Terrapod speaks
#: Go-style os/arch throughout; upstream does not always agree (Trivy uses
#: "Linux-64bit", Checkov an inconsistent "linux_X86_64" / "linux_arm64").
_TRIVY_PLATFORM = {
    ("linux", "amd64"): "Linux-64bit",
    ("linux", "arm64"): "Linux-ARM64",
    ("darwin", "amd64"): "macOS-64bit",
    ("darwin", "arm64"): "macOS-ARM64",
}
_CHECKOV_PLATFORM = {
    ("linux", "amd64"): "linux_X86_64",
    ("linux", "arm64"): "linux_arm64",
    ("darwin", "amd64"): "darwin_X86_64",
}


class UnsupportedPlatformError(ValueError):
    """The tool does not publish an asset for this os/arch."""


@dataclass(frozen=True)
class PlatformToolSpec:
    """How to fetch and check one platform tool."""

    #: What the cached object holds, so the runner knows how to unpack it.
    archive: Literal["raw", "targz", "zip"]
    #: Path within the archive to the executable ("" for `raw`).
    member: str
    #: Content type to store the cached object under.
    content_type: str


SPECS: dict[str, PlatformToolSpec] = {
    # A statically-linked bare executable — nothing to unpack.
    "opa": PlatformToolSpec(archive="raw", member="", content_type="application/octet-stream"),
    # A tarball whose only interesting member is the binary itself.
    "trivy": PlatformToolSpec(archive="targz", member="trivy", content_type="application/gzip"),
    # A PyInstaller bundle: one ~60MB self-contained executable in a zip.
    # The tarball unpacks to `pulumi/pulumi` plus the language plugins beside it.
    "pulumi": PlatformToolSpec(
        archive="targz", member="pulumi/pulumi", content_type="application/gzip"
    ),
    "checkov": PlatformToolSpec(
        archive="zip", member="dist/checkov", content_type="application/zip"
    ),
    # The Node runtime a Pulumi TypeScript/JavaScript program needs (#1566).
    # `pulumi-language-nodejs` ships inside the Pulumi tarball and is a shim: it
    # shells out to `node`, which is not in the runner image and never was. The
    # member is the path INSIDE the archive's root, because that root is
    # `node-v<version>-<platform>/` and this table knows neither.
    "node": PlatformToolSpec(archive="targz", member="bin/node", content_type="application/gzip"),
    # The Go toolchain a Pulumi Go program needs (#1566). Pulumi compiles the
    # program, so this is the whole toolchain rather than a runtime.
    "go": PlatformToolSpec(archive="targz", member="bin/go", content_type="application/gzip"),
}


def _mirror(tool: str) -> str:
    cfg = settings.registry.platform_tools
    return {
        "opa": cfg.opa_mirror_url,
        "trivy": cfg.trivy_mirror_url,
        "checkov": cfg.checkov_mirror_url,
        # Pulumi's mirror moved with it to the binary cache (#1559).
        "pulumi": settings.registry.binary_cache.pulumi_mirror_url,
        "node": settings.registry.binary_cache.node_mirror_url,
        "go": settings.registry.binary_cache.go_mirror_url,
    }[tool].rstrip("/")


def configured_version(tool: str) -> str:
    """The version this deployment pins for `tool`."""
    cfg = settings.registry.platform_tools
    return {
        "opa": cfg.opa_version,
        "trivy": cfg.trivy_version,
        "checkov": cfg.checkov_version,
    }[tool]


async def _go_index() -> list[dict]:
    """Go's release index: every release, with its files and their sha256."""
    url = settings.registry.binary_cache.go_version_index_url
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await arequest_with_retry(client, "GET", url)
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


def download_url(tool: str, version: str, os_: str, arch: str) -> str:
    """Upstream URL for a tool's per-platform asset.

    Raises UnsupportedPlatformError when the publisher ships nothing for this
    os/arch — better than a 404 the caller has to interpret.
    """
    base = _mirror(tool)
    if tool == "opa":
        # OPA offers both dynamically- and statically-linked builds. Take the
        # static one: the runner image is slim and the API image is Debian, and
        # a binary with no libc expectations works on both without thought.
        if os_ not in ("linux", "darwin") or arch not in ("amd64", "arm64"):
            raise UnsupportedPlatformError(f"opa publishes no static asset for {os_}/{arch}")
        return f"{base}/v{version}/opa_{os_}_{arch}_static"
    if tool == "trivy":
        plat = _TRIVY_PLATFORM.get((os_, arch))
        if plat is None:
            raise UnsupportedPlatformError(f"trivy publishes no asset for {os_}/{arch}")
        return f"{base}/v{version}/trivy_{version}_{plat}.tar.gz"
    if tool == "checkov":
        plat = _CHECKOV_PLATFORM.get((os_, arch))
        if plat is None:
            raise UnsupportedPlatformError(f"checkov publishes no asset for {os_}/{arch}")
        return f"{base}/{version}/checkov_{plat}.zip"
    if tool == "pulumi":
        plat = _PULUMI_PLATFORM.get((os_, arch))
        if plat is None:
            raise UnsupportedPlatformError(f"pulumi publishes no asset for {os_}/{arch}")
        return f"{base}/v{version}/pulumi-v{version}-{plat}.tar.gz"
    if tool == "go":
        plat = _GO_PLATFORM.get((os_, arch))
        if plat is None:
            raise UnsupportedPlatformError(f"go publishes no asset for {os_}/{arch}")
        return f"{base}/go{version}.{plat}.tar.gz"
    if tool == "node":
        plat = _NODE_PLATFORM.get((os_, arch))
        if plat is None:
            raise UnsupportedPlatformError(f"node publishes no asset for {os_}/{arch}")
        # The .tar.gz rather than the smaller .tar.xz: the runner unpacks with
        # the stdlib's tarfile, and gzip is what every other cached tool uses.
        return f"{base}/v{version}/node-v{version}-{plat}.tar.gz"
    raise ValueError(f"not a platform tool: {tool!r}")


def _asset_name(tool: str, version: str, os_: str, arch: str) -> str:
    return download_url(tool, version, os_, arch).rsplit("/", 1)[-1]


async def _expected_sha256(
    client: httpx.AsyncClient, tool: str, version: str, os_: str, arch: str
) -> str:
    """The publisher's expected SHA-256 for this asset, lowercase hex.

    Raises VerificationError when the material cannot be fetched or does not
    name this asset — fail closed, exactly like the CLI-binary path.
    """
    base = _mirror(tool)
    asset = _asset_name(tool, version, os_, arch)

    if tool == "opa":
        # A sibling .sha256 per asset: "<hex>  <filename>" (or bare hex).
        resp = await arequest_with_retry(client, "GET", f"{base}/v{version}/{asset}.sha256")
        if resp.status_code != 200:
            raise VerificationError(
                f"could not fetch the opa checksum for {asset} (HTTP {resp.status_code})"
            )
        first = resp.text.strip().split()
        if not first:
            raise VerificationError(f"opa checksum file for {asset} was empty")
        return first[0].lower()

    if tool == "trivy":
        # One manifest for the whole release, "<hex>  <filename>" per line.
        resp = await arequest_with_retry(
            client, "GET", f"{base}/v{version}/trivy_{version}_checksums.txt"
        )
        if resp.status_code != 200:
            raise VerificationError(
                f"could not fetch the trivy checksums manifest for {version} "
                f"(HTTP {resp.status_code})"
            )
        for line in resp.text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].lstrip("*") == asset:
                return parts[0].lower()
        raise VerificationError(f"{asset} is not listed in the trivy checksums manifest")

    if tool == "go":
        # Go publishes no checksum manifest: the sha256 sits beside each file in
        # the same release index used to resolve a version. One document, both
        # jobs, over TLS from the same host as the artifact.
        for release in await _go_index():
            for f in release.get("files", []):
                if f.get("filename") == asset and f.get("sha256"):
                    return str(f["sha256"]).lower()
        raise VerificationError(f"{asset} is not listed in the go release index")

    if tool == "node":
        # One SHASUMS256.txt per release, the same "<hex>  <filename>" shape as
        # Trivy's and Pulumi's. Node signs this file with its release keys, but
        # verifying that would mean pinning and rotating a keyring for a fourth
        # publisher; the checksum is what the other cached tools get and it is
        # fetched over TLS from the same host as the artifact.
        resp = await arequest_with_retry(client, "GET", f"{base}/v{version}/SHASUMS256.txt")
        if resp.status_code != 200:
            raise VerificationError(
                f"could not fetch the node checksums manifest for {version} "
                f"(HTTP {resp.status_code})"
            )
        for line in resp.text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].lstrip("*") == asset:
                return parts[0].lower()
        raise VerificationError(f"{asset} is not listed in the node checksums manifest")

    if tool == "pulumi":
        # One manifest for the whole release, same "<hex>  <filename>" shape as
        # Trivy's. Note the two spellings of the version in one URL: the release
        # is tagged `v3.208.0` but the manifest inside it is named without the
        # `v`. Getting that wrong 404s, which fails closed as an unverifiable
        # artifact rather than as the typo it is.
        resp = await arequest_with_retry(
            client, "GET", f"{base}/v{version}/pulumi-{version}-checksums.txt"
        )
        if resp.status_code != 200:
            raise VerificationError(
                f"could not fetch the pulumi checksums manifest for {version} "
                f"(HTTP {resp.status_code})"
            )
        for line in resp.text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].lstrip("*") == asset:
                return parts[0].lower()
        raise VerificationError(f"{asset} is not listed in the pulumi checksums manifest")

    if tool == "checkov":
        # No checksum file exists; the release API's per-asset digest is the
        # only material there is. Weaker, and documented as such.
        api = settings.registry.platform_tools.checkov_checksum_api_url.rstrip("/")
        resp = await arequest_with_retry(client, "GET", f"{api}/{version}")
        if resp.status_code != 200:
            raise VerificationError(
                f"could not reach the checkov release API for {version} "
                f"(HTTP {resp.status_code}). Checkov publishes no checksum file, so "
                f"this is the only verification material available; set "
                f"registry.platform_tools.verify=off to accept the artifact unverified."
            )
        try:
            payload = json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise VerificationError(f"checkov release API returned non-JSON: {exc}") from exc
        for entry in payload.get("assets") or []:
            if entry.get("name") == asset:
                digest = (entry.get("digest") or "").removeprefix("sha256:").lower()
                if not digest:
                    raise VerificationError(
                        f"the checkov release API reports no digest for {asset}"
                    )
                return digest
        raise VerificationError(f"{asset} is not present in the checkov {version} release")

    raise VerificationError(f"not a platform tool: {tool!r}")


async def verify_platform_tool(
    client: httpx.AsyncClient,
    tool: str,
    version: str,
    os_: str,
    arch: str,
    artifact_sha256_hex: str,
    level: str | None = None,
) -> None:
    """Check a downloaded artifact against the publisher's checksum.

    No-op when the level is off. Raises VerificationError on any mismatch or
    unobtainable material — the caller must cache nothing and serve nothing.

    `level` is the caller's, because since #1559 not every tool verified this way
    is a *platform* tool: pulumi is a per-workspace CLI tool that simply has no
    signature to check, and its switch is `binary_cache.verify`, not
    `platform_tools.verify`. Defaulting to the platform-tools switch keeps every
    existing caller unchanged.
    """
    if level is None:
        level = settings.registry.platform_tools.verify
    if level == "off":
        logger.warning(
            "platform-tool verification disabled (verify=off) — trusting upstream bytes",
            tool=tool,
            version=version,
        )
        return

    expected = await _expected_sha256(client, tool, version, os_, arch)
    if expected != artifact_sha256_hex.lower():
        raise VerificationError(
            f"checksum mismatch for {tool} {version} {os_}/{arch}: downloaded "
            f"{artifact_sha256_hex}, publisher says {expected} — refusing to cache "
            f"(possible tampering)"
        )
