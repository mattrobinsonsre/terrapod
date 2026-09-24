"""The AI policy gate (#1766).

The third post-plan gate, alongside OPA policy sets (deterministic Rego) and
the security scan (prebuilt rule catalogues). This one rules on operator-written
natural-language criteria and on the plan summary's own risk score, so it covers
the judgement calls the other two cannot express.

It does NOT make its own model call. `summariser` extends its existing forced
tool schema with a `policy_verdict` field when the gate applies, so a verdict
arrives with the summary a deployment was already paying for, and there is no
second budget to exhaust.

**How this gate differs from its two siblings, and why the difference matters.**
OPA results and scan results are posted by the RUNNER, before `plan-result`, so
by the time `complete_plan` runs the evidence already exists and those gates
merely verify it. The summariser runs in the API and is enqueued *after*
`complete_plan`, from the plan-JSON upload — deliberately, because firing it on
the `planned` transition raced the runner and hit `Object not found` about half
the time. So this gate cannot verify evidence that has not been produced yet.
It holds instead:

  * `advisory` never waits. The run reaches `planned` immediately and the
    verdict is recorded when it lands, for the operator to read.
  * `mandatory` holds the run in `planning` until the verdict arrives, and the
    summariser re-drives the idempotent `complete_plan` once it has one —
    exactly how an asynchronous run-task stage already works.

**Fail-closed, with the reason preserved.** A mandatory gate that cannot get a
usable verdict records an `errored` evaluation and blocks. Budget exhaustion is
an errored outcome with its own message rather than a generic model failure,
because the fleet-wide `daily_token_budget` means one busy day could otherwise
freeze every apply in the deployment behind an error that reads like a model
fault. An operator seeing "the fleet's AI budget is spent" knows to raise the
budget or override; "the model call failed" sends them looking at the model.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import AIPolicyEvaluation, Run, Workspace

logger = structlog.get_logger(__name__)

GATE_PASSED = "passed"
GATE_BLOCKED = "blocked"

#: Ordered worst-last, so a threshold comparison is an index comparison.
_RISK_ORDER = ("low", "medium", "high", "critical")

#: Recognisable prefix for the budget case, so the UI and an operator can tell
#: "we have spent our tokens" from "the model refused/failed".
BUDGET_EXHAUSTED_ERROR = (
    "The deployment's daily AI token budget is exhausted, so no verdict could "
    "be reached for this run. The gate is mandatory, so the run is held rather "
    "than allowed through unchecked. Raise `ai_summary.daily_token_budget`, "
    "wait for the budget to roll over, or override this run."
)


def effective_enforcement(ws: Workspace | None) -> str:
    """The gate's enforcement for this workspace: off / advisory / mandatory.

    The per-workspace `ai_policy_mode` can opt a workspace IN when the
    deployment default is off, and OUT of an advisory verdict. It cannot opt out
    of a **mandatory** gate, and that asymmetry is the point: a fleet-wide
    blocking control any workspace admin could switch off is not a control. It
    is the same hole as a `plan_only` run applying past a mandatory policy set.
    """
    cfg = settings.ai_summary.policy
    if not cfg.enabled:
        # A workspace cannot opt into a gate the deployment has not turned on:
        # there are no criteria and no threshold to rule against.
        return "off"

    level = cfg.enforcement_level
    mode = (getattr(ws, "ai_policy_mode", "") or "default").strip().lower()

    if level == "mandatory":
        # Deliberately ignores "disabled" — see the docstring.
        return "mandatory"
    if mode == "disabled":
        return "off"
    return level


def is_configured() -> bool:
    """Whether the gate has anything to rule on.

    Enabling the switch with no criteria and no threshold leaves the gate inert,
    which is deliberate: turning it on must not start blocking runs before an
    operator has said what to block.
    """
    cfg = settings.ai_summary.policy
    return bool(cfg.deny_criteria.strip()) or cfg.risk_threshold != "off"


def deny_criteria() -> str:
    """The operator's criteria, or "" when none are configured."""
    return settings.ai_summary.policy.deny_criteria.strip()


