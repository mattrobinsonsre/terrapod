"""Renders + posts the per-PR status comment (#282 phase 6).

One Terrapod-authored comment per PR/MR, edited in place. The renderer
collects every PR-affected workspace across modes (apply_then_merge and
merge_then_apply) and emits a single Markdown table per the worked
examples in #282.

Posted-from triggers:
  - vcs_poller after a plan finishes for a PR run
  - run_service apply-completion path
  - vcs_command_dispatcher on every command
  - run_reconciler when a planned run is invalidated by a sibling apply

The triggered task handler is `vcs_status_comment_update`. It's
idempotent — the same payload run multiple times converges on the same
comment body.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from terrapod.db.models import (
    PolicyEvaluation,
    PRSession,
    Run,
    SecurityScanResult,
    TaskStage,
    VCSConnection,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, run_links
from terrapod.services.scheduler import enqueue_trigger

logger = get_logger(__name__)


# Marker hidden in the comment body so we can find our own comment on a
# PR even if the status_comment_id isn't recorded (e.g. comment created
# manually, or PRSession was rebuilt). The HTML-comment form is invisible
# to GitHub / GitLab rendering.
_COMMENT_MARKER = "<!-- terrapod:status-comment -->"

#: Mirrors the `### Terrapod — <workspace>` heading `vcs_status_dispatcher`
#: puts on its per-workspace comment, so a reader recognises both as Terrapod's
#: at a glance. The qualifier differs because the scope does: that comment
#: speaks for one workspace, this one for every workspace the PR touches.
#: Worded without "pull request" so it reads the same on a GitLab MR.
_HEADING = "### Terrapod — all affected workspaces"


@dataclass(frozen=True)
class GateVerdict:
    """How one post-plan gate ruled on a run.

    Only gates that can actually block appear here — mandatory policy sets and
    enforced security scans. Advisory results are filtered out by the collector,
    so a verdict that reaches the renderer is listed whether it passed or
    failed: that is what makes the comment an attestation rather than only an
    alarm.

    `gate` deliberately reuses the vocabulary of `PostPlanHold.gate` and the
    run's `blocked-by` attribute (`policy`, `security-scan`, `run-task`), so the
    comment and the API cannot describe the same run differently. It is a
    separate type because `PostPlanHold` answers "what holds this run" with a
    single gate and `None` when nothing does, where this is per-gate and
    includes passes.
    """

    gate: str
    name: str
    passed: bool
    enforcement: str


@dataclass(frozen=True)
class _Row:
    """One row in the rendered status table."""

    workspace_name: str
    mode: str
    plan_summary: str
    apply_summary: str
    mergeable_summary: str
    cost_delta: str | None = None
    gates: tuple[GateVerdict, ...] = ()
    #: Run page for this row. None when `external_url` is unset, in which case
    #: the workspace name renders unlinked rather than as a broken link.
    run_url: str | None = None


def _plan_summary(run: Run | None) -> str:
    """Compact plan summary: `+ N ~ N` style, or status word for non-planned."""
    if run is None:
        return "—"
    if run.status in ("pending", "queued"):
        return "queued"
    if run.status == "planning":
        return "running"
    if run.status == "errored":
        return "errored"
    if run.status == "discarded":
        return "discarded"
    if run.status == "canceled":
        return "canceled"
    # Planned / applying / applied.
    if run.has_changes is False:
        return "no changes"
    counts = _resource_counts(run)
    return counts if counts else "changes"


def _resource_counts(run: Run) -> str:
    """`+3 ~1 -2`, omitting zero components, or "" when nothing is recorded.

    The counts are persisted by `plan-result`; runs from before that, and any
    run whose plan did not complete, leave them NULL. Returning "" there keeps
    the caller on the older `changes` wording rather than claiming `+0`.
    """
    parts = [
        (sym, n)
        for sym, n in (
            ("+", run.resource_additions),
            ("~", run.resource_changes),
            ("-", run.resource_destructions),
        )
        if n
    ]
    if not parts and not any(
        n is not None
        for n in (run.resource_additions, run.resource_changes, run.resource_destructions)
    ):
        return ""
    return " ".join(f"{sym}{n}" for sym, n in parts)


def _apply_summary(run: Run | None) -> str:
    if run is None:
        return "—"
    if run.status == "applied":
        return "applied"
    if run.status == "applying":
        return "applying"
    if run.status == "errored":
        return "errored"
    if run.status == "discarded":
        return "discarded"
    if run.status == "canceled":
        return "canceled"
    return "not applied"


def _mergeable_summary(run: Run | None) -> str:
    if run is None:
        return "—"
    if run.vcs_apply_blocked_reason:
        return f"blocked: {run.vcs_apply_blocked_reason[:60]}"
    return "yes"


#: Policy-evaluation outcomes that mean the set did not object. Anything else
#: — `failed`, and the synthetic `errored` the gate writes when a mandatory set
#: produced no evidence — is a block unless it was overridden.
_POLICY_PASS_OUTCOMES = frozenset({"passed"})


def _verdicts_from_evaluations(
    evaluations: Iterable[tuple[str, str, str, str | None]],
) -> list[GateVerdict]:
    """Turn `(set_name, enforcement_level, outcome, overridden_by)` rows into verdicts.

    Advisory sets are dropped: they cannot hold a run, so listing them would
    dilute an attestation that is meant to say "these are the gates with teeth,
    and here is how each ruled".

    An overridden failure reports as passed, because the run is not held — but
    the set is still named, so the override remains visible in the PR rather
    than only in the API.

    Pure so it can be tested without a database; the query that feeds it lives
    in the collector.
    """
    verdicts: list[GateVerdict] = []
    for name, enforcement, outcome, overridden_by in evaluations:
        if enforcement != "mandatory":
            continue
        passed = outcome in _POLICY_PASS_OUTCOMES or overridden_by is not None
        verdicts.append(GateVerdict("policy", name, passed, enforcement))
    return verdicts


#: The scan gate's enforcing level is spelled `enforced`, where the policy
#: gate spells its `mandatory`. Mirrors `security_scan_service`.
_SCAN_ENFORCING_LEVEL = "enforced"
_SCAN_PASS_OUTCOMES = frozenset({"passed"})

#: A task stage that reached `passed` or was `overridden` releases the run;
#: `pending` and `running` still hold it, which is why they are not passes.
_STAGE_PASS_STATUSES = frozenset({"passed", "overridden"})


def _verdict_from_scan(
    enforcement: str, outcome: str, overridden_by: str | None
) -> GateVerdict | None:
    """The security-scan verdict, or None when the scan could not have blocked."""
    if enforcement != _SCAN_ENFORCING_LEVEL:
        return None
    passed = outcome in _SCAN_PASS_OUTCOMES or overridden_by is not None
    return GateVerdict("security-scan", "security scan", passed, enforcement)


def _verdict_from_stage(status: str) -> GateVerdict:
    """The post-plan run-task verdict for a stage that exists.

    One verdict for the whole stage rather than one per task, because that is
    the granularity the gate works at: `run_task_service.resolve_stage` fails a
    stage only on a *mandatory* task failure, so a failed stage is by
    construction a mandatory failure and advisory tasks are already excluded.
    """
    return GateVerdict("run-task", "post-plan tasks", status in _STAGE_PASS_STATUSES, "mandatory")


async def _collect_gates(db, run_id: uuid.UUID) -> tuple[GateVerdict, ...]:
    """Every gate that could hold this run, in `post_plan_hold` evaluation order.

    Run task, then policy, then security scan — the order
    `run_service.post_plan_hold` checks them in, so the first failing verdict
    here is the same gate the run's `blocked-by` attribute names.

    Queries rather than calling the three services because those answer "is it
    blocked" with a bool; the comment needs the names and the passes too.
    """
    gates: list[GateVerdict] = []

    stage = (
        await db.execute(
            select(TaskStage.status)
            .where(TaskStage.run_id == run_id, TaskStage.stage == "post_plan")
            .order_by(TaskStage.created_at.asc(), TaskStage.id.asc())
            .limit(1)
        )
    ).first()
    if stage is not None:
        gates.append(_verdict_from_stage(stage[0]))

    evaluations = (
        await db.execute(
            select(
                PolicyEvaluation.policy_set_name,
                PolicyEvaluation.enforcement_level,
                PolicyEvaluation.outcome,
                PolicyEvaluation.overridden_by,
            )
            .where(PolicyEvaluation.run_id == run_id)
            .order_by(PolicyEvaluation.policy_set_name.asc())
        )
    ).all()
    gates.extend(_verdicts_from_evaluations(evaluations))

    scan = (
        await db.execute(
            select(
                SecurityScanResult.enforcement_level,
                SecurityScanResult.outcome,
                SecurityScanResult.overridden_by,
            )
            .where(SecurityScanResult.run_id == run_id)
            .limit(1)
        )
    ).first()
    if scan is not None:
        verdict = _verdict_from_scan(scan[0], scan[1], scan[2])
        if verdict is not None:
            gates.append(verdict)

    return tuple(gates)


def _signed_amount(amount: float) -> str:
    """`+412`, `-18.50` — sign always shown, pence only when there are any."""
    text = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    return f"+{text}" if amount > 0 else text


def _cost_delta(run: Run) -> str | None:
    """The monthly delta this run introduces, or None when it wasn't estimated.

    The engine's `diff` (go-terrapod/cost_estimate.go: "the monthly delta this
    run introduces"), not the projected total — a reviewer wants to know what
    merging costs, not what the whole workspace costs.

    None and zero are different answers: None means no cost artifact, zero
    means the plan was priced and changes nothing.
    """
    low = run.cost_diff_min
    high = run.cost_diff_max
    if low is None and high is None:
        return None
    low = high if low is None else low
    high = low if high is None else high
    if low == 0 and high == 0:
        return "no change"
    unit = f" {run.cost_currency}/mo" if run.cost_currency else "/mo"
    if low == high:
        return f"{_signed_amount(low)}{unit}"
    return f"{_signed_amount(low)} to {_signed_amount(high)}{unit}"


def _render_gate_details(row: _Row) -> str:
    """A collapsed per-workspace block listing the gates that can block.

    Empty string when the workspace has no enforcing gates — a PR touching a
    workspace with no mandatory policy set and no enforced scan should not grow
    an empty disclosure triangle.

    The summary line names the first failing gate so a reviewer can triage
    without expanding. Gates arrive in the order `post_plan_hold` evaluates
    them, so "first failing" is the same gate the run's `blocked-by` attribute
    reports.

    Follows the `<details>` convention already established for the AI summary
    in `vcs_status_dispatcher._ai_details_block`: single-line summary tag,
    `&mdash;` rather than a literal em dash, status emoji, blank line before
    the close.
    """
    if not row.gates:
        return ""
    failed = [g for g in row.gates if not g.passed]
    headline = f"blocked by {failed[0].gate}" if failed else "all gates passed"
    lines = [
        f"<details><summary><code>{_escape(row.workspace_name)}</code> "
        f"&mdash; {headline}</summary>",
        "",
    ]
    for g in row.gates:
        mark = "🔴" if not g.passed else "🟢"
        lines.append(f"- {mark} `{_escape(g.name)}` &mdash; {g.gate}, {g.enforcement}")
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def render_comment(rows: list[_Row], *, force_merge_hint: bool = False) -> str:
    """Render the Markdown status-comment body.

    Mode-aware rows: apply_then_merge workspaces show 'not applied' /
    'applied'; merge_then_apply workspaces show 'will apply on merge'
    in the apply column because they don't apply pre-merge by design.
    """
    if not rows:
        return f"{_COMMENT_MARKER}\n\n{_HEADING}\n\n_No Terrapod workspaces affected by this PR._"

    # No Mode column: `merge_then_apply` is internal vocabulary, and the Apply
    # cell already states the same fact in a reviewer's language ("will apply
    # on merge"). One column fewer also keeps the table readable on a phone.
    header = "| Workspace | Plan | Cost Δ | Apply | Mergeable |\n|---|---|---|---|---|"
    body_lines: list[str] = []
    detail_blocks: list[str] = []
    pending_apply: list[str] = []
    for r in rows:
        # merge_then_apply rows annotate the apply column to make the
        # mode-distinction explicit; apply_then_merge rows pass through.
        apply_cell = "will apply on merge" if r.mode == "merge_then_apply" else r.apply_summary
        name_cell = f"`{_escape(r.workspace_name)}`"
        if r.run_url:
            name_cell = f"[{name_cell}]({r.run_url})"
        body_lines.append(
            f"| {name_cell} | {r.plan_summary} "
            f"| {r.cost_delta or '—'} | {apply_cell} | {r.mergeable_summary} |"
        )
        # A workspace whose mandatory gate failed would have its apply refused
        # by `post_plan_hold`, so inviting one here would be the comment
        # contradicting its own details block. The block says why; the operator
        # overrides the gate and the next render offers the apply.
        blocked = any(not g.passed for g in r.gates)
        if r.mode == "apply_then_merge" and r.apply_summary == "not applied" and not blocked:
            pending_apply.append(r.workspace_name)
        block = _render_gate_details(r)
        if block:
            detail_blocks.append(block)

    parts: list[str] = [_COMMENT_MARKER, "", _HEADING, "", header, *body_lines]
    if detail_blocks:
        parts.append("")
        parts.extend(detail_blocks)
    if pending_apply:
        if len(pending_apply) == 1:
            parts.append("")
            parts.append(f"Comment `terrapod apply` to apply `{_escape(pending_apply[0])}`.")
        else:
            parts.append("")
            parts.append(
                "Comment `terrapod apply` to apply all pending workspaces, "
                "or `terrapod apply -W <workspace>` for one at a time."
            )
    if force_merge_hint:
        parts.append("")
        parts.append(
            "Auto-merge is blocked. Use `terrapod merge` to merge despite incomplete applies."
        )
    # Staleness signal, in the format `vcs_status_dispatcher` already uses on
    # its sibling comment. It carries more weight here: this comment is edited
    # as three separate runner uploads land, and every refresh is best-effort
    # by design (a dropped enqueue is swallowed rather than failing a plan), so
    # a reader has to be able to tell how current the row is.
    parts.append("")
    parts.append(f"*Updated {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}*")
    return "\n".join(parts)


def _escape(text: str) -> str:
    """Minimal Markdown / table-cell escaping for user-controlled cells."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


