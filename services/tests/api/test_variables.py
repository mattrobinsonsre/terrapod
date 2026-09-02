"""Tests for variable and variable set CRUD endpoints with RBAC."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="test@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _mock_workspace(ws_id=None):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = "test-ws"
    ws.execution_backend = "tofu"
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
    return ws


def _mock_var(key="region", value="us-east-1", sensitive=False, ws_id=None, var_id=None):
    var = MagicMock()
    var.id = var_id or uuid.uuid4()
    var.workspace_id = ws_id or uuid.uuid4()
    var.key = key
    var.value = value
    var.sensitive = sensitive
    var.category = "terraform"
    var.hcl = False
    var.value_source = "static"
    var.description = ""
    var.version_id = "abc123"
    var.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    var.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return var


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    # Also override require_admin for varset endpoints
    if "admin" in (user.roles or []):
        app.dependency_overrides[require_admin] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── List Variables ─────────────────────────────────────────────────────


class TestListVariables:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.list_variables")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_list_with_read_perm(self, mock_resolve, mock_list, *mocks):
        mock_resolve.return_value = caps_for_level("read")
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_list.return_value = [var]

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["attributes"]["key"] == "region"
        assert data[0]["attributes"]["value"] == "us-east-1"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.list_variables")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_sensitive_values_masked(self, mock_resolve, mock_list, *mocks):
        mock_resolve.return_value = caps_for_level("read")
        ws = _mock_workspace()
        var = _mock_var(key="secret", sensitive=True, ws_id=ws.id)
        mock_list.return_value = [var]

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        data = resp.json()["data"]
        assert data[0]["attributes"]["value"] is None
        assert data[0]["attributes"]["sensitive"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_list_no_permission_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = caps_for_level(None)
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        assert resp.status_code == 403


# ── Create Variable ────────────────────────────────────────────────────


class TestCreateVariable:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_create_with_write_perm(self, mock_resolve, mock_create, *mocks):
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_create.return_value = var

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={
                    "data": {
                        "attributes": {
                            "key": "region",
                            "value": "us-east-1",
                            "category": "terraform",
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_create_missing_key_returns_422(self, mock_resolve, *mocks):
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={"data": {"attributes": {"value": "val"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_create_read_only_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = caps_for_level("read")
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={"data": {"attributes": {"key": "k", "value": "v"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_create_encryption_error_returns_422(self, mock_resolve, mock_create, *mocks):
        mock_resolve.return_value = caps_for_level("write")
        mock_create.side_effect = ValueError("encryption not configured")
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={"data": {"attributes": {"key": "s", "value": "x", "sensitive": True}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422


# ── Update Variable ────────────────────────────────────────────────────


class TestUpdateVariable:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.update_variable")
    @patch("terrapod.api.routers.variables.variable_service.get_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_update_with_write_perm(self, mock_resolve, mock_get, mock_update, *mocks):
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_get.return_value = var
        mock_update.return_value = var

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}/vars/var-{var.id}",
                json={"data": {"attributes": {"value": "new-val"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.get_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_update_not_found_returns_404(self, mock_resolve, mock_get, *mocks):
        mock_resolve.return_value = caps_for_level("write")
        mock_get.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}/vars/var-{uuid.uuid4()}",
                json={"data": {"attributes": {"value": "x"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Delete Variable ────────────────────────────────────────────────────


class TestDeleteVariable:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.delete_variable")
    @patch("terrapod.api.routers.variables.variable_service.get_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_delete_with_write_perm(self, mock_resolve, mock_get, mock_delete, *mocks):
        mock_resolve.return_value = caps_for_level("write")
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_get.return_value = var

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/v2/workspaces/ws-{ws.id}/vars/var-{var.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 204


# ── Variable Sets ─────────────────────────────────────────────────────


def _mock_varset(name="my-varset", vs_id=None):
    vs = MagicMock()
    vs.id = vs_id or uuid.uuid4()
    vs.name = name
    vs.description = ""
    vs.global_set = False
    vs.priority = False
    vs.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    vs.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    vs.assignment_rule = None
    return vs


class TestVariableSetCRUD:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_varsets(self, *mocks):
        user = _user()
        app, mock_db = _make_app(user)
        vs = _mock_varset()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [vs]
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_varset_requires_admin(self, *mocks):
        """Non-admin cannot create variable sets."""
        user = _user(roles=["everyone"])  # not admin
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/varsets",
                json={"data": {"attributes": {"name": "test"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_varset_admin_returns_201(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/varsets",
                json={"data": {"attributes": {"name": "test-set"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 201

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_varset_requires_admin(self, *mocks):
        user = _user(roles=["everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/v2/varsets/varset-{uuid.uuid4()}",
                headers=_AUTH,
            )
        assert resp.status_code == 403


def _mock_vsvar(key="db_pass", value="s3cr3t", sensitive=True):
    v = MagicMock()
    v.id = uuid.uuid4()
    v.variable_set_id = uuid.uuid4()
    v.key = key
    v.value = value
    v.sensitive = sensitive
    v.category = "terraform"
    v.hcl = False
    v.description = ""
    v.version_id = "old-hash"
    v.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    v.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return v


class TestVarsetVarSensitiveDowngrade:
    """The varset-var PATCH path must clear a previously-hidden secret when a
    var is flipped sensitive→non-sensitive without a fresh value (mirrors the
    workspace-var service-tier rule; the router does it inline)."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables._get_varset")
    async def test_downgrade_without_value_clears_secret(self, mock_get_vs, *_mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        mock_get_vs.return_value = _mock_varset()
        vsv = _mock_vsvar(sensitive=True, value="s3cr3t")
        lookup = MagicMock()
        lookup.scalar_one_or_none.return_value = vsv
        mock_db.execute = AsyncMock(return_value=lookup)
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/varsets/varset-{uuid.uuid4()}/relationships/vars/var-{vsv.id}",
                json={"data": {"attributes": {"sensitive": False}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert vsv.sensitive is False
        assert vsv.value == ""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables._get_varset")
    async def test_downgrade_with_fresh_value_keeps_it(self, mock_get_vs, *_mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        mock_get_vs.return_value = _mock_varset()
        vsv = _mock_vsvar(sensitive=True, value="s3cr3t")
        lookup = MagicMock()
        lookup.scalar_one_or_none.return_value = vsv
        mock_db.execute = AsyncMock(return_value=lookup)
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/varsets/varset-{uuid.uuid4()}/relationships/vars/var-{vsv.id}",
                json={"data": {"attributes": {"sensitive": False, "value": "now-public"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert vsv.value == "now-public"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables._get_varset")
    async def test_non_sensitive_edit_keeps_value(self, mock_get_vs, *_mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        mock_get_vs.return_value = _mock_varset()
        vsv = _mock_vsvar(sensitive=False, value="keep-me")
        lookup = MagicMock()
        lookup.scalar_one_or_none.return_value = vsv
        mock_db.execute = AsyncMock(return_value=lookup)
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/varsets/varset-{uuid.uuid4()}/relationships/vars/var-{vsv.id}",
                json={"data": {"attributes": {"description": "new"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert vsv.value == "keep-me"


class TestVarsetAssignmentRuleValidation:
    """Write-path validation for assignment rules (#1440).

    A rule that does not parse matches nothing, so accepting one silently leaves
    an operator with a set that applies to no workspace and nothing to tell them
    why. These assert the API rejects it at the point of writing instead.
    """

    async def _post(self, attrs: dict):
        app, mock_db = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            return await c.post(
                "/api/v2/organizations/default/varsets",
                json={"data": {"attributes": attrs}},
                headers=_AUTH,
            )

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_unknown_filter_key_is_rejected(self, *mocks):
        resp = await self._post({"name": "x", "assignment-rule": {"nonsense": 1}})
        assert resp.status_code == 422
        assert "assignment-rule" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_global_plus_rule_is_rejected(self, *mocks):
        """Global already means every workspace, so a rule alongside it is a
        contradiction rather than a narrowing — neither field silently wins."""
        resp = await self._post(
            {"name": "x", "global": True, "assignment-rule": {"labels": {"env": "prod"}}}
        )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_a_non_object_rule_is_rejected(self, *mocks):
        resp = await self._post({"name": "x", "assignment-rule": "env=prod"})
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_a_valid_rule_is_accepted(self, *mocks):
        """The negative cases above are only meaningful if a good rule passes."""
        resp = await self._post({"name": "x", "assignment-rule": {"labels": {"env": "prod"}}})
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["assignment-rule"] == {"labels": {"env": "prod"}}

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_all_true_in_a_rule_is_rejected(self, *mocks):
        """`all: true` is the one rule shape that silently covers everything.

        It duplicates `global`, which the API already expresses properly and
        which a reader recognises at a glance — so reject the second spelling
        rather than let a scoped set quietly become estate-wide.
        """
        resp = await self._post({"name": "x", "assignment-rule": {"all": True}})
        assert resp.status_code == 422
        assert "global" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_workspace_ids_in_a_rule_is_rejected(self, *mocks):
        """A literal id list is explicit assignment, not a rule.

        Allowing it would give one outcome two mechanisms, and produce a "rule"
        whose membership can never re-evaluate — the property that makes rules
        worth having.
        """
        resp = await self._post({"name": "x", "assignment-rule": {"workspace_ids": ["ws-1"]}})
        assert resp.status_code == 422
        assert "explicitly" in resp.json()["detail"]


class TestVaultValueSource:
    """Write-time validation for the Vault value source (#1439).

    Validated here rather than only at run time so a malformed reference is an
    error the operator sees while saving, not a run that fails hours later.
    """

    async def _post(self, attrs: dict, ws_id=None):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        ws = MagicMock()
        ws.id = ws_id or uuid.uuid4()
        with (
            patch(
                "terrapod.api.routers.variables._get_workspace",
                new_callable=AsyncMock,
                return_value=ws,
            ),
            patch(
                "terrapod.api.routers.variables.resolve_workspace_capabilities_for",
                new_callable=AsyncMock,
                return_value=caps_for_level("admin"),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.create_variable",
                new_callable=AsyncMock,
                return_value=_mock_var(),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                return await c.post(
                    f"/api/v2/workspaces/ws-{ws.id}/vars",
                    json={"data": {"attributes": attrs}},
                    headers=_AUTH,
                )

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_an_unknown_value_source_is_rejected(self, *mocks):
        resp = await self._post({"key": "T", "value": "x", "value-source": "s3"})
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_a_vault_source_with_a_malformed_reference_is_rejected(self, *mocks):
        resp = await self._post({"key": "T", "value": "not json", "value-source": "vault"})
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_a_vault_reference_missing_coordinates_is_rejected(self, *mocks):
        resp = await self._post(
            {"key": "T", "value": json.dumps({"mount": "kvv2"}), "value-source": "vault"}
        )
        assert resp.status_code == 422
        assert "path" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_a_valid_reference_is_accepted(self, *mocks):
        """The rejections above only mean something if a good reference passes."""
        ref = json.dumps({"mount": "kvv2", "path": "apps/netbox", "field": "apitoken"})
        resp = await self._post({"key": "T", "value": ref, "value-source": "vault"})
        assert resp.status_code == 201

    def test_a_vault_reference_is_shown_not_masked(self):
        """The stored value is a path, not a secret. Masking it would hide
        configuration the operator needs to see while concealing nothing."""
        var = _mock_var(sensitive=True)
        var.value_source = "vault"
        var.value = '{"mount":"kvv2","path":"apps/netbox","field":"apitoken"}'
        from terrapod.api.routers.variables import _var_json

        assert _var_json(var)["attributes"]["value"] == var.value
        assert _var_json(var)["attributes"]["value-source"] == "vault"

    def test_an_ordinary_sensitive_value_is_still_masked(self):
        var = _mock_var(sensitive=True, value="a-real-secret")
        var.value_source = "static"
        from terrapod.api.routers.variables import _var_json

        assert _var_json(var)["attributes"]["value"] is None


class TestVaultAvailability:
    """The probe the UI gates its Vault option on (#1439).

    It decides whether an operator is offered a source at all, so a wrong answer
    either hides a configured feature or offers one that cannot work.
    """

    async def _get(self):
        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            return await c.get("/api/terrapod/v1/vault/availability", headers=_AUTH)

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_disabled_reports_no_instances(self, *mocks):
        """Names must not leak out of a disabled config — and an empty list is
        what makes the UI hide the option rather than offer a broken one."""
        from terrapod.config import VaultConfig, settings

        prior = settings.vault
        settings.vault = VaultConfig(enabled=False)
        try:
            resp = await self._get()
        finally:
            settings.vault = prior
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["enabled"] is False
        assert attrs["instances"] == []

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_enabled_reports_names_and_the_default(self, *mocks):
        from terrapod.config import VaultConfig, settings

        prior = settings.vault
        settings.vault = VaultConfig(
            enabled=True,
            instances=[
                {"name": "a", "address": "https://a"},
                {"name": "b", "address": "https://b", "default": True},
            ],
        )
        try:
            resp = await self._get()
        finally:
            settings.vault = prior
        attrs = resp.json()["data"]["attributes"]
        assert attrs["enabled"] is True
        assert attrs["instances"] == ["a", "b"]
        assert attrs["default-instance"] == "b"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_it_never_exposes_addresses_or_credentials(self, *mocks):
        """Anyone who can write a variable can call this, so it carries only
        what choosing an instance requires."""
        from terrapod.config import VaultConfig, settings

        prior = settings.vault
        settings.vault = VaultConfig(
            enabled=True,
            instances=[
                {
                    "name": "a",
                    "address": "https://vault.internal:8200",
                    "namespace": "team-a",
                    "auth": {"method": "kubernetes", "mount": "kubernetes", "role": "terrapod"},
                }
            ],
        )
        try:
            body = (await self._get()).text
        finally:
            settings.vault = prior
        for leaked in ("vault.internal", "8200", "team-a", "terrapod-role", "kubernetes"):
            assert leaked not in body, f"{leaked!r} leaked from the availability probe"
