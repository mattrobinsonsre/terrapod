"""Compliance reporting service for Terrapod runs and workspaces.

Aggregates policy checks (OPA/Rego), security scanning (Checkov/Trivy),
post-plan decision verdicts, and cost estimation into audit-ready compliance reports.
"""

import csv
import io
import uuid
from datetime import UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import PolicyEvaluation, Run, SecurityScanResult
from terrapod.services import policy_check_service


def _rfc3339(dt: Any) -> str:
    """Format datetime as RFC3339 UTC string with trailing Z."""
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_run_compliance_report_from_data(
    run: Run,
    checks: list[Any],
    policy_evals: list[PolicyEvaluation],
    scan: SecurityScanResult | None,
) -> dict[str, Any]:
    """Build the kebab-case compliance report dictionary for a single run."""
    has_blocking_policy = any(
        e.enforcement_level == "mandatory"
        and e.outcome in ("failed", "errored")
        and e.overridden_by is None
        for e in policy_evals
    )
    has_overridden_policy = any(
        e.enforcement_level == "mandatory"
        and e.outcome in ("failed", "errored")
        and e.overridden_by is not None
        for e in policy_evals
    )

    scan_enforced = scan is not None and scan.enforcement_level == "enforced"
    scan_blocked = scan_enforced and scan.outcome in ("failed", "errored")

    if run.status in ("planning", "post_plan_running", "post_plan_awaiting_decision"):
        verdict = "PENDING_REVIEW"
    elif has_blocking_policy or scan_blocked:
        verdict = "NON_COMPLIANT"
    elif has_overridden_policy:
        verdict = "OVERRIDDEN"
    else:
        verdict = "COMPLIANT"

    opa_checks_summary = [
        {
            "id": c.id,
            "kind": c.kind,
            "status": c.status,
            "passed": c.passed,
            "soft-failed": c.soft_failed,
            "advisory-failed": c.advisory_failed,
        }
        for c in checks
    ]

    evaluations_detail = [
        {
            "policy-set-id": str(e.policy_set_id) if e.policy_set_id else None,
            "policy-set-name": e.policy_set_name,
            "enforcement-level": e.enforcement_level,
            "outcome": e.outcome,
            "overridden-by": e.overridden_by,
            "overridden-at": _rfc3339(e.overridden_at),
        }
        for e in policy_evals
    ]

    scan_summary = None
    if scan:
        summary_counts = scan.summary or {}
        scan_summary = {
            "engine": scan.engine,
            "enforcement-level": scan.enforcement_level,
            "severity-threshold": scan.severity_threshold,
            "outcome": scan.outcome,
            "critical-count": summary_counts.get("critical", 0),
            "high-count": summary_counts.get("high", 0),
            "medium-count": summary_counts.get("medium", 0),
            "low-count": summary_counts.get("low", 0),
            "unknown-count": summary_counts.get("unknown", 0),
            "total-count": summary_counts.get("total", 0),
            "blocking-count": summary_counts.get("blocking", 0),
            "error": scan.error,
            "overridden-by": scan.overridden_by,
            "overridden-at": _rfc3339(scan.overridden_at),
        }

    return {
        "id": f"cmpl-{run.id}",
        "run-id": str(run.id),
        "workspace-id": str(run.workspace_id),
        "verdict": verdict,
        "run-status": run.status,
        "created-at": _rfc3339(getattr(run, "created_at", None)),
        "execution-backend": run.execution_backend,
        "is-destroy": run.is_destroy,
        "plan-only": run.plan_only,
        "policy-checks-summary": opa_checks_summary,
        "policy-evaluations": evaluations_detail,
        "security-scan": scan_summary,
    }


async def generate_run_compliance_report(db: AsyncSession, run: Run) -> dict[str, Any]:
    """Generate an audit-ready compliance report for a single run."""
    checks = await policy_check_service.list_checks(db, run)

    eval_stmt = select(PolicyEvaluation).where(PolicyEvaluation.run_id == run.id)
    eval_res = await db.execute(eval_stmt)
    policy_evals = list(eval_res.scalars().all())

    scan_stmt = select(SecurityScanResult).where(SecurityScanResult.run_id == run.id).limit(1)
    scan_res = await db.execute(scan_stmt)
    scan = scan_res.scalar_one_or_none()

    return _build_run_compliance_report_from_data(run, checks, policy_evals, scan)


