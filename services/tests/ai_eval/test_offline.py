"""Offline (no model creds) validation of the AI-eval harness (#602).

Runs in normal CI. Covers corpus integrity (generated + curated cases are
well-formed and their ground-truth references real plan addresses) and the
deterministic rubric logic (with synthetic model outputs). The live model
sweep is a separate, manually-dispatched job.
"""

from __future__ import annotations

from ai_eval import rubric
from ai_eval.cases import Case, MustFlag, RiskBand, Truth, load_corpus, risk_rank
from ai_eval.generator import build_generated_cases


def _plan_addresses(case: Case) -> set[str]:
    rcs = (case.plan_json or {}).get("resource_changes", []) or []
    drift = (case.plan_json or {}).get("resource_drift", []) or []
    return {rc["address"] for rc in (rcs + drift) if "address" in rc}


# --- corpus integrity --------------------------------------------------------


def test_generated_corpus_is_substantial_and_unique():
    cases = build_generated_cases()
    assert len(cases) >= 25, f"expected a substantial generated corpus, got {len(cases)}"
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "generated case ids must be unique"


def test_every_surface_represented():
    surfaces = {c.surface for c in build_generated_cases()}
    assert {"plan", "drift", "apply_failure"} <= surfaces


def test_generated_cases_well_formed():
    for case in build_generated_cases():
        if case.surface in ("plan", "drift"):
            assert isinstance(case.plan_json, dict), case.id
            assert "resource_changes" in case.plan_json, case.id
        elif case.surface == "apply_failure":
            assert case.apply_log, case.id


def test_must_flag_addresses_exist_in_plan():
    """A must-flag address that isn't even in the plan is a corpus bug — the
    case could never pass."""
    for case in build_generated_cases():
        if case.surface == "apply_failure":
            continue
        addrs = _plan_addresses(case)
        for mf in case.truth.must_flag:
            assert mf.address in addrs, f"{case.id}: must_flag {mf.address} not in plan"
        for addr in case.truth.churn_addresses:
            assert addr in addrs, f"{case.id}: churn {addr} not in plan"


def test_risk_band_values_valid():
    for case in build_generated_cases():
        for v in (case.truth.risk.exact, case.truth.risk.min, case.truth.risk.max):
            if v is not None:
                assert risk_rank(v) >= 0, f"{case.id}: bad risk level {v!r}"


def test_curated_corpus_loads_if_present():
    # Tolerates an empty curated dir; just asserts no duplicate ids / parse errs.
    cases = load_corpus()
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))


# --- rubric logic ------------------------------------------------------------


def _case(**truth_kw) -> Case:
    return Case(
        id="t",
        surface="plan",
        title="t",
        plan_json={
            "resource_changes": [
                {
                    "address": "aws_db_instance.main",
                    "type": "aws_db_instance",
                    "name": "main",
                    "change": {"actions": ["delete"]},
                }
            ]
        },
        truth=Truth(**truth_kw),
    )


def test_perfect_output_hard_passes():
    case = _case(
        risk=RiskBand(min="high"),
        must_flag=(MustFlag("aws_db_instance.main", "high"),),
        key_facts=("aws_db_instance.main", "destroy"),
    )
    out = {
        "risk_level": "critical",
        "risk_factors": [
            {
                "severity": "critical",
                "title": "DB destroy",
                "detail": "x",
                "resource_address": "aws_db_instance.main",
            }
        ],
        "description": "This will destroy aws_db_instance.main.",
    }
    res = rubric.score(case, out)
    assert res.hard_pass
    assert res.overall_score == 1.0


def test_wrong_risk_band_fails():
    case = _case(risk=RiskBand(min="high"))
    res = rubric.score(case, {"risk_level": "low", "risk_factors": [], "description": ""})
    assert not res.by_name["risk_band"].passed
    assert not res.hard_pass


def test_missing_must_flag_fails():
    case = _case(must_flag=(MustFlag("aws_db_instance.main", "high"),))
    out = {"risk_level": "high", "risk_factors": [], "description": ""}
    res = rubric.score(case, out)
    assert not res.by_name["must_flag"].passed


def test_severity_too_low_fails_must_flag():
    case = _case(must_flag=(MustFlag("aws_db_instance.main", "high"),))
    out = {
        "risk_level": "high",
        "risk_factors": [
            {"severity": "low", "title": "x", "resource_address": "aws_db_instance.main"}
        ],
        "description": "",
    }
    res = rubric.score(case, out)
    assert not res.by_name["must_flag"].passed


def test_churn_flagged_as_risk_fails_no_false_risk():
    case = _case(churn_addresses=("aws_db_instance.main",))
    out = {
        "risk_level": "low",
        "risk_factors": [
            {"severity": "high", "title": "DB churn", "resource_address": "aws_db_instance.main"}
        ],
        "description": "",
    }
    res = rubric.score(case, out)
    assert not res.by_name["no_false_risk"].passed
    assert not res.hard_pass


def test_forbidden_claim_fails():
    case = _case(forbidden_claims=("no changes",))
    out = {"risk_level": "high", "risk_factors": [], "description": "There are no changes here."}
    res = rubric.score(case, out)
    assert not res.by_name["no_forbidden"].passed
