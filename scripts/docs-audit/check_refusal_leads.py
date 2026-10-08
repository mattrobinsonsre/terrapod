#!/usr/bin/env python3
"""Every refusal lead the inventory router raises is quoted in the docs.

A doc that reproduces an error message makes a claim which goes stale in
silence: the code changes, the page keeps showing output nothing produces, and
an operator searching for the text they were handed finds nothing. It had
already happened inside one of these pages before anything pinned it --
`docs/api-reference.md` quoted a tail reading "has to refresh this inventory"
against a code tail reading "has to produce it".

The leads are **derived** from the router, never listed here. A hand-kept list
is the defect in miniature: it can agree with the pages perfectly while both
have drifted from the code, which is exactly what the pytest half of this
check once allowed -- every test there fed a lead from its own tuple INTO the
helper, so nothing tied the CALL SITES to anything. Changing one call site's
lead while leaving the tuple and both pages alone left 44 tests green.

Why it lives here rather than beside those tests: the pytest tiers run inside
`docker/Dockerfile.test`, which copies `services/`, `alembic/`, `helm/` and
`docker/` but ships only one file out of `docs/`. A test there resolving
`../docs/...` finds nothing, so it either fails or -- worse -- quietly stops
checking. The docs-audit job runs against a full checkout. Same reasoning as
`check_token_mints.py`.

The pytest side keeps what it can see: that the tuple matches the call sites,
that every lead is distinct, and that the shared tail is byte-exact.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ROUTER = ROOT / "services/terrapod/api/routers/inventory.py"
HELPER = "_unresolvable_error"

#: Both pages reproduce the refusal, so both must quote whatever the code says.
PAGES = ("docs/ansible-inventory.md", "docs/api-reference.md")

#: Below this the matcher has stopped matching rather than the code having
#: shrunk: the resolved read, the resolve action and the limit preview each
#: raise their own opening.
MIN_LEADS = 3


def leads_from(router: Path) -> list[str]:
    """The literal each call site passes, in source order."""
    out: list[str] = []
    for node in ast.walk(ast.parse(router.read_text())):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == HELPER):
            continue
        if len(node.args) < 2:
            print(f"FAIL {router.name}: {HELPER} called without a lead: {ast.unparse(node)}")
            sys.exit(1)
        lead = node.args[1]
        if not (isinstance(lead, ast.Constant) and isinstance(lead.value, str)):
            # A computed lead cannot be quoted in a page, so it cannot be
            # pinned at all -- refused rather than skipped.
            print(
                f"FAIL {router.name}: a refusal lead must be a literal to be "
                f"documented: {ast.unparse(node)}"
            )
            sys.exit(1)
        out.append(lead.value)
    return out


def main() -> int:
    if not ROUTER.is_file():
        print(f"FAIL: {ROUTER.relative_to(ROOT)} not found -- has the router moved?")
        return 1

    leads = leads_from(ROUTER)
    if len(leads) < MIN_LEADS:
        print(
            f"FAIL: found {len(leads)} refusal lead(s), expected at least "
            f"{MIN_LEADS} -- has {HELPER} been renamed?"
        )
        return 1

    pages = [ROOT / rel for rel in PAGES]
    missing_pages = [p for p in pages if not p.is_file()]
    if missing_pages:
        for p in missing_pages:
            print(f"FAIL: {p.relative_to(ROOT)} not found")
        return 1

    # Whitespace-normalised, because a page may wrap a long message across
    # lines and that is a formatting choice rather than a drift.
    corpus = " ".join(" ".join(p.read_text().split()) for p in pages)

    failures = 0
    for lead in sorted(set(leads)):
        if " ".join(lead.split()) not in corpus:
            print(f"FAIL: no page quotes the refusal lead: {lead!r}")
            failures += 1

    if failures:
        print(f"\nPages checked: {', '.join(PAGES)}")
        return 1

    print(f"PASS — {len(set(leads))} refusal lead(s) quoted in {len(pages)} page(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
