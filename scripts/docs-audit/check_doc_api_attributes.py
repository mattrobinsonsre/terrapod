#!/usr/bin/env python3
"""Every attribute the docs tell an operator to read is one we emit (#2406).

A runbook step is a command somebody runs during an incident. `docs/runbooks.md`
told an operator to check listener capacity with

    jq '.data[] | {active: .attributes["active-runs"], capacity: .attributes.capacity}'

and `_listener_json` serialises neither, so it printed `active: null, capacity:
null` -- in the runbook for the exact symptom the #2399 fix produces, and two
lines from a step that was being rewritten for that release. Nothing caught it,
because no gate reads a doc's field references against what the API serialises.

The oracle already exists: `api_attribute_contract.json` is the committed
snapshot of every attribute every serialiser emits, regenerated deliberately
whenever the surface grows. So this needs no second source of truth -- it joins
the two things that are already maintained.

Deliberately the UNION across serialisers rather than per-endpoint. Mapping a
doc snippet to one serialiser means parsing the URL out of a shell pipeline,
which is fragile; the union still catches the real class -- a name no serialiser
emits anywhere -- and cannot produce a false positive from a correct reference to
a different endpoint. The cost, stated plainly: a name that is right for some
other endpoint and wrong for this one passes.

`attributes` and `meta` are DIFFERENT name sets, and unioning them re-admits the
very defect. The snapshot keys a serialiser's `meta` keys under `<mod>.<fn>:meta`,
and 22 of those names are emitted ONLY there -- `signing-kid`, `cursor`,
`blocking`, `outstanding`. The first version of this gate unioned the two, so
`.attributes["signing-kid"]` was accepted: a plausible slip against a runbook step
that correctly reads `.meta["signing-kid"]`, and a diagnostic that prints null.
Only attribute names are valid for `.attributes.X` now, and the meta set is kept
to say so in the message -- "it IS emitted under `meta`" -- which turns the
failure into an actionable one.

Checking `.meta.X` as well is a deliberate NON-GOAL, because there is no oracle
for it. `meta.pagination` comes from the shared `paginate()` helper rather than
from any serialiser, so it is nowhere in the snapshot -- while 22 live references
across `AGENTS.md`, `docs/api-reference.md`, `llms.txt` and the pagination
convention itself read exactly that. A `.meta` arm fails on correct prose 22 times
out of 22, which is the #1915 shape: a gate whose findings are nearly all
legitimate is one people learn to silence. It stays out until `paginate()`'s own
keys are frozen somewhere.

The matcher covers every spelling, and SELF_TEST pins that. The first version read
three of jq's four ways to name a field and missed `.attributes."x"` -- the
idiomatic form for a hyphenated key, which is to say the exact shape of the
reference that caused #2406. Five live references used it, so the gate was blind
to a fifth of the docs while claiming the whole class in its own name. The
spellings are therefore asserted against literal fixtures BEFORE the scan rather
than against whatever the docs happen to contain: a doc dropping its last bracket
reference is benign, a matcher losing the bracket alternative is not, and a count
over the tree cannot tell those two apart.

STDLIB ONLY: `scripts/docs-audit/` runs in a job with no Terrapod dependencies
installed, so this reads the snapshot as JSON and the docs as text. Importing the
test module that GENERATES the snapshot would import `terrapod.api.routers`, and
therefore SQLAlchemy, which passes locally and fails in CI.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "services" / "tests" / "api" / "api_attribute_contract.json"

#: References that deliberately name something the API does NOT emit, each with a
#: phrase that must appear in the same document. The phrase is what stops an entry
#: being widened silently: a reader has to be told, in the document, that the field
#: is absent. It must be specific -- a first version took any substring, so `"the"`
#: would have exempted anything in a 3000-line runbook -- so it carries a length
#: floor, AND the attribute's own name must appear in that document, which anchors
#: the exemption to a passage that actually discusses the field.
DOCUMENTED_AS_ABSENT: dict[str, tuple[str, str]] = {
    "comment": (
        "docs/tfe-cli-surface.md",
        "find nothing",
    ),
}
MIN_EXEMPTION_PHRASE = 12

#: Names a serialiser emits by splatting a helper's return value, which the
#: attribute snapshot deliberately does not freeze -- `test_attribute_contract.py`
#: calls that a known gap in as many words. Without this the gate fails on CORRECT
#: prose: `docs/encryption-at-rest.md` and `docs/runbooks.md` already document this
#: set. The checkable half is the call itself: if the serialiser stops building its
#: attributes that way, the entry is stale and this says so.
_ENCRYPTION_STATUS = (
    "services/terrapod/api/routers/encryption.py",
    "get_encryption().status()",
)
HELPER_BUILT: dict[str, tuple[str, str]] = {
    "active_version": _ENCRYPTION_STATUS,
    "dek_versions": _ENCRYPTION_STATUS,
    "canary_ok": _ENCRYPTION_STATUS,
    "decryptable": _ENCRYPTION_STATUS,
}

#: Floors, so a matcher that stops matching FAILS rather than passing on nothing.
#: They bound total breakage only; SELF_TEST is what pins the individual spellings,
#: because a per-form count over the docs cannot distinguish a doc that stopped
#: using a form from a matcher that stopped reading it.
#: 20 against 31 on `main` and 25 on the oldest supported release line, where the
#: docs are smaller -- this file is carried unchanged to every branch, so the floor
#: has to clear the smallest of them with room for a page legitimately losing a
#: snippet. It is a backstop against the matcher breaking wholesale; SELF_TEST is
#: what pins each spelling.
MIN_REFERENCES = 20
MIN_ATTRIBUTE_NAMES = 400

#: The object whose field is being read. The prefix admits both `.attributes.x`
#: (preceded by a dot, as in `.data.attributes.x`) and the bare form the docs use
#: in running prose (`` `attributes.token` ``), while refusing a longer identifier
#: that merely ends in the word.
_OBJ = r"(?:(?<=\.)|(?<![\w.]))attributes"
#: One field, in each of jq's four spellings.
_FIELD = r"""(?:\.(?P<dot>[A-Za-z][\w-]*)|\."(?P<dq>[\w.-]+)"|\['(?P<sq>[\w.-]+)'\]|\["(?P<bq>[\w.-]+)"\])"""
REF = re.compile(_OBJ + _FIELD)
#: A single-line `.attributes | {a: ."x", b: ."y"}` rebinds the object, so the
#: field names carry no `attributes` prefix at all. Carrying that binding across
#: LINES would need a jq parser rather than a matcher, and is a documented gap.
PIPED = re.compile(_OBJ + r"\s*\|\s*\{(?P<body>[^}]*)\}")
PIPED_FIELD = re.compile(r"\.(?:\"(?P<dq>[\w.-]+)\"|(?P<dot>[A-Za-z][\w-]*))")
#: Trailing segments of a dotted reference, so a frozen leaf is read as the leaf.
TAIL = re.compile(r"((?:\.[A-Za-z][\w-]*)+)")

#: Literal spellings the matcher must read, with the names it must find. This is
#: the gate on the gate: narrowing a pattern fails here immediately, rather than
#: quietly shrinking what the scan can see.
SELF_TEST: list[tuple[str, list[str]]] = [
    (".attributes.capacity", ["capacity"]),
    ('.attributes["active-runs"]', ["active-runs"]),
    (".attributes['active-runs']", ["active-runs"]),
    ('.attributes."active-runs"', ["active-runs"]),
    (".attributes.capacity // 0", ["capacity"]),
    ("carried in `attributes.token`", ["token"]),
    ('.data.attributes."upload-url"', ["upload-url"]),
    ('{a: .attributes.name, b: .attributes."repo-url"}', ["name", "repo-url"]),
    (".attributes.actions.is-cancelable", ["actions.is-cancelable"]),
    (".attributes.actions.is-bogus", ["actions.is-bogus"]),
    (
        '.attributes | {e: ."vcs-last-error", at: ."vcs-last-error-at"}',
        ["vcs-last-error", "vcs-last-error-at"],
    ),
    # Must NOT match: a longer identifier ending in the word, and the object itself.
    ("the workspace_attributes.foo column", []),
    ("the `attributes` object", []),
]


def oracles() -> tuple[set[str], set[str]]:
    """The attribute names and the meta names, kept apart on purpose."""
    data = json.loads(CONTRACT.read_text())
    attributes: set[str] = set()
    metas: set[str] = set()
    for key, value in data.items():
        target = metas if key.endswith(":meta") else attributes
        target |= {v for v in value if isinstance(v, str)}
    return attributes, metas


def refs_in(text: str) -> list[tuple[str, int]]:
    """Every (name, offset) the docs tell a reader to read off `attributes`."""
    found: list[tuple[str, int]] = []
    for m in REF.finditer(text):
        name = m.group("dot") or m.group("dq") or m.group("sq") or m.group("bq")
        if m.group("dot"):
            tail = TAIL.match(text[m.end() :])
            if tail:
                name += tail.group(1)
        found.append((name, m.start()))
    for m in PIPED.finditer(text):
        for field in PIPED_FIELD.finditer(m.group("body")):
            found.append((field.group("dq") or field.group("dot"), m.start()))
    return found


def served(name: str, known: set[str]) -> bool:
    """Is this reference one the snapshot says we emit?

    Exact match aside, a DOTTED reference needs care in both directions, and the
    first version got it wrong in the permissive one. `api_attribute_contract.json`
    freezes nested objects leaf by leaf -- 68 of its names are dotted -- precisely
    because recording only the container was a real blind spot (#1344). So:

    * if the snapshot holds any name under `<container>.`, that container's leaves
      ARE frozen, and a reference to an unknown leaf under it is a defect.
      `.attributes.actions.is-bogus` is not served just because `actions` is.
    * if it holds the container and nothing beneath it, the depth is not frozen --
      the snapshot's own documented limit for a value built at runtime -- so the
      container is the most that can be checked and the reference passes.
    """
    if name in known:
        return True
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix not in known:
            continue
        frozen_below = prefix + "."
        return not any(k.startswith(frozen_below) for k in known)
    return False


def self_test() -> list[str]:
    out: list[str] = []
    for text, expected in SELF_TEST:
        got = sorted(name for name, _ in refs_in(text))
        if got != sorted(expected):
            out.append(
                f"the matcher no longer reads {text!r} the way it must: expected "
                f"{sorted(expected)}, read {got}. Widen the pattern rather than the "
                "fixture -- a spelling this gate cannot see is a spelling it does not "
                'check, and that is how it missed `.attributes."x"` the first time.'
            )
    return out


def ledger_problems() -> list[str]:
    """Check what is checkable about each ledger entry WITHOUT consulting it.

    The phrase floor and the document's existence are properties of the entry, so
    they hold on any branch. Whether that document mentions the field is checked
    at the point the exemption is actually USED (see `main`), because an unused
    entry has no reader to anchor: this gate runs on the release lines too, and
    there `docs/tfe-cli-surface.md` is a third the size, never mentions `comment`,
    and no doc references it -- so requiring the anchor unconditionally failed on
    two branches for an entry that is simply dormant there. An unused entry is
    reported in the summary line rather than as a problem.
    """
    out: list[str] = []
    for name, (where, phrase) in DOCUMENTED_AS_ABSENT.items():
        if len(phrase) < MIN_EXEMPTION_PHRASE:
            out.append(
                f"the exemption for `{name}` rests on the phrase {phrase!r}, too short to "
                f"mean anything (floor {MIN_EXEMPTION_PHRASE} characters). An exemption has "
                "to point at a passage, not at a common word."
            )
        if not (ROOT / where).is_file():
            out.append(f"the exemption for `{name}` names {where}, which does not exist.")
    for name, (where, call) in HELPER_BUILT.items():
        src = ROOT / where
        if not src.is_file() or call not in src.read_text(errors="ignore"):
            out.append(
                f"`{name}` is ledgered as built by `{call}` in {where}, and that call is no "
                "longer there. Either regenerate the attribute contract now that the "
                "serialiser names its own fields, or correct the ledger."
            )
    return out


def tracked_docs() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md", "llms.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / p for p in out.split("\0") if p]


def main() -> int:
    attributes, metas = oracles()

    # The gate's own health first: a broken matcher or an unread snapshot makes
    # every result below meaningless, so say so rather than reporting on nothing.
    blockers = self_test()
    if len(attributes) < MIN_ATTRIBUTE_NAMES:
        blockers.append(
            f"read {len(attributes)} attribute names from {CONTRACT.name}, below the floor "
            f"of {MIN_ATTRIBUTE_NAMES}. The snapshot reader has stopped reading."
        )
    if blockers:
        print("the docs-attribute gate cannot do its job:")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 1

    problems = ledger_problems()
    used: set[str] = set()
    seen = 0
    for path in tracked_docs():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(errors="ignore")
        for name, offset in refs_in(text):
            seen += 1
            if served(name, attributes) or name in HELPER_BUILT:
                continue
            exempt = DOCUMENTED_AS_ABSENT.get(name)
            if exempt is not None:
                where, phrase = exempt
                if rel == where and phrase in text and name in text:
                    used.add(name)
                    continue
                problems.append(
                    f"{rel}: `.attributes.{name}` is exempt only in {where}, and only while "
                    f"that document both says {phrase!r} and names the field — so a reader "
                    "is actually told it is absent. Not all of that holds here."
                )
                continue
            hint = (
                " It IS emitted under `meta` — the snippet is reading the wrong object."
                if served(name, metas)
                else ""
            )
            line = text[:offset].count("\n") + 1
            problems.append(
                f"{rel}:{line}: the docs tell a reader to read `.attributes.{name}`, "
                f"which NO serialiser emits.{hint}\n"
                "      Either fix the snippet to read a field the API returns, or add the "
                "field to the serialiser and regenerate the attribute contract. If the "
                "point is that it is deliberately absent, add it to DOCUMENTED_AS_ABSENT "
                "with the phrase that tells the reader so."
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

    dormant = sorted(set(DOCUMENTED_AS_ABSENT) - used)
    print(
        f"OK — {seen} `.attributes.*` reference(s) across the docs, all served by one of "
        f"{len(attributes)} snapshotted attributes; {len(used)} documented-as-absent "
        f"exemption(s) in use and {len(HELPER_BUILT)} helper-built. "
        f"{len(SELF_TEST)} matcher fixtures hold."
        + (f" Dormant on this branch: {dormant}." if dormant else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
