#!/usr/bin/env python3
"""The CLI-surface catalogue is also the CLI-contract test list (#2341).

`docs/tfe-cli-surface.md` catalogues the endpoints `terraform`/`tofu`/`tfci`
consume and calls them a stable contract. `scripts/cli-contract/run.py` drives
the real binary against them. Nothing tied the two together, so a section could
be added to the catalogue -- a new promise -- with no check behind it, and the
only sign would be that nobody had thought about it.

So this asserts that every catalogue SECTION is either exercised or carries an
explicit reason not to be. Sections, not individual rows: a row is one `go-tfe`
call, and a single `tofu apply` goes through a dozen of them in sequence -- a
per-row map would be a ledger of fiction. What the suite can honestly claim is
"the CLI does this, end to end, and these rows are how".

The two floors below prove the subject is non-empty and say nothing about
whether the predicate discriminates (#2425 §2.7). Twelve sections clear
`MIN_SECTIONS` whether the heading matcher reads `## ` or has quietly started
reading `#` as well; and the comparison itself -- exact membership, in both
directions -- has no floor at all, so a sloppy `in` against a joined string
would pass on every real input. `SELF_TEST_*` therefore pins the two matchers
and the decision against literal fixtures, asserted BEFORE either file is read,
including inputs that must NOT be read as a section, a check, or a match.

The `## ` level is the gate's whole granularity decision, so it is fixtured
explicitly: a `###` body-catalogue heading is NOT a section here, because a row
is one `go-tfe` call and a per-row map would be a ledger of fiction. The
must-not-match fixture for `### Workspace lock attributes` is what stops that
being eroded by a matcher widened to "any heading".

STDLIB ONLY, and it reads both files as text rather than importing the suite:
`run.py` imports nothing Terrapod-specific but does need `tofu` on PATH to do
anything useful, and a gate that needs a binary installed is a gate that gets
skipped.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "docs" / "tfe-cli-surface.md"
SUITE = ROOT / "scripts" / "cli-contract" / "run.py"

#: Sections deliberately not driven, each with the reason. An entry here is a
#: decision; an absence is an oversight, which is the whole distinction this
#: gate exists to keep visible.
NOT_DRIVEN: dict[str, str] = {
    "Organizations": "read during discovery by the cloud backend; exercised implicitly by every init",
    "Projects": "Terrapod has no projects (single organization, rule 9); the rows exist so go-tfe does not 404",
    "Configuration Versions": "the cloud backend uploads one inside `plan`/`apply`; covered by the lifecycle check",
    "Cost Estimates / Policy Checks / Task Stages": "server-side gates on a run, not endpoints the CLI calls",
    "tfci / tfc-workflows-github": "a separate binary with its own flow; its own job later (#2341 scope)",
    "Module Registry (CLI Download Protocol)": "exercised by the provider-mirror work",
    "Provider Registry (CLI Download Protocol)": "exercised by the provider-mirror work",
    "Maintaining This Document": "prose, not a surface",
}

#: A floor on each derived set, so a matcher that stops matching fails here
#: rather than passing on nothing. They bound total breakage only; SELF_TEST_*
#: is what pins each form, because a count over the catalogue cannot tell a
#: document that lost a section from a matcher that stopped reading one.
MIN_SECTIONS = 8
MIN_CHECKS = 6

SECTION = re.compile(r"^## (.+)$", re.M)
CHECK = re.compile(r'^@check\(\s*"([^"]+)"', re.M)

#: Markdown -> the section names it must yield, in order.
SELF_TEST_SECTIONS: list[tuple[str, list[str]]] = [
    ("## Runs / Plans / Applies", ["Runs / Plans / Applies"]),
    ("## tfci / tfc-workflows-github", ["tfci / tfc-workflows-github"]),
    # Trailing whitespace is a markdown accident, not a different section.
    ("## State Versions   ", ["State Versions"]),
    (
        "## Workspaces\n\nsome prose\n\n## State Versions\n",
        ["Workspaces", "State Versions"],
    ),
    # MUST NOT MATCH -- the granularity decision. A `###` body catalogue is not
    # a promise this gate tracks; see the module docstring.
    ("### Workspace lock attributes", []),
    ("### Run-action attributes (`apply` / `discard` / `cancel`)", []),
    # MUST NOT MATCH -- the document title, an indented line, and a heading
    # without the separating space.
    ("# TFE V2 CLI Surface", []),
    ("  ## Indented", []),
    ("##NoSpace", []),
]

#: Suite source -> the check names (first argument) it must yield.
SELF_TEST_CHECKS: list[tuple[str, list[str]]] = [
    ('@check("Workspaces", "init against a cloud block finds the workspace")', ["Workspaces"]),
    (
        '@check("State Versions", "state pull returns what apply wrote", needs=_LIFECYCLE)',
        ["State Versions"],
    ),
    # The multi-line form, which the suite really uses.
    (
        '@check(\n    "Runs / Plans / Applies",\n    "plan and apply run",\n'
        '    needs=("init against a cloud block finds the workspace",),\n)',
        ["Runs / Plans / Applies"],
    ),
    # MUST NOT MATCH: a commented-out decorator, and a mention in prose or a
    # docstring. Both would otherwise mark a section covered by a check that
    # does not run -- the one direction of this gate that fails silently.
    ('# @check("Workspaces", "retired")', []),
    ('"""Each surface is driven by @check("Workspaces", ...).  """', []),
]

#: `(sections, covered, not_driven)` -> how many problems the decision reports.
#: The floors cannot see this half at all, and the third entry is the one that
#: matters: coverage is EXACT membership, so a check whose name merely contains
#: a section's name does not cover it. A matcher relaxed to a substring test
#: would pass every real input and silently excuse a section.
SELF_TEST_DECISION: list[tuple[list[str], list[str], dict[str, str], int]] = [
    (["Workspaces"], ["Workspaces"], {}, 0),
    (["Workspaces"], [], {"Workspaces": "a stated reason"}, 0),
    # Neither driven nor excused: the #2341 defect, a promise with nothing behind it.
    (["Workspaces"], [], {}, 1),
    # Exact, not substring, in either direction.
    (["Organizations"], ["Organizations and projects"], {}, 1),
    (["Organizations and projects"], ["Organizations"], {}, 1),
    # A reason for a section that no longer exists is a stale excuse.
    ([], [], {"Gone": "a stated reason"}, 1),
    # Both driven and excused is a contradiction, and the section itself is fine,
    # so exactly one problem.
    (["Workspaces"], ["Workspaces"], {"Workspaces": "a stated reason"}, 1),
]


def sections_in(text: str) -> list[str]:
    """Every `## ` section of the catalogue, which is one promise each."""
    return [m.group(1).strip() for m in SECTION.finditer(text)]


def checks_in(text: str) -> set[str]:
    """Every section name the CLI-contract suite claims to drive."""
    return set(CHECK.findall(text))


def coverage_problems(
    sections: list[str], covered: set[str] | list[str], not_driven: dict[str, str]
) -> list[str]:
    """Both directions of the catalogue-to-suite comparison.

    Pure, over the three derived sets, so `SELF_TEST_DECISION` drives exactly
    what the scan drives -- this is the half no floor can see.
    """
    covered = set(covered)
    problems: list[str] = []
    for s in sections:
        if s in covered or s in not_driven:
            continue
        problems.append(
            f"catalogue section {s!r} is neither driven by the CLI contract "
            f"suite nor listed in NOT_DRIVEN with a reason"
        )
    # The other direction: a reason for a section that no longer exists is a
    # stale excuse, and a section both driven and excused is a contradiction.
    for s in sorted(not_driven):
        if s not in sections:
            problems.append(
                f"NOT_DRIVEN names {s!r}, which is not a section of the catalogue any more"
            )
        if s in covered:
            problems.append(f"{s!r} is both driven by the suite and listed as not driven")
    return problems


def self_test() -> list[str]:
    """Pin both matchers and the decision, before either file is read."""
    out: list[str] = []
    for text, want in SELF_TEST_SECTIONS:
        got = sections_in(text)
        if got != want:
            out.append(
                f"the section matcher no longer reads {text!r} the way it must: expected "
                f"{want}, read {got}. A heading this gate cannot see is a promise it does "
                "not check; a heading it should not see is granularity it has lost."
            )
    for text, want_checks in SELF_TEST_CHECKS:
        got_checks = checks_in(text)
        if sorted(got_checks) != sorted(want_checks):
            out.append(
                f"the @check matcher no longer reads {text!r} the way it must: expected "
                f"{sorted(want_checks)}, read {sorted(got_checks)}."
            )
    for secs, cov, nd, want_n in SELF_TEST_DECISION:
        got_problems = coverage_problems(secs, cov, nd)
        if len(got_problems) != want_n:
            out.append(
                f"the coverage decision for sections={secs} covered={cov} "
                f"not_driven={sorted(nd)} reported {len(got_problems)} problem(s), not "
                f"{want_n}: {got_problems}"
            )
    return out


def main() -> int:
    # The gate's own health first: a broken matcher or a relaxed comparison makes
    # every result below meaningless.
    broken = self_test()
    if broken:
        print("the CLI-surface-covered gate cannot do its job:")
        for problem in broken:
            print(f"  {problem}")
        return 1

    for f in (CATALOGUE, SUITE):
        if not f.exists():
            sys.exit(f"FAIL: {f.relative_to(ROOT)} is missing; this gate reads it")

    sections = sections_in(CATALOGUE.read_text())
    covered = checks_in(SUITE.read_text())

    problems: list[str] = []
    if len(sections) < MIN_SECTIONS:
        problems.append(
            f"only {len(sections)} catalogue section(s) matched, below the floor "
            f"of {MIN_SECTIONS}. The heading matcher has stopped matching; fix it "
            f"rather than lowering the floor."
        )
    if (
        len(covered) < 1
        or sum(1 for _ in re.finditer(r"^@check\(", SUITE.read_text(), re.M)) < MIN_CHECKS
    ):
        problems.append(
            f"fewer than {MIN_CHECKS} checks matched in {SUITE.name}; the @check "
            f"matcher has stopped matching."
        )

    problems += coverage_problems(sections, covered, NOT_DRIVEN)

    if problems:
        print(f"check_cli_surface_covered: {len(problems)} problem(s)\n")
        for p in problems:
            print(f"  {p}")
        print(
            "\n  docs/tfe-cli-surface.md is the catalogue of what terraform/tofu\n"
            "  consume, and it is also the test list. A section added there is a\n"
            "  promise; this is where it gets a check or a stated reason not to."
        )
        return 1

    print(
        f"check_cli_surface_covered: OK -- {len(sections)} catalogue section(s), "
        f"{len(covered)} driven by the CLI contract suite, "
        f"{len(NOT_DRIVEN)} excused with a reason, "
        f"{len(SELF_TEST_SECTIONS) + len(SELF_TEST_CHECKS) + len(SELF_TEST_DECISION)} "
        "matcher and decision fixtures hold"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
