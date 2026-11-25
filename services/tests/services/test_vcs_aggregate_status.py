"""Tests for the pure plan / policy / apply aggregation."""

import itertools
from types import SimpleNamespace

import pytest

from terrapod.services.vcs_aggregate_status import (
    MAX_DESCRIPTION,
    Aggregate,
    PREvaluation,
    Verdict,
    WorkspaceRun,
    aggregate,
    github_state,
    gitlab_state,
    render_gates,
)
from terrapod.services.vcs_status_comment import GateVerdict


def _eval(**kw) -> PREvaluation:
    base = {
        "complete": True,
        "affected": (),
        "not_evaluated": (),
        "undecided": (),
        "fork_skipped": (),
        "apply_then_merge_candidate": False,
    }
    base.update(kw)
    return PREvaluation(**base)


def _run(name="ws", **kw) -> WorkspaceRun:
    base = {
        "name": name,
        "mode": "merge_then_apply",
        "status": "planned",
        "plan_finished": True,
        "apply_started": False,
        "has_changes": True,
        "gates": (),
        "run_url": f"https://tp.example/runs/{name}",
    }
    base.update(kw)
    return WorkspaceRun(**base)


def _atm(name="ws", **kw) -> WorkspaceRun:
    return _run(name, mode="apply_then_merge", **kw)


def _states(agg):
    return (agg.plan.state, agg.policy.state, agg.apply.state if agg.apply else None)


class TestEmptyPullRequest:
    """A PR that touches no workspace must still get plan and policy green."""

    def test_complete_evaluation_with_no_rows_is_green(self):
        agg = aggregate([], _eval())
        assert agg.plan.state == "success"
        assert agg.plan.description == "No Terrapod workspaces affected"
        assert agg.policy.state == "success"
        assert agg.apply is None

    def test_apply_candidate_gets_nothing_to_apply(self):
        agg = aggregate([], _eval(apply_then_merge_candidate=True))
        assert agg.apply == Verdict("success", "Nothing to apply", None)

    def test_no_record_is_pending_never_green(self):
        agg = aggregate([], None)
        assert agg.plan.state == "pending"
        assert agg.plan.description == "Evaluating affected workspaces"
        assert agg.policy.state == "pending"
        assert agg.apply is None

    def test_finished_runs_with_no_recorded_evaluation_do_not_read_as_progress(self):
        """The branch that #2708 sat in for ever, worded to say so.

        Every run is finished and nothing blocks, but no evaluation was recorded,
        so the set cannot be proven complete and the verdict stays pending. It
        used to share `Evaluating affected workspaces` with the no-rows case,
        which reads as progress -- and on a Redis Cluster that wording was
        permanent, so the pull request gave no hint while the logs were explicit.

        The description must state that the evaluation is missing rather than
        that evaluation is under way, and must not blame the runs: they are fine.
        """
        rows = [_run(name="a"), _run(name="b")]
        agg = aggregate(rows, None)

        assert agg.plan.state == "pending"
        assert agg.plan.description != "Evaluating affected workspaces"
        assert "no affected-workspace evaluation recorded" in agg.plan.description
        assert "2 of 2" in agg.plan.description
        # The no-rows case is genuinely transient and keeps the old wording.
        assert aggregate([], None).plan.description == "Evaluating affected workspaces"

    def test_incomplete_evaluation_is_pending(self):
        agg = aggregate([], _eval(complete=False, not_evaluated=("a",)))
        assert agg.plan.state == "pending"
        assert "a" in agg.plan.description

    def test_undecided_workspace_is_named_and_never_proven_untouched(self):
        agg = aggregate([], _eval(undecided=(("api", "archive fetch failed"),)))
        assert agg.plan.state == "pending"
        assert "api" in agg.plan.description
        assert agg.policy.state == "pending"

    def test_incomplete_with_nothing_named_is_still_pending(self):
        for rows in ([], [_atm(status="applied", apply_started=True)]):
            agg = aggregate(rows, _eval(complete=False, apply_then_merge_candidate=True))
            assert agg.plan.state == "pending"
            assert agg.policy.state == "pending"
            assert agg.apply.state == "pending"

    def test_no_record_never_turns_rows_green(self):
        assert _states(aggregate([_run()], None))[:2] == ("pending", "pending")

    def test_no_record_keeps_a_failure(self):
        agg = aggregate([_run(status="errored", plan_finished=False)], None)
        assert agg.plan.state == "failure"

    def test_fork_skipped_fails_plan(self):
        agg = aggregate([], _eval(fork_skipped=("web",)))
        assert agg.plan.state == "failure"
        assert "not planned: fork PR" in agg.plan.description
        assert "web" in agg.plan.description
        assert agg.policy.state == "failure"