async def _collect_rows(db, sess: PRSession) -> list[_Row]:
    """Find every workspace whose runs reference this PR, latest run per."""
    # Workspaces in either mode that have a run for this PR. We don't
    # have a direct (connection, repo, pr) → workspace index, so we
    # pivot through the Run rows themselves.
    result = await db.execute(
        select(Run, Workspace)
        .join(Workspace, Workspace.id == Run.workspace_id)
        .where(
            Run.vcs_pull_request_number == sess.pr_number,
            Workspace.vcs_connection_id == sess.vcs_connection_id,
            (Workspace.vcs_repo_url.endswith(sess.repo)),
        )
        .order_by(Workspace.name, Run.created_at.desc())
    )
    # Reduce to latest run per workspace.
    latest_per_ws: dict[uuid.UUID, tuple[Workspace, Run]] = {}
    for run, ws in result.all():
        latest_per_ws.setdefault(ws.id, (ws, run))

    rows: list[_Row] = []
    for ws, run in sorted(latest_per_ws.values(), key=lambda pair: pair[0].name):
        rows.append(
            _Row(
                workspace_name=ws.name,
                mode=ws.vcs_workflow,
                plan_summary=_plan_summary(run),
                apply_summary=_apply_summary(run),
                mergeable_summary=_mergeable_summary(run),
                cost_delta=_cost_delta(run),
                gates=await _collect_gates(db, run.id),
                run_url=run_links.run_url(ws.id, run.id),
            )
        )
    return rows


