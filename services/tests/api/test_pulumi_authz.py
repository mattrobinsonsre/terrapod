"""Workspace authorization on the Pulumi service API (#1550).

The surface first shipped authenticating callers and authorizing no one: any
signed-in principal could list, read, overwrite and delete any Pulumi stack's
state, and a lease issued for one stack's update was accepted against another's.
Every unit test passed, because each one injected a user and none asked what that
user was *allowed* to do.

So these are the negative paths, which is where the defect lived:

- no workspace grant → every stack route refuses, and says nothing a stranger
  could use to tell a real stack from an imaginary one;
- a grant on workspace A → refused on workspace B, for read, write and delete;
- read access alone → refused wherever the call needs more, naming what;
- a lease for an update on A → refused against B; a preview's lease → refused
  on the one route that writes state;
- a bad lease → the same 401 whether or not the stack exists.

And a guard reading the router's source, so a handler added later cannot quietly
skip authorization the way every handler here once did.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap
from terrapod.db.session import get_db

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/pulumi/api"
MOD = "terrapod.api.routers.pulumi_service"
_ROUTER = pathlib.Path(__file__).resolve().parents[2] / "terrapod/api/routers/pulumi_service.py"

#: What a `read` preset holds on a workspace.
READ_ONLY = frozenset({cap.WORKSPACE_READ, cap.RUN_READ, cap.STATE_READ_METADATA})
EVERYTHING = frozenset(
    {
        cap.WORKSPACE_READ,
        cap.RUN_READ,
        cap.STATE_READ_METADATA,
        cap.STATE_READ,
        cap.STATE_WRITE,
        cap.RUN_PLAN,
        cap.RUN_APPLY,
        cap.RUN_APPLY_DESTROY,
        cap.WORKSPACE_DELETE,
    }
)

#: Every user-authenticated route addressed at a stack, and what it requires.
STACK_ROUTES = [
    ("get", "", cap.WORKSPACE_READ),
    ("delete", "", cap.WORKSPACE_DELETE),
    ("get", "/export", cap.STATE_READ),
    ("post", "/import", cap.STATE_WRITE),
    ("post", "/encrypt", cap.STATE_READ),
    ("post", "/decrypt", cap.STATE_READ),
    ("post", "/batch-decrypt", cap.STATE_READ),
    ("post", "/preview", cap.RUN_PLAN),
    ("post", "/update", cap.RUN_APPLY),
    ("post", "/refresh", cap.RUN_APPLY),
    ("post", "/destroy", cap.RUN_APPLY_DESTROY),
    ("get", "/update/u-1", cap.RUN_READ),
]

LEASE_ROUTES = [
    ("patch", "/update/u-1/checkpoint"),
    ("post", "/update/u-1/events/batch"),
    ("post", "/update/u-1/complete"),
    ("post", "/update/u-1/renew_lease"),
]


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="someone@example.test",
        display_name="Someone",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _ws(name: str) -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = name
    ws.labels = {}
    ws.engine = "pulumi"
    ws.updated_at = None
    return ws


A = _ws("proj::a")
B = _ws("proj::b")
_BY_ID = {"default/proj/a": A, "default/proj/b": B}


async def _find(db, stack_id):
    from fastapi import HTTPException

    if stack_id not in _BY_ID:
        raise HTTPException(status_code=404, detail="Stack not found")
    return _BY_ID[stack_id]


def _caps(grants: dict[str, frozenset[str]]):
    """Capabilities by workspace name — the whole of what the tests vary."""

    async def resolve(db, user, ws, **_):
        return grants.get(ws.name, frozenset())

    return resolve


def _app(db=None):
    from terrapod.api.app import create_application
    from terrapod.api.routers.pulumi_service import pulumi_user

    app = create_application()
    app.dependency_overrides[pulumi_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: db or AsyncMock()
    return app


def _quiet_redis() -> AsyncMock:
    """No update records and no locks — what a store with nothing in flight returns."""
    r = AsyncMock()
    r.hgetall.return_value = {}
    r.get.return_value = None
    return r


async def _call(method: str, path: str, grants: dict, *, headers=None, redis=None, db=None):
    """One request through the real app, with lookup and resolution controlled."""
    patches = [
        patch(f"{MOD}._find_stack", _find),
        patch(f"{MOD}.resolve_workspace_capabilities_for", _caps(grants)),
        # Past the gate, a call should not need a real store to answer; these stop
        # a correctly-authorized request failing for reasons unrelated to authz.
        patch(f"{MOD}._read_deployment", AsyncMock(return_value=None)),
        patch(f"{MOD}._write_deployment", AsyncMock()),
        patch(f"{MOD}._begin_update", AsyncMock(return_value={"updateID": "u-new"})),
        patch("terrapod.redis.client.get_redis_client", return_value=redis or _quiet_redis()),
    ]
    for p in patches:
        p.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url="http://t") as c:
            return await c.request(method, f"{BASE}{path}", headers=headers or {}, json={})
    finally:
        for p in patches:
            p.stop()


# ── the user-authenticated routes ────────────────────────────────────────────


class TestNoGrantMeansNothing:
    """A signed-in principal with no grant on the workspace — the advisory's
    case, and the one every earlier test skipped by injecting a user."""

    @pytest.mark.parametrize(("method", "suffix", "_required"), STACK_ROUTES)
    async def test_every_stack_route_refuses(self, method, suffix, _required) -> None:
        r = await _call(method, f"/stacks/default/proj/a{suffix}", grants={})
        assert r.status_code == 404, f"{method.upper()} {suffix or '/'} answered {r.status_code}"

    @pytest.mark.parametrize(("method", "suffix", "_required"), STACK_ROUTES)
    async def test_and_says_nothing_a_real_stack_would_not(self, method, suffix, _required) -> None:
        """Refused on a stack that exists must be byte-identical to asked about
        one that does not, or the refusal is an oracle for which names are real."""
        real = await _call(method, f"/stacks/default/proj/a{suffix}", grants={})
        imaginary = await _call(method, f"/stacks/default/proj/nope{suffix}", grants={})
        assert (real.status_code, real.json()) == (imaginary.status_code, imaginary.json())

    async def test_the_stack_list_omits_what_cannot_be_read(self) -> None:
        db = AsyncMock()
        rows = MagicMock()
        rows.scalars.return_value.all.return_value = [A, B]
        db.execute.return_value = rows

        r = await _call("get", "/user/stacks", grants={"proj::a": READ_ONLY}, db=db)
        assert r.status_code == 200
        assert [s["stackName"] for s in r.json()["stacks"]] == ["a"]


class TestAGrantOnOneWorkspaceIsNotAGrantOnAnother:
    """The issue's cross-workspace cases: allowed everything on A, nothing on B."""

    @pytest.mark.parametrize(
        ("what", "method", "suffix"),
        [
            ("read", "get", "/export"),
            ("write", "post", "/import"),
            ("delete", "delete", ""),
            ("decrypt", "post", "/decrypt"),
            ("apply", "post", "/update"),
        ],
    )
    async def test_refused_on_b(self, what, method, suffix) -> None:
        on_a = await _call(method, f"/stacks/default/proj/a{suffix}", {"proj::a": EVERYTHING})
        on_b = await _call(method, f"/stacks/default/proj/b{suffix}", {"proj::a": EVERYTHING})
        assert on_a.status_code not in (403, 404), f"{what} on A should be allowed"
        assert on_b.status_code == 404, f"{what} on B answered {on_b.status_code}"


