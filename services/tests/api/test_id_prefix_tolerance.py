"""Every id path param accepts both spellings, and never 500s (#1699).

The unit tests in `test_ids.py` pin the parser. This pins the *routes*, which
is the part that actually regressed: a new endpoint calling `uuid.UUID(...)`
directly would pass every parser test and still 500 in production.

Two properties, checked by driving the real app:

1.  **No id path param can produce a 500.** Whatever a caller sends -- a typo,
    a prefixed id, an empty segment -- the answer is a 4xx. A 500 means an
    uncaught ValueError reached the global handler.
2.  **Both spellings get the same answer.** `ws-{uuid}` and the bare uuid must
    not diverge, since consumers copy ids between endpoints.

Deliberately a source-plus-behaviour pair: the introspection half catches a
direct `uuid.UUID(...)` on request input even where no test drives that route,
which a purely behavioural test cannot do for 363 routes.
"""

import ast
import pathlib
import re
import uuid

import pytest

ROUTERS = pathlib.Path(__file__).resolve().parents[2] / "terrapod" / "api" / "routers"

#: Parses that are allowed to raise, with the reason. Two distinct categories,
#: kept apart because conflating them is how an allowlist stops meaning
#: anything.
#:
#: **Stored data.** The value came from Redis or a database column, not from
#: the request. A failure is a genuine data fault and must stay a 500 --
#: turning it into a 404 would hide exactly what the 500 is for.
#:
#: **Guarded one frame up.** The call is inside a helper whose every caller
#: wraps it in `except ValueError`, translating it to a field-specific message.
#: The AST walk only sees the helper's own body, so it cannot tell. Converting
#: these to raise an HTTPException would *silently change the status* the
#: caller returns -- which is the opposite of the additive fix.
_ALLOWED_RAISES = {
    # stored data
    "catalog.py": ["uuid.UUID(str(tid))"],
    "agent_pools.py": ['uuid.UUID(listener["pool_id"])', "uuid.UUID(pool_id_str)"],
    "runs.py": ['uuid.UUID(listener["pool_id"])'],
    # guarded one frame up: both callers of `_strip_uuid_prefix` translate the
    # ValueError into `422 "<field> is not a UUID"`.
    "autodiscovery_rules.py": ["uuid.UUID(raw)"],
}

#: Prefixes a serializer emits that are NOT addressable resource ids, so no
#: endpoint should accept them back and they do not belong in `ID_PREFIXES`.
#: Each was checked the same way: does anything in the codebase ever
#: `removeprefix` it? For all of these the answer is no.
_NOT_RESOURCE_IDS = {
    "tprun-",  # a Kubernetes object name, not an API id
    "lock-",  # `lock-{email}`: a lock token value handed to the CLI
    "re-",  # `re-{run_id}-{i}`: a synthetic index inside a collection
    "vrc-",  # `vrc-{uuid4()}`: minted per response, never re-fetched
    "at-",  # a real id, but `at-{token_hex}` -- not a uuid; the prefix is
    # part of the stored primary key, so tolerance there means
    # normalising up (see auth/api_tokens.get_token_by_id), not
    # stripping, and this table is prefix-plus-uuid only.
}


