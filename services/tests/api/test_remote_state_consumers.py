"""Tests for cross-workspace remote-state consumer allowlist (#344).

Two layers:
- Unit tests of `_runner_state_read_allowed` in `tfe_v2` — the
  security-critical authorization branch that decides whether an
  agent-mode `terraform_remote_state` read is permitted.
- HTTP-level CRUD tests of `routers/remote_state_consumers` — the
  Terrapod-native management endpoints (mirrors run-trigger tests).
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.routers.tfe_v2 import _runner_state_read_allowed
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


# ── Helpers ──────────────────────────────────────────────────────────────


def _user(email="test@example.com", roles=None, auth_method="session", run_id=None):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method=auth_method,
        run_id=run_id,
    )


def _mock_ws(ws_id=None, name="test-ws"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    return ws


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── Unit: _runner_state_read_allowed (security-critical) ─────────────────


class TestRunnerStateReadAllowed:
    """Direct unit tests of the authz branch added to current-state-version
    and download. This is the security boundary — it decides whether a
    runner-token holder may read another workspace's secret-bearing state.
    """

    async def test_non_runner_principal_falls_through(self):
        """User/API-token principals must not be granted by this helper —
        they continue through the existing per-user RBAC path."""
        db = AsyncMock()
        producer = _mock_ws()
        assert await _runner_state_read_allowed(db, _user(auth_method="session"), producer) is False
        assert (
            await _runner_state_read_allowed(db, _user(auth_method="api_token"), producer) is False
        )
        # Should not have touched the DB.
        db.execute.assert_not_awaited()

    async def test_runner_without_run_id_denied(self):
        db = AsyncMock()
        producer = _mock_ws()
        user = _user(auth_method="runner_token", run_id=None)
        assert await _runner_state_read_allowed(db, user, producer) is False
        db.execute.assert_not_awaited()

    async def test_runner_with_invalid_run_id_denied(self):
        db = AsyncMock()
        producer = _mock_ws()
        user = _user(auth_method="runner_token", run_id="not-a-uuid")
        assert await _runner_state_read_allowed(db, user, producer) is False
        db.execute.assert_not_awaited()

    async def test_runner_with_missing_run_denied(self):
        """Runner token references a run that no longer exists → fail safe."""
        db = AsyncMock()
        producer = _mock_ws()
        result = MagicMock()
        result.first.return_value = None
        db.execute.return_value = result
        user = _user(auth_method="runner_token", run_id=str(uuid.uuid4()))
        assert await _runner_state_read_allowed(db, user, producer) is False

    async def test_runner_self_read_granted(self):
        """A runner reading its OWN workspace's state via the v2 endpoint
        is harmless (the runner already owns its state via the artifact
        path). Granting self-reads here keeps the contract simple."""
        producer = _mock_ws()
        db = AsyncMock()
        run_result = MagicMock()
        # `first()` returns a row tuple — Run.workspace_id IS producer.id.
        run_result.first.return_value = (producer.id,)
        db.execute.return_value = run_result
        user = _user(auth_method="runner_token", run_id=str(uuid.uuid4()))
        assert await _runner_state_read_allowed(db, user, producer) is True

    async def test_runner_consumer_in_allowlist_granted(self):
        """Runner from workspace B reads producer A, and B is in A's
        consumer allowlist → granted."""
        producer = _mock_ws(name="producer")
        consumer_id = uuid.uuid4()
        db = AsyncMock()
        run_result = MagicMock()
        run_result.first.return_value = (consumer_id,)
        grant_result = MagicMock()
        grant_result.scalar_one_or_none.return_value = uuid.uuid4()  # grant row found
        db.execute.side_effect = [run_result, grant_result]
        user = _user(auth_method="runner_token", run_id=str(uuid.uuid4()))
        assert await _runner_state_read_allowed(db, user, producer) is True

    async def test_runner_consumer_not_in_allowlist_denied(self):
        """Runner from workspace B reads producer A; B is NOT in A's
        allowlist → denied. This is the core security gate."""
        producer = _mock_ws(name="producer")
        consumer_id = uuid.uuid4()
        db = AsyncMock()
        run_result = MagicMock()
        run_result.first.return_value = (consumer_id,)
        grant_result = MagicMock()
        grant_result.scalar_one_or_none.return_value = None  # not in allowlist
        db.execute.side_effect = [run_result, grant_result]
        user = _user(auth_method="runner_token", run_id=str(uuid.uuid4()))
        assert await _runner_state_read_allowed(db, user, producer) is False


# ── HTTP: management router CRUD ─────────────────────────────────────────


class TestCreateConsumer:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_create_happy_path(self, mock_resolve, *_):
        """Admin on producer → 201."""
        mock_resolve.return_value = "admin"
        producer = _mock_ws(name="producer")
        consumer = _mock_ws(name="consumer")

        app, db = _make_app(_user())
        # 1) producer lookup
        r1 = MagicMock()
        r1.scalar_one_or_none.return_value = producer
        # 2) consumer lookup
        r2 = MagicMock()
        r2.scalar_one_or_none.return_value = consumer
        # 3) existing-grant check (none)
        r3 = MagicMock()
        r3.scalar_one_or_none.return_value = None
        # 4) count check (0)
        r4 = MagicMock()
        r4.scalar_one.return_value = 0
        db.execute.side_effect = [r1, r2, r3, r4]
        db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/workspaces/ws-{producer.id}/remote-state-consumers",
                json={
                    "data": {
                        "relationships": {
                            "consumer": {"data": {"id": f"ws-{consumer.id}", "type": "workspaces"}}
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        assert resp.json()["data"]["type"] == "remote-state-consumers"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_create_requires_producer_admin(self, mock_resolve, *_):
        """Read on producer is not enough — mutation requires admin."""
        mock_resolve.return_value = "read"
        producer = _mock_ws()

        app, db = _make_app(_user())
        r = MagicMock()
        r.scalar_one_or_none.return_value = producer
        db.execute.return_value = r

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/workspaces/ws-{producer.id}/remote-state-consumers",
                json={
                    "data": {"relationships": {"consumer": {"data": {"id": f"ws-{uuid.uuid4()}"}}}}
                },
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_create_self_reference_rejected(self, mock_resolve, *_):
        """A workspace listing itself is meaningless (already reads own state)."""
        mock_resolve.return_value = "admin"
        ws = _mock_ws()
        app, db = _make_app(_user())
        r = MagicMock()
        r.scalar_one_or_none.return_value = ws
        db.execute.side_effect = [r, r]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/workspaces/ws-{ws.id}/remote-state-consumers",
                json={"data": {"relationships": {"consumer": {"data": {"id": f"ws-{ws.id}"}}}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_create_duplicate_rejected(self, mock_resolve, *_):
        mock_resolve.return_value = "admin"
        producer = _mock_ws(name="producer")
        consumer = _mock_ws(name="consumer")
        app, db = _make_app(_user())
        r1 = MagicMock()
        r1.scalar_one_or_none.return_value = producer
        r2 = MagicMock()
        r2.scalar_one_or_none.return_value = consumer
        r3 = MagicMock()
        r3.scalar_one_or_none.return_value = MagicMock()  # existing row
        db.execute.side_effect = [r1, r2, r3]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/workspaces/ws-{producer.id}/remote-state-consumers",
                json={
                    "data": {"relationships": {"consumer": {"data": {"id": f"ws-{consumer.id}"}}}}
                },
                headers=_AUTH,
            )
        assert resp.status_code == 409


class TestListConsumers:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_list_outbound_default(self, mock_resolve, *_):
        """No filter ⇒ outbound (workspaces I share to)."""
        mock_resolve.return_value = "read"
        producer = _mock_ws()
        app, db = _make_app(_user())
        ws_lookup = MagicMock()
        ws_lookup.scalar_one_or_none.return_value = producer
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = []
        db.execute.side_effect = [ws_lookup, list_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/workspaces/ws-{producer.id}/remote-state-consumers",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json() == {"data": []}

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_list_invalid_filter_rejected(self, mock_resolve, *_):
        mock_resolve.return_value = "read"
        producer = _mock_ws()
        app, db = _make_app(_user())
        r = MagicMock()
        r.scalar_one_or_none.return_value = producer
        db.execute.return_value = r

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/workspaces/ws-{producer.id}/remote-state-consumers"
                "?filter[remote-state-consumer][type]=sideways",
                headers=_AUTH,
            )
        assert resp.status_code == 422


class TestDeleteConsumer:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_delete_happy(self, mock_resolve, *_):
        """Admin on producer → 204."""
        mock_resolve.return_value = "admin"
        producer = _mock_ws()
        row = MagicMock()
        row.id = uuid.uuid4()
        row.producer_workspace = producer

        app, db = _make_app(_user())
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        db.execute.return_value = result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/remote-state-consumers/rsc-{row.id}", headers=_AUTH
            )
        assert resp.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.remote_state_consumers.resolve_workspace_permission")
    async def test_delete_requires_producer_admin(self, mock_resolve, *_):
        """A consumer (or any non-admin) cannot revoke their own grant
        — only the producer's admin may delete an edge."""
        mock_resolve.return_value = "write"  # not admin
        producer = _mock_ws()
        row = MagicMock()
        row.id = uuid.uuid4()
        row.producer_workspace = producer

        app, db = _make_app(_user())
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        db.execute.return_value = result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/remote-state-consumers/rsc-{row.id}", headers=_AUTH
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_404(self, *_):
        app, db = _make_app(_user())
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute.return_value = result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/remote-state-consumers/rsc-{uuid.uuid4()}",
                headers=_AUTH,
            )
        assert resp.status_code == 404


# Silence unused-import warning when pytest doesn't directly need datetime.
assert datetime(2026, 1, 1, tzinfo=UTC) and pytest is not None