class TestReadAccessAloneIsReadAccess:
    @pytest.mark.parametrize(
        ("method", "suffix", "required"),
        [r for r in STACK_ROUTES if r[2] not in READ_ONLY],
    )
    async def test_anything_more_is_refused_by_name(self, method, suffix, required) -> None:
        r = await _call(method, f"/stacks/default/proj/a{suffix}", {"proj::a": READ_ONLY})
        assert r.status_code == 403
        # They can see the stack, so saying what is missing gives nothing away.
        assert required in r.json()["message"]

    @pytest.mark.parametrize(("method", "suffix", "required"), STACK_ROUTES)
    async def test_exactly_the_required_capability_is_enough(
        self, method, suffix, required
    ) -> None:
        """Per verb, not per preset: holding read plus the one capability the
        call needs gets it through, which is the capability-model contract."""
        r = await _call(
            method, f"/stacks/default/proj/a{suffix}", {"proj::a": READ_ONLY | {required}}
        )
        assert r.status_code not in (403, 404), f"{method.upper()} {suffix} -> {r.status_code}"


class TestStackInitIsNotANameOracle:
    async def _init(self, grants) -> object:
        db = AsyncMock()
        found = MagicMock()
        found.scalar_one_or_none.return_value = A  # the name is taken
        db.execute.return_value = found
        with patch(f"{MOD}.resolve_workspace_capabilities_for", _caps(grants)):
            async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url="http://t") as c:
                return await c.post(f"{BASE}/stacks/default/proj", json={"stackName": "a"})

    async def test_a_taken_name_you_cannot_read_gets_the_ordinary_refusal(self) -> None:
        r = await self._init({})
        assert r.status_code == 404
        assert "already exists" not in r.json()["message"]

    async def test_a_taken_name_you_can_read_is_reported_as_taken(self) -> None:
        r = await self._init({"proj::a": READ_ONLY})
        assert r.status_code == 409