def _catches_value_error(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names: list[str] = []
    if isinstance(t, ast.Tuple):
        names = [n.id for n in t.elts if isinstance(n, ast.Name)]
    elif isinstance(t, ast.Name):
        names = [t.id]
    elif isinstance(t, ast.Attribute):
        names = [t.attr]
    return any(n in ("ValueError", "Exception", "BaseException") for n in names)


def _is_uuid_parse(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "UUID":
        return True
    return isinstance(f, ast.Name) and f.id in ("UUID", "_uuid")


class _Finder(ast.NodeVisitor):
    """Unprotected `uuid.UUID(...)` calls -- the shape that becomes a 500."""

    def __init__(self, source: str):
        self.source = source
        self.guarded: list[bool] = []
        self.hits: list[tuple[int, str]] = []

    def visit_Try(self, node: ast.Try) -> None:
        self.guarded.append(any(_catches_value_error(h) for h in node.handlers))
        for stmt in node.body:
            self.visit(stmt)
        self.guarded.pop()
        for group in (node.handlers, node.orelse, node.finalbody):
            for stmt in group:
                self.visit(stmt)

    def generic_visit(self, node: ast.AST) -> None:
        if _is_uuid_parse(node) and not any(self.guarded):
            self.hits.append((node.lineno, ast.get_source_segment(self.source, node) or ""))
        super().generic_visit(node)


def _router_files() -> list[pathlib.Path]:
    return sorted(p for p in ROUTERS.glob("*.py") if p.name != "__init__.py")


class TestNoRouterParsesAnIdUnguarded:
    """The invariant, enforced at the source so it cannot regress unseen."""

    @pytest.mark.parametrize("path", _router_files(), ids=lambda p: p.name)
    def test_no_unguarded_uuid_parse(self, path: pathlib.Path):
        source = path.read_text()
        finder = _Finder(source)
        finder.visit(ast.parse(source, filename=str(path)))

        allowed = _ALLOWED_RAISES.get(path.name, [])
        offenders = [
            f"{path.name}:{line}  {expr}"
            for line, expr in finder.hits
            if not any(a in expr for a in allowed)
        ]
        assert not offenders, (
            "These parse a caller-supplied id without catching ValueError, so a "
            "bad id reaches the global handler in app.py and is reported as "
            "`500 Internal server error` (#1699).\n\n"
            + "\n".join(offenders)
            + "\n\nUse `terrapod.api.ids.parse_id`, passing the status this "
            "endpoint already returns for a bad id. A raise is correct only "
            "if the value came from storage rather than the request, or if "
            "every caller already catches the ValueError -- add it to "
            "_ALLOWED_RAISES saying which of those it is."
        )


class TestThePrefixTableMatchesWhatIsEmitted:
    """A prefix in the table that no serializer emits is a typo, not tolerance."""

    def test_every_prefix_is_emitted_somewhere(self):
        from terrapod.api.ids import ID_PREFIXES

        emitted = "\n".join(p.read_text() for p in _router_files())
        missing = [
            f"{resource_type} ({prefix!r})"
            for resource_type, prefix in ID_PREFIXES.items()
            if prefix and f'f"{prefix}{{' not in emitted
        ]
        assert not missing, (
            "ID_PREFIXES lists a prefix no serializer emits, so `parse_id` "
            "would strip something no caller ever sends:\n  " + "\n  ".join(missing)
        )

    def test_no_serializer_emits_a_prefix_the_table_lacks(self):
        from terrapod.api.ids import ID_PREFIXES

        known = {p for p in ID_PREFIXES.values() if p}
        # Serializers spell it `f"ws-{...}"`. Two-to-nine lowercase letters
        # then a hyphen, immediately before an interpolation.
        found = set(
            re.findall(r'f"([a-z]{2,9}-)\{', "\n".join(p.read_text() for p in _router_files()))
        )
        unknown = sorted(found - known - _NOT_RESOURCE_IDS)
        assert not unknown, (
            "A serializer emits a typed id prefix that ID_PREFIXES does not "
            "know about, so no endpoint will accept that spelling back:\n  "
            + "\n  ".join(unknown)
            + "\n\nEither add it to ID_PREFIXES (if callers can hold that id "
            "and send it back), or to _NOT_RESOURCE_IDS with the reason. The "
            "test is: does anything `removeprefix` it?"
        )


SAMPLE = uuid.UUID("01a0aa66-8cd5-7ea3-b290-4004b03a8672")


class TestTheHelperIsWhatTheRoutersReachFor:
    """That the routers use the helper at all, not just that it exists.

    Deliberately source-level rather than behavioural. Driving these routes
    for real means authenticating and mocking the database far enough for the
    handler to reach its id parse -- at which point the test is exercising the
    mocks, and a handler that never parsed the id would pass just as happily.
    The property worth pinning is "no router parses a request id unguarded",
    and `TestNoRouterParsesAnIdUnguarded` above pins exactly that, across all
    of them, including routes no behavioural test would think to drive.

    What is checked here is the other half: that the helper is actually
    reached from the routers rather than sitting unused beside a pile of
    hand-rolled parses.
    """

    def test_the_routers_import_the_shared_helper(self):
        importers = [
            p.name for p in _router_files() if "from terrapod.api.ids import" in p.read_text()
        ]
        # Every router that resolves a typed id should route through it. The
        # exact set will grow; the point is that it is not empty and not one.
        assert len(importers) >= 10, (
            "Few routers import `terrapod.api.ids`, which suggests id parsing "
            f"is drifting back to hand-rolled `uuid.UUID(...)`. Importers: {importers}"
        )

    def test_the_runner_gate_normalises_both_sides(self):
        """The auth path, which a parse fix alone does not cover.

        `require_runner_for_run` compares the token's run id to the path's. The
        token carries a bare uuid, so comparing raw strings rejected a prefixed
        path id as "not scoped to this run" -- a spelling mismatch reported as
        a security failure (#1699).
        """
        from terrapod.api import dependencies

        source = pathlib.Path(dependencies.__file__).read_text()
        gate = source.split("def require_runner_for_run")[1].split("\ndef ")[0]
        assert "strip_id_prefix" in gate, (
            "require_runner_for_run compares the token's run id to the path's "
            "without normalising the spelling, so a prefixed path id is "
            "rejected as a scope failure."
        )

    def test_the_token_lookup_normalises_up(self):
        """Tokens are the reverse case: the prefix is part of the stored key."""
        from terrapod.auth import api_tokens

        source = pathlib.Path(api_tokens.__file__).read_text()
        lookup = source.split("async def get_token_by_id")[1].split("\nasync def ")[0]
        assert "_ID_PREFIX" in lookup, (
            "get_token_by_id looks up the raw string, so a bare id matches no "
            "row. The `at-` prefix is part of the primary key here, so "
            "tolerance means adding it when absent, not stripping it."
        )
