"""Compliance reporting service for Terrapod runs and workspaces.

Aggregates policy checks (OPA/Rego), security scanning (Checkov/Trivy),
post-plan decision verdicts, and cost estimation into audit-ready compliance reports.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import PolicyEvaluation, Run, SecurityScanResult
from terrapod.services import policy_check_service


async def generate_run_compliance_report(db: AsyncSession, run: Run) -> dict[str, Any]:
    """Generate an audit-ready compliance report for a single run."""
    # List derived policy checks (OPA + Security Scan)
    checks = await policy_check_service.list_checks(db, run)

    # Fetch detailed policy evaluations for OPA policy sets
    eval_stmt = select(PolicyEvaluation).where(PolicyEvaluation.run_id == run.id)
    eval_res = await db.execute(eval_stmt)
    policy_evals = list(eval_res.scalars().all())

    # Fetch security scan result
    scan_stmt = select(SecurityScanResult).where(SecurityScanResult.run_id == run.id).limit(1)
    scan_res = await db.execute(scan_stmt)
    scan = scan_res.scalar_one_or_none()

    # Determine overall compliance verdict
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

    scan_blocked = scan is not None and scan.enforced and scan.outcome in ("failed", "errored")

    if run.status in ("planning", "post_plan_running", "post_plan_awaiting_decision"):
        verdict = "PENDING_REVIEW"
    elif has_blocking_policy or scan_blocked:
        verdict = "NON_COMPLIANT"
    elif has_overridden_policy:
        verdict = "OVERRIDDEN"
    else:
        verdict = "COMPLIANT"

    # Summarize policy checks
    opa_checks_summary = [
        {
            "id": c.id,
            "kind": c.kind,
            "status": c.status,
            "passed": c.passed,
            "soft_failed": c.soft_failed,
            "advisory_failed": c.advisory_failed,
        }
        for c in checks
    ]

    # Summarize OPA evaluations
    evaluations_detail = [
        {
            "policy_set_id": str(e.policy_set_id),
            "enforcement_level": e.enforcement_level,
            "outcome": e.outcome,
            "overridden_by": str(e.overridden_by) if e.overridden_by else None,
            "overridden_at": e.overridden_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if e.overridden_at
            else None,
        }
        for e in policy_evals
    ]

    # Security scan summary
    scan_summary = None
    if scan:
        scan_summary = {
            "scanner": scan.scanner,
            "enforced": scan.enforced,
            "outcome": scan.outcome,
            "critical_count": scan.critical_count,
            "high_count": scan.high_count,
            "medium_count": scan.medium_count,
            "low_count": scan.low_count,
        }

    created_at_str = (
        run.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if getattr(run, "created_at", None) else None
    )

    return {
        "id": f"cmpl-{run.id}",
        "run_id": str(run.id),
        "workspace_id": str(run.workspace_id),
        "verdict": verdict,
        "run_status": run.status,
        "created_at": created_at_str,
        "execution_backend": run.execution_backend,
        "is_destroy": run.is_destroy,
        "plan_only": run.plan_only,
        "policy_checks_summary": opa_checks_summary,
        "policy_evaluations": evaluations_detail,
        "security_scan": scan_summary,
    }


async def generate_workspace_compliance_report(
    db: AsyncSession, workspace_id: uuid.UUID, limit: int = 50
) -> dict[str, Any]:
    """Generate an aggregate compliance report across recent runs for a workspace."""
    stmt = select(Run).where(Run.workspace_id == workspace_id).order_by(Run.id.desc()).limit(limit)
    res = await db.execute(stmt)
    runs = list(res.scalars().all())

    run_reports = []
    compliant_count = 0
    non_compliant_count = 0
    overridden_count = 0
    pending_count = 0

    for r in runs:
        report = await generate_run_compliance_report(db, r)
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
        "workspace_id": str(workspace_id),
        "total_runs_evaluated": total_runs,
        "summary": {
            "compliant": compliant_count,
            "non_compliant": non_compliant_count,
            "overridden": overridden_count,
            "pending_review": pending_count,
            "compliance_rate_percent": compliance_rate,
        },
        "runs": run_reports,
    }


def format_workspace_compliance_csv(workspace_report: dict[str, Any]) -> str:
    """Format a workspace compliance report as CSV text."""
    lines = [
        "run_id,workspace_id,verdict,run_status,created_at,execution_backend,is_destroy,plan_only"
    ]
    for r in workspace_report.get("runs", []):
        row = [
            r.get("run_id", ""),
            r.get("workspace_id", ""),
            r.get("verdict", ""),
            r.get("run_status", ""),
            r.get("created_at", "") or "",
            r.get("execution_backend", ""),
            str(r.get("is_destroy", False)),
            str(r.get("plan_only", False)),
        ]
        lines.append(",".join(row))
    return "\n".join(lines)