class TestPlan:
    def test_queued_and_planning_are_running(self):
        for status in ("pending", "queued", "planning"):
            agg = aggregate([_run(status=status, plan_finished=False)], _eval())
            assert agg.plan.state == "running", status

    def test_planned_is_success(self):
        agg = aggregate([_run()], _eval())
        assert agg.plan.state == "success"
        assert agg.plan.description == "Planned 1 of 1 workspaces"

    def test_a_planned_apply_then_merge_run_does_not_hold_plan_pending(self):
        agg = aggregate(
            [_atm()],
            _eval(apply_then_merge_candidate=True),
        )
        assert agg.plan.state == "success"
        assert agg.apply is not None and agg.apply.state == "pending"

    def test_held_at_gate_counts_as_planned(self):
        gate = GateVerdict("policy", "guard", False, "mandatory")
        agg = aggregate(
            [_run(status="planning", plan_finished=True, gates=(gate,))],
            _eval(),
        )
        assert agg.plan.state == "success"
        assert agg.policy.state == "failure"

    def test_errored_before_plan_fails_plan_and_policy(self):
        agg = aggregate([_run(status="errored", plan_finished=False)], _eval())
        assert agg.plan.state == "failure"
        assert agg.policy.state == "failure"
        assert "not checked" in agg.policy.description

    def test_discarded_and_canceled_fail(self):
        for status in ("discarded", "canceled"):
            agg = aggregate([_run(status=status, plan_finished=False)], _eval())
            assert agg.plan.state == "failure", status
            assert agg.policy.state == "failure", status

    def test_failure_beats_pending_beats_success(self):
        rows = [
            _run("a"),
            _run("b", status="planning", plan_finished=False),
            _run("c", status="errored", plan_finished=False),
        ]
        assert aggregate(rows, _eval()).plan.state == "failure"
        rows = rows[:2]
        assert aggregate(rows, _eval(undecided=(("d", "x"),))).plan.state == "pending"
        assert aggregate(rows, _eval()).plan.state == "running"

    def test_a_failure_description_names_blockers_after_counts(self):
        rows = [_run("a"), _run("b", status="errored", plan_finished=False)]
        desc = aggregate(rows, _eval()).plan.description
        assert desc.startswith("Planned 1 of 2")
        assert "b" in desc

    def test_target_url_is_the_first_blocker_run(self):
        rows = [_run("a"), _run("b", status="errored", plan_finished=False)]
        assert aggregate(rows, _eval()).plan.target_url == "https://tp.example/runs/b"

    def test_target_url_falls_back_to_first_run(self):
        assert aggregate([_run("a")], _eval()).plan.target_url == "https://tp.example/runs/a"

    def test_target_url_is_none_with_no_rows(self):
        assert aggregate([], _eval()).plan.target_url is None


class TestPolicy:
    def test_passed_gates_are_success(self):
        gate = GateVerdict("policy", "g", True, "mandatory")
        assert aggregate([_run(gates=(gate,))], _eval()).policy.state == "success"

    def test_overridden_failure_is_success(self):
        # an overridden set is reported as passed by the collector
        gate = GateVerdict("policy", "g", True, "mandatory")
        assert aggregate([_run(gates=(gate,))], _eval()).policy.state == "success"

    def test_failed_gate_awaiting_override_is_failure(self):
        gate = GateVerdict("policy", "g", False, "mandatory")
        agg = aggregate([_run(status="planning", gates=(gate,))], _eval())
        assert agg.policy.state == "failure"
        assert "awaiting override" in agg.policy.description

    def test_running_run_task_is_pending(self):
        gate = GateVerdict(
            "run-task", "post-plan tasks", False, "mandatory", pending=True, boundary="post_plan"
        )
        agg = aggregate([_run(status="planning", gates=(gate,))], _eval())
        assert agg.policy.state == "pending"

    def test_pre_plan_final_failure_is_failure_final(self):
        gate = GateVerdict(
            "run-task",
            "pre-plan tasks",
            False,
            "mandatory",
            final=True,
            boundary="pre_plan",
        )
        agg = aggregate(
            [_run(status="pending", plan_finished=False, gates=(gate,))],
            _eval(),
        )
        assert agg.policy.state == "failure"
        assert "final" in agg.policy.description

    def test_policy_waits_for_the_plan(self):
        agg = aggregate([_run(status="planning", plan_finished=False)], _eval())
        assert agg.policy.state == "running"

    def test_pre_apply_task_does_not_touch_policy(self):
        gate = GateVerdict(
            "run-task", "pre-apply tasks", False, "mandatory", pending=True, boundary="pre_apply"
        )
        agg = aggregate([_atm(gates=(gate,))], _eval(apply_then_merge_candidate=True))
        assert agg.policy.state == "success"
        assert agg.apply is not None and agg.apply.state == "pending"

    def test_ai_policy_awaiting_verdict_is_pending(self):
        gate = GateVerdict("ai-policy", "AI policy gate", False, "mandatory", pending=True)
        agg = aggregate([_run(status="planning", gates=(gate,))], _eval())
        assert agg.policy.state == "pending"


