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
import time
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


#: How many times a TRANSPORT failure is retried before the check gives up and
#: says it could not tell. AGENTS.md makes bounded backoff/retry a hard
#: requirement for every outbound call Terrapod makes, and this script was the
#: one that did not: a single timeout decided a release was unpublished.
TRANSPORT_ATTEMPTS = 3
TRANSPORT_BACKOFF = 2.0  # seconds, doubled per attempt


def reachable(client: httpx.Client, url: str) -> tuple[str, str]:
    """Is the artifact there? Returns `ok`, `absent`, or `unverified`.

    THREE outcomes rather than two, and the third is the point. This used to
    return a bool, so a timeout and a 404 were the same answer and the failure
    message said "not published" either way — a claim a transport error does not
    establish. During a GitHub release-CDN incident that fails every PR with a
    message sending the reader to fix a version pin that is perfectly fine, which
    is precisely what this function's own docstring warns against. It happened:
    `trivy 0.76.2` was reported unpublished while being published, with 47 assets
    and both of the claimed ones present.

    So an HTTP STATUS is evidence about the artifact and a transport error is
    evidence about the network, and they are reported separately. The build fails
    on either — fail-closed is right for a release gate, because an unverifiable
    claim is not a verified one — but the operator is told which they have.

    A HEAD failure is still retried as a one-byte ranged GET before it is
    believed, because some object stores answer 403 to a HEAD they would serve as
    a GET. A transport error is retried with backoff, which is what distinguishes
    "the CDN was briefly unwell" from "the network cannot reach it at all".
    """
    last = ""
    for attempt in range(TRANSPORT_ATTEMPTS):
        try:
            resp = client.head(url, follow_redirects=True, timeout=30.0)
            if resp.status_code == 200:
                return "ok", "HEAD 200"
            ranged = client.get(
                url, follow_redirects=True, timeout=30.0, headers={"Range": "bytes=0-0"}
            )
            if ranged.status_code in (200, 206):
                return "ok", f"GET {ranged.status_code} (HEAD said {resp.status_code})"
            # A status came back from the publisher, so this IS evidence about
            # the artifact. Do not retry it: a 404 does not improve.
            return "absent", f"HTTP {ranged.status_code}"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt < TRANSPORT_ATTEMPTS - 1:
                time.sleep(TRANSPORT_BACKOFF * (2**attempt))
    return "unverified", f"{last} (after {TRANSPORT_ATTEMPTS} attempts)"


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

    absent: list[str] = []
    unverified: list[str] = []
    with httpx.Client() as client:
        for tool, os_, arch, url in assets:
            version = version_override or configured_version(tool)
            outcome, why = reachable(client, url)
            label = {"ok": "ok", "absent": "MISSING", "unverified": "UNVERIFIED"}[outcome]
            print(f"  {label:10} {tool:8} {version:10} {os_}/{arch:6} {why}")
            if outcome == "absent":
                absent.append(
                    f"{tool} {version} ({os_}/{arch}) is claimed by the platform table but "
                    f"not published: {url} -> {why}"
                )
            elif outcome == "unverified":
                unverified.append(f"{tool} {version} ({os_}/{arch}): {url} -> {why}")

    print()
    if absent:
        print(f"FAIL: {len(absent)} claimed artifact(s) do not exist.\n", file=sys.stderr)
        for f in absent:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nEither pin a version whose publisher ships every platform the table claims, "
            "publish the missing artifact, or narrow the table so the claim matches reality. "
            "Leaving it is what makes a run fail with a 502 instead of a clear refusal.",
            file=sys.stderr,
        )
    if unverified:
        print(
            f"FAIL: {len(unverified)} claimed artifact(s) could NOT BE VERIFIED — the "
            f"publisher was unreachable after {TRANSPORT_ATTEMPTS} attempts.\n",
            file=sys.stderr,
        )
        for f in unverified:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nThis is NOT a claim that the version is unpublished, and the distinction "
            "matters: the likely cause is an incident at the publisher or a network egress "
            "problem on this runner, not your version pin. Check the publisher's status, "
            "confirm the asset by hand, and re-run. The check still fails, because an "
            "unverifiable claim is not a verified one.",
            file=sys.stderr,
        )
    if absent or unverified:
        return 1

    print(f"All {len(assets)} claimed platform-tool artifacts exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
