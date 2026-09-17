"""Governance on a Pulumi workspace must not hold every apply (#1567).

The post-plan OPA and security-scan gates fail closed: a mandatory policy set or
an enforced scan with no result from the runner records a synthetic `errored`
result and holds the run. The Pulumi runner evaluates neither -- there is no plan
JSON for OPA or Checkov/Trivy to read -- so turning governance on for a Pulumi
workspace held every one of its applies.

Until OPA over preview JSON lands (#1560) and a scan input exists (#1569), the
gates do not apply to Pulumi runs, the API refuses to enable a scan that can
never run, and the run endpoints say why nothing was evaluated. Terraform keeps
failing closed, which is the half worth proving just as hard.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod import engines
from terrapod.api.routers import tfe_v2
from terrapod.services import policy_set_service, security_scan_service


def _ws(engine="pulumi", **kw):
    base = {
        "id": uuid.uuid4(),
        "name": "stack",
        "labels": {},
        "engine": engine,
        "security_scan_enforcement": "enforced",
        "security_scan_engine": "checkov",
        "security_scan_severity_threshold": "high",
        "security_scan_skip_rules": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _run(ws):
    return SimpleNamespace(id=uuid.uuid4(), plan_only=False, workspace_id=ws.id)


class TestWhichEnginesAreEvaluated:
    def test_terraform_is_and_pulumi_is_not(self):
        assert engines.evaluates_policy_sets("terraform")
        assert engines.evaluates_security_scans("terraform")
        assert not engines.evaluates_policy_sets("pulumi")
        assert not engines.evaluates_security_scans("pulumi")

    def test_a_row_written_before_the_engine_column_is_terraform(self):
        assert engines.evaluates_policy_sets(None)
        assert engines.evaluates_security_scans("")

    def test_an_unknown_engine_keeps_the_gates_failing_closed(self):
        assert engines.evaluates_policy_sets("chef")
        assert engines.evaluates_security_scans("chef")

    def test_the_answer_does_not_depend_on_the_engine_being_switched_on(self):
        # A Pulumi workspace still reaches the gate after engines.pulumi is off.
        with patch("terrapod.engines.engine_enabled", return_value=False):
            assert not engines.evaluates_policy_sets("pulumi")


class TestThePolicyGate:
    async def test_a_pulumi_run_passes_without_a_synthetic_failure(self):
        ws = _ws()
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        mandatory = MagicMock(enforcement_level="mandatory", id=uuid.uuid4())
        with patch.object(
            policy_set_service, "applicable_policy_sets", AsyncMock(return_value=[mandatory])
        ) as applicable:
            gate = await policy_set_service.evaluate_post_plan(db, _run(ws))
        assert gate == policy_set_service.GATE_PASSED
        applicable.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_a_terraform_run_still_fails_closed(self):
        ws = _ws(engine="terraform")
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        mandatory = MagicMock(enforcement_level="mandatory", id=uuid.uuid4())
        mandatory.name = "baseline"
        recorded = MagicMock(all=MagicMock(return_value=[]))
        with (
            patch.object(
                policy_set_service, "applicable_policy_sets", AsyncMock(return_value=[mandatory])
            ),
            patch.object(policy_set_service, "_insert_evaluations", AsyncMock()) as insert,
            patch.object(policy_set_service, "run_is_policy_blocked", AsyncMock(return_value=True)),
        ):
            db.execute = AsyncMock(return_value=recorded)
            gate = await policy_set_service.evaluate_post_plan(db, _run(ws))
        assert gate == policy_set_service.GATE_BLOCKED
        insert.assert_awaited_once()

    def test_the_reason_names_the_engine_and_is_none_for_terraform(self):
        assert "pulumi" in policy_set_service.policy_sets_not_evaluated_reason(_ws())
        assert policy_set_service.policy_sets_not_evaluated_reason(_ws("terraform")) is None


class TestTheScanGate:
    async def test_an_enforced_pulumi_workspace_passes_and_records_nothing(self):
        ws = _ws(security_scan_enforcement="enforced")
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        with patch.object(security_scan_service, "record_scan_result", AsyncMock()) as record:
            gate = await security_scan_service.evaluate_post_plan(db, _run(ws))
        assert gate == security_scan_service.GATE_PASSED
        record.assert_not_awaited()

    @pytest.mark.parametrize("enforcement", ["enforced", "advisory"])
    def test_the_runner_is_told_not_to_scan_a_pulumi_workspace(self, enforcement):
        cfg = security_scan_service.resolve_scan_config(_ws(security_scan_enforcement=enforcement))
        assert cfg["enabled"] is False
        assert cfg["enforcement_level"] == "off"

    def test_terraform_enforcement_is_unchanged(self):
        ws = _ws("terraform", security_scan_enforcement="enforced")
        assert security_scan_service.effective_enforcement(ws) == "enforced"
        assert security_scan_service.scan_not_available_reason(ws) is None


class TestEnablingAScanThatCanNeverRun:
    def test_a_pulumi_workspace_defaults_to_off(self):
        assert tfe_v2._scan_enforcement_for(None, "pulumi", "advisory") == "off"
        assert tfe_v2._scan_enforcement_for("off", "pulumi", "advisory") == "off"

    @pytest.mark.parametrize("value", ["advisory", "enforced"])
    def test_a_pulumi_workspace_refuses_anything_else(self, value):
        with pytest.raises(HTTPException) as e:
            tfe_v2._scan_enforcement_for(value, "pulumi", "advisory")
        assert e.value.status_code == 422
        assert "pulumi" in e.value.detail

    def test_terraform_keeps_its_default_and_accepts_enforced(self):
        assert tfe_v2._scan_enforcement_for(None, "terraform", "advisory") == "advisory"
        assert tfe_v2._scan_enforcement_for("enforced", "terraform", "advisory") == "enforced"
