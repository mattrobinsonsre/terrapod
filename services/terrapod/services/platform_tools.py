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
    "checkov": PlatformToolSpec(
        archive="zip", member="dist/checkov", content_type="application/zip"
    ),
}


def _mirror(tool: str) -> str:
    cfg = settings.registry.platform_tools
    return {
        "opa": cfg.opa_mirror_url,
        "trivy": cfg.trivy_mirror_url,
        "checkov": cfg.checkov_mirror_url,
    }[tool].rstrip("/")


def configured_version(tool: str) -> str:
    """The version this deployment pins for `tool`."""
    cfg = settings.registry.platform_tools
    return {
        "opa": cfg.opa_version,
        "trivy": cfg.trivy_version,
        "checkov": cfg.checkov_version,
    }[tool]


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
) -> None:
    """Check a downloaded platform tool against the publisher's checksum.

    No-op when `verify` is off. Raises VerificationError on any mismatch or
    unobtainable material — the caller must cache nothing and serve nothing.
    """
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
