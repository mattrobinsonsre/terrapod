#!/usr/bin/env python3
"""A backticked identifier of OURS, cited in a comment, must resolve to something.

The dangerous shape is a comment that tells a reader to use a helper, or names
the gate that protects an invariant, where the thing named does not exist. A
reader who checks then ends up WORSE off than one who does not: they grep, find
nothing, and either conclude the protection was dropped or fall back to the
thing the comment was warning them away from.

Both halves of that happened (#2715). Two cache models said "read it through
`storage_key_of`, never directly: that helper is the one place the NULL fallback
lives" -- and there is no `storage_key_of` anywhere, so a reader who fell back to
the raw column targeted the wrong object for any pre-#2599 row. And
`replication_registry.py` said "There is nothing to write here; the point is
that nothing is written here", then cited a guard class in a file that EXISTS and
contains no such class.

Fifteen of these were fixed by hand. On the tree as it stood before that, this
check finds five of them, including the replication one -- so it is worth a
third of that batch mechanically, which is why it exists.

**Scope, and why it is this narrow.** Measured before building, because a gate
whose findings are mostly false positives is one people learn to regenerate
without reading:

  - every backticked token: 1,755 candidates, 120 unresolved, and essentially
    ALL of those are legitimate external references -- `ANSIBLE_CONFIG`,
    `MissingGreenlet`, `ContainerCreating`, `oaud`, `waitForRun`,
    `StackReference`. Unusable.
  - restricted to tokens that can only be OURS -- a `test_*`/`Test*` name or a
    single-underscore private symbol -- 400 candidates and 20 unresolved.
  - minus citations of a FILE (`test_id_prefix_tolerance` is
    `test_id_prefix_tolerance.py`) and minus deliberate history, where a comment
    records what was deleted and why it is not a loss: 300 candidates, 8
    unresolved, 5 of them real.

The two remaining false-positive classes are excluded explicitly below rather
than tolerated, because three quarters noise is how a gate stops being read.

What this cannot catch: a citation of a name that resolves to something ELSE,
and a cited name in prose that is not in a comment. Both need reading, which is
what audit step N is for.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROOTS = (
    "services",
    "go-terrapod",
    "provider",
    "mcp",
    "pulumi-provider",
    "migrate",
    "publish",
    "query",
)

#: A backticked identifier, long enough not to be an English word in backticks.
_TOKEN = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,}(?:\(\))?)`")

#: A line that is a comment or the start of a docstring. Prose in a string
#: literal mid-expression is not reached, deliberately -- see the docstring.
_COMMENT_LINE = re.compile(r'^\s*(#|//|\*|""")')

#: Prose saying the thing named is GONE. A file that records what it deleted and
#: why each deletion is not a loss is doing something valuable, and every such
#: citation is correct. The window is wide because a docstring's framing sentence
#: can be twenty lines above the list it introduces.
_HISTORY = re.compile(
    r"\b(deleted|retired|removed|gone|used to|no longer|supersede[sd]?|"
    r"replaced by|renamed|dropped|not a loss|there is no|existed because)\b",
    re.I,
)
_HISTORY_WINDOW = 25

#: Third-party private attributes. A leading underscore usually means ours, and
#: these are the exceptions: each is another project's internal, named in a
#: comment that is explaining that project's behaviour. Keyed on the symbol with
#: the owner stated, so a new one is a conscious entry rather than a silent pass.
_FOREIGN_PRIVATES = {
    "_refresh_lock": "botocore's RefreshableCredentials attribute",
}

#: A floor on the citations examined, not on the findings. If the matcher stops
#: matching -- a comment style changes, a root moves -- this is what fails rather
#: than the check passing on nothing. The other direction is that the population
#: can only grow as prose is added, so a lower bound is what is meant here.
MIN_CITATIONS = 150


def _ours(token: str) -> bool:
    """Could this identifier only be one of ours?

    A `test_*`/`Test*` name always is. A single-underscore private symbol
    usually is -- `_FOREIGN_PRIVATES` holds the measured exceptions.
    """
    base = token.removesuffix("()")
    if base in _FOREIGN_PRIVATES:
        return False
    if base.startswith(("test_", "Test")):
        return True
    return base.startswith("_") and not base.startswith("__")


