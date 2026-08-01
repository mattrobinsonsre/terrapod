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
import sys


def _read(path: pathlib.Path):
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"note: {path} unreadable ({exc}); treating as no findings", file=sys.stderr)
        return None


def from_trivy(data) -> list[dict]:
    out = []
    for result in (data or {}).get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            out.append({
                "id": v.get("VulnerabilityID", "?"),
                "package": v.get("PkgName", "?"),
                "installed": v.get("InstalledVersion", "?"),
                "fixed": v.get("FixedVersion", "?"),
                "severity": v.get("Severity", "?"),
            })
    return out


def from_pip_audit(data) -> list[dict]:
    out = []
    for dep in (data or {}).get("dependencies") or []:
        for v in dep.get("vulns") or []:
            fixes = v.get("fix_versions") or []
            out.append({
                "id": v.get("id", "?"),
                "package": dep.get("name", "?"),
                "installed": dep.get("version", "?"),
                "fixed": ", ".join(fixes) if fixes else "none published",
                "severity": "HIGH",   # pip-audit does not grade; the gate treats all as actionable
            })
    return out


def from_npm_audit(data, allowlist: set[str]) -> list[dict]:
    out = []
    for name, v in ((data or {}).get("vulnerabilities") or {}).items():
        if v.get("severity") not in ("high", "critical"):
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
                "fixed": "see advisory",
                "severity": v.get("severity", "?").upper(),
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


def main() -> int:
    if len(sys.argv) < 6:
        print(__doc__, file=sys.stderr)
        return 2
    kind, src, release, label, out = sys.argv[1:6]
    data = _read(pathlib.Path(src))

    if kind == "trivy":
        findings, report_kind = from_trivy(data), "dependency"
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
    else:
        print(f"unknown scanner {kind!r}", file=sys.stderr)
        return 2

    pathlib.Path(out).write_text(json.dumps({
        "release": release,
        "source": label,
        "kind": report_kind,
        "findings": findings,
    }, indent=2))
    print(f"{label}: {len(findings)} finding(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