class TestStartingAnUpdate:
    def _redis(self, *, workspace, kind="update") -> AsyncMock:
        redis = AsyncMock()
        redis.hgetall.return_value = {"workspace_id": str(workspace.id), "kind": kind}
        return redis

    async def test_an_update_begun_on_a_cannot_be_started_through_b(self) -> None:
        """Starting mints the lease that authorizes everything after it, so an
        update must only ever be started on the stack it was begun on."""
        r = await _call(
            "post",
            "/stacks/default/proj/b/update/u-1",
            {"proj::a": EVERYTHING, "proj::b": EVERYTHING},
            redis=self._redis(workspace=A),
        )
        assert r.status_code == 404

    async def test_starting_costs_what_beginning_cost(self) -> None:
        """The kind comes from the record, not the URL: a destroy is started with
        the destroy capability even though the start route is the same for all."""
        r = await _call(
            "post",
            "/stacks/default/proj/a/update/u-1",
            {"proj::a": READ_ONLY | {cap.RUN_APPLY}},
            redis=self._redis(workspace=A, kind="destroy"),
        )
        assert r.status_code == 403
        assert cap.RUN_APPLY_DESTROY in r.json()["message"]

    async def test_the_right_caller_on_the_right_stack_gets_a_lease(self) -> None:
        r = await _call(
            "post",
            "/stacks/default/proj/a/update/u-1",
            {"proj::a": READ_ONLY | {cap.RUN_APPLY}},
            redis=self._redis(workspace=A),
        )
        assert r.status_code == 200
        assert r.json()["token"]

    async def test_polling_an_update_on_another_stack_finds_nothing(self) -> None:
        r = await _call(
            "get",
            "/stacks/default/proj/b/update/u-1",
            {"proj::b": READ_ONLY},
            redis=self._redis(workspace=A),
        )
        assert r.status_code == 404


# ── the lease-authenticated routes ───────────────────────────────────────────


class TestALeaseIsBoundToItsStack:
    def _redis(self, *, workspace, kind="update") -> AsyncMock:
        redis = AsyncMock()
        redis.hgetall.return_value = {
            "workspace_id": str(workspace.id),
            "kind": kind,
            "lease": "good",
        }
        redis.get.return_value = None
        return redis

    @pytest.mark.parametrize(("method", "suffix"), LEASE_ROUTES)
    async def test_a_lease_for_a_is_refused_against_b(self, method, suffix) -> None:
        r = await _call(
            method,
            f"/stacks/default/proj/b{suffix}",
            {},
            headers={"authorization": "update-token good"},
            redis=self._redis(workspace=A),
        )
        assert r.status_code == 403

    @pytest.mark.parametrize(("method", "suffix"), LEASE_ROUTES)
    async def test_and_accepted_against_a(self, method, suffix) -> None:
        r = await _call(
            method,
            f"/stacks/default/proj/a{suffix}",
            {},
            headers={"authorization": "update-token good"},
            redis=self._redis(workspace=A),
        )
        assert r.status_code == 200

    async def test_a_previews_lease_cannot_write_state(self) -> None:
        """Otherwise run:plan, which begins a preview, would buy state:write."""
        r = await _call(
            "patch",
            "/stacks/default/proj/a/update/u-1/checkpoint",
            {},
            headers={"authorization": "update-token good"},
            redis=self._redis(workspace=A, kind="preview"),
        )
        assert r.status_code == 403

    @pytest.mark.parametrize(("method", "suffix"), LEASE_ROUTES)
    async def test_a_bad_lease_says_the_same_thing_whether_or_not_the_stack_exists(
        self, method, suffix
    ) -> None:
        """These calls carry no user. If a missing stack answered 404 and an
        existing one 401, anyone could map which stacks exist without signing in."""
        redis = AsyncMock()
        redis.hgetall.return_value = {}
        hdr = {"authorization": "update-token invented"}
        real = await _call(method, f"/stacks/default/proj/a{suffix}", {}, headers=hdr, redis=redis)
        fake = await _call(
            method, f"/stacks/default/proj/nope{suffix}", {}, headers=hdr, redis=redis
        )
        assert real.status_code == fake.status_code == 401
        assert real.json() == fake.json()


