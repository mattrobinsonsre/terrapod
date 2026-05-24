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
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import PolicyEvaluation, PolicySet, Run, Workspace, now_utc

logger = structlog.get_logger(__name__)

# evaluate_post_plan return values (the gate contract with complete_plan).
# GATE_PENDING is gone — the runner-on-side flow eliminates the JSON-wait
# window the API used to need a "try again next tick" signal for.
GATE_PASSED = "passed"
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


def build_run_context(ws: Workspace, run: Run) -> dict[str, Any]:
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


# ── Evaluation persistence ────────────────────────────────────────────


async def _insert_evaluations(db: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """Insert PolicyEvaluation rows with ``ON CONFLICT DO NOTHING``.

    The post-plan gate can race across replicas: two reconcilers can
    both observe ``already is None`` before either commits, then both
    attempt to write the same ``(run_id, policy_set_id)`` rows. The
    unique constraint ``uq_policy_evaluations_run_set`` would surface
    that as IntegrityError → 500. Using Postgres ``ON CONFLICT DO
    NOTHING`` makes the second writer's rows silent no-ops, leaving the
    canonical state from whichever replica won. The wasted OPA work in
    that narrow window is tolerated.
    """
    if not rows:
        return
    stmt = (
        pg_insert(PolicyEvaluation)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["run_id", "policy_set_id"])
    )
    await db.execute(stmt)


async def evaluate_post_plan(db: AsyncSession, run: Run) -> str:
    """Post-plan policy gate — a pure DB query.

    The runner has already fetched the applicable policy bundle from
    ``GET /policy-bundle``, run ``opa eval`` per policy against the
    plan JSON it produced locally, and POSTed the results to
    ``/policy-results`` *before* posting ``plan-result``. So by the
    time the API gets here the evaluation rows exist (or there were no
    applicable sets, in which case no rows is the correct answer).

    Speculative (plan-only) runs are evaluated and recorded but never
    gated — there is no apply to block.

    Returns ``GATE_PASSED`` or ``GATE_BLOCKED``. ``GATE_PENDING`` is
    gone — the runner-side flow eliminates the JSON-wait window and
    its grace-timer machinery.
    """
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
