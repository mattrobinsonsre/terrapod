#!/usr/bin/env python3
"""Every gate in this directory is actually run by CI.

Nothing auto-discovers `scripts/docs-audit/check_*.py` -- each one is an
explicit step in `ci.yml`'s `docs-audit` job -- so writing a gate and forgetting
the step produces a file that looks like enforcement and enforces nothing. That
is the same shape as a configuration flag no code reads, with the gate itself as
the unread thing, and it had already happened: `check_issue_refs.py` sat in this
directory unwired from the day it was written.

Finding the instance is not the fix. The question is what was meant to catch it,
and the answer was nothing, so this is that. It checks itself along with the
rest, which is what makes the set closed rather than merely one longer.

It deliberately says nothing about the exec bit. Every invocation of these
scripts names an interpreter (`python3 <path>`, `sh <path>`), so the bit is
load-bearing nowhere and asserting it would fail CI for a reason with no
consequence -- a gate whose every finding is a false positive, which is worse
than no gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORKFLOW = Path(".github/workflows/ci.yml")
JOB = "docs-audit"

#: A matcher that stops matching must fail rather than pass on nothing.
MIN_GATES = 8


def main() -> int:
    workflow = ROOT / WORKFLOW
    if not workflow.is_file():
        print(f"FAIL — {WORKFLOW} does not exist")
        return 1

    parsed = yaml.safe_load(workflow.read_text())
    job = (parsed.get("jobs") or {}).get(JOB)
    if job is None:
        print(f"FAIL — {WORKFLOW} has no `{JOB}` job; these gates run nowhere")
        return 1

    # The steps' own `run:` text, so a step that merely NAMES a script in its
    # `name:` does not count as running it. `str()` rather than the value,
    # because YAML types a bare `run: true` as a boolean and joining that raises
    # a TypeError -- a gate that dies on an ordinary step reports CI as unwell
    # rather than reporting its own property, which is the failure it exists to
    # avoid being mistaken for.
    commands = " ".join(str(step.get("run") or "") for step in job.get("steps") or [])

    gates = sorted(p for p in HERE.glob("check_*.py"))
    problems: list[str] = []

    for gate in gates:
        rel = gate.relative_to(ROOT)
        if str(rel) not in commands:
            problems.append(
                f"{rel} is not run by the `{JOB}` job; add an explicit step, "
                "because nothing discovers these"
            )

    if len(gates) < MIN_GATES:
        problems.append(
            f"only found {len(gates)} gate(s) in {HERE.relative_to(ROOT)}; expected at "
            f"least {MIN_GATES}, so this matcher has stopped matching"
        )

    if problems:
        print(f"FAIL — {len(problems)} problem(s) with the docs-audit wiring:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"PASS — all {len(gates)} docs-audit gate(s) are run by the `{JOB}` job.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
