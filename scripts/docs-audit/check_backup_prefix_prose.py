#!/usr/bin/env python3
"""Every prefix `shared_prefix` refuses must be named where an operator reads about it.

`backup.prefix` is refused for two different reasons, and both lists are copied into
prose: the `Field` description in `services/terrapod/config.py` and the table in
`docs/disaster-recovery.md`. Neither copy had a gate, so a seventh workspace prefix or
a second age-swept one left both stale -- and a doc that lists five of six refused
prefixes is worse than one that lists none, because the omission reads as permission
(#2621).

The constants are read as TEXT rather than imported: `terrapod/storage/__init__.py`
pulls in `config`, and so SQLAlchemy, which the `python-checks` job does not install.
A construction this cannot read raises rather than being skipped -- a prefix list the
gate cannot see is one it cannot vouch for.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
KEYS = ROOT / "services" / "terrapod" / "storage" / "keys.py"
CONFIG = ROOT / "services" / "terrapod" / "config.py"
DOC = ROOT / "docs" / "disaster-recovery.md"

#: A floor on each list, so a matcher that stops matching fails rather than
#: vouching for nothing.
MIN_WORKSPACE = 6
MIN_SWEPT = 1


def _constants(source: str) -> tuple[list[str], dict[str, str]]:
    """`WORKSPACE_KEY_PREFIXES` and `AGE_SWEPT_PREFIXES`, resolved from keys.py's AST."""
    tree = ast.parse(source)
    names: dict[str, ast.expr] = {}
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        for t in targets:
            if isinstance(t, ast.Name) and node.value is not None:
                names[t.id] = node.value

    def literal(node: ast.expr) -> object:
        # Resolve a bare Name against the module's own assignments, so
        # `PULUMI_CHECKPOINT_PREFIX` inside the tuple is seen as its string.
        if isinstance(node, ast.Name) and node.id in names:
            return literal(names[node.id])
        if isinstance(node, ast.Tuple):
            return tuple(literal(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return {literal(k): literal(v) for k, v in zip(node.keys, node.values, strict=True)}
        return ast.literal_eval(node)

    for required in ("WORKSPACE_KEY_PREFIXES", "AGE_SWEPT_PREFIXES"):
        if required not in names:
            raise SystemExit(f"{KEYS}: cannot find {required} -- teach this reader its new shape")

    workspace = [str(p) for p in literal(names["WORKSPACE_KEY_PREFIXES"])]  # type: ignore[union-attr]
    swept = {str(k): str(v) for k, v in literal(names["AGE_SWEPT_PREFIXES"]).items()}  # type: ignore[union-attr]
    return workspace, swept


def _backup_prefix_description(source: str) -> str:
    """The `description=` of the `prefix` Field in the backup config block."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "prefix" or not isinstance(node.value, ast.Call):
            continue
        kwargs = {k.arg: k.value for k in node.value.keywords}
        default = kwargs.get("default")
        if not (isinstance(default, ast.Constant) and default.value == "backups/"):
            continue
        desc = kwargs.get("description")
        if desc is None:
            raise SystemExit(f"{CONFIG}: the backup prefix Field has no description=")
        try:
            return str(ast.literal_eval(desc))
        except ValueError:
            raise SystemExit(
                f"{CONFIG}: the backup prefix description is not a literal; "
                "this gate cannot read a computed one"
            ) from None
    raise SystemExit(f"{CONFIG}: cannot find the backup `prefix` Field -- teach this reader")


#: Each case is (text, named-prefixes) -- literal fixtures asserted BEFORE the real
#: scan, so narrowing a matcher fails here and names the spelling, instead of quietly
#: shrinking what is checked.
SELF_TEST: list[tuple[str, set[str]]] = [
    ("not under state/, plans/", {"state/", "plans/"}),
    ("names `state/` and `vcs_archives/` in backticks", {"state/", "vcs_archives/"}),
    ("| a prefix (`state/`, `runs/`) | why |", {"state/", "runs/"}),
    ("mentions statements/ and stateful/ but no real prefix", set()),
    ("bare state and runs without slashes", set()),
]


def named(text: str, candidates: list[str]) -> set[str]:
    """Which candidate prefixes the text names, matched WITH the trailing slash.

    The slash matters: `statements/` and `stateful/` both contain `state`, so a
    matcher on the bare word reports a doc as complete when it names neither.
    """
    return {p for p in candidates if re.search(rf"(?<![\w./]){re.escape(p)}", text)}


def main() -> int:
    workspace, swept = _constants(KEYS.read_text())
    if len(workspace) < MIN_WORKSPACE:
        raise SystemExit(
            f"only {len(workspace)} workspace prefixes read; expected >= {MIN_WORKSPACE}"
        )
    if len(swept) < MIN_SWEPT:
        raise SystemExit(f"only {len(swept)} age-swept prefixes read; expected >= {MIN_SWEPT}")

    every = sorted({*workspace, *swept})
    for text, expected in SELF_TEST:
        got = named(text, every)
        if got != expected:
            raise SystemExit(
                f"self-test failed on {text!r}: named {sorted(got)}, wanted {sorted(expected)}"
            )

    problems: list[str] = []

    # `state/pulumi-checkpoints/` is UNDER `state/`, so naming `state/` covers it;
    # requiring it by name would be a doc listing an implementation detail.
    wanted = {p for p in workspace if not any(p.startswith(q) and p != q for q in workspace)}
    wanted |= set(swept)

    desc = _backup_prefix_description(CONFIG.read_text())
    if missing := sorted(wanted - named(desc, every)):
        problems.append(
            f"{CONFIG.relative_to(ROOT)}: the backup.prefix description does not name {missing}"
        )

    doc = DOC.read_text()
    if missing := sorted(wanted - named(doc, every)):
        problems.append(f"{DOC.relative_to(ROOT)}: does not name {missing}")
    for setting in sorted(swept.values()):
        if setting not in doc:
            problems.append(
                f"{DOC.relative_to(ROOT)}: does not name the setting {setting!r} that sweeps an age-swept prefix"
            )

    if problems:
        for p in problems:
            print(f"  {p}")
        print(f"\n{len(problems)} problem(s). Every prefix `shared_prefix` refuses must be named")
        print("where an operator reads about it; an omission reads as permission.")
        return 1

    print(
        f"OK: {len(wanted)} refused prefix(es) named in "
        f"{CONFIG.relative_to(ROOT)} and {DOC.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
