"""
Integration tests: Run creation and state transitions.

Tests the full run lifecycle against real Postgres — creation, listing,
show, cancel, confirm/discard guards.
"""

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
RUNS_ENDPOINT = "/api/v2/runs"


def _ws_body(name: str, **overrides) -> dict:
    attrs = {"name": name}
    attrs.update(overrides)
    return {"data": {"type": "workspaces", "attributes": attrs}}


def _run_body(ws_id: str, **attrs) -> dict:
    return {
        "data": {
            "type": "runs",
            "attributes": attrs,
            "relationships": {
                "workspace": {"data": {"id": ws_id, "type": "workspaces"}},
            },
        }
    }


async def _create_workspace(client, name: str) -> str:
    resp = await client.post(WS_ENDPOINT, json=_ws_body(name), headers=AUTH)
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


async def _create_run(client, ws_id: str, **attrs) -> dict:
    resp = await client.post(RUNS_ENDPOINT, json=_run_body(ws_id, **attrs), headers=AUTH)
    assert resp.status_code == 201
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# Run Creation
# ---------------------------------------------------------------------------


class TestRunCreation:
    async def test_create_run_returns_201(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "run-ws")

        run = await _create_run(client, ws_id)
        assert run["type"] == "runs"
        assert run["id"].startswith("run-")
        assert run["attributes"]["status"] in ("pending", "queued")

    async def test_create_plan_only_run(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "plan-ws")

        run = await _create_run(client, ws_id, **{"plan-only": True})
        assert run["attributes"]["plan-only"] is True

    async def test_create_run_with_target_addrs(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "target-ws")

        run = await _create_run(
            client,
            ws_id,
            **{"target-addrs": ["aws_instance.web", "aws_s3_bucket.data"]},
        )
        assert run["attributes"]["target-addrs"] == [
            "aws_instance.web",
            "aws_s3_bucket.data",
        ]

    async def test_list_workspace_runs(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "list-runs")

        for i in range(3):
            await _create_run(client, ws_id, message=f"Run {i}")

        resp = await client.get(f"/api/v2/workspaces/{ws_id}/runs", headers=AUTH)
        assert resp.status_code == 200
        runs = resp.json()["data"]
        assert len(runs) == 3
        # Newest first
        assert runs[0]["attributes"]["message"] == "Run 2"

    async def test_show_run_by_id(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "show-run")

        run = await _create_run(client, ws_id, message="hello")
        run_id = run["id"]

        resp = await client.get(f"/api/v2/runs/{run_id}", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["message"] == "hello"

    async def test_run_json_api_response_shape(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "shape-ws")

        run = await _create_run(client, ws_id)

        # Verify all expected TFE V2 keys are present
        attrs = run["attributes"]
        for key in [
            "status",
            "message",
            "is-destroy",
            "plan-only",
            "source",
            "created-at",
            "status-timestamps",
        ]:
            assert key in attrs, f"Missing attribute: {key}"

        assert "actions" in attrs
        assert "permissions" in attrs
        assert "relationships" in run


# ---------------------------------------------------------------------------
# Run Actions
# ---------------------------------------------------------------------------


class TestRunActions:
    async def test_cancel_queued_run(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "cancel-ws")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/cancel", headers=AUTH)
        assert resp.status_code == 200

        show = await client.get(f"/api/v2/runs/{run_id}", headers=AUTH)
        assert show.json()["data"]["attributes"]["status"] == "canceled"

    async def test_cancel_already_canceled_returns_conflict(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "dbl-cancel")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        await client.post(f"/api/v2/runs/{run_id}/actions/cancel", headers=AUTH)
        resp = await client.post(f"/api/v2/runs/{run_id}/actions/cancel", headers=AUTH)
        assert resp.status_code == 409

    async def test_confirm_non_planned_returns_conflict(self, app, client):
        """Can't confirm a run that hasn't reached 'planned' status."""
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "confirm-fail")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        # Should be 409 — run is not in 'planned' state
        assert resp.status_code == 409

    async def test_discard_non_planned_returns_conflict(self, app, client):
        """Can't discard a run that hasn't reached 'planned' status."""
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "discard-fail")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        resp = await client.post(f"/api/v2/runs/{run_id}/actions/discard", headers=AUTH)
        assert resp.status_code == 409

    async def test_cancel_unlocks_workspace(self, app, client):
        """Canceling a run should unlock the workspace."""
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "unlock-ws")

        run = await _create_run(client, ws_id)
        run_id = run["id"]

        # Cancel the run
        await client.post(f"/api/v2/runs/{run_id}/actions/cancel", headers=AUTH)

        ws_resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert ws_resp.json()["data"]["attributes"]["locked"] is False

    async def test_run_not_found_returns_404(self, app, client):
        set_auth(app, admin_user())

        resp = await client.get(
            "/api/v2/runs/run-00000000-0000-0000-0000-000000000000", headers=AUTH
        )
        assert resp.status_code == 404
