#!/usr/bin/env python3
"""Every numeric floor in a gate is inventoried, and new ones must justify
themselves.

**Why this exists.** A retrospective over 230 fix items found that the largest
single defect class -- 24 of them -- was a gate that could not fail, or measured
something other than what its name claimed. Every instance had one shape: a set
grew and the gate's subject did not. The provider gained data sources and the
committed-schema gate walked `resources` only; two Go modules landed and a
coverage floor stayed at 7; theme pairings moved into `*.ts` and the contrast
gate globbed `*.tsx`.

**The structural reason, measured.** This tree carries roughly a hundred
lower-bound floors and seventy named `MIN_*` constants against the best part of a
thousand equality assertions. A `>=` floor catches a matcher that stopped
matching -- which is exactly what it is for, and it should keep doing that -- and
is *structurally blind* to the set growing. `len(x) >= 7` is as true at nine as
at seven, which is how two modules came to be audited by nothing.

**What this gate does, and deliberately does not do.** It cannot compute the live
count behind an arbitrary floor without executing the gate that owns it, so it
does not try. Instead it does the two things that ARE mechanical:

1. **Inventory** every floor, so the set is a derived artefact rather than
   something someone remembers. `--report` prints it.
2. **Ratchet.** The floors present when this gate landed are recorded in
   `gate_floors_baseline.json`. A NEW floor must either be an equality against a
   derived count, or carry a comment on the line above saying what protects the
   other direction. Following the house pattern from the i18n hardcoded-literal
   gate: the existing population is grandfathered, and the population cannot
   grow without a reason being written down.

The baseline is deliberately NOT a count. A count would be the very defect this
gate is about: it would stay true while the set changed underneath it. It is the
set of locations, so a floor that moves or is added is visible, and one that is
removed simply drops out.

Regenerate the baseline with `UPDATE_GATE_FLOORS=1`, which is a conscious act
and should be reviewed like a snapshot regen: the delta must be an addition you
meant, and each addition must carry its justification comment.

Run from the repository root.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = pathlib.Path(__file__).with_name("gate_floors_baseline.json")

#: Where gates live. Derived from the tree inside each, never a file list.
GATE_ROOTS = (
    "services/tests",
    "scripts/docs-audit",
    "go-terrapod",
    "provider",
    "pulumi-provider",
    "mcp",
    "query",
    "web/tests",
)

SUFFIXES = {".py", ".go", ".ts", ".mjs", ".tsx"}

#: A floor is a count compared against an integer literal with a LOWER-BOUND
#: operator **in an ASSERTION context**. The context clause is the whole of the
#: matcher's correctness: the first version of this gate matched any
#: `len(x) > 0` and reported 320 hits across 201 files, almost all of them
#: ordinary nil guards in production code. That is not a coverage floor -- it is
#: a non-empty check -- and a gate whose findings are overwhelmingly false
#: positives is one people learn to regenerate without reading.
#:
#: So: the comparison must sit inside an assert, a test failure, or an explicit
#: refusal. And `> 0` is excluded outright, because "this is not empty" is never
#: a claim about coverage -- the floor this gate cares about is `>= N` for N
#: meaningfully above zero, which is someone asserting "I checked at least this
#: many".
#:
#: Equality is deliberately NOT a floor. It is the thing we want: it fails when
#: the set grows, which is the point.
_ASSERTION = r"assert(?:\.\w+)?\s|require\.|t\.(?:Fatal|Error)|expect\(|SystemExit|sys\.exit|print\(\s*[\"\']FAIL"

#: What the floor is ON. `len(...)`, `.length` and `count` are coverage floors
#: over a collection. An UPPER_CASE name is a tuning CONSTANT, and a floor on one
#: with no ceiling is the same defect in a different shape -- which this matcher
#: could not see, so `TERMINAL_UPLOAD_GRACE_SECONDS >= 60` was never in the
#: baseline at all and the credential reprieve had a floor and no upper bound for
#: as long as it has existed (#2715). That is this gate's own subject having been
#: narrower than its name, which is the class it exists to catch.
#:
#: Measured before widening: `len`/`length`/`count` alone matches 122, adding
#: UPPER_CASE constants takes it to 215, and widening further to ANY bare
#: identifier takes it to 216 -- one more. So almost everything a looser matcher
#: would add is a constant anyway, and the targeted form carries none of the risk
#: of matching arbitrary numeric comparisons.
_FLOOR_HEAD = r"\blen\s*\(|\.length\b|\bcount\b|\b[A-Z][A-Z0-9_]{3,}\b"

_FLOOR = re.compile(
    r"""(?:(?P<assertion>"""
    + _ASSERTION
    + r""")[^\n]*?)
        (?P<head>"""
    + _FLOOR_HEAD
    + r""")
        [^\n]*?(?P<op>>=|>)\s*(?P<n>\d+)""",
    re.VERBOSE | re.IGNORECASE,
)

#: A bare `if <count> < N:` followed by a refusal on the next line or two is the
#: same claim written the other way round, and the docs-audit scripts use it.
_FLOOR_REFUSAL = re.compile(
    r"""(?:\blen\s*\(|\.length\b|\bcount\b)[^\n]*?<\s*(?P<n>\d+)""",
    re.VERBOSE | re.IGNORECASE,
)

#: A named floor constant. `MIN_PINS = 10`, `_MIN_COLUMNS = 800`.
_MIN_CONST = re.compile(r"^\s*(?P<name>_?MIN_[A-Z0-9_]+)\s*(?::[^=]+)?=\s*(?P<n>\d+)\s*$", re.M)

#: A comment saying this `>=` is not a coverage floor at all -- a key length, a
#: bounds check, a nil guard. Out of the gate's scope entirely, which is a
#: DIFFERENT claim from a floor whose other direction is protected, below.
_NOT_A_FLOOR = re.compile(
    r"not a (coverage )?floor|key length|bounds check|nil guard|length check",
    re.I,
)

#: What counts as a written justification on the line(s) above a floor. The
#: point is not the exact wording -- it is that somebody said what the other
#: direction is protected by, because that is the question a floor cannot answer.
#:
#: These two patterns used to be one, and merging them was an escape hatch: the
#: single pattern also accepted `floor`, `lower bound`, `at least` and `sanity`,
#: so `assert len(x) >= 7  # sanity floor` was treated as justified AND dropped
#: from the inventory, which means the ratchet never saw it. Those four words are
#: self-referential -- they are what any comment ABOUT a floor says, and none of
#: them names the other direction. The legitimate case they were there to rescue
#: is the one `_NOT_A_FLOOR` now owns: `# a key length, not a coverage floor` is
#: out of scope, rather than an unprotected floor that someone excused.
_JUSTIFIED = re.compile(
    r"other direction|grows?\b|equality|exact count|derived|cannot shrink",
    re.I,
)


#: Literal fixtures, asserted BEFORE the scan. This gate's own first version
#: reported 320 hits across 201 files because its matcher had no context clause,
#: and a coverage floor over the tree cannot tell you that -- a per-file count
#: shrinking is indistinguishable from the docs genuinely losing a floor. So the
#: forms are pinned here, in both directions, and narrowing the matcher fails by
#: name rather than quietly shrinking what is checked.
_MUST_MATCH = (
    "assert len(_STORAGE_LOCATION_CONSUMERS) >= 6",
    'assert len(calls) >= 2, f"found only {len(calls)}"',
    "assert len(pinned) >= 10",
    'if len(resources) < 20 {\n\t\tt.Fatalf("only %d", len(resources))',
)
_MUST_NOT_MATCH = (
    "if len(parts) > 0 {",  # a nil guard, not coverage
    "if len(l) > 0 && err == nil {",  # ditto
    "assert len(found) == 3",  # equality is what we WANT
    'if len(id) > 4 && id[:4] == "run-" {',  # a bounds check
    "assert len(raw) >= 32  # a key length, not a coverage floor",  # out of scope
)

#: Comments that must NOT count as a justification. Each is a real phrasing that
#: reads as considered and says nothing about what stops the set growing, so a
#: floor carrying one is exactly what this gate exists to surface.
_UNJUSTIFIED_COMMENTS = (
    "# sanity floor",
    "# a lower bound",
    "# at least this many",
    "# sanity check",
)


def _matches(block: str) -> bool:
    """Would the scan flag this snippet? Mirrors `collect`'s own decision."""
    lines = block.split("\n")
    first = lines[0]
    m = _FLOOR.search(first)
    if m and m.group("n") != "0" and not _justified(lines, 0):
        return True
    r = _FLOOR_REFUSAL.search(first)
    if r and r.group("n") != "0" and not _justified(lines, 0):
        nxt = " ".join(lines[1:3])
        if re.search(_ASSERTION + r"|return 1|raise\b", nxt):
            return True
    return False


def _self_test() -> list[str]:
    """Fails by naming the spelling, never by a count going quietly down."""
    bad = []
    for s in _MUST_MATCH:
        if not _matches(s):
            bad.append(f"matcher no longer recognises a real floor: {s.splitlines()[0][:70]}")
    for s in _MUST_NOT_MATCH:
        if _matches(s):
            bad.append(f"matcher now flags a non-floor: {s.splitlines()[0][:70]}")
    for c in _UNJUSTIFIED_COMMENTS:
        if _justified([c, "assert len(mods) >= 7"], 1):
            bad.append(f"self-referential comment now counts as justified: {c}")
    if not _justified(["# the other direction is an equality below", "assert len(x) >= 3"], 1):
        bad.append("an honest justification is no longer accepted")
    if not _justified(["# a key length, not a coverage floor", "assert len(raw) >= 32"], 1):
        bad.append("an out-of-scope length check is no longer excluded")
    return bad


def _files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for rel in GATE_ROOTS:
        base = ROOT / rel
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.suffix not in SUFFIXES:
                continue
            if "__pycache__" in f.parts or "node_modules" in f.parts:
                continue
            out.append(f)
    return sorted(out)


#: Only COMMENT text can justify a floor. Found by mutation: the first version
#: searched whole lines, so a test named `test_planted_floor_for_...` justified
#: itself -- the word "floor" in an identifier was enough. That is a silent
#: false negative in the direction that matters, since a floor someone names
#: after the concept is exactly the one they have not reasoned about.
_COMMENT = re.compile(r"(?:#|//|/\*|\*)\s*(?P<text>.*)$")


def _justified(lines: list[str], idx: int) -> bool:
    """Is there a justification in a COMMENT on, or just above, this line?"""
    for j in (idx - 2, idx - 1, idx):
        if j < 0 or j >= len(lines):
            continue
        c = _COMMENT.search(lines[j])
        if c and (_JUSTIFIED.search(c.group("text")) or _NOT_A_FLOOR.search(c.group("text"))):
            return True
    return False


def collect() -> dict[str, list[str]]:
    """Every floor in the tree, as {relative path: [sorted location keys]}.

    A location key is `"<line text, squeezed>"` rather than a line NUMBER,
    deliberately: a line number changes whenever anything above it moves, which
    would make the baseline churn on every unrelated edit and train people to
    regenerate it without reading.
    """
    found: dict[str, list[str]] = {}
    for f in _files():
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(f.relative_to(ROOT))
        lines = text.split("\n")
        keys: set[str] = set()
        for n, line in enumerate(lines):
            m = _FLOOR.search(line)
            if m and m.group("n") != "0" and not _justified(lines, n):
                keys.add(" ".join(line.split()))
                continue
            # `if len(x) < N: <refuse>` -- the same claim inverted. Require the
            # refusal within two lines, or an ordinary bounds check matches.
            r = _FLOOR_REFUSAL.search(line)
            if r and r.group("n") != "0" and not _justified(lines, n):
                nxt = " ".join(lines[n + 1 : n + 3])
                if re.search(_ASSERTION + r"|return 1|raise\b", nxt):
                    keys.add(" ".join(line.split()))
        for m in _MIN_CONST.finditer(text):
            n = text[: m.start()].count("\n")
            if not _justified(lines, n):
                keys.add(" ".join(lines[n].split()))
        if keys:
            found[rel] = sorted(keys)
    return found


def main() -> int:
    broken = _self_test()
    if broken:
        print("FAIL — this gate's own matcher is wrong before it scanned anything:\n")
        for b in broken:
            print(f"  {b}")
        return 1

    found = collect()
    total = sum(len(v) for v in found.values())

    if "--report" in sys.argv:
        print(f"{total} unjustified floor(s) across {len(found)} file(s):\n")
        for rel in sorted(found):
            print(f"  {rel}")
            for k in found[rel]:
                print(f"      {k[:116]}")
        return 0

    if os.environ.get("UPDATE_GATE_FLOORS"):
        BASELINE.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
        print(f"baseline regenerated: {total} floor(s) across {len(found)} file(s)")
        return 0

    if not BASELINE.exists():
        print("FAIL — no baseline. Create one with UPDATE_GATE_FLOORS=1 and review it.")
        return 1

    base = json.loads(BASELINE.read_text())

    # A floor that is in the tree and not in the baseline is new and unjustified.
    new: list[tuple[str, str]] = []
    for rel, keys in found.items():
        known = set(base.get(rel, []))
        for k in keys:
            if k not in known:
                new.append((rel, k))

    # The floor set must not be EMPTY -- that would mean the matcher stopped
    # matching, which is the very failure this gate is about, applied to itself.
    if total == 0:
        print(
            "FAIL — found no floors at all. Either every one was justified (unlikely) "
            "or this gate's own matcher has stopped matching, which is the defect it "
            "exists to catch. Check `--report` before regenerating anything."
        )
        return 1

    if new:
        print(f"FAIL — {len(new)} new floor(s) with no justification:\n")
        for rel, k in sorted(new):
            print(f"  {rel}")
            print(f"      {k[:116]}")
        print(
            "\nA `>=` floor catches a matcher that stopped matching and is blind to the\n"
            "set GROWING. So either:\n"
            "  - make it an equality against a count derived from the tree; or\n"
            "  - say on the line above what protects the other direction, and why a\n"
            "    lower bound is what you mean here.\n"
            "Then regenerate with UPDATE_GATE_FLOORS=1."
        )
        return 1

    gone = sum(1 for rel, keys in base.items() for k in keys if k not in set(found.get(rel, [])))
    extra = (
        f", {gone} baseline entr{'y' if gone == 1 else 'ies'} now justified or removed"
        if gone
        else ""
    )
    print(f"PASS — {total} floor(s) inventoried, none new{extra}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