# ── the guard ────────────────────────────────────────────────────────────────


def _routes() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(_ROUTER.read_text())
    out = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "router"
            for d in node.decorator_list
        ):
            out.append(node)
    return out


def _params(fn: ast.AsyncFunctionDef) -> set[str]:
    return {a.arg for a in fn.args.args}


def _is_user_route(fn: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Name)
        and d.func.id == "Depends"
        and d.args
        and isinstance(d.args[0], ast.Name)
        and d.args[0].id == "pulumi_user"
        for d in fn.args.defaults
    )


def _calls(fn: ast.AsyncFunctionDef, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


#: The capability each user route passes to `_authorized_stack`. Adding a route
#: means adding it here — a conscious statement of what it costs.
EXPECTED = {
    "get_stack": "cap.WORKSPACE_READ",
    "delete_stack": "cap.WORKSPACE_DELETE",
    "export_stack": "cap.STATE_READ",
    "import_stack": "cap.STATE_WRITE",
    "encrypt_secret": "cap.STATE_READ",
    "decrypt_secret": "cap.STATE_READ",
    "batch_decrypt": "cap.STATE_READ",
    "begin_preview": "_KIND_CAPABILITY['preview']",
    "begin_up": "_KIND_CAPABILITY['update']",
    "begin_refresh": "_KIND_CAPABILITY['refresh']",
    "begin_destroy": "_KIND_CAPABILITY['destroy']",
    "start_update": "required",
    "get_update_status": "cap.RUN_READ",
}


class TestTheRouterCannotQuietlySkipAuthorization:
    def test_every_user_route_on_a_stack_is_authorized_with_the_pinned_capability(
        self,
    ) -> None:
        seen = {}
        for fn in _routes():
            if not (_is_user_route(fn) and "stack" in _params(fn)):
                continue
            calls = _calls(fn, "_authorized_stack")
            assert calls, f"{fn.name} addresses a stack but never calls _authorized_stack"
            seen[fn.name] = ast.unparse(calls[0].args[3])
        assert seen == EXPECTED

    def test_every_lease_route_binds_its_lease_to_the_stack(self) -> None:
        lease_routes = [
            fn for fn in _routes() if "update_id" in _params(fn) and not _is_user_route(fn)
        ]
        assert len(lease_routes) == 4
        for fn in lease_routes:
            calls = _calls(fn, "_require_lease")
            assert calls and len(calls[0].args) == 4, (
                f"{fn.name} must call _require_lease(request, update_id, db, stack_id) — "
                "the form that binds the lease to the stack in the URL"
            )

    def test_no_route_looks_a_stack_up_without_authorizing(self) -> None:
        """`_find_stack` authorizes no one; only the two gates may call it."""
        offenders = [fn.name for fn in _routes() if _calls(fn, "_find_stack")]
        assert not offenders, f"these call _find_stack directly: {offenders}"

    def test_the_unauthorized_loader_is_gone(self) -> None:
        assert "def _load_stack" not in _ROUTER.read_text()

    def test_the_kinds_cost_what_the_equivalent_terraform_runs_cost(self) -> None:
        from terrapod.api.routers.pulumi_service import _KIND_CAPABILITY

        assert _KIND_CAPABILITY == {
            "preview": cap.RUN_PLAN,
            "update": cap.RUN_APPLY,
            "refresh": cap.RUN_APPLY,
            "destroy": cap.RUN_APPLY_DESTROY,
        }
