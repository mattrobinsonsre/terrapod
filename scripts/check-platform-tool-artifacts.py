#!/usr/bin/env python3
"""Every platform-tool artifact we claim must actually exist (#2066).

`services/terrapod/services/platform_tools.py` holds a per-tool table of the
platforms each publisher ships. That table is **static and version-independent**
— it says `checkov` has a `linux/arm64` asset, full stop — and `download_url()`
raises `UnsupportedPlatformError` for anything outside it, which the router turns
into a 400 the runner treats as final.

That is the right shape, and it is why this check exists: availability is
actually a property of a *(version, platform)* pair, and the table cannot
express that. Upstream Checkov stopped publishing `checkov_linux_arm64.zip` at
3.3.21 while the table went on claiming it, so the fetch 404'd, the API
answered 502, the runner retried a deterministic failure as though it were
transient, and every security scan on an arm64 pool recorded `errored` from
v1.9.0 until someone noticed. Nothing in CI contradicted the claim, because
nothing asked the publisher.

So this asks. For every `(tool, os, arch)` the table claims — derived from
`download_url()` itself rather than restated here, so a tool gaining or losing
a platform cannot drift from what is checked — the asset must be fetchable at
the version this deployment pins, and the verification material
`_expected_sha256()` will reach for must be fetchable too. A tool whose
checksum material is missing fails the release gate at tag time with
`verify=checksum`, so an unreachable manifest is a release blocker, not a
detail.

Network-bound and therefore not a pytest tier: it runs as its own CI step
against a full checkout, the same way `scripts/docs-audit/` does.

    python3 scripts/check-platform-tool-artifacts.py          # pinned versions
    python3 scripts/check-platform-tool-artifacts.py 3.3.28   # a candidate bump

With a version argument it checks that version for every tool it applies to,
which is what makes a G0 bump answerable before it is committed rather than
after it ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services"))

import httpx  # noqa: E402

from terrapod.services.platform_tools import (  # noqa: E402
    PLATFORM_TOOLS,
    UnsupportedPlatformError,
    configured_version,
    download_url,
)

#: Every os/arch the binary cache will accept in a request. The intersection
#: with what each tool's table claims is what gets checked — so this list
#: deliberately over-reaches, and `UnsupportedPlatformError` does the narrowing.
#: It mirrors `binary_cache_service.VALID_OS` x `VALID_ARCH`.
CANDIDATE_PLATFORMS = [
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
]

#: A floor, so a refactor that stops resolving platforms fails loudly instead of
#: checking nothing and passing. Three tools, at least two platforms each.
MIN_CHECKED = 6


def claimed_assets(version_override: str | None) -> list[tuple[str, str, str, str]]:
    """(tool, os, arch, url) for every platform the tools' own tables claim."""
    out: list[tuple[str, str, str, str]] = []
    for tool in sorted(PLATFORM_TOOLS):
        version = version_override or configured_version(tool)
        for os_, arch in CANDIDATE_PLATFORMS:
            try:
                url = download_url(tool, version, os_, arch)
            except UnsupportedPlatformError:
                continue  # the table says nothing is published here; believed
            out.append((tool, os_, arch, url))
    return out


def reachable(client: httpx.Client, url: str) -> tuple[bool, str]:
    """Is the artifact there? HEAD, falling back to a ranged GET.

    A release-asset redirect chain does not always honour HEAD — some object
    stores answer 403 to a HEAD they would serve as a GET — so a HEAD failure is
    retried as a one-byte ranged GET before it is believed. Reporting a
    published asset as missing would send someone to fix a release that is fine.
    """
    try:
        resp = client.head(url, follow_redirects=True, timeout=30.0)
        if resp.status_code == 200:
            return True, "HEAD 200"
        resp = client.get(url, follow_redirects=True, timeout=30.0, headers={"Range": "bytes=0-0"})
        if resp.status_code in (200, 206):
            return True, f"GET {resp.status_code} (HEAD said {resp.status_code})"
        return False, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    version_override = sys.argv[1] if len(sys.argv) > 1 else None
    assets = claimed_assets(version_override)

    if len(assets) < MIN_CHECKED:
        print(
            f"FAIL: only {len(assets)} platform/tool combinations resolved, expected at "
            f"least {MIN_CHECKED}. Either the platform tables shrank or this check has "
            f"stopped resolving them — it must not pass by checking nothing.",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    with httpx.Client() as client:
        for tool, os_, arch, url in assets:
            version = version_override or configured_version(tool)
            ok, why = reachable(client, url)
            status = "ok" if ok else "MISSING"
            print(f"  {status:8} {tool:8} {version:10} {os_}/{arch:6} {why}")
            if not ok:
                failures.append(
                    f"{tool} {version} ({os_}/{arch}) is claimed by the platform table but "
                    f"not published: {url} -> {why}"
                )

    print()
    if failures:
        print(f"FAIL: {len(failures)} claimed artifact(s) do not exist.\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nEither pin a version whose publisher ships every platform the table claims, "
            "publish the missing artifact, or narrow the table so the claim matches reality. "
            "Leaving it is what makes a run fail with a 502 instead of a clear refusal.",
            file=sys.stderr,
        )
        return 1

    print(f"All {len(assets)} claimed platform-tool artifacts exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
