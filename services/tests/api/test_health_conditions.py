"""Tests for _compute_health_conditions and workspace JSON serialization."""

import uuid
from unittest.mock import MagicMock


def _mock_workspace(**overrides):
    ws = MagicMock()
    ws.id = overrides.get("id", uuid.uuid4())
    ws.name = overrides.get("name", "test-ws")
    ws.execution_mode = overrides.get("execution_mode", "local")
    ws.auto_apply = False
    ws.execution_backend = "tofu"
    ws.terraform_version = "1.11"
    ws.working_directory = ""
    ws.locked = False
    ws.lock_id = None
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.agent_pool_id = overrides.get("agent_pool_id", None)
    ws.agent_pool = None
    ws.vcs_connection_id = overrides.get("vcs_connection_id", None)
    ws.vcs_connection = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.var_files = []
    ws.trigger_prefixes = []
    ws.drift_detection_enabled = False
    ws.drift_detection_interval_seconds = 86400
    ws.drift_last_checked_at = None
    ws.drift_status = overrides.get("drift_status", "")
    ws.state_diverged = overrides.get("state_diverged", False)
    ws.vcs_last_polled_at = overrides.get("vcs_last_polled_at", None)
    ws.vcs_last_error = overrides.get("vcs_last_error", None)
    ws.vcs_last_error_at = overrides.get("vcs_last_error_at", None)
    ws.labels = {}
    ws.owner_email = "test@example.com"
    ws.created_at = None
    ws.updated_at = None
    return ws


class TestComputeHealthConditions:
    def test_empty_when_no_issues(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace()
        conditions = _compute_health_conditions(ws)
        assert conditions == []

    def test_state_diverged(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(state_diverged=True)
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "state_diverged"
        assert conditions[0]["severity"] == "error"

    def test_no_agent_pool_remote(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(execution_mode="remote", agent_pool_id=None)
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "no_agent_pool"
        assert conditions[0]["severity"] == "warning"

    def test_no_agent_pool_local_mode_no_condition(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(execution_mode="local", agent_pool_id=None)
        conditions = _compute_health_conditions(ws)
        assert conditions == []

    def test_vcs_error(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(vcs_last_error="Token expired")
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "vcs_error"
        assert conditions[0]["severity"] == "error"
        assert "Token expired" in conditions[0]["detail"]

    def test_drifted(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(drift_status="drifted")
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "drifted"
        assert conditions[0]["severity"] == "warning"

    def test_drift_errored(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(drift_status="errored")
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "drift_errored"
        assert conditions[0]["severity"] == "warning"

    def test_multiple_conditions_simultaneously(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(
            state_diverged=True,
            vcs_last_error="403 Forbidden",
            drift_status="drifted",
            execution_mode="remote",
            agent_pool_id=None,
        )
        conditions = _compute_health_conditions(ws)
        codes = {c["code"] for c in conditions}
        assert codes == {"state_diverged", "no_agent_pool", "vcs_error", "drifted"}

    def test_workspace_json_includes_conditions(self):
        from terrapod.api.routers.tfe_v2 import _workspace_json

        ws = _mock_workspace(state_diverged=True)
        result = _workspace_json(ws)
        attrs = result["data"]["attributes"]
        assert "health-conditions" in attrs
        assert len(attrs["health-conditions"]) == 1
        assert attrs["health-conditions"][0]["code"] == "state_diverged"

    def test_workspace_json_includes_vcs_fields(self):
        from terrapod.api.routers.tfe_v2 import _workspace_json

        ws = _mock_workspace(vcs_last_error="Something broke")
        result = _workspace_json(ws)
        attrs = result["data"]["attributes"]
        assert "vcs-last-polled-at" in attrs
        assert attrs["vcs-last-error"] == "Something broke"
        assert "vcs-last-error-at" in attrs
