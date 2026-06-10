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


def _mock_run(run_id=None, ws_id=None, is_drift_detection=False):
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.workspace_id = ws_id or uuid.uuid4()
    run.created_by = "matt@example.com"
    # Default False so the AI-summary tests see exactly one enqueue; the
    # drift-reclassify path (#482) is opt-in per test via True.
    run.is_drift_detection = is_drift_detection
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

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_existing_serial_same_md5_is_idempotent_200(self, mock_get_storage, *_mocks):
        """A serial-neutral no-op apply (state byte-identical to the recorded
        state at the same serial) is NOT a divergence. The API treats it as an
        idempotent success (200), does NOT insert a new row, does NOT store the
        blob, and clears any stale state_diverged flag — so the runner never
        signals state-diverged. This is the defence-in-depth half of the
        auth0-perpetual-diff fix (the runner also skips the upload entirely)."""
        import hashlib

        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)

        state_json = json.dumps({"version": 4, "serial": 8, "lineage": "abc"})
        body_md5 = hashlib.md5(state_json.encode()).hexdigest()  # noqa: S324

        mock_db = AsyncMock()
        # The existing state version at serial 8 has the SAME content hash.
        existing_sv = MagicMock(spec=StateVersion)
        existing_sv.md5 = body_md5
        ws = MagicMock()
        ws.state_diverged = True  # stale flag from a prior mis-fire
        # db.get: first _get_run(Run), then db.get(Workspace) in the no-op branch.
        mock_db.get.side_effect = [run, ws]
        lookup = MagicMock()
        lookup.scalar_one_or_none.return_value = existing_sv
        mock_db.execute.return_value = lookup

        mock_storage = AsyncMock()
        mock_get_storage.return_value = mock_storage

        app = _make_app(_runner_user(run_id), mock_db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/state",
                content=state_json,
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        # No new row, no blob write — there is nothing to persist.
        mock_db.add.assert_not_called()
        mock_storage.put.assert_not_called()
        # Stale divergence flag cleared.
        assert ws.state_diverged is False
        mock_db.commit.assert_awaited()


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

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    @patch("terrapod.api.routers.run_artifacts.settings")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_enqueues_ai_plan_summary_after_upload(
        self, mock_enq, mock_settings, mock_get_storage, *_mocks
    ):
        """The plan_summary trigger must fire only after the JSON has
        landed in storage — otherwise the summariser races the runner
        and hits "Object not found" (v0.30.2 fix).
        """
        mock_settings.ai_summary.enabled = True
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=b'{"resource_changes":[]}',
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        mock_enq.assert_awaited_once()
        args, kwargs = mock_enq.call_args
        assert args[0] == "ai_plan_summary"
        assert args[1] == {"run_id": str(run_id), "kind": "plan_summary"}
        assert kwargs.get("dedup_key") == f"aisum:{run_id}:plan_summary"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    @patch("terrapod.api.routers.run_artifacts.settings")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_skips_ai_plan_summary_when_disabled(
        self, mock_enq, mock_settings, mock_get_storage, *_mocks
    ):
        mock_settings.ai_summary.enabled = False
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=b'{"resource_changes":[]}',
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        mock_enq.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    @patch("terrapod.api.routers.run_artifacts.settings")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_reenqueues_drift_completion_for_drift_run(
        self, mock_enq, mock_settings, mock_get_storage, *_mocks
    ):
        """A drift-detection run must re-fire drift_run_completed AFTER the
        plan JSON lands (#482). handle_drift_run_completed first runs on
        the planned transition — before this upload — when has_json_output
        is still False, so the ignore-rule classifier can't read the plan
        and conservatively leaves drift_status='drifted'. Re-enqueuing here
        lets it re-run with the JSON available and flip to no_drift. Was
        the v0.36.1 fix.
        """
        mock_settings.ai_summary.enabled = False  # isolate the drift enqueue
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id, is_drift_detection=True)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=b'{"resource_changes":[]}',
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        mock_enq.assert_awaited_once()
        args, kwargs = mock_enq.call_args
        assert args[0] == "drift_run_completed"
        assert args[1] == {"run_id": str(run_id), "workspace_id": str(ws_id)}
        # Distinct dedup key from the transition-time enqueue (drift:{id})
        # so this re-trigger isn't swallowed by the 60s dedup window.
        assert kwargs.get("dedup_key") == f"drift_postjson:{run_id}"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    @patch("terrapod.api.routers.run_artifacts.settings")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_no_drift_reenqueue_for_normal_run(
        self, mock_enq, mock_settings, mock_get_storage, *_mocks
    ):
        """A normal (non-drift) run must NOT re-enqueue drift_run_completed."""
        mock_settings.ai_summary.enabled = False
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, is_drift_detection=False)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-json-output",
                content=b'{"resource_changes":[]}',
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 204
        mock_enq.assert_not_called()


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


# ── upload_plan_log / upload_apply_log — log_updated SSE publish ──────


