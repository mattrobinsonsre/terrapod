"""Policy-set scoping, evaluation orchestration and run gating (#343).

This service decides which policy sets apply to a workspace (label-RBAC
allow/deny, mirroring roles), drives OPA evaluation for a run at the
post-plan boundary via :mod:`policy_engine`, persists the outcome to
``policy_evaluations``, and answers the gating question for
``run_service.complete_plan``.

Gating model
------------
A mandatory policy set that fails (or errors) keeps the run *in
``planning``* — it is not transitioned to ``errored``. This is a
deliberate departure from the run-task stage gate: a run held in
``planning`` is re-driven cleanly by the idempotent ``complete_plan`` on
the next reconciler tick, so an admin override (or the operator editing
the policy set) takes effect without racing a reconciler that would
otherwise have errored the run. The block is surfaced to the UI via the
run's ``policy-checks`` attribute. The user can always discard.

Speculative (plan-only) runs are evaluated and recorded so results are
visible, but never gated — there is no apply to block.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import PolicyEvaluation, PolicySet, Run, Workspace, now_utc
from terrapod.services import policy_engine
from terrapod.storage import get_storage
from terrapod.storage.keys import plan_json_output_key
from terrapod.storage.protocol import ObjectNotFoundError

logger = structlog.get_logger(__name__)

# How long to wait for the runner to upload the plan JSON (uploaded just
# after it posts plan-result) before treating it as genuinely
# unavailable. The common case resolves within one reconciler tick.
PLAN_JSON_GRACE_SECONDS = 180

# evaluate_post_plan return values (the gate contract with complete_plan).
GATE_PASSED = "passed"
GATE_PENDING = "pending"
GATE_BLOCKED = "blocked"


# ── Scoping ───────────────────────────────────────────────────────────


def _labels_match(ws_labels: dict[str, Any], rule_labels: dict[str, Any]) -> bool:
    """True if the workspace's labels satisfy any one rule-label entry.

    Rule values may be a single scalar or a list of accepted values
    (same shape as role allow/deny labels).
    """
    for key, accepted in (rule_labels or {}).items():
        if key not in ws_labels:
            continue
        accepted_values = accepted if isinstance(accepted, list) else [accepted]
        if ws_labels[key] in accepted_values:
            return True
    return False


def policy_set_applies(ps: PolicySet, ws_name: str, ws_labels: dict[str, Any]) -> bool:
    """Decide whether a policy set is in scope for a workspace.

    ``global_scope`` wins outright. Otherwise the label-RBAC allow/deny
    model applies, with deny taking precedence over allow.
    """
    if not ps.enabled:
        return False
    if ps.global_scope:
        return True
    if ws_name in (ps.deny_names or []):
        return False
    if _labels_match(ws_labels, ps.deny_labels or {}):
        return False
    if ws_name in (ps.allow_names or []):
        return True
    return _labels_match(ws_labels, ps.allow_labels or {})


async def applicable_policy_sets(db: AsyncSession, ws: Workspace) -> list[PolicySet]:
    """All enabled policy sets in scope for the given workspace.

    Eager-loads ``policies`` so the per-policy evaluation downstream does
    not trigger a sync lazy-load (and the greenlet-spawn error) inside
    the async event loop.
    """
    rows = (
        (
            await db.execute(
                select(PolicySet)
                .where(PolicySet.enabled.is_(True))
                .options(selectinload(PolicySet.policies))
            )
        )
        .scalars()
        .all()
    )
    return [ps for ps in rows if policy_set_applies(ps, ws.name, ws.labels or {})]


# ── OPA input context ─────────────────────────────────────────────────


def _build_context(ws: Workspace, run: Run) -> dict[str, Any]:
    """Terrapod metadata exposed to policies as ``data.terrapod_context``."""
    return {
        "workspace": {
            "id": str(ws.id),
            "name": ws.name,
            "labels": ws.labels or {},
        },
        "run": {
            "id": str(run.id),
            "message": run.message or "",
            "source": run.source or "",
            "is_destroy": bool(run.is_destroy),
            "plan_only": bool(run.plan_only),
        },
    }


# ── Evaluation orchestration ──────────────────────────────────────────


async def _evaluate_one_set(
    ps: PolicySet, plan_json: bytes, context: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Evaluate every policy in a set; return ``(outcome, result_json)``.

    Outcome precedence: any policy that errors → ``errored``; else any
    violations → ``failed``; else ``passed``. ``errored`` and ``failed``
    both gate a mandatory set (fail-closed on an un-evaluable policy).
    """
    results = []
    any_error = False
    any_violation = False
    for policy in ps.policies:
        res = await policy_engine.evaluate_policy(policy.name, policy.rego, plan_json, context)
        results.append(res.to_dict())
        if res.error is not None:
            any_error = True
        elif res.violations:
            any_violation = True

    if any_error:
        outcome = "errored"
    elif any_violation:
        outcome = "failed"
    else:
        outcome = "passed"
    return outcome, {"policies": results, "evaluated_at": datetime.now(UTC).isoformat()}


