"""Route-level proof that the connection gates fire (GHSA-v8g7-pqrj-8mcm).

The predicate has unit tests and the routers have source-introspection guards, and
a mutation review showed both can be satisfied while the gate does nothing: an
`if False and not await may_reference_connection(...)` keeps every asserted
substring, and three inert gates keep the call count. Only driving the route
catches that.

Covers the two sites that had no behavioural test at all — the workspace PATCH
(which carries change-detection logic the create path does not) and the three
registry-module sites, which are reachable by any authenticated user.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer t"}
_CONN = "vcs-11111111-2222-3333-4444-555555555555"


def _user(roles: list[str] | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        email="someone@test.com",
        display_name="Someone",
        roles=roles if roles is not None else ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _app(db: AsyncMock | None = None):
    app = create_app()
    user = _user()
    app.dependency_overrides[get_current_user] = lambda: user
    db = db or AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


def _refuse():
    """Patch the predicate to refuse, as it would for an unauthorized caller."""
    return patch(
        "terrapod.services.vcs_connection_rbac.may_reference_connection",
        new_callable=AsyncMock,
        return_value=False,
    )


def _boot():
    return (
        patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
        patch("terrapod.api.app.init_redis"),
        patch("terrapod.api.app.init_db"),
    )


class TestTheWorkspacePatchGateFires:
    def _ws(self):
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.vcs_connection_id = None
        ws.owner_email = "someone@test.com"
        ws.name = "w"
        return ws

    async def _patch_workspace(self, body):
        ws = self._ws()
        app, db = _app()
        result = MagicMock()
        result.scalar_one_or_none.return_value = ws
        db.execute.return_value = result
        a, b, c_ = _boot()
        with (
            a,
            b,
            c_,
            _refuse(),
            patch(
                "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
                return_value=caps_for_level("admin"),
            ),
            patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as cl:
                resp = await cl.patch(f"/api/v2/workspaces/ws-{ws.id}", json=body, headers=_AUTH)
        return resp, db

    async def test_the_attribute_spelling_is_refused(self):
        resp, db = await self._patch_workspace(
            {"data": {"attributes": {"vcs-connection-id": _CONN}}}
        )
        assert resp.status_code == 403, resp.text
        db.commit.assert_not_awaited()

    async def test_the_relationship_spelling_is_refused_too(self):
        """The relationship is canonical and wins, so gating one leaves the other open."""
        resp, db = await self._patch_workspace(
            {
                "data": {
                    "attributes": {},
                    "relationships": {
                        "vcs-connection": {"data": {"type": "vcs-connections", "id": _CONN}}
                    },
                }
            }
        )
        assert resp.status_code == 403, resp.text
        db.commit.assert_not_awaited()

    async def test_a_patch_that_does_not_touch_the_connection_never_consults_the_gate(self):
        """The gate fires on a CHANGE only.

        Asserted by watching the predicate rather than the status code: a PATCH
        that leaves the connection alone serializes a whole workspace on the way
        out, and mocking that faithfully would test the serializer rather than the
        gate. "Was the authorization question even asked?" is the property — if it
        is, an operator who administers a VCS-connected workspace today starts
        getting 403s for editing an unrelated field.
        """
        ws = self._ws()
        app, _db = _app()
        result = MagicMock()
        result.scalar_one_or_none.return_value = ws
        _db.execute.return_value = result
        a, b, c_ = _boot()
        with (
            a,
            b,
            c_,
            patch(
                "terrapod.services.vcs_connection_rbac.may_reference_connection",
                new_callable=AsyncMock,
                return_value=False,
            ) as gate,
            patch(
                "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
                return_value=caps_for_level("admin"),
            ),
            patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as cl:
                try:
                    await cl.patch(
                        f"/api/v2/workspaces/ws-{ws.id}",
                        json={"data": {"attributes": {"auto-apply": True}}},
                        headers=_AUTH,
                    )
                except Exception:
                    # The mock workspace cannot be serialized into the response,
                    # which is downstream of the gate and irrelevant to it: by the
                    # time the serializer runs, the authorization question has
                    # either been asked or not. Swallowed so the assertion below is
                    # what decides the test.
                    pass
        gate.assert_not_awaited()


class TestTheRegistryModuleGateFires:
    """Module creation is open to any authenticated user and the creator becomes
    owner, so an ungated connection reference here is the same escalation as on a
    workspace — through a door the workspace gate cannot see.
    """

    async def test_create_is_refused(self):
        app, db = _app()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.scalars.return_value.first.return_value = MagicMock()  # the conn exists
        db.execute.return_value = result
        a, b, c_ = _boot()
        with a, b, c_, _refuse():
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as cl:
                resp = await cl.post(
                    "/api/terrapod/v1/registry-modules",
                    json={
                        "data": {
                            "type": "registry-modules",
                            "attributes": {
                                "name": "vpc",
                                "provider": "aws",
                                "vcs_connection_id": _CONN,
                                "vcs_repo_url": "https://github.com/someone-else/private",
                            },
                        }
                    },
                    headers=_AUTH,
                )
        assert resp.status_code == 403, resp.text
        assert "every repository" in resp.text
        db.commit.assert_not_awaited()

    async def test_every_site_that_accepts_a_connection_also_authorizes_it(self):
        """A behavioural test per site would need three different fixtures; this
        pins the pairing instead, which is what the count-based guard could not see:
        each existence check must be followed by an authorization call before the
        id is assigned."""
        import inspect
        import re

        from terrapod.api.routers import registry_modules

        src = inspect.getsource(registry_modules)
        # every "not found" raise, and what follows it before the next assignment
        for m in re.finditer(
            r'detail="VCS connection not found"\)\n(.{0,900}?)'
            r"module\.vcs_connection_id = conn_id",
            src,
            re.S,
        ):
            between = m.group(1)
            assert "may_reference_connection(" in between, (
                "a connection id is assigned without an authorization call between "
                f"the existence check and the assignment:\n{between[:200]}"
            )
            assert "status_code=403" in between, (
                "the authorization result is not turned into a refusal"
            )


class TestTheEmptyLineageCannotSkipTheCheck:
    """The first guard asserted one spelling and a reworded condition passed it.

    `latest.lineage and lineage and latest.lineage != lineage` and
    `lineage and latest.lineage and latest.lineage != lineage` are the same bypass,
    and a substring check for the first misses the second. What matters is the
    property: the UPLOADED lineage must not be tested for truthiness anywhere in
    that comparison, because an empty one is attacker-supplied rather than a legacy
    artefact. So this parses the condition instead of matching text.
    """

    def test_the_uploaded_lineage_is_never_a_truthiness_term(self):
        import ast
        import inspect

        from terrapod.api.routers import run_artifacts

        tree = ast.parse(inspect.getsource(run_artifacts))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.And):
                continue
            # the comparison this guard is about
            if not any(
                isinstance(v, ast.Compare)
                and ast.unparse(v).replace(" ", "") == "latest.lineage!=lineage"
                for v in node.values
            ):
                continue
            for v in node.values:
                if isinstance(v, ast.Name) and v.id == "lineage":
                    offenders.append(ast.unparse(node))
        assert not offenders, (
            "the uploaded lineage is a bare truthiness term in the mismatch guard, "
            "so omitting `lineage` from the state JSON skips the check and a foreign "
            f"state at serial+1 is accepted:\n  {offenders}"
        )
