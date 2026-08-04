#!/usr/bin/env python3
"""Review the accepted-risk register against current advisory data (#1255).

Every entry in the register exists because there was no fix to take. Re-running
each scanner with the **register disabled** shows what we would be told if we
stopped suppressing — no advisory API and no version comparison of our own,
because the scanners already know.

**The scan must include unfixed findings**, and getting that wrong was #1257.
The release gate runs `ignore-unfixed`, so carrying that setting over here
looks natural and is exactly backwards: an entry is in the register BECAUSE it
is unfixed, so a fix-only scan renders every legitimate entry invisible and the
review reports it as matching nothing — telling you to delete a live
suppression. Scanning everything yields both signals from one pass instead.

That matters because re-reading an entry by hand checks the wrong thing. An
entry's stated exit condition is a *prediction* about how upstream will fix it,
and GHSA-mh99-v99m-4gvg sat accepted for months behind a prediction that the
fix required a major bump we could not take. Upstream backported to a patch
release instead. The entry was re-tested against its exit and correctly stayed;
the exit was simply the wrong thing to watch.

Two classifications, and they call for opposite actions:

``fixable``
    Still present, and a fix is now available. Bump, and delete the entry in
    the same pull request — rule 1 of the register.
``unmatched``
    Present in the register, reported by nothing. Stale: rule 4 says it is a
    liability rather than a leftover, because it silently shadows the
    regression that would bring the finding back.

**Unlike its sibling, this report carries full detail, and that is deliberate.**
`rescan_report.py` withholds ids and versions because naming a supported
release alongside what is wrong with it is a map for anyone deciding where to
look. Nothing here is new information: the register files are committed, and
their headers say the reasoning is "written to be read by someone outside the
project". Every id below is already public in this repository, so listing it
discloses nothing an attacker could not read from `main` — and the detail is
the entire value, since the action is "bump X to Y".

    register_review.py <scan-dir> <out.md>

Writes the report to <out.md> and prints `has_findings` / `fingerprint` for
the workflow step to consume.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys

MARKER = "terrapod-register-review"
_REPO = os.environ.get("GITHUB_REPOSITORY", "mattrobinsonsre/terrapod")

#: Where each register lives, and the scan whose output covers it. Keeping the
#: mapping here rather than in the workflow means a new register file is one
#: line, and the pairing can be exercised without a scheduled run.
REGISTERS = {
    "trivy": "pentest/trivy/.trivyignore",
    "pip-audit": "pentest/pip-audit-ignore.txt",
    "npm-audit": "pentest/npm-audit-allow.txt",
}


def read_register(path: pathlib.Path) -> list[str]:
    """Ids from a register file.

    All three use the same convention — one id per line, `#` starts a comment —
    which is what lets the CI gate, the re-scan and this share the same files.
    A missing file is empty rather than fatal: a register we have emptied is a
    success, not a reason to fail the review.
    """
    if not path.exists():
        return []
    ids = []
    for line in path.read_text().splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            ids.append(entry)
    return ids


def _load(path: pathlib.Path):
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else None
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"note: {path} unreadable ({exc}); treating as no findings", file=sys.stderr
        )
        return None


def from_trivy(data) -> dict[str, dict]:
    """{id: {package, installed, fixed}} from a Trivy JSON report.

    Produced WITHOUT the ignore file and WITHOUT `ignore-unfixed`, so presence
    means "still there" and `FixedVersion` alone decides whether anything can be
    done about it — the same shape pip-audit has always had. See the module
    docstring for why fix-only scanning here is a bug rather than an
    optimisation (#1257).
    """
    found: dict[str, dict] = {}
    for result in (data or {}).get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            vid = v.get("VulnerabilityID")
            if not vid:
                continue
            found.setdefault(
                vid,
                {
                    "package": v.get("PkgName", "?"),
                    "installed": v.get("InstalledVersion", "?"),
                    "fixed": v.get("FixedVersion", ""),
                },
            )
    return found


def from_pip_audit(data) -> dict[str, dict]:
    """{id: {...}} from pip-audit JSON, run without --ignore-vuln.

    pip-audit reports unfixed advisories too, so unlike Trivy the fix has to be
    read off the entry: `fix_versions` empty means still unfixed, which is the
    entry doing its job rather than something to act on.
    """
    found: dict[str, dict] = {}
    for dep in (data or {}).get("dependencies") or []:
        for v in dep.get("vulns") or []:
            vid = v.get("id")
            if not vid:
                continue
            fixes = v.get("fix_versions") or []
            entry = {
                "package": dep.get("name", "?"),
                "installed": dep.get("version", "?"),
                "fixed": ", ".join(fixes),
            }
            # Index by the aliases too. The same advisory has a PYSEC id and a
            # CVE, and which one lands in the register depends on where the
            # person adding it was reading. Matching on the primary id alone
            # would report a live suppression as matching nothing — and this
            # report tells you to DELETE those, so the failure is not cosmetic.
            for key in [vid, *(v.get("aliases") or [])]:
                found.setdefault(key, entry)
    return found


def from_npm_audit(data) -> dict[str, dict]:
    """{id: {...}} from `npm audit --json`, with no allowlist applied.

    npm keys by package rather than advisory, so the ids come out of `via` —
    the same path the CI gate walks. `fixAvailable` is a bool or an object
    describing the upgrade; both mean a fix exists, and the object's version is
    worth carrying through.
    """
    found: dict[str, dict] = {}
    for name, v in ((data or {}).get("vulnerabilities") or {}).items():
        fix = v.get("fixAvailable")
        if isinstance(fix, dict):
            fixed = f"{fix.get('name', name)}@{fix.get('version', '?')}"
        else:
            fixed = "available" if fix else ""
        for via in v.get("via") or []:
            if not isinstance(via, dict) or not via.get("url"):
                continue
            found.setdefault(
                via["url"].rstrip("/").split("/")[-1],
                {
                    "package": name,
                    "installed": v.get("range", "?"),
                    "fixed": fixed,
                },
            )
    return found


_PARSERS = {
    "trivy": from_trivy,
    "pip-audit": from_pip_audit,
    "npm-audit": from_npm_audit,
}


def collect(scan_dir: pathlib.Path) -> tuple[dict[str, dict[str, dict]], set[str]]:
    """Merge every scan output in the directory, keyed by scanner.

    Files are named `<scanner>[-anything].json` so one scanner can contribute
    several (Trivy runs per image and per architecture). An unparseable file
    contributes nothing rather than sinking the run — but see `missing` below:
    a scanner that produced no file at all is reported, because "nothing to do"
    and "nobody looked" must not render identically.
    """
    merged: dict[str, dict[str, dict]] = {s: {} for s in _PARSERS}
    seen: set[str] = set()
    for path in sorted(scan_dir.rglob("*.json")):
        scanner = next((s for s in _PARSERS if path.name.startswith(s)), None)
        if scanner is None:
            continue
        seen.add(scanner)
        merged[scanner].update(_PARSERS[scanner](_load(path)))
    return merged, seen


def classify(registers: dict[str, list[str]], found: dict[str, dict[str, dict]]):
    fixable, unmatched, holding = [], [], []
    for scanner, ids in registers.items():
        for vid in ids:
            hit = found.get(scanner, {}).get(vid)
            row = {"scanner": scanner, "id": vid, **(hit or {})}
            if hit is None:
                unmatched.append(row)
            elif hit.get("fixed"):
                fixable.append(row)
            else:
                holding.append(row)
    return fixable, unmatched, holding


def _table(rows: list[dict], with_fix: bool) -> list[str]:
    head = "| Advisory | Register | Package | Installed |"
    sep = "|---|---|---|---|"
    if with_fix:
        head += " Fixed in |"
        sep += "---|"
    out = [head, sep]
    for r in rows:
        line = (
            f"| `{r['id']}` | {REGISTERS.get(r['scanner'], r['scanner'])} "
            f"| {r.get('package', '?')} | {r.get('installed', '?')} |"
        )
        if with_fix:
            line += f" **{r.get('fixed', '?')}** |"
        out.append(line)
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    scan_dir, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

    registers = {s: read_register(pathlib.Path(p)) for s, p in REGISTERS.items()}
    found, scanned = collect(scan_dir)
    fixable, unmatched, holding = classify(registers, found)

    # A scanner whose output never arrived cannot distinguish "this entry
    # matches nothing" from "nothing looked for it", so its entries are not
    # reported as stale — and the gap is named rather than swallowed.
    skipped = [s for s, ids in registers.items() if ids and s not in scanned]
    if skipped:
        unmatched = [r for r in unmatched if r["scanner"] not in skipped]

    fp = hashlib.sha256(
        json.dumps(
            sorted(
                (r["scanner"], r["id"], kind)
                for kind, rows in (("fixable", fixable), ("unmatched", unmatched))
                for r in rows
            ),
        ).encode()
    ).hexdigest()[:16]

    body = [
        "Automated review of the accepted-risk register "
        f"([policy](https://github.com/{_REPO}/blob/main/docs/cve-policy.md)).",
        "",
        (
            "Each entry was accepted because no fix existed. This re-runs every "
            "scanner with the register **disabled**, so anything listed below is "
            "either an accepted risk that upstream has since fixed, or an entry "
            "that no longer matches anything at all."
        ),
        "",
    ]

    if fixable:
        body += [
            "## Fixable now — take the fix",
            "",
            (
                "A patched version exists. Rule 1 of the register: bump to it, and "
                "delete the entry in the **same** pull request rather than leaving "
                "it behind."
            ),
            "",
            *_table(fixable, with_fix=True),
            "",
            (
                "Check the advisory's own affected/patched ranges rather than the "
                "entry's written exit condition — the exit is a guess about how "
                "upstream would fix it, and a backport to an older line is exactly "
                "the case that guess misses."
            ),
            "",
        ]

    if unmatched:
        body += [
            "## Matching nothing — delete",
            "",
            (
                "Reported by no scanner, including as an unfixed finding. Rule 4: a "
                "stale entry is a liability rather than a leftover, because it "
                "silently shadows the regression that would bring the finding back."
            ),
            "",
            "| Advisory | Register |",
            "|---|---|",
            *[
                f"| `{r['id']}` | {REGISTERS.get(r['scanner'], r['scanner'])} |"
                for r in unmatched
            ],
            "",
        ]

    if holding:
        body += [
            f"<details><summary>{len(holding)} still doing their job (no fix upstream)"
            "</summary>",
            "",
            *_table(holding, with_fix=False),
            "",
            "</details>",
            "",
        ]

    if skipped:
        body += [
            "> **Incomplete run.** No output from: "
            + ", ".join(f"`{s}`" for s in sorted(skipped))
            + ". Their entries are not reported as stale above, because a scanner that "
            "did not run looks identical to one that found nothing.",
            "",
        ]

    body += [f"<!-- {MARKER}:{fp} -->"]
    out.write_text("\n".join(body) + "\n")

    print(f"fingerprint={fp}")
    print(f"has_findings={'true' if (fixable or unmatched) else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
