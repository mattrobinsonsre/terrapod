"""Tests for variable and variable set CRUD endpoints with RBAC."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    ws.catalog_item_id = None
    ws.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
    return ws


def _mock_var(
    key="region",
    value="us-east-1",
    sensitive=False,
    ws_id=None,
    var_id=None,
    apply_only=False,
    configure_only=False,
):
    var = MagicMock()
    var.id = var_id or uuid.uuid4()
    var.workspace_id = ws_id or uuid.uuid4()
    var.key = key
    var.value = value
    var.sensitive = sensitive
    var.apply_only = apply_only
    var.configure_only = configure_only
    var.category = "terraform"
    var.structured = False
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
        mock_db.add = MagicMock()  # AsyncSession.add is synchronous.
        mock_db.execute.return_value = MagicMock()  # Result.scalar_one_or_none is synchronous.
        mock_db.get.return_value = None
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


@pytest.mark.usefixtures("_no_boot")
class TestCatalogWorkspaceVariableValues:
    @pytest.mark.parametrize("prefix", ["/api/tfe/v2", "/api/v2", "/api/v1", "/api/terrapod/v1"])
    async def test_current_visible_catalog_inputs_use_stored_sensitivity(self, prefix):
        ws = _mock_workspace()
        ws.catalog_item_id = uuid.uuid4()
        ordinary = _mock_var(key="terrapod_hostname", value="https://example.com", ws_id=ws.id)
        ordinary.category = "native"
        token = _mock_var(key="terrapod_token", value="secret-token", sensitive=True, ws_id=ws.id)
        token.category = "native"
        hidden = _mock_var(key="fixed", value="admin-only", ws_id=ws.id)
        hidden.category = "native"
        item = MagicMock(
            variable_options=[{"name": "fixed", "hidden": True, "default": "admin-only"}],
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        app, db = _make_app(_user())
        db.get.return_value = item
        cv_result = MagicMock()
        cv_result.scalar_one_or_none.return_value = datetime(2026, 1, 2, tzinfo=UTC)
        db.execute.return_value = cv_result
        with (
            patch("terrapod.api.routers.variables._get_workspace", AsyncMock(return_value=ws)),
            patch(
                "terrapod.api.routers.variables.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.list_variables",
                AsyncMock(return_value=[ordinary, token, hidden]),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"{prefix}/workspaces/ws-{ws.id}/vars", headers=_AUTH)

        assert resp.status_code == 200, resp.text
        attrs = {row["attributes"]["key"]: row["attributes"] for row in resp.json()["data"]}
        assert resp.json()["meta"]["pagination"]["total-count"] == 3
        assert attrs["terrapod_hostname"]["value"] == "https://example.com"
        assert attrs["terrapod_hostname"]["sensitive"] is False
        assert attrs["terrapod_hostname"]["version-id"] == "abc123"
        for key in ("terrapod_token", "fixed"):
            assert attrs[key]["value"] is None
            assert attrs[key]["sensitive"] is True
            assert attrs[key]["version-id"] is None
        assert "secret-token" not in resp.text
        assert "admin-only" not in resp.text

    async def test_catalog_item_edit_never_declassifies_a_prior_hidden_value(self):
        ws = _mock_workspace()
        ws.catalog_item_id = uuid.uuid4()
        var = _mock_var(key="formerly_hidden", value="admin-only", ws_id=ws.id)
        item = MagicMock(variable_options=[], updated_at=datetime(2026, 1, 3, tzinfo=UTC))
        app, db = _make_app(_user())
        db.get.return_value = item
        cv_result = MagicMock()
        # Even a later successful reconfigure cannot prove the old value was not
        # the fixed default when this instance was first created.
        cv_result.scalar_one_or_none.return_value = datetime(2026, 1, 4, tzinfo=UTC)
        db.execute.return_value = cv_result
        with (
            patch("terrapod.api.routers.variables._get_workspace", AsyncMock(return_value=ws)),
            patch(
                "terrapod.api.routers.variables.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.list_variables",
                AsyncMock(return_value=[var]),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"][0]["attributes"]
        assert attrs["value"] is None
        assert attrs["sensitive"] is True
        assert attrs["version-id"] is None
        assert "admin-only" not in resp.text

    @pytest.mark.parametrize(
        ("prefix", "category", "wire_category"),
        [
            ("/api/tfe/v2", "native", "terraform"),
            ("/api/v2", "terraform", "terraform"),
            ("/api/v1", "native", "native"),
            ("/api/terrapod/v1", "terraform", "terraform"),
        ],
    )
    async def test_unclassifiable_catalog_value_not_returned_on_list(
        self, prefix, category, wire_category
    ):
        ws = _mock_workspace()
        ws.catalog_item_id = uuid.uuid4()
        var = _mock_var(key="hidden_input", value="old-secret", ws_id=ws.id)
        var.category = category
        app, _ = _make_app(_user())
        with (
            patch(
                "terrapod.api.routers.variables._get_workspace",
                new_callable=AsyncMock,
                return_value=ws,
            ),
            patch(
                "terrapod.api.routers.variables.resolve_workspace_capabilities_for",
                new_callable=AsyncMock,
                return_value=caps_for_level("read"),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.list_variables",
                new_callable=AsyncMock,
                return_value=[var],
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"{prefix}/workspaces/ws-{ws.id}/vars", headers=_AUTH)

        assert resp.status_code == 200, resp.text
        assert resp.json()["meta"]["pagination"]["total-count"] == 1
        attrs = resp.json()["data"][0]["attributes"]
        assert attrs["value"] is None
        assert attrs["sensitive"] is True
        assert attrs["version-id"] is None
        assert attrs["category"] == wire_category
        assert var.value == "old-secret"
        assert var.sensitive is False
        assert var.version_id == "abc123"

    @pytest.mark.parametrize(
        ("catalog_managed", "category", "source", "sensitive", "expected"),
        [
            (False, "native", "static", False, "plain-value"),
            (False, "native", "static", True, None),
            (True, "env", "static", False, "plain-value"),
            (True, "native", "vault", True, "plain-value"),
            (True, "terraform", "vault", True, "plain-value"),
            (True, "native", "static", True, None),
        ],
    )
    async def test_other_values_keep_their_existing_visibility(
        self, catalog_managed, category, source, sensitive, expected
    ):
        ws = _mock_workspace()
        ws.catalog_item_id = uuid.uuid4() if catalog_managed else None
        var = _mock_var(value="plain-value", sensitive=sensitive, ws_id=ws.id)
        var.category = category
        var.value_source = source
        app, _ = _make_app(_user())
        with (
            patch(
                "terrapod.api.routers.variables._get_workspace",
                new_callable=AsyncMock,
                return_value=ws,
            ),
            patch(
                "terrapod.api.routers.variables.resolve_workspace_capabilities_for",
                new_callable=AsyncMock,
                return_value=caps_for_level("read"),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.list_variables",
                new_callable=AsyncMock,
                return_value=[var],
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"][0]["attributes"]
        assert attrs["value"] == expected
        assert attrs["sensitive"] is sensitive
        assert attrs["version-id"] == (
            None if catalog_managed and source == "static" and category == "native" else "abc123"
        )

    @pytest.mark.parametrize("method", ["post", "patch"])
    async def test_unclassifiable_catalog_value_not_echoed_on_workspace_variable_write(
        self, method
    ):
        ws = _mock_workspace()
        ws.catalog_item_id = uuid.uuid4()
        var = _mock_var(key="hidden_input", value="old-secret", ws_id=ws.id)
        var.category = "native"
        app, _ = _make_app(_user(roles=["admin"]))
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
                "terrapod.api.routers.variables.variable_service.get_variable",
                new_callable=AsyncMock,
                return_value=var,
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.create_variable",
                new_callable=AsyncMock,
                return_value=var,
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.update_variable",
                new_callable=AsyncMock,
                return_value=var,
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                if method == "post":
                    resp = await c.post(
                        f"/api/v1/workspaces/ws-{ws.id}/vars",
                        json={
                            "data": {"attributes": {"key": "hidden_input", "value": "old-secret"}}
                        },
                        headers=_AUTH,
                    )
                else:
                    resp = await c.patch(
                        f"/api/v1/workspaces/ws-{ws.id}/vars/var-{var.id}",
                        json={"data": {"attributes": {"description": "updated"}}},
                        headers=_AUTH,
                    )
        assert resp.status_code == (201 if method == "post" else 200), resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["value"] is None
        assert attrs["sensitive"] is True
        assert attrs["version-id"] is None
        assert var.value == "old-secret"
        assert var.sensitive is False
        assert var.version_id == "abc123"

    @pytest.mark.parametrize("method", ["post", "patch"])
    async def test_catalog_non_sensitive_write_response_shows_value(self, method):
        ws = _mock_workspace()
        ws.catalog_item_id = uuid.uuid4()
        var = _mock_var(key="terrapod_hostname", value="https://example.com", ws_id=ws.id)
        var.category = "native"
        item = MagicMock(variable_options=[], updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        app, db = _make_app(_user(roles=["admin"]))
        db.get.return_value = item
        cv_result = MagicMock()
        cv_result.scalar_one_or_none.return_value = datetime(2026, 1, 2, tzinfo=UTC)
        db.execute.return_value = cv_result
        with (
            patch("terrapod.api.routers.variables._get_workspace", AsyncMock(return_value=ws)),
            patch(
                "terrapod.api.routers.variables.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps_for_level("admin")),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.get_variable",
                AsyncMock(return_value=var),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.create_variable",
                AsyncMock(return_value=var),
            ),
            patch(
                "terrapod.api.routers.variables.variable_service.update_variable",
                AsyncMock(return_value=var),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                if method == "post":
                    resp = await c.post(
                        f"/api/v1/workspaces/ws-{ws.id}/vars",
                        json={"data": {"attributes": {"key": var.key, "value": var.value}}},
                        headers=_AUTH,
                    )
                else:
                    resp = await c.patch(
                        f"/api/v1/workspaces/ws-{ws.id}/vars/var-{var.id}",
                        json={"data": {"attributes": {"description": "updated"}}},
                        headers=_AUTH,
                    )
        assert resp.status_code == (201 if method == "post" else 200), resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["value"] == "https://example.com"
        assert attrs["sensitive"] is False
        assert attrs["version-id"] == "abc123"


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
        # An ADMIN: the page this route exists for. A non-admin now goes through
        # the reach filter, which TestVarsetVisibility drives in both directions.
        user = _user(roles=["admin"])
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


class TestVarsetVisibility:
    """A variable set is visible when it reaches a workspace you can read (#2379).

    Before this, all three read routes authenticated and authorized nothing: any
    principal who could log in could enumerate every variable set in the
    deployment, and through `/relationships/vars` every variable KEY and
    `sensitive` flag in each. The values were masked; the inventory was not.

    The filter is driven here against mocked reach and mocked capabilities --
    the router helper is the unit, and those two are its dependencies. The real
    resolution against real roles, labels and assignments is exercised in
    `tests/integration/test_varset_visibility.py`.
    """

    @staticmethod
    def _reach(ws):
        """What `workspaces_for_varset` returns: (workspace, how it applies)."""
        return [(ws, "explicit")]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    async def test_an_admin_sees_a_set_reaching_nothing(self, mock_reach, *mocks):
        """The see-all fast path, and the assertion that makes it a fast path:
        reach is never consulted, so the admin page costs no per-set query."""
        mock_reach.side_effect = AssertionError("an admin must not pay the reach query")
        app, mock_db = _make_app(_user(roles=["admin"]))
        res = MagicMock()
        res.scalars.return_value.all.return_value = [_mock_varset()]
        mock_db.execute.return_value = res

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1
        assert not mock_reach.called

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    async def test_an_auditor_sees_a_set_reaching_nothing(self, mock_reach, *mocks):
        """`audit` is read-only everywhere, so it takes the same path as `admin`.
        Asserted separately because the two are a disjunction a single test
        cannot distinguish -- dropping either half would still pass the other."""
        mock_reach.side_effect = AssertionError("an auditor must not pay the reach query")
        app, mock_db = _make_app(_user(roles=["audit"]))
        res = MagicMock()
        res.scalars.return_value.all.return_value = [_mock_varset()]
        mock_db.execute.return_value = res

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    async def test_a_global_set_is_visible_without_a_reach_query(self, mock_reach, *mocks):
        """A global set reaches the whole estate, so the reach test would pass for
        anyone with any reach at all -- and `workspaces_for_varset` would return
        the entire workspace table to answer it. Short-circuited, which is why
        the mock asserts rather than returning."""
        mock_reach.side_effect = AssertionError("a global set must not be queried for reach")
        vs = _mock_varset(name="global-set")
        vs.global_set = True
        app, mock_db = _make_app(_user())
        res = MagicMock()
        res.scalars.return_value.all.return_value = [vs]
        mock_db.execute.return_value = res

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert resp.status_code == 200
        assert [d["attributes"]["name"] for d in resp.json()["data"]] == ["global-set"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    async def test_a_reachable_set_is_visible(self, mock_reach, mock_caps, *mocks):
        mock_reach.return_value = self._reach(_mock_workspace())
        mock_caps.return_value = caps_for_level("read")
        app, mock_db = _make_app(_user())
        res = MagicMock()
        res.scalars.return_value.all.return_value = [_mock_varset(name="reachable")]
        mock_db.execute.return_value = res

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert resp.status_code == 200
        assert [d["attributes"]["name"] for d in resp.json()["data"]] == ["reachable"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    async def test_an_unreachable_set_is_withheld(self, mock_reach, mock_caps, *mocks):
        """No capability on the only workspace the set reaches -> not listed.
        This and the test above are the pair: either alone passes whichever way
        the filter is written, because the property is a relationship."""
        mock_reach.return_value = self._reach(_mock_workspace())
        mock_caps.return_value = frozenset()
        app, mock_db = _make_app(_user())
        res = MagicMock()
        res.scalars.return_value.all.return_value = [_mock_varset()]
        mock_db.execute.return_value = res

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        # The count is the VISIBLE count, because the filter runs before paginate.
        # Reporting the row count would disclose how many sets exist, which is
        # most of what the filter exists to withhold.
        assert body["meta"]["pagination"]["total-count"] == 0

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    async def test_one_readable_workspace_is_enough(self, mock_reach, mock_caps, *mocks):
        """Reach is ANY, not ALL: a set spanning the estate must not be hidden
        from someone who can read one of the workspaces it feeds."""
        unreadable, readable = _mock_workspace(), _mock_workspace()
        mock_reach.return_value = [(unreadable, "rule"), (readable, "explicit")]
        mock_caps.side_effect = lambda db, user, ws, **kw: (
            caps_for_level("read") if ws is readable else frozenset()
        )
        app, mock_db = _make_app(_user())
        res = MagicMock()
        res.scalars.return_value.all.return_value = [_mock_varset(name="spanning")]
        mock_db.execute.return_value = res

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert [d["attributes"]["name"] for d in resp.json()["data"]] == ["spanning"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    async def test_readability_is_resolved_once_per_workspace(self, mock_reach, mock_caps, *mocks):
        """Memoised per workspace id, so a set-per-workspace estate does not pay
        the O(fleet x sets) resolution #1056 forbids."""
        ws = _mock_workspace()
        mock_reach.return_value = self._reach(ws)
        mock_caps.return_value = caps_for_level("read")
        app, mock_db = _make_app(_user())
        res = MagicMock()
        res.scalars.return_value.all.return_value = [
            _mock_varset(name=f"set-{i}") for i in range(5)
        ]
        mock_db.execute.return_value = res

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert len(resp.json()["data"]) == 5
        assert mock_caps.await_count == 1, "five sets, one workspace, one resolution"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    @patch("terrapod.api.routers.variables._get_varset")
    async def test_an_invisible_set_is_404_by_id(self, mock_get, mock_reach, mock_caps, *mocks):
        """404, not 403: the by-id route is the enumeration hole a list filter
        cannot close, and a distinguishable 403 would still answer "does this id
        exist" for a principal with no reach at all."""
        mock_get.return_value = _mock_varset()
        mock_reach.return_value = self._reach(_mock_workspace())
        mock_caps.return_value = frozenset()
        app, _ = _make_app(_user())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/varsets/varset-{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Variable set not found"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    @patch("terrapod.api.routers.variables._get_varset")
    async def test_the_variables_of_an_invisible_set_are_404(
        self, mock_get, mock_reach, mock_caps, *mocks
    ):
        """The route that discloses most: every key and `sensitive` flag in the
        set. Gated separately because it is a separate handler -- a filter on
        the collection alone leaves this one open."""
        mock_get.return_value = _mock_varset()
        mock_reach.return_value = self._reach(_mock_workspace())
        mock_caps.return_value = frozenset()
        app, _ = _make_app(_user())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/varsets/varset-{uuid.uuid4()}/relationships/vars", headers=_AUTH
            )
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.variables.variable_service.workspaces_for_varset")
    @patch("terrapod.api.routers.variables._get_varset")
    async def test_a_visible_set_still_serves_its_variables(
        self, mock_get, mock_reach, mock_caps, *mocks
    ):
        """The other direction: the guard must not break the route it protects."""
        vs = _mock_varset()
        vs.variables = [_mock_vsvar()]
        mock_get.return_value = vs
        mock_reach.return_value = self._reach(_mock_workspace())
        mock_caps.return_value = caps_for_level("read")
        app, mock_db = _make_app(_user())
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/varsets/varset-{uuid.uuid4()}/relationships/vars", headers=_AUTH
            )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_the_blast_radius_view_stays_admin_only(self, *mocks):
        """`/relationships/workspaces` lists EVERY workspace a set reaches, so
        the new rule would turn it into a leak: visible because you can read one
        of them, then showing you the ones you cannot. It keeps `require_admin`,
        and that is a decision rather than an omission."""
        app, _ = _make_app(_user())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/varsets/varset-{uuid.uuid4()}/relationships/workspaces",
                headers=_AUTH,
            )
        assert resp.status_code == 403


