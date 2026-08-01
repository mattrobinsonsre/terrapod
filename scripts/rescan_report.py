#!/usr/bin/env python3
"""Aggregate release re-scan findings into an issue body (#1212).

Every scanner in the re-scan workflow normalises its output to one JSON file
per (release, source) with a common shape, and this collapses the lot into a
single report:

    {"release": "v1.1.1",
     "source": "image:terrapod-api (amd64)",
     "kind": "dependency" | "code",
     "findings": [{...}]}

Two things here matter more than the formatting.

**The fingerprint.** A scheduled job that re-notifies on an unchanged finding
set gets muted within a week, and then the one time it matters nobody looks. So
the body carries a fingerprint of the finding *set* (not the run, not the
timestamp), and the workflow only touches the issue when that changes. Re-runs
that find the same things are silent by construction rather than by someone
remembering to check.

**Grouping by release.** The follow-up action is a patch release, and a patch
release is per-release — so that is the axis the report is organised on, even
though scanning is organised by image and architecture.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys

MARKER = "terrapod-release-rescan"

# Issue bodies do not resolve relative links the way a file in the repo does,
# so every link here is absolute, built from the repository the run belongs to.
_REPO = os.environ.get("GITHUB_REPOSITORY", "mattrobinsonsre/terrapod")
_REPO_URL = f"https://github.com/{_REPO}"

# Report copy lives here rather than inline in the list literals below.
# Implicit concatenation inside a list is how a missing comma silently merges
# two entries into one and looks exactly like deliberate line-wrapping, so the
# prose is assembled in parentheses where there is no comma to lose.
_INTRO = (
    "Automated re-scan of the releases we still support. Raised by "
    "`.github/workflows/release-rescan.yml`; the policy it enforces is "
    f"[docs/cve-policy.md]({_REPO_URL}/blob/main/docs/cve-policy.md)."
)
_WHY = (
    "**Why this can appear on a release that was clean when it shipped:** the "
    "release gate uses `ignore-unfixed`, so a vulnerability only becomes "
    "actionable once upstream publishes a fix. Everything below has a fix "
    "available *now* that did not exist, or was not yet taken, when the "
    "release was cut."
)
_SCOPE = (
    "**This report covers dependency findings only.** Code findings from the "
    "source scan go to code scanning, which is visible to write-access users "
    "rather than the world — a static-analysis hit in Terrapod's own source is "
    "a potential undisclosed vulnerability, and this issue is public."
)
_CLEAN = (
    "Every supported release is clean against current advisory data and "
    "current rules, with the accepted-risk register applied."
)
_TRIAGE_HEAD = (
    "Each release above needs a judgement call, not an automatic patch. Worth "
    "checking in this order:"
)
_TRIAGE_FIXED = (
    "1. **Is it already fixed on `main`?** If so the fix needs backporting "
    "to that release line, which is what the patch release carries."
)
_TRIAGE_REACHABLE = (
    "2. **Is it reachable in the way Terrapod uses the component?** If not, "
    "it belongs in the accepted-risk register with its reasoning and exit "
    "condition — not silently ignored, and not patched for appearance."
)
_TRIAGE_URGENCY = (
    "3. **Does it warrant a patch release on its own**, or can it wait for "
    "the next scheduled one? Cadence is roughly weekly; an unreachable "
    "medium-impact finding can reasonably wait, a reachable critical one "
    "cannot."
)
_TRIAGE = [_TRIAGE_FIXED, _TRIAGE_REACHABLE, _TRIAGE_URGENCY]
_FOOTER = (
    "This issue is refreshed in place while the finding set is unchanged, so "
    "it will not re-notify on every run."
)


def load_code_counts(directory: pathlib.Path) -> dict[str, int]:
    """release -> number of code findings, from the count-only files."""
    counts: dict[str, int] = {}
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and "code_findings" in data:
            counts[data["release"]] = counts.get(data["release"], 0) + int(data["code_findings"])
    return counts


def load(directory: pathlib.Path) -> list[dict]:
    reports = []
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"skipping unreadable {path}: {exc}", file=sys.stderr)
            continue
        # Must carry findings. The count-only files also have a "release", and
        # accepting one here registers the release as present-but-empty, which
        # is enough to hide a code-only release from the branch that reports it.
        if not (isinstance(data, dict) and "release" in data and "findings" in data):
            continue
        if data.get("kind") == "code":
            # The workflow sends code findings to code scanning and never here,
            # but this report is published as a PUBLIC issue — so refuse them at
            # the door rather than relying on the caller to have got it right.
            # Silently dropping would be worse: it would look like the scan
            # found nothing.
            print(
                f"REFUSING code-kind findings from {path}: this report is public, "
                "and code findings belong in code scanning",
                file=sys.stderr,
            )
            continue
        reports.append(data)
    return reports


def fingerprint(reports: list[dict]) -> str:
    """Identity of the finding *set*, stable across runs and orderings.

    Deliberately excludes the source: the same CVE found in five images of one
    release is one problem to fix, and letting the image list move the
    fingerprint would re-notify on noise (an image list changing between
    releases, a scan legitimately skipped).
    """
    keys = set()
    for report in reports:
        for finding in report.get("findings") or []:
            keys.add(f"{report['release']}|{finding.get('id', '')}")
    digest = hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:16]
    return digest


def _code_pointer(release: str, count: int) -> str:
    """Say that code findings exist and where, without saying what they are."""
    return (
        f"> **{count} code finding(s)** from the source scan of `{release}`, "
        "not listed here. Details are in "
        f"[code scanning]({_REPO_URL}/security/code-scanning?query=is%3Aopen) "
        "under the "
        f"`release-rescan-{release}` category — this issue is public, and a "
        "static-analysis hit in Terrapod's own source is a potential "
        "undisclosed vulnerability."
    )


def _dependency_table(findings: list[dict]) -> list[str]:
    lines = [
        "| Severity | Package | Installed | Fixed in | Advisory | Seen in |",
        "|---|---|---|---|---|---|",
    ]
    for f in sorted(findings, key=lambda x: (x.get("severity", ""), x.get("id", ""))):
        where = ", ".join(sorted(set(f.get("sources") or [])))
        lines.append(
            f"| {f.get('severity', '?')} | `{f.get('package', '?')}` "
            f"| {f.get('installed', '?')} | **{f.get('fixed', '?')}** "
            f"| {f.get('id', '?')} | {where} |"
        )
    return lines



def merge(reports: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """release -> kind -> findings, deduplicated, recording where each was seen."""
    out: dict[str, dict[str, dict[str, dict]]] = {}
    for report in reports:
        release = report["release"]
        kind = report.get("kind", "dependency")
        source = report.get("source", "?")
        bucket = out.setdefault(release, {}).setdefault(kind, {})
        for finding in report.get("findings") or []:
            key = f"{finding.get('id')}|{finding.get('package')}"
            entry = bucket.setdefault(key, {**finding, "sources": []})
            entry["sources"].append(source)
    return {r: {k: list(v.values()) for k, v in kinds.items()} for r, kinds in out.items()}


def render(
    reports: list[dict], scanned: list[str], code_counts: dict[str, int] | None = None
) -> tuple[str, str, bool]:
    code_counts = code_counts or {}
    merged = merge(reports)
    fp = fingerprint(reports)
    total = sum(len(f) for kinds in merged.values() for f in kinds.values())

    body = [
        f"<!-- {MARKER}:{fp} -->",
        "",
        _INTRO,
        "",
        _WHY,
        "",
        _SCOPE,
        "",
        f"Scanned: {', '.join(scanned) if scanned else '(none)'}",
        "",
    ]

    # Releases with no dependency findings but some code findings still need
    # saying, or the issue reads as "clean" when it is not.
    for release in sorted(set(code_counts) - set(merged), reverse=True):
        if code_counts[release]:
            body += [f"## {release} — code findings only", "",
                     _code_pointer(release, code_counts[release]), ""]
            total += code_counts[release]

    if not total:
        body += ["## No findings", "", _CLEAN]
        return "\n".join(body), fp, False

    for release in sorted(merged, reverse=True):
        kinds = merged[release]
        count = sum(len(v) for v in kinds.values())
        body += [f"## {release} — {count} finding(s)", ""]
        if code_counts.get(release):
            body += [_code_pointer(release, code_counts[release]), ""]
        if kinds.get("dependency"):
            body += ["**Dependencies and packages**", ""]
            body += _dependency_table(kinds["dependency"])
            body += [""]

    body += ["## What to do with this", "", _TRIAGE_HEAD, ""]
    body += _TRIAGE
    body += ["", _FOOTER]
    return "\n".join(body), fp, True


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: rescan_report.py <findings-dir> <out.md> [tag ...]", file=sys.stderr)
        return 2
    directory, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    scanned = sys.argv[3:]
    reports = load(directory)
    body, fp, has_findings = render(reports, scanned, load_code_counts(directory))
    out.write_text(body)
    print(f"fingerprint={fp}")
    print(f"has_findings={'true' if has_findings else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
