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

A SECOND BLIND SPOT, stated rather than gated: the subject is derived by matching
`scripts/eval.sh`, and `make eval` runs exactly that -- so a job booting the stack
through the Makefile target is invisible here, and invisible is the one failure
this gate must not have. No job in CI does that today (one step is merely NAMED
`make eval` while running the script), so it is latent; widening the matcher would
change which jobs are in scope, which is a decision rather than a refactor. The
fixtures below pin it as a must-not-match so the gap stays visible.

SELF_TEST is what makes the classification checkable, and `MIN_JOBS` cannot do it:
a floor proves some job matched as booting the stack and says nothing about whether
"delivers an image" still discriminates. The first version counted `ghcr-login` as
a delivery, so deleting the `docker pull` and keeping the login PASSED -- the floor
was satisfied throughout, because the job still matched and still had steps. That
exact pair is fixtured.

STDLIB PLUS PyYAML, which the `python-checks` job installs before this runs.
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

#: A job boots the eval stack if it runs the eval script. Matching on the script
#: rather than on a job name is what makes a new job covered automatically.
# `make eval` counts, because `Makefile`'s own recipe for it IS
# `scripts/eval.sh up` -- so a job booting the stack that way was outside this
# gate's SUBJECT, and a derived gate's subject decides its blind spot. No job
# does it today, which is what made the hole latent rather than live; being
# invisible is the one failure mode a gate must not have.
BOOTS = re.compile(r"scripts/eval\.sh|make\s+eval\b")

#: Steps that actually MOVE AN IMAGE. Authentication is deliberately NOT in this
#: list, and that distinction is the whole correctness of this check: the first
#: version included `ghcr-login`, so deleting the `docker pull` and keeping the
#: login PASSED -- the gate written to catch "no images were delivered" could not
#: catch exactly that. A login delivers nothing. Mutation-proven both ways.
MOVES = (
    "download-artifact",
    "docker pull",
    "docker load",
    "docker tag",
    "docker push",
)

#: Authentication, which a branch/tag pull needs but which is not delivery. An
#: anonymous GHCR pull is rate-limited per source IP and runners share egress,
#: so a pull without this is a flake waiting for a busy afternoon -- reported
#: separately rather than counted as a delivery.
AUTHENTICATES = ("ghcr-login",)

#: A floor, so a matcher that stops matching fails here instead of passing on
#: nothing. Two jobs boot the eval stack today.
MIN_JOBS = 2

PR_ONLY = "is_pr == 'true'"
NOT_PR = "is_pr != 'true'"


def step_text(step: dict) -> str:
    parts = [str(step.get("name") or ""), str(step.get("uses") or ""), str(step.get("run") or "")]
    return "\n".join(parts)


def boots_eval(steps: list[dict]) -> bool:
    """Does this job stand up the eval stack?"""
    return any(BOOTS.search(str(s.get("run") or "")) for s in steps)


