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
POOLS_ENDPOINT = "/api/terrapod/v1/agent-pools"
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
        f"/api/terrapod/v1/agent-pools/{pool_id}/tokens",
        json={"data": {"attributes": {"description": "test token"}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["attributes"]["token"]


async def _join_listener(client, pool_id: str, join_token: str, name="test-listener") -> dict:
    """Join a listener to a pool via token exchange, return result dict."""
    resp = await client.post(
        f"/api/terrapod/v1/agent-pools/{pool_id}/listeners/join",
        json={"join_token": join_token, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _create_remote_workspace(
    client, pool_id: str, name: str, auto_apply: bool = False
) -> str:
    """Create an agent-execution workspace tied to a pool, return ws_id.

    Also seeds an uploaded CV — non-VCS workspaces need one before runs
    can be queued (#358), and none of the tests in this file exercise
    the no-CV 422 path.
    """
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": name,
                    "execution-mode": "agent",
                    "agent-pool-id": pool_id,
                    "auto-apply": auto_apply,
                },
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    ws_id = resp.json()["data"]["id"]
    await _seed_uploaded_cv(client, ws_id)
    return ws_id


async def _seed_uploaded_cv(client, ws_id: str) -> str:
    """Create + mark-uploaded an empty CV; returns cv_id."""
    resp = await client.post(
        f"/api/v2/workspaces/{ws_id}/configuration-versions",
        json={"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    cv_id = resp.json()["data"]["id"]
    resp = await client.put(
        f"/api/v2/configuration-versions/{cv_id}/upload",
        content=b"placeholder-tarball-for-tests",
        headers={"Content-Type": "application/x-tar"},
    )
    assert resp.status_code in (200, 204), resp.text
    return cv_id


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
    resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    phase = data["attributes"]["phase"]
    return data, phase


async def _report_job_launched(client, listener_id: str, run_id: str) -> None:
    """Report that a K8s Job was launched for a run."""
    resp = await client.post(
        f"/api/terrapod/v1/listeners/{listener_id}/runs/{run_id}/job-launched",
        json={"job_name": f"tprun-{run_id[:8]}", "job_namespace": "terrapod-runners"},
    )
    assert resp.status_code == 200, resp.text


async def _get_runner_token(client, listener_id: str, run_id: str) -> str:
    """Get a runner token for artifact uploads. Run must be claimed first."""
    resp = await client.post(
        f"/api/terrapod/v1/listeners/{listener_id}/runs/{run_id}/runner-token",
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
        f"/api/terrapod/v1/runs/{bare_id}/artifacts/{artifact_type}",
        content=data,
        headers={"Authorization": f"Bearer {runner_token}"},
    )
    return resp.status_code


async def _report_job_status(
    client, listener_id: str, run_id: str, phase: str, job_status: str
) -> None:
    """Report Job status (writes to Redis for reconciler)."""
    resp = await client.post(
        f"/api/terrapod/v1/listeners/{listener_id}/runs/{run_id}/job-status",
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

    async def test_claim_run_delivers_vars_payload(self, app, client, setup):
        """next_run returns terraform-vars carrying `hcl` (never `sensitive`) + env-vars.

        The runner consumes `hcl` to render terrapod.auto.tfvars (raw expression
        vs quoted string). Sensitivity is NOT part of the runner contract — all
        terraform vars, sensitive or not, are delivered uniformly via the per-run
        vars Secret — so `sensitive` must not leak into this payload, and the
        sensitive value IS delivered (the runner needs it; the Secret, not
        masking, is what protects it).
        """
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "vars-payload-ws")

        async def _add_var(key, value, category, *, sensitive=False, hcl=False):
            resp = await client.post(
                f"/api/v2/workspaces/{ws_id}/vars",
                json={
                    "data": {
                        "type": "vars",
                        "attributes": {
                            "key": key,
                            "value": value,
                            "category": category,
                            "sensitive": sensitive,
                            "hcl": hcl,
                        },
                    }
                },
                headers=AUTH,
            )
            assert resp.status_code == 201, resp.text

        await _add_var("ports", "[80, 443]", "terraform", hcl=True)
        await _add_var("secret", "s3cr3t", "terraform", sensitive=True)
        await _add_var("MY_ENV", "envval", "env")

        await _create_run(client, ws_id)
        result = await _claim_run(client, listener_id)
        assert result is not None
        data, _ = result

        tvars = {v["key"]: v for v in data["attributes"]["terraform-vars"]}
        assert set(tvars) == {"ports", "secret"}
        for v in tvars.values():
            assert "hcl" in v
            assert "sensitive" not in v  # dead field removed
        assert tvars["ports"]["hcl"] is True
        assert tvars["secret"]["hcl"] is False
        assert tvars["secret"]["value"] == "s3cr3t"  # sensitive value delivered

        env = {v["key"]: v for v in data["attributes"]["env-vars"]}
        assert env["MY_ENV"]["value"] == "envval"

    async def test_execution_hooks_killswitch_enforced_server_side(
        self, app, client, setup, monkeypatch
    ):
        """The API serves NO execution hooks when the kill-switch is off (#678),
        so a listener that ignores the flag still never receives a hook script.
        Enabled (default) delivers the associated hook; disabled delivers []."""
        from types import SimpleNamespace

        pool_id, listener_id = setup

        async def _mk_hook_ws(suffix):
            ws_id = await _create_remote_workspace(client, pool_id, f"hook-ks-{suffix}")
            r = await client.post(
                "/api/terrapod/v1/execution-hooks",
                json={
                    "data": {
                        "type": "execution-hooks",
                        "attributes": {
                            "name": f"ks-{suffix}",
                            "hook-point": "pre_init",
                            "script": "echo hi",
                        },
                    }
                },
                headers=AUTH,
            )
            assert r.status_code == 201, r.text
            hook_id = r.json()["data"]["id"]
            r = await client.post(
                f"/api/terrapod/v1/execution-hooks/{hook_id}/relationships/workspaces",
                json={"data": [{"id": ws_id, "type": "workspaces"}]},
                headers=AUTH,
            )
            assert r.status_code == 204, r.text
            return ws_id

        # Enabled (default): the associated hook is delivered to the runner.
        ws_on = await _mk_hook_ws("on")
        await _create_run(client, ws_on)
        data_on, _ = await _claim_run(client, listener_id)
        assert len(data_on["attributes"]["execution-hooks"]) == 1

        # Kill-switch off: the API serves no hooks even though one is associated.
        from terrapod import config as _cfg

        monkeypatch.setattr(
            _cfg, "load_runner_config", lambda *a, **k: SimpleNamespace(hooks_enabled=False)
        )
        ws_off = await _mk_hook_ws("off")
        await _create_run(client, ws_off)
        data_off, _ = await _claim_run(client, listener_id)
        assert data_off["attributes"]["execution-hooks"] == []


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


class TestSupersedeStaleRuns:
    """Auto-discard of superseded apply-capable (plan+apply) runs.

    A newer apply-capable run on a workspace makes older un-applied
    apply-capable runs stale: `planned` runs awaiting confirmation are
    discarded and `pending`/`queued` runs are canceled, so the newest run is
    the one that proceeds. In-flight execution (`confirmed`/`applying`) is
    never superseded, and plan-only runs neither supersede nor are superseded.
    These run against real Postgres/Redis through the real run state machine —
    confirming the behaviour, not just that a helper is called.
    """

    async def test_newer_run_discards_stale_planned(self, app, client, setup):
        """A planned run awaiting confirmation is discarded when a newer
        apply-capable run is queued behind it."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "supersede-planned")

        # Run A → planned, awaiting manual confirm.
        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]
        await _run_plan_lifecycle(client, listener_id, run_a_id)
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "planned"

        # Run B queued behind it supersedes A.
        run_b = await _create_run(client, ws_id, message="newer")
        run_b_id = run_b["id"]

        a_after = await _get_run(client, run_a_id)
        assert a_after["attributes"]["status"] == "discarded"
        assert "superseded" in a_after["attributes"]["message"].lower()
        assert (await _get_run(client, run_b_id))["attributes"]["status"] not in (
            "discarded",
            "canceled",
        )

    async def test_plan_only_run_does_not_supersede(self, app, client, setup):
        """A newer plan-only run must NOT discard an older planned apply run
        (plan-only runs are safe to run concurrently)."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "supersede-planonly")

        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]
        await _run_plan_lifecycle(client, listener_id, run_a_id)
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "planned"

        # A plan-only run is not an apply-capable superseder.
        await _create_run(client, ws_id, **{"plan-only": True}, message="speculative")
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "planned"

    async def test_newer_run_cancels_stale_queued(self, app, client, setup):
        """Older queued (never-planned) runs are canceled by a newer run."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "supersede-queued")

        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "queued"

        run_b = await _create_run(client, ws_id, message="newer")
        run_b_id = run_b["id"]

        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "canceled"
        assert (await _get_run(client, run_b_id))["attributes"]["status"] == "queued"

    async def test_in_flight_confirmed_run_not_superseded(self, app, client, setup):
        """A confirmed (apply-committed) run is NOT discarded by a newer run."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "supersede-confirmed")

        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]
        await _run_plan_lifecycle(client, listener_id, run_a_id)
        resp = await client.post(f"/api/v2/runs/{run_a_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 200
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "confirmed"

        await _create_run(client, ws_id, message="newer")

        # Committed apply intent must survive — never auto-discarded.
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "confirmed"

    async def test_planning_race_run_born_superseded(self, app, client, setup):
        """A run that finishes planning only to find a newer run already
        waiting is discarded on reaching `planned` (closes the race the
        queued-time supersede can't see)."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "supersede-race")

        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]

        # Claim A → it is now `planning` (NOT yet superseded — in-flight).
        result = await _claim_run(client, listener_id)
        assert result is not None and result[0]["id"] == run_a_id
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "planning"

        # Newer run B queues while A is still planning — A is not yet touched.
        run_b = await _create_run(client, ws_id, message="newer")
        run_b_id = run_b["id"]
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "planning"

        # Finish A's plan. On reaching `planned` it finds B waiting → discarded.
        runner_token = await _get_runner_token(client, listener_id, run_a_id)
        await _report_job_launched(client, listener_id, run_a_id)
        assert (
            await _upload_artifact(client, run_a_id, "plan-log", FAKE_PLAN_LOG, runner_token) == 204
        )
        assert (
            await _upload_artifact(client, run_a_id, "plan-file", FAKE_PLAN_FILE, runner_token)
            == 204
        )
        await _report_job_status(client, listener_id, run_a_id, "plan", "succeeded")
        from terrapod.services.run_reconciler import reconcile_runs

        await reconcile_runs()

        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "discarded"
        assert (await _get_run(client, run_b_id))["attributes"]["status"] not in (
            "discarded",
            "canceled",
        )

    async def test_supersede_is_per_workspace(self, app, client, setup):
        """A newer run on one workspace must not touch runs on another."""
        pool_id, listener_id = setup
        ws1 = await _create_remote_workspace(client, pool_id, "supersede-ws1")
        ws2 = await _create_remote_workspace(client, pool_id, "supersede-ws2")

        run1 = await _create_run(client, ws1)
        run1_id = run1["id"]
        await _run_plan_lifecycle(client, listener_id, run1_id)
        assert (await _get_run(client, run1_id))["attributes"]["status"] == "planned"

        # A run on ws2 must not supersede ws1's planned run.
        await _create_run(client, ws2, message="other-workspace")
        assert (await _get_run(client, run1_id))["attributes"]["status"] == "planned"


async def _lock_workspace(client, ws_id: str) -> None:
    resp = await client.post(f"/api/v2/workspaces/{ws_id}/actions/lock", headers=AUTH)
    assert resp.status_code == 200, resp.text


async def _unlock_workspace(client, ws_id: str) -> None:
    resp = await client.post(f"/api/v2/workspaces/{ws_id}/actions/unlock", headers=AUTH)
    assert resp.status_code == 200, resp.text


class TestRunSerializationAndLocking:
    """Per-workspace serialization of apply-capable runs + manual-lock gating.

    Only one plan+apply run executes per workspace at a time; a newer one waits
    until the in-flight one is terminal. Plan-only runs run concurrently and
    ignore the lock. A manual workspace lock blocks apply-capable runs from
    starting and blocks confirm, but not plan-only runs. Driven through the
    real dispatcher + Postgres row-locking — this is the concurrency guarantee
    that mocked tests cannot prove.
    """

    async def test_apply_capable_runs_serialize_per_workspace(self, app, client, setup):
        """A second apply-capable run cannot start while the first is applying;
        it starts only once the first reaches a terminal state."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "serialize-ws", auto_apply=True)

        # Run A → confirmed (auto-apply), then claim its apply → `applying`.
        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]
        runner_token = await _run_plan_lifecycle(client, listener_id, run_a_id)
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "confirmed"
        result = await _claim_run(client, listener_id)
        assert result is not None and result[1] == "apply" and result[0]["id"] == run_a_id
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "applying"

        # Newer apply-capable run B queues — A is in-flight, so not superseded.
        run_b = await _create_run(client, ws_id, message="newer")
        run_b_id = run_b["id"]
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "applying"

        # Serialization: nothing claimable while A applies (B's plan is gated).
        assert await _claim_run(client, listener_id) is None

        # Finish A's apply.
        await _report_job_launched(client, listener_id, run_a_id)
        assert (
            await _upload_artifact(client, run_a_id, "apply-log", FAKE_APPLY_LOG, runner_token)
            == 204
        )
        assert await _upload_artifact(client, run_a_id, "state", FAKE_STATE, runner_token) == 204
        await _report_job_status(client, listener_id, run_a_id, "apply", "succeeded")
        from terrapod.services.run_reconciler import reconcile_runs

        await reconcile_runs()
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "applied"

        # Now B can start.
        result = await _claim_run(client, listener_id)
        assert result is not None and result[0]["id"] == run_b_id and result[1] == "plan"

    async def test_plan_only_run_starts_alongside_in_flight_apply(self, app, client, setup):
        """A plan-only run is claimable even while an apply-capable run is
        in flight on the same workspace (plan-only is concurrency-safe)."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(
            client, pool_id, "serialize-planonly", auto_apply=True
        )

        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]
        await _run_plan_lifecycle(client, listener_id, run_a_id)
        result = await _claim_run(client, listener_id)  # claim apply → applying
        assert result is not None and result[1] == "apply"
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "applying"

        # Plan-only run C is NOT gated by A's in-flight apply.
        run_c = await _create_run(client, ws_id, **{"plan-only": True}, message="speculative")
        run_c_id = run_c["id"]
        result = await _claim_run(client, listener_id)
        assert result is not None and result[0]["id"] == run_c_id and result[1] == "plan"

    async def test_manual_lock_blocks_apply_capable_claim(self, app, client, setup):
        """A manually locked workspace will not start an apply-capable run;
        unlocking lets it proceed."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "lock-blocks-ws")

        await _lock_workspace(client, ws_id)
        run_a = await _create_run(client, ws_id)
        run_a_id = run_a["id"]
        assert (await _get_run(client, run_a_id))["attributes"]["status"] == "queued"

        # Locked → not claimable.
        assert await _claim_run(client, listener_id) is None

        # Unlock → now claimable.
        await _unlock_workspace(client, ws_id)
        result = await _claim_run(client, listener_id)
        assert result is not None and result[0]["id"] == run_a_id

    async def test_manual_lock_allows_plan_only_claim(self, app, client, setup):
        """A plan-only run runs even on a locked workspace."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "lock-planonly-ws")

        await _lock_workspace(client, ws_id)
        run = await _create_run(client, ws_id, **{"plan-only": True})
        run_id = run["id"]
        result = await _claim_run(client, listener_id)
        assert result is not None and result[0]["id"] == run_id and result[1] == "plan"

    async def test_manual_lock_blocks_confirm(self, app, client, setup):
        """Confirming (applying) a planned run on a locked workspace is 409."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "lock-confirm-ws")

        run = await _create_run(client, ws_id)
        run_id = run["id"]
        await _run_plan_lifecycle(client, listener_id, run_id)
        assert (await _get_run(client, run_id))["attributes"]["status"] == "planned"

        await _lock_workspace(client, ws_id)
        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 409, resp.text

        # Unlock → confirm now works.
        await _unlock_workspace(client, ws_id)
        resp = await client.post(f"/api/v2/runs/{run_id}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert (await _get_run(client, run_id))["attributes"]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# Conditional auto-apply (#1274) — the orchestrator, not the predicate
# ---------------------------------------------------------------------------
#
# `tests/services/test_conditional_auto_apply.py` covers the pure decision
# functions exhaustively and stops there. What was untested is everything
# that runs *around* them: the guards `_auto_apply_if_permitted` shares with
# the `always` path, the early returns, the declined path that records a
# reason, and `complete_plan`'s narrowing from `if run.auto_apply` to
# `resolve_auto_apply_mode(run) == "always"` — a regression to the old
# predicate would auto-apply every conditional run on plan-result, before the
# counts that decide it even exist (#1297).


def _plan_json(*, add=0, change=0, destroy=0, replace=0) -> bytes:
    """A plan JSON whose resource_changes produce the requested counts.

    A replace is the {create, delete} action pair — the shape a "no destroys"
    check misses, because it is counted as a replacement rather than a
    destruction.
    """
    changes = []
    for i in range(add):
        changes.append({"address": f"null_resource.add{i}", "change": {"actions": ["create"]}})
    for i in range(change):
        changes.append({"address": f"null_resource.upd{i}", "change": {"actions": ["update"]}})
    for i in range(destroy):
        changes.append({"address": f"null_resource.del{i}", "change": {"actions": ["delete"]}})
    for i in range(replace):
        changes.append(
            {"address": f"null_resource.rep{i}", "change": {"actions": ["create", "delete"]}}
        )
    return json.dumps({"format_version": "1.2", "resource_changes": changes}).encode()


async def _create_conditional_workspace(client, pool_id: str, name: str, mode: str) -> str:
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {
                    "name": name,
                    "execution-mode": "agent",
                    "agent-pool-id": pool_id,
                    "auto-apply-mode": mode,
                },
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    ws_id = resp.json()["data"]["id"]
    await _seed_uploaded_cv(client, ws_id)
    return ws_id


async def _plan_then_upload_json(client, listener_id: str, ws_id: str, plan_json: bytes) -> dict:
    """Drive a run through plan, then upload the plan JSON — the real order.

    The runner POSTs plan-result (which drives `complete_plan`) BEFORE it
    uploads the JSON, so the conditional decision genuinely happens after the
    run has already reached `planned`. Reproducing that order is the point:
    a `complete_plan` that auto-applied on the boolean alone would have
    applied by the time the JSON arrives.
    """
    run = await _create_run(client, ws_id)
    run_id = run["id"]
    runner_token = await _run_plan_lifecycle(client, listener_id, run_id)

    assert (
        await _upload_artifact(client, run_id, "plan-json-output", plan_json, runner_token)
    ) == 204
    return await _get_run(client, run_id)


class TestConditionalAutoApplyOrchestration:
    async def test_create_mode_applies_a_pure_addition(self, app, client, setup):
        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-create", "create")

        run = await _plan_then_upload_json(client, listener_id, ws_id, _plan_json(add=3))

        assert run["attributes"]["status"] == "confirmed"
        assert run["attributes"]["auto-apply-declined-reason"] in (None, "")

    async def test_create_mode_holds_an_update_and_says_why(self, app, client, setup):
        """The declined path is the one a human reads. A hold with no reason
        is indistinguishable from a run nobody got to."""
        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-create-upd", "create")

        run = await _plan_then_upload_json(client, listener_id, ws_id, _plan_json(add=1, change=2))

        assert run["attributes"]["status"] == "planned"
        assert "2 updates" in (run["attributes"]["auto-apply-declined-reason"] or "")

    async def test_create_update_mode_applies_additions_and_updates(self, app, client, setup):
        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-cu", "create_update")

        run = await _plan_then_upload_json(client, listener_id, ws_id, _plan_json(add=2, change=2))

        assert run["attributes"]["status"] == "confirmed"

    async def test_a_replacement_is_held_in_every_conditional_mode(self, app, client, setup):
        """The case a naive "no destroys" check sails straight past."""
        pool_id, listener_id = setup
        for mode in ("create", "create_update"):
            ws_id = await _create_conditional_workspace(client, pool_id, f"cond-rep-{mode}", mode)
            run = await _plan_then_upload_json(
                client, listener_id, ws_id, _plan_json(add=1, replace=1)
            )
            assert run["attributes"]["status"] == "planned", mode
            assert "1 replace" in (run["attributes"]["auto-apply-declined-reason"] or ""), mode

    async def test_complete_plan_does_not_apply_before_the_counts_exist(self, app, client, setup):
        """The narrowing in `complete_plan`.

        A conditional run reaches `planned` on plan-result and must STAY there
        until the JSON arrives. If that condition regressed to the old
        `if run.auto_apply` — which is True for every non-`never` mode — every
        conditional run would auto-apply here, before anything had looked at
        its shape.
        """
        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-noearly", "create")

        run = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run["id"])

        after_plan = await _get_run(client, run["id"])
        assert after_plan["attributes"]["status"] == "planned"

    async def test_a_plan_json_that_never_arrives_leaves_the_run_for_a_human(
        self, app, client, setup
    ):
        """The upload is best-effort, so failing to arrive must mean "nobody
        auto-applies" — never "apply anyway"."""
        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-nojson", "create")

        run = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run["id"])

        from terrapod.services.run_reconciler import reconcile_runs

        await reconcile_runs()
        assert (await _get_run(client, run["id"]))["attributes"]["status"] == "planned"

    async def test_an_unparseable_plan_json_holds_rather_than_applies(self, app, client, setup):
        """Unknown shape is not a pass — it is exactly the run to look at."""
        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-badjson", "create")

        run = await _plan_then_upload_json(client, listener_id, ws_id, b"not json at all")

        assert run["attributes"]["status"] == "planned"


class TestConditionalAutoApplySharesTheGuards:
    """`_auto_apply_if_permitted` was extracted so the conditional path would
    inherit the `always` path's guards. Nothing pinned that it does — and both
    guards protect against applying something a human did not sanction."""

    async def test_a_manual_lock_holds_a_permitted_conditional_run(self, app, client, setup):
        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-locked", "create")

        run = await _create_run(client, ws_id)
        runner_token = await _run_plan_lifecycle(client, listener_id, run["id"])
        # Lock AFTER the plan: the operator's lock has to win over a decision
        # that would otherwise be an unambiguous yes.
        await _lock_workspace(client, ws_id)

        assert (
            await _upload_artifact(
                client, run["id"], "plan-json-output", _plan_json(add=1), runner_token
            )
        ) == 204

        held = await _get_run(client, run["id"])
        assert held["attributes"]["status"] == "planned"

        # ...and it is still applicable once the lock comes off, rather than
        # having been discarded.
        await _unlock_workspace(client, ws_id)
        resp = await client.post(f"{RUNS_ENDPOINT}/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["status"] == "confirmed"

    async def test_a_stale_plan_is_discarded_not_auto_applied(self, app, client, setup):
        """State moved between plan and decision. Applying a plan computed
        against state that no longer exists is the worst outcome available."""
        import uuid as _uuid

        from terrapod.db.models import StateVersion
        from terrapod.db.session import get_db_session

        pool_id, listener_id = setup
        ws_id = await _create_conditional_workspace(client, pool_id, "cond-stale", "create")
        ws_uuid = _uuid.UUID(ws_id.removeprefix("ws-"))

        # A baseline the plan can be measured stale against. Without one the
        # guard correctly declines to call anything stale, so seeding it is
        # what makes this test about the guard rather than about its absence.
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=1, lineage="x"))
            await db.commit()

        run = await _create_run(client, ws_id)
        runner_token = await _run_plan_lifecycle(client, listener_id, run["id"])

        # Somebody else writes state under it, between the plan and the
        # decision.
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=2, lineage="x"))
            await db.commit()

        assert (
            await _upload_artifact(
                client, run["id"], "plan-json-output", _plan_json(add=1), runner_token
            )
        ) == 204

        stale = await _get_run(client, run["id"])
        assert stale["attributes"]["status"] == "discarded"