class TestLogUploadPublishesEvent:
    """Final log uploads from the runner's EXIT trap MUST publish a
    `log_updated` SSE event. Otherwise the UI — which only re-fetches
    on `log_updated` — sits on its last Redis-snapshot polled
    mid-flight and never picks up the trailing bytes that storage now
    holds (PR #424's symptom: `[entrypoint] PLAN_HAS_CHANGES=true` and
    post-plan OPA lines missing until refresh).

    The companion server-side fix (PR #423) already keeps the stream
    open by withholding ETX on the Redis path; this event is the
    matching nudge that makes the UI come back for the tail.
    """

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_plan_log_upload_publishes_log_updated(
        self, mock_get_storage, mock_publish, *_mocks
    ):
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-log",
                content=b"final plan log bytes",
                headers={**_AUTH, "Content-Type": "text/plain"},
            )

        assert resp.status_code == 204
        mock_publish.assert_awaited_once()
        channel, payload = mock_publish.await_args.args
        assert channel == f"tp:run_events:{ws_id}"
        body = json.loads(payload)
        assert body == {
            "event": "log_updated",
            "run_id": str(run_id),
            "workspace_id": str(ws_id),
            "phase": "plan",
        }

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_apply_log_upload_publishes_log_updated(
        self, mock_get_storage, mock_publish, *_mocks
    ):
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, ws_id=ws_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/apply-log",
                content=b"final apply log bytes",
                headers={**_AUTH, "Content-Type": "text/plain"},
            )

        assert resp.status_code == 204
        mock_publish.assert_awaited_once()
        _, payload = mock_publish.await_args.args
        body = json.loads(payload)
        assert body["event"] == "log_updated"
        assert body["phase"] == "apply"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.api.routers.run_artifacts.get_storage")
    async def test_publish_failure_does_not_break_upload(
        self, mock_get_storage, mock_publish, *_mocks
    ):
        """A Redis publish error must NOT fail the artifact upload —
        the log file has already landed in storage; the worst case is
        the UI falls back to its pre-fix behaviour (refresh-required).
        Matches the same try/except guarantee in `upload_log_stream`.
        """
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run
        mock_get_storage.return_value = AsyncMock()
        mock_publish.side_effect = RuntimeError("redis is angry")

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run.id}/artifacts/plan-log",
                content=b"x",
                headers={**_AUTH, "Content-Type": "text/plain"},
            )

        assert resp.status_code == 204


# ── resource-profile (#430) ──────────────────────────────────────────


class TestResourceProfile:
    """The runner POSTs its cgroup-v2 peak memory/CPU + exit code at exit.

    Hard rules being protected here:
      - Runner-token auth, scoped to this run_id (mirrors other artifact endpoints)
      - Subset bodies accepted (runner may not be able to read all cgroup files)
      - Non-int / negative values → 400 (DB schema is unsigned-ish BIGINT)
      - runner_exit_status is NEVER set by this endpoint — that bucketing is
        owned by report_job_status / the reconciler (single source of truth)
    """

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_persists_all_fields(self, *_mocks):
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        run.runner_exit_status = ""
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/terrapod/v1/runs/{run.id}/resource-profile",
                json={
                    "peak_memory_bytes": 1_500_000_000,
                    "peak_cpu_usec": 42_000_000,
                    "exit_code": 0,
                },
                headers=_AUTH,
            )

        assert resp.status_code == 204
        assert run.peak_memory_bytes == 1_500_000_000
        assert run.peak_cpu_usec == 42_000_000
        assert run.runner_exit_code == 0
        # The endpoint must NOT set runner_exit_status — that's the reconciler's job.
        assert run.runner_exit_status == ""
        mock_db.commit.assert_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_accepts_partial_body(self, *_mocks):
        """Runner may not be able to read every cgroup file (dev env without
        cgroup v2). Missing fields must NOT clobber existing columns."""
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        run.peak_memory_bytes = None
        run.peak_cpu_usec = 99  # pre-existing — must be preserved
        run.runner_exit_code = None
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/terrapod/v1/runs/{run.id}/resource-profile",
                json={"peak_memory_bytes": 42},
                headers=_AUTH,
            )

        assert resp.status_code == 204
        assert run.peak_memory_bytes == 42
        assert run.peak_cpu_usec == 99
        assert run.runner_exit_code is None

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_rejects_negative_value(self, *_mocks):
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/terrapod/v1/runs/{run.id}/resource-profile",
                json={"peak_memory_bytes": -1},
                headers=_AUTH,
            )

        assert resp.status_code == 400
        assert "peak_memory_bytes" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_rejects_bool_for_int_field(self, *_mocks):
        """Python booleans are `int` subclasses — naive isinstance accepts
        True/False as 1/0. Reject them so a client bug can't corrupt
        the column."""
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        app = _make_app(_runner_user(run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/terrapod/v1/runs/{run.id}/resource-profile",
                json={"exit_code": True},
                headers=_AUTH,
            )

        assert resp.status_code == 400

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_403_for_wrong_run_scope(self, *_mocks):
        run_id = uuid.uuid4()
        wrong_run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id)
        mock_db = AsyncMock()
        mock_db.get.return_value = run

        app = _make_app(_runner_user(wrong_run_id), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/terrapod/v1/runs/{run.id}/resource-profile",
                json={"exit_code": 0},
                headers=_AUTH,
            )

        assert resp.status_code == 403