async def _record_unavailable(db: AsyncSession, run: Run, sets: list[PolicySet]) -> None:
    """Record an ``errored`` evaluation per set when the plan JSON never
    arrived. Fail-closed: a mandatory set then blocks the run."""
    for ps in sets:
        db.add(
            PolicyEvaluation(
                run_id=run.id,
                policy_set_id=ps.id,
                policy_set_name=ps.name,
                enforcement_level=ps.enforcement_level,
                outcome="errored",
                result={"error": "plan JSON was not available for policy evaluation"},
            )
        )
    logger.warning(
        "Policy evaluation skipped — plan JSON unavailable",
        run_id=str(run.id),
        sets=len(sets),
    )


async def evaluate_post_plan(db: AsyncSession, run: Run) -> str:
    """Evaluate applicable policy sets for a run at the post-plan gate.

    Returns one of :data:`GATE_PASSED` / :data:`GATE_PENDING` /
    :data:`GATE_BLOCKED`. Adds ``PolicyEvaluation`` rows to the session
    (the caller commits). Idempotent — evaluation runs once; later calls
    only re-check the gate (so an override takes effect).
    """
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        return GATE_PASSED

    sets = await applicable_policy_sets(db, ws)
    if not sets:
        return GATE_PASSED

    already = (
        await db.execute(
            select(PolicyEvaluation.id).where(PolicyEvaluation.run_id == run.id).limit(1)
        )
    ).first()

    if already is None:
        # First pass — evaluate. We need the plan JSON the runner uploads
        # just after posting plan-result; wait a bounded time for it.
        if not run.has_json_output:
            finished = run.plan_finished_at
            waited = (now_utc() - finished).total_seconds() if finished is not None else 0.0
            if waited <= PLAN_JSON_GRACE_SECONDS:
                return GATE_PENDING
            await _record_unavailable(db, run, sets)
        else:
            try:
                plan_json = await get_storage().get(
                    plan_json_output_key(str(run.workspace_id), str(run.id))
                )
            except ObjectNotFoundError:
                await _record_unavailable(db, run, sets)
                plan_json = None

            if plan_json is not None:
                context = _build_context(ws, run)
                for ps in sets:
                    outcome, result = await _evaluate_one_set(ps, plan_json, context)
                    db.add(
                        PolicyEvaluation(
                            run_id=run.id,
                            policy_set_id=ps.id,
                            policy_set_name=ps.name,
                            enforcement_level=ps.enforcement_level,
                            outcome=outcome,
                            result=result,
                        )
                    )
                logger.info(
                    "Policy evaluation complete",
                    run_id=str(run.id),
                    sets=len(sets),
                )
        await db.flush()

    # Speculative runs are recorded but never gated — there is no apply.
    if run.plan_only:
        return GATE_PASSED

    return GATE_BLOCKED if await run_is_policy_blocked(db, run.id) else GATE_PASSED


# ── Gate query + summary ──────────────────────────────────────────────


async def run_is_policy_blocked(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """True if the run has a mandatory policy evaluation that failed or
    errored and has not been overridden."""
    row = (
        await db.execute(
            select(PolicyEvaluation.id)
            .where(
                PolicyEvaluation.run_id == run_id,
                PolicyEvaluation.enforcement_level == "mandatory",
                PolicyEvaluation.outcome.in_(("failed", "errored")),
                PolicyEvaluation.overridden_by.is_(None),
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def get_run_evaluations(db: AsyncSession, run_id: uuid.UUID) -> list[PolicyEvaluation]:
    """All policy evaluations recorded for a run, newest first."""
    return list(
        (
            await db.execute(
                select(PolicyEvaluation)
                .where(PolicyEvaluation.run_id == run_id)
                .order_by(PolicyEvaluation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def run_policy_summary(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any] | None:
    """Compact policy status for a run's ``policy-checks`` attribute.

    Returns ``None`` when no policy sets were evaluated for the run, so
    the attribute is omitted entirely for unaffected runs.
    """
    evals = await get_run_evaluations(db, run_id)
    if not evals:
        return None
    passed = sum(1 for e in evals if e.outcome == "passed")
    failed = sum(1 for e in evals if e.outcome in ("failed", "errored"))
    blocked = await run_is_policy_blocked(db, run_id)
    if blocked:
        status = "blocked"
    elif failed:
        status = "advisory-failed"
    else:
        status = "passed"
    return {
        "status": status,
        "total": len(evals),
        "passed": passed,
        "failed": failed,
    }


async def override_run_policies(db: AsyncSession, run_id: uuid.UUID, email: str) -> int:
    """Mark every failed/errored evaluation of a run as overridden.

    Returns the number of evaluations overridden. The caller commits.
    """
    evals = await get_run_evaluations(db, run_id)
    count = 0
    stamp = now_utc()
    for e in evals:
        if e.outcome in ("failed", "errored") and e.overridden_by is None:
            e.overridden_by = email
            e.overridden_at = stamp
            count += 1
    return count