class TestPlanStaleness:
    """State-drift (#647, always on) and time-based expiry (#646, per-workspace)
    auto-discard of stale apply-capable planned runs — against real Postgres
    through the real state machine, the state-version discard hook, and the
    expiry sweep. Composes with supersede (a separate reason)."""

    async def test_new_state_version_discards_stale_plan(self, app, client, setup):
        """A planned run whose plan predates a newly-landed state version is
        auto-discarded with a state-changed reason (#647)."""
        import uuid as _uuid

        from terrapod.db.models import StateVersion
        from terrapod.db.session import get_db_session
        from terrapod.services import run_service

        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "stale-state-ws")
        ws_uuid = _uuid.UUID(ws_id.removeprefix("ws-"))

        # Seed a baseline state version (serial 1) so the plan has something to
        # be measured stale against. (No auto_apply → B stays `planned`.)
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=1, lineage="x"))
            await db.commit()

        # Run B plans against serial 1, then awaits confirmation.
        run_b = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run_b["id"])
        assert (await _get_run(client, run_b["id"]))["attributes"]["status"] == "planned"

        # A new state version (serial 2) lands → the hook discards B.
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=2, lineage="x"))
            await db.flush()
            await run_service.discard_stale_plans_for_state_change(db, ws_uuid, 2)
            await db.commit()

        b_after = (await _get_run(client, run_b["id"]))["attributes"]
        assert b_after["status"] == "discarded"
        assert "state changed" in (b_after["discard-reason"] or "")

    async def test_plan_with_no_baseline_is_not_stale(self, app, client, setup):
        """A first apply (no prior state version) has no baseline → the state
        guard never fires and the plan confirms normally (#647)."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "no-baseline-ws")

        run = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run["id"])
        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert (await _get_run(client, run["id"]))["attributes"]["status"] == "confirmed"

    async def test_plan_expiry_sweep_discards_aged_plan(self, app, client, setup):
        """The periodic sweep discards a planned run older than the workspace's
        plan-expiry TTL (#646)."""
        import uuid as _uuid
        from datetime import timedelta

        from terrapod.db.models import Run, Workspace, now_utc
        from terrapod.db.session import get_db_session
        from terrapod.services import run_service

        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "expiry-ws")

        run = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run["id"])
        assert (await _get_run(client, run["id"]))["attributes"]["status"] == "planned"

        # Enable a TTL and age the plan past it, then run the sweep.
        async with get_db_session() as db:
            ws = await db.get(Workspace, _uuid.UUID(ws_id.removeprefix("ws-")))
            ws.plan_expiry_seconds = 3600
            r = await db.get(Run, _uuid.UUID(run["id"].removeprefix("run-")))
            r.plan_finished_at = now_utc() - timedelta(seconds=7200)
            await db.commit()

        await run_service.expire_stale_plans_cycle()

        after = (await _get_run(client, run["id"]))["attributes"]
        assert after["status"] == "discarded"
        assert "plan expired" in (after["discard-reason"] or "")

    async def test_confirm_time_guard_409s_and_discards(self, app, client, setup):
        """Confirm-time backstop (#647): if state moved but the event hook did not
        discard this run (e.g. it was still `planning` when the version landed),
        confirming returns 409 AND the run is left `discarded` — the discard is
        committed before the 409 raises, not rolled back with the errored request."""
        import uuid as _uuid

        from terrapod.db.models import StateVersion
        from terrapod.db.session import get_db_session

        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "confirm-stale-ws")
        ws_uuid = _uuid.UUID(ws_id.removeprefix("ws-"))

        # Baseline serial 1, then plan against it.
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=1, lineage="x"))
            await db.commit()
        run = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run["id"])

        # Advance the state serial WITHOUT the discard hook, so the run is still
        # `planned` when we try to confirm it (simulates the hook-missed race).
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=2, lineage="x"))
            await db.commit()

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 409
        assert "state changed" in resp.text
        after = (await _get_run(client, run["id"]))["attributes"]
        assert after["status"] == "discarded"
        assert "state changed" in (after["discard-reason"] or "")

    async def test_listener_status_report_respects_state_drift_guard(self, app, client, setup):
        """The listener status-report PATCH (update_run_status) must route an
        auto-apply plan completion through the guarded complete_plan path — it
        must NOT bare-transition a stale plan straight to confirmed/applying
        against state that moved since the plan was computed (#665)."""
        import uuid as _uuid

        from terrapod.db.models import StateVersion
        from terrapod.db.session import get_db_session

        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(
            client, pool_id, "listener-stale-ws", auto_apply=True
        )
        ws_uuid = _uuid.UUID(ws_id.removeprefix("ws-"))

        # Baseline serial 1 — the plan is measured stale against it.
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=1, lineage="x"))
            await db.commit()

        # Claim the run so it enters `planning` and snapshots plan_state_serial=1.
        run = await _create_run(client, ws_id)
        claimed = await _claim_run(client, listener_id)
        assert claimed is not None
        assert (await _get_run(client, run["id"]))["attributes"]["status"] == "planning"

        # State advances to serial 2 before the listener reports the plan done.
        async with get_db_session() as db:
            db.add(StateVersion(workspace_id=ws_uuid, serial=2, lineage="x"))
            await db.commit()

        # Listener reports the plan finished via the PATCH status endpoint.
        resp = await client.patch(
            f"/api/terrapod/v1/listeners/{listener_id}/runs/{run['id']}",
            json={"status": "planned", "has_changes": True},
        )
        assert resp.status_code == 200, resp.text

        after = (await _get_run(client, run["id"]))["attributes"]
        # Guarded: discarded as stale — NOT auto-applied (confirmed/applying).
        assert after["status"] == "discarded", after["status"]
        assert "state changed" in (after["discard-reason"] or "")


