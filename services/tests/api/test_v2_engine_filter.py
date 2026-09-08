"""The `/api/v2/` surface never serves a non-Terraform row (#1407 §2, #1487).

A source-introspection test, which is the right shape here: the invariant is
"every workspace/run lookup in `tfe_v2.py` carries the engine filter", and that is
a property of the *source*, not of any one response. A future edit that adds a
query without the filter is exactly what this catches, and nothing else would —
with Terraform the only engine, every runtime assertion passes either way.

The failure it guards against is silent. A `terraform` CLI handed a Pulumi
workspace does not get an error; it gets a workspace it cannot parse.

**The two exceptions are load-bearing.** `workspaces.name` is unique *globally*,
across engines (`uq_workspaces`), so the name-uniqueness guards must NOT filter:
one that did would decide a taken name was free and then hit an IntegrityError,
turning a clean 422 into a 500. They are allow-listed by name here so that
reasoning survives — rather than being invisibly absent from the list.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROUTER = pathlib.Path(__file__).resolve().parents[2] / "terrapod/api/routers/tfe_v2.py"

#: Models whose rows are engine-scoped, so a V2 query must not return another
#: engine's.
GUARDED = {"Workspace", "Run", "ConfigurationVersion"}

#: Queries that deliberately span every engine, and why. A name-uniqueness check
#: is asking "is this name taken anywhere", which is what the database constraint
#: enforces; scoping it to one engine would make it answer a different question
#: from the one the constraint asks.
UNSCOPED_BY_DESIGN = {
    "create_workspace": "name-uniqueness guard against a globally unique constraint",
    "update_workspace": "rename uniqueness guard against a globally unique constraint",
}


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or (node.end_lineno - node.lineno) < (
                    best.end_lineno - best.lineno
                ):
                    best = node
    return best.name if best else "<module>"


def _select_calls(tree: ast.AST):
    """Every `select(Model)` whose model is engine-scoped, with its statement."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "select"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in GUARDED:
                yield node, arg.id


def _statement_source(src: str, tree: ast.AST, call: ast.Call) -> str:
    """The whole statement a call sits in.

    Taking the statement rather than the call matters: the filter is applied in a
    chained `.where(...)`, which is a *parent* of the `select(...)` node, so
    looking only at the call itself would never see it.
    """
    lines = src.splitlines()
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            if node.lineno <= call.lineno and (node.end_lineno or node.lineno) >= call.end_lineno:
                if best is None or (node.end_lineno - node.lineno) < (
                    best.end_lineno - best.lineno
                ):
                    best = node
    if best is None:
        return ""
    return "\n".join(lines[best.lineno - 1 : best.end_lineno])


def test_every_v2_lookup_is_engine_scoped():
    src = ROUTER.read_text()
    tree = ast.parse(src)

    unfiltered: list[str] = []
    for call, model in _select_calls(tree):
        func = _enclosing_function(tree, call.lineno)
        stmt = _statement_source(src, tree, call)
        if "_engine_filter" in stmt:
            continue
        if func in UNSCOPED_BY_DESIGN:
            continue
        unfiltered.append(f"{func}() line {call.lineno}: select({model}) has no _engine_filter")

    assert not unfiltered, (
        "every workspace/run lookup on the /api/v2/ surface must be scoped to the "
        "Terraform engine, or a `terraform` CLI can be handed a row it cannot "
        "parse:\n  " + "\n  ".join(unfiltered)
    )


@pytest.mark.parametrize("func", sorted(UNSCOPED_BY_DESIGN))
def test_the_allow_listed_guards_still_exist(func: str):
    """An allow-list entry for a function that no longer exists is a silent hole.

    If `create_workspace` is renamed, its entry stops matching anything and the
    real query underneath it is no longer excused — but nothing would say so, and
    the entry would sit there looking like it still meant something.
    """
    tree = ast.parse(ROUTER.read_text())
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert func in names, (
        f"{func}() is allow-listed as intentionally engine-unscoped but no longer "
        "exists — remove the entry, or point it at the function that replaced it"
    )


def test_the_filter_helper_is_not_the_auxiliary_run_filter():
    """They mean different things and must not be merged.

    `_primary_run_filter` excludes auxiliary runs from workspace health;
    `_engine_filter` scopes to an engine. Folding them together is how one gets
    removed later for a reason that only applied to the other.
    """
    src = ROUTER.read_text()
    assert "def _engine_filter(" in src
    assert "def _primary_run_filter(" in src
    tree = ast.parse(src)
    engine_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_engine_filter"
    )
    body = ast.dump(engine_fn)
    assert "engine" in body, "_engine_filter must actually compare the engine column"
    assert "source" not in body, "_engine_filter must not have absorbed run-source logic"
