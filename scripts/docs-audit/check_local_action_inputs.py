#!/usr/bin/env python3
"""Every call of a LOCAL composite action passes the inputs that action requires.

WHY THIS EXISTS. `ssh-target-gate` and `git-source-gate` both called
`./.github/actions/ghcr-login` with no `with:` block at all. That action declares
`token` and `username` as `required: true`, so `docker login` ran with an empty
password and the step failed -- and the step is gated
`if: needs.prepare.outputs.is_pr != 'true'`, so it is SKIPPED on every pull
request and could not fail where anyone was looking.

The result: both gates passed their own PRs and `main` was red for four
consecutive runs, with neither gate's own logic ever executing. A green PR is
exactly what makes this invisible, which is why reading the call site is not
enough and a gate is.

It is DERIVED from each action's own `action.yml` rather than from a list of
input names here. If `ghcr-login` gains a required input tomorrow, every call
site is checked against it without anyone remembering this file exists -- and a
hardcoded list would have gone stale in exactly the way the thing it guards did.

WHAT IT CANNOT CATCH, stated because the limit matters: an input passed as an
expression that evaluates to empty at run time (`${{ secrets.MISSING }}`) is
present as far as YAML is concerned. This checks that the caller SAYS something
for every required input, which is the defect that occurred; whether the value
is non-empty is a secrets question no static reader can answer.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
ACTIONS_DIR = ROOT / ".github" / "actions"

#: A matcher that stops matching must fail rather than pass on nothing. There
#: are local actions and they are called; a floor of zero would make this file
#: decorative the moment the `uses:` spelling changed.
MIN_CALLS = 2


def required_inputs(action_dir: pathlib.Path) -> set[str] | None:
    """The input names this action declares `required: true`.

    `None` when there is no readable `action.yml`, which the caller reports
    rather than skipping: a `uses:` pointing at a directory with no action in it
    is its own defect and must not read as "nothing required".
    """
    for name in ("action.yml", "action.yaml"):
        path = action_dir / name
        if path.is_file():
            spec = yaml.safe_load(path.read_text()) or {}
            inputs = spec.get("inputs") or {}
            return {
                key
                for key, meta in inputs.items()
                # `required` may be a YAML boolean or the string "true".
                if str((meta or {}).get("required", False)).lower() == "true"
            }
    return None


def walk_steps(node, trail: tuple[str, ...] = ()):
    """Yield every `steps:` entry in a workflow, with a trail naming where it is.

    Walks rather than indexing `jobs.<id>.steps`, so a step inside anything the
    schema grows later is still reached.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "steps" and isinstance(value, list):
                for i, step in enumerate(value):
                    if isinstance(step, dict):
                        yield trail, i, step
            else:
                yield from walk_steps(value, trail + (str(key),))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk_steps(item, trail + (str(i),))


def main() -> int:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    problems: list[str] = []
    calls = 0

    for wf in workflows:
        try:
            doc = yaml.safe_load(wf.read_text()) or {}
        except yaml.YAMLError as exc:  # a workflow that will not parse is its own problem
            problems.append(f"{wf.name}: will not parse as YAML: {exc}")
            continue

        for trail, index, step in walk_steps(doc):
            uses = step.get("uses")
            if not isinstance(uses, str) or not uses.startswith("./.github/actions/"):
                continue
            calls += 1
            where = f"{wf.name}:{'.'.join(trail) or '<root>'} step {index}"
            name = uses.removeprefix("./.github/actions/").rstrip("/")
            needed = required_inputs(ACTIONS_DIR / name)
            if needed is None:
                problems.append(f"{where}: `uses: {uses}` names no readable action.yml")
                continue
            supplied = set((step.get("with") or {}).keys())
            missing = sorted(needed - supplied)
            if missing:
                label = step.get("name") or uses
                problems.append(
                    f"{where}: step {label!r} calls {name} without its required "
                    f"input(s) {missing}. The action declares them `required: true`, "
                    "so the step fails at run time -- and if it carries an "
                    "`if: ... is_pr != 'true'`, it is skipped on every pull request "
                    "and the failure only appears on main."
                )

    if calls < MIN_CALLS:
        problems.append(
            f"only found {calls} local-action call(s); expected at least "
            f"{MIN_CALLS}, so the `uses: ./.github/actions/` matcher has stopped matching"
        )

    if problems:
        print(f"FAIL — {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"PASS — all {calls} local-action call(s) across {len(workflows)} workflow(s) "
        "pass every input their action declares required."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