class TestRunTriggerRunsAreApplicable:
    """A run created by a run trigger on a VCS-connected agent workspace must
    be applicable (#1307).

    It was not. `fire_run_triggers` created the run with `source="tfe-api"` —
    the CLI-upload source — and the apply guard keyed off the run's source, so
    a triggered run was refused for not being VCS-managed while pointing at the
    destination's latest VCS-fetched CV. The downstream half of run triggers
    was non-functional on exactly the workspaces most likely to use it.

    The guard now tests the CONFIGURATION VERSION's source, which is what it
    was always trying to ask.
    """

    @staticmethod
    async def _attach_vcs_connection(client, ws_id, name):
        """Attach a VCS connection to an existing workspace, in the DB.

        Deliberately not through the API: creating a VCS-connected workspace
        (or a run on one) makes the server resolve refs against the live
        provider, which a test has no business doing. The guard reads exactly
        one thing — `ws.vcs_connection_id is not None` — so attaching it
        directly puts the workspace in precisely the state under test without
        dragging the network in.

        Called AFTER the plan, so the run gets created and planned normally and
        only the confirm meets the guard, which is where it lives.
        """
        import uuid as _uuid

        from terrapod.db.models import VCSConnection, Workspace
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            conn = VCSConnection(
                name=f"conn-{name}",
                provider="gitlab",
                server_url="https://example.invalid",
                token="not-used-by-this-test",
            )
            db.add(conn)
            await db.flush()
            ws = await db.get(Workspace, _uuid.UUID(ws_id.removeprefix("ws-")))
            ws.vcs_connection_id = conn.id
            ws.vcs_repo_url = "https://example.invalid/org/repo"
            ws.vcs_branch = "main"
            await db.commit()

    @staticmethod
    async def _set_cv_source(ws_id, source):
        """Mark the workspace's CV as VCS- or API-sourced.

        Set directly because the API has no way to declare a CV VCS-sourced —
        only the poller writes those — and the guard reads exactly this field.
        """
        import uuid as _uuid

        from sqlalchemy import select

        from terrapod.db.models import ConfigurationVersion
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            res = await db.execute(
                select(ConfigurationVersion).where(
                    ConfigurationVersion.workspace_id == _uuid.UUID(ws_id.removeprefix("ws-"))
                )
            )
            for cv in res.scalars().all():
                cv.source = source
            await db.commit()

    @staticmethod
    async def _set_run_source(run_id, source):
        """What `fire_run_triggers` stamps. Set directly rather than firing a
        real trigger, so the test is about the guard rather than about the
        upstream apply that would drive it."""
        import uuid as _uuid

        from terrapod.db.models import Run
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            r = await db.get(Run, _uuid.UUID(run_id.removeprefix("run-")))
            r.source = source
            await db.commit()

    async def test_a_triggered_run_against_vcs_code_can_be_applied(self, app, client, setup):
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "trigger-dest-vcs")
        await self._set_cv_source(ws_id, "vcs")

        run = await _create_run(client, ws_id)
        await self._set_run_source(run["id"], "run-trigger")
        await _run_plan_lifecycle(client, listener_id, run["id"])
        assert (await _get_run(client, run["id"]))["attributes"]["status"] == "planned"

        await self._attach_vcs_connection(client, ws_id, "vcs")

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["status"] == "confirmed"

    async def test_a_run_against_cli_uploaded_code_is_still_refused(self, app, client, setup):
        """The property the guard exists for. A CV that came in over the API on
        a VCS-connected workspace must still not be applicable, whatever the
        run's source says."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "trigger-dest-cli")
        await self._set_cv_source(ws_id, "tfe-api")

        run = await _create_run(client, ws_id)
        await self._set_run_source(run["id"], "run-trigger")
        await _run_plan_lifecycle(client, listener_id, run["id"])
        await self._attach_vcs_connection(client, ws_id, "cli")

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 422, resp.text
        # Names the CV's origin rather than blaming the caller for using a CLI
        # they may never have touched.
        assert "configuration version" in resp.text.lower()

    async def test_a_destroy_is_still_exempt(self, app, client, setup):
        """Destroys don't depend on uploaded code at all."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "trigger-dest-destroy")
        await self._set_cv_source(ws_id, "tfe-api")

        run = await _create_run(client, ws_id, **{"is-destroy": True})
        await _run_plan_lifecycle(client, listener_id, run["id"])
        await self._attach_vcs_connection(client, ws_id, "destroy")

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text

    async def test_a_non_vcs_workspace_is_unaffected(self, app, client, setup):
        """CLI apply on a plain agent workspace is a supported workflow and
        must keep working — the guard is scoped to VCS-connected ones."""
        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "trigger-dest-plain")

        run = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run["id"])

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 200, resp.text


