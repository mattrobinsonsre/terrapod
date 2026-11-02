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
#: rather than passing on nothing.
MIN_SECTIONS = 8
MIN_CHECKS = 6


def main() -> int:
    for f in (CATALOGUE, SUITE):
        if not f.exists():
            sys.exit(f"FAIL: {f.relative_to(ROOT)} is missing; this gate reads it")

    sections = [m.group(1).strip() for m in re.finditer(r"^## (.+)$", CATALOGUE.read_text(), re.M)]
    covered = set(re.findall(r'^@check\(\s*"([^"]+)"', SUITE.read_text(), re.M))

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

    for s in sections:
        if s in covered or s in NOT_DRIVEN:
            continue
        problems.append(
            f"catalogue section {s!r} is neither driven by the CLI contract "
            f"suite nor listed in NOT_DRIVEN with a reason"
        )

    # The other direction: a reason for a section that no longer exists is a
    # stale excuse, and a section both driven and excused is a contradiction.
    for s in sorted(NOT_DRIVEN):
        if s not in sections:
            problems.append(
                f"NOT_DRIVEN names {s!r}, which is not a section of the catalogue any more"
            )
        if s in covered:
            problems.append(f"{s!r} is both driven by the suite and listed as not driven")

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
        f"{len(NOT_DRIVEN)} excused with a reason"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
