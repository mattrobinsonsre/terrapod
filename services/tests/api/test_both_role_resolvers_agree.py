"""The login-side and token-side role resolvers must apply the same matching rule.

There are two of them, and that is deliberate: login already holds the identity while
the token path has to reconstruct it from a stored row. But a rule applied in one and
not the other is a hole rather than an inconsistency — the provider-scoping fix for
GHSA-3m8x-ff8g-7x8c would have been defeated by logging in rather than by using a
token, and the subject-pinning rule has exactly the same shape.

This repo has shipped that failure before: "the Sync button got `force: True` so a set
whose SHA has not moved is re-read; the PATCH path that sets the same column got
nothing." Fixing one path and not its sibling is how a fail-open ships.

So both resolvers share one predicate (`db.models.subject_matches`) and this gate
fails if either stops using it.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2] / "terrapod"

#: (module, function) pairs that resolve roles from the assignment tables.
#: Add to this when a third resolver appears -- and if you are adding a third,
#: consider whether it should exist at all.
RESOLVERS = [
    ("api/dependencies.py", "_resolve_user_roles"),
    ("services/sso_service.py", "_load_internal_assignments"),
]


def _fn(rel: str, name: str) -> ast.AST:
    tree = ast.parse((ROOT / rel).read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{rel}: {name} not found — was it renamed? Update RESOLVERS.")


def test_both_resolvers_exist_so_this_gate_is_not_vacuous():
    for rel, name in RESOLVERS:
        assert _fn(rel, name) is not None


def _counts(rel: str, name: str) -> tuple[int, int]:
    """(queries against the assignment tables, calls to the shared predicate)."""
    fn = _fn(rel, name)
    models = {"RoleAssignment", "PlatformRoleAssignment"}
    queries = sum(
        1
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "select"
        and any(
            isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id in models
            for a in n.args
        )
    )
    predicates = sum(
        1
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"_subject_matches", "subject_matches"}
    )
    return queries, predicates


def test_every_assignment_query_applies_the_shared_subject_predicate():
    """Per QUERY, not per function.

    An earlier version of this gate asked only whether the function *mentioned* the
    predicate. Both resolvers query two tables, so removing it from one of the two
    left the other call standing and the gate passed — a mutation check caught it.
    That is the shape this repo keeps re-learning: a gate that trusts a single
    mention cannot see a half-applied rule.
    """
    bad = []
    for rel, name in RESOLVERS:
        queries, predicates = _counts(rel, name)
        assert queries > 0, f"{rel}::{name}: no assignment-table query found — gate vacuous"
        if predicates < queries:
            bad.append(f"{rel}::{name}: {queries} quer(ies) but only {predicates} predicate(s)")
    assert not bad, (
        "An assignment-table query is missing the shared subject predicate:\n  "
        + "\n  ".join(bad)
        + "\n\nEvery query against role_assignments or platform_role_assignments must "
        "carry subject_matches(...), or a pinned assignment leaks through that one."
    )


def test_both_resolvers_filter_on_the_provider():
    """The provider join is the other half, and has the same sibling-path risk."""
    missing = []
    for rel, name in RESOLVERS:
        src = ast.unparse(_fn(rel, name))
        if "provider_name ==" not in src:
            missing.append(f"{rel}::{name}")
    assert not missing, (
        f"These do not filter on provider_name: {missing}. Role assignments are keyed "
        "(provider, email); matching on email alone is GHSA-3m8x-ff8g-7x8c."
    )