class TestApply:
    def _ev(self):
        return _eval(apply_then_merge_candidate=True)

    def test_none_when_no_apply_then_merge_workspace(self):
        assert aggregate([_run()], _eval()).apply is None

    def test_planned_with_changes_is_pending(self):
        assert aggregate([_atm()], self._ev()).apply.state == "pending"

    def test_no_change_plan_counts_as_applied(self):
        agg = aggregate([_atm(has_changes=False)], self._ev())
        assert agg.apply.state == "success"

    def test_applied_is_success(self):
        agg = aggregate([_atm(status="applied", apply_started=True)], self._ev())
        assert agg.apply.state == "success"

    def test_errored_after_apply_started(self):
        agg = aggregate([_atm(status="errored", apply_started=True)], self._ev())
        assert agg.plan.state == "success"
        assert agg.apply.state == "failure"

    def test_ended_without_applying_is_failure(self):
        agg = aggregate([_atm(status="discarded")], self._ev())
        assert agg.apply.state == "failure"

    @pytest.mark.parametrize("status", ["canceled", "canceling"])
    def test_a_canceled_apply_is_labelled_canceled_not_failed(self, status):
        agg = aggregate([_atm(status=status, apply_started=True)], self._ev())
        assert agg.apply.state == "failure"
        assert "canceled" in agg.apply.description
        assert "apply failed" not in agg.apply.description

    def test_an_errored_apply_is_labelled_apply_failed(self):
        agg = aggregate([_atm(status="errored", apply_started=True)], self._ev())
        assert "apply failed" in agg.apply.description

    def test_mixed_modes_count_only_apply_then_merge(self):
        rows = [_run("m"), _atm("a", status="applied", apply_started=True)]
        agg = aggregate(rows, self._ev())
        assert agg.apply.state == "success"
        assert "1 of 1" in agg.apply.description

    def test_merge_then_apply_failure_does_not_reach_apply(self):
        rows = [_run("m", status="errored", plan_finished=False)]
        assert aggregate(rows, self._ev()).apply.state == "success"

    def test_final_pre_apply_failure_is_failure(self):
        gate = GateVerdict(
            "run-task", "pre-apply tasks", False, "mandatory", final=True, boundary="pre_apply"
        )
        agg = aggregate([_atm(gates=(gate,))], self._ev())
        assert agg.apply.state == "failure"
        assert agg.policy.state == "success"

    def test_unfinished_evaluation_is_never_a_success(self):
        rows = [_atm(status="applied", apply_started=True)]
        agg = aggregate(
            rows, _eval(complete=False, not_evaluated=("x",), apply_then_merge_candidate=True)
        )
        assert agg.apply.state == "pending"


class TestDescriptions:
    def test_fifty_long_named_workspaces_fit_and_are_ascii(self):
        names = [f"workspace-{i:02d}-" + "x" * 47 for i in range(50)]
        assert all(len(n) == 60 for n in names)
        rows = [_run(n, status="errored", plan_finished=False) for n in names]
        agg = aggregate(rows, _eval())
        for v in (agg.plan, agg.policy):
            assert len(v.description) <= MAX_DESCRIPTION
            assert v.description.isascii()
        assert "more" in agg.plan.description

    def test_a_single_overlong_name_is_cut(self):
        rows = [_run("y" * 300, status="errored", plan_finished=False)]
        desc = aggregate(rows, _eval()).plan.description
        assert len(desc) <= MAX_DESCRIPTION
        assert "..." in desc

    def test_non_ascii_names_are_made_ascii(self):
        rows = [_run("caf\u00e9", status="errored", plan_finished=False)]
        assert aggregate(rows, _eval()).plan.description.isascii()

    def test_every_verdict_is_within_limit_on_apply_too(self):
        names = [f"w{i}" + "z" * 58 for i in range(50)]
        rows = [_atm(n, status="discarded") for n in names]
        agg = aggregate(rows, _eval(apply_then_merge_candidate=True))
        assert len(agg.apply.description) <= MAX_DESCRIPTION


