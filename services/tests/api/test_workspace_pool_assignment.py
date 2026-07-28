"""Tests for workspace agent pool assignment with pool RBAC gating."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
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


def _mock_workspace(ws_id=None, pool_id=None, extra_pool_ids=None):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = "test-ws"
    ws.execution_mode = "agent"
    ws.auto_apply = False
    ws.execution_backend = "tofu"
    ws.terraform_version = "1.11"
    ws.terragrunt_enabled = False
    ws.terragrunt_version = "1.0"
    ws.working_directory = ""
    ws.locked = False
    ws.lock_id = None
    ws.agent_pool_id = pool_id
    # Real column, real default (#1085) — a MagicMock here would leak into the
    # serialized pool set.
    ws.agent_pool_extra_ids = extra_pool_ids if extra_pool_ids is not None else []
    ws.agent_pool = None
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.labels = {}
    ws.owner_email = "test@example.com"
    ws.vcs_connection_id = None
    ws.vcs_connection = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.vcs_last_commit_sha = ""
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
    ws.var_files = []
    ws.trigger_prefixes = []
    ws.drift_ignore_rules = []
    ws.drift_detection_enabled = False
    ws.drift_detection_interval_seconds = 86400
    ws.security_scan_enforcement = "advisory"
    ws.security_scan_engine = "checkov"
    ws.security_scan_severity_threshold = "high"
    ws.security_scan_skip_rules = []
    ws.plan_expiry_seconds = None
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
    ws.slack_channel = ""
    ws.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    ws.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return ws


def _mock_pool(pool_id=None, name="test-pool", labels=None, owner_email=None):
    pool = MagicMock()
    pool.id = pool_id or uuid.uuid4()
    pool.name = name
    pool.labels = labels or {}
    pool.owner_email = owner_email
    return pool


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


class TestWorkspacePoolAssignment:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_capabilities_for",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_assign_pool_with_write_permission(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """User with write on pool can assign it to workspace."""
        user = _user(roles=["everyone", "pool-writer"])
        pool = _mock_pool(name="prod-pool")
        ws = _mock_workspace()

        mock_ws_perm.return_value = caps_for_level("admin")
        mock_get_pool.return_value = pool
        mock_pool_perm.return_value = caps_for_level("write")

        app, mock_db = _make_app(user)

        # Mock workspace lookup
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": f"apool-{pool.id}",
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_capabilities_for",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_assign_pool_without_write_permission_403(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """User without pool:assign capability gets 403."""
        user = _user(roles=["everyone"])
        pool = _mock_pool(name="restricted-pool")
        ws = _mock_workspace()

        mock_ws_perm.return_value = caps_for_level("admin")
        mock_get_pool.return_value = pool
        mock_pool_perm.return_value = caps_for_level("read")  # Only read, no pool:assign

        app, mock_db = _make_app(user)

        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": f"apool-{pool.id}",
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 403
        assert "write permission" in res.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_clear_pool_no_permission_check(self, mock_ws_perm, *mocks):
        """Clearing pool (setting null) does not require pool permission."""
        user = _user(roles=["everyone"])
        ws = _mock_workspace(pool_id=uuid.uuid4())

        mock_ws_perm.return_value = caps_for_level("admin")

        app, mock_db = _make_app(user)

        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": None,
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_capabilities_for",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_platform_admin_bypasses_pool_check(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """Platform admin can assign any pool."""
        user = _user(email="admin@example.com", roles=["admin"])
        pool = _mock_pool(name="restricted-pool")
        ws = _mock_workspace()

        mock_ws_perm.return_value = caps_for_level("admin")
        mock_get_pool.return_value = pool
        mock_pool_perm.return_value = caps_for_level("admin")  # admin resolves to admin

        app, mock_db = _make_app(user)

        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": f"apool-{pool.id}",
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200


class TestWorkspacePoolSet:
    """Multi-pool routing (#1085) — the API carries both attributes."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_capabilities_for",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_assign_several_pools(self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks):
        user = _user(roles=["everyone", "pool-writer"])
        pool_a, pool_b = _mock_pool(name="eu"), _mock_pool(name="us")
        ws = _mock_workspace()

        mock_ws_perm.return_value = caps_for_level("admin")
        mock_get_pool.side_effect = lambda _db, pid: {pool_a.id: pool_a, pool_b.id: pool_b}[pid]
        mock_pool_perm.return_value = caps_for_level("write")

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-ids": [f"apool-{pool_a.id}", f"apool-{pool_b.id}"],
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200, res.text
        # Element 0 lands in the pre-existing column, the rest in the new one —
        # a storage split, not a preference.
        assert ws.agent_pool_id == pool_a.id
        assert ws.agent_pool_extra_ids == [str(pool_b.id)]
        attrs = res.json()["data"]["attributes"]
        assert attrs["agent-pool-ids"] == [f"apool-{pool_a.id}", f"apool-{pool_b.id}"]
        # The singular attribute stays, resolving to element 0.
        assert attrs["agent-pool-id"] == f"apool-{pool_a.id}"
        rels = res.json()["data"]["relationships"]
        assert [d["id"] for d in rels["agent-pools"]["data"]] == [
            f"apool-{pool_a.id}",
            f"apool-{pool_b.id}",
        ]
        assert rels["agent-pool"]["data"]["id"] == f"apool-{pool_a.id}"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_capabilities_for",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_pool_assign_required_on_every_pool(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """403 when the caller lacks pool:assign on ANY pool in the set.

        A caller must not be able to attach a workspace to a pool they could
        not have attached it to on its own.
        """
        user = _user(roles=["everyone"])
        allowed, denied = _mock_pool(name="mine"), _mock_pool(name="theirs")
        ws = _mock_workspace()

        mock_ws_perm.return_value = caps_for_level("admin")
        mock_get_pool.side_effect = lambda _db, pid: {allowed.id: allowed, denied.id: denied}[pid]
        mock_pool_perm.side_effect = lambda *a, **kw: (
            caps_for_level("write") if kw.get("pool_name") == "mine" else caps_for_level("read")
        )

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-ids": [f"apool-{allowed.id}", f"apool-{denied.id}"],
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 403
        # Nothing was written — the check runs before any mutation.
        assert ws.agent_pool_extra_ids == []

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_setting_both_attributes_is_422(self, mock_ws_perm, *mocks):
        user = _user(roles=["admin"])
        ws = _mock_workspace()
        mock_ws_perm.return_value = caps_for_level("admin")

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": f"apool-{uuid.uuid4()}",
                            "agent-pool-ids": [f"apool-{uuid.uuid4()}"],
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 422
        assert "not both" in res.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_capabilities_for",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_singular_write_replaces_the_whole_set(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """The documented semantic: `agent-pool-id` means "exactly this one".

        This is also how an un-upgraded client drops a pool it cannot see, so
        the behaviour is pinned deliberately rather than left to chance.
        """
        user = _user(roles=["admin"])
        old_a, old_b, new = uuid.uuid4(), uuid.uuid4(), _mock_pool(name="replacement")
        ws = _mock_workspace(pool_id=old_a, extra_pool_ids=[str(old_b)])

        mock_ws_perm.return_value = caps_for_level("admin")
        mock_get_pool.return_value = new
        mock_pool_perm.return_value = caps_for_level("write")

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"agent-pool-id": f"apool-{new.id}"}}},
                headers=_AUTH,
            )

        assert res.status_code == 200, res.text
        assert ws.agent_pool_id == new.id
        assert ws.agent_pool_extra_ids == []

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_patch_without_pool_attributes_leaves_the_set_alone(self, mock_ws_perm, *mocks):
        """Back-compat: a PATCH that says nothing about pools changes nothing."""
        user = _user(roles=["admin"])
        a, b = uuid.uuid4(), uuid.uuid4()
        ws = _mock_workspace(pool_id=a, extra_pool_ids=[str(b)])
        mock_ws_perm.return_value = caps_for_level("admin")

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"auto-apply": True}}},
                headers=_AUTH,
            )

        assert res.status_code == 200, res.text
        assert ws.agent_pool_id == a
        assert ws.agent_pool_extra_ids == [str(b)]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_single_pool_workspace_serializes_as_before(self, mock_ws_perm, *mocks):
        """A workspace with one pool must serialize exactly as it did pre-#1085."""
        user = _user(roles=["admin"])
        pool_id = uuid.uuid4()
        ws = _mock_workspace(pool_id=pool_id)
        mock_ws_perm.return_value = caps_for_level("admin")

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        # The GET also looks up the workspace's latest run.
        no_run = MagicMock()
        no_run.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        assert res.status_code == 200, res.text
        attrs = res.json()["data"]["attributes"]
        assert attrs["agent-pool-id"] == f"apool-{pool_id}"
        assert attrs["agent-pool-ids"] == [f"apool-{pool_id}"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_unknown_liveness_raises_no_alarm(self, mock_ws_perm, *mocks):
        """Redis unreachable must NOT look like "every pool is dead" (#1085).

        `live_pool_ids` returns None when it couldn't tell, and the health
        condition is skipped — a false "no runner for this workspace" banner
        across the whole estate on a Redis blip would be worse than no banner.
        """
        user = _user(roles=["admin"])
        ws = _mock_workspace(pool_id=uuid.uuid4())
        mock_ws_perm.return_value = caps_for_level("admin")

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run = MagicMock()
        no_run.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run]

        with patch(
            "terrapod.api.routers.tfe_v2._agent_pool_service.live_pool_ids",
            new_callable=AsyncMock,
            return_value=None,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                res = await client.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        assert res.status_code == 200, res.text
        codes = [c["code"] for c in res.json()["data"]["attributes"]["health-conditions"]]
        assert "no_live_agent_pool" not in codes

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
        new_callable=AsyncMock,
    )
    async def test_all_pools_dark_raises_the_alarm(self, mock_ws_perm, *mocks):
        """Losing ONE pool is survivable; losing them all is the alertable state."""
        user = _user(roles=["admin"])
        a, b = uuid.uuid4(), uuid.uuid4()
        ws = _mock_workspace(pool_id=a, extra_pool_ids=[str(b)])
        mock_ws_perm.return_value = caps_for_level("admin")

        app, mock_db = _make_app(user)
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run = MagicMock()
        no_run.scalar_one_or_none.return_value = None

        # One pool still live → no alarm.
        mock_db.execute.side_effect = [ws_result, no_run]
        with patch(
            "terrapod.api.routers.tfe_v2._agent_pool_service.live_pool_ids",
            new_callable=AsyncMock,
            return_value={b},
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                res = await client.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)
        assert res.status_code == 200, res.text
        codes = [c["code"] for c in res.json()["data"]["attributes"]["health-conditions"]]
        assert "no_live_agent_pool" not in codes

        # Both dark → alarm.
        mock_db.execute.side_effect = [ws_result, no_run]
        with patch(
            "terrapod.api.routers.tfe_v2._agent_pool_service.live_pool_ids",
            new_callable=AsyncMock,
            return_value=set(),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                res = await client.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)
        assert res.status_code == 200, res.text
        conditions = res.json()["data"]["attributes"]["health-conditions"]
        stranded = [c for c in conditions if c["code"] == "no_live_agent_pool"]
        assert len(stranded) == 1
        assert stranded[0]["severity"] == "error"
