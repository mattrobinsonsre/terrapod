#!/usr/bin/env python3
"""`ci-pass` must actually judge every job it waits for.

`ci-pass` is the one required status check, and it decides the verdict from a
`results=()` array it builds by hand. A job listed in `needs` but absent from
that array is WAITED FOR AND THEN IGNORED: it can fail while CI reports green,
which is the worst shape a gate can take -- the signal is present, the verdict
is wrong, and nothing says so.

That is not hypothetical here. `helm-eval-boot` once ran on tag pushes, sat
only in `ci-pass`, and so could fail while a release published (#1446); the
remedy was wiring it into every site, and this is the half of that which can be
checked mechanically.

The converse is also checked: a `results` entry for a job that is not in
`needs` reads as covered and is evaluated before the job has run.

ONE DELIBERATE EXEMPTION, and it carries its reason in `ci.yml` itself:
`e2e-report` is excluded from BOTH by design -- an artifact-storage failure
with no Terrapod code involved failed a correct release on v1.5.3, and listing
it in `needs` made `ci-pass` wait 5m35s for a job whose result it ignored
(#1468). It is exempt here only because it is in neither, which is consistent.

Needs PyYAML, which the docs-audit job installs.
"""

from __future__ import annotations

import pathlib
import re
import sys

try:
    import yaml
except ModuleNotFoundError:
    sys.exit("FAIL: PyYAML is required; the docs-audit job installs it")

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

#: A floor, so a matcher that stops matching fails here rather than passing on
#: nothing. `ci-pass` waits on dozens of jobs; a handful means the parse broke.
MIN_NEEDS = 20


def main() -> int:
    if not CI.exists():
        sys.exit(f"FAIL: {CI.relative_to(ROOT)} is missing; this gate reads it")
    workflow = yaml.safe_load(CI.read_text())
    jobs = workflow.get("jobs", {})
    if "ci-pass" not in jobs:
        sys.exit("FAIL: ci.yml has no `ci-pass` job; it is the required status check")

    cp = jobs["ci-pass"]
    needs = list(cp.get("needs") or [])
    run = "\n".join(str(s.get("run", "")) for s in cp.get("steps", []))
    judged = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.result", run))

    problems: list[str] = []
    if len(needs) < MIN_NEEDS:
        problems.append(
            f"ci-pass lists only {len(needs)} job(s) in `needs`, below the floor "
            f"of {MIN_NEEDS}. The parse has broken; fix it rather than lowering "
            f"the floor."
        )

    for job in needs:
        if job not in judged:
            problems.append(
                f"`{job}` is in ci-pass.needs but never read in results=() -- "
                f"ci-pass WAITS for it and then ignores its result, so it can "
                f"fail while CI reports green"
            )

    for job in sorted(judged):
        if job not in needs:
            problems.append(
                f"results=() reads `{job}`, which is not in ci-pass.needs -- its "
                f"result is evaluated before the job has necessarily run"
            )

    # A job that exists, runs on pull requests, and is in neither. Not
    # automatically wrong -- e2e-report is deliberately in neither -- so this
    # reports rather than fails, because deciding requires reading the job.
    notes = [name for name in jobs if name not in ("ci-pass", *needs) and name not in judged]

    if problems:
        print(f"check_ci_pass_wiring: {len(problems)} problem(s)\n")
        for p in problems:
            print(f"  {p}")
        print(
            "\n  ci-pass is the only required check. Every job it waits for must\n"
            "  also be judged by it, or the verdict is silently wrong."
        )
        return 1

    print(f"check_ci_pass_wiring: OK -- ci-pass waits on {len(needs)} job(s) and judges every one")
    if notes:
        print(f"  (not wired into ci-pass, which may be deliberate: {', '.join(sorted(notes))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
