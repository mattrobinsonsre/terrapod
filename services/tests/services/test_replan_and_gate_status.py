"""Two fixes whose original tests could not see the bug (#1831).

Both #1795 and #1798 shipped with tests that exercised the new code directly
and never drove the caller, so both headline fixes were inert in the case they
were written for. These drive the callers.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import run_service

# ── the re-plan dedup (#1795) ─────────────────────────────────────────


class TestAnExplicitReplanIsBlockedOnlyByALiveRun:
    """`terrapod plan` was a permanent no-op after a plan ERRORED.

    `_route_plan` cancels runs `notin_(TERMINAL_STATES)`, so after an errored
    run there is nothing to cancel -- and the dedup excluded only `canceled`,
    so the errored run still matched and `_create_vcs_run` returned None. The
    author was told nothing, and because the dedup keys on the SHA, every
    later `terrapod plan` on that commit did nothing too. The only escape was
    a new commit, which is the symptom #1795 set out to remove.

    The original test mocked `_create_vcs_run` wholesale and asserted only
    that `replaces_canceled=True` was passed, so it never ran this query.
    """

    def test_every_terminal_state_is_excluded_not_just_canceled(self):
        """Read the predicate rather than the comment.

        A terminal run is history, not coverage: none of applied, errored,
        discarded or canceled means "a plan for this SHA is on its way".
        """
        import inspect

        src = inspect.getsource(run_service)
        assert 'TERMINAL_STATES = {"applied", "errored", "discarded", "canceled"}' in src

        from terrapod.services import vcs_poller

        dedup = inspect.getsource(vcs_poller._create_vcs_run)
        assert "if replaces_canceled:" in dedup
        assert "notin_(run_service.TERMINAL_STATES)" in dedup, (
            "the re-plan dedup must exclude every terminal state. Excluding "
            "only `canceled` makes `terrapod plan` a no-op after an errored "
            "or discarded run -- the most likely reason to type it."
        )
        assert 'Run.status != "canceled"' not in dedup


# ── the gate-hold commit status (#1798) ───────────────────────────────


def _run(**kw):
    base = {
        "id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
        "is_drift_detection": False,
        "vcs_commit_sha": "a" * 40,
        "has_changes": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestAGateTakingHoldRefreshesTheCommitStatus:
    """A held run stays in `planning` and `complete_plan` returns WITHOUT a
    transition, so the ordinary enqueue in `transition_run` never fires for
    it. The only `planning` status a PR received came from `queued ->
    planning`, before `plan_finished_at` was set, when `blocked_by` still
    answers None -- so #1798's gate vocabulary could never render and the
    check sat on "Plan in progress" for as long as the gate held.
    """

    async def test_it_enqueues_a_planning_refresh(self):
        run = _run()
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enqueue:
            await run_service._enqueue_gate_hold_status(run)

        enqueue.assert_awaited_once()
        name, payload = enqueue.await_args.args[0], enqueue.await_args.args[1]
        assert name == "vcs_commit_status"
        assert payload["target_status"] == "planning"
        assert payload["run_id"] == str(run.id)

    async def test_its_dedup_key_cannot_collide_with_the_plan_start_status(self):
        """Load-bearing. `_enqueue_vcs_status` keys on
        `vcs_status:{run}:{target}` with a 60s TTL, so a plan that finishes
        within a minute of starting -- the common case -- would have this
        silently swallowed as a duplicate of its own plan-start status."""
        run = _run()
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enqueue:
            await run_service._enqueue_gate_hold_status(run)

        key = enqueue.await_args.kwargs["dedup_key"]
        assert key == f"vcs_status:gate:{run.id}"
        assert key != f"vcs_status:{run.id}:planning", (
            "sharing the plan-start dedup key silently swallows this refresh "
            "for any plan that finishes inside the 60s TTL"
        )

    async def test_a_drift_run_is_left_alone(self):
        """Drift SHAs are already-merged default-branch HEADs; posting there
        is noise on commits nobody is reviewing."""
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enqueue:
            await run_service._enqueue_gate_hold_status(_run(is_drift_detection=True))
        enqueue.assert_not_awaited()

    async def test_a_non_vcs_run_is_left_alone(self):
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enqueue:
            await run_service._enqueue_gate_hold_status(_run(vcs_commit_sha=None))
        enqueue.assert_not_awaited()

    async def test_an_enqueue_failure_never_breaks_the_gate(self):
        """Best effort: a status update is not worth failing a run over."""
        with patch(
            "terrapod.services.scheduler.enqueue_trigger",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ):
            await run_service._enqueue_gate_hold_status(_run())  # must not raise

    @pytest.mark.parametrize(
        "gate",
        # Three on this line: the AI policy gate arrived with #1766, which
        # 1.7 does not carry, so there is no fourth holding return to guard.
        ["run-task", "policy", "security-scan"],
    )
    def test_every_gate_hold_return_refreshes_the_status(self, gate):
        """Enumerated from the source, not listed by hand.

        Every `return run` in `complete_plan` that leaves the run held must
        be preceded by the refresh. A gate added later without one is the
        same defect again, silently.
        """
        import inspect

        # Where the gates live differs by line: 1.8+ delegates from
        # `complete_plan` to `_complete_plan`, 1.7 does not. Read whichever
        # exists, because reading the WRAPPER finds nothing and the assertion
        # would pass vacuously -- the same delegation trap that produced a
        # false audit against `create_workspace` rather than
        # `_create_workspace_impl`.
        impl = getattr(run_service, "_complete_plan", run_service.complete_plan)
        src = inspect.getsource(impl)
        assert src.count("await _enqueue_gate_hold_status(run)") == 3, (
            "expected one gate-hold status refresh per holding gate "
            f"(run-task, policy, security-scan); found "
            f"{src.count('await _enqueue_gate_hold_status(run)')}"
        )
