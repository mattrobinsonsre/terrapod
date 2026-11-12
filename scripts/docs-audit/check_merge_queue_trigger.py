#!/usr/bin/env python3
"""The merge-queue trigger covers every branch the PR trigger does.

A merge queue blocks on the repository's REQUIRED checks. If `ci.yml` does not
fire on `merge_group` for a branch, the required check never registers on an
entry for that branch -- and the entry does not fail, it waits. Every merge to
that branch hangs, with nothing red to look at. That is the absent-signal shape
this repository keeps meeting, and it is worse here than usual because the
symptom is "merging is broken" rather than "a test failed".

The realistic way in is drift, not omission: somebody adds a release line to
`pull_request.branches` -- the trigger people think about -- and not to
`merge_group.branches`. So the assertion is PARITY between the two rather than
the presence of any particular name, which needs no list of its own and cannot
go stale when a line is added or retired.

Deliberately NOT asserted: that a merge queue is enabled on the repository.
That is a repository setting rather than a file, so a test could only read it
over the network, and a gate that needs credentials to decide is one that fails
for reasons unrelated to its subject. The parity above is what we can know from
the tree, and it is the half that drifts.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

CI = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def main() -> int:
    doc = yaml.safe_load(CI.read_text())
    # PyYAML resolves a bare `on:` key to the boolean True (the Norway problem's
    # cousin), so accept either spelling rather than assuming one.
    triggers = doc.get(True) or doc.get("on")
    if not isinstance(triggers, dict):
        print("FAIL: could not read the trigger block from ci.yml", file=sys.stderr)
        return 1

    problems: list[str] = []
    pr = triggers.get("pull_request")
    mg = triggers.get("merge_group")

    if not isinstance(pr, dict):
        problems.append("ci.yml has no `pull_request` trigger -- this gate is reading nothing")
    if mg is None:
        problems.append(
            "ci.yml has no `merge_group` trigger. With a merge queue enabled, the required "
            "check never registers on a queue entry, so merges WAIT rather than fail."
        )

    if isinstance(pr, dict) and isinstance(mg, dict):
        pr_b = set(pr.get("branches") or [])
        mg_b = set(mg.get("branches") or [])
        # A floor: if the PR trigger stops naming branches, comparing two empty
        # sets would pass while asserting nothing.
        if not pr_b:
            problems.append("`pull_request.branches` is empty -- the comparison below is vacuous")
        missing = pr_b - mg_b
        if missing:
            problems.append(
                "`merge_group.branches` is missing "
                + ", ".join(sorted(missing))
                + " -- a merge-queue entry on those branches would hang with no check"
            )
        extra = mg_b - pr_b
        if extra:
            problems.append(
                "`merge_group.branches` names "
                + ", ".join(sorted(extra))
                + " which the PR trigger does not -- one of the two is wrong"
            )

    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1

    print(f"OK — merge_group covers the {len(set(pr.get('branches') or []))} PR-trigger branches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