async def _post_or_update(
    conn: VCSConnection,
    repo: str,
    pr_number: int,
    sess: PRSession,
    body: str,
) -> None:
    """Provider-dispatched post-or-update via the existing comment helpers."""
    owner, repo_name = repo.split("/", 1)
    if conn.provider == "github":
        post = github_service.create_pr_comment
        update = github_service.update_pr_comment
    elif conn.provider == "gitlab":
        post = gitlab_service.create_mr_comment
        update = gitlab_service.update_mr_comment
    else:
        logger.warning("status comment: unknown provider", provider=conn.provider)
        return

    try:
        if sess.status_comment_id:
            if conn.provider == "gitlab":
                # GitLab update takes (conn, owner, repo, mr_number, note_id, body)
                await update(conn, owner, repo_name, pr_number, int(sess.status_comment_id), body)
            else:
                await update(conn, owner, repo_name, int(sess.status_comment_id), body)
            return
        new_id = await post(conn, owner, repo_name, pr_number, body)
        sess.status_comment_id = str(new_id)
    except Exception as e:
        # Status comment failure must never break the run lifecycle. Log
        # and move on; the next state-change trigger will retry.
        logger.warning(
            "status comment post/update failed",
            provider=conn.provider,
            pr_number=pr_number,
            error=str(e),
        )