class TestProviderStates:
    @pytest.mark.parametrize(
        ("state", "github", "gitlab"),
        [
            ("pending", "pending", "pending"),
            ("running", "pending", "running"),
            ("success", "success", "success"),
            ("failure", "failure", "failed"),
        ],
    )
    def test_maps(self, state, github, gitlab):
        v = Verdict(state, "d", None)
        assert github_state(v) == github
        assert gitlab_state(v) == gitlab


class TestAFailedPlanIsNeverGreen:
    """The run goes planning -> errored and the plan-finished stamp is set on
    that very transition, so the stamp alone proves nothing."""

    def test_errored_with_a_stamped_plan_fails_plan_and_policy(self):
        agg = aggregate([_run(status="errored", plan_finished=True, has_changes=None)], _eval())
        assert agg.plan.state == "failure"
        assert agg.policy.state == "failure"
        assert "not checked" in agg.policy.description

    def test_errored_after_apply_started_still_counts_as_planned(self):
        agg = aggregate([_atm(status="errored", plan_finished=True, apply_started=True)], _eval())
        assert agg.plan.state == "success"

    @pytest.mark.parametrize("status", ["discarded", "canceled", "canceling"])
    def test_stopped_runs_with_a_finished_plan_are_not_green(self, status):
        agg = aggregate([_run(status=status, plan_finished=True)], _eval())
        assert agg.plan.state == "failure"
        assert agg.policy.state == "failure"


_ALL_STATUSES = [
    "pending",
    "queued",
    "planning",
    "planned",
    "confirmed",
    "applying",
    "configuring",
    "applied",
    "errored",
    "discarded",
    "canceled",
    "canceling",
]
_STOPPED = {"errored", "discarded", "canceled", "canceling"}


def _plan_expected(status, plan_finished, apply_started):
    """Independent restatement: when does `plan` mean planned and nothing wrong."""
    if status in ("planned", "confirmed", "applying", "applied"):
        return "success"
    if status == "configuring":
        return "success" if (apply_started or plan_finished) else "running"
    if status == "planning":
        return "success" if plan_finished else "running"
    if status == "errored":
        return "success" if apply_started else "failure"
    if status in _STOPPED:
        return "failure"
    return "running"


def _policy_expected(status, plan_finished, apply_started):
    """No gates at all: only a finished, unstopped, unheld plan is clear."""
    if status in _STOPPED and not apply_started:
        return "failure"
    if status == "planning":
        return "pending" if plan_finished else "running"
    if status in ("pending", "queued"):
        return "running"
    if status == "configuring" and not (apply_started or plan_finished):
        return "running"
    return "success"


def _apply_expected(status, has_changes, apply_started):
    if status == "applied" or (status == "planned" and has_changes is False):
        return "success"
    return "failure" if status in _STOPPED else "pending"


_CELLS = list(
    itertools.product(
        _ALL_STATUSES,
        [True, False],
        [True, False],
        [True, False, None],
        ["merge_then_apply", "apply_then_merge"],
    )
)


class TestEveryRunStateAgainstTheVerdicts:
    @pytest.mark.parametrize(
        ("status", "plan_finished", "apply_started", "has_changes", "mode"), _CELLS
    )
    def test_cell(self, status, plan_finished, apply_started, has_changes, mode):
        row = _run(
            "w",
            mode=mode,
            status=status,
            plan_finished=plan_finished,
            apply_started=apply_started,
            has_changes=has_changes,
            gates=(),
        )
        agg = aggregate([row], _eval(apply_then_merge_candidate=mode == "apply_then_merge"))
        assert agg.plan.state == _plan_expected(status, plan_finished, apply_started)
        assert agg.policy.state == _policy_expected(status, plan_finished, apply_started)
        if mode == "apply_then_merge":
            assert agg.apply.state == _apply_expected(status, has_changes, apply_started)
        else:
            assert agg.apply is None

    def test_the_table_covers_every_status_the_state_machine_knows(self):
        from terrapod.services.run_service import TERMINAL_STATES, VALID_TRANSITIONS

        known = set(VALID_TRANSITIONS) | set(TERMINAL_STATES)
        assert known <= set(_ALL_STATUSES)

    def test_planned_without_a_stamp_is_still_planned(self):
        agg = aggregate([_run(status="planned", plan_finished=False)], _eval())
        assert agg.plan.state == "success"

    def test_applied_without_a_stamp_is_still_planned(self):
        agg = aggregate([_run(status="applied", plan_finished=False)], _eval())
        assert agg.plan.state == "success"