def classify(steps: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """(PR-path deliveries, branch/tag deliveries, branch/tag logins) by label.

    The scan calls this, so the fixtures below drive the same code the real run
    does rather than a restatement of it.
    """
    pr_half: list[str] = []
    tag_half: list[str] = []
    tag_auth: list[str] = []
    for s in steps:
        cond = str(s.get("if") or "")
        text = step_text(s)
        label = s.get("name") or s.get("uses") or "(unnamed)"
        if any(a in text for a in AUTHENTICATES) and NOT_PR in cond:
            tag_auth.append(label)
        if not any(d in text for d in MOVES):
            continue
        if PR_ONLY in cond:
            pr_half.append(label)
        elif NOT_PR in cond:
            tag_half.append(label)
    return pr_half, tag_half, tag_auth


#: `(steps, pr_half, tag_half, tag_auth)`. Labels, because that is what the
#: failure message prints and therefore what a reader acts on.
SELF_TEST_CLASSIFY: list[tuple[list[dict], list[str], list[str], list[str]]] = [
    # The live shape, both halves present: a login and a pull on the branch/tag
    # path, an artifact download and a load on the pull-request path.
    (
        [
            {
                "name": "Log in to GHCR (branch/tag)",
                "uses": "./.github/actions/ghcr-login",
                "if": "needs.prepare.outputs.is_pr != 'true'",
            },
            {
                "name": "Pull images (branch/tag)",
                "run": "docker pull ghcr.io/x/y:sha-abc",
                "if": "needs.prepare.outputs.is_pr != 'true'",
            },
            {
                "name": "Download image artifacts (PR)",
                "uses": "actions/download-artifact@abc",
                "if": "needs.prepare.outputs.is_pr == 'true'",
            },
            {
                "name": "Load images into docker (PR, k3d)",
                "run": "docker load -i img.tar",
                "if": "needs.prepare.outputs.is_pr == 'true' && matrix.tool == 'k3d'",
            },
            {"name": "make eval", "run": "scripts/eval.sh up"},
        ],
        ["Download image artifacts (PR)", "Load images into docker (PR, k3d)"],
        ["Pull images (branch/tag)"],
        ["Log in to GHCR (branch/tag)"],
    ),
    # MUST NOT count as a delivery: a login on its own. This is the #2334 defect
    # verbatim -- the first version of this gate passed on exactly these steps,
    # with the `docker pull` deleted, which is the one arrangement it exists to
    # refuse. If `tag_half` ever reads non-empty here the check is back to
    # accepting authentication as delivery.
    (
        [
            {
                "name": "Log in to GHCR (branch/tag)",
                "uses": "./.github/actions/ghcr-login",
                "if": "needs.prepare.outputs.is_pr != 'true'",
            },
            {
                "name": "Download image artifacts (PR)",
                "uses": "actions/download-artifact@abc",
                "if": "needs.prepare.outputs.is_pr == 'true'",
            },
        ],
        ["Download image artifacts (PR)"],
        [],
        ["Log in to GHCR (branch/tag)"],
    ),
    # `uses` and `run` are both read. Either one alone leaves a live form
    # unexamined: the PR half arrives as a `uses`, the branch/tag half as a `run`.
    (
        [{"uses": "actions/download-artifact@abc", "if": PR_ONLY}],
        ["actions/download-artifact@abc"],
        [],
        [],
    ),
    ([{"run": "docker pull x", "if": NOT_PR}], [], ["(unnamed)"], []),
    # MUST NOT be assigned to a half: an UNGATED delivery declares neither path,
    # and a job with only this one would still be red on main.
    ([{"name": "Load", "run": "docker load -i img.tar"}], [], [], []),
    # MUST NOT be assigned to a half: a step that moves nothing.
    ([{"name": "Boot", "run": "scripts/eval.sh up", "if": NOT_PR}], [], [], []),
    ([], [], [], []),
]

#: `(run text, boots)`. The subject's own derivation, which decides what every
#: check above is applied TO.
SELF_TEST_BOOTS: list[tuple[str, bool]] = [
    ("scripts/eval.sh up", True),
    ("set -euo pipefail\n./scripts/eval.sh down", True),
    # Must NOT match: a different script whose name merely starts the same way.
    ("scripts/eval-smoke-plan.sh --workspace x", False),
    # MUST match: `make eval`'s own recipe is `scripts/eval.sh up`, so a job
    # written that way boots the stack. It was a documented gap here until the
    # rule was widened to cover it; no workflow uses that form today, which is
    # what made the hole latent rather than live.
    ("make eval", True),
    ("make  eval", True),
    # Must NOT match: a longer target that merely starts with the same word.
    ("make evaluate-something", False),
    ("", False),
]


def self_test() -> list[str]:
    """Asserted BEFORE the workflow is read, so a broken rule names the step.

    Order matters. A classification that has stopped recognising a delivery
    otherwise reports the job as missing a half -- loud, but blaming `ci.yml` for
    the gate's own defect -- while one that has started accepting too much
    reports OK over a job that will be red on main.
    """
    out: list[str] = []
    for steps, pr, tag, auth in SELF_TEST_CLASSIFY:
        got = classify(steps)
        if got != (pr, tag, auth):
            out.append(
                f"classify() no longer reads {[s.get('name') or s.get('uses') for s in steps]} "
                f"the way it must: expected {(pr, tag, auth)}, got {got}"
            )
    for text, expected in SELF_TEST_BOOTS:
        got = boots_eval([{"run": text}])
        if got != expected:
            out.append(
                f"boots_eval({text!r}) gave {got}, expected {expected} -- the SUBJECT "
                f"of this gate has changed, so every check above applies to a "
                f"different set of jobs"
            )
    return out


def main() -> int:
    blockers = self_test()
    if blockers:
        print("check_eval_image_delivery: this gate cannot do its job\n")
        for blocker in blockers:
            print(f"  {blocker}")
        print(
            "\n  Fix the rule rather than the fixture. A delivery form this check\n"
            "  cannot see is one it does not require, and the floor cannot tell\n"
            "  those apart -- that is how a GHCR login once counted as a delivery."
        )
        return 1

    try:
        wf = yaml.safe_load(CI.read_text())
    except yaml.YAMLError as exc:
        sys.exit(f"FAIL: {CI} does not parse: {exc}")

    jobs = wf.get("jobs") or {}
    booting = {}
    for name, job in jobs.items():
        steps = job.get("steps") or []
        if boots_eval(steps):
            booting[name] = steps

    problems: list[str] = []
    if len(booting) < MIN_JOBS:
        problems.append(
            f"only {len(booting)} job(s) matched as booting the eval stack, below "
            f"the floor of {MIN_JOBS}. The matcher has stopped matching -- fix it "
            f"rather than lowering the floor."
        )

    for name, steps in sorted(booting.items()):
        pr_half, tag_half, tag_auth = classify(steps)
        if not pr_half:
            problems.append(
                f"[{name}] delivers no images on the PULL REQUEST path (no "
                f"delivery step gated `{PR_ONLY}`)."
            )
        if not tag_half:
            problems.append(
                f"[{name}] delivers no images on the BRANCH/TAG path (no step that "
                f"MOVES an image, gated `{NOT_PR}`). This job will be green on "
                f"every pull request and red on main, which is how it shipped the "
                f"first time. Note a GHCR login does not count: it authenticates "
                f"a pull, it does not perform one."
            )
        elif not tag_auth:
            problems.append(
                f"[{name}] pulls images on the branch/tag path ({', '.join(tag_half)}) "
                f"with no GHCR login gated `{NOT_PR}`. An anonymous pull is "
                f"rate-limited per source IP and runners share egress, so this is "
                f"a flake waiting for a busy afternoon."
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
        f"({', '.join(sorted(booting))}); "
        f"{len(SELF_TEST_CLASSIFY) + len(SELF_TEST_BOOTS)} rule fixtures hold"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