def _mock_vsvar(
    key="db_pass", value="s3cr3t", sensitive=True, apply_only=False, configure_only=False
):
    v = MagicMock()
    v.id = uuid.uuid4()
    v.variable_set_id = uuid.uuid4()
    v.key = key
    v.value = value
    v.sensitive = sensitive
    v.apply_only = apply_only
    v.configure_only = configure_only
    v.category = "terraform"
    v.structured = False
    v.value_source = "static"
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


class TestBothNamesForTheTypedFlag:
    """`structured` is the name; `hcl` keeps working for ever (#1435).

    `hcl` is not deprecated and cannot be. Variables live on `/api/v2`, which
    `tfci variable create` writes to and `go-tfe`'s `Variables.List` reads, so an
    un-upgraded client of either must see exactly what it sees today. These are
    permanent-compatibility tests, not transitional ones.
    """

    async def _create(self, mock_create, attrs: dict):
        ws = _mock_workspace()
        mock_create.return_value = _mock_var(ws_id=ws.id)
        app, mock_db = _make_app(_user())
        result = MagicMock()
        result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = result
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            return await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={"data": {"attributes": {"key": "ports", "value": "[80]", **attrs}}},
                headers=_AUTH,
            )

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_the_old_name_still_sets_the_flag(self, resolve, create, *mocks) -> None:
        """The whole point: an un-upgraded `tfci` sends this."""
        resolve.return_value = caps_for_level("write")
        r = await self._create(create, {"hcl": True})

        assert r.status_code == 201
        assert create.call_args.kwargs["structured"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_the_new_name_sets_it_too(self, resolve, create, *mocks) -> None:
        resolve.return_value = caps_for_level("write")
        r = await self._create(create, {"structured": True})

        assert r.status_code == 201
        assert create.call_args.kwargs["structured"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_both_agreeing_is_fine(self, resolve, create, *mocks) -> None:
        resolve.return_value = caps_for_level("write")
        r = await self._create(create, {"hcl": True, "structured": True})

        assert r.status_code == 201
        assert create.call_args.kwargs["structured"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_both_disagreeing_is_refused(self, resolve, create, *mocks) -> None:
        """Not a precedence rule. A client that disagrees with itself about
        whether a value is typed has a bug, and picking a winner hides it."""
        resolve.return_value = caps_for_level("write")
        r = await self._create(create, {"hcl": True, "structured": False})

        assert r.status_code == 422
        create.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_capabilities_for")
    async def test_the_response_carries_both_and_they_agree(self, resolve, *mocks) -> None:
        """An un-upgraded reader looks for `hcl` and must still find it."""
        resolve.return_value = caps_for_level("read")
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        var.structured = True

        app, mock_db = _make_app(_user())
        ws_result, var_result = MagicMock(), MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        var_result.scalars.return_value.all.return_value = [var]
        mock_db.execute.side_effect = [ws_result, var_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.get(f"/api/v2/workspaces/ws-{ws.id}/vars", headers=_AUTH)

        attrs = r.json()["data"][0]["attributes"]
        assert attrs["hcl"] is True
        assert attrs["structured"] is True


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


# ── Vault file delivery on write (#1619) ──────────────────────────────

_COORDS = {"mount": "kvv2", "path": "apps/gcp", "field": "sa"}


def _file_ref(**file) -> str:
    return json.dumps({**_COORDS, "file": file})


def _name_detail(name, reason, key="T"):
    return f"variable {key!r}: vault file name {name!r} is invalid: {reason}"


def _chars(seg):
    return f"has a path segment with characters outside [A-Za-z0-9._-]: {seg!r}"


_HCL_DETAIL = (
    "`file` delivery cannot be combined with structured (hcl): the variable's value "
    "becomes the file's path, which is not a typed expression. Turn structured off."
)
_STATIC_DETAIL = (
    "`file` delivery needs value-source 'vault': this value is an OpenBao/Vault "
    "reference, and with a static source it would be delivered to the run as the "
    "literal JSON"
)

_BAD_NAMES = [
    ("", "must not be empty"),
    ("../x", "has a '.' or '..' path segment"),
    ("a/./b", "has a '.' or '..' path segment"),
    ("a//b", "has an empty path segment"),
    (
        "/etc/x",
        "must be a relative path, or start with ~/ for a path in the runner's home "
        "directory; absolute paths are not allowed",
    ),
    ("a\x00b", "contains a NUL character"),
    ("a\\b", _chars("a\\b")),
    ("café.json", _chars("café.json")),
    ("x" * 256, "is longer than 255 characters"),
    ("~/.ssh/id_rsa", "targets ~/.ssh, which the runner manages itself; choose another path"),
    ("~/.gitconfig", "targets ~/.gitconfig, which the runner manages itself; choose another path"),
    (
        "~/.config/terrapod-git/gitconfig",
        "targets ~/.config/terrapod-git, which the runner manages itself; choose another path",
    ),
    (
        "~/.terraform.d/credentials.tfrc.json",
        "targets ~/.terraform.d, which the runner manages itself; choose another path",
    ),
]


@pytest.fixture
def _no_boot():
    with (
        patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
        patch("terrapod.api.app.init_redis"),
        patch("terrapod.api.app.init_db"),
        patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock),
        patch(
            "terrapod.services.variable_service.local_workspaces_for_varset",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        yield


def _vault_var(key="T", value=None, hcl=False, category="env"):
    var = _mock_var(key=key, sensitive=True)
    var.value_source = "vault"
    var.value = value if value is not None else _file_ref()
    var.structured = hcl
    var.category = category
    return var


@pytest.mark.usefixtures("_no_boot")
class _WritePaths:
    """The four variable write paths, driven end to end.

    A mixin rather than a base class with tests of its own, and NOT named
    `Test*` so pytest does not collect it: anything asserting a rule holds on
    every write path needs all four, and a second copy of this scaffolding is
    how two such rules come to be checked on different sets of paths.
    """

    async def _create_ws(self, attrs):
        app, _ = _make_app(_user(roles=["admin"]))
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.execution_mode = "agent"
        created = {}

        async def fake_create(db, **kw):
            created.update(kw)
            return _vault_var(key=kw["key"], value=kw["value"])

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
                new=fake_create,
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/v2/workspaces/ws-{ws.id}/vars",
                    json={"data": {"attributes": attrs}},
                    headers=_AUTH,
                )
        return resp, created

    async def _patch_ws(self, var, attrs):
        app, _ = _make_app(_user(roles=["admin"]))
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.execution_mode = "agent"
        update = AsyncMock(return_value=var)
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
                "terrapod.api.routers.variables.variable_service.get_variable",
                new_callable=AsyncMock,
                return_value=var,
            ),
            patch("terrapod.api.routers.variables.variable_service.update_variable", new=update),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.patch(
                    f"/api/v2/workspaces/ws-{ws.id}/vars/var-{var.id}",
                    json={"data": {"attributes": attrs}},
                    headers=_AUTH,
                )
        return resp, update

    async def _create_set(self, attrs):
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_db.add = MagicMock()
        mock_db.refresh = AsyncMock()
        with patch(
            "terrapod.api.routers.variables._get_varset",
            new_callable=AsyncMock,
            return_value=_mock_varset(),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/v2/varsets/varset-{uuid.uuid4()}/relationships/vars",
                    json={"data": {"attributes": attrs}},
                    headers=_AUTH,
                )
        return resp, mock_db

    async def _patch_set(self, vsv, attrs):
        app, mock_db = _make_app(_user(roles=["admin"]))
        lookup = MagicMock()
        lookup.scalar_one_or_none.return_value = vsv
        mock_db.execute = AsyncMock(return_value=lookup)
        mock_db.refresh = AsyncMock()
        with patch(
            "terrapod.api.routers.variables._get_varset",
            new_callable=AsyncMock,
            return_value=_mock_varset(),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.patch(
                    f"/api/v2/varsets/varset-{uuid.uuid4()}/relationships/vars/var-{vsv.id}",
                    json={"data": {"attributes": attrs}},
                    headers=_AUTH,
                )
        return resp, mock_db


class TestVaultFileDeliveryWrites(_WritePaths):
    """Every write path — workspace and set variables, create and patch —
    validates `file` against the variable as it will be after the write."""

    @staticmethod
    def _vault(key, value, **extra):
        return {"key": key, "value": value, "value-source": "vault", "category": "env", **extra}

    # workspace create ───────────────────────────────────────────────

    @pytest.mark.parametrize(
        ("key", "file"),
        [
            ("GOOGLE_APPLICATION_CREDENTIALS", {"name": "gcp/adc.json"}),
            ("creds", {}),
            ("AWS_SHARED_CREDENTIALS_FILE", {"name": "~/.aws/credentials"}),
        ],
    )
    async def test_a_valid_file_reference_is_stored_as_written(self, key, file):
        resp, created = await self._create_ws(self._vault(key, _file_ref(**file)))
        assert resp.status_code == 201, resp.text
        assert json.loads(created["value"])["file"] == file
        assert created["sensitive"] is True
        # The API returns the reference — including the file name — never a value.
        assert json.loads(resp.json()["data"]["attributes"]["value"])["file"] == file

    async def test_a_terraform_category_file_is_accepted(self):
        attrs = self._vault("sa_file", _file_ref(), category="terraform")
        resp, _ = await self._create_ws(attrs)
        assert resp.status_code == 201

    @pytest.mark.parametrize(("name", "reason"), _BAD_NAMES)
    async def test_an_invalid_name_is_422_with_the_exact_reason(self, name, reason):
        resp, created = await self._create_ws(self._vault("T", _file_ref(name=name)))
        assert resp.status_code == 422
        assert resp.json()["detail"] == _name_detail(name, reason)
        assert created == {}

    async def test_an_invalid_defaulted_name_is_422(self):
        resp, _ = await self._create_ws(self._vault("my key", _file_ref()))
        assert resp.status_code == 422
        assert resp.json()["detail"] == _name_detail("my key", _chars("my key"), key="my key")

    async def test_a_file_that_is_not_an_object_is_422(self):
        value = json.dumps({**_COORDS, "file": "gcp/adc.json"})
        resp, _ = await self._create_ws(self._vault("T", value))
        assert resp.status_code == 422
        assert resp.json()["detail"] == "variable 'T' has a vault `file` that is not an object"

    async def test_an_unknown_file_key_is_422(self):
        resp, _ = await self._create_ws(self._vault("T", _file_ref(colour="blue")))
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "variable 'T' has a vault `file` with unknown keys: colour (supported: `name`, "
            "`template`, `format`, `fields`, `encoding`)"
        )

    async def test_a_reserved_file_key_is_422(self):
        resp, _ = await self._create_ws(self._vault("T", _file_ref(mode="0600")))
        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "variable 'T' has a vault `file` using mode, which is reserved for a later release "
            "and not supported yet (supported: `name`, `template`, `format`, `fields`, `encoding`)"
        )

    # ── #1648: templates, formats and encoding are validated on write ──

    @staticmethod
    def _whole_secret_ref(**file) -> str:
        """A reference whose content is built from the whole secret: no field."""
        coords = {k: v for k, v in _COORDS.items() if k != "field"}
        return json.dumps({**coords, "file": file})

    async def test_a_template_without_a_field_is_accepted(self):
        tpl = "[default]\naws_access_key_id = {{ access_key }}\n"
        value = self._whole_secret_ref(name="~/.aws/credentials", template=tpl)
        resp, created = await self._create_ws(self._vault("T", value))
        assert resp.status_code == 201, resp.text
        assert json.loads(created["value"])["file"]["template"] == tpl

    async def test_a_format_with_fields_is_accepted(self):
        value = self._whole_secret_ref(format="env", fields=["DB_USER", "DB_PASS"])
        resp, _ = await self._create_ws(self._vault("T", value))
        assert resp.status_code == 201, resp.text

    async def test_a_field_with_base64_encoding_is_accepted(self):
        resp, _ = await self._create_ws(self._vault("T", _file_ref(encoding="base64")))
        assert resp.status_code == 201, resp.text

    async def test_a_template_syntax_error_is_422_before_any_run(self):
        value = self._whole_secret_ref(template="x = {{ access_key | upper }}")
        resp, created = await self._create_ws(self._vault("T", value))
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith(
            "variable 'T': vault file template tag {{ access_key | upper }} uses unknown "
            "filter 'upper'"
        )
        assert created == {}

    async def test_a_template_beside_a_field_is_422(self):
        resp, _ = await self._create_ws(self._vault("T", _file_ref(template="{{ a }}")))
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith(
            "variable 'T': `field` and `file.template` cannot be combined"
        )

    async def test_a_template_and_a_format_together_are_422(self):
        value = self._whole_secret_ref(template="{{ a }}", format="json")
        resp, _ = await self._create_ws(self._vault("T", value))
        assert resp.status_code == 422
        assert "`file.template` and `file.format` cannot be combined" in resp.json()["detail"]

    async def test_encoding_with_a_template_is_422(self):
        value = self._whole_secret_ref(template="{{ a }}", encoding="base64")
        resp, _ = await self._create_ws(self._vault("T", value))
        assert resp.status_code == 422
        assert "`encoding` decodes the one `field`" in resp.json()["detail"]

    async def test_a_template_on_a_static_source_is_422(self):
        """`file` on a static source is refused for a field-less reference too."""
        attrs = {"key": "T", "value": self._whole_secret_ref(template="{{ a }}"), "category": "env"}
        resp, created = await self._create_ws(attrs)
        assert resp.status_code == 422
        assert resp.json()["detail"] == _STATIC_DETAIL
        assert created == {}

    async def test_file_with_hcl_is_422(self):
        attrs = self._vault("T", _file_ref(), category="terraform", hcl=True)
        resp, _ = await self._create_ws(attrs)
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL

    async def test_file_with_structured_is_422(self):
        """`structured` is the flag's name on this line (#1435); `hcl` its alias."""
        attrs = self._vault("T", _file_ref(), category="terraform", structured=True)
        resp, created = await self._create_ws(attrs)
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL
        assert created == {}

    @pytest.mark.parametrize("source", ["static", None])
    async def test_file_on_a_non_vault_source_is_422(self, source):
        attrs = {"key": "T", "value": _file_ref(), "category": "env"}
        if source:
            attrs["value-source"] = source
        resp, created = await self._create_ws(attrs)
        assert resp.status_code == 422
        assert resp.json()["detail"] == _STATIC_DETAIL
        assert created == {}

    async def test_an_ordinary_static_json_value_with_a_file_key_is_untouched(self):
        value = json.dumps({"file": "main.tf", "lines": 3})
        resp, _ = await self._create_ws({"key": "T", "value": value, "category": "terraform"})
        assert resp.status_code == 201

    # workspace patch ────────────────────────────────────────────────

    async def test_patching_hcl_on_for_a_file_variable_is_422(self):
        var = _vault_var(category="terraform")
        resp, update = await self._patch_ws(var, {"hcl": True})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL
        update.assert_not_awaited()

    async def test_patching_structured_on_for_a_file_variable_is_422(self):
        var = _vault_var(category="terraform")
        resp, update = await self._patch_ws(var, {"structured": True})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL
        update.assert_not_awaited()

    async def test_an_unrelated_patch_on_a_structured_file_variable_is_422(self):
        """The stored flag counts, not only the one in the request."""
        var = _vault_var(category="terraform", hcl=True)
        resp, update = await self._patch_ws(var, {"description": "x"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL
        update.assert_not_awaited()

    async def test_renaming_the_key_revalidates_a_defaulted_name(self):
        resp, update = await self._patch_ws(_vault_var(), {"key": "bad key"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _name_detail("bad key", _chars("bad key"), key="bad key")
        update.assert_not_awaited()

    async def test_patching_in_a_managed_home_path_is_422(self):
        resp, _ = await self._patch_ws(_vault_var(), {"value": _file_ref(name="~/.ssh/config")})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _name_detail(
            "~/.ssh/config", "targets ~/.ssh, which the runner manages itself; choose another path"
        )

    async def test_an_unrelated_patch_on_a_file_variable_succeeds(self):
        resp, update = await self._patch_ws(_vault_var(), {"description": "adc"})
        assert resp.status_code == 200
        update.assert_awaited_once()

    async def test_patching_a_static_variable_to_a_file_reference_is_422(self):
        var = _mock_var(key="T", value="plain")
        resp, _ = await self._patch_ws(var, {"value": _file_ref()})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _STATIC_DETAIL

    # set variables ──────────────────────────────────────────────────

    async def test_a_set_variable_with_a_valid_file_is_created(self):
        resp, mock_db = await self._create_set(self._vault("CREDS", _file_ref(name="c.json")))
        assert resp.status_code == 201, resp.text
        vsv = mock_db.add.call_args.args[0]
        assert json.loads(vsv.value)["file"] == {"name": "c.json"}
        assert vsv.sensitive is True

    @pytest.mark.parametrize(("name", "reason"), _BAD_NAMES[:4] + _BAD_NAMES[-4:])
    async def test_a_set_variable_with_an_invalid_name_is_422(self, name, reason):
        resp, mock_db = await self._create_set(self._vault("T", _file_ref(name=name)))
        assert resp.status_code == 422
        assert resp.json()["detail"] == _name_detail(name, reason)
        mock_db.add.assert_not_called()

    async def test_a_set_variable_with_file_and_hcl_is_422(self):
        attrs = self._vault("T", _file_ref(), category="terraform", hcl=True)
        resp, _ = await self._create_set(attrs)
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL

    async def test_a_static_set_variable_with_a_file_reference_is_422(self):
        resp, _ = await self._create_set({"key": "T", "value": _file_ref(), "category": "env"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _STATIC_DETAIL

    async def test_patching_hcl_on_for_a_set_file_variable_is_422(self):
        vsv = _mock_vsvar(key="T", value=_file_ref())
        vsv.value_source = "vault"
        resp, _ = await self._patch_set(vsv, {"hcl": True})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL

    async def test_patching_structured_on_for_a_set_file_variable_is_422(self):
        vsv = _mock_vsvar(key="T", value=_file_ref())
        vsv.value_source = "vault"
        resp, _ = await self._patch_set(vsv, {"structured": True})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL

    async def test_a_set_variable_with_file_and_structured_is_422(self):
        attrs = self._vault("T", _file_ref(), category="terraform", structured=True)
        resp, _ = await self._create_set(attrs)
        assert resp.status_code == 422
        assert resp.json()["detail"] == _HCL_DETAIL

    async def test_patching_a_set_file_variable_to_a_bad_name_is_422(self):
        vsv = _mock_vsvar(key="T", value=_file_ref())
        vsv.value_source = "vault"
        resp, _ = await self._patch_set(vsv, {"value": _file_ref(name="../../etc/shadow")})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _name_detail(
            "../../etc/shadow", "has a '.' or '..' path segment"
        )

    async def test_renaming_a_set_file_variable_revalidates_its_default_name(self):
        vsv = _mock_vsvar(key="T", value=_file_ref())
        vsv.value_source = "vault"
        resp, _ = await self._patch_set(vsv, {"key": "a b"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == _name_detail("a b", _chars("a b"), key="a b")


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


@pytest.mark.usefixtures("_no_boot")
class TestAnsibleCredentialCategoryWrites(_WritePaths):
    """#1933's two categories, refused at the write boundary.

    Refused HERE rather than dropped at resolution, which is where git auth
    handles a malformed envelope. That was survivable for git -- the operator
    meets it as an `init` failure -- and is not for a configure, which spends
    the mistake part way through a fleet, on a host it has already changed.

    Every rule is asserted on all four write paths, because
    `_credential_envelope_guard` is only a rule if it is called at all four;
    called at three it is a check somebody remembered.
    """

    MACHINE = "ansible_machine_auth"
    VAULT = "ansible_vault_auth"

    @staticmethod
    def _cred(key, category, envelope, **extra):
        return {"key": key, "value": json.dumps(envelope), "category": category, **extra}

    async def test_a_valid_vault_password_is_stored_as_written(self):
        resp, created = await self._create_ws(self._cred("prod", self.VAULT, {"password": "p"}))
        assert resp.status_code == 201, resp.text
        # Stored byte for byte: an altered credential is worse than a refused
        # one, which is the whole of the v1.9.0 transform lesson.
        assert json.loads(created["value"]) == {"password": "p"}

    async def test_a_credential_is_always_sensitive_on_the_varset_path(self):
        """Asserted on the varset path because that is where the ROUTER forces
        it. The workspace paths force it inside `variable_service`, which this
        fixture replaces with a fake -- so the same assertion there would be
        about the fake. `tests/services/test_variable_service.py` owns that
        half, and the two together are the four paths.
        """
        resp, _ = await self._create_set(
            self._cred("prod", self.VAULT, {"password": "p"}, sensitive=False)
        )
        assert resp.status_code in (200, 201), resp.text

    async def test_a_valid_machine_credential_is_stored(self):
        # `known_hosts` rather than `user`, which #2413 F9 withdrew -- and it
        # keeps the read-back assertion on a value that is not a secret.
        resp, created = await self._create_ws(
            self._cred(
                "web",
                self.MACHINE,
                {"private_key": "k", "known_hosts": "h.example.com ssh-ed25519 AAA"},
            )
        )
        assert resp.status_code == 201, resp.text
        assert json.loads(created["value"])["known_hosts"].startswith("h.example.com")

    # ── the envelope, on every write path ──────────────────────────────

    async def test_an_empty_vault_envelope_is_422_on_workspace_create(self):
        resp, _ = await self._create_ws(self._cred("prod", self.VAULT, {}))
        assert resp.status_code == 422
        assert "password" in resp.text

    async def test_an_empty_vault_envelope_is_422_on_workspace_patch(self):
        var = _mock_var(key="prod", sensitive=True)
        var.category = self.VAULT
        resp, _ = await self._patch_ws(var, {"value": json.dumps({})})
        assert resp.status_code == 422, resp.text
        assert "password" in resp.text

    async def test_an_empty_vault_envelope_is_422_on_varset_create(self):
        resp, _ = await self._create_set(self._cred("prod", self.VAULT, {}))
        assert resp.status_code == 422, resp.text
        assert "password" in resp.text

    async def test_an_empty_vault_envelope_is_422_on_varset_patch(self):
        vsv = _mock_var(key="prod", sensitive=True)
        vsv.category = self.VAULT
        resp, _ = await self._patch_set(vsv, {"value": json.dumps({})})
        assert resp.status_code == 422, resp.text
        assert "password" in resp.text

    # ── the scope ─────────────────────────────────────────────────────

    async def test_a_vault_label_carrying_an_identity_delimiter_is_422(self):
        """`,` and `@` are ANSIBLE_VAULT_IDENTITY_LIST's own delimiters, so a
        label carrying one produces a different list rather than a rejected
        one."""
        resp, _ = await self._create_ws(self._cred("a,b", self.VAULT, {"password": "p"}))
        assert resp.status_code == 422
        assert "label" in resp.text

    async def test_a_name_a_placeholder_could_not_carry_is_422(self):
        """`key` is the credential's NAME, and an inventory refers to it as
        `${terrapod:<name>.<field>}` -- so a character the placeholder's own
        grammar uses would make the reference unresolvable rather than
        rejected."""
        resp, _ = await self._create_ws(
            self._cred("not a name", self.MACHINE, {"private_key": "k"})
        )
        assert resp.status_code == 422
        assert "name" in resp.text

    async def test_an_ordinary_credential_name_is_accepted(self):
        resp, _ = await self._create_ws(self._cred("web-key", self.MACHINE, {"private_key": "k"}))
        assert resp.status_code == 201, resp.text

    async def test_nothing_about_PLACEMENT_is_validated_here(self):
        """A name that is not a host or a group in any inventory is perfectly
        valid: where the credential applies is said in the inventory, and
        validating it here would be a second place that says so."""
        resp, _ = await self._create_ws(
            self._cred("nothing.in.any.inventory", self.MACHINE, {"private_key": "k"})
        )
        assert resp.status_code == 201, resp.text

    async def test_a_machine_credential_with_no_way_to_authenticate_is_422(self):
        # `known_hosts` is a VALID field that authenticates nothing, which is
        # what this test is about. It was `{"user": "deploy"}`, and since #2413
        # F9 withdrew that field the envelope would be refused as unknown
        # instead -- so the test would have gone on passing for a reason that
        # has nothing to do with its name.
        resp, _ = await self._create_ws(
            self._cred("all", self.MACHINE, {"known_hosts": "h.example.com ssh-ed25519 AAA"})
        )
        assert resp.status_code == 422
        assert "private_key" in resp.text

    async def test_an_unknown_envelope_field_is_422_and_named(self):
        resp, _ = await self._create_ws(
            self._cred("all", self.MACHINE, {"private_key": "k", "becom_method": "sudo"})
        )
        assert resp.status_code == 422
        assert "becom_method" in resp.text

    # ── the value source ──────────────────────────────────────────────

    async def test_an_openbao_vault_source_is_422_on_an_ansible_credential(self):
        """An envelope is an object; an OpenBao/Vault reference resolves to a
        single field. The rule already existed for the git categories and now
        keys on the property, so these are covered by it rather than by a
        second copy -- which is what `CREDENTIAL_CATEGORIES` is for."""
        resp, _ = await self._create_ws(
            {
                "key": "prod",
                "value": _file_ref(),
                "value-source": "vault",
                "category": self.VAULT,
            }
        )
        assert resp.status_code == 422
        assert "vault" in resp.text.lower()

    async def test_the_existing_git_refusal_still_holds(self):
        """The generalisation must not have narrowed what it already covered."""
        resp, _ = await self._create_ws(
            {
                "key": "github.com",
                "value": _file_ref(),
                "value-source": "vault",
                "category": "git_ssh_auth",
            }
        )
        assert resp.status_code == 422

    async def test_an_ordinary_variable_is_untouched_by_any_of_this(self):
        """The negative path. The guard sees every category, so a plain
        terraform variable whose value is not JSON must pass straight through --
        otherwise the feature breaks every workspace in the estate."""
        resp, created = await self._create_ws({"key": "region", "value": "eu-west-1"})
        assert resp.status_code == 201, resp.text
        assert created["value"] == "eu-west-1"
