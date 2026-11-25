#!/usr/bin/env python3
"""The same function body, deciding the same thing, in three or more modules.

Duplication is the one defect class a gate structurally cannot catch in general,
because two copies of a rule both work and every test passes. This catches the
mechanical corner of it: a body that is byte-identical after comments,
docstrings and whitespace are stripped. That is not a judgement about whether
two things ARE one rule -- it is the case where they demonstrably are.

It is worth having because it is what the timestamp defect looked like. Twenty
copies of `_rfc3339` were identical and seven more had drifted into four shapes,
two of which emitted no `Z` at all (#2715). On the tree as it stood then this
check reports nine groups; the drift was in a tenth that this cannot see,
because divergent bodies are not identical -- so the pairing is: this gate for
the copies, audit step N for the drift.

**A DELEGATION is excluded, and that distinction is the gate.** After the
timestamp fold there are twenty-five modules whose `_rfc3339` is one line handing
off to `serialization.rfc3339_or_empty`. A naive version of this check flagged
that -- the count went UP, from twenty identical formatters to twenty-five
identical delegations -- which would have reported the fix as the defect. N copies
that delegate cannot disagree, because they all funnel to one producer; N copies
that each DECIDE can. So a body whose only statement returns a call is not a
finding, and step N's own catalogue says the same thing in words: "a tree walker
computed twice cannot disagree about anything, where a predicate or a limit can".

**The ledger records growth, not just newness.** Each known group carries its
module count, so a tenth copy of an eight-copy helper fails even though the
group is already listed. A hand-kept list that only says "we know about this
name" would be the ledger shape AGENTS.md forbids.
"""

from __future__ import annotations

import ast
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCOPE = ROOT / "services" / "terrapod"

#: Known identical-body groups: (function name, module count) -> why it stands.
#: A count that no longer matches fails, in EITHER direction -- a new copy is a
#: finding, and a consolidation should shrink this entry rather than leave it
#: overstating the duplication.
KNOWN: dict[tuple[str, int], str] = {
    ("_resolve_tmpdir", 8): (
        "the PVC-tempfile resolver (architecture rule 14). The public one lives "
        "under `api/`, so a `services/` -> `api/` import would be a layering "
        "inversion; consolidating needs an owner chosen first, not a mechanical move"
    ),
    ("_resolve_tmpdir", 3): "same resolver, a second body variant",
    ("_resolve_ephemeral_tmpdir", 3): "same resolver under its other name",
    (
        "fetch_custom_roles",
        4,
    ): "per-axis RBAC role fetch; the axes differ in their caller, not here",
    ("_require_ws_capability", 4): "per-router capability gate, one per router by design",
    ("_redis", 3): "a two-line client accessor",
    ("_get_workspace", 3): "per-router workspace load",
}

#: A floor on the bodies EXAMINED, not on the findings: if the walker stops
#: walking, this fails rather than the check passing on nothing. The population
#: only grows as modules are added, so a lower bound is what is meant here.
MIN_BODIES = 1500


def _normalise(seg: str) -> str:
    """A body with comments, docstrings and whitespace removed."""
    seg = re.sub(r'""".*?"""', "", seg, flags=re.S)
    seg = re.sub(r"#.*", "", seg)
    return re.sub(r"\s+", "", seg)


def _is_delegation(fn: ast.AST) -> bool:
    """Does this body do nothing but hand off to one callable?"""
    body = [
        n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return False
    v = body[0].value
    v = v.value if isinstance(v, ast.Await) else v
    return isinstance(v, ast.Call)


def _self_test() -> list[str]:
    """Literal fixtures, asserted BEFORE the scan, one per clause."""
    bad = []
    cases = [
        ("def f():\n    return g(x)\n", True, "a bare delegation"),
        ("def f():\n    return await g(x)\n", True, "an awaited delegation"),
        ('def f():\n    """Doc."""\n    return g(x)\n', True, "a delegation with a docstring"),
        (
            "def f():\n    if x:\n        return 1\n    return g(x)\n",
            False,
            "a branch, then a call",
        ),
        ("def f():\n    return x + 1\n", False, "an expression, not a call"),
        ("def f():\n    y = g(x)\n    return y\n", False, "a call bound then returned"),
    ]
    for src, want, label in cases:
        fn = ast.parse(src).body[0]
        if _is_delegation(fn) is not want:
            bad.append(f"_is_delegation on {label} should be {want}")
    if _normalise("def f():\n  # c\n  return 1\n") != _normalise("def f():\n    return 1\n"):
        bad.append("normalisation is sensitive to comments or whitespace")
    if _normalise('def f():\n    """A."""\n    return 1\n') == _normalise(
        "def f():\n    return 2\n"
    ):
        bad.append("normalisation collapses two genuinely different bodies")
    return bad


def main() -> int:
    broken = _self_test()
    if broken:
        print("FAIL — this gate's own matchers are wrong before it scanned anything:")
        for b in broken:
            print(f"  {b}")
        return 1

    groups: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    examined = 0
    for path in sorted(SCOPE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        src = path.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            examined += 1
            if _is_delegation(node):
                continue
            seg = ast.get_source_segment(src, node) or ""
            groups[(node.name, _normalise(seg))].append(str(path.relative_to(SCOPE)))

    if examined < MIN_BODIES:
        print(
            f"FAIL — only {examined} function bodies examined, below the floor of "
            f"{MIN_BODIES}. The walker has probably stopped walking."
        )
        return 1

    found = collections.Counter()
    where: dict[tuple[str, int], list[str]] = {}
    for (name, _), mods in groups.items():
        if len(mods) >= 3:
            found[(name, len(mods))] += 1
            where[(name, len(mods))] = sorted(mods)

    problems = []
    for key in sorted(found):
        if key not in KNOWN:
            name, n = key
            listed = [k for k in KNOWN if k[0] == name]
            hint = (
                f" This name is listed at {[k[1] for k in listed]} module(s), so it has GROWN."
                if listed
                else ""
            )
            problems.append(
                f"{name}: {n} modules share one identical deciding body.{hint}\n"
                f"      {', '.join(where[key])}"
            )
    for key in sorted(KNOWN):
        if key not in found:
            problems.append(
                f"{key[0]}: no longer has {key[1]} identical copies. If it was "
                f"consolidated, remove or correct this entry -- a ledger that "
                f"overstates the duplication is a ledger nobody trusts."
            )

    if problems:
        print(f"FAIL — {len(problems)} identical-body problem(s):")
        for p in problems:
            print(f"\n    {p}")
        print(
            "\n  N copies that each DECIDE can disagree; N copies that DELEGATE to one "
            "\n  producer cannot, and are not reported. Either point them at one owner, "
            "\n  or add an entry saying why they stand."
        )
        return 1

    print(
        f"PASS — {examined} bodies examined, {len(found)} identical-body group(s), "
        "all accounted for."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
