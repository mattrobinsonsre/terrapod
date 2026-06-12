"""Tests for workspace CRUD and lock/unlock endpoints with RBAC."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="test@example.com", roles=None, auth_method="session"):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method=auth_method,
    )


def _mock_workspace(
    name="my-ws",
    ws_id=None,
    owner_email="",
    labels=None,
    locked=False,
    lock_id=None,
    auto_apply=False,
    execution_mode="local",
    terraform_version="1.11",
    resource_cpu="1",
    resource_memory="2Gi",
):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.auto_apply = auto_apply
    ws.execution_mode = execution_mode
    ws.terraform_version = terraform_version
    ws.working_directory = ""
    ws.locked = locked
    ws.lock_id = lock_id
    ws.resource_cpu = resource_cpu
    ws.execution_backend = "tofu"
    ws.resource_memory = resource_memory
    ws.agent_pool_id = None
    ws.agent_pool = None
    ws.labels = labels or {}
    ws.owner_email = owner_email
    ws.vcs_connection_id = None
    ws.vcs_connection = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
    ws.var_files = []
    ws.trigger_prefixes = []
    ws.drift_ignore_rules = []
    ws.drift_detection_enabled = False
    ws.drift_detection_interval_seconds = 86400
    ws.drift_last_checked_at = None
    ws.drift_status = ""
    ws.state_diverged = False
    ws.vcs_workflow = "merge_then_apply"
    ws.auto_merge = False
    ws.auto_merge_strategy = "merge"
    ws.lifecycle_state = "active"
    ws.lifecycle_reason = ""
    ws.autodiscovery_pr_number = None
    ws.ai_summary_mode = "default"
    ws.ai_summary_context = ""
    ws.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    ws.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return ws


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user

    if mock_db is None:
        mock_db = AsyncMock()

    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── Create Workspace ────────────────────────────────────────────────────


class TestCreateWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_returns_201(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        # No existing workspace
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/workspaces",
                json={"data": {"type": "workspaces", "attributes": {"name": "new-ws"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["type"] == "workspaces"
        assert data["attributes"]["name"] == "new-ws"
        assert data["attributes"]["owner-email"] == user.email

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_duplicate_returns_422(self, *mocks):
        user = _user()
        app, mock_db = _make_app(user)
        # Existing workspace found
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/workspaces",
                json={"data": {"attributes": {"name": "existing"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_missing_name_returns_422(self, *mocks):
        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/workspaces",
                json={"data": {"attributes": {}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422


# ── Show Workspace ─────────────────────────────────────────────────────


class TestShowWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_show_by_name(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace(name="test-ws")
        user = _user()
        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run_result = MagicMock()
        no_run_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/organizations/default/workspaces/test-ws",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["name"] == "test-ws"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_show_no_permission_returns_404(self, mock_resolve, *mocks):
        """TFE behavior: workspace invisible (404) when no permission, not 403."""
        mock_resolve.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/organizations/default/workspaces/my-ws",
                headers=_AUTH,
            )
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_not_found_returns_404(self, *mocks):
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/organizations/default/workspaces/nope",
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Show Workspace by ID ──────────────────────────────────────────────


class TestShowWorkspaceById:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_show_by_id_with_read(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run_result = MagicMock()
        no_run_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_show_by_id_no_permission_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 403


# ── Update Workspace ──────────────────────────────────────────────────


class TestUpdateWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_update_requires_admin(self, mock_resolve, *mocks):
        mock_resolve.return_value = "write"  # not admin
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"auto-apply": True}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_update_owner_requires_platform_admin(self, mock_resolve, *mocks):
        """owner-email change requires platform admin, not just workspace admin."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace(owner_email="old@test.com")
        # User is workspace admin via ownership but NOT platform admin
        user = _user(email="old@test.com", roles=["everyone"])
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"owner-email": "new@test.com"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403


# ── Delete Workspace ──────────────────────────────────────────────────


class TestDeleteWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_delete_with_admin_returns_204(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/terrapod/v1/workspaces/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_delete_without_admin_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = "write"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/terrapod/v1/workspaces/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 403


# ── Lock / Unlock ─────────────────────────────────────────────────────


class TestLockWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_lock_with_plan_permission(self, mock_resolve, *mocks):
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=False)
        user = _user()
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/lock",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_lock_already_locked_returns_409(self, mock_resolve, *mocks):
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=True, lock_id="lock-other@test.com")
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/lock",
                headers=_AUTH,
            )
        assert resp.status_code == 409

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_lock_read_only_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/lock",
                headers=_AUTH,
            )
        assert resp.status_code == 403


class TestUnlockWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_unlock_own_lock(self, mock_resolve, *mocks):
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=True, lock_id="lock-test@example.com")
        user = _user(email="test@example.com")
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/unlock",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_force_unlock_requires_admin(self, mock_resolve, *mocks):
        """Non-admin with plan perm can't force-unlock another user's lock."""
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=True, lock_id="lock-other@test.com")
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/unlock",
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_admin_can_force_unlock(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace(locked=True, lock_id="lock-other@test.com")
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/unlock",
                headers=_AUTH,
            )
        assert resp.status_code == 200


# ── Permissions block ─────────────────────────────────────────────────


class TestPermissionsBlock:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_read_user_permissions(self, mock_resolve, *mocks):
        """Read permission: can read, but not update/destroy/queue."""
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run_result = MagicMock()
        no_run_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        perms = resp.json()["data"]["attributes"]["permissions"]
        assert perms["can-read-state-versions"] is True
        assert perms["can-read-variable"] is True
        assert perms["can-update"] is False
        assert perms["can-destroy"] is False
        assert perms["can-queue-run"] is False
        assert perms["can-lock"] is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_admin_user_permissions(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user(roles=["admin"]))
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run_result = MagicMock()
        no_run_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        perms = resp.json()["data"]["attributes"]["permissions"]
        assert perms["can-update"] is True
        assert perms["can-destroy"] is True
        assert perms["can-queue-run"] is True
        assert perms["can-lock"] is True
        assert perms["can-force-unlock"] is True


# ── Tag bindings (terraform key-value tag support probe) ───────────────


class TestLabelsToTagNames:
    """`tag-names` is the legacy attribute that OpenTofu/Terraform's cloud
    backend reads on workspace lookup. Without it, the CLI thinks the
    workspace has no tags, fires AddTags (POST /relationships/tags), 404s,
    init fails. We render labels in both bare-key and key=value form so
    cloud blocks written either way match."""

    def test_empty_labels(self):
        from terrapod.api.routers.tfe_v2 import _labels_to_tag_names

        assert _labels_to_tag_names({}) == []
        assert _labels_to_tag_names(None) == []

    def test_renders_both_bare_and_kv_form(self):
        from terrapod.api.routers.tfe_v2 import _labels_to_tag_names

        names = _labels_to_tag_names({"env": "dev", "team": "platform"})
        assert "env" in names
        assert "env=dev" in names
        assert "team" in names
        assert "team=platform" in names

    def test_empty_value_skips_kv_form(self):
        """An empty-value label only appears in bare-key form."""
        from terrapod.api.routers.tfe_v2 import _labels_to_tag_names

        names = _labels_to_tag_names({"flag": ""})
        assert "flag" in names
        assert "flag=" not in names


