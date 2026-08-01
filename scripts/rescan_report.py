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
import pathlib
import sys

MARKER = "terrapod-release-rescan"


def load(directory: pathlib.Path) -> list[dict]:
    reports = []
    for path in sorted(directory.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"skipping unreadable {path}: {exc}", file=sys.stderr)
            continue
        if isinstance(data, dict) and "release" in data:
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


def _code_table(findings: list[dict]) -> list[str]:
    lines = ["| Severity | Rule | Location | Seen in |", "|---|---|---|---|"]
    for f in sorted(findings, key=lambda x: (x.get("severity", ""), x.get("id", ""))):
        where = ", ".join(sorted(set(f.get("sources") or [])))
        lines.append(
            f"| {f.get('severity', '?')} | `{f.get('id', '?')}` "
            f"| `{f.get('package', '?')}` | {where} |"
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


def render(reports: list[dict], scanned: list[str]) -> tuple[str, str, bool]:
    merged = merge(reports)
    fp = fingerprint(reports)
    total = sum(len(f) for kinds in merged.values() for f in kinds.values())

    body = [
        f"<!-- {MARKER}:{fp} -->",
        "",
        "Automated re-scan of the releases we still support. Raised by "
        "`.github/workflows/release-rescan.yml`; the policy it enforces is "
        "[docs/cve-policy.md](../blob/main/docs/cve-policy.md).",
        "",
        "**Why this can appear on a release that was clean when it shipped:** the "
        "release gate uses `ignore-unfixed`, so a vulnerability only becomes "
        "actionable once upstream publishes a fix. Everything below has a fix "
        "available *now* that did not exist, or was not yet taken, when the "
        "release was cut.",
        "",
        "**This report covers dependency findings only.** Code findings from the "
        "source scan go to code scanning, which is visible to write-access users "
        "rather than the world — a static-analysis hit in Terrapod's own source is "
        "a potential undisclosed vulnerability, and this issue is public.",
        "",
        f"Scanned: {', '.join(scanned) if scanned else '(none)'}",
        "",
    ]

    if not total:
        body += [
            "## No findings",
            "",
            "Every supported release is clean against current advisory data and "
            "current rules, with the accepted-risk register applied.",
        ]
        return "\n".join(body), fp, False

    for release in sorted(merged, reverse=True):
        kinds = merged[release]
        count = sum(len(v) for v in kinds.values())
        body += [f"## {release} — {count} finding(s)", ""]
        if kinds.get("dependency"):
            body += ["**Dependencies and packages**", ""]
            body += _dependency_table(kinds["dependency"])
            body += [""]
        if kinds.get("code"):
            body += ["**Code**", ""]
            body += _code_table(kinds["code"])
            body += [""]

    body += [
        "## What to do with this",
        "",
        "Each release above needs a judgement call, not an automatic patch. Worth "
        "checking in this order:",
        "",
        "1. **Is it already fixed on `main`?** If so the fix needs backporting to "
        "that release line, which is what the patch release carries.",
        "2. **Is it reachable in the way Terrapod uses the component?** If not, it "
        "belongs in the accepted-risk register with its reasoning and exit "
        "condition — not silently ignored, and not patched for appearance.",
        "3. **Does it warrant a patch release on its own**, or can it wait for the "
        "next scheduled one? Cadence is roughly weekly; an unreachable medium-"
        "impact finding can reasonably wait, a reachable critical one cannot.",
        "",
        "This issue is refreshed in place while the finding set is unchanged, so "
        "it will not re-notify on every run.",
    ]
    return "\n".join(body), fp, True


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: rescan_report.py <findings-dir> <out.md> [tag ...]", file=sys.stderr)
        return 2
    directory, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    scanned = sys.argv[3:]
    reports = load(directory)
    body, fp, has_findings = render(reports, scanned)
    out.write_text(body)
    print(f"fingerprint={fp}")
    print(f"has_findings={'true' if has_findings else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
