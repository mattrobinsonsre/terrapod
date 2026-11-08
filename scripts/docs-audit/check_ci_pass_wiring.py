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

SELF_TEST is the gate on the gate, and `MIN_NEEDS` cannot do that job: a floor
proves `needs` parsed to something non-empty and says nothing about whether the
`results=()` reader still discriminates. Narrow that regex and every job reads as
unjudged (loud, but for the wrong reason); widen it and a job that is merely
MENTIONED reads as judged, which is the silent direction and the one this gate
exists to prevent. Neither moves the floor, because `needs` is parsed separately.

A KNOWN GAP, stated rather than gated: the `results=()` reader matches inside a
shell comment, so commenting a line out of the array leaves the job reading as
judged. That is this gate's own defect shape arriving by the easiest route, and
it is left alone here only because closing it changes what the gate accepts --
which is a decision for whoever is reading this, not a refactor.

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

#: The `results=()` array as `ci.yml` actually writes it, used by the fixtures so
#: they exercise the real shape rather than a one-line paraphrase of it.
_REAL_RESULTS = """results=(
  "${{ needs.prepare.result }}"
  "${{ needs.python-checks.result }}"
  "${{ needs.build-images-amd64.result }}"
)

for r in "${results[@]}"; do
  if [[ "$r" == "failure" ]]; then
    echo "One or more jobs failed"
    exit 1
  fi
done
"""

#: Literal `run:` text and the job names it must be read as judging.
SELF_TEST_JUDGED: list[tuple[str, list[str]]] = [
    (_REAL_RESULTS, ["build-images-amd64", "prepare", "python-checks"]),
    # The expression without the whitespace GitHub does not require.
    ('"${{needs.sast.result}}"', ["sast"]),
    # A bare reference, which is also a valid way to write it.
    ("needs.helm-smoke.result", ["helm-smoke"]),
    # Must NOT match: a line commented OUT of the array. This is the gate's own
    # defect shape with a `#` in front of it -- the job stays in `needs` and is
    # still waited for, while nothing judges its verdict -- so a reader that
    # matched inside the comment would report it as judged and the whole gate
    # would pass over exactly what it exists to catch.
    ('    # "${{ needs.sast.result }}"', []),
    # And the live entry beside a commented one is still read, so the skip
    # cannot be satisfied by discarding the array wholesale.
    (
        '    # "${{ needs.sast.result }}"\n    "${{ needs.e2e-test.result }}"',
        ["e2e-test"],
    ),
    # Both characters a job name may carry besides letters and digits.
    (
        '"${{ needs.build_images.result }}" "${{ needs.e2e-test.result }}"',
        ["build_images", "e2e-test"],
    ),
    # Must NOT be read as judging: a job OUTPUT, not the job's result. These are
    # what the array sits next to, and reading either as a verdict would let a
    # job that is merely consulted pass as judged.
    ('echo "${{ needs.prepare.outputs.sha_short }}"', []),
    ('"${{ needs.prepare.outputs.result }}"', []),
    # Must NOT be read as judging: the loop that does the judging names no job.
    ('if [[ "$r" == "failure" ]]; then', []),
    ("", []),
]

#: `needs` is a list OR a bare string in GitHub Actions, and eleven jobs in this
#: very workflow use the scalar form (`needs: prepare`). `ci-pass` happens to use
#: a list, so this is latent rather than live -- but `list("prepare")` is seven
#: single characters, every one of them absent from `results=()`, so a scalar
#: would turn this gate into seven nonsense failures naming letters.
SELF_TEST_NEEDS: list[tuple[dict, list[str]]] = [
    ({"needs": ["prepare", "sast"]}, ["prepare", "sast"]),
    ({"needs": "prepare"}, ["prepare"]),
    ({"needs": None}, []),
    ({}, []),
]


def judged_jobs(run_text: str) -> set[str]:
    """The jobs a `ci-pass` step's shell actually reads a RESULT for.

    SHELL COMMENTS ARE SKIPPED, and that is this gate's own defect shape by the
    easiest available route: commenting a line out of the `results=()` array
    leaves the job in `needs`, so it is still waited for, while nothing judges
    its verdict -- and a reader that matched inside the comment would report it
    as judged. Exactly #1446 with a `#` in front of it.

    The converse direction was already covered (an entry for a job absent from
    `needs` is flagged), so this closes the half that was open.
    """
    live = "\n".join(line for line in run_text.split("\n") if not line.lstrip().startswith("#"))
    return set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.result", live))


def needs_of(job: dict) -> list[str]:
    """The jobs this one waits for, in either of the two forms Actions allows."""
    raw = job.get("needs")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def self_test() -> list[str]:
    """Asserted BEFORE the workflow is read, so a broken reader names the form.

    Order matters: a reader that has stopped seeing a spelling otherwise just
    checks less of the array, and reports on whatever it can still see.
    """
    out: list[str] = []
    for text, expected in SELF_TEST_JUDGED:
        got = sorted(judged_jobs(text))
        if got != sorted(expected):
            out.append(
                f"the results=() reader no longer reads {text.strip()[:60]!r} the way "
                f"it must: expected {sorted(expected)}, read {got}"
            )
    for job, expected in SELF_TEST_NEEDS:
        got = needs_of(job)
        if got != expected:
            out.append(f"needs_of({job!r}) gave {got!r}, expected {expected!r}")
    return out


def main() -> int:
    blockers = self_test()
    if blockers:
        print("check_ci_pass_wiring: this gate cannot do its job\n")
        for blocker in blockers:
            print(f"  {blocker}")
        print(
            "\n  Fix the reader rather than the fixture. A form it cannot see is a\n"
            "  form it does not check, and the floor below cannot tell those apart."
        )
        return 1

    if not CI.exists():
        sys.exit(f"FAIL: {CI.relative_to(ROOT)} is missing; this gate reads it")
    workflow = yaml.safe_load(CI.read_text())
    jobs = workflow.get("jobs", {})
    if "ci-pass" not in jobs:
        sys.exit("FAIL: ci.yml has no `ci-pass` job; it is the required status check")

    cp = jobs["ci-pass"]
    needs = needs_of(cp)
    run = "\n".join(str(s.get("run", "")) for s in cp.get("steps", []))
    judged = judged_jobs(run)

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

    print(
        f"check_ci_pass_wiring: OK -- ci-pass waits on {len(needs)} job(s) and judges "
        f"every one; {len(SELF_TEST_JUDGED) + len(SELF_TEST_NEEDS)} reader fixtures hold"
    )
    if notes:
        print(f"  (not wired into ci-pass, which may be deliberate: {', '.join(sorted(notes))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