class TestSpeculativeConfigVersionsAreNeverApplied:
    """A speculative configuration version can never be applied. Not by any
    caller, not by any combination of arguments (#1396).

    A speculative CV is the artifact of a plan-only run — an unmerged pull
    request, or a `tofu plan` upload. Applying one puts unreviewed code into a
    real workspace.

    The rule was enforced only in the HTTP endpoint (#661), so every internal
    caller could opt out of it simply by passing `plan_only=False`.
    `fire_run_triggers` did, because it selected the newest uploaded CV without
    excluding speculative ones — and a speculative CV created by the VCS poller
    for an open PR carries `source="vcs"`, which the apply guard waves through.
    """

    @staticmethod
    async def _mark_cv_speculative(ws_id: str):
        import uuid as _uuid

        from sqlalchemy import select

        from terrapod.db.models import ConfigurationVersion
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            result = await db.execute(
                select(ConfigurationVersion)
                .where(ConfigurationVersion.workspace_id == _uuid.UUID(ws_id.removeprefix("ws-")))
                .order_by(ConfigurationVersion.created_at.desc())
                .limit(1)
            )
            cv = result.scalar_one_or_none()
            assert cv is not None
            cv.speculative = True
            await db.commit()
            return cv.id

    async def test_the_service_forces_plan_only_whatever_the_caller_asked_for(
        self, app, client, setup
    ):
        """The chokepoint. Every internal caller goes through `create_run`, so
        the invariant belongs there rather than in one HTTP handler."""
        import uuid as _uuid

        from terrapod.db.models import Workspace
        from terrapod.db.session import get_db_session
        from terrapod.services import run_service

        pool_id, _ = setup
        ws_id = await _create_remote_workspace(client, pool_id, "spec-service")
        cv_id = await self._mark_cv_speculative(ws_id)

        async with get_db_session() as db:
            ws = await db.get(Workspace, _uuid.UUID(ws_id.removeprefix("ws-")))
            run = await run_service.create_run(
                db,
                workspace=ws,
                message="asking for an apply against speculative code",
                plan_only=False,  # explicitly asking for the thing that must not happen
                configuration_version_id=cv_id,
            )
            await db.commit()
            assert run.plan_only is True, (
                "an apply-capable run was created against a speculative CV"
            )

    async def test_confirm_refuses_even_if_such_a_run_exists(self, app, client, setup):
        """Second line of defence, for rows written before the guard above —
        which is not hypothetical, since this shipped broken."""
        import uuid as _uuid

        from terrapod.db.models import Run
        from terrapod.db.session import get_db_session

        pool_id, listener_id = setup
        ws_id = await _create_remote_workspace(client, pool_id, "spec-confirm")

        run = await _create_run(client, ws_id)
        await _run_plan_lifecycle(client, listener_id, run["id"])
        # Make the CV speculative AFTER the run was created and planned, which
        # reproduces the pre-fix row shape: plan_only False, speculative CV.
        await self._mark_cv_speculative(ws_id)
        async with get_db_session() as db:
            r = await db.get(Run, _uuid.UUID(run["id"].removeprefix("run-")))
            r.plan_only = False
            await db.commit()

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/apply", headers=AUTH)
        assert resp.status_code == 409, resp.text
        assert "speculative" in resp.text.lower()
