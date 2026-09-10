"""Workspaces are created by Terrapod, not by an engine's CLI (#1535).

`pulumi stack init` used to create a Terrapod workspace. Terraform's CLI never
has — `init` looks a workspace up and fails if it is absent — so Pulumi was
governed differently for no reason a user could see, and a CLI could bring a
platform resource into being with no RBAC review and no record of its origin.

Two halves, and both are tested here because neither is safe alone: the refusal
would leave Pulumi workspaces uncreatable without the native `engine` attribute,
and the attribute without the refusal would leave two ways in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.db.session import get_db

pytestmark = pytest.mark.asyncio

PULUMI_BASE = "/api/v1/pulumi/api"


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="a@b.c",
        display_name="A",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _empty_db() -> AsyncMock:
    """A db whose lookups find nothing.

    `execute()` is awaited but its RESULT is sync, so the result has to be a
    MagicMock — a bare AsyncMock hands back a coroutine and the caller never
    reaches its "not found" branch.
    """
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    db.execute.return_value = result

    # The created row is never round-tripped through a real database here, so
    # the columns the serializer reads are filled on add. Without this the
    # assertion under test is masked by a 500 from timestamp formatting.
    def _fill(obj: object) -> None:
        now = datetime.now(UTC)
        for field in ("created_at", "updated_at"):
            if getattr(obj, field, None) is None:
                setattr(obj, field, now)

    db.add = MagicMock(side_effect=_fill)
    return db


def _app(db: object):
    from terrapod.api.app import create_application
    from terrapod.api.dependencies import get_current_user, require_non_runner
    from terrapod.api.routers.pulumi_service import pulumi_user

    app = create_application()
    app.dependency_overrides[pulumi_user] = lambda: _user()
    app.dependency_overrides[require_non_runner] = lambda: _user()
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: db
    return app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _body(name: str = "ws-under-test", **attrs: object) -> dict:
    return {"data": {"attributes": {"name": name, **attrs}}}


class TestPulumiStackInitRefuses:
    """The CLI must not be able to create a platform resource."""

    async def test_it_refuses_and_says_where_workspaces_come_from(self) -> None:
        db = _empty_db()
        async with _client(_app(db)) as c:
            r = await c.post(
                f"{PULUMI_BASE}/stacks/default/proj",
                content=json.dumps({"stackName": "dev"}),
            )

        assert r.status_code == 404
        message = r.json()["message"]
        # The operator's next action has to be in the message: the CLI prints it
        # verbatim and there is nowhere else for them to learn it.
        assert "stack select" in message, message
        assert any(word in message for word in ("UI", "provider", "API")), message

    async def test_it_creates_nothing(self) -> None:
        """The refusal is the point; a 404 that still wrote a row would be worse
        than the behaviour it replaced, because nothing would reveal it."""
        db = _empty_db()
        async with _client(_app(db)) as c:
            await c.post(
                f"{PULUMI_BASE}/stacks/default/proj",
                content=json.dumps({"stackName": "dev"}),
            )

        db.add.assert_not_called()
        db.commit.assert_not_called()

    async def test_an_existing_stack_still_conflicts_for_someone_who_can_see_it(self) -> None:
        """Distinct from the refusal: "already there" and "cannot be created here"
        have different fixes.

        Only for a caller who can read the stack, though (#1550). Telling anyone
        at all that a name is taken would make this an oracle for workspace names
        they have no access to; that side is covered in test_pulumi_authz.py.
        """
        from terrapod.auth import capabilities as cap

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = MagicMock()
        db.execute.return_value = result

        with patch(
            "terrapod.api.routers.pulumi_service.resolve_workspace_capabilities_for",
            AsyncMock(return_value=frozenset({cap.WORKSPACE_READ})),
        ):
            async with _client(_app(db)) as c:
                r = await c.post(
                    f"{PULUMI_BASE}/stacks/default/proj",
                    content=json.dumps({"stackName": "dev"}),
                )
        assert r.status_code == 409


class TestTheNativeSurfaceCarriesTheEngine:
    async def test_omitting_the_engine_yields_terraform(self) -> None:
        """What every existing caller sends, so the change stays additive."""
        db = _empty_db()
        with patch("terrapod.redis.client.publish_workspace_event", AsyncMock()):
            async with _client(_app(db)) as c:
                r = await c.post("/api/v1/workspaces", json=_body())

        assert r.status_code == 201, r.text
        assert db.add.call_args[0][0].engine == "terraform"

    async def test_an_enabled_engine_is_accepted(self) -> None:
        db = _empty_db()
        with (
            patch("terrapod.engines.known_engines", return_value=("pulumi", "terraform")),
            patch("terrapod.redis.client.publish_workspace_event", AsyncMock()),
        ):
            async with _client(_app(db)) as c:
                # A Pulumi workspace's name is `project::stack` — see
                # TestTheNameRuleKnowsTheEngine below.
                r = await c.post(
                    "/api/v1/workspaces",
                    json=_body(name="proj::dev", engine="pulumi"),
                )

        assert r.status_code == 201, r.text
        assert db.add.call_args[0][0].engine == "pulumi"

    async def test_a_gated_off_engine_is_refused(self) -> None:
        """Off means absent (#1429): the engine is not offered, so it is not in
        the list and the error names how to turn it on.

        The name is a valid Pulumi one so the 422 can only be about the gate —
        a name the rule would reject anyway would pass this test for the wrong
        reason.
        """
        db = _empty_db()
        with patch("terrapod.engines.known_engines", return_value=("terraform",)):
            async with _client(_app(db)) as c:
                r = await c.post(
                    "/api/v1/workspaces",
                    json=_body(name="proj::dev", engine="pulumi"),
                )

        assert r.status_code == 422
        assert "engines.pulumi.enabled" in json.dumps(r.json())
        db.add.assert_not_called()

    async def test_an_unknown_engine_is_refused(self) -> None:
        db = _empty_db()
        async with _client(_app(db)) as c:
            r = await c.post("/api/v1/workspaces", json=_body(engine="nonesuch"))

        assert r.status_code == 422
        db.add.assert_not_called()


class TestTheTfeSurfaceStaysTerraformOnly:
    async def test_it_ignores_an_engine_in_the_body(self) -> None:
        """The compatibility surface cannot express another engine — a client
        there could not see the workspace it had just made.

        Ignoring rather than rejecting: the attribute is meaningless on this
        surface, and TFE clients routinely round-trip attributes they do not
        understand.
        """
        db = _empty_db()
        with (
            patch("terrapod.engines.known_engines", return_value=("pulumi", "terraform")),
            patch("terrapod.redis.client.publish_workspace_event", AsyncMock()),
        ):
            async with _client(_app(db)) as c:
                r = await c.post(
                    "/api/tfe/v2/organizations/default/workspaces",
                    json=_body(engine="pulumi"),
                )

        assert r.status_code == 201, r.text
        assert db.add.call_args[0][0].engine == "terraform"


class TestTheNameRuleKnowsTheEngine:
    """A Pulumi workspace is named `project::stack`.

    That shape is not decoration: it is what `pulumi stack select` resolves to,
    composed by `_stack_workspace_name`. The plain rule rejects the separator, so
    without this the whole feature is unreachable — every attempt to create the
    workspace `stack select` needs would 422. `stack init` never hit it because
    it built the row directly, which is the bypass #1535 removed.
    """

    @pytest.mark.parametrize(
        ("name", "engine", "ok"),
        [
            ("proj::dev", "pulumi", True),
            ("proj", "pulumi", False),  # one part — nothing to select
            ("a::b::c", "pulumi", False),  # ambiguous split
            ("::dev", "pulumi", False),  # empty project
            ("plain", "terraform", True),
            ("proj::dev", "terraform", False),  # the separator is Pulumi's alone
        ],
    )
    def test_the_rule(self, name: str, engine: str, ok: bool) -> None:
        from terrapod.services.workspace_name import validate_workspace_name

        if ok:
            assert validate_workspace_name(name, engine) == name
        else:
            with pytest.raises(ValueError):
                validate_workspace_name(name, engine)

    async def test_a_pulumi_workspace_can_be_created_with_a_composed_name(self) -> None:
        """The end-to-end version of the above, through the endpoint."""
        db = _empty_db()
        with (
            patch("terrapod.engines.known_engines", return_value=("pulumi", "terraform")),
            patch("terrapod.redis.client.publish_workspace_event", AsyncMock()),
        ):
            async with _client(_app(db)) as c:
                r = await c.post(
                    "/api/v1/workspaces",
                    json={"data": {"attributes": {"name": "proj::dev", "engine": "pulumi"}}},
                )

        assert r.status_code == 201, r.text
        assert db.add.call_args[0][0].name == "proj::dev"


class TestPulumiBindPlan:
    """`pulumi-bind-plan` (#1553): off by default, Pulumi only.

    Binding the update to the preview's saved plan rests on Pulumi's update plans,
    still experimental upstream, so a workspace opts in; any other engine is
    refused rather than handed a setting that would do nothing.
    """

    async def _create(self, body: dict):
        db = _empty_db()
        with (
            patch("terrapod.engines.known_engines", return_value=("pulumi", "terraform")),
            patch("terrapod.redis.client.publish_workspace_event", AsyncMock()),
        ):
            async with _client(_app(db)) as c:
                r = await c.post("/api/v1/workspaces", json=body)
        return r, db

    async def test_off_unless_asked(self) -> None:
        r, db = await self._create(_body(name="proj::dev", engine="pulumi"))
        assert r.status_code == 201, r.text
        assert db.add.call_args[0][0].pulumi_bind_plan is False
        assert r.json()["data"]["attributes"]["pulumi-bind-plan"] is False

    async def test_a_pulumi_workspace_can_opt_in(self) -> None:
        r, db = await self._create(
            _body(name="proj::dev", engine="pulumi", **{"pulumi-bind-plan": True})
        )
        assert r.status_code == 201, r.text
        assert db.add.call_args[0][0].pulumi_bind_plan is True
        assert r.json()["data"]["attributes"]["pulumi-bind-plan"] is True

    async def test_any_other_engine_is_refused(self) -> None:
        r, db = await self._create(_body(engine="terraform", **{"pulumi-bind-plan": True}))
        assert r.status_code == 422
        assert "Pulumi" in r.json()["detail"]
        db.add.assert_not_called()

    async def test_a_non_boolean_is_refused(self) -> None:
        r, _ = await self._create(
            _body(name="proj::dev", engine="pulumi", **{"pulumi-bind-plan": "yes"})
        )
        assert r.status_code == 422

    def test_the_update_path_applies_the_same_rule(self) -> None:
        """The update route shares the validator, so the rule holds there too —
        on the TFE surface today, and on the native one when #1554 adds it."""
        from fastapi import HTTPException

        from terrapod.api.routers.tfe_v2 import _validate_pulumi_bind_plan

        assert _validate_pulumi_bind_plan(True, "pulumi") is True
        assert _validate_pulumi_bind_plan(False, "terraform") is False
        with pytest.raises(HTTPException) as exc:
            _validate_pulumi_bind_plan(True, "terraform")
        assert exc.value.status_code == 422
