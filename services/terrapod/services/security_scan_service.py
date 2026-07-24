"""Security-scan config resolution, result persistence and run gating (#1036).

The deterministic IaC-security-scan stage — Checkov / Trivy misconfiguration
scanning — is the structural twin of OPA policy sets (:mod:`policy_set_service`),
but the rules are *prebuilt* by the scanners instead of operator-authored Rego,
and the config is a per-workspace setting rather than a label-scoped, shareable
ruleset.

This service:
  * resolves the per-workspace scan config the runner pulls (engine, enforcement,
    severity threshold, skip rules) — :func:`resolve_scan_config`;
  * persists the runner's scan result idempotently — :func:`record_scan_result`;
  * answers the post-plan gate for ``run_service.complete_plan`` —
    :func:`evaluate_post_plan` / :func:`run_is_scan_blocked`.

Gating model (identical to policy sets by design)
-------------------------------------------------
An ``enforced`` scan that fails (a blocking finding) or errors (the scanner
crashed / an enforced scan produced no runner result) keeps the run *in
``planning``* — never ``errored`` — so the idempotent ``complete_plan`` re-drives
cleanly on the next reconciler tick and an admin override releases it without a
race. ``advisory`` scans record their findings but never block. ``off`` skips the
stage entirely. Speculative (plan-only) runs are recorded but never gated — there
is no apply to block. The **severity threshold is applied on the runner** when it
computes ``outcome``; the gate here keys purely on the recorded outcome.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, SecurityScanResult, Workspace, now_utc

logger = structlog.get_logger(__name__)

# evaluate_post_plan return values (the gate contract with complete_plan).
GATE_PASSED = "passed"
GATE_BLOCKED = "blocked"

VALID_ENFORCEMENT = frozenset({"off", "advisory", "enforced"})
VALID_ENGINE = frozenset({"checkov", "trivy", "both"})
VALID_SEVERITY = frozenset({"critical", "high", "medium", "low"})
VALID_OUTCOME = frozenset({"passed", "failed", "errored"})


# ── Config resolution (what the runner pulls) ─────────────────────────────


def scan_enabled(ws: Workspace) -> bool:
    """True when the workspace has the scan stage turned on (not ``off``)."""
    return (ws.security_scan_enforcement or "off") != "off"


def resolve_scan_config(ws: Workspace) -> dict[str, Any]:
    """The per-workspace scan config the runner fetches before scanning.

    ``enabled=False`` tells the runner to skip the stage outright.
    """
    enforcement = ws.security_scan_enforcement or "off"
    return {
        "enabled": enforcement != "off",
        "enforcement_level": enforcement,
        "engine": ws.security_scan_engine or "checkov",
        "severity_threshold": ws.security_scan_severity_threshold or "high",
        "skip_rules": list(ws.security_scan_skip_rules or []),
    }


# ── Result persistence ────────────────────────────────────────────────────


async def record_scan_result(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    engine: str,
    enforcement_level: str,
    severity_threshold: str,
    outcome: str,
    findings: list[dict[str, Any]],
    summary: dict[str, Any],
    error: str | None = None,
) -> None:
    """Insert the run's scan result with ``ON CONFLICT DO NOTHING`` on ``run_id``.

    Idempotent so the runner's bounded POST retries (and any cross-replica race)
    are safe — exactly like ``policy_set_service._insert_evaluations``. The
    caller commits.
    """
    stmt = (
        pg_insert(SecurityScanResult)
        .values(
            id=uuid.uuid4(),
            run_id=run_id,
            engine=engine,
            enforcement_level=enforcement_level,
            severity_threshold=severity_threshold,
            outcome=outcome,
            findings=findings,
            summary=summary,
            error=error,
            created_at=now_utc(),
        )
        .on_conflict_do_nothing(index_elements=["run_id"])
    )
    await db.execute(stmt)


# ── Post-plan gate ────────────────────────────────────────────────────────


async def evaluate_post_plan(db: AsyncSession, run: Run) -> str:
    """Post-plan security-scan gate.

    The runner POSTs the scan result *before* ``plan-result`` (same ordering
    contract as policy results). The gate verifies that evidence: if the
    workspace is set to ``enforced`` and there is **no** recorded result (e.g. a
    runner image predating the scan stage skipped it), a synthetic ``errored``
    result is written so the gate fails closed and an admin can override. An
    ``off``/``advisory`` workspace is never gated; a missing advisory result has
    no enforcement effect to safeguard, so it is left unrecorded.

    Speculative (plan-only) runs are never gated — there is no apply to block.
    """
    if run.plan_only:
        return GATE_PASSED

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        return GATE_PASSED
    if (ws.security_scan_enforcement or "off") != "enforced":
        # off / advisory never block; nothing to gate.
        return GATE_PASSED

    existing = (
        await db.execute(
            select(SecurityScanResult.id).where(SecurityScanResult.run_id == run.id).limit(1)
        )
    ).first()
    if existing is None:
        # Fail-closed safety net: an enforced scan with no runner result.
        await record_scan_result(
            db,
            run_id=run.id,
            engine="",
            enforcement_level="enforced",
            severity_threshold=ws.security_scan_severity_threshold or "high",
            outcome="errored",
            findings=[],
            summary={},
            error=(
                "Runner did not perform the enforced security scan. Usually this "
                "means the runner image is older than the security-scan stage and "
                "skipped it — roll the runner image forward and retry, or override "
                "to release this run."
            ),
        )
        await db.flush()
        logger.warning(
            "Enforced security scan had no result from the runner — synthetic "
            "errored result recorded to fail closed",
            run_id=str(run.id),
        )

    return GATE_BLOCKED if await run_is_scan_blocked(db, run.id) else GATE_PASSED


async def run_is_scan_blocked(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """True if the run has an enforced scan result that failed or errored and
    has not been overridden."""
    row = (
        await db.execute(
            select(SecurityScanResult.id)
            .where(
                SecurityScanResult.run_id == run_id,
                SecurityScanResult.enforcement_level == "enforced",
                SecurityScanResult.outcome.in_(("failed", "errored")),
                SecurityScanResult.overridden_by.is_(None),
            )
            .limit(1)
        )
    ).first()
    return row is not None


# ── Read + summary + override ─────────────────────────────────────────────


async def get_run_scan(db: AsyncSession, run_id: uuid.UUID) -> SecurityScanResult | None:
    """The scan result recorded for a run (one per run), or None."""
    return (
        await db.execute(
            select(SecurityScanResult).where(SecurityScanResult.run_id == run_id).limit(1)
        )
    ).scalar_one_or_none()


async def run_scan_summary(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any] | None:
    """Compact scan status for a run's ``security-scan`` attribute.

    Returns None when no scan ran for the run, so the attribute is omitted for
    unaffected runs.
    """
    scan = await get_run_scan(db, run_id)
    if scan is None:
        return None
    blocked = await run_is_scan_blocked(db, run_id)
    if blocked:
        status = "blocked"
    elif scan.outcome in ("failed", "errored"):
        status = "advisory-failed"
    else:
        status = "passed"
    summary = scan.summary or {}
    total = summary.get("total")
    if total is None:  # fall back to the finding count only when not summarised
        total = len(scan.findings or [])
    return {
        "status": status,
        "outcome": scan.outcome,
        "engine": scan.engine,
        "total": int(total),
        "blocking": int(summary.get("blocking", 0)),
    }


async def override_run_scan(db: AsyncSession, run_id: uuid.UUID, email: str) -> int:
    """Mark a failed/errored scan result as overridden. Returns the count
    overridden (0 or 1). The caller commits."""
    scan = await get_run_scan(db, run_id)
    if scan is None or scan.outcome not in ("failed", "errored") or scan.overridden_by is not None:
        return 0
    scan.overridden_by = email
    scan.overridden_at = now_utc()
    return 1
