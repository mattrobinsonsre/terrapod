"""Governance on a Pulumi workspace must not hold every apply (#1567).

The post-plan OPA and security-scan gates fail closed: a mandatory policy set or
an enforced scan with no result from the runner records a synthetic `errored`
result and holds the run. The Pulumi runner evaluated neither -- there was no
plan JSON for OPA or Checkov/Trivy to read -- so turning governance on for a
Pulumi workspace held every one of its applies.

That was fixed in two halves, and the difference between them is what this file
pins:

  - **Policy sets now apply.** The runner builds an OPA input document from the
    preview's engine event log and evaluates applicable sets against it before
    posting plan-result, so a Pulumi run fails closed for the same reason a
    Terraform one does: the runner did not report. Pulumi is no longer exempt.
  - **Security scans still do not.** Checkov and Trivy read Terraform plan JSON,
    and whether they have a meaningful Pulumi input at all is #1569. Until then
    the API refuses to enable a scan that can never run, and the run endpoints
    say why nothing was scanned.

Terraform failing closed throughout is the half worth proving just as hard.
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
    def test_both_engines_are_policy_evaluated(self):
        # Pulumi joined Terraform when the runner learned to build an OPA input
        # from the preview's event log (#1567).
        assert engines.evaluates_policy_sets("terraform")
        assert engines.evaluates_policy_sets("pulumi")

    def test_only_terraform_is_security_scanned(self):
        # Checkov and Trivy read Terraform plan JSON; whether they have a
        # meaningful Pulumi input at all is #1569.
        assert engines.evaluates_security_scans("terraform")
        assert not engines.evaluates_security_scans("pulumi")

    def test_a_row_written_before_the_engine_column_is_terraform(self):
        assert engines.evaluates_policy_sets(None)
        assert engines.evaluates_security_scans("")

    def test_an_unknown_engine_keeps_the_gates_failing_closed(self):
        assert engines.evaluates_policy_sets("chef")
        assert engines.evaluates_security_scans("chef")

    def test_the_answer_does_not_depend_on_the_engine_being_switched_on(self):
        # The property under test is that the gate's view of an engine does not
        # move when `engines.pulumi` is switched off -- a Pulumi workspace still
        # exists, and still reaches the post-plan gate. Only the expected value
        # changed with #1567; the independence is the point.
        with patch("terrapod.engines.engine_enabled", return_value=False):
            assert engines.evaluates_policy_sets("pulumi")
            assert not engines.evaluates_security_scans("pulumi")


class TestThePolicyGate:
    async def test_a_pulumi_run_now_fails_closed_like_terraform(self):
        """The inverse of what this asserted before #1567's second half.

        A mandatory set that the runner did not evaluate used to pass a Pulumi
        run, because nothing could evaluate it and holding the apply forever was
        the worse failure. Now the runner does evaluate, so a missing result is
        the safety net firing for its real reason -- a runner that did not
        report -- and it must block exactly as Terraform's does.
        """
        ws = _ws()
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        mandatory = MagicMock(enforcement_level="mandatory", id=uuid.uuid4())
        mandatory.name = "baseline"
        recorded = MagicMock(all=MagicMock(return_value=[]))
        with (
            patch.object(
                policy_set_service, "applicable_policy_sets", AsyncMock(return_value=[mandatory])
            ) as applicable,
            patch.object(policy_set_service, "_insert_evaluations", AsyncMock()) as insert,
            patch.object(policy_set_service, "run_is_policy_blocked", AsyncMock(return_value=True)),
        ):
            db.execute = AsyncMock(return_value=recorded)
            gate = await policy_set_service.evaluate_post_plan(db, _run(ws))
        assert gate == policy_set_service.GATE_BLOCKED
        applicable.assert_awaited_once()
        insert.assert_awaited_once()

    async def test_a_pulumi_run_with_no_applicable_set_still_passes(self):
        # Being evaluated is not the same as being gated: a workspace no set is
        # scoped to must not acquire one by changing engine.
        ws = _ws()
        db = AsyncMock()
        db.get = AsyncMock(return_value=ws)
        with patch.object(policy_set_service, "applicable_policy_sets", AsyncMock(return_value=[])):
            gate = await policy_set_service.evaluate_post_plan(db, _run(ws))
        assert gate == policy_set_service.GATE_PASSED

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

    def test_no_engine_reports_a_not_evaluated_reason_today(self):
        """Both known engines are evaluated, so the reason is None for both.

        The mechanism is deliberately kept rather than deleted: it is how the
        next engine that cannot be evaluated -- Ansible under #1407, which has
        no separable plan for OPA to read at all -- reports that instead of
        silently holding every apply. It is inert, not unused, and a reader who
        finds a function that cannot currently return non-None should find this
        test rather than wonder.
        """
        assert policy_set_service.policy_sets_not_evaluated_reason(_ws("pulumi")) is None
        assert policy_set_service.policy_sets_not_evaluated_reason(_ws("terraform")) is None

    def test_the_reason_fires_for_an_engine_that_is_not_evaluated(self):
        # Proves the mechanism still works, without waiting for Ansible.
        with patch.object(engines, "evaluates_policy_sets", return_value=False):
            reason = policy_set_service.policy_sets_not_evaluated_reason(_ws("pulumi"))
        assert reason is not None
        assert "pulumi" in reason


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
