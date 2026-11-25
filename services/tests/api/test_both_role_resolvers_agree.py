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
#:
#: This list is NOT the gate's whole subject, because it used to be and that was
#: the hole (#2715): naming the functions we check means a third resolver is
#: checked by nothing. Sixteen queries over these two tables exist across seven
#: modules, and this list reached four of them.
#:
#: What cannot be derived is which queries resolve a LOGGED-IN identity's
#: effective roles. The other twelve are admin management and reach queries:
#: `list_role_assignments`, `list_identities` and `_role_holders` list every
#: assignment for an operator view and are not identity-scoped at all, while
#: `set_role_assignments`, `delete_role_assignment` and `bootstrap` address one
#: specific row by its email AND provider. None of them decides what a session
#: may do, so `subject_matches` -- which asks "is this assignment unpinned, or
#: pinned to exactly this `sub`" -- has nothing to say about them.
#:
#: So the list stays, and `test_the_resolver_set_is_closed` below holds it to
#: the tree from the other side: the functions that CALL `subject_matches` must
#: be exactly these. A third resolver that forgets the predicate is invisible to
#: any list, but one that uses it and is not listed here now fails, and so does
#: a listed one that stops using it.
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


def test_the_resolver_set_is_closed():
    """The functions calling `subject_matches` are exactly `RESOLVERS`.

    `RESOLVERS` is a hand-kept list, so on its own it can only check what it
    already knows about -- and the defect it exists to prevent (GHSA-3m8x) is a
    SECOND path becoming the way around a rule applied on the first. This holds
    the list to the tree from the other side, which is the half a ledger cannot
    do for itself.

    It catches two things a list cannot. A function that starts using the
    predicate without being listed here means a third role-resolution path
    exists and the per-query checks above never looked at it. A listed function
    that stops using it means a resolver has been gutted while its entry still
    reads as covered.

    It deliberately does NOT claim to catch a third resolver that never calls
    the predicate at all -- nothing mechanical can, because "resolves a
    logged-in identity's roles" is a semantic property. That residue is why the
    docstring above asks whether a third resolver should exist.
    """
    import ast

    found = set()
    for path in sorted(ROOT.rglob("*.py")):
        src = path.read_text()
        if "subject_matches" not in src:
            continue
        tree = ast.parse(src)
        # Both resolvers import it ALIASED -- `subject_matches as
        # _subject_matches` -- so matching the call by its original name finds
        # neither of them. Resolve the local name from the import first.
        local_names = {"subject_matches"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "subject_matches":
                        local_names.add(alias.asname or alias.name)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                if name in local_names:
                    found.add((str(path.relative_to(ROOT)), fn.name))
                    break

    expected = {(rel, name) for rel, name in RESOLVERS}
    extra = sorted(found - expected)
    missing = sorted(expected - found)
    assert not extra, (
        "a function outside RESOLVERS applies the shared subject predicate, so a "
        "role-resolution path exists that the per-query checks above never read: "
        f"{extra}. Add it to RESOLVERS (and ask whether it should exist)."
    )
    assert not missing, (
        "a listed resolver no longer applies the shared subject predicate, so its "
        f"entry reads as covered while the rule is gone: {missing}"
    )