class TestWorkspaceTagBindings:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_returns_labels_as_bindings(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace(labels={"repo": "infra-core", "env": "dev"})
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/tag-bindings", headers=_AUTH)
        assert resp.status_code == 200
        items = resp.json()["data"]
        # Same key/value pairs, regardless of ordering
        assert {(i["attributes"]["key"], i["attributes"]["value"]) for i in items} == {
            ("repo", "infra-core"),
            ("env", "dev"),
        }
        assert all(i["type"] == "tag-bindings" for i in items)
        # JSON:API requires `id` on every resource. go-tfe's jsonapi parser
        # silently drops entries that are missing it — leaving the CLI to
        # think the workspace has no tags and try to PATCH them in.
        assert all(i["id"] for i in items)
        # IDs must be unique within the response (JSON:API requirement).
        assert len({i["id"] for i in items}) == len(items)

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_empty_labels_returns_empty_array(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace(labels={})
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/tag-bindings", headers=_AUTH)
        # Returning 200 with [] (rather than 404) is what tells terraform's
        # cloud backend that this server supports key-value tags.
        assert resp.status_code == 200
        assert resp.json() == {"data": []}

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_no_permission_blocks_access(self, mock_resolve, *mocks):
        mock_resolve.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/tag-bindings", headers=_AUTH)
        # 403 is fine for terraform's KV-tag-support probe — only a 404 would
        # cause it to conclude the endpoint doesn't exist.
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_effective_tag_bindings_mirrors_workspace_bindings(self, mock_resolve, *mocks):
        # Terrapod has no project hierarchy, so effective bindings == workspace bindings
        mock_resolve.return_value = "read"
        ws = _mock_workspace(labels={"repo": "infra-core"})
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/effective-tag-bindings", headers=_AUTH
            )
        assert resp.status_code == 200
        items = resp.json()["data"]
        assert items == [
            {
                "id": f"{ws.id}:repo",
                "type": "effective-tag-bindings",
                "attributes": {"key": "repo", "value": "infra-core"},
            }
        ]


# ── Cloud-block tag → label filtering ──────────────────────────────────


class TestParseTagFilters:
    """Unit tests for the cloud-block `tags` query parameter parser."""

    def _req(self, query: str):
        from urllib.parse import urlencode

        from starlette.requests import Request

        if isinstance(query, dict):
            query = urlencode(query, doseq=True)
        scope = {"type": "http", "query_string": query.encode("ascii"), "headers": []}
        return Request(scope)

    def test_no_tag_params_returns_empty(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        assert _parse_tag_filters(self._req("")) == []
        assert _parse_tag_filters(self._req("search%5Bname%5D=foo")) == []

    def test_list_form_bare_keys(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        # search[tags]=core,internal -> two key-only entries
        out = _parse_tag_filters(self._req("search%5Btags%5D=core,internal"))
        assert out == [("core", None), ("internal", None)]

    def test_list_form_key_value(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        # search[tags]=env=prod,team=platform
        out = _parse_tag_filters(self._req("search%5Btags%5D=env=prod,team=platform"))
        assert out == [("env", "prod"), ("team", "platform")]

    def test_list_form_mixed(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        out = _parse_tag_filters(self._req("search%5Btags%5D=core,env=prod"))
        assert out == [("core", None), ("env", "prod")]

    def test_list_form_strips_whitespace(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        out = _parse_tag_filters(self._req("search%5Btags%5D=%20core%20,%20env=prod%20"))
        assert out == [("core", None), ("env", "prod")]

    def test_list_form_skips_empty_tokens(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        out = _parse_tag_filters(self._req("search%5Btags%5D=,core,,internal,"))
        assert out == [("core", None), ("internal", None)]

    def test_map_form_single(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        # filter[tagged][0][key]=env&filter[tagged][0][value]=prod
        q = "filter%5Btagged%5D%5B0%5D%5Bkey%5D=env&filter%5Btagged%5D%5B0%5D%5Bvalue%5D=prod"
        out = _parse_tag_filters(self._req(q))
        assert out == [("env", "prod")]

    def test_map_form_multiple_indexed(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        # Two indexed entries; ensure they're returned in numeric (not insertion) order
        q = (
            "filter%5Btagged%5D%5B1%5D%5Bkey%5D=team"
            "&filter%5Btagged%5D%5B1%5D%5Bvalue%5D=platform"
            "&filter%5Btagged%5D%5B0%5D%5Bkey%5D=env"
            "&filter%5Btagged%5D%5B0%5D%5Bvalue%5D=prod"
        )
        out = _parse_tag_filters(self._req(q))
        assert out == [("env", "prod"), ("team", "platform")]

    def test_map_form_missing_value_treated_as_key_only(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        # value omitted -> key-only filter (matches any value)
        q = "filter%5Btagged%5D%5B0%5D%5Bkey%5D=core"
        out = _parse_tag_filters(self._req(q))
        assert out == [("core", None)]

    def test_map_form_empty_key_skipped(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        q = "filter%5Btagged%5D%5B0%5D%5Bkey%5D=&filter%5Btagged%5D%5B0%5D%5Bvalue%5D=prod"
        out = _parse_tag_filters(self._req(q))
        assert out == []

    def test_combined_list_and_map_forms(self):
        from terrapod.api.routers.tfe_v2 import _parse_tag_filters

        q = (
            "search%5Btags%5D=core"
            "&filter%5Btagged%5D%5B0%5D%5Bkey%5D=env"
            "&filter%5Btagged%5D%5B0%5D%5Bvalue%5D=prod"
        )
        out = _parse_tag_filters(self._req(q))
        assert out == [("core", None), ("env", "prod")]


# ── VCS workflow + auto-merge (#282 phase 1) ───────────────────────────


class TestVcsWorkflowAttributes:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_default_workspace_serializes_default_workflow(self, mock_resolve, *_mocks):
        """Default-mode regression: an existing workspace serializes with the
        new attributes at default values. Frontend / providers must see the
        new fields, but their values are inert."""
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run = MagicMock()
        no_run.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["vcs-workflow"] == "merge_then_apply"
        assert attrs["auto-merge"] is False
        assert attrs["auto-merge-strategy"] == "merge"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_invalid_workflow_value_rejected(self, mock_resolve, *_mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        ws.vcs_connection_id = uuid.uuid4()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"vcs-workflow": "nonsense"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "vcs-workflow" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_apply_then_merge_requires_vcs_connection(self, mock_resolve, *_mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()  # no VCS connection
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"vcs-workflow": "apply_then_merge"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "VCS connection" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_apply_then_merge_incompatible_with_auto_apply(self, mock_resolve, *_mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace(auto_apply=True)
        ws.vcs_connection_id = uuid.uuid4()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"vcs-workflow": "apply_then_merge"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "auto-apply" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_combined_flip_off_auto_apply_and_into_apply_then_merge_succeeds(
        self, mock_resolve, *_mocks
    ):
        """User may set vcs-workflow=apply_then_merge AND auto-apply=false in
        one request — the validation evaluates the post-update state."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace(auto_apply=True)
        ws.vcs_connection_id = uuid.uuid4()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        # No active PR runs.
        active_result = MagicMock()
        active_result.all.return_value = []
        mock_db.execute.side_effect = [mock_result, active_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "vcs-workflow": "apply_then_merge",
                            "auto-apply": False,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert ws.vcs_workflow == "apply_then_merge"
        assert ws.auto_apply is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_workflow_flip_blocked_by_active_pr_runs(self, mock_resolve, *_mocks):
        """Q4 of the design: cannot flip vcs-workflow with PR runs in flight.
        Operator must cancel/discard them first."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        ws.vcs_workflow = "apply_then_merge"
        ws.vcs_connection_id = uuid.uuid4()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        # Two PR runs in flight.
        active_result = MagicMock()
        active_result.all.return_value = [(uuid.uuid4(),), (uuid.uuid4(),)]
        mock_db.execute.side_effect = [mock_result, active_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"vcs-workflow": "merge_then_apply"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422
        body = resp.json()["detail"]
        assert "2 PR run(s)" in body
        assert "Cancel" in body

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_invalid_auto_merge_strategy_rejected(self, mock_resolve, *_mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"auto-merge-strategy": "ff"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "merge" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
    async def test_auto_merge_toggle_persists(self, mock_resolve, *_mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "auto-merge": True,
                            "auto-merge-strategy": "squash",
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert ws.auto_merge is True
        assert ws.auto_merge_strategy == "squash"