class TestAHeldRunIsNotCleared:
    def test_held_with_no_visible_gate_is_pending(self):
        agg = aggregate([_run(status="planning", plan_finished=True, gates=())], _eval())
        assert agg.plan.state == "success"
        assert agg.policy.state == "pending"
        assert "held" in agg.policy.description


class TestAnAffectedWorkspaceWithNoRowBlocks:
    def test_missing_row_is_pending_and_named(self):
        agg = aggregate([_run("a")], _eval(affected=("a", "b")))
        assert agg.plan.state == "pending"
        assert agg.policy.state == "pending"
        assert "b" in agg.plan.description
        assert "Planned 1 of 2" in agg.plan.description

    def test_every_affected_workspace_having_a_row_is_green(self):
        agg = aggregate([_run("a"), _run("b")], _eval(affected=("a", "b")))
        assert (agg.plan.state, agg.policy.state) == ("success", "success")

    def test_missing_row_holds_apply_when_a_candidate_exists(self):
        ev = _eval(affected=("a", "b"), apply_then_merge_candidate=True)
        agg = aggregate([_atm("a", status="applied", apply_started=True)], ev)
        assert agg.apply.state == "pending"

    def test_affected_only_and_no_rows_is_pending_not_untouched(self):
        agg = aggregate([], _eval(affected=("a",)))
        assert agg.plan.state == "pending"
        assert agg.plan.description != "No Terrapod workspaces affected"


class TestForkSkippedAndApply:
    def test_a_fork_pr_with_an_apply_candidate_is_not_nothing_to_apply(self):
        agg = aggregate([], _eval(fork_skipped=("web",), apply_then_merge_candidate=True))
        assert agg.apply.state == "failure"
        assert "web" in agg.apply.description

    def test_fork_skipped_beside_an_applied_workspace_still_fails_apply(self):
        ev = _eval(fork_skipped=("web",), apply_then_merge_candidate=True)
        agg = aggregate([_atm("a", status="applied", apply_started=True)], ev)
        assert agg.apply.state == "failure"

    def test_no_candidate_means_no_apply_even_with_a_fork(self):
        assert aggregate([], _eval(fork_skipped=("web",))).apply is None


class TestUndecidedAndApply:
    def test_undecided_with_a_complete_record_is_not_nothing_to_apply(self):
        ev = _eval(undecided=(("a", "locked"),), apply_then_merge_candidate=True)
        agg = aggregate([], ev)
        assert agg.apply.state == "pending"
        assert "a" in agg.apply.description

    def test_not_evaluated_beside_applied_rows_is_pending(self):
        ev = _eval(not_evaluated=("x",), apply_then_merge_candidate=True)
        agg = aggregate([_atm("a", status="applied", apply_started=True)], ev)
        assert agg.apply.state == "pending"


class TestDescriptionDetails:
    def test_a_long_label_is_shortened_and_the_suffix_survives(self):
        gate = GateVerdict("policy", "p" * 200, False, "mandatory", final=True)
        rows = [_run(f"w{i}", gates=(gate,), status="planning") for i in range(5)]
        desc = aggregate(rows, _eval()).policy.description
        assert len(desc) <= MAX_DESCRIPTION
        assert desc.endswith("more")
        assert "+4 more" in desc

    def test_the_more_suffix_is_never_cut(self):
        names = [f"n{i}" + "q" * 70 for i in range(10)]
        rows = [_run(n, status="errored", plan_finished=False) for n in names]
        desc = aggregate(rows, _eval()).plan.description
        assert len(desc) <= MAX_DESCRIPTION
        assert desc.endswith(" more")

    def test_a_workspace_is_listed_once_with_the_strongest_reason(self):
        rows = [_run("a", status="errored", plan_finished=False)]
        desc = aggregate(rows, _eval(undecided=(("a", "locked"),))).plan.description
        assert desc.count("a (") == 1
        assert "errored" in desc and "locked" not in desc

    def test_counts_include_evaluation_only_workspaces(self):
        rows = [_run("a"), _run("b")]
        desc = aggregate(rows, _eval(undecided=(("x", "locked"),))).plan.description
        assert desc.startswith("Planned 2 of 3")


