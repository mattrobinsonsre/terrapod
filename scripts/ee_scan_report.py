#!/usr/bin/env python3
"""Report fixable findings in the published execution-environment images (#2154).

Its own issue family, deliberately. The supported-releases issue is titled about
releases and an EE is not one -- conflating them would make that title stop being
true, and would mix "a release we ship has a finding" with "an image a configure
runs on has one", which have different owners and different fixes.

**Every finding here is fixable by construction**, because the scan runs with
`ignore-unfixed: true`. That is what makes the report actionable rather than
informative: a rebuild picks up current Debian packages through the
`apt-get upgrade` in `Dockerfile.ee`, whether or not the base tag's digest has
moved. An EE on an EOL ansible-core line carries unfixable CVEs for ever -- every
route to a Python 2 target is an EOL core -- and those are excluded before they
reach here, because reporting them would be noise nobody can act on.

**It reports; it does not act.** `release-rescan.yml` says that of itself and the
same reasoning holds: a scanner that started publishing images on its own would
be the opposite of a maintainer deciding. So the body carries the exact dispatch
that clears each line, and a maintainer runs it.

That matters because the two triggers genuinely do not meet. The build plan
rebuilds when PyPI or the base DIGEST moves; a Debian fix that upstream has not
yet rebuilt into `python:3.X-slim` moves neither, so the plan reports "current"
while a rebuild would clear the finding. `--force` exists for exactly that case.

    ee_scan_report.py <findings-dir> <out.md>
"""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import sys

#: Distinct from the release re-scan's marker, so the two issues are found and
#: refreshed independently rather than one overwriting the other.
MARKER = "terrapod-ee-scan"


def load(directory: pathlib.Path) -> list[dict]:
    """Dependency findings for EE images. Anything else is refused, not skipped.

    Refused rather than ignored because a code finding reaching this path would
    mean the scan was pointed at something other than an image, and silently
    dropping it would hide that.
    """
    reports = []
    for path in sorted(directory.rglob("*.json")):
        data = json.loads(path.read_text())
        if data.get("kind") != "dependency":
            raise SystemExit(
                f"{path} is a {data.get('kind')!r} report; this reporter handles image "
                "findings only, so something is pointed at the wrong target."
            )
        reports.append(data)
    return reports


def by_line(reports: list[dict]) -> dict[str, set[str]]:
    """Advisory ids per ansible-core line, merged across architectures.

    Merged because a finding present on both arches is one thing to fix, not
    two -- and listing it twice would make the report read as worse than it is.
    """
    lines: dict[str, set[str]] = collections.defaultdict(set)
    for report in reports:
        line = str(report.get("release", "")).removeprefix("ee-")
        for finding in report.get("findings", []):
            if finding.get("id"):
                lines[line].add(finding["id"])
    return {line: ids for line, ids in lines.items() if ids}


def fingerprint(lines: dict[str, set[str]]) -> str:
    """Stable over the finding SET, so an unchanged set does not re-notify.

    The body is refreshed every run regardless: whether to notify and what to
    display are two different decisions, and conflating them leaves an issue
    showing stale information after the tooling has improved.
    """
    material = ";".join(f"{line}:{','.join(sorted(ids))}" for line, ids in sorted(lines.items()))
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def render(lines: dict[str, set[str]], digest: str) -> str:
    """The body, with the fingerprint embedded in the marker.

    Embedded rather than kept alongside: the workflow decides whether to
    re-notify by grepping the EXISTING body for `MARKER:<fingerprint>`, so a
    marker without one can never match and the issue would comment on every
    single run. Pinned by a test, because the failure is noise rather than an
    error and nobody would chase it.
    """
    out = [
        "The published Ansible execution-environment images carry findings that a "
        "**rebuild would fix**.",
        "",
        "Every advisory below has a fix available — the scan runs with "
        "`ignore-unfixed: true`, so anything unfixable is excluded before it reaches "
        "here. An EE pinned to an EOL ansible-core line carries unfixable CVEs "
        "permanently, which is inherent to supporting EL6 and EL7 rather than a "
        "defect, and those belong in the accepted-risk register.",
        "",
        "| line | advisories |",
        "|---|---|",
    ]
    for line, ids in sorted(lines.items()):
        shown = ", ".join(f"`{i}`" for i in sorted(ids)[:6])
        if len(ids) > 6:
            shown += f" (+{len(ids) - 6} more)"
        out += [f"| **{line}** | {shown} |"]
    out += [
        "",
        "## Clearing them",
        "",
        "A rebuild picks up current Debian packages through the `apt-get upgrade` in "
        "`docker/Dockerfile.ee`, **whether or not the base tag's digest has moved** — "
        "the official python images compile CPython into `/usr/local`, so it is not "
        "dpkg-owned, while the libraries around it are.",
        "",
        "The scheduled build will NOT pick these up on its own: it rebuilds when PyPI "
        "or the base digest moves, and a Debian fix that upstream has not yet rebuilt "
        "into `python:3.X-slim` moves neither. So dispatch it, per line:",
        "",
        "```",
    ]
    for line in sorted(lines):
        out += [f"gh workflow run ee-build.yml -f line={line} -f force=true"]
    out += [
        "```",
        "",
        "If a rebuild does **not** clear a finding, it is not fixable by us — route it "
        "to `pentest/trivy/.trivyignore` with a stated exit condition, which is the "
        "same flow a platform-image finding takes.",
        "",
        f"<!-- {MARKER}:{digest} -->",
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    directory, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    lines = by_line(load(directory)) if directory.is_dir() else {}

    digest = fingerprint(lines)
    out.write_text(render(lines, digest) if lines else "")
    # Printed as job outputs so the workflow can decide without re-parsing. An
    # empty set means "do not raise or refresh" -- never "close the issue":
    # someone may be mid-rebuild on it, and a list that empties because a scan
    # leg was skipped must not read as done.
    print(f"has_findings={'true' if lines else 'false'}")
    print(f"fingerprint={digest}")
    print(f"lines={','.join(sorted(lines))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