def _self_test() -> list[str]:
    """Literal fixtures, asserted BEFORE the scan.

    Each clause gets its own, because a fixture that only exercises the headline
    matcher passes while another clause silently filters the case out.
    """
    bad = []
    for good in ("`test_a_thing`", "`TestAThing`", "`_private_helper`", "`_helper()`"):
        if not _TOKEN.search(good):
            bad.append(f"the token matcher no longer sees {good}")
    for token, want in (
        ("test_a_thing", True),
        ("TestAThing", True),
        ("_private_helper", True),
        ("_refresh_lock", False),  # a measured foreign private
        ("__dunder__", False),  # a language protocol, never ours
        ("SomeExternalType", False),  # an external name without an underscore
    ):
        if _ours(token) is not want:
            bad.append(f"_ours({token!r}) should be {want}")
    for line in ("# a `_thing`", "// a `_thing`", '""" a `_thing`', "    #: a `_thing`"):
        if not _COMMENT_LINE.search(line):
            bad.append(f"the comment matcher no longer sees {line!r}")
    if _COMMENT_LINE.search("    value = compute()  # not a leading comment"):
        bad.append("the comment matcher now matches a trailing comment")
    for prose, want in (
        ("Specifically deleted, and why each is not a loss:", True),
        ("Supersedes `test_old_name` (#2305).", True),
        ("`_tp_host_width` raises it per play", False),
    ):
        if bool(_HISTORY.search(prose)) is not want:
            bad.append(f"_HISTORY on {prose[:40]!r} should be {want}")
    return bad


def _files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for rel in ROOTS:
        base = ROOT / rel
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.suffix not in (".py", ".go"):
                continue
            if "__pycache__" in f.parts or ".claude" in f.parts:
                continue
            out.append(f)
    return sorted(out)


def main() -> int:
    broken = _self_test()
    if broken:
        print("FAIL — this gate's own matchers are wrong before it scanned anything:")
        for b in broken:
            print(f"  {b}")
        return 1

    files = _files()
    corpus = "\n".join(f.read_text(errors="ignore") for f in files)
    stems = {f.stem for f in files}

    cited: dict[str, list[str]] = {}
    for f in files:
        lines = f.read_text(errors="ignore").split("\n")
        for n, line in enumerate(lines, 1):
            if not _COMMENT_LINE.search(line):
                continue
            window = "\n".join(lines[max(0, n - _HISTORY_WINDOW) : n + 5])
            if _HISTORY.search(window):
                continue
            for m in _TOKEN.finditer(line):
                token = m.group(1)
                if not _ours(token) or token.removesuffix("()") in stems:
                    continue
                cited.setdefault(token, []).append(f"{f.relative_to(ROOT)}:{n}")

    if len(cited) < MIN_CITATIONS:
        print(
            f"FAIL — only {len(cited)} cited identifiers found, below the floor of "
            f"{MIN_CITATIONS}. The matcher has probably stopped matching rather than "
            "the citations having gone away."
        )
        return 1

    dead = []
    for token, locs in sorted(cited.items()):
        base = token.removesuffix("()")
        total = len(re.findall(rf"(?<![A-Za-z0-9_]){re.escape(base)}(?![A-Za-z0-9_])", corpus))
        if total - corpus.count("`" + token + "`") <= 0:
            dead.append((token, locs))

    if dead:
        print(f"FAIL — {len(dead)} comment(s) cite an identifier of ours that does not exist:")
        for token, locs in dead:
            print(f"\n  `{token}` — named at {', '.join(locs)}")
            print("      but it occurs nowhere outside backticks. Cite the real symbol, or")
            print("      say the thing is gone (this check skips a citation whose prose")
            print("      records a deletion) -- a comment naming a helper or a gate that")
            print("      does not exist leaves a reader who CHECKS worse off than one who")
            print("      does not.")
        return 1

    print(f"PASS — {len(cited)} cited identifier(s) of ours, all resolving.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
