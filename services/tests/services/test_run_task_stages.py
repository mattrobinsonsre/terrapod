"""The pre_plan and pre_apply run task boundaries (#1837).

`pre_plan` and `pre_apply` were valid stage names from the beginning —
`VALID_STAGES` has held all three since run tasks shipped — but nothing ever
created a stage at either, so a task configured there was accepted, stored,
listed in the UI, and silently never called. The name being valid is what made
it look implemented.

The three boundaries differ in one way that matters beyond where they sit:
**only `post_plan` can be overridden.** A `pre_apply` verdict is a go/no-go
taken before any infrastructure moves, and the platform already opens itself to
human intervention there — the run sits `planned` for a person to confirm or
discard — so an override would add a second, weaker escape from a decision that
is meant to be final. `pre_plan` is the same answer for a simpler reason:
nothing has executed yet, so fixing the cause and queueing again costs nothing.
Operators who want a waivable gate put the task at `post_plan`, which keeps its
override. See docs/run-tasks.md.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import run_service, run_task_service


def _stage(stage="post_plan", status="failed"):
    return SimpleNamespace(id=uuid.uuid4(), stage=stage, status=status, results=[])


def _run(**kw):
    base = {"id": uuid.uuid4(), "workspace_id": uuid.uuid4()}
    base.update(kw)
    return SimpleNamespace(**base)


# ── the shared predicate ──────────────────────────────────────────────


class TestEvaluateGate:
    """One predicate behind all three boundaries, so they cannot drift."""

    async def test_a_boundary_nobody_configured_passes(self):
        """`create_task_stage` returns None when the workspace has no enabled
        task at this boundary. That must read as "proceed", not as a hold —
        otherwise turning on the feature would stop every run everywhere."""
        with patch.object(run_task_service, "create_task_stage", AsyncMock(return_value=None)):
            verdict = await run_task_service.evaluate_gate(AsyncMock(), _run(), "pre_plan")
        assert verdict == run_task_service.GATE_PASSED

    @pytest.mark.parametrize(
        ("stage_status", "expected"),
        [
            ("passed", run_task_service.GATE_PASSED),
            ("overridden", run_task_service.GATE_PASSED),
            ("failed", run_task_service.GATE_FAILED),
            ("running", run_task_service.GATE_RUNNING),
        ],
    )
    async def test_it_maps_each_stage_status(self, stage_status, expected):
        with (
            patch.object(run_task_service, "create_task_stage", AsyncMock(return_value=_stage())),
            patch.object(run_task_service, "resolve_stage", AsyncMock(return_value=stage_status)),
        ):
            verdict = await run_task_service.evaluate_gate(AsyncMock(), _run(), "pre_apply")
        assert verdict == expected

    async def test_an_overridden_stage_still_counts_as_passed(self):
        """`post_plan` keeps its override, and this is the predicate every
        boundary shares — so "overridden" has to mean "proceed" here even
        though the two new boundaries can never reach that state."""
        with (
            patch.object(run_task_service, "create_task_stage", AsyncMock(return_value=_stage())),
            patch.object(run_task_service, "resolve_stage", AsyncMock(return_value="overridden")),
        ):
            verdict = await run_task_service.evaluate_gate(AsyncMock(), _run(), "post_plan")
        assert verdict == run_task_service.GATE_PASSED


# ── no override at the two new boundaries ─────────────────────────────


class TestOnlyPostPlanCanBeOverridden:
    """Enforced in `override_stage`, not at the router.

    Until this issue `post_plan` was the only stage anything created, so the
    override path never had to ask which boundary it was looking at. The
    moment `pre_plan` and `pre_apply` stages begin to exist, that same
    endpoint would have started accepting them — shipping an override for both
    without anyone deciding to add one.
    """

    @pytest.mark.parametrize("stage_name", ["pre_plan", "pre_apply"])
    async def test_the_final_boundaries_refuse(self, stage_name):
        db = AsyncMock()
        ts = _stage(stage=stage_name, status="failed")
        with patch.object(run_task_service, "get_task_stage", AsyncMock(return_value=ts)):
            with pytest.raises(ValueError, match="cannot be overridden"):
                await run_task_service.override_stage(db, ts.id)
        assert ts.status == "failed", "a refused override must not mutate the stage"

    async def test_the_refusal_says_where_an_overridable_gate_goes(self):
        """A bare "not allowed" leaves the operator with no move. The
        capability is not lost — it is a property of the boundary they chose."""
        db = AsyncMock()
        ts = _stage(stage="pre_apply", status="failed")
        with patch.object(run_task_service, "get_task_stage", AsyncMock(return_value=ts)):
            with pytest.raises(ValueError, match="post_plan"):
                await run_task_service.override_stage(db, ts.id)

    async def test_post_plan_is_unchanged(self):
        db = AsyncMock()
        ts = _stage(stage="post_plan", status="failed")
        with patch.object(run_task_service, "get_task_stage", AsyncMock(return_value=ts)):
            result = await run_task_service.override_stage(db, ts.id)
        assert result.status == "overridden"

    def test_the_allow_list_is_exactly_post_plan(self):
        assert run_task_service.OVERRIDABLE_STAGES == frozenset({"post_plan"})

    async def test_the_boundary_is_checked_before_the_status(self):
        """Order matters for the message. A failed `pre_apply` stage checked
        status-first would answer "can only override failed stages" for a
        stage that IS failed, or — worse — succeed."""
        db = AsyncMock()
        ts = _stage(stage="pre_apply", status="running")
        with patch.object(run_task_service, "get_task_stage", AsyncMock(return_value=ts)):
            with pytest.raises(ValueError, match="cannot be overridden"):
                await run_task_service.override_stage(db, ts.id)


# ── pre_plan: the dispatcher holds the run ────────────────────────────


class TestThePrePlanGateHoldsTheRunBeforeItPlans:
    """The listener's own poll loop is the re-drive at this boundary.

    Each boundary needs a driver that comes back and asks again, and they do
    not share one: `post_plan` has the reconciler, `pre_apply` has the run task
    callback, `pre_plan` has a listener polling for work.
    """

    async def test_a_mandatory_failure_errors_the_run(self):
        """Final, with no override. Nothing has executed, so the escape is to
        fix the cause and queue again — and the run must say that rather than
        sitting `queued` forever."""
        run = _run(status="queued")
        db = AsyncMock()
        candidates = MagicMock()
        candidates.scalars.return_value.all.return_value = [run]
        # The failure path re-reads the run under a row lock before erroring
        # it, so the candidate object cannot be acted on while stale.
        locked = MagicMock()
        locked.scalar_one_or_none.return_value = run
        db.execute = AsyncMock(side_effect=[candidates, locked])

        with (
            patch.object(
                run_task_service,
                "evaluate_gate",
                AsyncMock(return_value=run_task_service.GATE_FAILED),
            ),
            patch.object(
                run_task_service, "failed_task_summary", AsyncMock(return_value="compliance-check")
            ),
            patch.object(run_service, "transition_run", AsyncMock()) as transition,
        ):
            await run_service._open_pre_plan_stages(db, uuid.uuid4())

        transition.assert_awaited_once()
        assert transition.await_args.args[2] == "errored"
        message = transition.await_args.kwargs["error_message"]
        assert "compliance-check" in message, "name the task that failed"
        assert "final" in message

    async def test_a_running_gate_leaves_the_run_queued(self):
        run = _run()
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [run]
        db.execute = AsyncMock(return_value=result)

        with (
            patch.object(
                run_task_service,
                "evaluate_gate",
                AsyncMock(return_value=run_task_service.GATE_RUNNING),
            ),
            patch.object(run_service, "transition_run", AsyncMock()) as transition,
        ):
            await run_service._open_pre_plan_stages(db, uuid.uuid4())

        transition.assert_not_awaited(), "a pending verdict is not a failure"

    async def test_one_runs_gate_error_does_not_stop_the_loop(self):
        """The pre-pass runs on every poll, so one run's gate blowing up must
        not stop the others being considered.

        **This proves less than its old name claimed.** It drove an
        `AsyncMock` db, where there is no transaction state to poison and no
        claim afterwards — so it passed happily while the real failure (a
        session left needing a rollback, which then breaks the claim query and
        500s the endpoint for the whole pool) went unnoticed. The property it
        cannot reach lives in
        `test_pre_plan_gate_integration.py::TestThePrePassCannotStarveTheListener`,
        against real Postgres. Kept for the cheap loop-continues check only.
        """
        bad, good = _run(), _run()
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [bad, good]
        db.execute = AsyncMock(return_value=result)

        calls = []

        async def _flaky(_db, run, _stage):
            calls.append(run.id)
            if run.id == bad.id:
                raise RuntimeError("webhook dispatch blew up")
            return run_task_service.GATE_RUNNING

        with patch.object(run_task_service, "evaluate_gate", _flaky):
            await run_service._open_pre_plan_stages(db, uuid.uuid4())

        assert calls == [bad.id, good.id], "the second run must still be evaluated"

    def test_the_claim_query_excludes_on_the_task_not_the_stage_row(self):
        """The window that matters is between a run becoming `queued` and its
        stage being created: there IS no stage row then, so a predicate keyed
        on "an unresolved stage exists" finds nothing to exclude and hands the
        run to a listener — running the very plan the gate exists to hold.

        Keying on "the workspace wants a gate AND this run has not cleared
        one" is race-proof without depending on the pre-pass having run.
        """
        import inspect

        src = inspect.getsource(run_service.claim_next_run)
        assert "wants_pre_plan" in src and "cleared_pre_plan" in src
        assert 'RunTask.stage == "pre_plan"' in src, (
            "the exclusion must key on an enabled pre_plan RunTask existing, "
            "so a run with no stage row yet is still held"
        )
        assert "not_(wants_pre_plan)" in src

    def test_the_pre_pass_runs_before_any_row_lock_is_taken(self):
        """`create_task_stage` commits internally — it must, so the rows are
        visible to the webhook consumer before the trigger fires. A commit
        inside the claim transaction would release the `FOR UPDATE SKIP
        LOCKED` locks that make the claim exactly-once."""
        import inspect

        src = inspect.getsource(run_service.claim_next_run)
        pre_pass = src.index("_open_pre_plan_stages(db, pool_id)")
        lock = src.index("with_for_update")
        assert pre_pass < lock, (
            "the stage-opening pre-pass must run before the claim loop takes row locks"
        )


# ── pre_apply: the callback is the re-drive ───────────────────────────


class TestRedriveAutoApply:
    """Without this a held auto-applying run parks in `planned` forever.

    The reconciler only works runs in `planning`/`applying`, and
    `_complete_plan` returns early once a run is `planned` — so nothing
    re-enters the auto-apply decision after the gate clears.
    """

    async def test_it_declines_a_run_that_is_not_planned(self):
        for status in ("planning", "confirmed", "applied", "errored"):
            run = _run(status=status, plan_only=False)
            with patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as gate:
                await run_service.redrive_auto_apply(AsyncMock(), run)
            gate.assert_not_awaited(), f"status={status} has nothing to re-drive"

    async def test_it_declines_a_plan_only_run(self):
        run = _run(status="planned", plan_only=True)
        with patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as gate:
            await run_service.redrive_auto_apply(AsyncMock(), run)
        gate.assert_not_awaited()

    async def test_always_mode_goes_through_the_shared_guard(self):
        run = _run(status="planned", plan_only=False)
        with (
            patch.object(run_service, "resolve_auto_apply_mode", return_value="always"),
            patch.object(
                run_service, "_auto_apply_if_permitted", AsyncMock(return_value=run)
            ) as gate,
            patch.object(run_service, "evaluate_conditional_auto_apply", AsyncMock()),
        ):
            await run_service.redrive_auto_apply(AsyncMock(), run)
        (
            gate.assert_awaited_once(),
            (
                "must reuse _auto_apply_if_permitted so staleness and the manual "
                "lock are re-checked, not just the gate"
            ),
        )

    async def test_a_conditional_mode_is_settled_by_its_own_evaluator(self):
        run = _run(status="planned", plan_only=False)
        with (
            patch.object(run_service, "resolve_auto_apply_mode", return_value="create"),
            patch.object(run_service, "_auto_apply_if_permitted", AsyncMock()) as always,
            patch.object(
                run_service, "evaluate_conditional_auto_apply", AsyncMock(return_value=run)
            ) as conditional,
        ):
            await run_service.redrive_auto_apply(AsyncMock(), run)
        always.assert_not_awaited()
        conditional.assert_awaited_once()

    async def test_a_run_that_just_confirmed_is_not_evaluated_twice(self):
        """`always` reaching `confirmed` must not then fall into the
        conditional evaluator — the status guard is what stops it."""
        run = _run(status="planned", plan_only=False)

        async def _confirm(_db, r):
            r.status = "confirmed"
            return r

        with (
            patch.object(run_service, "resolve_auto_apply_mode", return_value="always"),
            patch.object(run_service, "_auto_apply_if_permitted", _confirm),
            patch.object(run_service, "evaluate_conditional_auto_apply", AsyncMock()) as cond,
        ):
            await run_service.redrive_auto_apply(AsyncMock(), run)
        cond.assert_not_awaited()


class TestThePreApplyMessageIsActionable:
    async def test_a_failure_names_the_task_and_says_it_is_final(self):
        run = _run()
        with (
            patch.object(
                run_task_service,
                "evaluate_gate",
                AsyncMock(return_value=run_task_service.GATE_FAILED),
            ),
            patch.object(
                run_task_service, "failed_task_summary", AsyncMock(return_value="cost-guard")
            ),
        ):
            verdict, message = await run_service.pre_apply_gate(AsyncMock(), run)
        assert verdict == "failed"
        assert "cost-guard" in message
        assert "final" in message
        assert "discard" in message, "say what the operator can actually do"

    async def test_a_running_gate_says_so_without_claiming_failure(self):
        run = _run()
        with patch.object(
            run_task_service,
            "evaluate_gate",
            AsyncMock(return_value=run_task_service.GATE_RUNNING),
        ):
            verdict, message = await run_service.pre_apply_gate(AsyncMock(), run)
        assert verdict == "running"
        assert "failed" not in message


# ── the unreachable-callback wedge ────────────────────────────────────


class TestAResultThatCanNoLongerBeReportedIsExpired:
    """The wedge #1837 turned from bounded into unbounded.

    `run_task_dispatcher` leaves a result `running` on a 2xx, and
    `resolve_stage` holds the stage while any result is non-terminal. Nothing
    aged a result out, so an external service that accepts the webhook and
    never calls back held the run forever — and ENFORCEMENT LEVEL DID NOT
    MATTER, because the stage never resolved at all.

    The deadline is the callback token's own TTL, which makes it a fact rather
    than a policy: past it `verify_callback_token` refuses, so the service
    cannot report back even if it tries.
    """

    def _result(self, *, age_seconds: int, status: str = "running"):
        from terrapod.db.models import now_utc

        return SimpleNamespace(
            status=status,
            message="",
            finished_at=None,
            created_at=now_utc() - timedelta(seconds=age_seconds),
            run_task=SimpleNamespace(enforcement_level="mandatory", name="slow-task"),
        )

    def test_a_result_past_its_token_lifetime_is_errored(self):
        stage = SimpleNamespace(status="running", results=[self._result(age_seconds=3601)])
        assert run_task_service.expire_unreachable_results(stage) == 1
        assert stage.results[0].status == "errored"
        assert "callback token expired" in stage.results[0].message

    def test_a_result_still_within_its_lifetime_is_left_alone(self):
        stage = SimpleNamespace(status="running", results=[self._result(age_seconds=60)])
        assert run_task_service.expire_unreachable_results(stage) == 0
        assert stage.results[0].status == "running"

    def test_an_advisory_task_expires_too(self):
        """Advisory means "does not BLOCK on failure", not "cannot hold the
        run". An advisory task that never answers wedges the stage exactly as
        hard as a mandatory one, because resolution waits on every result."""
        r = self._result(age_seconds=7200)
        r.run_task = SimpleNamespace(enforcement_level="advisory", name="advisory-task")
        stage = SimpleNamespace(status="running", results=[r])
        assert run_task_service.expire_unreachable_results(stage) == 1
        assert r.status == "errored"

    def test_terminal_results_are_untouched(self):
        for status in ("passed", "failed", "errored", "unreachable"):
            r = self._result(age_seconds=99999, status=status)
            stage = SimpleNamespace(status="running", results=[r])
            assert run_task_service.expire_unreachable_results(stage) == 0
            assert r.status == status

    def test_a_naive_created_at_does_not_crash_the_sweep(self):
        """Defensive: a row read back without tzinfo must not raise inside
        what is otherwise a liveness backstop."""
        from datetime import datetime

        r = self._result(age_seconds=7200)
        r.created_at = datetime.utcnow() - timedelta(seconds=7200)  # noqa: DTZ003
        stage = SimpleNamespace(status="running", results=[r])
        assert run_task_service.expire_unreachable_results(stage) == 1

    def test_the_deadline_is_the_callback_token_ttl(self):
        """Not an independent number. A tunable timeout would only let an
        operator pick one that disagrees with the token, producing results
        that are 'still waiting' for a callback that would be refused."""
        import inspect

        src = inspect.getsource(run_task_service.expire_unreachable_results)
        assert "_CALLBACK_TOKEN_TTL" in src


# ── a held run must not read as unblocked ─────────────────────────────


class TestBlockedByAnswersForTheNewBoundaries:
    """The `pre_apply` case is a safety issue, not a cosmetic one.

    A run held there sits in `planned`, which every consumer reads as "plan
    succeeded, waiting for a human". So the PR check reported SUCCESS: a
    required check passed and the PR looked mergeable while the run was held
    and would never apply. Someone could merge on the strength of it.

    #1831 in this same release exists because a held run's check sat on
    "Plan in progress"; these two boundaries reintroduced that class.
    """

    async def test_a_run_held_before_its_plan_names_the_gate(self):
        run = _run(status="queued", plan_finished_at=None)
        held = _stage(stage="pre_plan", status="running")
        with patch.object(run_task_service, "_existing_stage", AsyncMock(return_value=held)):
            assert await run_service.blocked_by(AsyncMock(), run) == "run-task"

    async def test_a_run_held_before_its_apply_names_the_gate(self):
        run = _run(status="planned", plan_finished_at=None)
        held = _stage(stage="pre_apply", status="running")
        with patch.object(run_task_service, "_existing_stage", AsyncMock(return_value=held)):
            assert await run_service.blocked_by(AsyncMock(), run) == "run-task"

    @pytest.mark.parametrize("cleared", ["passed", "overridden"])
    async def test_a_cleared_gate_does_not_report_a_block(self, cleared):
        run = _run(status="planned", plan_finished_at=None)
        with patch.object(
            run_task_service,
            "_existing_stage",
            AsyncMock(return_value=_stage(stage="pre_apply", status=cleared)),
        ):
            assert await run_service.blocked_by(AsyncMock(), run) is None

    async def test_an_ordinary_planned_run_is_not_blocked(self):
        """The overwhelmingly common case: no stage at all. It must not start
        reporting a block, or every run awaiting confirm looks gated."""
        run = _run(status="planned", plan_finished_at=None)
        with patch.object(run_task_service, "_existing_stage", AsyncMock(return_value=None)):
            assert await run_service.blocked_by(AsyncMock(), run) is None

    def test_is_held_at_gate_is_deliberately_not_widened(self):
        """It means "a finished plan waiting for a decision" and governs
        discardability and whether the CLI waits (#1725). A `queued` run has
        no plan yet; a `planned` one is already discardable the ordinary way.
        Widening it to cover the new boundaries would change both."""
        assert run_service.is_held_at_gate(_run(status="queued", plan_finished_at=None)) is False
        assert run_service.is_held_at_gate(_run(status="planned", plan_finished_at=None)) is False

    async def test_resolve_stage_actually_applies_the_expiry(self):
        """Drives `resolve_stage`, not the helper.

        Testing the helper alone proves it can expire a result, not that
        anything ever calls it — and an expiry nothing invokes fixes no wedge.
        Caught by mutation: neutering the call inside `resolve_stage` left
        every direct-call test above passing.
        """
        from terrapod.db.models import now_utc

        overdue = SimpleNamespace(
            status="running",
            message="",
            finished_at=None,
            created_at=now_utc() - timedelta(seconds=7200),
            run_task=SimpleNamespace(enforcement_level="mandatory", name="silent-task"),
        )
        stage = SimpleNamespace(
            id=uuid.uuid4(), stage="pre_apply", status="running", results=[overdue]
        )
        db = AsyncMock()
        with patch.object(run_task_service, "get_task_stage", AsyncMock(return_value=stage)):
            status = await run_task_service.resolve_stage(db, stage.id)

        assert status == "failed", (
            "a stage whose only result can no longer be reported must resolve, "
            "not stay `running` forever"
        )
        assert overdue.status == "errored"
