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
                f"/api/terrapod/v1/runs/{run.id}/artifacts/state",
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
    async def test_race_window_falls_back_to_409(self, *_mocks):
        """SELECT-then-INSERT race: another upload inserts between our
        check and our flush. The unique constraint catches it; the
        IntegrityError handler translates to 409 instead of letting it
        surface as 500.
        """
        from sqlalchemy.exc import IntegrityError

        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)

        mock_db = AsyncMock()
        mock_db.get.return_value = run
        # First db.execute is the proactive lookup — returns None (no row).
        empty = MagicMock()
        empty.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = empty
        # The race: db.flush() raises IntegrityError as the constraint kicks in.
        mock_db.flush.side_effect = IntegrityError("INSERT", {}, Exception("uq_state_versions"))

        app = _make_app(_runner_user(run_id), mock_db)

        state_json = json.dumps({"version": 4, "serial": 8, "lineage": "abc"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/state",
                content=state_json,
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 409
        assert "serial 8 already exists" in resp.json()["detail"].lower()
        # Rollback was issued so the session is usable for the caller.
        mock_db.rollback.assert_awaited_once()

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
                f"/api/terrapod/v1/runs/{run.id}/artifacts/state",
                content=state_json,
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        mock_db.add.assert_called_once()
        mock_storage.put.assert_called_once()


# ── upload_plan_json_output (#280) ─────────────────────────────────────


class TestUploadPlanJsonOutput:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_writes_to_canonical_key(self, mock_get_storage, *_mocks):
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_storage = AsyncMock()
        mock_get_storage.return_value = mock_storage

        app = _make_app(_runner_user(run_id), mock_db)
        body = b'{"format_version":"1.2","resource_changes":[]}'
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=body,
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        mock_storage.put.assert_called_once()
        key, payload = mock_storage.put.call_args.args
        assert key == f"plans/{ws_id}/{run_id}.json-output"
        assert payload == body
        # Flag flip is the source of truth for `_plan_json` advertising the URL.
        assert run.has_json_output is True
        mock_db.commit.assert_awaited_once()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_403_for_wrong_run_scope(self, *_mocks):
        run_id = uuid.uuid4()
        wrong_run_id = uuid.uuid4()  # token scoped to a different run
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        app = _make_app(_runner_user(wrong_run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=b"{}",
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_populates_resource_counts_from_parsed_plan(self, mock_get_storage, *_mocks):
        """Issue #301: the upload handler parses the JSON plan and writes
        resource_additions / _changes / _destructions / _replacements /
        _imports onto the run row so the UI can render a summary badge.
        """
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        plan = {
            "resource_changes": [
                {"change": {"actions": ["create"]}},
                {"change": {"actions": ["create"]}},
                {"change": {"actions": ["update"]}},
                {"change": {"actions": ["delete"]}},
                {"change": {"actions": ["create", "delete"]}},
                {"change": {"actions": ["update"], "importing": {"id": "i-abc"}}},
            ]
        }

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=json.dumps(plan).encode(),
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        assert run.resource_additions == 2
        assert run.resource_changes == 2  # one plain update + one update-with-import
        assert run.resource_destructions == 1
        assert run.resource_replacements == 1
        assert run.resource_imports == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_malformed_plan_leaves_counts_null_but_still_204(self, mock_get_storage, *_mocks):
        """A parse failure must not fail the upload — the download URL
        is still served, the UI just won't show a summary.
        """
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        # Sentinel: count columns start unset and must STAY unset after a
        # malformed body.
        run.resource_additions = None
        run.resource_changes = None
        run.resource_destructions = None
        run.resource_replacements = None
        run.resource_imports = None
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=b"not json at all",
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        assert run.has_json_output is True
        assert run.resource_additions is None
        assert run.resource_changes is None


# ── lock-file (#306) ─────────────────────────────────────────────────────


class TestLockFile:
    """The .terraform.lock.hcl from plan is carried to apply via these
    endpoints so both phases resolve to the same provider versions.
    """

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_upload_writes_to_canonical_key(self, mock_get_storage, *_mocks):
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_storage = AsyncMock()
        mock_get_storage.return_value = mock_storage

        app = _make_app(_runner_user(run_id), mock_db)
        body = b'# This file is maintained automatically by "terraform init".\n'
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/lock-file",
                content=body,
                headers={**_AUTH, "Content-Type": "application/octet-stream"},
            )

        assert resp.status_code == 204
        mock_storage.put.assert_called_once()
        key, payload = mock_storage.put.call_args.args
        assert key == f"plans/{ws_id}/{run_id}.terraform.lock.hcl"
        assert payload == body

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_upload_403_for_wrong_run_scope(self, *_mocks):
        run_id = uuid.uuid4()
        wrong_run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        app = _make_app(_runner_user(wrong_run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/lock-file",
                content=b"",
                headers={**_AUTH, "Content-Type": "application/octet-stream"},
            )

        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_download_redirects_to_presigned_url(self, mock_get_storage, *_mocks):
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        mock_storage = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://example.invalid/presigned"
        mock_storage.presigned_get_url.return_value = presigned
        mock_get_storage.return_value = mock_storage

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE, follow_redirects=False
        ) as client:
            resp = await client.get(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/lock-file",
                headers=_AUTH,
            )

        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.invalid/presigned"
        # Hits the same per-run key the upload writes to.
        mock_storage.presigned_get_url.assert_awaited_once_with(
            f"plans/{ws_id}/{run_id}.terraform.lock.hcl"
        )
