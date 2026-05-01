"""Tests for runner artifact upload/download endpoints."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import StateVersion
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer runtok:dummy"}


def _runner_user(run_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        email="runner",
        display_name="Runner Job",
        roles=["everyone"],
        provider_name="runner_token",
        auth_method="runner_token",
        run_id=str(run_id),
    )


def _mock_run(run_id=None, ws_id=None):
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.workspace_id = ws_id or uuid.uuid4()
    run.created_by = "matt@example.com"
    return run


def _make_app(user, mock_db):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db
    return app


# ── upload_state — duplicate-serial 409 regression ────────────────────


class TestUploadStateDuplicateSerial:
    """Re-uploading state with an already-used serial returns 409, not 500.

    A no-op `tofu apply` doesn't bump the state serial — without this
    check, the re-upload triggers `IntegrityError` on `uq_state_versions`
    and FastAPI surfaces a 500. The reconciler short-circuit (no-op
    planned → applied) prevents this path from being hit in steady state,
    but the explicit 409 keeps the API correct against legacy clients,
    races, and any future caller that re-uploads stale state.
    """

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_existing_serial_returns_409(self, *_mocks):
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)

        mock_db = AsyncMock()
        # `_get_run` does db.get(Run, uuid); the dup-serial check does
        # db.execute(select(StateVersion)) — different call sites, separate mocks.
        mock_db.get.return_value = run
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = MagicMock(spec=StateVersion)
        mock_db.execute.return_value = existing

        app = _make_app(_runner_user(run_id), mock_db)

        state_json = json.dumps({"version": 4, "serial": 8, "lineage": "abc"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/v2/runs/{run.id}/artifacts/state",
                content=state_json,
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert "serial 8 already exists" in body["detail"].lower()
        # Critically, the StateVersion row was NOT inserted.
        mock_db.add.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_new_serial_succeeds(self, mock_get_storage, *_mocks):
        """Sanity: a genuinely new serial still goes through (no false-positive 409)."""
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)

        mock_db = AsyncMock()
        # First db.get is for Run (in _get_run). Second is for Workspace
        # (in the post-insert state_diverged clear branch).
        ws = MagicMock()
        ws.state_diverged = False
        mock_db.get.side_effect = [run, ws]
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = None  # no conflict
        mock_db.execute.return_value = existing

        mock_storage = AsyncMock()
        mock_get_storage.return_value = mock_storage

        app = _make_app(_runner_user(run_id), mock_db)

        state_json = json.dumps({"version": 4, "serial": 9, "lineage": "abc"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/v2/runs/{run.id}/artifacts/state",
                content=state_json,
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        mock_db.add.assert_called_once()
        mock_storage.put.assert_called_once()