class TestRenderGates:
    NAMES = SimpleNamespace(
        plan_name="terrapod/plan", policy_name="terrapod/policy", apply_name="terrapod/apply"
    )

    def _agg(self, plan="success", policy="success", apply=None):
        def v(state):
            return Verdict(state, f"{state} description", None)

        return Aggregate(v(plan), v(policy), v(apply) if apply else None)

    def test_a_heading_with_the_short_sha_then_one_line_per_posted_verdict(self):
        lines = render_gates(self._agg(), self.NAMES, "abcdef1234567890")
        assert lines[0] == "**Gates at `abcdef1`**"
        body = [line for line in lines if line.startswith("- ")]
        assert len(body) == 2
        assert "`terrapod/plan`" in body[0] and "success" in body[0]
        assert "`terrapod/policy`" in body[1]

    def test_apply_appears_only_when_there_is_an_apply_verdict(self):
        lines = render_gates(self._agg(apply="pending"), self.NAMES, "a" * 40)
        assert any("`terrapod/apply`" in line for line in lines)
        assert not any(
            "`terrapod/apply`" in line for line in render_gates(self._agg(), self.NAMES, "a" * 40)
        )

    def test_custom_names_render(self):
        names = SimpleNamespace(plan_name="ci/tf-plan", policy_name="ci/tf-policy", apply_name="x")
        text = "\n".join(render_gates(self._agg(apply="success"), names, "a" * 40))
        assert "`ci/tf-plan`" in text and "`ci/tf-policy`" in text and "`x`" in text
        assert "terrapod/plan" not in text

    @pytest.mark.parametrize(
        ("state", "shown"),
        [
            ("running", "pending"),
            ("pending", "pending"),
            ("success", "success"),
            ("failure", "failure"),
        ],
    )
    def test_the_state_shown_is_the_one_the_provider_is_given(self, state, shown):
        line = render_gates(self._agg(plan=state), self.NAMES, "a" * 40)[2]
        assert f"`terrapod/plan`: {shown} " in line
        assert shown == github_state(Verdict(state, "d", None))

    def test_the_description_cannot_break_the_markup(self):
        desc = "a `b` <c> | d\n e"
        agg = Aggregate(Verdict("failure", desc, None), Verdict("success", "ok", None), None)
        lines = render_gates(agg, self.NAMES, "a" * 40)
        assert len(lines) == 4  # heading, blank, two verdicts: the newline did not add a line
        assert "<c>" not in lines[2] and "`b`" not in lines[2]
        assert "d e" in lines[2]

    def test_a_fifty_workspace_block_is_small(self):
        rows = [
            WorkspaceRun(
                f"workspace-{i:02d}-" + "n" * 47,
                "merge_then_apply",
                "errored",
                False,
                False,
                None,
                (),
                None,
            )
            for i in range(50)
        ]
        agg = aggregate(rows, PREvaluation(True, (), (), (), (), False))
        text = "\n".join(render_gates(agg, self.NAMES, "a" * 40))
        assert len(text) < 800


class TestRenderGatesHardening:
    NAMES = SimpleNamespace(
        plan_name="terrapod/plan", policy_name="terrapod/policy", apply_name="terrapod/apply"
    )

    def test_a_mention_in_an_admin_chosen_name_does_not_ping_anyone(self):
        agg = Aggregate(
            Verdict(
                "failure", "Gates passed for 0 of 1 workspaces; ws (@platform-team failed)", None
            ),
            Verdict("success", "ok @octocat", None),
            None,
        )
        text = "\n".join(render_gates(agg, self.NAMES, "a" * 40))
        assert "@platform-team" not in text and "@octocat" not in text
        assert "platform-team" in text and "octocat" in text

    def test_a_note_is_appended_to_the_line_of_its_status(self):
        agg = Aggregate(Verdict("success", "ok", None), Verdict("success", "ok", None), None)
        lines = render_gates(
            agg, self.NAMES, "a" * 40, notes={"terrapod/plan": "not posted yet, retrying"}
        )
        plan = next(line for line in lines if "`terrapod/plan`" in line)
        policy = next(line for line in lines if "`terrapod/policy`" in line)
        assert plan.endswith("(not posted yet, retrying)")
        assert "not posted" not in policy

    def test_the_docstring_does_not_overclaim(self):
        doc = render_gates.__doc__ or ""
        assert "as the provider" not in doc
        assert "database" in doc and "not" in doc