def gate_applies_to(run: Run, ws: Workspace | None) -> bool:
    """Whether this run is ruled on at all.

    Three reasons it is not, and each is a deliberate exemption rather than an
    oversight:

      * a speculative (plan-only) run — there is no apply to block;
      * the gate is off for this workspace;
      * the gate is on but has no criteria and no threshold.

    On this line every run is Terraform/OpenTofu, so there is no engine to
    exempt: the gate reads the plan JSON and that is the only plan document
    produced here.
    """
    if run.plan_only:
        return False
    if effective_enforcement(ws) == "off":
        return False
    return is_configured()


def wants_verdict(run: Run, ws: Workspace | None) -> bool:
    """Whether the summariser should ask the model for a `policy_verdict`.

    Separate from `gate_applies_to` only in name — it is the same question asked
    from the other side, and keeping one predicate means the summariser cannot
    drift from the gate about which runs are being judged.
    """
    return gate_applies_to(run, ws)


def _meets_threshold(risk_level: str) -> bool:
    """Whether `risk_level` is at or above the configured threshold."""
    threshold = settings.ai_summary.policy.risk_threshold
    if threshold == "off":
        return False
    level = (risk_level or "").strip().lower()
    if level not in _RISK_ORDER or threshold not in _RISK_ORDER:
        return False
    return _RISK_ORDER.index(level) >= _RISK_ORDER.index(threshold)


def decide_outcome(verdict: dict[str, Any] | None, risk_level: str) -> tuple[str, str | None]:
    """The outcome for a verdict the model actually returned.

    Returns `(outcome, error)`. Either trigger is sufficient: a model `deny`, or
    a risk score at/above the threshold. They are independent on purpose —
    the threshold still protects a deployment whose criteria did not anticipate
    this change.
    """
    if verdict is None:
        # The model was asked and did not answer. On the summary path a missing
        # field is a shrug; on the gate path it is the "cannot gate on fuzzy
        # text" failure, so it is an error rather than an implicit allow.
        return "errored", (
            "The model returned no policy verdict despite being asked for one. "
            "A gate cannot infer consent from silence, so this run is treated "
            "as un-ruled."
        )

    decision = str(verdict.get("decision", "")).strip().lower()
    if decision not in {"allow", "deny"}:
        return "errored", (
            f"The model returned an unusable policy decision ({decision!r}). "
            "Expected 'allow' or 'deny'."
        )

    if decision == "deny":
        return "failed", None
    if _meets_threshold(risk_level):
        return "failed", None
    return "passed", None


async def record_evaluation(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    enforcement_level: str,
    outcome: str,
    verdict: dict[str, Any] | None = None,
    risk_level: str = "",
    error: str | None = None,
) -> AIPolicyEvaluation:
    """Upsert this run's evaluation.

    Upsert rather than insert because a summary can be regenerated, and a
    re-ruling must replace the old verdict rather than collide with it or
    accumulate a second row the gate would then have to choose between.
    """
    existing = (
        await db.execute(select(AIPolicyEvaluation).where(AIPolicyEvaluation.run_id == run_id))
    ).scalar_one_or_none()

    threshold = settings.ai_summary.policy.risk_threshold

    if existing is not None:
        existing.enforcement_level = enforcement_level
        existing.risk_threshold = threshold
        existing.outcome = outcome
        existing.verdict = verdict or {}
        existing.risk_level = risk_level or ""
        existing.error = error
        # An override belongs to the verdict it released. A re-ruling is a new
        # verdict, so the override does not carry over -- otherwise
        # regenerating a summary would silently launder a fresh deny through a
        # decision an admin made about a different one.
        existing.overridden_by = None
        existing.overridden_at = None
        return existing

    row = AIPolicyEvaluation(
        run_id=run_id,
        enforcement_level=enforcement_level,
        risk_threshold=threshold,
        outcome=outcome,
        verdict=verdict or {},
        risk_level=risk_level or "",
        error=error,
    )
    db.add(row)
    return row


