"""The plan, policy and apply verdicts for one pull request head.

Pure: rows in, verdicts out, no I/O. A verdict is recomputed from the rows on
every call, and a missing or incomplete evaluation can only ever produce
`pending`, never `success`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from terrapod.services.vcs_status_comment import GateVerdict

MAX_DESCRIPTION = 140

State = Literal["pending", "running", "success", "failure"]

_APPLY_THEN_MERGE = "apply_then_merge"
_PLANNED_STATUSES = frozenset({"planned", "confirmed", "applying", "applied"})
_STOPPED_STATUSES = frozenset({"errored", "discarded", "canceled", "canceling"})
_NO_AFFECTED = "No Terrapod workspaces affected"
_EVALUATING = "Evaluating affected workspaces"
#: Rows exist and none of them blocks, yet the poll pass has recorded no
#: evaluation for this head -- so there is nothing to prove the set complete and
#: the verdict cannot go green. Worded separately from `_EVALUATING` because the
#: two are reached from opposite situations and only one of them is progress:
#: with no rows at all, "evaluating" is true and transient; with every run
#: finished, it reads as progress while being capable of lasting for ever. It
#: states the fact rather than diagnosing it, because the same branch covers the
#: ordinary window between a run appearing and the next poll pass storing its
#: decisions. Distinguishing them is what would have shown #2708 on the pull
#: request instead of only in the logs.
_NO_EVALUATION = "no affected-workspace evaluation recorded for this head"
# Worst first: the order a verdict's state and its blocker list are ranked in.
_SEVERITY: dict[str, int] = {"failure": 0, "pending": 1, "running": 2}


@dataclass(frozen=True)
class WorkspaceRun:
    """The latest run of one workspace at the PR head."""

    name: str
    mode: str
    status: str
    plan_finished: bool
    apply_started: bool
    has_changes: bool | None
    gates: tuple[GateVerdict, ...]
    run_url: str | None


@dataclass(frozen=True)
class PREvaluation:
    """Which workspaces were judged affected by this PR head."""

    complete: bool
    affected: tuple[str, ...]
    not_evaluated: tuple[str, ...]
    undecided: tuple[tuple[str, str], ...]
    fork_skipped: tuple[str, ...]
    apply_then_merge_candidate: bool


@dataclass(frozen=True)
class Verdict:
    state: State
    description: str
    target_url: str | None


@dataclass(frozen=True)
class Aggregate:
    plan: Verdict
    policy: Verdict
    apply: Verdict | None


@dataclass(frozen=True)
class _Blocker:
    state: State
    name: str
    label: str
    url: str | None = None


def github_state(v: Verdict) -> str:
    return "pending" if v.state == "running" else v.state


def gitlab_state(v: Verdict) -> str:
    return "failed" if v.state == "failure" else v.state


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def _render(b: _Blocker, limit: int | None = None) -> str:
    name, label = _ascii(b.name), _ascii(b.label)
    full = f"{name} ({label})"
    if limit is None or len(full) <= limit:
        return full
    label_room = max(limit // 2 - 3, 6)
    if len(label) > label_room:
        label = label[: label_room - 3] + "..."
    name_room = limit - len(label) - 3
    if len(name) > name_room:
        name = name[: max(name_room - 3, 1)] + "..."
    return f"{name} ({label})"


def _describe(head: str, blockers: Sequence[_Blocker]) -> str:
    """The counts first, then as many blockers as fit, then `+N more`."""
    if not blockers:
        return head[:MAX_DESCRIPTION]
    head = _ascii(head)
    n = len(blockers)
    base = f"{head}; "
    for k in range(n, 0, -1):
        text = base + ", ".join(_render(b) for b in blockers[:k])
        if k < n:
            text += f", +{n - k} more"
        if len(text) <= MAX_DESCRIPTION:
            return text
    more = f", +{n - 1} more" if n > 1 else ""
    room = MAX_DESCRIPTION - len(base) - len(more)
    return base + _render(blockers[0], room) + more


def _dedupe(blockers: list[_Blocker]) -> list[_Blocker]:
    """One blocker per workspace: the worst state wins, the earlier one on a tie."""
    best: dict[str, _Blocker] = {}
    for b in blockers:
        cur = best.get(b.name)
        if cur is None or _SEVERITY[b.state] < _SEVERITY[cur.state]:
            best[b.name] = b
    return sorted(best.values(), key=lambda b: (_SEVERITY[b.state], b.name))


def _url(blockers: Sequence[_Blocker], rows: Sequence[WorkspaceRun]) -> str | None:
    for b in blockers:
        if b.url:
            return b.url
    return next((r.run_url for r in rows if r.run_url), None)


def _plan_done(r: WorkspaceRun) -> bool:
    """A plan that finished and nothing went wrong.

    `plan_finished` is stamped on planning -> errored as well, so it proves a
    plan only for a run that is still held; an errored run counts only once an
    apply had started.
    """
    if r.status in _PLANNED_STATUSES:
        return True
    if r.status == "planning":
        return r.plan_finished
    if r.status == "configuring":
        return r.apply_started or r.plan_finished
    return r.status == "errored" and r.apply_started


def _held(r: WorkspaceRun) -> bool:
    return r.status == "planning" and r.plan_finished


def _plan_blocker(r: WorkspaceRun) -> _Blocker | None:
    if r.status in ("discarded", "canceled", "canceling"):
        return _Blocker("failure", r.name, r.status.replace("canceling", "canceled"), r.run_url)
    if _plan_done(r):
        return None
    if r.status == "errored":
        return _Blocker("failure", r.name, "errored", r.run_url)
    label = "queued" if r.status in ("pending", "queued") else "planning"
    return _Blocker("running", r.name, label, r.run_url)


def _policy_blocker(r: WorkspaceRun) -> _Blocker | None:
    gates = [g for g in r.gates if g.boundary != "pre_apply"]
    for g in gates:
        if not g.passed and not g.pending:
            how = "final" if g.final else "awaiting override"
            return _Blocker("failure", r.name, f"{g.name} failed, {how}", r.run_url)
    if r.status in _STOPPED_STATUSES and not r.apply_started:
        return _Blocker("failure", r.name, "not checked", r.run_url)
    if r.status not in _STOPPED_STATUSES and not _plan_done(r):
        return _Blocker("running", r.name, "awaiting plan", r.run_url)
    for g in gates:
        if not g.passed and g.pending:
            return _Blocker("pending", r.name, f"{g.name} running", r.run_url)
    if _held(r):
        return _Blocker("pending", r.name, "held", r.run_url)
    return None


def _apply_blocker(r: WorkspaceRun) -> _Blocker | None:
    if r.status == "applied" or (r.status == "planned" and r.has_changes is False):
        return None
    if r.status in _STOPPED_STATUSES:
        failed = r.status == "errored" and r.apply_started
        label = "apply failed" if failed else r.status.replace("canceling", "canceled")
        return _Blocker("failure", r.name, label, r.run_url)
    gates = [g for g in r.gates if g.boundary == "pre_apply"]
    for g in gates:
        if not g.passed and not g.pending:
            return _Blocker("failure", r.name, f"{g.name} failed", r.run_url)
    for g in gates:
        if not g.passed and g.pending:
            return _Blocker("pending", r.name, f"{g.name} running", r.run_url)
    if r.status in ("applying", "configuring"):
        label = "applying"
    elif r.status in ("planned", "confirmed") or _held(r):
        label = "awaiting apply"
    else:
        label = "planning"
    return _Blocker("pending", r.name, label, r.run_url)


def _evaluation_blockers(
    ev: PREvaluation | None, rows: Sequence[WorkspaceRun], *, fork_label: str
) -> list[_Blocker]:
    if ev is None:
        return []
    have = {r.name for r in rows}
    out = [_Blocker("failure", n, fork_label) for n in ev.fork_skipped]
    out += [_Blocker("pending", n, reason or "undecided") for n, reason in ev.undecided]
    out += [_Blocker("pending", n, "not evaluated") for n in ev.not_evaluated]
    out += [_Blocker("pending", n, "no run yet") for n in ev.affected if n not in have]
    return out


def _verdict(
    rows: Sequence[WorkspaceRun],
    blockers: list[_Blocker],
    *,
    ev: PREvaluation | None,
    noun: str,
    empty: str,
) -> Verdict:
    """Failure over pending over running over success; success only when proven."""
    blockers = _dedupe(blockers)
    proven = ev is not None and ev.complete
    if blockers:
        state: State = blockers[0].state
    elif proven:
        state = "success"
    else:
        state = "pending"
    total = len({r.name for r in rows} | {b.name for b in blockers})
    if not total:
        head = empty if state == "success" else _EVALUATING
    elif not blockers and not proven:
        head = f"{noun} {total} of {total} workspaces; {_NO_EVALUATION}"
    else:
        head = f"{noun} {total - len(blockers)} of {total} workspaces"
    return Verdict(state, _describe(head, blockers), _url(blockers, rows))


def aggregate(rows: Sequence[WorkspaceRun], evaluation: PREvaluation | None) -> Aggregate:
    rows = sorted(rows, key=lambda r: r.name)

    plan_b = [b for r in rows if (b := _plan_blocker(r))]
    plan_b += _evaluation_blockers(evaluation, rows, fork_label="not planned: fork PR")
    plan = _verdict(rows, plan_b, ev=evaluation, noun="Planned", empty=_NO_AFFECTED)

    pol_b = [b for r in rows if (b := _policy_blocker(r))]
    pol_b += _evaluation_blockers(evaluation, rows, fork_label="not checked: fork PR")
    policy = _verdict(rows, pol_b, ev=evaluation, noun="Gates passed for", empty=_NO_AFFECTED)

    atm = [r for r in rows if r.mode == _APPLY_THEN_MERGE]
    candidate = bool(atm) or (evaluation is not None and evaluation.apply_then_merge_candidate)
    apply: Verdict | None = None
    if candidate:
        apply_b = [b for r in atm if (b := _apply_blocker(r))]
        apply_b += _evaluation_blockers(evaluation, rows, fork_label="not applied: fork PR")
        apply = _verdict(atm, apply_b, ev=evaluation, noun="Applied", empty="Nothing to apply")
    return Aggregate(plan, policy, apply)


_GATE_ICONS = {"success": "\u2705", "failure": "\u274c", "pending": "\u23f3"}


def render_gates(
    agg: Aggregate, names: Any, head_sha: str, notes: Mapping[str, str] | None = None
) -> list[str]:
    """The comment's Gates block: the verdicts as computed now from the database.

    It can differ from what the provider shows: it ignores a trigger's not-yet-committed
    state, and a post may have failed, be backed off or have been refused (`notes`, by
    status name, says so on that line). Mentions in descriptions are defused.
    """
    notes = notes or {}
    lines = [f"**Gates at `{head_sha[:7]}`**", ""]
    for name, v in (
        (names.plan_name, agg.plan),
        (names.policy_name, agg.policy),
        (names.apply_name, agg.apply),
    ):
        if v is None:
            continue
        state = github_state(v)
        text = " ".join(
            v.description.replace("`", "'").replace("<", "&lt;").replace("@", "@\u200b").split()
        )
        note = f" ({notes[name]})" if name in notes else ""
        lines.append(f"- {_GATE_ICONS[state]} `{name}`: {state} \u2014 {text}{note}")
    return lines