async def refresh_for_run(db, run: Run, reason: str) -> None:
    """Re-post the PR status comment for the PR this run belongs to.

    The comment is first enqueued when the poller *creates* the run, and at
    that moment none of what the comment reports exists yet: the plan has not
    run, so there are no resource counts, no cost estimate and no gate
    verdicts. Worse, those three arrive in three separate runner requests —
    `plan-json-output` carries the counts, `plan-result` drives the gates, and
    `cost-estimate` lands seconds later again — so there is no single moment
    at which one refresh would see all of them. Each input calls this as it
    lands; the handler edits one comment in place, so the PR sees it converge
    rather than gain rows.

    `reason` names which input landed and is what keeps these refreshes
    distinct: `enqueue_trigger` dedups on `SET NX` with a 300s TTL, so sharing
    one key per session would mean the *first* enqueue wins and every
    better-informed refresh inside five minutes is silently dropped — the
    comment would freeze on the `queued` snapshot the poller enqueued. Keyed
    per run and reason, a repeat of the same input still dedups, which is what
    dedup is for.

    Called from the run lifecycle, so it is deliberately best-effort in both
    directions: a run with no PR, or a PR with no session row yet, is a no-op
    rather than an error, and a failed enqueue is logged and swallowed. A
    missing comment must never fail a plan — the next input re-enqueues, and
    the handler is idempotent.
    """
    if run.vcs_pull_request_number is None:
        return
    try:
        ws = await db.get(Workspace, run.workspace_id)
        if ws is None or ws.vcs_connection_id is None:
            return
        result = await db.execute(
            select(PRSession).where(
                PRSession.vcs_connection_id == ws.vcs_connection_id,
                PRSession.pr_number == run.vcs_pull_request_number,
                PRSession.state == "open",
            )
        )
        sess = result.scalars().first()
        if sess is None:
            return
        await enqueue_trigger(
            "vcs_status_comment_update",
            {"session_id": str(sess.id)},
            dedup_key=f"vcs_status:{sess.id}:{run.id}:{reason}",
        )
    except Exception as e:
        # The whole body, not just the enqueue: the session lookup is two
        # reads on the run lifecycle's critical path, and a comment nobody
        # gets is worth less than a plan that fails to land.
        logger.warning(
            "Failed to enqueue PR status-comment refresh",
            run_id=str(run.id),
            pr_number=run.vcs_pull_request_number,
            error=repr(e),
        )


async def handle_vcs_status_comment_update(payload: dict[str, Any]) -> None:
    """Scheduler trigger handler.

    Payload:
      { "session_id": "<uuid>" }
    """
    session_id = payload.get("session_id")
    if not session_id:
        return
    async with get_db_session() as db:
        sess = await db.get(PRSession, uuid.UUID(session_id))
        if sess is None or sess.state != "open":
            return
        conn = await db.get(VCSConnection, sess.vcs_connection_id)
        if conn is None:
            return
        rows = await _collect_rows(db, sess)
        body = render_comment(rows)
        await _post_or_update(conn, sess.repo, sess.pr_number, sess, body)
        await db.commit()