async def get_evaluation(db: AsyncSession, run_id: uuid.UUID) -> AIPolicyEvaluation | None:
    return (
        await db.execute(select(AIPolicyEvaluation).where(AIPolicyEvaluation.run_id == run_id))
    ).scalar_one_or_none()


async def run_is_ai_policy_blocked(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """True when a mandatory evaluation failed or errored and was not overridden."""
    row = await get_evaluation(db, run_id)
    if row is None:
        return False
    if row.enforcement_level != "mandatory":
        return False
    if row.overridden_by:
        return False
    return row.outcome in {"failed", "errored"}


async def evaluate_post_plan(db: AsyncSession, run: Run) -> str:
    """Post-plan AI policy gate.

    Unlike its two siblings this one may be reached BEFORE its evidence exists
    (see the module docstring), so "no evaluation yet" means *wait*, not *fail*.
    A mandatory gate holds the run in `planning`; the summariser re-drives
    `complete_plan` once it has ruled.
    """
    ws = await db.get(Workspace, run.workspace_id)

    if not gate_applies_to(run, ws):
        return GATE_PASSED

    enforcement = effective_enforcement(ws)
    if enforcement != "mandatory":
        # Advisory records but never blocks, and never delays: waiting would
        # make "advisory" cost a run the same latency as "mandatory" while
        # changing nothing about the outcome.
        return GATE_PASSED

    row = await get_evaluation(db, run.id)
    if row is None:
        # The verdict has not landed. Hold -- the summariser re-drives.
        logger.info(
            "AI policy gate holding run until the verdict lands",
            run_id=str(run.id),
        )
        return GATE_BLOCKED

    return GATE_BLOCKED if await run_is_ai_policy_blocked(db, run.id) else GATE_PASSED


async def override(
    db: AsyncSession, *, run_id: uuid.UUID, actor: str, enforcement_level: str = ""
) -> AIPolicyEvaluation:
    """Release a held run, recording who did it.

    Creates the evaluation when none exists rather than refusing. A run can be
    held precisely BECAUSE no verdict landed -- the summariser never ran, its
    enqueue was dropped, or it raised before settling -- and that is the state
    where release is most needed. Refusing it was backwards: the endpoint
    answered 409 and told the operator to wait for a verdict that was never
    coming, leaving discard as the only exit while the run kept its workspace
    lock.

    The row it writes is an honest record, not a forged pass: outcome
    `overridden`, no verdict, and an `error` saying no ruling was ever
    reached. An auditor can tell it apart from a gate that actually ran.
    """
    from terrapod.db.models import now_utc

    row = await get_evaluation(db, run_id)
    if row is None:
        row = await record_evaluation(
            db,
            run_id=run_id,
            enforcement_level=enforcement_level or "mandatory",
            outcome="overridden",
            error=(
                "Released by an operator before any verdict was recorded. The "
                "gate never ruled on this run."
            ),
        )
    row.overridden_by = actor
    row.overridden_at = now_utc()
    await db.flush()
    logger.warning(
        "AI policy gate overridden",
        run_id=str(run_id),
        actor=actor,
        outcome=row.outcome,
    )
    return row


__all__ = [
    "BUDGET_EXHAUSTED_ERROR",
    "GATE_BLOCKED",
    "GATE_PASSED",
    "decide_outcome",
    "deny_criteria",
    "effective_enforcement",
    "evaluate_post_plan",
    "gate_applies_to",
    "get_evaluation",
    "is_configured",
    "override",
    "record_evaluation",
    "run_is_ai_policy_blocked",
    "wants_verdict",
]
