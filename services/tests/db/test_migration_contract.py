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
