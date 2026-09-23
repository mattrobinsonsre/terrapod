"""The AI policy gate (#1766).

The properties worth pinning here are the ones whose failure is silent:

  * a mandatory gate cannot be switched off per workspace (a governance hole
    if it could);
  * a missing or unusable verdict is `errored`, never an implicit allow;
  * an engine whose plan artifact is truncated is not ruled on at all;
  * the gate holds when it has no verdict yet, rather than failing the run for
    evidence nobody has produced.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from terrapod.config import settings
from terrapod.services import ai_policy_service as svc


@pytest.fixture(autouse=True)
def _reset_policy_config():
    """Every test configures the gate explicitly; restore afterwards."""
    cfg = settings.ai_summary.policy
    before = (cfg.enabled, cfg.enforcement_level, cfg.risk_threshold, cfg.deny_criteria)
    yield
    (cfg.enabled, cfg.enforcement_level, cfg.risk_threshold, cfg.deny_criteria) = before


def _configure(
    *,
    enabled=True,
    enforcement_level="advisory",
    risk_threshold="off",
    deny_criteria="block any 0.0.0.0/0 ingress",
):
    cfg = settings.ai_summary.policy
    cfg.enabled = enabled
    cfg.enforcement_level = enforcement_level
    cfg.risk_threshold = risk_threshold
    cfg.deny_criteria = deny_criteria


def _ws(**kw):
    base = {"id": uuid.uuid4(), "engine": "terraform", "ai_policy_mode": "default"}
    base.update(kw)
    return SimpleNamespace(**base)


def _run(plan_only=False, engine="terraform", workspace_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        plan_only=plan_only,
        engine=engine,
        workspace_id=workspace_id or uuid.uuid4(),
    )


# ── enforcement resolution ────────────────────────────────────────────────


def test_gate_is_off_when_the_deployment_has_not_enabled_it():
    _configure(enabled=False)
    assert svc.effective_enforcement(_ws(ai_policy_mode="enabled")) == "off"


def test_a_workspace_can_opt_out_of_an_advisory_verdict():
    _configure(enforcement_level="advisory")
    assert svc.effective_enforcement(_ws(ai_policy_mode="disabled")) == "off"
    assert svc.effective_enforcement(_ws(ai_policy_mode="default")) == "advisory"


def test_a_workspace_CANNOT_opt_out_of_a_mandatory_gate():
    """The security property. A fleet-wide blocking control that any workspace
    admin could switch off is not a control -- the same hole as a plan-only run
    applying past a mandatory policy set."""
    _configure(enforcement_level="mandatory")
    assert svc.effective_enforcement(_ws(ai_policy_mode="disabled")) == "mandatory"


# ── which runs are ruled on ───────────────────────────────────────────────


def test_speculative_runs_are_never_gated():
    _configure(enforcement_level="mandatory")
    assert svc.gate_applies_to(_run(plan_only=True), _ws()) is False


def test_pulumi_is_not_ruled_on_because_its_plan_artifact_is_truncated():
    """A preview uploads a digest capped at MAX_STEPS, so a verdict could allow
    a plan whose offending resource fell off the end."""
    _configure(enforcement_level="mandatory")
    assert svc.gate_applies_to(_run(engine="pulumi"), _ws(engine="pulumi")) is False


def test_an_unknown_engine_is_still_gated_so_the_gate_fails_closed():
    _configure(enforcement_level="mandatory")
    assert svc.gate_applies_to(_run(engine="something-new"), _ws()) is True


def test_enabling_the_switch_with_nothing_to_rule_on_gates_nothing():
    """Turning the gate on must not start blocking before an operator has said
    what to block."""
    _configure(enforcement_level="mandatory", risk_threshold="off", deny_criteria="")
    assert svc.is_configured() is False
    assert svc.gate_applies_to(_run(), _ws()) is False


def test_a_threshold_alone_is_enough_to_configure_the_gate():
    _configure(risk_threshold="high", deny_criteria="")
    assert svc.is_configured() is True


# ── the verdict → outcome decision ────────────────────────────────────────


def test_an_allow_below_the_threshold_passes():
    _configure(risk_threshold="off")
    assert svc.decide_outcome({"decision": "allow", "reasons": []}, "high") == ("passed", None)


def test_a_deny_fails():
    _configure()
    outcome, err = svc.decide_outcome({"decision": "deny", "reasons": [{"criterion": "x"}]}, "low")
    assert outcome == "failed"
    assert err is None


def test_the_risk_threshold_fails_independently_of_the_model_decision():
    """The threshold still protects a deployment whose criteria did not
    anticipate this change."""
    _configure(risk_threshold="high")
    assert svc.decide_outcome({"decision": "allow", "reasons": []}, "critical")[0] == "failed"
    assert svc.decide_outcome({"decision": "allow", "reasons": []}, "high")[0] == "failed"
    assert svc.decide_outcome({"decision": "allow", "reasons": []}, "medium")[0] == "passed"


def test_a_missing_verdict_is_errored_not_an_implicit_allow():
    """The 'you cannot gate on fuzzy text' failure. Silence is not consent."""
    _configure()
    outcome, err = svc.decide_outcome(None, "low")
    assert outcome == "errored"
    assert err and "no policy verdict" in err


def test_an_unusable_decision_is_errored():
    _configure()
    outcome, err = svc.decide_outcome({"decision": "probably fine", "reasons": []}, "low")
    assert outcome == "errored"
    assert err and "unusable" in err


def test_threshold_off_never_fires_on_any_risk_level():
    _configure(risk_threshold="off")
    for level in ("low", "medium", "high", "critical"):
        assert svc.decide_outcome({"decision": "allow", "reasons": []}, level)[0] == "passed"
