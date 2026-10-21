#!/usr/bin/env python3
"""Normalise one scanner's output into the re-scan report shape (#1212).

Four scanners with four unrelated JSON schemas feed one report, so each is
flattened here rather than in the workflow. Keeping it in a script means the
shapes can be exercised without pushing a workflow change and waiting for a
scheduled run.

    rescan_normalise.py <trivy|pip-audit|npm-audit|semgrep> \
        <input.json> <release> <source-label> <output.json> [allowlist]

Every scanner is run in a mode that already applies our accepted-risk register
(Trivy reads .trivyignore, pip-audit takes --ignore-vuln), except npm audit,
which has no native ignore mechanism — its allowlist is applied here from the
same file the release gate uses.

A missing or unparseable input yields an empty finding list rather than an
error. A scanner that could not run must not be indistinguishable from a
scanner that ran and found nothing, so the workflow reports skips separately;
what this must not do is fail the whole aggregation because one leg was absent.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys


def _read(path: pathlib.Path):
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"note: {path} unreadable ({exc}); treating as no findings", file=sys.stderr)
        return None


# GitHub advisories accept a fixed set of ecosystems; anything we cannot map
# confidently is "other", which is accurate rather than a guess.
_TRIVY_ECOSYSTEM = {
    "gobinary": "go",
    "gomod": "go",
    "node-pkg": "npm",
    "npm": "npm",
    "yarn": "npm",
    "python-pkg": "pip",
    "pip": "pip",
    "poetry": "pip",
}


#: The one package an EE image pins to its own ansible-core line, per
#: `docker/Dockerfile.ee`: `pip install "ansible-core==${ANSIBLE_CORE_VERSION}"`.
#: Everything else that image installs (`pip`, `wheel`) is unpinned and upgraded,
#: so a rebuild takes the newest. A name rather than a derivation because the
#: pinning is a fact about the Dockerfile, and
#: `test_ee_scan_reachability.py::test_the_dockerfile_still_pins_only_this`
#: fails if that stops being true.
LINE_PINNED = "ansible-core"

#: A stable release: digits and dots only. Anything carrying a letter is a
#: pre-release or a local/dev version, and the deployment-wide pre-release policy
#: refuses those by default -- so offering one as "the fix" would recommend
#: something the platform would not install. Deliberately a shape test rather
#: than a list of suffixes, which would have to enumerate every spelling PEP 440
#: permits.
_STABLE = re.compile(r"^\d+(?:\.\d+)*$")


def _fix_versions(fixed: str) -> list[str]:
    """Trivy reports `FixedVersion` as one comma-separated string."""
    return [part.strip() for part in (fixed or "").split(",") if part.strip()]


def reachable(finding: dict, release: str) -> tuple[bool, str]:
    """Can a REBUILD of this image install the version Trivy names as the fix?

    `ignore-unfixed: true` is read as making every reported finding actionable,
    and that inference is only sound where the fix is obtainable. It holds for
    Debian packages, since `apt-get upgrade` stays inside the distribution. It
    fails in two ways that a scan of these images hits immediately, and the
    first EE scan reported five findings of which a rebuild would have cleared
    **none** (#2201):

      * **A vendored copy.** `FixedVersion` names the standalone distribution,
        but the finding is about a copy inside another one -- `setuptools`
        carries its own `jaraco.context` and `wheel`, and `pip` ships a
        `bom.cdx.json` declaring `certifi`, `msgpack`, `setuptools` and
        `urllib3`. Installing the named version changes none of them; only a
        new release of the vendoring package does. Measured: the image's real
        `wheel` was already 0.48.0 while the finding reported 0.45.1, which is
        setuptools' copy.
      * **A package pinned to this image's line.** The image *is* the line, so a
        fix published only in a later one is unreachable by construction -- and
        taking it would destroy the reason that line is built, since the older
        lines exist precisely to reach a Python 2.6/2.7 target. "Fix 2.12 by
        installing 2.17" is not a fix, it is a different image.

    Returns `(True, "")` when a rebuild could clear it, else `(False, reason)`.
    The reason is recorded on the finding rather than used for reporting, so a
    maintainer reading the raw artifact can see what was judged and why.
    """
    path = finding.get("path") or ""
    if "/_vendor/" in path:
        return False, f"vendored inside another distribution ({path})"
    if not path and finding.get("from_sbom"):
        # No installed distribution to upgrade: Trivy found this in an SBOM
        # embedded in the image, which for these images is pip's own
        # `_vendor/bom.cdx.json`. It carries a `BOMRef` and no `PkgPath`.
        return False, "declared by a vendored SBOM, not an installed distribution"
    if finding.get("package") == LINE_PINNED:
        line = release.removeprefix("ee-")
        in_line = [v for v in _fix_versions(finding.get("fixed", "")) if v.startswith(f"{line}.")]
        stable = [v for v in in_line if _STABLE.match(v)]
        if not in_line:
            return False, f"fixed only outside the {line} line, which this image is pinned to"
        if not stable:
            return False, f"fixed in the {line} line only by a pre-release ({', '.join(in_line)})"
    return True, ""


def from_trivy(data, release: str = "") -> list[dict]:
    out = []
    for result in (data or {}).get("Results") or []:
        ecosystem = _TRIVY_ECOSYSTEM.get(result.get("Type", ""), "other")
        for v in result.get("Vulnerabilities") or []:
            finding = {
                "id": v.get("VulnerabilityID", "?"),
                "package": v.get("PkgName", "?"),
                "installed": v.get("InstalledVersion", "?"),
                "fixed": v.get("FixedVersion", "?"),
                "severity": v.get("Severity", "?"),
                "ecosystem": ecosystem,
                # Carried so reachability is derived from Trivy's own output
                # rather than from a list of package names that would rot.
                "path": v.get("PkgPath") or "",
                "from_sbom": bool((v.get("PkgIdentifier") or {}).get("BOMRef")),
            }
            ok, why = reachable(finding, release)
            finding["reachable"] = ok
            finding["unreachable"] = why
            out.append(finding)
    return out


def from_pip_audit(data) -> list[dict]:
    out = []
    for dep in (data or {}).get("dependencies") or []:
        for v in dep.get("vulns") or []:
            fixes = v.get("fix_versions") or []
            if not fixes:
                # Same ignore-unfixed policy as the other two legs: nothing to
                # bump to means nothing a patch release can do.
                continue
            out.append({
                "id": v.get("id", "?"),
                "package": dep.get("name", "?"),
                "installed": dep.get("version", "?"),
                "fixed": ", ".join(fixes),
                "severity": "HIGH",   # pip-audit does not grade; the gate treats all as actionable
                "ecosystem": "pip",
            })
    return out


def _npm_fix(v) -> str | None:
    """The upgrade that resolves this advisory, or None when there isn't one.

    `fixAvailable` is `false` when nothing upstream fixes it yet, an object
    naming the upgrade when there is one, and `true` when npm can resolve it
    within the existing ranges.
    """
    fix = v.get("fixAvailable")
    if fix is False or fix is None:
        return None
    if isinstance(fix, dict):
        target = f"{fix.get('name', '?')}@{fix.get('version', '?')}"
        return f"{target} (major)" if fix.get("isSemVerMajor") else target
    return "npm audit fix"


def from_npm_audit(data, allowlist: set[str]) -> list[dict]:
    out = []
    for name, v in ((data or {}).get("vulnerabilities") or {}).items():
        if v.get("severity") not in ("high", "critical"):
            continue
        fixed = _npm_fix(v)
        if fixed is None:
            # Match the artifact leg's ignore-unfixed policy. An advisory with
            # no available fix cannot be actioned by a patch release, and
            # listing it only pads the report with rows nobody can close.
            continue
        ids = [
            x["url"].rstrip("/").split("/")[-1]
            for x in (v.get("via") or [])
            if isinstance(x, dict) and x.get("url")
        ]
        for advisory in ids:
            if advisory in allowlist:
                continue
            out.append({
                "id": advisory,
                "package": name,
                "installed": (v.get("range") or "?"),
                "fixed": fixed,
                "severity": v.get("severity", "?").upper(),
                "ecosystem": "npm",
            })
    return out


def from_semgrep(data) -> list[dict]:
    out = []
    for r in (data or {}).get("results") or []:
        extra = r.get("extra") or {}
        if (extra.get("severity") or "").upper() != "ERROR":
            continue        # WARNING/INFO are advisory; only ERROR is actionable here
        start = (r.get("start") or {}).get("line", "?")
        path = r.get("path", "?")
        # Paths are absolute inside the scanner container; the repo-relative
        # form is what a reader needs.
        path = path.split("/src/", 1)[-1]
        out.append({
            "id": r.get("check_id", "?"),
            "package": f"{path}:{start}",
            "installed": "-",
            "fixed": "-",
            "severity": "ERROR",
        })
    return out


def count_semgrep_sarif(data) -> int:
    """How many error-level results, and nothing else.

    A count is not a disclosure — the detail is. This exists so the public
    report can say "3 code findings on v1.1.1, see code scanning" instead of
    staying silent about them, which would leave a maintainer reading the issue
    with no idea they existed.
    """
    total = 0
    for run in (data or {}).get("runs") or []:
        # Build ruleId -> level from the driver, because Semgrep puts severity
        # on the rule definition and usually omits it on the result. Reading
        # result["level"] alone counted 0 while Semgrep reported 8.
        levels = {}
        for rule in ((run.get("tool") or {}).get("driver") or {}).get("rules") or []:
            lvl = ((rule.get("defaultConfiguration") or {}).get("level") or "").lower()
            if rule.get("id"):
                levels[rule["id"]] = lvl
        for r in run.get("results") or []:
            level = (r.get("level") or levels.get(r.get("ruleId", ""), "") or "").lower()
            if level == "error":
                total += 1
    return total


def main() -> int:
    if len(sys.argv) < 6:
        print(__doc__, file=sys.stderr)
        return 2
    kind, src, release, label, out = sys.argv[1:6]
    data = _read(pathlib.Path(src))

    if kind == "trivy":
        findings, report_kind = from_trivy(data, release), "dependency"
    elif kind == "pip-audit":
        findings, report_kind = from_pip_audit(data), "dependency"
    elif kind == "npm-audit":
        allow: set[str] = set()
        if len(sys.argv) > 6:
            for line in pathlib.Path(sys.argv[6]).read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    allow.add(line)
        findings, report_kind = from_npm_audit(data, allow), "dependency"
    elif kind == "semgrep":
        findings, report_kind = from_semgrep(data), "code"
    elif kind == "semgrep-count":
        # Count only. Deliberately does not go through the findings path.
        pathlib.Path(out).write_text(json.dumps({
            "release": release,
            "code_findings": count_semgrep_sarif(data),
        }, indent=2))
        print(f"{label}: {count_semgrep_sarif(data)} code finding(s) (count only)")
        return 0
    else:
        print(f"unknown scanner {kind!r}", file=sys.stderr)
        return 2

    pathlib.Path(out).write_text(json.dumps({
        "release": release,
        "source": label,
        "kind": report_kind,
        "findings": findings,
    }, indent=2))
    # Say how many a rebuild could actually clear, not just how many were found.
    # The raw count alone reads as the size of the problem, and on the EE images
    # it was five when the answer was none (#2201).
    clearable = sum(1 for f in findings if f.get("reachable", True))
    if clearable == len(findings):
        print(f"{label}: {len(findings)} finding(s)")
    else:
        print(f"{label}: {len(findings)} finding(s), {clearable} a rebuild can clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
