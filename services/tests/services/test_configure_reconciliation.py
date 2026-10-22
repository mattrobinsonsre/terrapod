"""The reconciler supervises a configure, and its clock reads the right column.

A pure-function tier deliberately: the property is which timestamp the stale
check measures from, and that is decided by a mapping rather than by any
database behaviour. The wrong answer here does not raise -- it silently either
never times a run out or times a healthy one out early -- so it needs asserting
rather than exercising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from terrapod.services.run_reconciler import _phase_of, _phase_start_of

PLAN_AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
APPLY_AT = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
CONFIGURE_AT = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


def _run(
    status: str,
    *,
    plan=PLAN_AT,
    apply=APPLY_AT,
    configure=CONFIGURE_AT,
    apply_finished=None,
):
    run = MagicMock()
    run.status = status
    # Set explicitly: an absent attribute on a MagicMock answers with a distinct
    # auto-attribute, so a mapping that returned the WRONG column would still
    # compare unequal to the right one and the test would pass for no reason.
    run.plan_started_at = plan
    run.apply_started_at = apply
    run.configure_started_at = configure
    # `None` is the ordinary run: `transition_run` stamps this only on the way
    # to `applied`/`errored`, so a run still IN `applying` has it NULL unless it
    # is a composite that has handed over (#1974). Left as a MagicMock
    # attribute it would be truthy and every `applying` run would read as one.
    run.apply_finished_at = apply_finished
    return run


class TestThePhaseIsDerivedNotGuessed:
    def test_configuring_is_the_configure_phase(self):
        """Already true via `LIVE_STATES_FOR_PHASE` (#2131); pinned because the
        reconciler's phase derivation is what names the runner token's phase."""
        assert _phase_of("configuring") == "configure"

    def test_the_other_two_are_unchanged(self):
        assert _phase_of("planning") == "plan"
        assert _phase_of("applying") == "apply"


class TestTheStaleClockReadsTheInFlightPhase:
    def test_a_configure_is_measured_from_its_own_start(self):
        assert _phase_start_of(_run("configuring")) == CONFIGURE_AT

    def test_a_standalone_configure_has_no_apply_to_borrow_from(self):
        """The defect the producer exists for. Before it, `configuring` fell to
        the `else` branch and read `apply_started_at` -- NULL on a standalone
        configure -- so the run had no timestamp to time out against and would
        sit live for ever.
        """
        assert _phase_start_of(_run("configuring", apply=None)) == CONFIGURE_AT

    def test_a_composite_configure_does_not_measure_from_its_apply(self):
        """Worse than the standalone case rather than better.

        A composite run HAS an `apply_started_at`, so the old expression would
        silently measure the configure's age from when its apply began and time
        out a healthy configure early -- a failure that looks like a slow
        playbook rather than like a clock bug.
        """
        assert _phase_start_of(_run("configuring")) != APPLY_AT

    def test_plan_and_apply_are_unchanged(self):
        assert _phase_start_of(_run("planning")) == PLAN_AT
        assert _phase_start_of(_run("applying")) == APPLY_AT

    def test_a_composite_awaiting_its_configure_claim_is_measured_from_the_handover(self):
        """The wait between the two phases of a composite (#1974).

        `complete_apply` leaves such a run in `applying` with `apply_finished_at`
        stamped, waiting for a listener to claim the configure. Measured from
        `apply_started_at` the wait would be judged against the apply's own age,
        which is already past -- so the launch-timeout cohort would error it
        almost immediately and report a stalled apply for an apply that
        succeeded. The wait began when the apply ended.
        """
        handover = datetime(2026, 10, 20, 10, 30, tzinfo=UTC)
        assert _phase_start_of(_run("applying", apply_finished=handover)) == handover

    def test_an_ordinary_applying_run_is_still_measured_from_its_start(self):
        """The branch above must not capture every apply. It cannot: the column
        is stamped on the way OUT of `applying`, so an apply in flight has it
        NULL -- which is what this pins, because a reader could reasonably
        assume otherwise."""
        assert _phase_start_of(_run("applying", apply_finished=None)) == APPLY_AT

    def test_a_status_with_no_live_phase_keeps_the_previous_behaviour(self):
        """`canceling` and the terminals belong to whichever phase was in
        flight, which a status alone cannot say -- so the fallback is the apply's
        start, as it was before, rather than a new answer invented here."""
        assert _phase_start_of(_run("canceling")) == APPLY_AT


class TestCancellingAConfigureInFlight:
    """Stopping a configure is one of the two controls the trigger design rests
    on (#1974): auto-configure is deliberately broad, so visibility and
    reversibility are what make it tolerable.

    The service tier because the property is which terminal a cancelled
    configure reaches and what it writes to the workspace -- both decided from
    the run's own columns, with no database behaviour in it.
    """

    def test_configuring_is_cancelable(self):
        from terrapod.services import run_service

        assert "configuring" in run_service.CANCELABLE_STATES

    def test_the_router_reads_the_services_set_rather_than_a_copy(self):
        """It was a copy, and the copy is what `is-cancelable` and `can-cancel`
        are served from -- so the API would have told every client a configure
        could not be cancelled while accepting the cancel."""
        from terrapod.api.routers import runs as runs_router
        from terrapod.services import run_service

        assert runs_router._CANCELABLE_STATES is run_service.CANCELABLE_STATES

    async def test_a_cancel_mid_configure_goes_through_canceling(self):
        """Not straight to `canceled`: a playbook part-way through a fleet has
        already changed the hosts it reached, so the terminal has to come from
        the observed Job outcome -- the same reason `applying` does."""
        from unittest.mock import AsyncMock

        from terrapod.services import run_service

        run = MagicMock()
        run.status = "configuring"
        run.id = "11111111-1111-1111-1111-111111111111"
        run.discard_reason = None
        with (
            patch.object(run_service, "publish_cancel_job", new_callable=AsyncMock),
            patch.object(run_service, "transition_run", new_callable=AsyncMock) as mock_tx,
        ):
            await run_service.cancel_run(AsyncMock(), run)
        assert mock_tx.await_args.args[2] == "canceling"


class TestResolvingACancelledConfigure:
    """The state-version test is apply-shaped and both of its answers are wrong
    for a configure (#1974)."""

    def _run(self, *, standalone: bool, state_version: bool):
        run = MagicMock()
        run.status = "canceling"
        run.id = "22222222-2222-2222-2222-222222222222"
        run.workspace_id = "33333333-3333-3333-3333-333333333333"
        run.is_configure = True
        run.configure_after_apply = not standalone
        run.configure_started_at = CONFIGURE_AT
        run.configure_finished_at = None
        run.plan_only = False
        self._has_state = state_version
        return run

    async def _resolve(self, run, *, job_status):
        from unittest.mock import AsyncMock

        from terrapod.services import run_service

        db = AsyncMock()
        with patch.object(run_service, "transition_run", new_callable=AsyncMock) as mock_tx:
            mock_tx.side_effect = lambda _db, r, target, **kw: r
            await run_service.resolve_canceling_run(db, run, job_status=job_status)
        return mock_tx

    async def test_a_standalone_configure_killed_cleanly_is_canceled(self):
        mock_tx = await self._resolve(
            self._run(standalone=True, state_version=False), job_status="deleted"
        )
        assert mock_tx.await_args.args[2] == "canceled"

    async def test_a_standalone_configure_never_flags_state_divergence(self):
        """The apply-shaped path sets the workspace's `state_diverged` flag when
        a cancel finds no state version -- and a playbook cannot move
        tofu/terraform state, so that would be a false drift signal on a
        workspace whose state is provably untouched. The configure branch
        returns before the state-version query, which is what this pins: the
        resolution never reads the database at all.
        """
        from unittest.mock import AsyncMock

        from terrapod.services import run_service

        run = self._run(standalone=True, state_version=False)
        db = AsyncMock()
        with patch.object(run_service, "transition_run", new_callable=AsyncMock) as mock_tx:
            mock_tx.side_effect = lambda _db, r, target, **kw: r
            await run_service.resolve_canceling_run(db, run, job_status="deleted")
        db.execute.assert_not_awaited()

    async def test_a_composite_is_errored_rather_than_canceled(self):
        """Its apply LANDED, so `canceled` would read as "nothing happened" on a
        run that changed infrastructure -- while `applied` would claim the
        configure finished. `_apply_status` already maps `errored` plus
        `configure_started_at` to an apply that finished, so the CLI is told the
        infrastructure change landed and the configure did not.
        """
        mock_tx = await self._resolve(
            self._run(standalone=False, state_version=True), job_status="deleted"
        )
        assert mock_tx.await_args.args[2] == "errored"
        assert "apply had already succeeded" in mock_tx.await_args.kwargs["error_message"]

    async def test_a_failed_configure_job_errors_either_shape(self):
        for standalone in (True, False):
            mock_tx = await self._resolve(
                self._run(standalone=standalone, state_version=False), job_status="failed"
            )
            assert mock_tx.await_args.args[2] == "errored", standalone
