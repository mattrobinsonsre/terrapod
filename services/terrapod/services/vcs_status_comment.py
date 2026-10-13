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

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from terrapod.db.models import (
    PlanSummary,
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


#: Session states whose comment is still worth editing.
#:
#: `merged` is here for #1878: the post-merge plan+apply happens strictly after
#: the PR is merged, so refusing to touch a merged session would refuse exactly
#: the updates that turn "will apply on merge" into what actually happened.
#: `closed` is NOT here — a PR abandoned without merging sets off no runs, and
#: nothing more will ever be learned about it.
_LIVE_SESSION_STATES = ("open", "merged")

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


#: Redis key prefix caching this PR's one comment id, so the common refresh
#: costs no comment listing. Moved here from `vcs_status_dispatcher` with the
#: rest of the posting machinery when that module stopped owning a comment.
_COMMENT_CACHE_PREFIX = "tp:vcs_comment:"
_COMMENT_CACHE_TTL = 7 * 24 * 3600  # 7 days

_COMMENT_LOCK_PREFIX = "tp:vcs_comment_lock:"
_COMMENT_LOCK_TTL = 10  # seconds — generous bound on one create/update round-trip
_COMMENT_LOCK_MAX_ATTEMPTS = 20  # x 0.1s = ~2s total wait
_COMMENT_LOCK_INTERVAL = 0.1

#: Atomic release: delete only while the lock still carries our token, so a
#: worker whose TTL lapsed cannot delete the lock its successor now holds.
_LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


@dataclass(frozen=True)
class GateVerdict:
    """How one post-plan gate ruled on a run.

    Only gates that can actually block appear here — mandatory policy sets and
    enforced security scans. Advisory results are filtered out by the collector,
    so a verdict that reaches the renderer is listed whether it passed or
    failed: that is what makes the comment an attestation rather than only an
    alarm.

    `gate` deliberately reuses the vocabulary of `PostPlanHold.gate` and the
    run's `blocked-by` attribute (`run-task`, `policy`, `security-scan`,
    `ai-policy`), so the comment and the API cannot describe the same run
    differently. It is a separate type because `PostPlanHold` answers "what
    holds this run" with a single gate and `None` when nothing does, where this
    is per-gate and includes passes.
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
    #: What became of the plan+apply the merge set off (#1878), and where to
    #: read it. Empty until that run exists, which is every render before the
    #: PR is merged — so an open PR's table is exactly what it was.
    post_merge_summary: str = ""
    post_merge_url: str | None = None
    #: The AI summary for this row's run (#401), folded into this workspace's
    #: own `<details>` block rather than posted as a second comment (#1940).
    #: None when the summariser is off (its default), when no row has landed
    #: yet, or when the row it landed is not `ready`.
    ai_summary: PlanSummary | None = None


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
#: Risk pill per AI-summary level and per risk-factor severity. Shared by
#: both so one level cannot render as two different colours in one block.
_RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}

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


def _verdict_from_ai_policy(row: Any | None, enforcement: str, *, held: bool) -> GateVerdict | None:
    """The AI policy gate's verdict, or None when it could not have blocked.

    Unlike its three siblings this gate can hold a run with **no row at all**:
    its verdict is produced in the API after the plan, so a mandatory gate
    holds the run while the summariser is still ruling, and forever if it never
    does. Keying the verdict on a row therefore reports a held run as clear —
    which is the bug this exists to fix, because the comment then offered a
    `terrapod apply` the gate would refuse.

    `held` is the authority (`ai_policy_service.run_is_held_by_ai_policy`);
    `row` only supplies the wording.
    """
    if enforcement != "mandatory":
        return None
    if row is None and not held:
        # The gate is mandatory but not ruling on this run at all (plan-only,
        # or no criteria and no threshold). Nothing to attest.
        return None
    name = "AI policy gate"
    if row is None:
        name = "AI policy gate (awaiting verdict)"
    return GateVerdict("ai-policy", name, not held, "mandatory")


# The three boundaries run tasks fire at (#1837), in the order a run meets
# them. `post_plan` is the only one this comment used to know about, which is
# why a run held at `pre_apply` was reported as having nothing wrong with it.
_STAGE_LABELS = {
    "pre_plan": "pre-plan tasks",
    "post_plan": "post-plan tasks",
    "pre_apply": "pre-apply tasks",
}


def _verdict_from_stage(status: str, stage: str = "post_plan") -> GateVerdict:
    """The run-task verdict for a stage that exists.

    One verdict for the whole stage rather than one per task, because that is
    the granularity the gate works at: `run_task_service.resolve_stage` fails a
    stage only on a *mandatory* task failure, so a failed stage is by
    construction a mandatory failure and advisory tasks are already excluded.
    """
    return GateVerdict(
        "run-task",
        _STAGE_LABELS.get(stage, stage),
        status in _STAGE_PASS_STATUSES,
        "mandatory",
    )


async def _collect_gates(db, run: Run) -> tuple[GateVerdict, ...]:
    """Every gate that could hold this run, in `post_plan_hold` evaluation order.

    Run task, then policy, then security scan, then the AI policy gate — the
    order `run_service.post_plan_hold` checks them in, so the first failing
    verdict here is the same gate the run's `blocked-by` attribute names.

    Queries rather than calling the services because those answer "is it
    blocked" with a bool; the comment needs the names and the passes too. The
    AI gate is the exception — it is asked directly, because the state that
    matters there (held with no verdict yet) has no row to read.
    """
    run_id = run.id
    gates: list[GateVerdict] = []

    # ALL THREE boundaries, not just `post_plan`. Querying one of them is how
    # a run held at `pre_apply` came back with no gates at all: `blocked` was
    # then False, so the comment offered "Comment `terrapod apply`" for a run
    # `confirm_run` would refuse — and the refusal posts nothing, so the
    # reviewer commented, got a success reaction, and watched the same
    # invitation re-render. `apply_then_merge` plus a mandatory `pre_apply`
    # task is the headline use case for #1837, so that is the intended
    # configuration, not a corner.
    stages = (
        await db.execute(
            select(TaskStage.stage, TaskStage.status)
            .where(TaskStage.run_id == run_id, TaskStage.stage.in_(tuple(_STAGE_LABELS)))
            .order_by(TaskStage.created_at.asc(), TaskStage.id.asc())
        )
    ).all()
    seen_stages: set[str] = set()
    for stage_name, stage_status in stages:
        # One verdict per boundary: a re-driven run can accumulate more than
        # one row for the same stage, and the earliest is the live one.
        if stage_name in seen_stages:
            continue
        seen_stages.add(stage_name)
        gates.append(_verdict_from_stage(stage_status, stage_name))

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

    from terrapod.services import ai_policy_service

    ws = await db.get(Workspace, run.workspace_id)
    enforcement = ai_policy_service.effective_enforcement(ws)
    if enforcement == "mandatory":
        # Only then are the two reads worth making: an off or advisory gate
        # cannot hold a run, so it never appears in the attestation.
        ai_verdict = _verdict_from_ai_policy(
            await ai_policy_service.get_evaluation(db, run_id),
            enforcement,
            held=await ai_policy_service.run_is_held_by_ai_policy(db, run),
        )
        if ai_verdict is not None:
            gates.append(ai_verdict)

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


def _render_ai_narrative(s: PlanSummary) -> list[str]:
    """The AI summary's body lines, for nesting inside a workspace block.

    Returns lines rather than a joined string, and deliberately opens no
    `<details>` of its own: this is folded into the single per-workspace block
    `_render_workspace_details` builds, and a disclosure triangle inside
    another renders as a second click for the same content.

    Moved here from `vcs_status_dispatcher` when the per-workspace comment was
    retired (#1940). The narrative's only home is now this comment, so the
    renderer lives beside it rather than being imported across modules.

    A risk factor that is not a mapping is skipped rather than rendered as its
    repr: `risk_factors` is model-authored JSON, so a malformed element is
    possible and a PR comment is the wrong place to surface it.
    """
    lines = [(s.description or "").strip()]
    if s.risk_factors:
        heading = "**Suggested fixes:**" if s.kind == "failure_analysis" else "**Risk factors:**"
        lines.extend(["", heading, ""])
        for rf in s.risk_factors:
            if not isinstance(rf, dict):
                continue
            sev = (rf.get("severity") or "").lower()
            head = f"- {_RISK_EMOJI.get(sev, '⚪')} **{_escape(rf.get('title', ''))}**"
            addr = rf.get("resource_address", "")
            if addr:
                head += f" — `{_escape(addr)}`"
            lines.append(head)
            detail = (rf.get("detail", "") or "").strip()
            if detail:
                lines.append(f"  {detail}")
    return lines


def _render_workspace_details(row: _Row) -> str:
    """One collapsed block per workspace: its AI narrative and its gates.

    This function is the whole of #1940. The narrative and the gate verdicts
    used to live on two different comments with two opposite update semantics
    — this table edited in place for ever, the narrative reposted on every
    push because the commit SHA was part of its identity — so a
    four-workspace PR with four pushes carried seventeen Terrapod comments.
    Both now render here, inside the row they describe, on the one comment
    this module edits.

    Empty string when the workspace has neither a narrative nor an enforcing
    gate: a PR touching a workspace with no mandatory policy set, no enforced
    scan and no summariser should not grow an empty disclosure triangle.

    The summary line carries the risk pill and names the first failing gate, so
    a reviewer can triage the whole PR without expanding anything. Gates arrive
    in the order `post_plan_hold` evaluates them, so "first failing" is the same
    gate the run's `blocked-by` attribute reports.
    """
    summary = row.ai_summary
    ready = summary is not None and summary.status == "ready"
    if not row.gates and not ready:
        return ""

    bits: list[str] = []
    if ready:
        level = summary.risk_level or "unknown"
        pill = _RISK_EMOJI.get(summary.risk_level or "", "⚪")
        bits.append(f"{pill} risk: <strong>{_escape(level)}</strong>")
    if row.gates:
        failed = [g for g in row.gates if not g.passed]
        bits.append(f"blocked by {failed[0].gate}" if failed else "all gates passed")

    lines = [
        f"<details><summary><code>{_escape(row.workspace_name)}</code> "
        f"&mdash; {' &middot; '.join(bits)}</summary>",
        "",
    ]
    if ready:
        kind_label = "Failure analysis" if summary.kind == "failure_analysis" else "AI summary"
        lines.extend([f"**🤖 {kind_label}**", ""])
        lines.extend(_render_ai_narrative(summary))
    if row.gates:
        # The heading only earns its place when a narrative sits above it;
        # on a gates-only block the list is the whole content.
        if ready:
            lines.extend(["", "**Gates:**", ""])
        for g in row.gates:
            mark = "🔴" if not g.passed else "🟢"
            lines.append(f"- {mark} `{_escape(g.name)}` &mdash; {g.gate}, {g.enforcement}")
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def render_comment(rows: list[_Row]) -> str:
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
        # Once the merge has actually happened, the Apply cell reports it (#1878).
        # This is the whole point of that issue in one line: a `merge_then_apply`
        # row used to read "will apply on merge" for ever — including long after
        # the merge, the apply, and whatever came of it — so the PR's last word
        # on its own change was a prediction. Now the prediction is replaced by
        # the outcome, linked to the run that produced it.
        if r.post_merge_summary:
            apply_cell = r.post_merge_summary
            if r.post_merge_url:
                apply_cell = f"[{apply_cell}]({r.post_merge_url})"
        elif r.mode == "merge_then_apply":
            # merge_then_apply rows annotate the apply column to make the
            # mode-distinction explicit; apply_then_merge rows pass through.
            apply_cell = "will apply on merge"
        else:
            apply_cell = r.apply_summary
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
        block = _render_workspace_details(r)
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


async def _ready_ai_summary(db, run: Run) -> PlanSummary | None:
    """This run's AI summary when there is one worth showing (#1940).

    Only a `ready` row. The other three statuses — pending, skipped,
    errored — have nothing a PR reader can act on, and disclosing an empty
    or failed row would make this comment noisier than the two comments it
    replaced, which is the opposite of the point.

    `PlanSummary` is one-to-one with `Run` and upserted on `run_id`, so there
    is at most one row to find and `kind` distinguishes a plan summary from a
    failure analysis within it.

    Failure answers None. The narrative is the one part of this comment that
    enhances rather than reports — losing it degrades the comment, where
    raising here would cost the whole table.
    """
    try:
        return (
            await db.execute(
                select(PlanSummary).where(
                    PlanSummary.run_id == run.id,
                    PlanSummary.status == "ready",
                )
            )
        ).scalar_one_or_none()
    except Exception as e:
        logger.debug(
            "Could not load the AI summary for the PR comment",
            run_id=str(run.id),
            error=str(e),
        )
        return None


async def _row_for(db, ws: Workspace, run: Run, merged_run: Run | None = None) -> _Row:
    """One table row for a (workspace, run) pair.

    Shared by both collectors so a workspace PR and a module PR cannot end up
    reporting the same run differently — which is the whole point of there
    being one comment and one renderer.
    """
    return _Row(
        workspace_name=ws.name,
        mode=ws.vcs_workflow,
        plan_summary=_plan_summary(run),
        apply_summary=_apply_summary(run),
        mergeable_summary=_mergeable_summary(run),
        cost_delta=_cost_delta(run),
        gates=await _collect_gates(db, run),
        ai_summary=await _ready_ai_summary(db, run),
        run_url=run_links.run_url(ws.id, run.id),
        post_merge_summary=_post_merge_summary(merged_run),
        post_merge_url=(run_links.run_url(ws.id, merged_run.id) if merged_run else None),
    )


async def _collect_module_rows(db, workspace_ids, pr_number: int) -> list[_Row]:
    """Rows for a module PR: every consuming workspace's module-impact run.

    A module PR has no `PRSession` — the poller opens those for workspace
    repositories, and this PR belongs to the module's — so the rows come from
    the runs directly and the comment is found by its marker.

    Scoped to `source == "module-test"` as well as the number. PR numbers are
    per-repository and nothing on a run records which repository its number
    came from, so without the source filter a workspace's own PR #7 would
    appear on a module's PR #7.
    """
    ids = list(workspace_ids or [])
    if not ids:
        return []
    result = await db.execute(
        select(Run, Workspace)
        .join(Workspace, Workspace.id == Run.workspace_id)
        .where(
            Run.workspace_id.in_(ids),
            Run.source == "module-test",
            Run.vcs_pull_request_number == pr_number,
        )
        .order_by(Workspace.name, Run.created_at.desc())
    )
    latest: dict[uuid.UUID, tuple[Workspace, Run]] = {}
    for run, ws in result.all():
        latest.setdefault(ws.id, (ws, run))
    return [
        await _row_for(db, ws, run)
        for ws, run in sorted(latest.values(), key=lambda pair: pair[0].name)
    ]


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

    post_merge = await _post_merge_runs(db, sess)

    rows: list[_Row] = []
    # Every workspace this PR touched, whether it was seen before the merge or
    # only after it (#1878) — a workspace whose first run for this change is the
    # post-merge one still belongs in the table.
    seen: dict[uuid.UUID, Workspace] = {ws.id: ws for ws, _ in latest_per_ws.values()}
    seen.update({ws.id: ws for ws, _ in post_merge.values()})
    for ws in sorted(seen.values(), key=lambda w: w.name):
        pair = latest_per_ws.get(ws.id)
        merged_pair = post_merge.get(ws.id)
        # The speculative run is the row's basis where there is one; otherwise
        # the post-merge run is all there is to report.
        run = pair[1] if pair else merged_pair[1]  # type: ignore[index]
        merged_run = merged_pair[1] if merged_pair else None
        rows.append(await _row_for(db, ws, run, merged_run))
    return rows


async def _post_merge_runs(db, sess: PRSession) -> dict[uuid.UUID, tuple[Workspace, Run]]:
    """The latest run per workspace that this PR's merge set off (#1878).

    Empty until the poller has attributed a merge commit to the session, which
    is every call before the PR is merged.

    Matched on the commit, not on a PR number: these are branch runs and carry
    `vcs_pull_request_number IS NULL`. That is load-bearing rather than
    incidental — the poller reads a non-null value there as "this is a
    speculative PR run" in three places, so writing the number onto these runs
    would make the commit look unhandled, break the branch-run dedup, and get
    the run force-cancelled when the PR left the open list.
    """
    if not sess.merge_commit_sha:
        return {}
    result = await db.execute(
        select(Run, Workspace)
        .join(Workspace, Workspace.id == Run.workspace_id)
        .where(
            Run.vcs_commit_sha == sess.merge_commit_sha,
            Run.vcs_pull_request_number.is_(None),
            Workspace.vcs_connection_id == sess.vcs_connection_id,
            (Workspace.vcs_repo_url.endswith(sess.repo)),
        )
        .order_by(Workspace.name, Run.created_at.desc())
    )
    latest: dict[uuid.UUID, tuple[Workspace, Run]] = {}
    for run, ws in result.all():
        latest.setdefault(ws.id, (ws, run))
    return latest


def _post_merge_summary(run: Run | None) -> str:
    """What became of the run the merge set off (#1878).

    Deliberately says `applied` / `errored` and not much else: the row already
    carries what the change was, and this column answers the one question the
    reviewer is left with after approving it — did it land?
    """
    if run is None:
        return ""
    if run.status == "applied":
        return "applied"
    if run.status == "errored":
        return "errored"
    if run.status in ("applying", "confirmed"):
        return "applying"
    if run.status in ("planning", "queued", "pending"):
        return "running"
    if run.status == "planned":
        # Reached `planned` and stopped: something is holding the apply — an
        # unmet gate, or a workspace that does not auto-apply.
        return "awaiting apply"
    return run.status


async def _acquire_comment_lock(redis, key: str) -> str | None:
    """Bounded-retry SETNX lock; the token on success, None on timeout.

    Keyed per PR, not per run or per status. Two triggers for the same PR can
    be in flight at once — three runner uploads land separately, and a module
    PR refreshes once per consuming workspace — and without this they race
    through "no comment found" and each POST one, leaving duplicates that no
    later edit can merge. Serialising confines that window to one worker.
    """
    token = uuid.uuid4().hex
    for _ in range(_COMMENT_LOCK_MAX_ATTEMPTS):
        if await redis.set(key, token, nx=True, ex=_COMMENT_LOCK_TTL):
            return token
        await asyncio.sleep(_COMMENT_LOCK_INTERVAL)
    return None


async def _release_comment_lock(redis, key: str, token: str) -> None:
    """Atomic release — delete only while the lock still carries our token."""
    try:
        await redis.eval(_LOCK_RELEASE_LUA, 1, key, token)
    except Exception as e:
        logger.debug("Failed to release the PR comment lock", error=str(e))


async def _find_comment_by_marker(
    conn: VCSConnection, owner: str, repo_name: str, pr_number: int
) -> int | None:
    """This PR's Terrapod comment, found by the marker in its body.

    `_COMMENT_MARKER` has been written into every comment this module posts
    since it was introduced, with a docstring saying it exists so we can find
    our own comment when the recorded id is missing — but until #1940 nothing
    read it, so that fallback did not exist. It does now, and it is what lets
    a PR with no `PRSession` row keep to one comment: a module PR belongs to
    the module's repository, so the poller never opens a session for it.

    None on any failure, which the caller treats as "not found" and posts a
    new one. That can duplicate a comment if a listing fails transiently,
    which is why the caller holds the per-PR lock and caches the id it ends
    up with.
    """
    try:
        if conn.provider == "gitlab":
            comments = await gitlab_service.list_mr_comments(conn, owner, repo_name, pr_number)
        else:
            comments = await github_service.list_pr_comments(conn, owner, repo_name, pr_number)
    except Exception as e:
        logger.warning("Could not list PR comments to find ours", error=str(e))
        return None
    for c in comments:
        if _COMMENT_MARKER in (c.get("body") or ""):
            return c["id"]
    return None


async def _post_or_update(
    conn: VCSConnection,
    repo: str,
    pr_number: int,
    body: str,
    recorded_id: str | None = None,
) -> str | None:
    """Post or edit THE one Terrapod comment on this PR. Returns its id.

    Three ways to find it, cheapest first: the id the caller recorded, the id
    cached in Redis, then a listing matched on `_COMMENT_MARKER`. Only when
    all three come up empty is a comment created — which is what makes this
    "one comment per PR" rather than "one comment per thing that posts".

    The returned id is for the caller to persist where it has somewhere to
    persist it (`PRSession.status_comment_id`); a module PR has nowhere, and
    relies on the Redis cache plus the marker instead.

    Failure returns None and is logged, never raised: a comment nobody gets is
    worth less than the run that was trying to report itself.
    """
    from terrapod.redis.client import get_redis_client

    owner, repo_name = repo.split("/", 1)
    if conn.provider == "gitlab":
        create, update = gitlab_service.create_mr_comment, gitlab_service.update_mr_comment
    elif conn.provider == "github":
        create, update = github_service.create_pr_comment, github_service.update_pr_comment
    else:
        logger.warning("status comment: unknown provider", provider=conn.provider)
        return None

    async def _edit(comment_id: int) -> None:
        if conn.provider == "gitlab":
            await update(conn, owner, repo_name, pr_number, comment_id, body)
        else:
            await update(conn, owner, repo_name, comment_id, body)

    redis = get_redis_client()
    cache_key = f"{_COMMENT_CACHE_PREFIX}{repo}:{pr_number}"
    lock_key = f"{_COMMENT_LOCK_PREFIX}{repo}:{pr_number}"

    token = await _acquire_comment_lock(redis, lock_key)
    if token is None:
        # Another worker is mid-flight on this PR. Every refresh renders the
        # whole comment from current rows, so dropping this one loses nothing
        # the lock holder is not already about to write.
        logger.warning(
            "Could not acquire the PR-comment lock; another worker is updating",
            repo=repo,
            pr_number=pr_number,
        )
        return recorded_id

    try:
        candidate: int | None = None
        if recorded_id:
            try:
                candidate = int(recorded_id)
            except (TypeError, ValueError):
                candidate = None
        if candidate is None:
            try:
                cached = await redis.get(cache_key)
                candidate = int(cached) if cached else None
            except Exception as e:
                logger.debug("Comment-id cache unreadable", error=str(e))

        if candidate is not None:
            try:
                await _edit(candidate)
                await redis.set(cache_key, str(candidate), ex=_COMMENT_CACHE_TTL)
                return str(candidate)
            except Exception:
                # Deleted by hand, or the cache is stale. Fall through and
                # look for it properly rather than posting a second one.
                logger.debug("Known comment id did not accept an edit", comment_id=candidate)

        found = await _find_comment_by_marker(conn, owner, repo_name, pr_number)
        if found is not None:
            try:
                await _edit(found)
                await redis.set(cache_key, str(found), ex=_COMMENT_CACHE_TTL)
                return str(found)
            except Exception as e:
                logger.warning("Failed to update the PR status comment", error=str(e))
                return recorded_id

        try:
            new_id = await create(conn, owner, repo_name, pr_number, body)
            await redis.set(cache_key, str(new_id), ex=_COMMENT_CACHE_TTL)
            return str(new_id)
        except Exception as e:
            logger.warning(
                "Failed to create the PR status comment",
                provider=conn.provider,
                pr_number=pr_number,
                error=str(e),
            )
            return recorded_id
    finally:
        await _release_comment_lock(redis, lock_key, token)


async def refresh_module_pr_comment(
    db, conn: VCSConnection, repo: str, pr_number: int, workspace_ids
) -> None:
    """Refresh the one Terrapod comment on a module's PR (#1940).

    A module PR used to carry one comment per consuming workspace, keyed on
    the workspace id — so a module with ten consumers put ten comments on one
    PR, each reposted on every push because the commit SHA was part of its
    identity too. This renders every consumer as a row of the same table the
    workspace PRs get, into one comment, edited in place.

    Best-effort in both directions: no rows means nothing is posted rather
    than an empty table, and a failure is logged rather than raised, because
    this runs on the module-impact path and must not fail an analysis run.
    """
    try:
        rows = await _collect_module_rows(db, workspace_ids, pr_number)
        if not rows:
            return
        await _post_or_update(conn, repo, pr_number, render_comment(rows))
    except Exception as e:
        logger.warning(
            "Failed to refresh the module PR status comment",
            repo=repo,
            pr_number=pr_number,
            error=repr(e),
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
    try:
        ws = await db.get(Workspace, run.workspace_id)
        if ws is None or ws.vcs_connection_id is None:
            return
        if run.vcs_pull_request_number is not None:
            # A speculative run on the PR: found by the PR it belongs to.
            where = [
                PRSession.pr_number == run.vcs_pull_request_number,
                PRSession.state.in_(_LIVE_SESSION_STATES),
            ]
        elif run.vcs_commit_sha:
            # A branch run — the plan+apply a merge set off (#1878). It carries
            # no PR number, so the merge commit the poller attributed is the
            # join. A commit that closed no PR matches nothing and this is a
            # cheap miss, which is the common case for a direct push.
            where = [PRSession.merge_commit_sha == run.vcs_commit_sha]
        else:
            return
        result = await db.execute(
            select(PRSession).where(PRSession.vcs_connection_id == ws.vcs_connection_id, *where)
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
        if sess is None or sess.state not in _LIVE_SESSION_STATES:
            return
        conn = await db.get(VCSConnection, sess.vcs_connection_id)
        if conn is None:
            return
        rows = await _collect_rows(db, sess)
        body = render_comment(rows)
        comment_id = await _post_or_update(
            conn, sess.repo, sess.pr_number, body, recorded_id=sess.status_comment_id
        )
        # Recorded so the next refresh costs no comment listing. Left alone on
        # failure rather than cleared: a transient listing error must not make
        # the next refresh believe there is no comment and post a second one.
        if comment_id:
            sess.status_comment_id = comment_id
        await db.commit()
