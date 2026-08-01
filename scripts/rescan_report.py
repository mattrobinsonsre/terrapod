#!/usr/bin/env python3
"""Build the public notification for the release re-scan (#1212).

This issue is **public**, so it deliberately carries no detail: no advisory
ids, no package versions, not even which releases are affected. Naming a
supported release alongside what is wrong with it is a map for anyone deciding
where to look, and the people who need the detail are maintainers — who can
read it in the draft advisory.

    draft security advisory   the findings, per release   private
    code scanning             static-analysis findings    private
    this issue                "there is something to do"  public

What it does carry is a fingerprint of the finding set, so an unchanged set
does not re-notify. The body is refreshed every run regardless: those are two
different decisions, and conflating them once left the issue displaying stale
information after the tooling had already improved.

    rescan_report.py <findings-dir> <out.md> [advisory-urls.json]
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys

MARKER = "terrapod-release-rescan"
_REPO = os.environ.get("GITHUB_REPOSITORY", "mattrobinsonsre/terrapod")
_REPO_URL = f"https://github.com/{_REPO}"


def load(directory: pathlib.Path) -> list[dict]:
    """Dependency findings only. Code findings are refused, not skipped."""
    reports = []
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"skipping unreadable {path}: {exc}", file=sys.stderr)
            continue
        # Must carry findings: the count-only files also have a "release", and
        # accepting one registers the release as present-but-empty.
        if not (isinstance(data, dict) and "release" in data and "findings" in data):
            continue
        if data.get("kind") == "code":
            print(
                f"REFUSING code-kind findings from {path}: this issue is public",
                file=sys.stderr,
            )
            continue
        reports.append(data)
    return reports


def code_counts(directory: pathlib.Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in directory.rglob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and "code_findings" in data:
            counts[data["release"]] = counts.get(data["release"], 0) + int(data["code_findings"])
    return counts


def fingerprint(reports: list[dict], counts: dict[str, int]) -> str:
    """Identity of the finding set — computed, never displayed.

    Excludes which image or architecture saw a finding: the same advisory
    across five images is one thing to fix, and letting the image list move the
    fingerprint would re-notify on noise.
    """
    keys = {
        f"{r['release']}|{f.get('id', '')}"
        for r in reports
        for f in (r.get("findings") or [])
    }
    keys |= {f"{rel}|code:{n}" for rel, n in counts.items() if n}
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:16]


_LEDE = (
    "The scheduled re-scan of supported releases has findings that need "
    "attention."
)
_WHY_EMPTY = (
    "**Deliberately no detail here.** This issue is public, and naming a "
    "supported release together with what is wrong with it tells anyone "
    "reading exactly where to look. The findings are recorded privately "
    "instead:"
)
_ADVISORIES = (
    f"- **[Security advisories]({_REPO_URL}/security/advisories)** — a draft "
    "per affected release: package, installed version, fixed version, advisory "
    "id. Drafts are visible to maintainers only."
)
_CODESCAN = (
    f"- **[Code scanning]({_REPO_URL}/security/code-scanning)** — static "
    "analysis findings in Terrapod's own source at that tag."
)
_PUBLISH = (
    "Drafts are published when the patch release that fixes them ships — that "
    "is what turns them into the operator-facing record."
)
_REFS = (
    f"Policy: [docs/cve-policy.md]({_REPO_URL}/blob/main/docs/cve-policy.md). "
    "Procedure: *Patching an older release line* in "
    f"[AGENTS.md]({_REPO_URL}/blob/main/AGENTS.md)."
)
_FOOTER = (
    "Refreshed in place; only notifies when the finding set actually changes."
)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    directory, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

    reports = load(directory)
    counts = code_counts(directory)
    total = sum(len(r.get("findings") or []) for r in reports) + sum(counts.values())
    fp = fingerprint(reports, counts)

    if not total:
        out.write_text(f"<!-- {MARKER}:{fp} -->\n\nNo findings.\n")
        print(f"fingerprint={fp}")
        print("has_findings=false")
        return 0

    body = [
        f"<!-- {MARKER}:{fp} -->",
        "",
        _LEDE,
        "",
        _WHY_EMPTY,
        "",
        _ADVISORIES,
        _CODESCAN,
        "",
        _PUBLISH,
        "",
        _REFS,
        "",
        _FOOTER,
    ]

    # Advisory URLs are safe to include: a draft is unreadable without
    # repository access, so the link discloses nothing on its own.
    if len(sys.argv) > 3 and pathlib.Path(sys.argv[3]).exists():
        try:
            urls = json.loads(pathlib.Path(sys.argv[3]).read_text())
        except (json.JSONDecodeError, OSError):
            urls = {}
        live = [u for u in urls.values() if u]
        if live:
            body += ["", "Drafts raised or refreshed this run:"]
            body += [f"- {u}" for u in live]

    out.write_text("\n".join(body) + "\n")
    print(f"fingerprint={fp}")
    print("has_findings=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
