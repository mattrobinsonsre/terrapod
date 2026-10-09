"""Service-tier unit tests for compliance_report_service (#1704).

Tests verdict logic, aggregate calculations, SecurityScanResult handling,
and CSV formatting against real model instances and mock database sessions.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from terrapod.db.models import PolicyEvaluation, Run, SecurityScanResult
from terrapod.services import compliance_report_service

STAMP = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _make_run(
    status: str = "applied",
    workspace_id: uuid.UUID | None = None,
    created_at: datetime = STAMP,
) -> Run:
    run = Run()
    run.id = uuid.uuid4()
    run.workspace_id = workspace_id or uuid.uuid4()
    run.status = status
    run.created_at = created_at
    run.execution_backend = "tofu"
    run.is_destroy = False
    run.plan_only = False
    return run


def _make_policy_eval(
    run_id: uuid.UUID,
    enforcement_level: str = "mandatory",
    outcome: str = "passed",
    overridden_by: str | None = None,
) -> PolicyEvaluation:
    eval_row = PolicyEvaluation()
    eval_row.id = uuid.uuid4()
    eval_row.run_id = run_id
    eval_row.policy_set_id = uuid.uuid4()
    eval_row.policy_set_name = "security-baseline"
    eval_row.enforcement_level = enforcement_level
    eval_row.outcome = outcome
    eval_row.overridden_by = overridden_by
    eval_row.overridden_at = STAMP if overridden_by else None
    eval_row.result = {}
    return eval_row


def _make_security_scan(
    run_id: uuid.UUID,
    enforcement_level: str = "enforced",
    outcome: str = "passed",
    critical: int = 0,
    high: int = 0,
) -> SecurityScanResult:
    scan = SecurityScanResult()
    scan.id = uuid.uuid4()
    scan.run_id = run_id
    scan.engine = "checkov"
    scan.enforcement_level = enforcement_level
    scan.severity_threshold = "high"
    scan.outcome = outcome
    scan.summary = {
        "critical": critical,
        "high": high,
        "medium": 0,
        "low": 0,
        "unknown": 0,
        "total": critical + high,
        "blocking": critical + high if outcome in ("failed", "errored") else 0,
    }
    scan.findings = []
    scan.error = None
    scan.overridden_by = None
    scan.overridden_at = None
    return scan


def _mock_db(
    evals: list[PolicyEvaluation] | None = None,
    scan: SecurityScanResult | None = None,
    runs: list[Run] | None = None,
) -> MagicMock:
    db = MagicMock()

    async def _execute(stmt):
        mock_result = MagicMock()
        stmt_str = str(stmt)
        if "policy_evaluations" in stmt_str:
            mock_result.scalars().all.return_value = evals or []
        elif "security_scan_results" in stmt_str:
            mock_result.scalar_one_or_none.return_value = scan
            mock_result.scalars().all.return_value = [scan] if scan else []
        elif "runs" in stmt_str:
            mock_result.scalars().all.return_value = runs or []
        else:
            mock_result.scalars().all.return_value = []
            mock_result.scalar_one_or_none.return_value = None
        return mock_result

    db.execute = AsyncMock(side_effect=_execute)
    return db


class TestRunComplianceReportVerdict:
    async def test_verdict_compliant_when_all_checks_pass(self):
        run = _make_run(status="applied")
        eval_row = _make_policy_eval(run.id, outcome="passed")
        scan = _make_security_scan(run.id, outcome="passed")
        db = _mock_db(evals=[eval_row], scan=scan)

        report = await compliance_report_service.generate_run_compliance_report(db, run)

        assert report["verdict"] == "COMPLIANT"
        assert report["run-id"] == str(run.id)
        assert report["security-scan"]["engine"] == "checkov"
        assert report["security-scan"]["enforcement-level"] == "enforced"
        assert report["policy-evaluations"][0]["policy-set-name"] == "security-baseline"

    async def test_verdict_non_compliant_when_mandatory_policy_fails(self):
        run = _make_run(status="planned")
        eval_row = _make_policy_eval(run.id, enforcement_level="mandatory", outcome="failed")
        db = _mock_db(evals=[eval_row])

        report = await compliance_report_service.generate_run_compliance_report(db, run)

        assert report["verdict"] == "NON_COMPLIANT"

    async def test_verdict_non_compliant_when_enforced_security_scan_fails(self):
        run = _make_run(status="planned")
        scan = _make_security_scan(
            run.id, enforcement_level="enforced", outcome="failed", critical=1
        )
        db = _mock_db(scan=scan)

        report = await compliance_report_service.generate_run_compliance_report(db, run)

        assert report["verdict"] == "NON_COMPLIANT"
        assert report["security-scan"]["critical-count"] == 1

    async def test_verdict_overridden_when_failed_mandatory_policy_has_override(self):
        run = _make_run(status="applied")
        eval_row = _make_policy_eval(
            run.id,
            enforcement_level="mandatory",
            outcome="failed",
            overridden_by="admin@example.com",
        )
        db = _mock_db(evals=[eval_row])

        report = await compliance_report_service.generate_run_compliance_report(db, run)

        assert report["verdict"] == "OVERRIDDEN"

    async def test_verdict_pending_review_when_run_is_in_planning_status(self):
        run = _make_run(status="planning")
        db = _mock_db()

        report = await compliance_report_service.generate_run_compliance_report(db, run)

        assert report["verdict"] == "PENDING_REVIEW"


class TestWorkspaceComplianceReport:
    async def test_workspace_sweep_and_csv_formatting(self):
        ws_id = uuid.uuid4()
        run1 = _make_run(status="applied", workspace_id=ws_id)
        run2 = _make_run(status="planned", workspace_id=ws_id)

        eval1 = _make_policy_eval(run1.id, outcome="passed")
        eval2 = _make_policy_eval(run2.id, outcome="failed")

        db = MagicMock()

        async def _execute(stmt):
            mock_res = MagicMock()
            stmt_str = str(stmt)
            if "runs" in stmt_str:
                mock_res.scalars().all.return_value = [run1, run2]
            elif "policy_evaluations" in stmt_str:
                mock_res.scalars().all.return_value = [eval1, eval2]
            elif "security_scan_results" in stmt_str:
                mock_res.scalars().all.return_value = []
            return mock_res

        db.execute = AsyncMock(side_effect=_execute)
        # Deliberately larger than the sample: the two numbers must not be
        # reported from the same source, or "50 of 500" cannot be told from
        # "50 of 50".
        db.scalar = AsyncMock(return_value=137)

        ws_report = await compliance_report_service.generate_workspace_compliance_report(
            db, ws_id, limit=50
        )

        assert ws_report["workspace-id"] == str(ws_id)
        assert ws_report["total-runs-evaluated"] == 2
        assert ws_report["total-runs-in-workspace"] == 137
        assert ws_report["summary"]["compliant"] == 1
        assert ws_report["summary"]["non-compliant"] == 1
        assert ws_report["summary"]["compliance-rate-percent"] == 50.0

        csv_text = compliance_report_service.format_workspace_compliance_csv(ws_report)
        assert "run_id,workspace_id,verdict" in csv_text
        assert "COMPLIANT" in csv_text
        assert "NON_COMPLIANT" in csv_text

    async def test_an_unevaluated_workspace_reports_no_rate_rather_than_a_perfect_one(self):
        """A workspace that has never run is unevaluated, not compliant.

        It reported `compliance-rate-percent: 100.0`, so an audit over a fleet
        would show a perfect score for every workspace nobody had used -- and
        100.0 is indistinguishable from a genuine clean sweep. `None` is the
        only answer that cannot be read as a finding either way.
        """
        ws_id = uuid.uuid4()
        db = MagicMock()

        async def _execute(stmt):
            mock_res = MagicMock()
            mock_res.scalars().all.return_value = []
            return mock_res

        db.execute = AsyncMock(side_effect=_execute)
        db.scalar = AsyncMock(return_value=0)

        report = await compliance_report_service.generate_workspace_compliance_report(db, ws_id)

        assert report["total-runs-evaluated"] == 0
        assert report["total-runs-in-workspace"] == 0
        assert report["summary"]["compliance-rate-percent"] is None
        assert report["runs"] == []
