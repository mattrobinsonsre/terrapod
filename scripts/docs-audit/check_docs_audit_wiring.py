#!/usr/bin/env python3
"""Every gate in the repository is actually run by CI.

Two families, because the defect is identical in both and the second arrived
later: `scripts/docs-audit/check_*.py`, and the shell gates `scripts/*-gate.sh`.
Nothing auto-discovers either -- each one is an explicit step in `ci.yml` -- so
writing a gate and forgetting the step produces a file that looks like
enforcement and enforces nothing. That
is the same shape as a configuration flag no code reads, with the gate itself as
the unread thing, and it had already happened: `check_issue_refs.py` sat in this
directory unwired from the day it was written.

Finding the instance is not the fix. The question is what was meant to catch it,
and the answer was nothing, so this is that. It checks itself along with the
rest, which is what makes the set closed rather than merely one longer.

It asks whether SOME job in the workflow runs each gate, rather than naming
the job, because which job that is is a property of how the waves are carved and
that moves: these gates lived in a `docs-audit` job of their own until it was
folded into a combined lint job, at which point a check keyed on the job name
failed while every gate was still running. The invariant worth holding is "this
gate runs in CI", and the job it runs in is not part of it.

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

#: A matcher that stops matching must fail rather than pass on nothing. One
#: floor PER FAMILY: a single combined floor would let a broken glob take its
#: own family to zero while the other family carried the count.
MIN_GATES = 8
MIN_SHELL_GATES = 2


def main() -> int:
    workflow = ROOT / WORKFLOW
    if not workflow.is_file():
        print(f"FAIL — {WORKFLOW} does not exist")
        return 1

    parsed = yaml.safe_load(workflow.read_text())
    jobs = parsed.get("jobs") or {}
    if not jobs:
        print(f"FAIL — {WORKFLOW} declares no jobs; these gates run nowhere")
        return 1

    # Every job's steps' own `run:` text, so a step that merely NAMES a script
    # in its `name:` does not count as running it. `str()` rather than the
    # value, because YAML types a bare `run: true` as a boolean and joining that
    # raises a TypeError -- a gate that dies on an ordinary step reports CI as
    # unwell rather than reporting its own property, which is the failure it
    # exists to avoid being mistaken for.
    runs_in: dict[str, str] = {}
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        runs_in[name] = " ".join(
            str(step.get("run") or "") for step in job.get("steps") or [] if isinstance(step, dict)
        )

    gates = sorted(p for p in HERE.glob("check_*.py"))
    # The shell gates, which live beside this directory rather than in it.
    # Matched on the `-gate.sh` suffix the existing ones share, so a new one is
    # in scope by its name alone and needs no entry anywhere.
    shell_gates = sorted(p for p in (ROOT / "scripts").glob("*-gate.sh"))
    problems: list[str] = []

    for gate in gates + shell_gates:
        rel = gate.relative_to(ROOT)
        where = sorted(job for job, commands in runs_in.items() if str(rel) in commands)
        if not where:
            problems.append(
                f"{rel} is run by no job in {WORKFLOW}; add an explicit step to "
                "one, because nothing discovers these"
            )

    if len(shell_gates) < MIN_SHELL_GATES:
        problems.append(
            f"only found {len(shell_gates)} shell gate(s) matching scripts/*-gate.sh; "
            f"expected at least {MIN_SHELL_GATES}, so that matcher has stopped matching"
        )

    if len(gates) < MIN_GATES:
        problems.append(
            f"only found {len(gates)} gate(s) in {HERE.relative_to(ROOT)}; expected at "
            f"least {MIN_GATES}, so this matcher has stopped matching"
        )

    if problems:
        print(f"FAIL — {len(problems)} problem(s) with the CI gate wiring:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"PASS — all {len(gates)} docs-audit gate(s) and {len(shell_gates)} shell "
        f"gate(s) are run by a job in {WORKFLOW}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
