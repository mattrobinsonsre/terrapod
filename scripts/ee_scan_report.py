#!/usr/bin/env python3
"""Report fixable findings in the published execution-environment images (#2154).

Its own issue family, deliberately. The supported-releases issue is titled about
releases and an EE is not one -- conflating them would make that title stop being
true, and would mix "a release we ship has a finding" with "an image a configure
runs on has one", which have different owners and different fixes.

**Every finding here is one a rebuild can clear**, and that is now checked rather
than assumed. `ignore-unfixed: true` means Trivy reports only findings with a fix
published *somewhere*, which is not the same as a fix this image's build can
install -- and reading the one as the other is how the first EE scan raised five
findings of which a rebuild would have cleared none (#2201). The judgement is
`rescan_normalise.reachable`, derived from Trivy's own `PkgPath` and
`PkgIdentifier`: a copy vendored inside another distribution cannot be upgraded
by installing the standalone version, and `ansible-core` is pinned to this
image's own line, so a fix published only in a later line is unreachable by
construction. An EE on an EOL line therefore carries unfixable CVEs for ever --
every route to a Python 2 target is an EOL core -- and those are excluded before
they reach here, because reporting them would be noise nobody can act on.

**The issue is PUBLIC, so it carries no detail** — no advisory ids, no package
versions, not which ansible-core lines are affected. That is the same split
`rescan_report.py` states for releases, and for the same reason: naming an image
alongside what is wrong with it is a map for anyone deciding where to look, and
the people who need the detail are maintainers, who read it in the draft
advisory. The first version of this reporter listed CVE ids per line and had to
be withdrawn (#2168).

    draft security advisory   the findings, per line     private
    this issue                "there is something to do"  public

**It reports; it does not act.** `release-rescan.yml` says that of itself and the
same reasoning holds: a scanner that started publishing images on its own would
be the opposite of a maintainer deciding. So the body says what to run, without
saying what is wrong.

That matters because the two triggers genuinely do not meet. The build plan
rebuilds when PyPI or the base DIGEST moves; a Debian fix that upstream has not
yet rebuilt into `python:3.X-slim` moves neither, so the plan reports "current"
while a rebuild would clear the finding. `--force` exists for exactly that case.

    ee_scan_report.py <findings-dir> <out.md> [advisory-urls.json]
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
            # The exclusion this module's docstring has always claimed and never
            # performed (#2201). `reachable` is decided in `rescan_normalise`
            # from Trivy's own `PkgPath` and `PkgIdentifier`, so a finding whose
            # named fix this image's build cannot install never reaches the
            # issue. Defaults to True so a report produced before the field
            # existed, or by a leg that does not set it, is reported rather than
            # silently dropped -- an absent signal must not read as "nothing to
            # do" here of all places.
            if not finding.get("reachable", True):
                continue
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


def render(lines: dict[str, set[str]], digest: str, advisories: dict[str, str]) -> str:
    """The PUBLIC body: what to do, never what is wrong.

    Deliberately carries no advisory id, no package name and no line number. The
    counts are the most it says, because "how many" is not a map to anything
    while "which line carries which CVE" is.

    The fingerprint is embedded in the marker because the workflow decides
    whether to re-notify by grepping the EXISTING body for
    `MARKER:<fingerprint>` — a marker without one can never match and the issue
    would comment on every single run.
    """
    affected = len(lines)
    total = len({i for ids in lines.values() for i in ids})
    out = [
        (
            "The scheduled scan found findings in the published Ansible "
            "execution-environment images that **a rebuild would fix**."
        ),
        "",
        (
            f"**{total} advisory/advisories across {affected} ansible-core line(s).** "
            "The detail — which line, which package, which advisory — is in the "
            "**draft security advisory**, which is private to maintainers. It is "
            "deliberately not here: naming an image alongside what is wrong with it "
            "is a map for anyone deciding where to look."
        ),
        "",
    ]
    if advisories:
        out += ["Draft advisories:", ""]
        for name in sorted(advisories):
            out += [f"* [{name}]({advisories[name]})"]
        out += [""]
    else:
        out += [
            (
                "> No draft advisory URL was recorded for this run — check the "
                "`Record EE findings in draft advisories` step. The detail is not in "
                "this issue by design, so a missing advisory means the detail is "
                "nowhere and the step needs fixing."
            ),
            "",
        ]
    out += [
        "## Clearing them",
        "",
        (
            "A rebuild picks up current Debian packages through the `apt-get upgrade` "
            "in `docker/Dockerfile.ee`, **whether or not the base tag's digest has "
            "moved** — the official python images compile CPython into `/usr/local`, "
            "so it is not dpkg-owned, while the libraries around it are."
        ),
        "",
        (
            "The scheduled build will **not** pick these up on its own: it rebuilds "
            "when PyPI or the base digest moves, and a Debian fix that upstream has "
            "not yet rebuilt into `python:3.X-slim` moves neither. The advisory names "
            "the affected lines; dispatch per line:"
        ),
        "",
        "```",
        "gh workflow run ee-build.yml -f line=<LINE> -f force=true",
        "```",
        "",
        (
            "If a rebuild does **not** clear a finding it is not fixable by us — it "
            "goes to `pentest/trivy/.trivyignore` with a stated exit condition, and "
            "the EE images are in the weekly register review so the entry is "
            "re-tested rather than renewed by neglect."
        ),
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

    # Written by the advisory step, as rescan_report consumes its own. Absent is
    # tolerated and SAID SO in the body rather than silently omitted: with no
    # detail in the issue, a missing advisory means the detail is nowhere.
    advisories: dict[str, str] = {}
    if len(sys.argv) > 3:
        urls = pathlib.Path(sys.argv[3])
        if urls.is_file():
            advisories = json.loads(urls.read_text())

    digest = fingerprint(lines)
    out.write_text(render(lines, digest, advisories) if lines else "")
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
