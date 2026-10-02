"""Every `AuthenticatedUser` must carry the IdP it authenticated with.

GHSA-3m8x-ff8g-7x8c was role resolution ignoring the provider, and the reason it
went unnoticed is that `provider_name` already existed and *looked* like the
answer -- for a token it holds the literal "api_token", which is the auth METHOD.
So the field that means "which IdP" is `identity_provider`, and a construction
that omits it silently resolves to no roles.

Resolving to no roles fails closed, which is the right direction, but it is still
a bug: the principal stops being able to do things they are entitled to, and the
cause is invisible at the call site. Five call sites across three modules build
this object, and more will be added.

**The rule is structural, not a list of blessed line numbers.** A construction
either passes `identity_provider`, or it is a runner token -- which authenticates
by HMAC against a run id and has no IdP at all. There is nothing to keep in step
and nothing to regenerate, so this cannot rot into an allowlist of things nobody
rechecked.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2] / "terrapod"


def _constructions() -> list[tuple[pathlib.Path, ast.Call]]:
    found: list[tuple[pathlib.Path, ast.Call]] = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AuthenticatedUser"
            ):
                found.append((path, node))
    return found


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_runner(call: ast.Call) -> bool:
    v = _kwarg(call, "auth_method")
    return isinstance(v, ast.Constant) and v.value == "runner_token"


def test_the_guard_is_actually_looking_at_something():
    """A rglob that matches nothing would make every assertion below vacuous."""
    assert len(_constructions()) >= 10, len(_constructions())


def test_every_construction_names_its_identity_provider_or_is_a_runner_token():
    offenders = [
        f"{path.relative_to(ROOT.parent)}:{call.lineno}"
        for path, call in _constructions()
        if _kwarg(call, "identity_provider") is None and not _is_runner(call)
    ]
    assert not offenders, (
        "These build an AuthenticatedUser without saying which IdP it "
        "authenticated with, so its roles resolve to nothing and the reason is "
        "invisible at the call site (GHSA-3m8x-ff8g-7x8c):\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nPass identity_provider=. For a session it is session.provider_name; "
        "for an API token it is api_token.identity_provider; for a principal that "
        "is synthesised in-process and never resolves roles from the assignment "
        "tables, pass None explicitly and say why in a comment."
    )


def test_the_resolver_never_reads_provider_name_off_a_PRINCIPAL():
    """It must take the IdP as an argument, not read it off the thing it is resolving.

    This is the specific confusion that produced the vulnerability, and it reads as
    perfectly sensible code: `provider_name` exists on both a session and an
    `AuthenticatedUser`, but for a token it holds the literal "api_token".

    The two ORM columns are exempt because reading `RoleAssignment.provider_name`
    is the *fix* -- that is the join. Anything else reading `.provider_name` inside
    this function is reading it off a principal.
    """
    src = (ROOT / "api" / "dependencies.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_resolve_user_roles"
    )
    allowed = {"RoleAssignment", "PlatformRoleAssignment"}
    bad = [
        ast.unparse(n)
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute)
        and n.attr == "provider_name"
        and not (isinstance(n.value, ast.Name) and n.value.id in allowed)
    ]
    assert not bad, (
        "_resolve_user_roles reads `.provider_name` off something other than the "
        f"assignment columns: {bad}. For an API token that attribute is the auth "
        "method ('api_token'), not the IdP -- take the provider as an argument."
    )
