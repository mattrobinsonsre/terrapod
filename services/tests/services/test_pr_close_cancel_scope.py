"""A PR going away must not cancel a FINISHED speculative plan (#2173).

Two sweeps fire when a pull request stops being open, and both asked
`Run.status.notin_(TERMINAL_STATES)`. `planned` is not terminal, so the
complement swept up every run that had *finished* planning -- the speculative
plan a reviewer was reading, and, in the module-impact case, the blast-radius
analysis somebody merged the PR in order to act on. `force=True` then stepped
over `cancel_run`'s own guard, and no `reason=` was passed, so the row came out
identical to a user-initiated cancel and the symptom read as flakiness.

**The fix is narrower than "stop cancelling finished plans", and the first
attempt at it was wrong in a way worth recording.** Excluding every `planned`
run also excludes the apply-capable ones, and `claim_next_run`'s dispatch gate
blocks a workspace on any other run that is `plan_only=False` and in
`planning/planned/confirmed/applying/canceling`. So an apply-then-merge plan
abandoned by a closed PR would have held that workspace against every other
pull request for ever, with nothing else to clear it. `docs/vcs-workflows.md`
states that lock in as many words; the doc was right and the inference from the
staleness queries (which do exclude `plan_only`) was wrong.

Hence `pr_closure_cancels`, a Python predicate with a SQL twin, following
`is_deferred_saved_plan` / `_not_deferred_saved_plan`. The truth table is the
property; the twin is proven against a real database in
`tests/integration/test_pr_close_cancel_scope.py`, because a mocked session
cannot show which rows a WHERE clause selects.

**The two SUPERSEDE sweeps are deliberately the other way round.** A new head
commit makes the old plan a plan for code that no longer exists, so cancelling
it in `planned` is the point -- which is what `force`'s docstring describes as
its reason to exist. The invariant turns on *why* a sweep fires:

    the PR went AWAY  -> keep a finished speculative plan
    the PR MOVED ON   -> cancel it; it is stale by construction

**Blind spot, stated rather than left to be found:** this gate names the two
closure sweeps. A third added elsewhere would not be checked, because nothing
in the code marks a sweep as closure-triggered -- `_cancel_stale_module_runs`
and the supersede block in `_process_module_pr` differ only in a comment.
"""

import ast
import inspect
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import module_impact_service, run_service, vcs_poller

#: The two sweeps that fire because a pull request is no longer open.
CLOSURE_SWEEPS = (
    vcs_poller._reconcile_closed_pr_sessions,
    module_impact_service._cancel_stale_module_runs,
)

#: A matcher that stops matching must fail rather than pass on nothing.
MIN_SWEEPS = 2


def _run(status, *, plan_only):
    return MagicMock(id="run-1", status=status, plan_only=plan_only)


class TestWhatAClosedPRTakesWithIt:
    """The truth table IS the fix. Each row is a different kind of mistake."""

    def test_a_finished_speculative_plan_survives(self):
        """The defect: a result somebody is reading, deleted by a merge."""
        assert not run_service.pr_closure_cancels(_run("planned", plan_only=True))

    def test_a_running_speculative_plan_is_still_cancelled(self):
        """The over-correction: work nobody is waiting for, left running."""
        assert run_service.pr_closure_cancels(_run("planning", plan_only=True))
        assert run_service.pr_closure_cancels(_run("queued", plan_only=True))
        assert run_service.pr_closure_cancels(_run("pending", plan_only=True))

    def test_a_finished_APPLY_CAPABLE_plan_is_still_cancelled(self):
        """The other over-correction, and the expensive one.

        `claim_next_run` blocks a workspace on another `plan_only=False` run in
        `planned`. Leaving this one holds the workspace against every other pull
        request for ever, because this sweep is the only thing that clears it.
        """
        assert run_service.pr_closure_cancels(_run("planned", plan_only=False))

    def test_a_run_mid_apply_is_still_cancelled(self):
        assert run_service.pr_closure_cancels(_run("applying", plan_only=False))
        assert run_service.pr_closure_cancels(_run("confirmed", plan_only=False))

    def test_a_terminal_run_is_never_touched(self):
        for status in run_service.TERMINAL_STATES:
            assert not run_service.pr_closure_cancels(_run(status, plan_only=True))
            assert not run_service.pr_closure_cancels(_run(status, plan_only=False))


