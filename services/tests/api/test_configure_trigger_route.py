"""Triggering a configure through the API, and who may (#1974, #1976).

Two halves, and the second is the reason this file exists.

**The refusals.** A configure is a third kind of run, so the create route has
combinations to reject that an apply does not: a plan-only configure (no such
thing -- ansible's dry run is `--check`, its own option), a configure against a
speculative configuration version (an unmerged branch's playbook against real
hosts, with no plan to review), a configure on a workspace that declares no
configure operation, and the plan/apply run options on a run that has no
plan/apply half. Every one is refused rather than ignored, which is the #2116
class: a caller's attribute silently doing nothing.

**The gate.** Running a playbook against production hosts is at least as
consequential as an apply -- it can restart services, rotate credentials and
reboot machines -- so `run:plan` must not reach it, and a RUNNER TOKEN must not
reach it at all. The negative paths matter more than the positive one here:
a runner token carries `everyone` plus an implicit registry read, and a gate
added without testing that principal has broken every agent run in this
repository before (#1976's own scope says so).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db
from terrapod.services import ansible_ee_service, inventory_resolve

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(*, roles=None, auth_method="session", run_id=None):
    return AuthenticatedUser(
        email="test@example.com",
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method=auth_method,
        run_id=run_id,
    )


def _ws(*, configure_enabled=True, plan_apply_enabled=True, configure_after_apply=False):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "cfg-ws"
    ws.engine = "terraform"
    ws.vcs_connection_id = None
    ws.vcs_repo_url = ""
    ws.catalog_item_id = None
    ws.plan_expiry_seconds = None
    ws.locked = False
    ws.execution_mode = "agent"
    # Explicit rather than truthy MagicMock attributes: these decide which
    # refusals the route reaches, so a fixture that left them as Mocks would
    # make every test here pass for the wrong reason.
    ws.plan_apply_enabled = plan_apply_enabled
    ws.configure_enabled = configure_enabled
    ws.configure_after_apply = configure_after_apply
    ws.auto_configure_enabled = False
    ws.default_playbook = "site.yml"
    ws.default_limit = ""
    # Same reason as the pair above, one layer down: `create_run` now resolves
    # the EE image for a configure, and `effective_ee_image` treats a truthy
    # `ee_label` as an operator label it must find in the Helm map. A MagicMock
    # is truthy, so leaving these would make every configure test here fail as
    # an unknown execution environment rather than reaching its own assertion.
    ws.ansible_version = ""
    ws.ansible_execution_environment = ""
    return ws


def _cv(*, speculative=False):
    """A configuration version the route will find for a non-VCS workspace.

    `speculative=False` explicitly, and that is not boilerplate: a MagicMock
    attribute is truthy, so a fixture that left it alone made `create_run`
    refuse EVERY configure as "against a speculative configuration version" --
    the right refusal for the wrong reason, which is how a test comes to pass
    while proving something else.
    """
    cv = MagicMock()
    cv.id = uuid.uuid4()
    cv.workspace_id = None  # set by `_app`, which knows the workspace
    cv.status = "uploaded"
    cv.speculative = speculative
    return cv


def _app(user, ws, *, cv=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    cv = cv if cv is not None else _cv()
    cv.workspace_id = ws.id
    db = AsyncMock()
    found = MagicMock()
    found.scalar_one_or_none.return_value = ws
    found.scalars.return_value.first.return_value = None
    found.scalars.return_value.all.return_value = []
    found.all.return_value = []
    db.execute = AsyncMock(return_value=found)

    async def _get(model, ident, *a, **kw):
        # Dispatched on the model, because the route asks for both and they are
        # different objects: the workspace, and the configuration version it
        # falls back to for a workspace with no VCS binding.
        return cv if getattr(model, "__name__", "") == "ConfigurationVersion" else ws

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


async def _post(app, attrs, ws):
    body = {
        "data": {
            "attributes": attrs,
            "relationships": {"workspace": {"data": {"id": f"ws-{ws.id}", "type": "workspaces"}}},
        }
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.post("/api/v1/runs", json=body, headers=_AUTH)


_BOOT = (
    patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
    patch("terrapod.api.app.init_redis"),
    patch("terrapod.api.app.init_db"),
)


def _boot(fn):
    for p in _BOOT:
        fn = p(fn)
    return fn


class TestWhoMayTriggerAConfigure:
    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_plan_tier_is_refused(self, mock_caps, *_):
        """`run:plan` is not enough, and this is the whole of #1976's answer:
        a configure is apply-equivalent, so it sits at the write tier."""
        mock_caps.return_value = caps_for_level("plan")
        ws = _ws()
        app, _db = _app(_user(), ws)

        resp = await _post(app, {"is-configure": True, "plan-only": False}, ws)

        assert resp.status_code == 403
        assert cap.RUN_APPLY in resp.json()["detail"]

    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_runner_token_is_refused(self, mock_caps, *_):
        """A runner Job must not be able to start a configure (#1976).

        A runner token carries `everyone` plus an implicit registry read, so it
        resolves to the read floor -- which holds neither `run:apply` nor
        `run:plan`. Asserted rather than assumed, because every capability gate
        in this repository that was written without this principal in mind has
        broken agent runs.
        """
        mock_caps.return_value = caps_for_level("read")
        ws = _ws()
        run_id = uuid.uuid4()
        app, _db = _app(
            _user(auth_method="runner_token", run_id=run_id),
            ws,
        )

        resp = await _post(app, {"is-configure": True, "plan-only": False}, ws)

        assert resp.status_code == 403

    @_boot
    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_write_tier_may(self, mock_caps, mock_create, mock_queue, *_):
        """The positive path, so the refusals above are not passing because the
        route refuses everyone."""
        mock_caps.return_value = caps_for_level("write")
        ws = _ws()
        run = MagicMock()
        run.id = uuid.uuid4()
        mock_create.return_value = run
        mock_queue.return_value = run
        app, _db = _app(_user(), ws)

        with patch("terrapod.api.routers.runs._run_json", return_value={"data": {}}):
            resp = await _post(app, {"is-configure": True, "plan-only": False}, ws)

        assert resp.status_code == 201, resp.text
        assert mock_create.await_args.kwargs["is_configure"] is True

    @_boot
    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_plan_on_the_same_workspace_still_needs_only_plan(
        self, mock_caps, mock_create, mock_queue, *_
    ):
        """The gate must not have widened. A configure needing write does not
        make a plan need write, and a workspace that has both operations is
        where that could regress unnoticed."""
        mock_caps.return_value = caps_for_level("plan")
        ws = _ws()
        run = MagicMock()
        run.id = uuid.uuid4()
        mock_create.return_value = run
        mock_queue.return_value = run
        app, _db = _app(_user(), ws)

        with patch("terrapod.api.routers.runs._run_json", return_value={"data": {}}):
            resp = await _post(app, {"plan-only": True}, ws)

        assert resp.status_code == 201, resp.text
        assert mock_create.await_args.kwargs["is_configure"] is False


class TestTheIncoherentRequestsAreRefused:
    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_plan_only_and_configure(self, mock_caps, *_):
        mock_caps.return_value = caps_for_level("write")
        ws = _ws()
        app, _db = _app(_user(), ws)

        resp = await _post(app, {"is-configure": True, "plan-only": True}, ws)

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "plan-only" in detail and "check" in detail

    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_configure_on_a_workspace_that_declares_none(self, mock_caps, *_):
        mock_caps.return_value = caps_for_level("write")
        ws = _ws(configure_enabled=False)
        app, _db = _app(_user(), ws)

        resp = await _post(app, {"is-configure": True, "plan-only": False}, ws)

        assert resp.status_code == 422
        assert "configure-enabled" in resp.json()["detail"]

    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_plan_apply_option_on_a_standalone_configure(self, mock_caps, *_):
        """`-target` narrows a plan; `--limit` narrows a configure. Accepting
        the first on a configure would store a flag nothing reads."""
        mock_caps.return_value = caps_for_level("write")
        ws = _ws()
        app, _db = _app(_user(), ws)

        resp = await _post(
            app,
            {"is-configure": True, "plan-only": False, "target-addrs": ["null_resource.a"]},
            ws,
        )

        assert resp.status_code == 422
        assert "target-addrs" in resp.json()["detail"]

    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_playbook_on_a_run_that_is_not_a_configure(self, mock_caps, *_):
        """Refused at the route rather than dropped. The likeliest way to get
        here is a workspace with no configure operation, where silently
        ignoring the playbook would run a plan and say nothing."""
        mock_caps.return_value = caps_for_level("write")
        ws = _ws(configure_enabled=False)
        app, _db = _app(_user(), ws)

        resp = await _post(app, {"plan-only": False, "playbook": "patch.yml"}, ws)

        assert resp.status_code == 422
        assert "playbook" in resp.json()["detail"]

    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_limit_on_a_run_that_is_not_a_configure(self, mock_caps, *_):
        mock_caps.return_value = caps_for_level("write")
        ws = _ws(configure_enabled=False)
        app, _db = _app(_user(), ws)

        resp = await _post(app, {"plan-only": False, "limit": "canary"}, ws)

        assert resp.status_code == 422
        assert "limit" in resp.json()["detail"]

    @_boot
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_a_configure_against_a_speculative_configuration_version(self, mock_caps, *_):
        """An unmerged branch's playbook must not run against real hosts.

        It is refused rather than forced plan-only, which is what happens to an
        apply-capable run here: a configure made plan-only would be the
        contradiction the chokepoint rejects, reached by a different door --
        and the refusal in front of that forcing reads the caller's own
        arguments, so it cannot see a value forced afterwards.
        """
        mock_caps.return_value = caps_for_level("write")
        ws = _ws()
        app, _db = _app(_user(), ws, cv=_cv(speculative=True))

        resp = await _post(app, {"is-configure": True, "plan-only": False}, ws)

        assert resp.status_code == 422
        assert "speculative" in resp.json()["detail"]


class TestTheOptionsReachTheRun:
    @_boot
    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_playbook_and_limit_are_passed_through(
        self, mock_caps, mock_create, mock_queue, *_
    ):
        """The attribute an operator sent has to be what the run carries, or
        the override is decoration."""
        mock_caps.return_value = caps_for_level("write")
        ws = _ws()
        run = MagicMock()
        run.id = uuid.uuid4()
        mock_create.return_value = run
        mock_queue.return_value = run
        app, _db = _app(_user(), ws)

        with patch("terrapod.api.routers.runs._run_json", return_value={"data": {}}):
            resp = await _post(
                app,
                {
                    "is-configure": True,
                    "plan-only": False,
                    "playbook": "patch.yml",
                    "limit": "canary",
                },
                ws,
            )

        assert resp.status_code == 201, resp.text
        kwargs = mock_create.await_args.kwargs
        assert kwargs["playbook"] == "patch.yml"
        assert kwargs["ansible_limit"] == "canary"

    @_boot
    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_absent_options_stay_empty(self, mock_caps, mock_create, mock_queue, *_):
        """Empty means "the workspace's default", which is what a git-driven
        configure runs -- so the route must not substitute anything here."""
        mock_caps.return_value = caps_for_level("write")
        ws = _ws()
        run = MagicMock()
        run.id = uuid.uuid4()
        mock_create.return_value = run
        mock_queue.return_value = run
        app, _db = _app(_user(), ws)

        with patch("terrapod.api.routers.runs._run_json", return_value={"data": {}}):
            resp = await _post(app, {"is-configure": True, "plan-only": False}, ws)

        assert resp.status_code == 201, resp.text
        kwargs = mock_create.await_args.kwargs
        assert kwargs["playbook"] == ""
        assert kwargs["ansible_limit"] == ""


class TestTheCapabilityIsOneProducer:
    """The rule is a function, and both the create route and the retry route
    read it (#1976).

    It was inlined at both, and a third kind of run is exactly how two inlined
    copies come to disagree -- which is the defect `confirm_capability` was
    written to fix for the Slack approve button.
    """

    @pytest.mark.parametrize(
        ("plan_only", "is_destroy", "is_configure", "expected"),
        [
            (True, False, False, cap.RUN_PLAN),
            (False, False, False, cap.RUN_APPLY),
            (False, True, False, cap.RUN_APPLY_DESTROY),
            (False, False, True, cap.RUN_APPLY),
            # A configure is checked first, because a configure can never be
            # plan-only and must never be gated as a destroy.
            (True, False, True, cap.RUN_APPLY),
            (False, True, True, cap.RUN_APPLY),
        ],
    )
    def test_the_matrix(self, plan_only, is_destroy, is_configure, expected):
        assert (
            cap.create_run_capability(
                plan_only=plan_only, is_destroy=is_destroy, is_configure=is_configure
            )
            == expected
        )

    def test_both_routes_call_it(self):
        """A source check because the property is that neither route restates
        the rule -- the thing that drifted before."""
        import ast
        import pathlib

        from terrapod.api.routers import runs as runs_router

        src = pathlib.Path(runs_router.__file__).read_text()
        tree = ast.parse(src)
        for name in ("create_run", "_retry_capability"):
            fn = next(
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
            )
            calls = [
                c
                for c in ast.walk(fn)
                if isinstance(c, ast.Call)
                and (getattr(c.func, "attr", None) or getattr(c.func, "id", None))
                == "create_run_capability"
            ]
            assert calls, f"`{name}` does not use the shared capability producer"
            # And it is handed the run's kind rather than deciding again.
            assert {k.arg for k in calls[0].keywords} == {
                "plan_only",
                "is_destroy",
                "is_configure",
            }, f"`{name}` calls it with the wrong arguments"


class TestTheTwoRefusalsThatUsedToBe500s:
    """Both were raised deliberately and translated by nothing (#1972).

    Found by a live run, not by a test: the first configure ever created died
    with `Internal server error` and no reason, while the READ route answered
    503 naming the offending file. One condition, two paths, two answers -- and
    the one an operator reaches from the Configure button was the uninformative
    one.
    """

    @_boot
    async def test_an_unparseable_inventory_is_refused_with_its_reason(self, *_):
        ws = _ws(plan_apply_enabled=False)
        app, _db = _app(_user(roles=["admin"]), ws)
        reason = "ansible-inventory failed (exit 1): could not parse site.yml"
        with patch.object(
            inventory_resolve,
            "write_run_artifact",
            AsyncMock(side_effect=inventory_resolve.InventoryResolutionFailed(reason)),
        ):
            r = await _post(app, {"is-configure": True}, ws)
        assert r.status_code == 503, r.text
        # The REASON, not a generic string: `create_run`'s own comment promises
        # "the refusal reaches whoever asked for the run", and a 500 body does
        # not tell them which file ansible refused.
        assert "site.yml" in r.json()["errors"][0]["detail"]

    @_boot
    async def test_an_unknown_execution_environment_is_refused_naming_the_field(self, *_):
        ws = _ws(plan_apply_enabled=False)
        app, _db = _app(_user(roles=["admin"]), ws)
        with patch.object(
            ansible_ee_service,
            "effective_ee_image",
            MagicMock(
                side_effect=ansible_ee_service.UnknownExecutionEnvironment(
                    "ansible-execution-environment 'hardened' is not configured on this deployment"
                )
            ),
        ):
            r = await _post(app, {"is-configure": True}, ws)
        # 422 rather than 503: the map is local configuration, so unlike a
        # resolution failure "cannot tell" does not arise. Its own docstring
        # asks for exactly this.
        assert r.status_code == 422, r.text
        assert "hardened" in r.json()["errors"][0]["detail"]
