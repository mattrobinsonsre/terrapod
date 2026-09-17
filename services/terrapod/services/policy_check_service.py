"""A run's policy checks, in the shape the `tofu`/`terraform` CLI reads (#1704).

The cloud backend in the CLI shows each of a run's policy checks after the plan,
and on a `soft_failed` one asks whether to override it (or, with
`-auto-approve`, overrides it itself when the caller may). Terrapod has two
post-plan gates that fit that model, so each becomes one check:

- **OPA** (`polchk-opa-<run>`): every policy set evaluated for the run. It
  soft-fails while a mandatory set's failure is not overridden; overriding it
  overrides them all, as the Policy Checks panel does. Scope `organization`,
  since policy sets belong to the organization.
- **Security scan** (`polchk-scan-<run>`): the run's Checkov/Trivy result. It
  soft-fails while an enforced scan's failure is not overridden. Scope
  `workspace`, since the scan is configured per workspace.

Nothing here is stored: a check is read off the evaluation and scan rows each
time, so it cannot drift from what the gates in `complete_plan` see. A check
exists only when its gate recorded something for the run.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import PolicyEvaluation, Run, SecurityScanResult
from terrapod.services import policy_set_service, security_scan_service

OPA = "opa"
SCAN = "scan"
KINDS = (OPA, SCAN)

# Terraform Enterprise's policy check statuses that Terrapod reports.
PASSED = "passed"
SOFT_FAILED = "soft_failed"
OVERRIDDEN = "overridden"
STATUSES = frozenset({PASSED, SOFT_FAILED, OVERRIDDEN})

_FAILING = ("failed", "errored")
_SEVERITY_ORDER = ("critical", "high", "medium", "low", "unknown")


@dataclass
class PolicyCheck:
    """One policy check, derived from what a gate recorded for a run."""

    kind: str
    run_id: uuid.UUID
    status: str
    scope: str
    passed: int = 0
    advisory_failed: int = 0
    soft_failed: int = 0
    queued_at: datetime | None = None
    decided_at: datetime | None = None
    output: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return check_id(self.kind, self.run_id)

    @property
    def is_overridable(self) -> bool:
        return self.status == SOFT_FAILED


def check_id(kind: str, run_id: uuid.UUID) -> str:
    return f"polchk-{kind}-{run_id}"


def parse_check_id(value: str) -> tuple[str, uuid.UUID] | None:
    """`polchk-<kind>-<run uuid>` to (kind, run uuid), or None if malformed."""
    rest = value.removeprefix("polchk-")
    kind, _, run_part = rest.partition("-")
    if kind not in KINDS or rest == value:
        return None
    try:
        return kind, uuid.UUID(run_part.removeprefix("run-"))
    except ValueError:
        return None


def _opa_check(run_id: uuid.UUID, evals: list[PolicyEvaluation]) -> PolicyCheck:
    blocking = [
        e
        for e in evals
        if e.enforcement_level == "mandatory" and e.outcome in _FAILING and e.overridden_by is None
    ]
    overridden = [e for e in evals if e.outcome in _FAILING and e.overridden_by is not None]
    advisory = [e for e in evals if e.enforcement_level != "mandatory" and e.outcome in _FAILING]
    if blocking:
        status = SOFT_FAILED
    elif any(e.enforcement_level == "mandatory" for e in overridden):
        status = OVERRIDDEN
    else:
        status = PASSED

    lines: list[str] = []
    for e in sorted(evals, key=lambda e: e.policy_set_name):
        state = e.outcome
        if e.outcome in _FAILING and e.overridden_by is not None:
            state = f"{e.outcome}, overridden by {e.overridden_by}"
        lines.append(f"Policy set {e.policy_set_name!r} ({e.enforcement_level}): {state}")
        result = e.result or {}
        if result.get("error"):
            lines.append(f"  error: {result['error']}")
        for pol in result.get("policies") or []:
            mark = "passed" if pol.get("passed") else "failed"
            lines.append(f"  {pol.get('policy', '')}: {mark}")
            if pol.get("error"):
                lines.append(f"    error: {pol['error']}")
            for msg in pol.get("violations") or []:
                lines.append(f"    deny: {msg}")
            for msg in pol.get("warnings") or []:
                lines.append(f"    warn: {msg}")

    decided = [e.overridden_at for e in overridden if e.overridden_at]
    return PolicyCheck(
        kind=OPA,
        run_id=run_id,
        status=status,
        scope="organization",
        passed=sum(1 for e in evals if e.outcome == "passed"),
        advisory_failed=len(advisory),
        soft_failed=len(blocking),
        queued_at=min(e.created_at for e in evals),
        decided_at=max(decided) if decided else None,
        output="\n".join(lines),
    )


def _scan_check(run_id: uuid.UUID, scan: SecurityScanResult) -> PolicyCheck:
    failing = scan.outcome in _FAILING
    enforced = scan.enforcement_level == "enforced"
    if failing and enforced and scan.overridden_by is None:
        status = SOFT_FAILED
    elif failing and enforced:
        status = OVERRIDDEN
    else:
        status = PASSED

    summary = scan.summary or {}
    lines = [
        f"Security scan ({scan.engine or 'no engine'}, {scan.enforcement_level}, "
        f"blocking at {scan.severity_threshold} and above): {scan.outcome}"
    ]
    if scan.overridden_by:
        lines.append(f"  overridden by {scan.overridden_by}")
    if scan.error:
        lines.append(f"  error: {scan.error}")
    findings = sorted(
        scan.findings or [],
        key=lambda f: (
            _SEVERITY_ORDER.index(f.get("severity"))
            if f.get("severity") in _SEVERITY_ORDER
            else len(_SEVERITY_ORDER)
        ),
    )
    if findings:
        lines.append(f"  {len(findings)} finding(s), {int(summary.get('blocking', 0))} blocking:")
    for f in findings:
        where = f.get("resource") or f.get("file") or ""
        lines.append(
            f"    [{f.get('severity', 'unknown')}] {f.get('rule_id', '')} {f.get('title', '')}"
            + (f" ({where})" if where else "")
        )

    return PolicyCheck(
        kind=SCAN,
        run_id=run_id,
        status=status,
        scope="workspace",
        passed=0 if failing else 1,
        advisory_failed=1 if failing and not enforced else 0,
        soft_failed=1 if status == SOFT_FAILED else 0,
        queued_at=scan.created_at,
        decided_at=scan.overridden_at,
        output="\n".join(lines),
    )


async def list_checks(db: AsyncSession, run: Run) -> list[PolicyCheck]:
    """The run's policy checks, OPA first, in the order its gates run."""
    checks: list[PolicyCheck] = []
    evals = await policy_set_service.get_run_evaluations(db, run.id)
    if evals:
        checks.append(_opa_check(run.id, evals))
    scan = await security_scan_service.get_run_scan(db, run.id)
    if scan is not None:
        checks.append(_scan_check(run.id, scan))
    return checks


async def get_check(db: AsyncSession, run: Run, kind: str) -> PolicyCheck | None:
    for check in await list_checks(db, run):
        if check.kind == kind:
            return check
    return None


async def override_check(db: AsyncSession, run: Run, kind: str, email: str) -> int:
    """Override what a check covers; the caller commits and re-drives the run."""
    if kind == OPA:
        return await policy_set_service.override_run_policies(db, run.id, email)
    return await security_scan_service.override_run_scan(db, run.id, email)