class TestAClosureCancelIsLegible:
    """`force=True` with no reason is what made the symptom read as flakiness."""

    def _session_db(self, runs):
        db = AsyncMock()
        sess = MagicMock(pr_number=7, state="open")
        sessions = MagicMock()
        sessions.scalars.return_value.all.return_value = [sess]
        run_result = MagicMock()
        run_result.scalars.return_value.all.return_value = runs
        db.execute = AsyncMock(side_effect=[sessions] + [run_result] * 8)
        return db, sess

    async def test_the_workspace_sweep_passes_a_reason(self):
        db, _ = self._session_db([_run("planning", plan_only=True)])
        with patch.object(run_service, "cancel_run", AsyncMock()) as cancel:
            await vcs_poller._reconcile_closed_pr_sessions(
                db, MagicMock(id="conn-1"), "org/repo", set()
            )
        cancel.assert_awaited_once()
        assert "#7" in cancel.await_args.kwargs["reason"], cancel.await_args.kwargs

    async def test_the_module_sweep_passes_a_reason(self):
        db = AsyncMock()
        runs = MagicMock()
        runs.scalars.return_value.all.return_value = [_run("planning", plan_only=True)]
        db.execute = AsyncMock(return_value=runs)
        module = MagicMock(name="m", workspace_links=[MagicMock(workspace_id="ws-1")])
        with patch.object(run_service, "cancel_run", AsyncMock()) as cancel:
            await module_impact_service._cancel_stale_module_runs(db, module, 7)
        cancel.assert_awaited_once()
        assert "#7" in cancel.await_args.kwargs["reason"], cancel.await_args.kwargs

    async def test_a_run_that_goes_terminal_mid_sweep_is_not_logged_as_a_failure(self):
        """The race `force=True` does not cover: terminal between select and cancel.

        Leaving it is correct, so it is a deliberate outcome rather than a
        failure. Asserting the session still closes does NOT pin that -- the
        broad `except Exception` catches it too, so removing the dedicated
        handler leaves every behavioural assertion green (measured). The log is
        what distinguishes them, and calling a correct outcome a failure is how
        an operator learns to ignore the line that would have explained a real
        one.
        """
        db, sess = self._session_db([_run("planning", plan_only=True)])
        boom = AsyncMock(side_effect=ValueError("Cannot cancel run in terminal state"))
        with (
            patch.object(run_service, "cancel_run", boom),
            patch.object(vcs_poller, "logger") as log,
        ):
            await vcs_poller._reconcile_closed_pr_sessions(
                db, MagicMock(id="conn-1"), "org/repo", set()
            )
        assert sess.state == "closed", "the session close was abandoned"
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert not warned, f"a deliberate outcome was logged as a failure: {warned}"
        informed = [c.args[0] for c in log.info.call_args_list]
        assert any("no longer cancelable" in m for m in informed), informed


class TestTheTwoKindsOfSweepStaySeparate:
    """Static backstop: the driven tests cannot see one rule become two."""

    def test_both_closure_sweeps_use_the_shared_predicate(self):
        assert len(CLOSURE_SWEEPS) >= MIN_SWEEPS
        for fn in CLOSURE_SWEEPS:
            src = textwrap.dedent(inspect.getsource(fn))
            code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
            assert "pr_closure_cancels_sql" in code, fn.__qualname__
            assert "notin_(run_service.TERMINAL_STATES)" not in code, fn.__qualname__

            # By AST, not by substring: `reason=` also appears in the
            # `except ValueError` log line, so a substring check is satisfied
            # with the reason dropped from `cancel_run` itself -- which is the
            # mutation that found this assertion weaker than it looked.
            calls = [
                n
                for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "cancel_run"
            ]
            assert len(calls) == 1, f"{fn.__qualname__}: {len(calls)} cancel_run calls"
            kwargs = {kw.arg for kw in calls[0].keywords}
            assert "reason" in kwargs, f"{fn.__qualname__}: {kwargs}"
            assert "force" in kwargs, (
                f"{fn.__qualname__}: force is required -- `cancel_run` refuses "
                f"`planned`, and an apply-capable `planned` run must still be "
                f"cancelled or it holds the workspace: {kwargs}"
            )

    def test_the_supersede_sweeps_still_cancel_a_superseded_plan(self):
        """If this fails, someone has "fixed" the wrong two sites.

        A superseded plan is for code that no longer exists, so cancelling it in
        `planned` is the point. Asserting it keeps the fix from spreading by
        sympathy to the sweeps that are correct as they are.
        """
        superseders = 0
        for mod in (vcs_poller, module_impact_service):
            src = pathlib_read(mod)
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if node.name in {f.__name__ for f in CLOSURE_SWEEPS}:
                    continue
                body = ast.get_source_segment(src, node) or ""
                if "notin_(run_service.TERMINAL_STATES)" in body and "force=True" in body:
                    superseders += 1
        assert superseders == 2, (
            f"expected the two supersede sweeps to keep cancelling superseded "
            f"plans, found {superseders}"
        )


def pathlib_read(mod):
    import pathlib

    return pathlib.Path(mod.__file__).read_text()


def test_the_reason_is_one_producer():
    """Two sites, one sentence -- or they drift into describing one event twice."""
    assert run_service.pr_closed_cancel_reason(7) == "Pull request #7 is no longer open"
    for fn in CLOSURE_SWEEPS:
        assert "pr_closed_cancel_reason" in inspect.getsource(fn), fn.__qualname__
