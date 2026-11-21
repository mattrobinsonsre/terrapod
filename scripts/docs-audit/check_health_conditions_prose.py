#!/usr/bin/env python3
"""The docs' list of workspace health conditions matches the code's.

`docs/upgrading-to-2.0.md` tells a 1.x operator what to replace
`status:unhealthy` with: the seven `flag:` terms, one per health condition,
followed by "(all **seven** health conditions)".

Both halves were ungated. `check_counts.py` matches digit forms, so the WORD
survives an eighth condition untouched, and nothing at all compared the flag
list against the conditions `_compute_health_conditions` actually emits. An
operator following a short list gets a filter that silently misses a workspace
in the state it was written to find -- which is worse than the `status:unhealthy`
they were migrating away from, because it looks like it worked (#2632).

Reads `tfe_v2.py` as TEXT by AST rather than importing it: this job installs no
Terrapod dependencies, and importing an API router pulls in the whole closure.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTER = ROOT / "services/terrapod/api/routers/tfe_v2.py"
DOC = ROOT / "docs/upgrading-to-2.0.md"

#: Enough to cover a plausible count; an unlisted word fails loudly below rather
#: than passing, since a number we cannot read is one we cannot check.
WORDS = {
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

#: The floor exists because every check here is "the doc agrees with a set"; a
#: reader that stops finding the set reports agreement with nothing.
MIN_CONDITIONS = 5


def condition_codes() -> set[str]:
    """The `"code": "..."` literals inside `_compute_health_conditions`."""
    tree = ast.parse(ROUTER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_compute_health_conditions":
            codes = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Dict):
                    continue
                for k, v in zip(sub.keys, sub.values, strict=True):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "code"
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                    ):
                        codes.add(v.value)
            return codes
    raise SystemExit("FAIL — `_compute_health_conditions` not found in tfe_v2.py")


def main() -> int:
    codes = condition_codes()
    if len(codes) < MIN_CONDITIONS:
        print(f"FAIL — only {len(codes)} health condition(s) read; the reader has stopped matching")
        return 1
    expected_flags = {"flag:" + c.replace("_", "-") for c in codes}

    text = DOC.read_text()
    # Selected on the CLAIM, not on the two words appearing together: prose
    # elsewhere in the page explains what `flag:` reads without enumerating
    # anything, and a looser filter reports that line as missing every flag --
    # a gate whose findings are mostly correct text is one people learn to
    # silence.
    claim = re.compile(r"all \*\*(?:[a-z]+|\d+)\*\* health conditions")
    rows = [line for line in text.split("\n") if claim.search(line)]
    if not rows:
        print(
            "FAIL — no row in docs/upgrading-to-2.0.md claims to list all the health "
            "conditions; if the wording changed, teach this gate the new shape rather "
            "than dropping the claim"
        )
        return 1

    problems: list[str] = []
    for row in rows:
        named = set(re.findall(r"flag:[a-z-]+", row))
        if named != expected_flags:
            missing = sorted(expected_flags - named)
            extra = sorted(named - expected_flags)
            problems.append(f"flag list differs: missing={missing} unknown={extra}")
        m = re.search(r"all \*\*([a-z]+|\d+)\*\* health conditions", row)
        if m is None:
            problems.append("the row no longer states how many conditions there are")
            continue
        raw = m.group(1)
        got = int(raw) if raw.isdigit() else WORDS.get(raw)
        if got is None:
            problems.append(f"unreadable count word {raw!r}; add it to WORDS or use a digit")
        elif got != len(codes):
            problems.append(f"says {raw} ({got}) health conditions; the code emits {len(codes)}")

    if problems:
        print("FAIL — docs/upgrading-to-2.0.md disagrees with _compute_health_conditions:")
        for p in problems:
            print(f"  {p}")
        return 1

    print(
        f"PASS — {len(rows)} row(s) list all {len(codes)} health conditions, "
        "and say so in a number that matches."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
