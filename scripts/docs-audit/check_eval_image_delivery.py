#!/usr/bin/env python3
"""A job that boots the eval stack must deliver images on BOTH paths.

WHY THIS EXISTS. Image delivery into the eval stack is a two-halved pattern: on a
pull request the images arrive as build artifacts and are loaded into docker; on a
push to a branch or a tag they are pulled from GHCR instead. A job that implements
only the pull-request half is GREEN ON EVERY PR and RED ON MAIN -- the half that
is exercised while the change is under review is the half that was written, so
review cannot see the gap. `cli-contract` shipped exactly that and failed its first
run on main with

    No such image: .../terrapod-api:sha-<short>-amd64

while `helm-eval-boot`, ten lines away in the same file, had both halves.

That was the fourth time in one job that half a pattern was copied from a sibling,
so this checks the property rather than the instance: whatever boots the eval
stack has to say how images arrive on each path. It is derived from the workflow --
jobs are found by what they RUN, not from a list here -- so a new eval-booting job
is covered the day it is written rather than the day it breaks main.

WHAT IT CANNOT SEE: whether the delivered images are the RIGHT ones. A job can pull
the correct tags and still install something else, which is what `TERRAPOD_VERSION`
decides; that is the next value in this family and it has no gate.

STDLIB PLUS PyYAML, which the `python-checks` job installs before this runs.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

#: A job boots the eval stack if it runs the eval script. Matching on the script
#: rather than on a job name is what makes a new job covered automatically.
BOOTS = re.compile(r"scripts/eval\.sh")

#: Steps that put an image where the cluster can reach it. `ghcr-login` is listed
#: because a pull without it is anonymous and rate-limited, so the two travel
#: together on the branch/tag path.
DELIVERS = (
    "download-artifact",
    "ghcr-login",
    "docker pull",
    "docker load",
    "docker tag",
    "docker push",
)

#: A floor, so a matcher that stops matching fails here instead of passing on
#: nothing. Two jobs boot the eval stack today.
MIN_JOBS = 2

PR_ONLY = "is_pr == 'true'"
NOT_PR = "is_pr != 'true'"


def step_text(step: dict) -> str:
    parts = [str(step.get("name") or ""), str(step.get("uses") or ""), str(step.get("run") or "")]
    return "\n".join(parts)


def main() -> int:
    try:
        wf = yaml.safe_load(CI.read_text())
    except yaml.YAMLError as exc:
        sys.exit(f"FAIL: {CI} does not parse: {exc}")

    jobs = wf.get("jobs") or {}
    booting = {}
    for name, job in jobs.items():
        steps = job.get("steps") or []
        if any(BOOTS.search(str(s.get("run") or "")) for s in steps):
            booting[name] = steps

    problems: list[str] = []
    if len(booting) < MIN_JOBS:
        problems.append(
            f"only {len(booting)} job(s) matched as booting the eval stack, below "
            f"the floor of {MIN_JOBS}. The matcher has stopped matching -- fix it "
            f"rather than lowering the floor."
        )

    for name, steps in sorted(booting.items()):
        pr_half, tag_half = [], []
        for s in steps:
            cond = str(s.get("if") or "")
            if not any(d in step_text(s) for d in DELIVERS):
                continue
            label = s.get("name") or s.get("uses") or "(unnamed)"
            if PR_ONLY in cond:
                pr_half.append(label)
            elif NOT_PR in cond:
                tag_half.append(label)
        if not pr_half:
            problems.append(
                f"[{name}] delivers no images on the PULL REQUEST path (no "
                f"delivery step gated `{PR_ONLY}`)."
            )
        if not tag_half:
            problems.append(
                f"[{name}] delivers no images on the BRANCH/TAG path (no delivery "
                f"step gated `{NOT_PR}`). This job will be green on every pull "
                f"request and red on main, which is how it shipped the first time."
            )

    if problems:
        print(f"check_eval_image_delivery: {len(problems)} problem(s)\n")
        for p in problems:
            print(f"  {p}")
        print(
            "\n  Both halves are needed because a PR and a push deliver images by "
            "different\n  routes. Copy them from a job that already has both "
            "rather than writing new\n  ones -- that is the mistake this check "
            "was written after."
        )
        return 1

    print(
        f"check_eval_image_delivery: OK -- {len(booting)} job(s) boot the eval "
        f"stack, each delivering images on both the PR and the branch/tag path "
        f"({', '.join(sorted(booting))})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
