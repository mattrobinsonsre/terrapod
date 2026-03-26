"""
Integration tests: Run execution pipeline.

Exercises the full listener/runner protocol — pool creation, listener join,
run claiming, artifact upload, job status reporting, and reconciler-driven
state transitions — against real Postgres and Redis.

A "fake runner" embedded in the test code acts as both the admin client
and the listener/runner, calling the same endpoints the real listener uses.
"""

import json

import pytest

from tests.integration.conftest import AUTH, admin_user, set_auth, set_listener_auth

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
POOLS_ENDPOINT = "/api/v2/organizations/default/agent-pools"
RUNS_ENDPOINT = "/api/v2/runs"

FAKE_PLAN_LOG = b"Terraform will perform the following actions:\n  + aws_instance.web\nPlan: 1 to add, 0 to change, 0 to destroy."
FAKE_PLAN_FILE = b"fake-plan-binary-data"
FAKE_APPLY_LOG = b"aws_instance.web: Creating...\naws_instance.web: Creation complete after 30s [id=i-abc123]\nApply complete! Resources: 1 added, 0 changed, 0 destroyed."
FAKE_STATE = json.dumps(
    {
        "version": 4,
        "terraform_version": "1.9.0",
        "serial": 1,
        "lineage": "e2e-test-lineage",
        "outputs": {},
        "resources": [],
    }
).encode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_pool(client, name="test-pool") -> str:
    """Create an agent pool, return pool_id."""
    resp = await client.post(
        POOLS_ENDPOINT,
        json={"data": {"type": "agent-pools", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _create_pool_token(client, pool_id: str) -> str:
    """Create a join token for a pool, return the raw token string."""
    resp = await client.post(
        f"/api/v2/agent-pools/{pool_id}/tokens",
        json={"data": {"attributes": {"description": "test token"}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["attributes"]["token"]


async def _join_listener(client, pool_id: str, join_token: str, name="test-listener") -> dict:
    """Join a listener to a pool via token exchange, return result dict."""
    resp = await client.post(
        f"/api/v2/agent-pools/{pool_id}/listeners/join",
        json={"join_token": join_token, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _create_remote_workspace(
    client, pool_id: str, name: str, auto_apply: bool = False
) -> str:
    """Create a remote-execution workspace tied to a pool, return ws_id."""
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": name,
                    "execution-mode": "remote",
                    "agent-pool-id": pool_id,
                    "auto-apply": auto_apply,
                },
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _create_run(client, ws_id: str, **attrs) -> dict:
    """Create a run, return response data dict."""
    resp = await client.post(
        RUNS_ENDPOINT,
        json={
            "data": {
                "type": "runs",
                "attributes": attrs,
                "relationships": {
                    "workspace": {"data": {"id": ws_id, "type": "workspaces"}},
                },
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _claim_run(client, listener_id: str):
    """Claim the next available run. Returns (data, phase) or None."""
    resp = await client.get(f"/api/v2/listeners/{listener_id}/runs/next")
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    phase = data["attributes"]["phase"]
    return data, phase


async def _report_job_launched(client, listener_id: str, run_id: str) -> None:
    """Report that a K8s Job was launched for a run."""
    resp = await client.post(
        f"/api/v2/listeners/{listener_id}/runs/{run_id}/job-launched",
        json={"job_name": f"tprun-{run_id[:8]}", "job_namespace": "terrapod-runners"},
    )
    assert resp.status_code == 200, resp.text


async def _get_runner_token(client, listener_id: str, run_id: str) -> str:
    """Get a runner token for artifact uploads. Run must be claimed first."""
    resp = await client.post(
        f"/api/v2/listeners/{listener_id}/runs/{run_id}/runner-token",
        json={},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _bare_run_id(run_id: str) -> str:
    """Strip 'run-' prefix to get bare UUID (artifact endpoints use bare UUIDs)."""
    return run_id.removeprefix("run-")


async def _upload_artifact(
    client, run_id: str, artifact_type: str, data: bytes, runner_token: str
) -> int:
    """Upload an artifact with runner token auth. Returns status code."""
    bare_id = _bare_run_id(run_id)
    resp = await client.put(
        f"/api/v2/runs/{bare_id}/artifacts/{artifact_type}",
        content=data,
        headers={"Authorization": f"Bearer {runner_token}"},
    )
    return resp.status_code


async def _report_job_status(
    client, listener_id: str, run_id: str, phase: str, job_status: str
) -> None:
    """Report Job status (writes to Redis for reconciler)."""
    resp = await client.post(
        f"/api/v2/listeners/{listener_id}/runs/{run_id}/job-status",
        json={"status": job_status, "phase": phase},
    )
    assert resp.status_code == 200, resp.text


async def _get_run(client, run_id: str) -> dict:
    """Get a run by ID, return data dict."""
    resp = await client.get(f"/api/v2/runs/{run_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _get_workspace(client, ws_id: str) -> dict:
    """Get a workspace by ID, return data dict."""
    resp = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _do_plan_phase(client, listener_id: str, run_id: str, runner_token: str) -> None:
    """Execute the full plan phase: claim + job-launched + artifacts + status + reconcile.

    The run must already have a runner_token (obtained after claiming in a
    prior step, or from a previous phase). This helper claims the run
    internally — the caller should NOT have claimed it yet.
    """
    # Claim the run (plan phase) — sets listener_id on the run
    result = await _claim_run(client, listener_id)
    assert result is not None, "Expected a run to claim"
    data, phase = result
    assert phase == "plan"
    assert data["id"] == run_id

    # Report job launched
    await _report_job_launched(client, listener_id, run_id)

    # Upload plan artifacts
    assert await _upload_artifact(client, run_id, "plan-log", FAKE_PLAN_LOG, runner_token) == 204
    assert await _upload_artifact(client, run_id, "plan-file", FAKE_PLAN_FILE, runner_token) == 204

    # Report plan succeeded
    await _report_job_status(client, listener_id, run_id, "plan", "succeeded")

    # Run reconciler to transition the run
    from terrapod.services.run_reconciler import reconcile_runs

    await reconcile_runs()


async def _do_apply_phase(client, listener_id: str, run_id: str, runner_token: str) -> None:
    """Execute the full apply phase: claim + job-launched + artifacts + state + status + reconcile."""
    # Claim the run (apply phase)
    result = await _claim_run(client, listener_id)
    assert result is not None, "Expected a run to claim for apply"
    data, phase = result
    assert phase == "apply"
    assert data["id"] == run_id

    # Report job launched
    await _report_job_launched(client, listener_id, run_id)

    # Upload apply artifacts
    assert await _upload_artifact(client, run_id, "apply-log", FAKE_APPLY_LOG, runner_token) == 204
    assert await _upload_artifact(client, run_id, "state", FAKE_STATE, runner_token) == 204

    # Report apply succeeded
    await _report_job_status(client, listener_id, run_id, "apply", "succeeded")

    # Run reconciler
    from terrapod.services.run_reconciler import reconcile_runs

    await reconcile_runs()


async def _run_plan_lifecycle(client, listener_id: str, run_id: str) -> str:
    """Claim a run, get runner token, execute plan phase. Returns runner_token."""
    # Claim sets listener_id on the run
    result = await _claim_run(client, listener_id)
    assert result is not None, "Expected a run to claim"
    data, phase = result
    assert phase == "plan"
    assert data["id"] == run_id

    # Now that run is claimed, get runner token
    runner_token = await _get_runner_token(client, listener_id, run_id)

    # Report job launched
    await _report_job_launched(client, listener_id, run_id)

    # Upload plan artifacts
    assert await _upload_artifact(client, run_id, "plan-log", FAKE_PLAN_LOG, runner_token) == 204
    assert await _upload_artifact(client, run_id, "plan-file", FAKE_PLAN_FILE, runner_token) == 204

    # Report plan succeeded
    await _report_job_status(client, listener_id, run_id, "plan", "succeeded")

    # Run reconciler to transition the run
    from terrapod.services.run_reconciler import reconcile_runs

    await reconcile_runs()

    return runner_token


# ---------------------------------------------------------------------------
# Fixture: shared pool + listener setup
# ---------------------------------------------------------------------------


@pytest.fixture
async def setup(app, client):
    """Create pool, join listener, set both auth overrides.

    Yields (pool_id, listener_id).
    """
    set_auth(app, admin_user())

    # Create pool + token
    pool_id = await _create_pool(client)
    raw_token = await _create_pool_token(client, pool_id)

    # Strip "apool-" prefix for UUID
    pool_uuid = pool_id.removeprefix("apool-")

    # Join listener
    join_result = await _join_listener(client, pool_id, raw_token)
    listener_id = join_result["listener_id"]

    # Override listener auth dependency
    set_listener_auth(app, listener_id, pool_uuid)

    yield pool_id, f"listener-{listener_id}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListenerJoinFlow:
    async def test_listener_join_returns_certificate(self, app, client):
        """Pool creation + token exchange returns listener_id + certificate."""
        set_auth(app, admin_user())

        pool_id = await _create_pool(client, name="join-test-pool")
        raw_token = await _create_pool_token(client, pool_id)

        result = await _join_listener(client, pool_id, raw_token, name="join-listener")

        assert "listener_id" in result
        assert "certificate" in result
        assert "private_key" in result
        assert "ca_certificate" in result
        assert result["certificate"].startswith("-----BEGIN CERTIFICATE-----")


class TestClaimRun:
    async def test_claim_queued_run(self, app, client, setup):
        """Remote workspace run can be claimed; transitions to planning."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "claim-ws")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        result = await _claim_run(client, listener_id)
        assert result is not None
        data, phase = result
        assert data["id"] == run_id
        assert phase == "plan"
        assert data["attributes"]["status"] == "planning"

    async def test_no_run_returns_204(self, app, client, setup):
        """No queued run returns None (204)."""
        _, listener_id = setup
        assert await _claim_run(client, listener_id) is None


class TestPlanOnlyLifecycle:
    async def test_plan_only_full_lifecycle(self, app, client, setup):
        """Plan-only run: claim → runner token → artifacts → reconciler → planned + unlocked."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "plan-only-ws")
        run = await _create_run(client, ws_id, **{"plan-only": True})
        run_id = run["id"]

        # Claim → get token → plan phase (all in one helper)
        await _run_plan_lifecycle(client, listener_id, run_id)

        # Verify final state
        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "planned"

        ws_data = await _get_workspace(client, ws_id)
        assert ws_data["attributes"]["locked"] is False


class TestAutoApplyLifecycle:
    async def test_auto_apply_full_lifecycle(self, app, client, setup):
        """Auto-apply: plan → reconciler auto-confirms → apply → applied + state version."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "auto-apply-ws", auto_apply=True)
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        # Plan phase (claim + token + artifacts + reconcile)
        runner_token = await _run_plan_lifecycle(client, listener_id, run_id)

        # After reconciler, run should be "confirmed" (auto-apply)
        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "confirmed"

        # Apply phase
        await _do_apply_phase(client, listener_id, run_id, runner_token)

        # Verify final state
        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "applied"

        ws_data = await _get_workspace(client, ws_id)
        assert ws_data["attributes"]["locked"] is False

        # Verify state version was created
        resp = await client.get(f"/api/v2/workspaces/{ws_id}/state-versions", headers=AUTH)
        assert resp.status_code == 200
        state_versions = resp.json()["data"]
        assert len(state_versions) >= 1
        assert state_versions[0]["attributes"]["serial"] == 1


class TestManualConfirmApply:
    async def test_manual_confirm_apply(self, app, client, setup):
        """Plan → planned → POST actions/apply → apply phase → applied."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "manual-ws")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        # Plan phase
        runner_token = await _run_plan_lifecycle(client, listener_id, run_id)

        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "planned"

        # Manually confirm
        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 200

        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "confirmed"

        # Apply phase
        await _do_apply_phase(client, listener_id, run_id, runner_token)

        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "applied"


class TestDiscardAfterPlan:
    async def test_discard_after_plan(self, app, client, setup):
        """Plan → planned → POST actions/discard → discarded + unlocked."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "discard-ws")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        # Plan phase
        await _run_plan_lifecycle(client, listener_id, run_id)

        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "planned"

        # Discard
        resp = await client.post(f"/api/v2/runs/{run_id}/actions/discard", headers=AUTH)
        assert resp.status_code == 200

        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "discarded"

        ws_data = await _get_workspace(client, ws_id)
        assert ws_data["attributes"]["locked"] is False


class TestCancelDuringPlanning:
    async def test_cancel_during_planning(self, app, client, setup):
        """Claim → planning → POST actions/cancel → canceled + unlocked."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "cancel-ws")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        # Claim the run (transitions to "planning")
        result = await _claim_run(client, listener_id)
        assert result is not None
        _, phase = result
        assert phase == "plan"

        # Cancel while planning
        resp = await client.post(f"/api/v2/runs/{run_id}/actions/cancel", headers=AUTH)
        assert resp.status_code == 200

        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "canceled"

        ws_data = await _get_workspace(client, ws_id)
        assert ws_data["attributes"]["locked"] is False


class TestErroredRun:
    async def test_errored_run(self, app, client, setup):
        """Claim → job-status failed → reconciler → errored + unlocked."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "error-ws")
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        # Claim the run
        result = await _claim_run(client, listener_id)
        assert result is not None

        # Report job launched
        await _report_job_launched(client, listener_id, run_id)

        # Report job failed
        await _report_job_status(client, listener_id, run_id, "plan", "failed")

        # Run reconciler
        from terrapod.services.run_reconciler import reconcile_runs

        await reconcile_runs()

        # Verify errored state
        run_data = await _get_run(client, run_id)
        assert run_data["attributes"]["status"] == "errored"

        ws_data = await _get_workspace(client, ws_id)
        assert ws_data["attributes"]["locked"] is False


class TestRunnerTokenScope:
    async def test_runner_token_scoped_to_run(self, app, client, setup):
        """Runner token can upload to its run but not another run."""
        pool_id, listener_id = setup

        # Create two workspaces and runs
        ws1_id = await _create_remote_workspace(client, pool_id, "scope-ws-1")
        ws2_id = await _create_remote_workspace(client, pool_id, "scope-ws-2")

        run1 = await _create_run(client, ws1_id)
        run1_id = run1["id"]

        run2 = await _create_run(client, ws2_id)
        run2_id = run2["id"]

        # Claim run1 (sets listener_id), then get its token
        await _claim_run(client, listener_id)
        runner_token = await _get_runner_token(client, listener_id, run1_id)

        # Upload to run1 — should succeed
        status_code = await _upload_artifact(
            client, run1_id, "plan-log", FAKE_PLAN_LOG, runner_token
        )
        assert status_code == 204

        # Upload to run2 with run1's token — should be rejected
        status_code = await _upload_artifact(
            client, run2_id, "plan-log", FAKE_PLAN_LOG, runner_token
        )
        assert status_code == 403


class TestStateUpload:
    async def test_state_upload_creates_version(self, app, client, setup):
        """Apply phase state upload creates a StateVersion record."""
        pool_id, listener_id = setup

        ws_id = await _create_remote_workspace(client, pool_id, "state-ws", auto_apply=True)
        run = await _create_run(client, ws_id)
        run_id = run["id"]

        # Plan phase (claim + token + artifacts + reconcile)
        runner_token = await _run_plan_lifecycle(client, listener_id, run_id)

        # Apply phase (includes state upload)
        await _do_apply_phase(client, listener_id, run_id, runner_token)

        # Verify state version exists via API
        resp = await client.get(f"/api/v2/workspaces/{ws_id}/state-versions", headers=AUTH)
        assert resp.status_code == 200
        versions = resp.json()["data"]
        assert len(versions) == 1
        sv = versions[0]
        assert sv["attributes"]["serial"] == 1
        assert sv["attributes"]["lineage"] == "e2e-test-lineage"
