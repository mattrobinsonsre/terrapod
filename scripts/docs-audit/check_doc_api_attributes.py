#!/usr/bin/env python3
"""Every `.attributes.X` the docs tell an operator to read is one we emit (#2406).

A runbook step is a command somebody runs during an incident. `docs/runbooks.md`
told an operator to check listener capacity with

    jq '.data[] | {active: .attributes["active-runs"], capacity: .attributes.capacity}'

and `_listener_json` serialises neither, so it printed `active: null, capacity:
null` — in the runbook for the exact symptom the #2399 fix produces, and two
lines from a step that was being rewritten for that release. Nothing caught it,
because no gate reads a doc's field references against what the API serialises.

The oracle already exists: `api_attribute_contract.json` is the committed
snapshot of every attribute every serialiser emits, regenerated deliberately
whenever the surface grows. So this needs no second source of truth — it joins
the two things that are already maintained.

Deliberately the UNION across serialisers rather than per-endpoint. Mapping a
doc snippet to one serialiser means parsing the URL out of a shell pipeline,
which is fragile; the union still catches the real class — a name no serialiser
emits anywhere — and cannot produce a false positive from a correct reference to
a different endpoint.

STDLIB ONLY: `scripts/docs-audit/` runs in a job with no Terrapod dependencies
installed, so this reads the snapshot as JSON and the docs as text.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "services" / "tests" / "api" / "api_attribute_contract.json"

#: References that deliberately name something the API does NOT emit, each with
#: a phrase that must appear in the same document. The phrase is what stops an
#: entry here being widened silently: a reader has to be told, in the document,
#: that the field is absent.
DOCUMENTED_AS_ABSENT: dict[str, tuple[str, str]] = {
    "comment": (
        "docs/tfe-cli-surface.md",
        "find nothing",
    ),
}

#: Floors, so a matcher that stops matching FAILS rather than passing on nothing.
MIN_REFERENCES = 8
MIN_CONTRACT_NAMES = 200

REF = re.compile(r"\.attributes(?:\.([A-Za-z][\w-]*)|\[\"([\w-]+)\"\]|\['([\w-]+)'\])")


def contract_names() -> set[str]:
    """Every attribute name any serialiser emits, at any nesting depth."""

    def walk(value) -> set[str]:
        out: set[str] = set()
        if isinstance(value, list):
            out |= {v for v in value if isinstance(v, str)}
        elif isinstance(value, dict):
            for key, sub in value.items():
                out.add(key)
                out |= walk(sub)
        return out

    data = json.loads(CONTRACT.read_text())
    names = set()
    for value in data.values():
        names |= walk(value)
    return names


def tracked_docs() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md", "llms.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / p for p in out]


def main() -> int:
    emitted = contract_names()
    if len(emitted) < MIN_CONTRACT_NAMES:
        print(
            f"only {len(emitted)} attribute names read from {CONTRACT.name}, below the "
            f"floor of {MIN_CONTRACT_NAMES}. The snapshot reader has stopped reading."
        )
        return 1

    problems: list[str] = []
    seen = 0
    for path in tracked_docs():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(errors="ignore")
        for match in REF.finditer(text):
            name = match.group(1) or match.group(2) or match.group(3)
            seen += 1
            if name in emitted:
                continue
            exempt = DOCUMENTED_AS_ABSENT.get(name)
            if exempt is not None:
                where, phrase = exempt
                if rel == where and phrase in text:
                    continue
                problems.append(
                    f"{rel}: `.attributes.{name}` is exempt only in {where} and only while "
                    f"that document says {phrase!r} — neither holds here."
                )
                continue
            line = text[: match.start()].count("\n") + 1
            problems.append(
                f"{rel}:{line}: the docs tell a reader to read `.attributes.{name}`, "
                "which NO serialiser emits.\n"
                "      Either fix the snippet to read a field the API returns, or add the "
                "attribute to the serialiser and regenerate the attribute contract. If the "
                "point is that the field is deliberately absent, add it to "
                "DOCUMENTED_AS_ABSENT with the phrase that tells the reader so."
            )

    if seen < MIN_REFERENCES:
        problems.append(
            f"only {seen} `.attributes.*` references found across the docs, below the floor "
            f"of {MIN_REFERENCES}. The reference matcher has stopped matching."
        )

    if problems:
        print("the docs reference API attributes that are not served:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"OK — {seen} `.attributes.*` reference(s) across the docs, all served by one of "
        f"{len(emitted)} snapshotted attributes; "
        f"{len(DOCUMENTED_AS_ABSENT)} documented-as-absent exemption(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
