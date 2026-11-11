"""Migration reversibility + expand/contract contract gate (#550).

Two zero-downtime-upgrade hazards that no API/attribute contract catches:

1. **Reversibility** — every Alembic migration must have a real `upgrade()` AND
   `downgrade()` (not a stub), so a bad release can be rolled back. This is a
   stated project invariant; this test enforces it.

2. **Expand/contract** — during a rolling upgrade, old and new API replicas run
   against the SAME database at the same time. A migration that DROPS or RETYPES
   a column/table in `upgrade()` breaks the *old* replica still serving traffic,
   the moment the migration Job runs. Such contractions are only safe when the
   column has already been unused for a prior release (expand → migrate → wait a
   release → contract). This test ledgers every `upgrade()`-side contraction so a
   NEW one fails CI until it's consciously acknowledged (i.e. you confirm it
   followed expand/contract), rather than slipping in silently.

Regenerate the contraction ledger after an intentional, expand/contract-safe
contraction:

    UPDATE_API_CONTRACT=1 pytest tests/db/test_migration_contract.py
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

_LEDGER = Path(__file__).parent / "migration_contractions.json"
_CONTRACTION_OPS = frozenset({"drop_column", "drop_table", "drop_constraint", "alter_column"})


def _versions_dir() -> Path:
    for base in (
        Path("/app/alembic/versions"),  # test image
        Path(__file__).resolve().parents[3] / "alembic" / "versions",  # repo
    ):
        if base.is_dir():
            return base
    raise AssertionError("Could not locate alembic/versions")


def _migration_files() -> list[Path]:
    return sorted(p for p in _versions_dir().glob("*.py") if p.name != "__init__.py")


def _func(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _is_real_body(fn: ast.FunctionDef | None) -> bool:
    """A migration hook is 'real' if it does more than a docstring / pass / a bare
    NotImplementedError raise."""
    if fn is None:
        return False
    stmts = list(fn.body)
    # drop a leading docstring
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
        stmts = stmts[1:]
    if not stmts:
        return False
    if len(stmts) == 1 and isinstance(stmts[0], ast.Pass | ast.Raise):
        return False
    return True


def _op_signature(call: ast.Call) -> str | None:
    """`op.<contraction>(...)` → a stable signature from its string-literal args."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in _CONTRACTION_OPS):
        return None
    args = [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return f"{func.attr}({', '.join(args)})"


def _upgrade_contractions(tree: ast.Module) -> set[str]:
    """Contraction ops that run in `upgrade()` only (a drop in `downgrade()` is a
    normal reversal of a create and is NOT a contraction)."""
    up = _func(tree, "upgrade")
    if up is None:
        return set()
    out: set[str] = set()
    for node in ast.walk(up):
        if isinstance(node, ast.Call):
            sig = _op_signature(node)
            if sig:
                out.add(sig)
    return out


# The migration that creates `schema_contractions` (#2500). A migration at or
# before it cannot record anything, because the table does not exist yet; one
# after it can, and must.
_MARKER_REVISION = "f9bb1c338a1c"

# Of the four contraction ops, these are the two that remove something the old
# code may still be READING, which is what makes an ahead-of-code pod unsafe.
# `drop_constraint` and `alter_column` leave every column in place and readable,
# so they are ledgered (above) without needing the runtime marker.
_BREAKING_OPS = frozenset({"drop_column", "drop_table"})


def _revision_ids(tree: ast.Module) -> tuple[str | None, str | None]:
    """A migration's own revision and its parent, read exactly rather than
    matched. (A regex over these lines manufactures false roots and heads when a
    value is not on one line in the shape the pattern assumed.)"""
    found: dict[str, str | None] = {}
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        value = getattr(node, "value", None)
        for name in names:
            if name in ("revision", "down_revision"):
                found[name] = (
                    value.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else None
                )
    return found.get("revision"), found.get("down_revision")


def _descendants_of(marker: str, parents: dict[str, str | None]) -> set[str]:
    """Every revision whose ancestry passes through `marker`, exclusive."""
    children: dict[str, list[str]] = {}
    for rev, parent in parents.items():
        if parent:
            children.setdefault(parent, []).append(rev)
    out: set[str] = set()
    queue = list(children.get(marker, []))
    while queue:
        rev = queue.pop()
        if rev in out:
            continue
        out.add(rev)
        queue.extend(children.get(rev, []))
    return out


def test_a_breaking_contraction_records_itself_in_the_marker_table() -> None:
    """A contraction that drops a column or table must tell old pods (#2500).

    An API pod stays READY when the schema is ahead of its code, because an
    additive migration leaves it working. A dropped column does not, and the pod
    cannot know that by inspecting a revision it has never heard of -- so the
    migration writes its own revision into `schema_contractions` and the pod
    refuses readiness on finding one outside its own revision graph.

    Derived from the revision graph rather than from a second ledger: the
    requirement applies to exactly those migrations that descend from the one
    creating the table, which is self-maintaining and needs no grandfather list
    for the contractions that shipped before it.
    """
    trees = {path: ast.parse(path.read_text(), filename=str(path)) for path in _migration_files()}
    parents: dict[str, str | None] = {}
    by_revision: dict[str, Path] = {}
    for path, tree in trees.items():
        rev, parent = _revision_ids(tree)
        if rev:
            parents[rev] = parent
            by_revision[rev] = path

    assert _MARKER_REVISION in by_revision, (
        f"the marker migration {_MARKER_REVISION} is missing; this gate has nothing to "
        "anchor on and would pass on every tree"
    )
    subject = _descendants_of(_MARKER_REVISION, parents)

    offenders: list[str] = []
    for rev in sorted(subject):
        path = by_revision[rev]
        up = _func(trees[path], "upgrade")
        if up is None:
            continue
        breaking = {
            sig
            for sig in _upgrade_contractions(trees[path])
            if sig.split("(", 1)[0] in _BREAKING_OPS
        }
        if not breaking:
            continue
        records = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and any(
                isinstance(a, ast.Constant)
                and isinstance(a.value, str)
                and "schema_contractions" in a.value
                for a in node.args
            )
            for node in ast.walk(up)
        )
        if not records:
            offenders.append(f"{path.name} ({rev}): {', '.join(sorted(breaking))}")

    assert not offenders, (
        "A migration drops a column or table without recording itself in "
        "`schema_contractions`. An API pod from the previous release stays READY "
        "against an ahead-of-code schema (#2500) and would read a column that is "
        "now gone. Add to its `upgrade()`:\n\n"
        "    op.execute(\n"
        '        "INSERT INTO schema_contractions (revision, note) "\n'
        "        \"VALUES ('<this revision>', '<what it drops and why it is safe>') \"\n"
        '        "ON CONFLICT (revision) DO NOTHING"\n'
        "    )\n\n"
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_every_migration_is_reversible() -> None:
    offenders: list[str] = []
    for path in _migration_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        if not _is_real_body(_func(tree, "upgrade")):
            offenders.append(f"{path.name}: upgrade() is missing or a stub")
        if not _is_real_body(_func(tree, "downgrade")):
            offenders.append(f"{path.name}: downgrade() is missing or a stub")
    assert not offenders, (
        "Every migration must have a real upgrade() AND downgrade() (reversibility "
        "is a hard invariant). Stubs found:\n  " + "\n  ".join(offenders)
    )


def _current_contractions() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in _migration_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        sigs = _upgrade_contractions(tree)
        if sigs:
            out[path.stem] = sorted(sigs)
    return out


def test_upgrade_contractions_are_acknowledged() -> None:
    current = _current_contractions()

    if os.environ.get("UPDATE_API_CONTRACT"):
        _LEDGER.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        return

    assert _LEDGER.exists(), (
        f"Contraction ledger missing at {_LEDGER}. Generate it with:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/db/test_migration_contract.py"
    )
    ledger: dict[str, list[str]] = json.loads(_LEDGER.read_text())

    new: list[str] = []
    for revision, sigs in current.items():
        unacked = sorted(set(sigs) - set(ledger.get(revision, [])))
        for sig in unacked:
            new.append(f"{revision}: {sig}")

    assert not new, (
        "New schema CONTRACTION(s) in a migration `upgrade()`. During a rolling "
        "upgrade the OLD API replica runs against this new schema — dropping or "
        "retyping a column/table it still reads breaks it. Only acknowledge this "
        "if the column/table has been UNUSED for a prior release (expand → migrate "
        "→ wait a release → contract). If so, regenerate the ledger:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/db/test_migration_contract.py\n"
        "Unacknowledged contractions:\n  " + "\n  ".join(new)
    )


def test_ledger_and_scan_are_nonempty() -> None:
    # Bite-check: prove the scanner resolves migrations + finds real contractions,
    # so an empty ledger can't silently disable the gate.
    assert len(_migration_files()) > 40
    assert _current_contractions(), "expected at least one upgrade() contraction historically"
