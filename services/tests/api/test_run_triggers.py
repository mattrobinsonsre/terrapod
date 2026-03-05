"""Tests for run trigger CRUD endpoints and trigger firing logic."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
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


def _mock_workspace(ws_id=None, name="test-ws"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.auto_apply = False
    return ws


def _mock_trigger(trigger_id=None, ws=None, source_ws=None):
    trigger = MagicMock()
    trigger.id = trigger_id or uuid.uuid4()
    ws = ws or _mock_workspace()
    source_ws = source_ws or _mock_workspace(name="source-ws")
    trigger.workspace_id = ws.id
    trigger.source_workspace_id = source_ws.id
    trigger.workspace = ws
    trigger.source_workspace = source_ws
    trigger.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return trigger


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── Create Run Trigger ─────────────────────────────────────────────────


class TestCreateRunTrigger:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_create_run_trigger(self, mock_resolve, *mocks):
        """Happy path: create a run trigger → 201."""
        mock_resolve.return_value = "admin"
        dest_ws = _mock_workspace(name="dest")
        source_ws = _mock_workspace(name="source")

        user = _user()
        app, mock_db = _make_app(user)

        # First call returns dest ws, second returns source ws
        mock_result_dest = MagicMock()
        mock_result_dest.scalar_one_or_none.return_value = dest_ws
        mock_result_source = MagicMock()
        mock_result_source.scalar_one_or_none.return_value = source_ws
        # Third call: check existing trigger (None), Fourth: count (0)
        mock_result_existing = MagicMock()
        mock_result_existing.scalar_one_or_none.return_value = None
        mock_result_count = MagicMock()
        mock_result_count.scalar_one.return_value = 0

        mock_db.execute.side_effect = [
            mock_result_dest,
            mock_result_source,
            mock_result_existing,
            mock_result_count,
        ]
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{dest_ws.id}/run-triggers",
                json={
                    "data": {
                        "relationships": {
                            "sourceable": {
                                "data": {"id": f"ws-{source_ws.id}", "type": "workspaces"}
                            }
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        assert resp.json()["data"]["type"] == "run-triggers"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_create_self_referential_rejected(self, mock_resolve, *mocks):
        """Same source and destination workspace → 422."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.side_effect = [mock_result, mock_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/run-triggers",
                json={
                    "data": {
                        "relationships": {
                            "sourceable": {"data": {"id": f"ws-{ws.id}", "type": "workspaces"}}
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "cannot trigger itself" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_create_duplicate_rejected(self, mock_resolve, *mocks):
        """Same pair twice → 409."""
        mock_resolve.return_value = "admin"
        dest_ws = _mock_workspace(name="dest")
        source_ws = _mock_workspace(name="source")

        app, mock_db = _make_app(_user())
        mock_result_dest = MagicMock()
        mock_result_dest.scalar_one_or_none.return_value = dest_ws
        mock_result_source = MagicMock()
        mock_result_source.scalar_one_or_none.return_value = source_ws
        mock_result_existing = MagicMock()
        mock_result_existing.scalar_one_or_none.return_value = _mock_trigger()

        mock_db.execute.side_effect = [
            mock_result_dest,
            mock_result_source,
            mock_result_existing,
        ]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{dest_ws.id}/run-triggers",
                json={
                    "data": {
                        "relationships": {
                            "sourceable": {
                                "data": {"id": f"ws-{source_ws.id}", "type": "workspaces"}
                            }
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 409

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_create_max_20_sources(self, mock_resolve, *mocks):
        """21st source → 422."""
        mock_resolve.return_value = "admin"
        dest_ws = _mock_workspace(name="dest")
        source_ws = _mock_workspace(name="source")

        app, mock_db = _make_app(_user())
        mock_result_dest = MagicMock()
        mock_result_dest.scalar_one_or_none.return_value = dest_ws
        mock_result_source = MagicMock()
        mock_result_source.scalar_one_or_none.return_value = source_ws
        mock_result_existing = MagicMock()
        mock_result_existing.scalar_one_or_none.return_value = None
        mock_result_count = MagicMock()
        mock_result_count.scalar_one.return_value = 20  # Already at max

        mock_db.execute.side_effect = [
            mock_result_dest,
            mock_result_source,
            mock_result_existing,
            mock_result_count,
        ]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{dest_ws.id}/run-triggers",
                json={
                    "data": {
                        "relationships": {
                            "sourceable": {
                                "data": {"id": f"ws-{source_ws.id}", "type": "workspaces"}
                            }
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "Maximum" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_create_requires_admin(self, mock_resolve, *mocks):
        """Non-admin → 403."""
        mock_resolve.return_value = "write"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/run-triggers",
                json={
                    "data": {
                        "relationships": {
                            "sourceable": {
                                "data": {"id": f"ws-{uuid.uuid4()}", "type": "workspaces"}
                            }
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 403


# ── List Run Triggers ──────────────────────────────────────────────────


class TestListRunTriggers:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_list_inbound(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        trigger = _mock_trigger(ws=ws)

        app, mock_db = _make_app(_user())
        mock_result_ws = MagicMock()
        mock_result_ws.scalar_one_or_none.return_value = ws
        mock_result_triggers = MagicMock()
        mock_result_triggers.scalars.return_value.all.return_value = [trigger]

        mock_db.execute.side_effect = [mock_result_ws, mock_result_triggers]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/run-triggers",
                params={"filter[run-trigger][type]": "inbound"},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_list_outbound(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        trigger = _mock_trigger(source_ws=ws)

        app, mock_db = _make_app(_user())
        mock_result_ws = MagicMock()
        mock_result_ws.scalar_one_or_none.return_value = ws
        mock_result_triggers = MagicMock()
        mock_result_triggers.scalars.return_value.all.return_value = [trigger]

        mock_db.execute.side_effect = [mock_result_ws, mock_result_triggers]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/run-triggers",
                params={"filter[run-trigger][type]": "outbound"},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_list_requires_filter(self, mock_resolve, *mocks):
        """Missing filter → 422."""
        mock_resolve.return_value = "read"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/run-triggers",
                headers=_AUTH,
            )
        assert resp.status_code == 422


# ── Show Run Trigger ───────────────────────────────────────────────────


class TestShowRunTrigger:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_show_run_trigger(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        trigger = _mock_trigger()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = trigger
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/run-triggers/rt-{trigger.id}", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "run-triggers"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_not_found(self, *mocks):
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/run-triggers/rt-{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404


# ── Delete Run Trigger ─────────────────────────────────────────────────


class TestDeleteRunTrigger:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_triggers.resolve_workspace_permission")
    async def test_delete_run_trigger(self, mock_resolve, *mocks):
        """Happy path → 204."""
        mock_resolve.return_value = "admin"
        trigger = _mock_trigger()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = trigger
        mock_db.execute.return_value = mock_result
        mock_db.delete = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/v2/run-triggers/rt-{trigger.id}", headers=_AUTH)
        assert resp.status_code == 204


# ── Trigger Firing ─────────────────────────────────────────────────────


class TestFireRunTriggers:
    @patch("terrapod.services.run_service.queue_run")
    @patch("terrapod.services.run_service.create_run")
    async def test_fire_triggers_on_apply(self, mock_create, mock_queue):
        """Apply completes → downstream run created."""
        from terrapod.services.run_service import fire_run_triggers

        source_ws_id = uuid.uuid4()
        dest_ws = _mock_workspace(name="downstream")

        trigger = MagicMock()
        trigger.workspace = dest_ws

        source_ws = _mock_workspace(ws_id=source_ws_id, name="upstream")

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [trigger]
        mock_db.execute.return_value = mock_result
        mock_db.get.return_value = source_ws

        downstream_run = MagicMock()
        mock_create.return_value = downstream_run
        mock_queue.return_value = downstream_run

        await fire_run_triggers(mock_db, source_ws_id)

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["workspace"] == dest_ws
        assert "upstream" in call_kwargs[1]["message"]
        mock_queue.assert_called_once_with(mock_db, downstream_run)

    @patch("terrapod.services.run_service.fire_run_triggers")
    async def test_plan_only_does_not_fire(self, mock_fire):
        """Speculative run → no downstream run."""
        from terrapod.services.run_service import transition_run

        run = MagicMock()
        run.status = "applying"
        run.plan_only = True
        run.plan_started_at = None
        run.plan_finished_at = None
        run.apply_started_at = datetime(2026, 1, 1, tzinfo=UTC)
        run.apply_finished_at = None

        mock_db = AsyncMock()

        await transition_run(mock_db, run, "applied")

        mock_fire.assert_not_called()

    @patch("terrapod.services.run_service.fire_run_triggers")
    async def test_non_plan_only_fires(self, mock_fire):
        """Non-speculative apply → fires triggers."""
        from terrapod.services.run_service import transition_run

        run = MagicMock()
        run.status = "applying"
        run.plan_only = False
        run.plan_started_at = None
        run.plan_finished_at = None
        run.apply_started_at = datetime(2026, 1, 1, tzinfo=UTC)
        run.apply_finished_at = None

        mock_db = AsyncMock()

        await transition_run(mock_db, run, "applied")

        mock_fire.assert_called_once_with(mock_db, run.workspace_id)