async def generate_workspace_compliance_report(
    db: AsyncSession, workspace_id: uuid.UUID, limit: int = 50
) -> dict[str, Any]:
    """Generate an aggregate compliance report across recent runs for a workspace without N+1 queries."""
    stmt = (
        select(Run)
        .where(Run.workspace_id == workspace_id)
        .order_by(Run.created_at.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    runs = list(res.scalars().all())

    if not runs:
        return {
            "workspace-id": str(workspace_id),
            "total-runs-evaluated": 0,
            "summary": {
                "compliant": 0,
                "non-compliant": 0,
                "overridden": 0,
                "pending-review": 0,
                "compliance-rate-percent": 100.0,
            },
            "runs": [],
        }

    run_ids = [r.id for r in runs]

    # Batch query evaluations for all runs in one query
    evals_stmt = select(PolicyEvaluation).where(PolicyEvaluation.run_id.in_(run_ids))
    evals_res = await db.execute(evals_stmt)
    all_evals = list(evals_res.scalars().all())
    evals_by_run: dict[uuid.UUID, list[PolicyEvaluation]] = {rid: [] for rid in run_ids}
    for e in all_evals:
        if e.run_id in evals_by_run:
            evals_by_run[e.run_id].append(e)

    # Batch query security scans for all runs in one query
    scans_stmt = select(SecurityScanResult).where(SecurityScanResult.run_id.in_(run_ids))
    scans_res = await db.execute(scans_stmt)
    all_scans = list(scans_res.scalars().all())
    scan_by_run: dict[uuid.UUID, SecurityScanResult] = {}
    for s in all_scans:
        scan_by_run[s.run_id] = s

    run_reports = []
    compliant_count = 0
    non_compliant_count = 0
    overridden_count = 0
    pending_count = 0

    for r in runs:
        evals = evals_by_run.get(r.id, [])
        scan = scan_by_run.get(r.id)

        checks = []
        opa_check = policy_check_service._opa_check(r.id, evals)
        if opa_check.status in policy_check_service.STATUSES and (evals or opa_check.output):
            checks.append(opa_check)

        if scan:
            scan_check = policy_check_service._scan_check(r.id, scan)
            checks.append(scan_check)

        report = _build_run_compliance_report_from_data(r, checks, evals, scan)
        run_reports.append(report)

        v = report["verdict"]
        if v == "COMPLIANT":
            compliant_count += 1
        elif v == "NON_COMPLIANT":
            non_compliant_count += 1
        elif v == "OVERRIDDEN":
            overridden_count += 1
        elif v == "PENDING_REVIEW":
            pending_count += 1

    total_runs = len(runs)
    compliance_rate = (
        round((compliant_count + overridden_count) / total_runs * 100, 2)
        if total_runs > 0
        else 100.0
    )

    return {
        "workspace-id": str(workspace_id),
        "total-runs-evaluated": total_runs,
        "summary": {
            "compliant": compliant_count,
            "non-compliant": non_compliant_count,
            "overridden": overridden_count,
            "pending-review": pending_count,
            "compliance-rate-percent": compliance_rate,
        },
        "runs": run_reports,
    }


def format_workspace_compliance_csv(workspace_report: dict[str, Any]) -> str:
    """Format a workspace compliance report as CSV text using standard csv.writer."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "run_id",
            "workspace_id",
            "verdict",
            "run_status",
            "created_at",
            "execution_backend",
            "is_destroy",
            "plan_only",
        ]
    )
    for r in workspace_report.get("runs", []):
        writer.writerow(
            [
                r.get("run-id", ""),
                r.get("workspace-id", ""),
                r.get("verdict", ""),
                r.get("run-status", ""),
                r.get("created-at", "") or "",
                r.get("execution-backend", ""),
                str(r.get("is-destroy", False)).lower(),
                str(r.get("plan-only", False)).lower(),
            ]
        )
    return output.getvalue()
