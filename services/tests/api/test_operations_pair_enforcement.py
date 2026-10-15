"""Every path that gives a row the operations pair accounts for it (#1966).

The gap this exists to close was real and shipped inside this very change. The
both-false invariant was added at the workspace create and PATCH routes, and
that read as "enforced" -- it is a validator with its own unit tests and two
route-driven tests either side of it. It was enforced at two of the THREE write
boundaries. The autodiscovery rule accepted a rule declaring neither
operation, and `find_or_autocreate_workspace` builds its `Workspace` directly
from the rule's fields rather than through the create route, so such a rule
materialised an inert workspace on every scan, one per matched directory --
with nothing refusing it anywhere.

That is the recorded lesson twice over: a presence check is not an application
check, and when the answer to "is X enforced" is "yes, at every call site", the
finding is the difference between the number of call sites and the number a
test covers.

So this gate is DERIVED rather than listed. It finds every construction of a
`Workspace` or `AutodiscoveryRule` anywhere under `terrapod/` and requires each
one to account for the pair in one of exactly two ways:

  1. the enclosing function calls `validate_operations` -- it is a write
     boundary and it checks; or
  2. both columns are COPIED from another row (`plan_apply_enabled=rule.
     plan_apply_enabled`) -- it inherits a decision already validated upstream,
     which is what materialisation does and why it needs no check of its own.

Anything else -- a literal, a computed value, one copied and one invented -- is
a new way for an unvalidated pair to reach the database, and fails here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "terrapod"
_PAIR = ("plan_apply_enabled", "configure_enabled")

#: A floor, so a rename cannot make this gate pass by matching nothing. Three
#: today: the workspace create route, the rule's transient preview object, and
#: autodiscovery materialisation.
MIN_CONSTRUCTIONS = 3


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """node -> the function that contains it."""
    owner: dict[ast.AST, ast.AST] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(fn):
                owner.setdefault(child, fn)
    return owner


def _constructions() -> list[tuple[str, str, ast.Call, ast.AST | None]]:
    """(path, class, call, enclosing function) for every Workspace/Rule build."""
    found: list[tuple[str, str, ast.Call, ast.AST | None]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("Workspace", "AutodiscoveryRule"):
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            if not any(c in kwargs for c in _PAIR):
                continue
            found.append((str(path.relative_to(_SRC.parent)), name, node, owner.get(node)))
    return found


class TestEveryConstructionAccountsForThePair:
    def test_at_least_the_known_constructions_are_found(self):
        """A floor, because a gate that matches nothing passes loudest."""
        found = _constructions()
        assert len(found) >= MIN_CONSTRUCTIONS, (
            f"found only {len(found)} construction(s) carrying the operations pair; "
            f"expected at least {MIN_CONSTRUCTIONS}. If the pair was renamed, this gate "
            "is now inspecting nothing -- update _PAIR rather than lowering the floor."
        )

    def test_each_one_validates_or_copies(self):
        problems: list[str] = []
        for path, cls, call, fn in _constructions():
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            missing = [c for c in _PAIR if c not in kw]
            if missing:
                problems.append(
                    f"{path}: {cls}(...) sets {[c for c in _PAIR if c in kw]} but not "
                    f"{missing} -- one operation declared and the other left to the "
                    "column default is how a half-copied pair reaches a row"
                )
                continue

            # (2) copied wholesale from another row?
            copied = all(isinstance(kw[c], ast.Attribute) and kw[c].attr == c for c in _PAIR)
            if copied:
                continue

            # (1) the enclosing function validates?
            validates = (
                fn is not None
                and any(
                    getattr(n.func, "attr", None) == "validate_operations"
                    or getattr(n.func, "id", None) == "validate_operations"
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                )
                or (fn is not None and "validate_operations" in ast.dump(fn))
            )
            if validates:
                continue

            where = getattr(fn, "name", "<module level>")
            problems.append(
                f"{path}:{call.lineno} {cls}(...) in {where}() neither validates the "
                "operations pair nor copies it from an already-validated row. Call "
                "`workspace_settings.validate_operations` on the resulting values, or "
                "pass both columns straight through from the row you are copying."
            )

        assert not problems, "\n".join(problems)


class TestMaterialisationCopiesRatherThanInvents:
    """The one construction that is allowed to skip the check, and why.

    `find_or_autocreate_workspace` has no caller-supplied values to validate --
    it is applying a rule that was validated when it was written. That is only
    true while it COPIES both columns; the moment one is a literal, the rule's
    decision stops reaching the workspace and the gate above fails.
    """

    def test_the_materialiser_passes_both_through_from_the_rule(self):
        src = (_SRC / "services" / "workspace_autodiscovery_service.py").read_text()
        tree = ast.parse(src)
        builds = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == "Workspace")
            and any(k.arg in _PAIR for k in n.keywords if k.arg)
        ]
        assert builds, "no Workspace construction found in the materialiser"
        for call in builds:
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            for col in _PAIR:
                val = kw.get(col)
                assert isinstance(val, ast.Attribute) and val.attr == col, (
                    f"materialisation sets {col} from something other than the rule's "
                    f"own {col}; a rule's declaration must reach every workspace it "
                    "materialises, or the rule-level refusal guards nothing"
                )


@pytest.mark.parametrize("col", _PAIR)
def test_the_columns_still_exist(col):
    """Guards the gate above: _PAIR naming a dropped column inspects nothing."""
    from terrapod.db.models import AutodiscoveryRule, Workspace

    assert col in Workspace.__table__.columns
    assert col in AutodiscoveryRule.__table__.columns
