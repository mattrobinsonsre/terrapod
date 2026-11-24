#!/usr/bin/env python3
"""Prove the CI path gating has not started skipping something that matters.

WHY THIS EXISTS

`scripts/ci-affected.py` decides which job families a pull request cannot
affect. Its input sets are derived from the tree, so they cannot lag the thing
they derive from -- but a derivation has a blind spot by construction, and the
failure is SILENT: a skipped job and a passing job are indistinguishable in the
checks list, so a gating mistake does not announce itself. Nothing in the
repository would notice if the gate quietly started skipping a job that a
change really did break.

WHAT IT CHECKS, AND WHY IT CAN ACTUALLY FAIL

Pushes to `main` run everything, deliberately -- that is the safety net the
gating leans on. So `main`'s own history is a record of which jobs a given
change really did break, graded without any gating applied. This replays that
record:

    for each recent push to main
        recompute what the gate WOULD have skipped for that merge's diff
        if any job that FAILED was in the would-skip set -> the gate is wrong

A hit is a real defect with a named job and a named merge, not a heuristic.

It is deliberately an audit rather than a test. The property is about the
repository's history, which no unit test can see, and the evidence only exists
once changes have landed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

# The families a job belongs to, read from the workflow rather than listed, so
# a newly gated job is audited without touching this file.
GATE_RE = re.compile(r"needs\.prepare\.outputs\.run_([a-z_]+)")


#: Transient shapes in `gh`'s own stderr, matched as POSITIVE evidence. An
#: unrecognised failure is final: retrying by default would make a 404 or an
#: auth error wait out the whole backoff before reporting, and a deny-list of
#: definitive shapes cannot stay complete against someone else's CLI.
#:
#: `gh: Server Error (HTTP 502)` is the one that fired: this audit makes one API
#: read per replayed push, so with `--runs 30` a single upstream 502 failed the
#: whole scheduled run -- and a red `CI Selection Audit` reads as "the CI
#: selection gate is unsound" rather than as a registry hiccup, which is the
#: expensive part.
_GH_TRANSIENT = re.compile(
    r"HTTP (?:429|5\d\d)"
    r"|connection reset|connection refused|no such host"
    r"|i/o timeout|TLS handshake timeout|unexpected EOF|EOF$",
    re.IGNORECASE | re.MULTILINE,
)

#: Mirrors `terrapod.http_retry`'s policy (3 retries, 1s base, 8s ceiling)
#: rather than importing it: this script runs from a bare checkout where
#: `terrapod` is not installed, so it stays stdlib-only like every other script
#: in here.
_GH_RETRIES = 3
_GH_BASE_DELAY = 1.0
_GH_MAX_DELAY = 8.0


def _gh(*args: str) -> str:
    """One `gh` call, retried on a transient failure (#2689's sibling).

    Bounded, and the LAST attempt reports whatever the reason -- there is
    nothing left to try, so classifying it again would only change the message.
    """
    for attempt in range(_GH_RETRIES + 1):
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
        if out.returncode == 0:
            return out.stdout
        stderr = out.stderr.strip()
        last = attempt == _GH_RETRIES
        if last or not _GH_TRANSIENT.search(stderr):
            tried = f" after {attempt + 1} attempts" if attempt else ""
            raise SystemExit(f"gh {' '.join(args)} failed{tried}: {stderr[:200]}")
        delay = min(_GH_MAX_DELAY, _GH_BASE_DELAY * (2**attempt))
        print(
            f"  gh {args[0] if args else ''} failed transiently, retrying in {delay:.0f}s "
            f"(attempt {attempt + 1}/{_GH_RETRIES + 1}): {stderr[:120]}",
            file=sys.stderr,
        )
        time.sleep(delay)
    raise SystemExit("unreachable")  # pragma: no cover - the loop always returns or raises


def _load_affected():
    """Load `ci-affected.py` by path -- its filename is not an importable name."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent / "ci-affected.py"
    spec = importlib.util.spec_from_file_location("ci_affected", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gated_jobs(repo: str, ref: str) -> dict[str, str]:
    """job name -> family, derived from ci.yml AT THAT REF."""
    import yaml

    raw = _gh(
        "api", f"repos/{repo}/contents/.github/workflows/ci.yml?ref={ref}", "--jq", ".content"
    )
    import base64

    wf = yaml.safe_load(base64.b64decode(raw.strip().replace("\n", "")))
    out: dict[str, str] = {}
    for jid, job in wf["jobs"].items():
        m = GATE_RE.search(str(job.get("if", "")))
        if m:
            # the display name is what the API reports; fall back to the id
            out[str(job.get("name", jid))] = m.group(1)
    return out


def findings(sha, gated, decisions, failed_jobs):
    """The whole judgement, as a pure function so it can be self-tested.

    A matrix job reports as "Name (shard-3)", so both sides are compared with
    the matrix suffix stripped. Without that every sharded job silently fails
    to match and the audit passes on everything -- the one direction that would
    make it worthless.
    """

    def base(n):
        return re.sub(r"\s*\([^)]*\)$", "", n)

    would_skip = {n for n, fam in gated.items() if not decisions.get(fam, True)}
    out = []
    for failed in failed_jobs:
        for skipped in would_skip:
            if base(failed) == base(skipped):
                out.append(
                    f"{sha[:8]}: '{failed}' FAILED on main, and the gate would have "
                    f"skipped it on the pull request -- the derived input set for "
                    f"family '{gated[skipped]}' is missing something this change touched"
                )
    return out


_FIXTURES = [
    (
        "a sharded job that failed and would have been skipped",
        {"Python Test": "python"},
        {"python": False},
        ["Python Test (integration-3)"],
        1,
    ),
    (
        "the same job, but the family would have run",
        {"Python Test": "python"},
        {"python": True},
        ["Python Test (integration-3)"],
        0,
    ),
    (
        "a failure in a job that is not gated at all",
        {"Python Test": "python"},
        {"python": False},
        ["E2E Tests (2/5)"],
        0,
    ),
    (
        "an unknown family resolves to RUN, so nothing is flagged",
        {"Python Test": "python"},
        {},
        ["Python Test (unit)"],
        0,
    ),
    (
        "no failures at all",
        {"Python Test": "python"},
        {"python": False},
        [],
        0,
    ),
]


def self_test():
    for why, gated, decisions, failed, expected in _FIXTURES:
        got = len(findings("abcdef1234", gated, decisions, failed))
        if got != expected:
            raise SystemExit(
                f"ci-selection-audit self-test FAILED: {why}\n"
                f"  expected {expected} finding(s), got {got}"
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="mattrobinsonsre/terrapod")
    ap.add_argument("--runs", type=int, default=25, help="how many main pushes to replay")
    args = ap.parse_args()

    self_test()
    affected = _load_affected()
    fams = affected.families()
    runs = json.loads(
        _gh(
            "run",
            "list",
            "--repo",
            args.repo,
            "--workflow",
            "ci.yml",
            "--limit",
            str(args.runs * 3),
            "--json",
            "databaseId,headBranch,event,conclusion,headSha",
        )
    )
    runs = [
        r
        for r in runs
        if r["headBranch"] == "main"
        and r["event"] == "push"
        and r["conclusion"] in ("success", "failure")
    ][: args.runs]

    if not runs:
        print("no completed pushes to main to audit")
        return 0

    problems: list[str] = []
    checked = 0
    for run in runs:
        sha = run["headSha"]
        try:
            gated = _gated_jobs(args.repo, sha)
        except SystemExit:
            continue  # the gate did not exist at that ref
        if not gated:
            continue
        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        if not files:
            continue
        decisions, _ = affected.decide(files, fams)
        would_skip = {name for name, fam in gated.items() if not decisions.get(fam, True)}
        if not would_skip:
            continue
        checked += 1
        jobs = json.loads(
            _gh(
                "api",
                f"repos/{args.repo}/actions/runs/{run['databaseId']}/jobs",
                "--paginate",
                "--jq",
                "[.jobs[]|{name,conclusion}]",
            )
        )
        failed = [j["name"] for j in jobs if j["conclusion"] == "failure"]
        problems.extend(findings(sha, gated, decisions, failed))

    if problems:
        print("ci-selection-audit: FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"PASS — replayed {len(runs)} push(es) to main; {checked} had at least one "
        f"family the gate would have skipped, and none of those jobs failed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
