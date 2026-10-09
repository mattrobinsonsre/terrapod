"""`oidc-audiences` is refused at every write path that accepts it (#1901).

`tests/services/test_validate_oidc_audiences.py` exercises the rule and says so
in its own docstring: it "is the only thing standing behind four write paths".
Nothing pinned that it is *called* at any of them. Before this file no test in
the repository sent an `oidc-audiences` payload to a router at all, and there
was no 422 test for the attribute anywhere.

**The bulk path is the one that was genuinely unguarded.** Deleting
`"oidc-audiences": workspace_settings.validate_oidc_audiences` from
`_SETTING_RULES` in `routers/workspace_bulk.py` leaves every existing guard
satisfied: the invariant test there checks only that `_SETTING_RULES` is a
subset of `_FIELD_MAP` (removing a rule leaves no orphan), and
`test_workspace_setting_parity` only requires the field to be present in
`_FIELD_MAP`. A fleet-wide bulk update would then write unvalidated JSONB —
including a provider key the runner joins into `<token dir>/<key>/token`, which
is why the validator refuses a separator or a parent reference in the first
place.

So the invalid value used throughout is a traversal key rather than a
type error: it is the one the validator exists for, and the one whose absence
has a consequence beyond an ugly row.

The two structural tests at the bottom are DERIVED from the tree rather than
from a list of modules, because a hand-written test per write path is exactly
what goes stale when a fifth path is added.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_CREATE = "/api/v2/organizations/default/workspaces"
_BULK = "/api/terrapod/v1/workspaces/actions/bulk-update"

#: The value every path is driven with. A key the runner would join into a
#: filesystem path, which `unsafe_target_reason` refuses — not a mere type
#: error, so a path that "validates" by coincidence (a JSONB column happily
#: storing a dict) cannot pass.
TRAVERSAL = {"aws/../vault": ["sts.amazonaws.example"]}

#: A second shape, so a path is not accidentally refusing only on the one
#: branch. An explicitly empty list is refused because it means nothing:
#: removing the key is how you stop overriding.
EMPTY_LIST = {"aws": []}

VALID = {"aws": ["sts.amazonaws.example"], "vault.eu": ["https://vault.example/"]}


def _user(roles=None):
    return AuthenticatedUser(
        email="test@terrapod.test",
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _mock_workspace():
    """The PATCH response serialises the whole workspace, so this reuses the
    canonical fixture rather than growing a second one.

    A per-file mock is how a newly serialised column breaks half a dozen test
    files at once with a `MagicMock is not JSON serializable` that names neither
    the column nor the fixture; one more copy of it here would be one more place
    to fix.
    """
    from tests.api.test_workspaces import _mock_workspace as canonical

    ws = canonical(name="dns-prod")
    ws.oidc_audiences = {}
    return ws


def _app(user, *, admin_override=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if admin_override is not None:
        app.dependency_overrides[require_admin] = lambda: admin_override
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


def _no_existing_workspace(db):
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)


def _existing_workspace(db, ws):
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    db.execute = AsyncMock(return_value=result)


def _created(db):
    return next(
        c.args[0]
        for c in db.add.call_args_list
        if getattr(c.args[0], "__tablename__", "") == "workspaces"
    )


@pytest.fixture(autouse=True)
def _no_event_publish():
    with patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock):
        yield


class TestWorkspaceCreate:
    """`POST /api/v2/organizations/default/workspaces` (`routers/tfe_v2.py`)."""

    async def _post(self, attributes):
        app, db = _app(_user(roles=["admin"]))
        _no_existing_workspace(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                _CREATE,
                json={"data": {"type": "workspaces", "attributes": attributes}},
                headers=_AUTH,
            )
        return resp, db

    @pytest.mark.parametrize(
        "bad", [TRAVERSAL, EMPTY_LIST, {"a b": ["x"]}, {"a.b.c": ["x"]}, ["aws"], "aws"]
    )
    async def test_an_invalid_map_is_refused_before_the_workspace_exists(self, bad):
        resp, db = await self._post({"name": "oidc-create-bad", "oidc-audiences": bad})
        assert resp.status_code == 422, resp.text
        assert "oidc-audiences" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    async def test_a_valid_map_is_stored_byte_for_byte(self):
        """The positive case, which is also the no-normalisation property: the
        provider writes the server's response back into state, so lower-casing
        or trimming here would make every plan disagree with its own apply."""
        resp, db = await self._post({"name": "oidc-create-ok", "oidc-audiences": VALID})
        assert resp.status_code == 201, resp.text
        assert _created(db).oidc_audiences == VALID

    async def test_omitting_it_leaves_an_empty_map_not_none(self):
        """An override *over* the deployment catalogue, so absent means "take
        the catalogue" — and `None` in a JSONB column would make every reader
        guard for it."""
        resp, db = await self._post({"name": "oidc-create-absent"})
        assert resp.status_code == 201, resp.text
        assert _created(db).oidc_audiences == {}


class TestWorkspaceUpdate:
    """`PATCH /api/v2/workspaces/{id}` (`routers/tfe_v2.py`)."""

    async def _patch(self, value):
        ws = _mock_workspace()
        app, db = _app(_user())
        _existing_workspace(db, ws)
        with patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for") as caps:
            caps.return_value = caps_for_level("admin")
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.patch(
                    f"/api/v2/workspaces/ws-{ws.id}",
                    json={"data": {"attributes": {"oidc-audiences": value}}},
                    headers=_AUTH,
                )
        return resp, db, ws

    @pytest.mark.parametrize("bad", [TRAVERSAL, EMPTY_LIST, "aws"])
    async def test_an_invalid_map_is_refused_and_nothing_is_written(self, bad):
        resp, db, ws = await self._patch(bad)
        assert resp.status_code == 422, resp.text
        assert "oidc-audiences" in resp.json()["detail"]
        assert ws.oidc_audiences == {}
        db.commit.assert_not_awaited()

    async def test_a_valid_map_is_stored_byte_for_byte(self):
        resp, _db, ws = await self._patch(VALID)
        assert resp.status_code == 200, resp.text
        assert ws.oidc_audiences == VALID


class TestBulkUpdate:
    """`POST /api/terrapod/v1/workspaces/actions/bulk-update`.

    The path with the widest reach — one request writes a hundred workspaces —
    and the one whose validator could be deleted with every existing guard
    still green.
    """

    async def _post(self, value, *, dry_run=True):
        app, db = _app(_user(roles=["admin"]), admin_override=_user(roles=["admin"]))
        scalars = MagicMock()
        scalars.all.return_value = []
        result = MagicMock()
        result.scalars.return_value = scalars
        db.execute = AsyncMock(return_value=result)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                _BULK,
                json={
                    "filter": {"all": True},
                    "update": {"oidc-audiences": value},
                    "dry_run": dry_run,
                },
                headers=_AUTH,
            )
        return resp, db

    @pytest.mark.parametrize("bad", [TRAVERSAL, EMPTY_LIST, {"a b": ["x"]}, "aws"])
    async def test_an_invalid_map_is_refused_with_zero_mutation(self, bad):
        resp, db = await self._post(bad)
        assert resp.status_code == 422, resp.text
        assert "oidc-audiences" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    async def test_the_refusal_also_holds_when_dry_run_is_off(self):
        """A dry run validates on the same code path, so a test that only ever
        asked for one would not prove the applying path refuses."""
        resp, db = await self._post(TRAVERSAL, dry_run=False)
        assert resp.status_code == 422, resp.text
        db.commit.assert_not_awaited()

    async def test_a_valid_map_is_accepted(self):
        """So the 422s above are not the endpoint rejecting the key outright —
        which is how a deleted rule could be mistaken for a working one if the
        payload key had also been dropped from `_FIELD_MAP`."""
        resp, _db = await self._post(VALID)
        assert resp.status_code == 200, resp.text


# ── Derived, not declared ────────────────────────────────────────────────────
#
# Four hand-written tests go stale the moment a fifth write path is added, and
# the gap this file closes is precisely that nobody noticed a path was missing.
# These read the tree instead.

_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "terrapod"
_KEY = "oidc-audiences"
_VALIDATOR = "validate_oidc_audiences"


def _package_modules():
    mods = sorted(_PACKAGE.rglob("*.py"))
    assert len(mods) > 50, f"found only {len(mods)} modules under {_PACKAGE} — wrong path?"
    return mods


def test_every_module_that_mentions_the_attribute_also_references_its_validator():
    """Module-scoped, and derived from the package rather than from a list.

    This is what catches the stated mutation: `workspace_bulk.py` mentions the
    validator in exactly one place, so deleting the `_SETTING_RULES` entry
    removes the last reference from that module and this fails.

    The floor matters as much as the loop. Renaming the payload key would
    otherwise make the scan match nothing and pass, which is the failure mode
    every ledger-driven gate in this suite carries a floor against.
    """
    mentions_key = []
    missing = []
    for path in _package_modules():
        src = path.read_text()
        if f'"{_KEY}"' not in src:
            continue
        mentions_key.append(path.name)
        if _VALIDATOR not in src:
            missing.append(str(path.relative_to(_PACKAGE)))

    assert len(mentions_key) >= 3, (
        f"only {mentions_key} mention {_KEY!r} — the attribute was renamed and "
        f"this gate is now scanning for a string nothing uses"
    )
    assert not missing, (
        f"these modules accept or surface {_KEY!r} without referencing {_VALIDATOR}: {missing}"
    )


class _PayloadReads(ast.NodeVisitor):
    """Every place the literal is used to READ a value out of a mapping.

    Three forms, which is all the write paths use: `attrs["k"]`,
    `attrs.get("k")` and `"k" in attrs`. A dict LITERAL carrying the key is
    deliberately not one — that is a serializer writing a response, and
    requiring it to mention the validator would be nonsense.
    """

    def __init__(self):
        self.reads: list[tuple[str, int]] = []
        self._fn: list[str] = []

    def _enter(self, node):
        self._fn.append(node.name)
        self.generic_visit(node)
        self._fn.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter

    def _hit(self, node):
        self.reads.append((self._fn[-1] if self._fn else "<module>", node.lineno))

    def visit_Subscript(self, node):
        if isinstance(node.slice, ast.Constant) and node.slice.value == _KEY:
            self._hit(node)
        self.generic_visit(node)

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == _KEY
        ):
            self._hit(node)
        self.generic_visit(node)

    def visit_Compare(self, node):
        if (
            isinstance(node.left, ast.Constant)
            and node.left.value == _KEY
            and any(isinstance(op, ast.In) for op in node.ops)
        ):
            self._hit(node)
        self.generic_visit(node)


def test_every_payload_read_of_the_attribute_sits_in_a_function_that_validates_it():
    """Function-scoped, which the module check cannot be.

    `tfe_v2.py` reads the attribute on two paths — create and update — and
    mentions the validator twice, once per path. A module-scoped check cannot
    tell those apart, so deleting the create-path validator would leave the
    update-path reference standing and pass. This one fails, naming the
    function.
    """
    offenders = []
    total = 0
    for path in _package_modules():
        src = path.read_text()
        if f'"{_KEY}"' not in src:
            continue
        tree = ast.parse(src)
        visitor = _PayloadReads()
        visitor.visit(tree)
        if not visitor.reads:
            continue
        # The function's own source, so the check is scoped to the path that
        # does the reading rather than to the whole module.
        bodies = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                bodies[node.name] = ast.get_source_segment(src, node) or ""
        for fn, lineno in visitor.reads:
            total += 1
            scope = bodies.get(fn, src)
            if _VALIDATOR not in scope:
                offenders.append(f"{path.relative_to(_PACKAGE)}:{lineno} in {fn}()")

    assert total >= 3, (
        f"found only {total} payload reads of {_KEY!r} — the read forms moved and "
        f"this gate now inspects nothing"
    )
    assert not offenders, (
        f"these read {_KEY!r} from a request payload without routing it through "
        f"{_VALIDATOR}: {offenders}"
    )
