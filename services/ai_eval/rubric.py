"""Deterministic scoring of an analysis output against a Case's ground truth.

Engine-free and fully objective: given the parsed model output
(``{description, risk_level, risk_factors}``) and a :class:`Truth`, it scores
the axes that don't need a judge. The LLM-judge (``judge.py``) scores the
softer description-quality axes on top of this.

Axes (priority order — risk-calling first):
  1. ``risk_band``     — overall risk_level falls in the expected band.
  2. ``must_flag``     — every must-flag resource appears at >= min severity.
  3. ``no_false_risk`` — no must-not-flag / churn address is flagged as a risk
                         (the real-change-vs-churn-noise axis).
  4. ``key_facts``     — description contains the required substrings.
  5. ``no_forbidden``  — description omits the forbidden claims.

``hard_pass`` is True only when axes 1–3 all pass — those are the
risk-correctness guarantees the user prioritised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cases import Case, risk_rank

# Axes 1–3 are the risk-correctness core; a regression in any is a hard fail.
HARD_AXES = ("risk_band", "must_flag", "no_false_risk")

# Relative weights for the graded overall score.
AXIS_WEIGHTS = {
    "risk_band": 0.30,
    "must_flag": 0.30,
    "no_false_risk": 0.20,
    "key_facts": 0.12,
    "no_forbidden": 0.08,
}


@dataclass
class AxisResult:
    name: str
    passed: bool
    score: float  # 0..1
    detail: str = ""


@dataclass
class RubricResult:
    case_id: str
    axes: list[AxisResult] = field(default_factory=list)

    @property
    def by_name(self) -> dict[str, AxisResult]:
        return {a.name: a for a in self.axes}

    @property
    def hard_pass(self) -> bool:
        bn = self.by_name
        return all(bn[a].passed for a in HARD_AXES if a in bn)

    @property
    def overall_score(self) -> float:
        total = sum(AXIS_WEIGHTS.get(a.name, 0.0) for a in self.axes)
        if total == 0:
            return 0.0
        got = sum(AXIS_WEIGHTS.get(a.name, 0.0) * a.score for a in self.axes)
        return got / total

    @property
    def failures(self) -> list[str]:
        return [f"{a.name}: {a.detail}" for a in self.axes if not a.passed]


# --- helpers -----------------------------------------------------------------


def _factors(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    rf = parsed.get("risk_factors") or []
    return [f for f in rf if isinstance(f, dict)]


def _factor_matches_address(factor: dict[str, Any], address: str) -> bool:
    """A factor is *about* an address when its resource_address equals it, or
    (fallback for models that leave resource_address empty) the address appears
    verbatim in the title."""
    ra = (factor.get("resource_address") or "").strip()
    if ra == address:
        return True
    title = factor.get("title") or ""
    return address in title


def _flagged_as_risk(parsed: dict[str, Any], address: str) -> bool:
    """Whether the output presents ``address`` as a risk factor at all."""
    return any(_factor_matches_address(f, address) for f in _factors(parsed))


# --- axis scorers ------------------------------------------------------------


def _score_risk_band(case: Case, parsed: dict[str, Any]) -> AxisResult:
    level = str(parsed.get("risk_level", "")).lower()
    band = case.truth.risk
    ok = band.contains(level)
    return AxisResult(
        "risk_band",
        ok,
        1.0 if ok else 0.0,
        f"got {level!r}, expected {band.describe()}",
    )


def _score_must_flag(case: Case, parsed: dict[str, Any]) -> AxisResult:
    expected = case.truth.must_flag
    if not expected:
        return AxisResult("must_flag", True, 1.0, "no must-flag resources")
    hits = 0
    misses: list[str] = []
    for mf in expected:
        matched = [f for f in _factors(parsed) if _factor_matches_address(f, mf.address)]
        ok = any(risk_rank(f.get("severity", "")) >= risk_rank(mf.min_severity) for f in matched)
        if ok:
            hits += 1
        else:
            why = "absent" if not matched else "severity too low"
            misses.append(f"{mf.address} (>= {mf.min_severity}: {why})")
    score = hits / len(expected)
    return AxisResult(
        "must_flag",
        hits == len(expected),
        score,
        "all flagged" if not misses else "missed: " + "; ".join(misses),
    )


def _score_no_false_risk(case: Case, parsed: dict[str, Any]) -> AxisResult:
    forbidden = set(case.truth.must_not_flag) | set(case.truth.churn_addresses)
    if not forbidden:
        return AxisResult("no_false_risk", True, 1.0, "no false-risk guards")
    violations = [addr for addr in sorted(forbidden) if _flagged_as_risk(parsed, addr)]
    score = 1.0 - (len(violations) / len(forbidden))
    return AxisResult(
        "no_false_risk",
        not violations,
        score,
        "clean" if not violations else "falsely flagged: " + ", ".join(violations),
    )


def _score_key_facts(case: Case, parsed: dict[str, Any]) -> AxisResult:
    facts = case.truth.key_facts
    if not facts:
        return AxisResult("key_facts", True, 1.0, "no required facts")
    desc = str(parsed.get("description", "")).lower()
    missing = [f for f in facts if f.lower() not in desc]
    score = (len(facts) - len(missing)) / len(facts)
    return AxisResult(
        "key_facts",
        not missing,
        score,
        "all present" if not missing else "missing: " + ", ".join(missing),
    )


def _score_no_forbidden(case: Case, parsed: dict[str, Any]) -> AxisResult:
    forbidden = case.truth.forbidden_claims
    if not forbidden:
        return AxisResult("no_forbidden", True, 1.0, "no forbidden claims")
    desc = str(parsed.get("description", "")).lower()
    present = [f for f in forbidden if f.lower() in desc]
    score = 1.0 - (len(present) / len(forbidden))
    return AxisResult(
        "no_forbidden",
        not present,
        score,
        "clean" if not present else "contains: " + ", ".join(present),
    )


def score(case: Case, parsed: dict[str, Any]) -> RubricResult:
    """Score one parsed analysis output against a Case's ground truth."""
    return RubricResult(
        case_id=case.id,
        axes=[
            _score_risk_band(case, parsed),
            _score_must_flag(case, parsed),
            _score_no_false_risk(case, parsed),
            _score_key_facts(case, parsed),
            _score_no_forbidden(case, parsed),
        ],
    )
